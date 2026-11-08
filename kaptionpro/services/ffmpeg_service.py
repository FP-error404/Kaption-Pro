"""Layanan FFmpeg: probe stream, preflight, dan export video bersubtitle."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import APP_DIR, EXPORT_HEIGHTS, log
from ..core.utils import CancelledError, InterProcessLock, _pid_alive, fmt_clock
from ..models import ExportOptions, VideoInfo


class FFmpegError(RuntimeError):
    """Kegagalan FFmpeg/ffprobe dengan pesan yang bisa ditampilkan langsung ke user."""


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROGRESS_KEYS = {"frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms", "out_time",
                  "dup_frames", "drop_frames", "speed", "progress"}


@dataclass
class StreamInfo:
    """Hasil ffprobe: daftar stream video/audio + durasi + ukuran file."""
    video: list = field(default_factory=list)
    audio: list = field(default_factory=list)
    duration: float = 0.0
    size: int = 0

    @property
    def has_video(self) -> bool:
        return bool(self.video)

    @property
    def has_audio(self) -> bool:
        return bool(self.audio)

    @property
    def audio_codecs(self) -> list[str]:
        return [str(a.get("codec_name", "")).lower() for a in self.audio]

    @property
    def width(self) -> int:
        return int(self.video[0].get("width") or 0) if self.video else 0

    @property
    def height(self) -> int:
        return int(self.video[0].get("height") or 0) if self.video else 0

    @property
    def fps(self) -> float:
        rate = (self.video[0].get("r_frame_rate") if self.video else None) or "0/0"
        try:
            a, b = rate.split("/")
            return float(a) / float(b) if float(b) else 0.0
        except (ValueError, ZeroDivisionError):
            return 0.0


@dataclass
class ExportResult:
    path: str
    size: int
    duration: float
    height: int
    notes: list = field(default_factory=list)

    def summary(self) -> str:
        mb = self.size / 1_048_576
        res = f" · {self.height}p" if self.height else ""
        return f"{mb:,.1f} MB · {fmt_clock(self.duration)}{res}"


class FFmpegService:
    """
    Probe video, preflight, export hardsub (ASS) yang aman terhadap karakter path aneh,
    proses macet, pembatalan, dan hasil rusak (output divalidasi dengan ffprobe sebelum dipakai).
    """

    SUB_FILE_NAME = "kp_sub.ass"
    STALL_TIMEOUT = 180.0                       # detik tanpa output progres -> dianggap macet
    SAFE_AUDIO_CODECS = {"aac", "mp3", "ac3", "eac3"}      # aman di-copy ke kontainer MP4
    SIZE_FACTOR = {"original": 1.3, "1080p": 0.9, "720p": 0.5, "480p": 0.25}
    TERMINATE_WAIT = 3.0                        # detik menunggu setelah terminate() sebelum kill()
    KILL_WAIT = 5.0                             # detik menunggu setelah kill()
    # Batas atas waktu sampai worker export BERHENTI setelah Cancel: terminate + kill + validasi ffprobe yang sedang
    # berjalan (timeout 30 dtk) + penghapusan partial. Dipakai penutupan aplikasi untuk menunggu worker.
    MAX_STOP_SECONDS = TERMINATE_WAIT + KILL_WAIT + 30.0 + 5.0

    # Proses FFmpeg yang sedang hidup (seluruh instance service): dipakai penutupan paksa agar tidak ada yang yatim.
    _active_procs: "set[subprocess.Popen]" = set()
    _active_lock = threading.Lock()

    def __init__(self) -> None:
        self.stall_timeout = self.STALL_TIMEOUT

    @staticmethod
    def ffmpeg_path() -> str | None:
        return shutil.which("ffmpeg")

    @staticmethod
    def ffprobe_path() -> str | None:
        return shutil.which("ffprobe")

    # ---------- ffprobe ----------
    def probe_streams(self, path: str) -> StreamInfo | None:
        """Daftar stream + durasi + ukuran. None bila ffprobe tidak ada / gagal / file tidak terbaca."""
        ffprobe = self.ffprobe_path()
        if not ffprobe or not Path(path).exists():
            return None
        cmd = [ffprobe, "-v", "error", "-show_entries",
               "stream=index,codec_type,codec_name,width,height,r_frame_rate:stream_disposition=attached_pic"
               ":format=duration,size", "-of", "json", str(path)]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                 encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
            data = json.loads(res.stdout or "{}")
            streams = [s for s in (data.get("streams") or [])
                       if not (s.get("disposition") or {}).get("attached_pic")]       # abaikan cover art
            fmt = data.get("format") or {}
            return StreamInfo(
                video=[s for s in streams if s.get("codec_type") == "video"],
                audio=[s for s in streams if s.get("codec_type") == "audio"],
                duration=float(fmt.get("duration") or 0.0), size=int(fmt.get("size") or 0))
        except (OSError, subprocess.SubprocessError, ValueError, TypeError) as e:
            log.warning("ffprobe gagal: %s", e)
            return None

    def probe(self, video_path: str) -> VideoInfo | None:
        info = self.probe_streams(video_path)
        if info is None or not info.has_video:
            return None
        return VideoInfo(info.width or 1920, info.height or 1080, info.duration, info.fps)

    # ---------- preflight ----------
    def estimate_output_bytes(self, src: Path, info: StreamInfo | None, options: ExportOptions) -> int:
        try:
            size = (info.size if info and info.size else 0) or src.stat().st_size
        except OSError:
            size = 0
        return int(max(50 * 1_048_576, size * self.SIZE_FACTOR.get(options.quality, 1.3)) * 1.1)

    def preflight(self, video_path: str, out_path: str, options: ExportOptions) -> StreamInfo | None:
        """Periksa semuanya SEBELUM encoding: FFmpeg ada, sumber valid, ada stream video, folder & ruang disk cukup."""
        if not self.ffmpeg_path():
            raise FFmpegError("FFmpeg tidak ditemukan di PATH. Install FFmpeg lalu restart KaptionPro.")
        src, out = Path(video_path), Path(out_path)
        if not src.exists():
            raise FFmpegError(f"File video sumber tidak ditemukan:\n{src}")
        info = self.probe_streams(video_path)
        if info is not None and not info.has_video:
            raise FFmpegError("File ini tidak punya stream video yang bisa di-export.")
        outdir = out.resolve().parent
        if not outdir.exists():
            raise FFmpegError(f"Folder tujuan tidak ada:\n{outdir}")
        if not os.access(outdir, os.W_OK):
            raise FFmpegError(f"Folder tujuan tidak bisa ditulis:\n{outdir}")
        need = self.estimate_output_bytes(src, info, options)
        free = shutil.disk_usage(outdir).free
        if free < need:
            raise FFmpegError(f"Ruang disk tidak cukup di {outdir}:\nbutuh sekitar {need / 1e9:.2f} GB, "
                              f"tersedia {free / 1e9:.2f} GB.")
        return info

    # ---------- perintah ----------
    def build_export_command(self, ffmpeg: str, video: Path, out: Path,
                             options: ExportOptions | None = None, info: StreamInfo | None = None) -> list[str]:
        """
        Subtitle dirujuk lewat nama relatif (cwd = folder temp) -> aman dari karakter khusus di path.
        Hanya stream video pertama + SEMUA stream audio yang dipetakan (subtitle/data sumber dibuang).
        """
        opts = (options or ExportOptions()).validated()
        vf = f"ass={self.SUB_FILE_NAME}"
        height = EXPORT_HEIGHTS.get(opts.quality)
        if height:
            vf += f",scale=-2:'min({height},ih)'"          # hanya mengecilkan; tidak pernah upscale
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
               "-i", str(video), "-map", "0:v:0", "-map", "0:a?", "-vf", vf,
               "-c:v", "libx264", "-preset", opts.preset, "-crf", str(opts.crf), "-pix_fmt", "yuv420p"]
        if info is None or info.has_audio:
            can_copy = bool(info and info.audio_codecs and all(c in self.SAFE_AUDIO_CODECS for c in info.audio_codecs))
            if opts.audio == "copy" or (opts.audio == "auto" and can_copy):
                cmd += ["-c:a", "copy"]
            else:
                cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-max_muxing_queue_size", "1024", "-movflags", "+faststart", str(out)]
        return cmd

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> bool:
        """Hentikan proses: terminate -> tunggu 3 dtk -> kill. True bila proses BENAR-BENAR sudah berhenti."""
        if proc.poll() is not None:
            return True
        try:
            proc.terminate()
            proc.wait(timeout=FFmpegService.TERMINATE_WAIT)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=FFmpegService.KILL_WAIT)
            except subprocess.TimeoutExpired:
                pass
            except OSError:
                pass
        except OSError:
            pass
        if proc.poll() is None:
            log.error("Proses FFmpeg tidak mau berhenti (pid %s)", proc.pid)
            return False
        return True

    @classmethod
    def _stop_or_raise(cls, proc: subprocess.Popen) -> None:
        """Hentikan FFmpeg; bila tetap hidup, lapor jujur ke user (jangan pura-pura sudah batal)."""
        if not cls._terminate(proc):
            raise FFmpegError(f"FFmpeg tidak bisa dihentikan (PID {proc.pid}). Tutup prosesnya lewat "
                              "Task Manager agar tidak terus menulis file.")

    @classmethod
    def active_process_count(cls) -> int:
        with cls._active_lock:
            return sum(1 for p in cls._active_procs if p.poll() is None)

    @classmethod
    def kill_all_active(cls) -> int:
        """Penutupan PAKSA (worker tak mau berhenti): kill semua FFmpeg yang masih hidup tanpa menunggu.
        Return jumlah proses yang dikirimi kill. Tidak pernah melempar exception."""
        with cls._active_lock:
            procs = [p for p in cls._active_procs if p.poll() is None]
        n = 0
        for p in procs:
            try:
                p.kill()
                n += 1
            except OSError:
                pass
        return n

    def _wait_exit(self, proc: subprocess.Popen, cancel_event: threading.Event, timeout: float = 30.0) -> int:
        """Tunggu FFmpeg keluar tapi tetap tanggap terhadap Cancel (tidak buta selama `timeout`)."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                if cancel_event.is_set():
                    self._stop_or_raise(proc)
                    raise CancelledError("Dibatalkan user") from None
                if time.monotonic() >= deadline:
                    self._stop_or_raise(proc)
                    raise FFmpegError("FFmpeg tidak berhenti setelah selesai menulis — dihentikan.") from None

    @staticmethod
    def _discard_partial(partial: Path) -> None:
        """Hapus file partial; mencoba ulang sebentar (Windows: handle bisa terlambat dilepas setelah proses mati).
        Tidak pernah melempar exception, supaya tidak menutupi error asli export."""
        for attempt in range(5):
            try:
                partial.unlink(missing_ok=True)
                return
            except OSError as e:
                if attempt == 4:
                    log.warning("File partial tidak bisa dihapus: %s (%s)", partial, e)
                    return
                time.sleep(0.2)

    _PARTIAL_PID = re.compile(r"\.kp_partial-(\d+)-[0-9a-f]+")

    # ---------- reservasi tujuan export (atomik) ----------
    # File reservasi disimpan di FOLDER APLIKASI (bukan di sebelah video hasil): di Windows file lock dipertahankan
    # permanen (hanya kunci OS yang dilepas), sehingga tidak boleh meninggalkan sampah di folder milik user.
    # Namanya diturunkan dari path tujuan (normcase) -> tujuan yang sama selalu memakai file lock yang sama.
    LOCK_DIR: Path | None = None                       # override (tes); default APP_DIR/export-locks

    @classmethod
    def _reservation_path(cls, out: Path) -> Path:
        digest = hashlib.sha256(os.path.normcase(str(out)).encode("utf-8", "surrogatepass")).hexdigest()[:32]
        return (cls.LOCK_DIR or (APP_DIR / "export-locks")) / f"{digest}.lock"

    @classmethod
    def _reserve_destination(cls, out: Path) -> InterProcessLock:
        """
        Pesan tujuan export memakai KUNCI OS pada file reservasi (lihat _reservation_path) sebelum FFmpeg dimulai. Dari dua export
        bersamaan (proses lain maupun thread lain) hanya SATU yang mendapat kunci. Tidak ada tahap 'baca isi file
        lalu hapus bila basi': file reservasi yang baru dibuat tak pernah bisa dianggap rusak oleh pihak lain,
        dan kunci dilepas OS bila pemilik crash. Mengembalikan lock untuk _release_destination().
        Raise FFmpegError bila tujuan sedang dipakai export lain atau reservasi tak bisa dibuat (fail-closed).
        """
        if not out.parent.exists():
            raise FFmpegError(f"Folder tujuan tidak ada:\n{out.parent}")
        path = cls._reservation_path(out)
        lock = InterProcessLock(path, remove_on_release=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            got = lock.acquire()
        except OSError as e:
            raise FFmpegError(f"Reservasi tujuan export tidak bisa dibuat:\n{path}\n{e}") from e
        if not got:
            raise FFmpegError("Export lain ke tujuan yang sama sedang berjalan:\n" + str(out) +
                              "\nTunggu hingga selesai atau pilih nama file lain.")
        return lock

    @classmethod
    def _release_destination(cls, out: Path, lock: InterProcessLock) -> None:
        """Lepas reservasi — hanya lock milik export ini."""
        try:
            lock.release()
        except OSError as e:
            log.warning("Reservasi export tidak bisa dilepas: %s", e)

    @classmethod
    def _find_partials(cls, out: Path) -> tuple[list[Path], list[Path]]:
        """
        (basi, aktif) untuk file partial tujuan `out`. Aktif = nama berformat baru dan pid pemiliknya masih
        hidup (export lain yang sedang berjalan ke tujuan yang sama). Format lama (tanpa pid) atau pemilik
        sudah mati = basi.
        """
        stale: list[Path] = []
        active: list[Path] = []
        try:
            for p in out.parent.iterdir():
                if not p.name.startswith(f"{out.stem}.kp_partial"):
                    continue
                m = cls._PARTIAL_PID.search(p.name)
                (active if (m and _pid_alive(int(m.group(1)))) else stale).append(p)
        except OSError:
            pass
        return stale, active

    MP4_SUFFIXES = (".mp4", ".m4v", ".mov")

    @staticmethod
    def check_mp4_structure(path: Path) -> str | None:
        """
        Pemeriksaan struktur kontainer MP4 TANPA ffprobe: telusuri box tingkat atas (ftyp / moov / mdat). Box harus
        utuh dan jumlah ukurannya tepat sama dengan ukuran file -> file terpotong (disk penuh, proses mati) atau bukan
        MP4 terdeteksi. Return None bila lolos, atau alasan singkat. TIDAK memeriksa isi stream/durasi (itu tugas ffprobe).
        """
        try:
            total = path.stat().st_size
            seen: list[str] = []
            with open(path, "rb") as f:
                offset = 0
                while offset < total:
                    if len(seen) > 10_000:
                        return "terlalu banyak box (bukan MP4 yang wajar)"
                    f.seek(offset)
                    head = f.read(16)
                    if len(head) < 8:
                        return f"header box terpotong di byte {offset}"
                    size, kind = int.from_bytes(head[:4], "big"), head[4:8]
                    header_len = 8
                    if size == 1:                                    # ukuran 64-bit
                        if len(head) < 16:
                            return f"header box besar terpotong di byte {offset}"
                        size, header_len = int.from_bytes(head[8:16], "big"), 16
                    elif size == 0:                                  # sampai akhir file
                        size = total - offset
                    if size < header_len:
                        return f"ukuran box tidak valid di byte {offset}"
                    if offset + size > total:
                        return (f"box '{kind.decode('latin-1', 'replace')}' terpotong "
                                f"(butuh {offset + size} byte, file {total} byte)")
                    seen.append(kind.decode("latin-1", "replace"))
                    offset += size
        except OSError as e:
            return f"file tidak bisa dibaca ({e})"
        if not seen or seen[0] != "ftyp":
            return "bukan file MP4 (box 'ftyp' tidak ada di awal)"
        missing = [b for b in ("moov", "mdat") if b not in seen]
        if missing:
            return "box " + ", ".join(f"'{b}'" for b in missing) + " tidak ditemukan"
        return None

    def validate_output(self, path: Path, src_info: StreamInfo | None, expected_duration: float,
                        options: ExportOptions) -> list[str]:
        """Periksa hasil export dengan ffprobe (tanpa ffprobe: ukuran file + struktur MP4). Kosong = valid."""
        try:
            size = path.stat().st_size
        except OSError:
            return ["file output tidak ada"]
        problems: list[str] = []
        if size < 1024:
            problems.append(f"ukuran file hanya {size} byte")
        if not self.ffprobe_path():
            log.warning("ffprobe tidak ada: validasi output terbatas pada ukuran file + struktur MP4")
            if path.suffix.lower() in self.MP4_SUFFIXES:
                bad = self.check_mp4_structure(path)
                if bad:
                    problems.append(f"struktur MP4 tidak utuh: {bad}")
            return problems
        out = self.probe_streams(str(path))
        if out is None:
            return problems + ["file output tidak bisa dibaca ffprobe (kemungkinan rusak)"]
        if not out.has_video:
            problems.append("tidak ada stream video")
        if src_info is not None and src_info.has_audio and not out.has_audio:
            problems.append("audio hilang dari hasil export")
        if expected_duration > 1 and out.duration > 0 and out.duration < expected_duration * 0.97 - 1.0:
            problems.append(f"durasi hasil ({fmt_clock(out.duration)}) lebih pendek dari sumber "
                            f"({fmt_clock(expected_duration)})")
        target = EXPORT_HEIGHTS.get(options.quality)
        if target and out.height and out.height > target + 2:
            problems.append(f"resolusi {out.height}p tidak sesuai preset {options.quality}")
        return problems

    # ---------- export ----------
    def export_video(self, video_path: str, ass_content: str, out_path: str, duration: float,
                     progress_cb: Callable[[float | None, str], None] | None,
                     cancel_event: threading.Event, options: ExportOptions | None = None) -> ExportResult:
        """Burn subtitle ke video. Tujuan DIPESAN secara atomik dulu (lihat _reserve_destination) lalu
        dilepas di finally, apa pun hasilnya."""
        out = Path(out_path).resolve()
        lock = self._reserve_destination(out)
        try:
            return self._export_video_reserved(video_path, ass_content, out_path, duration,
                                               progress_cb, cancel_event, options)
        finally:
            self._release_destination(out, lock)

    def _export_video_reserved(self, video_path: str, ass_content: str, out_path: str, duration: float,
                               progress_cb: Callable[[float | None, str], None] | None,
                               cancel_event: threading.Event, options: ExportOptions | None = None) -> ExportResult:
        """
        Burn subtitle (ASS) ke video. Blocking -> panggil dari thread.
        - preflight (ffmpeg, stream video, folder, ruang disk)
        - output ditulis ke file '.kp_partial-<pid>-<uuid>', DIVALIDASI, baru di-rename (file lama tak pernah rusak)
        - pembacaan output di thread terpisah + watchdog: proses macet dihentikan, Cancel selalu bisa
        - file partial & temp selalu dibersihkan saat gagal / dibatalkan
        """
        opts = (options or ExportOptions()).validated()
        info = self.preflight(video_path, out_path, opts)
        ffmpeg = self.ffmpeg_path()
        src, out = Path(video_path).resolve(), Path(out_path).resolve()
        if src == out:
            raise FFmpegError("File output tidak boleh sama dengan video sumber.")
        duration = duration or (info.duration if info else 0.0)
        # Reservasi atomik sudah dipegang; pemeriksaan partial ini tinggal pertahanan berlapis
        # (mis. export dari versi lama yang belum memakai reservasi).
        stale, active = self._find_partials(out)
        if active:
            raise FFmpegError("Export lain ke tujuan yang sama sedang berjalan:\n" + str(out) +
                              "\nTunggu hingga selesai atau pilih nama file lain.")
        for p in stale:                              # sisa crash / pemilik sudah mati
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        # Nama UNIK per proses export (pid + uuid): dua export tidak pernah saling menghapus file sementara.
        partial = out.with_name(f"{out.stem}.kp_partial-{os.getpid()}-{uuid.uuid4().hex[:10]}{out.suffix}")
        tail: deque[str] = deque(maxlen=12)
        committed = False

        # Partial DIJAMIN dibersihkan bila export gagal/dibatalkan di titik mana pun — termasuk saat
        # os.replace() sendiri gagal (izin, file tujuan terkunci, masalah filesystem). Hanya commit sukses
        # yang boleh meninggalkan file di tujuan.
        try:
            with tempfile.TemporaryDirectory(prefix="kaptionpro_") as tmp:
                (Path(tmp) / self.SUB_FILE_NAME).write_text(ass_content, encoding="utf-8")
                cmd = self.build_export_command(ffmpeg, src, partial, opts, info)
                log.info("FFmpeg export: %s | cwd=%s", " ".join(cmd), tmp)
                proc = subprocess.Popen(cmd, cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
                with self._active_lock:
                    self._active_procs.add(proc)
                lines: queue.Queue = queue.Queue()

                def reader() -> None:
                    try:
                        for line in proc.stdout:                 # type: ignore[union-attr]
                            lines.put(line)
                    except (OSError, ValueError):
                        pass
                    finally:
                        lines.put(None)

                threading.Thread(target=reader, daemon=True, name="kp-ffmpeg-reader").start()
                last_activity = time.monotonic()
                speed = ""
                try:
                    while True:
                        if cancel_event.is_set():
                            self._stop_or_raise(proc)
                            raise CancelledError("Dibatalkan user")
                        try:
                            line = lines.get(timeout=0.5)
                        except queue.Empty:
                            if time.monotonic() - last_activity > self.stall_timeout:
                                self._stop_or_raise(proc)
                                raise FFmpegError(f"FFmpeg tidak memberi respons selama {int(self.stall_timeout)} "
                                                  "detik (macet) — proses dihentikan.")
                            continue
                        if line is None:
                            break
                        last_activity = time.monotonic()
                        line = line.strip()
                        key, _, val = line.partition("=")
                        if key == "speed":
                            speed = val.strip()
                        if key in ("out_time_us", "out_time_ms") and val.isdigit() and progress_cb:
                            secs = int(val) / 1_000_000          # nilai ffmpeg dalam mikrodetik
                            extra = f" · {speed}" if speed and speed != "N/A" else ""
                            if duration > 0:
                                progress_cb(min(1.0, secs / duration),
                                            f"{fmt_clock(secs)} / {fmt_clock(duration)}{extra}")
                            else:
                                progress_cb(None, fmt_clock(secs) + extra)
                        elif line and key not in _PROGRESS_KEYS:
                            tail.append(line)
                    code = self._wait_exit(proc, cancel_event)
                    if code != 0:
                        raise FFmpegError("FFmpeg gagal (kode %d):\n%s" % (code, "\n".join(tail)))
                    if cancel_event.is_set():                    # Cancel saat FFmpeg baru selesai: jangan commit
                        raise CancelledError("Dibatalkan user")
                    if progress_cb:
                        progress_cb(1.0, "memvalidasi hasil…")
                    problems = self.validate_output(partial, info, duration, opts)
                    if problems:
                        raise FFmpegError("Validasi hasil export gagal:\n- " + "\n- ".join(problems))
                finally:
                    if proc.poll() is None:
                        self._terminate(proc)
                    if proc.poll() is not None:
                        with self._active_lock:
                            self._active_procs.discard(proc)
                    if proc.stdout:
                        try:
                            proc.stdout.close()
                        except OSError:
                            pass
            if cancel_event.is_set():                            # Cancel saat validasi: tujuan tidak disentuh
                raise CancelledError("Dibatalkan user")
            try:
                os.replace(partial, out)                         # COMMIT — satu-satunya langkah yang menyentuh tujuan
            except OSError as e:
                raise FFmpegError(f"Hasil export tidak bisa disimpan ke tujuan:\n{out}\n{e}\n\n"
                                  "Pastikan file tujuan tidak sedang dibuka program lain dan folder bisa ditulis.") from e
            committed = True
        finally:
            if not committed:
                self._discard_partial(partial)
        log.info("Export selesai: %s", out)
        res_info = self.probe_streams(str(out))
        return ExportResult(str(out), out.stat().st_size, res_info.duration if res_info else duration,
                            res_info.height if res_info else 0,
                            [] if res_info else ["ffprobe tidak tersedia: hanya ukuran file dan struktur MP4 "
                                                 "yang diperiksa (stream/durasi tidak)"])
