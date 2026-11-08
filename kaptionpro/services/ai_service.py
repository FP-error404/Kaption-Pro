"""Layanan AI: transkripsi (Whisper) dan penerjemahan (backend berlapis)."""

from __future__ import annotations

import random
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

from ..config import (
    APP_DIR,
    HAS_ARGOS,
    HAS_DEEP_TRANSLATOR,
    HAS_FASTER_WHISPER,
    TRANSLATORS,
    _FasterWhisperModel,
    _GoogleTranslator,
    _argos_translate,
    log,
)
from ..core.translation_cache import TranslationCache
from ..core.utils import CancelledError, _check_cancel
from ..models import SubtitleIO, SubtitleSegment, Word, retime_words


_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
# kode bahasa Whisper -> kode sumber penerjemah
TRANSLATOR_SRC = {"ko": "ko", "zh": "zh-CN", "ja": "ja", "en": "en", "id": "id"}


def _is_gpu_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("cudnn", "cublas", "cuda", "libcudart", "nvrtc", "cufft"))


def retry_with_backoff(fn: Callable, attempts: int = 5, base: float = 0.6, cap: float = 10.0,
                       cancel_event: threading.Event | None = None, sleep: Callable | None = None,
                       abort_event: threading.Event | None = None):
    """
    Panggil fn() dengan exponential backoff + jitter (0,5-1,0 x). Jeda dipotong-potong 0,2 dtk
    supaya Cancel tetap responsif. abort_event (mis. circuit breaker) menghentikan retry yang sedang
    berjalan seperti cancel. Melempar exception terakhir bila semua percobaan gagal.
    """
    last: Exception | None = None
    for i in range(attempts):
        _check_cancel(cancel_event)
        _check_cancel(abort_event)
        try:
            return fn()
        except CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if i == attempts - 1:
                break
            remaining = min(cap, base * (2 ** i)) * (0.5 + random.random() / 2)
            while remaining > 0:
                step = min(0.2, remaining)
                (sleep or time.sleep)(step)
                remaining -= step
                _check_cancel(cancel_event)
                _check_cancel(abort_event)
    assert last is not None
    raise last


class RequestTimeout(Exception):
    """Satu panggilan terjemahan melewati batas waktunya (akan di-retry seperti kegagalan jaringan biasa)."""


_ABANDONED_LOCK = threading.Lock()
_ABANDONED: set[threading.Thread] = set()          # thread panggilan yang ditinggalkan karena timeout/cancel
MAX_ABANDONED_CALLS = 12                           # batas thread 'menggantung' sebelum terjemahan dihentikan


def abandoned_calls() -> int:
    """Jumlah panggilan jaringan yang ditinggalkan dan MASIH berjalan (untuk log/uji)."""
    with _ABANDONED_LOCK:
        for t in [t for t in _ABANDONED if not t.is_alive()]:
            _ABANDONED.discard(t)
        return len(_ABANDONED)


def call_with_deadline(fn: Callable, timeout: float | None, cancel_event: threading.Event | None = None,
                       abort_event: threading.Event | None = None, poll: float = 0.1):
    """
    Jalankan fn() dengan batas waktu yang DIKENDALIKAN APLIKASI dan tetap tanggap terhadap Cancel/abort.

    Library HTTP pihak ketiga (mis. deep-translator) tidak selalu menerima timeout dan tidak bisa diinterupsi dari
    luar. fn() dijalankan di thread daemon pendamping; thread pemanggil menunggu sambil memeriksa cancel/abort tiap
    `poll` detik dan menyerah setelah `timeout`. Panggilan yang ditinggalkan akan selesai sendiri saat soket-nya
    kembali/ditutup OS; hasilnya dibuang. Dibatasi MAX_ABANDONED_CALLS agar thread menggantung tidak menumpuk.
    timeout=None -> panggil langsung (untuk backend offline/CPU yang tak bisa menggantung karena jaringan).
    """
    if timeout is None:
        _check_cancel(cancel_event)
        _check_cancel(abort_event)
        return fn()
    if abandoned_calls() >= MAX_ABANDONED_CALLS:
        raise RequestTimeout(f"terlalu banyak panggilan jaringan yang menggantung (>= {MAX_ABANDONED_CALLS})")
    box: dict = {}
    done = threading.Event()

    def runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - diteruskan ke pemanggil
            box["error"] = exc
        finally:
            done.set()

    t = threading.Thread(target=runner, daemon=True, name="kp-translate-call")
    t.start()
    deadline = time.monotonic() + timeout
    while not done.wait(poll):
        try:
            _check_cancel(cancel_event)
            _check_cancel(abort_event)
            if time.monotonic() >= deadline:
                raise RequestTimeout(f"tidak ada respons dalam {timeout:.0f} detik")
        except BaseException:
            with _ABANDONED_LOCK:
                _ABANDONED.add(t)
            raise
    if "error" in box:
        raise box["error"]
    return box.get("value")


class TranslatorBackend:
    """Antarmuka backend terjemahan. translate() boleh melempar exception (akan di-retry dengan backoff)."""
    key = "base"
    label = "base"
    max_workers = 4
    call_timeout: float | None = 30.0      # batas waktu SATU panggilan translate() (detik); None = tanpa batas
    online: bool = False                   # True = teks subtitle DIKIRIM ke layanan pihak ketiga lewat internet

    @classmethod
    def available(cls) -> tuple[bool, str]:
        return True, ""

    def translate(self, text: str, src: str, tgt: str) -> str:
        raise NotImplementedError


class GoogleBackend(TranslatorBackend):
    key = "google"
    label = TRANSLATORS["google"]
    max_workers = 4
    online = True

    def __init__(self) -> None:
        self._local = threading.local()          # objek penerjemah per-thread (tidak dijamin thread-safe)

    @classmethod
    def available(cls) -> tuple[bool, str]:
        return (True, "") if HAS_DEEP_TRANSLATOR else (False, "pip install deep-translator")

    def translate(self, text: str, src: str, tgt: str) -> str:
        key = (src, tgt)
        if getattr(self._local, "key", None) != key:
            self._local.tr = _GoogleTranslator(source=src, target=tgt)
            self._local.key = key
        return self._local.tr.translate(text)


class MyMemoryBackend(TranslatorBackend):
    key = "mymemory"
    label = TRANSLATORS["mymemory"]
    max_workers = 2
    call_timeout = 30.0
    online = True
    _CODES = {"ko": "ko-KR", "zh-CN": "zh-CN", "ja": "ja-JP", "en": "en-GB", "id": "id-ID"}

    @classmethod
    def available(cls) -> tuple[bool, str]:
        return (True, "") if HAS_DEEP_TRANSLATOR else (False, "pip install deep-translator")

    def translate(self, text: str, src: str, tgt: str) -> str:
        from deep_translator import MyMemoryTranslator          # impor lambat: hanya bila dipakai
        if src not in self._CODES:
            raise ValueError("MyMemory butuh bahasa sumber eksplisit (pilih preset K-Drama/C-Drama atau Auto yang terdeteksi)")
        return MyMemoryTranslator(source=self._CODES[src], target=self._CODES.get(tgt, tgt)).translate(text)


class ArgosBackend(TranslatorBackend):
    """Offline. Butuh paket bahasa Argos terpasang (mis. ko→en dan en→id)."""
    key = "argos"
    label = TRANSLATORS["argos"]
    max_workers = 1
    call_timeout = None                    # offline (CPU lokal): tidak bisa menggantung karena jaringan

    @classmethod
    def available(cls) -> tuple[bool, str]:
        return (True, "") if HAS_ARGOS else (False, "pip install argostranslate  (lalu pasang paket bahasa)")

    def translate(self, text: str, src: str, tgt: str) -> str:
        code = {"zh-CN": "zh"}.get(src, src)
        langs = _argos_translate.get_installed_languages()
        frm = next((l for l in langs if l.code == code), None)
        to = next((l for l in langs if l.code == tgt), None)
        if frm is None or to is None or frm.get_translation(to) is None:
            raise RuntimeError(f"Paket bahasa Argos {code}→{tgt} belum terpasang. Pasang lewat argostranslate.")
        return _argos_translate.translate(text, code, tgt)


def needs_online_notice(backend_cls: type[TranslatorBackend], acknowledged: bool) -> bool:
    """True bila pengguna harus diberi tahu (sekali) bahwa teks subtitle akan dikirim ke layanan pihak ketiga."""
    return bool(getattr(backend_cls, "online", False)) and not acknowledged


BACKENDS: dict[str, type[TranslatorBackend]] = {
    "google": GoogleBackend, "mymemory": MyMemoryBackend, "argos": ArgosBackend,
}


class AiService:
    """
    Transkripsi (faster-whisper) + terjemahan dengan:
      - cache permanen, deduplikasi baris, terjemahan paralel per-chunk
      - retry exponential backoff + jitter, circuit breaker bila koneksi/kuota bermasalah
      - pemeriksaan hasil kosong / tidak berubah, backend alternatif
      - sinkronisasi ulang kata (karaoke) dari 'amplop' waktu bicara sumber
      - deteksi bahasa yang konsisten (sekali untuk seluruh file, dipakai sebagai bahasa sumber)
    """

    MAX_CONSECUTIVE_TRANSLATE_FAILURES = 8
    TRANSLATE_TOTAL_TIMEOUT = 3600.0       # batas waktu TOTAL satu proses terjemahan (detik); hasil parsial tetap dipakai

    def __init__(self, cache_path: Path | None = None) -> None:
        self._model = None
        self._model_key: tuple[str, str, str] | None = None
        self._lock = threading.Lock()
        self._force_cpu = False
        self.cache = TranslationCache(cache_path or (APP_DIR / "translation_cache.sqlite"))
        self.last_translate_failures = 0
        self.last_translate_stats: dict = {}
        self.last_language: str | None = None
        self.last_language_prob = 0.0

    def clear_cache(self) -> None:
        """Hapus seluruh cache terjemahan (kontrol privasi pengguna)."""
        self.cache.clear()
        self.cache.vacuum()

    def prune_cache(self) -> int:
        """Terapkan kebijakan retensi cache terjemahan (umur + jumlah entri). Return jumlah entri terhapus."""
        return self.cache.prune()

    def close(self) -> None:
        """Lepas sumber daya layanan (koneksi cache SQLite). Dipanggil saat aplikasi ditutup."""
        self.cache.close()

    # ---------- Whisper ----------
    def _get_model(self, size: str, device: str = "auto", compute_type: str = "int8",
                   progress_cb: Callable[[float | None, str], None] | None = None):
        if not HAS_FASTER_WHISPER:
            raise RuntimeError(
                "faster-whisper belum terpasang.\n\nInstall: pip install faster-whisper\n"
                "Lalu restart KaptionPro."
            )
        key = (size, device, compute_type)
        with self._lock:
            if self._model is None or self._model_key != key:
                if progress_cb:
                    progress_cb(None, f"Loading Whisper '{size}' (unduh pertama kali bisa besar)…")
                log.info("Loading WhisperModel: size=%s device=%s compute=%s", size, device, compute_type)
                self._model = _FasterWhisperModel(size, device=device, compute_type=compute_type)
                self._model_key = key
            return self._model

    def transcribe(self, media_path: str, source_lang: str | None, model_size: str = "small",
                   progress_cb: Callable[[float | None, str], None] | None = None,
                   cancel_event: threading.Event | None = None) -> list[SubtitleSegment]:
        """Transkripsi. Jika GPU bermasalah (cuDNN/CUDA tidak lengkap) otomatis fallback ke CPU."""
        if not Path(media_path).exists():
            raise FileNotFoundError(f"Media tidak ditemukan: {media_path}")
        device = "cpu" if self._force_cpu else "auto"
        try:
            return self._transcribe_once(media_path, source_lang, model_size, device, progress_cb, cancel_event)
        except CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            if device != "auto" or not _is_gpu_error(e):
                raise
            log.warning("GPU gagal (%s) -> fallback CPU", e)
            self._force_cpu = True
            with self._lock:
                self._model, self._model_key = None, None
            if progress_cb:
                progress_cb(None, "GPU tidak siap (cuDNN/CUDA) — beralih ke CPU…")
            return self._transcribe_once(media_path, source_lang, model_size, "cpu", progress_cb, cancel_event)

    def _transcribe_once(self, media_path: str, source_lang: str | None, model_size: str, device: str,
                         progress_cb: Callable[[float | None, str], None] | None,
                         cancel_event: threading.Event | None) -> list[SubtitleSegment]:
        model = self._get_model(model_size, device=device, progress_cb=progress_cb)
        segs_iter, info = model.transcribe(
            media_path, language=source_lang, word_timestamps=True, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400},
        )
        self.last_language = getattr(info, "language", None) or source_lang
        try:
            self.last_language_prob = float(getattr(info, "language_probability", 0.0) or 0.0)
        except (TypeError, ValueError):
            self.last_language_prob = 0.0
        if progress_cb and getattr(info, "language", None):
            progress_cb(None, f"Bahasa terdeteksi: {info.language} ({self.last_language_prob:.0%})")
        total = float(getattr(info, "duration", 0.0) or 0.0)
        out: list[SubtitleSegment] = []
        for seg in segs_iter:
            _check_cancel(cancel_event)
            text = (seg.text or "").strip()
            if not text:
                continue
            words: list[Word] = []
            for w in getattr(seg, "words", None) or []:
                wt = (w.word or "").strip()
                if wt:
                    words.append(Word(float(w.start), float(w.end), wt))
            out.append(SubtitleSegment(float(seg.start), float(seg.end), text, words))
            if progress_cb:
                pct = (float(seg.end) / total) if total > 0 else None
                progress_cb(min(1.0, pct) if pct is not None else None, text[:60])
        if progress_cb:
            progress_cb(1.0, f"{len(out)} segments")
        return SubtitleIO.normalize(out)[0]          # validasi timestamp + urut waktu

    # ---------- terjemahan ----------
    @staticmethod
    def _looks_untranslated(orig: str, new: str, src: str) -> bool:
        """Hasil sama persis dengan teks sumber yang berhuruf CJK/Hangul = hampir pasti tidak diterjemahkan."""
        return new == orig and src in ("ko", "zh-CN", "ja", "auto") and bool(_CJK_RE.search(orig))

    def translate_to_indonesian(self, segments: list[SubtitleSegment], source_name: str,
                                progress_cb: Callable[[float | None, str], None] | None = None,
                                cancel_event: threading.Event | None = None,
                                backend_key: str = "google",
                                source_code: str | None = None) -> list[SubtitleSegment]:
        """
        Terjemahkan ke Indonesia. Baris yang gagal tetap berbahasa asli (+ word timestamp) dan DIHITUNG
        di last_translate_failures / last_translate_stats. Bila gagal beruntun (offline / rate-limit),
        proses berhenti dan hasil yang sudah ada tetap dipakai + disimpan ke cache untuk dilanjutkan.
        """
        self.last_translate_failures = 0
        self.last_translate_stats = {}
        backend_cls = BACKENDS.get(backend_key, GoogleBackend)
        ok, hint = backend_cls.available()
        if not ok:
            raise RuntimeError(f"Penerjemah '{backend_cls.label}' belum tersedia.\n\n{hint}\nLalu restart KaptionPro.")
        src = source_code or {"Korean": "ko", "Chinese": "zh-CN", "Auto": "auto"}.get(source_name, "auto")
        backend = backend_cls()

        originals = [s.text.replace("\n", " ").strip() for s in segments]
        uniq = list(dict.fromkeys(t for t in originals if t))               # deduplikasi, urutan dijaga
        keys = {t: TranslationCache.key(backend.key, src, "id", t) for t in uniq}
        cached = self.cache.get_many(list(keys.values()))
        results: dict[str, str] = {t: cached[keys[t]] for t in uniq if (cached.get(keys[t]) or "").strip()}
        todo = [t for t in uniq if t not in results]
        stats = {"backend": backend.key, "source": src, "unique": len(uniq), "cached": len(results),
                 "translated": 0, "failed": 0, "aborted": False}

        failed: set[str] = set()
        new_items: dict[str, str] = {}
        if progress_cb:
            progress_cb(0.0 if todo else 1.0, f"{len(results)} dari cache, {len(todo)} perlu diterjemahkan")
        try:
            if todo:
                stop = threading.Event()
                streak = {"n": 0}                       # gagal beruntun (dihitung di worker, bukan di thread utama)
                streak_lock = threading.Lock()

                def record(success: bool) -> None:
                    with streak_lock:
                        streak["n"] = 0 if success else streak["n"] + 1
                        if streak["n"] >= self.MAX_CONSECUTIVE_TRANSLATE_FAILURES and not stop.is_set():
                            stop.set()                   # circuit breaker: hentikan juga retry yang sedang berjalan
                            stats["aborted"] = True
                            log.warning("Terjemahan dihentikan: %d gagal beruntun", streak["n"])

                def work(text: str):
                    if stop.is_set():
                        return text, None, "skipped"
                    try:
                        if time.monotonic() >= total_deadline:
                            if not stop.is_set():
                                stop.set()
                                stats["aborted"] = True
                                stats["timed_out"] = True
                                log.warning("Terjemahan dihentikan: melewati batas total %.0f dtk",
                                            self.TRANSLATE_TOTAL_TIMEOUT)
                            return text, None, "skipped"
                        out = retry_with_backoff(
                            lambda: call_with_deadline(lambda: backend.translate(text, src, "id"),
                                                       backend.call_timeout, cancel_event, stop),
                            cancel_event=cancel_event, abort_event=stop)
                    except CancelledError:
                        return text, None, "skipped"
                    except Exception as e:  # noqa: BLE001
                        record(False)
                        return text, None, str(e) or e.__class__.__name__
                    out = (out or "").strip()
                    if not out or self._looks_untranslated(text, out, src):
                        record(False)
                        return text, None, "hasil kosong / tidak berubah"
                    record(True)
                    return text, out, ""

                workers = max(1, min(backend.max_workers, len(todo)))
                done = 0
                total_deadline = time.monotonic() + self.TRANSLATE_TOTAL_TIMEOUT
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    pending = {pool.submit(work, t) for t in todo}
                    try:
                        while pending:
                            # Tunggu singkat lalu periksa Cancel: tidak buta menunggu future yang lambat.
                            finished, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                            if cancel_event is not None and cancel_event.is_set():
                                raise CancelledError("Dibatalkan user")
                            for fut in finished:
                                text, out, err = fut.result()
                                if err == "skipped":
                                    continue
                                done += 1
                                if out is not None:
                                    results[text] = out
                                    new_items[keys[text]] = out
                                    stats["translated"] += 1
                                else:
                                    failed.add(text)
                                    # JANGAN mencatat isi subtitle di log (privasi): cukup panjang teks.
                                    log.warning("Terjemahan gagal (%s): teks %d karakter", err, len(text))
                                if progress_cb:
                                    progress_cb(done / len(todo),
                                                f"{done}/{len(todo)} diterjemahkan (cache {stats['cached']})")
                    except BaseException:
                        stop.set()                                  # hentikan retry yang sedang berjalan
                        for fut in pending:
                            fut.cancel()                            # antrean yang belum mulai dibatalkan
                        raise
        finally:
            self.cache.put_many(new_items)          # simpan yang berhasil walau dibatalkan/gagal di tengah

        failed |= {t for t in todo if t not in results}      # yang di-skip karena abort ikut dihitung gagal
        stats["failed"] = len(failed)
        result: list[SubtitleSegment] = []
        lines_failed = 0
        for seg, orig in zip(segments, originals):
            tr = results.get(orig)
            if tr is None:
                result.append(seg)                   # pertahankan teks asli + word timestamp
                lines_failed += 1 if orig else 0
            else:
                # karaoke: petakan ulang kata baru ke amplop waktu bicara sumber (jeda ucapan terjaga)
                words = retime_words(tr, seg.words, seg.start, seg.end, seg.start, seg.end)
                result.append(SubtitleSegment(seg.start, seg.end, tr, words))
        stats["lines_failed"] = lines_failed
        self.last_translate_failures = lines_failed
        self.last_translate_stats = stats
        return result
