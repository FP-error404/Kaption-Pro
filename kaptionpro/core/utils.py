"""Helper murni: waktu, teks/ASS, file atomik, pid, dan pembatalan."""

from __future__ import annotations

import contextlib
import errno
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

log = logging.getLogger("KaptionPro")


def srt_time(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def ass_time(seconds: float) -> str:
    cs = int(round(max(0.0, seconds) * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def fmt_clock(seconds: float) -> str:
    total = int(max(0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


_ASS_NEWLINE = re.compile(r"\r\n|\r|\n")


def escape_ass(text: str) -> str:
    text = text.replace("{", "(").replace("}", ")")
    return _ASS_NEWLINE.sub(r"\\N", text)


def read_text_guess(path: str) -> str:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def is_valid_hex(h: str) -> bool:
    if h is None:
        return False
    h = h.strip().lstrip("#")
    return len(h) == 6 and all(c in "0123456789abcdefABCDEF" for c in h)


def normalize_hex(h: str, default: str = "#FFFFFF") -> str:
    if not is_valid_hex(h):
        return default
    h = h.strip().lstrip("#").upper()
    return "#" + h


def hex_to_ass_bgr(hex_color: str, alpha: int = 0) -> str:
    h = normalize_hex(hex_color).lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def ass_alpha_from_percent(pct: float) -> int:
    pct = max(0.0, min(100.0, pct))
    return int(round(255 * pct / 100.0))


def sanitize_ass_field(s: str) -> str:
    """Hilangkan koma (pemisah kolom di baris Style ASS)."""
    return s.replace(",", " ").strip() or "Arial"


_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, fallback: str = "subtitle", max_len: int = 120) -> str:
    """Bersihkan nama file dari karakter terlarang di Windows/macOS/Linux."""
    cleaned = _BAD_FILENAME_CHARS.sub("_", (name or "").strip()).strip(" .")
    return (cleaned or fallback)[:max_len]


def _clamp_num(v, default: float, lo: float, hi: float) -> float:
    """Ubah ke float, tolak NaN/inf/non-angka (-> default), lalu batasi ke [lo, hi]."""
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(x):
        return default
    return max(lo, min(hi, x))


# errno yang berarti "filesystem ini memang tidak mendukung fsync" (BUKAN kegagalan menulis ke media).
_FSYNC_UNSUPPORTED = {getattr(errno, n) for n in ("EINVAL", "ENOTSUP", "EOPNOTSUPP", "ENOSYS") if hasattr(errno, n)}


def _fsync_strict(fd: int) -> None:
    """fsync yang MELAPORKAN kegagalan (EIO, ENOSPC, EDQUOT, ...). Hanya 'tidak didukung' yang dimaklumi."""
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno in _FSYNC_UNSUPPORTED:
            log.debug("fsync tidak didukung filesystem ini (%s); dilanjutkan", e)
            return
        raise


def _fsync_dir(folder: Path) -> None:
    """POSIX: fsync folder agar rename-nya tahan crash. Dicatat bila gagal; file baru sudah terpasang, jadi tak dibatalkan."""
    if _IS_WIN:
        return
    try:
        fd = os.open(str(folder), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        if e.errno not in _FSYNC_UNSUPPORTED:
            log.warning("fsync folder %s gagal: %s", folder, e)


def _atomic_write(path: Path, text: str) -> None:
    """
    Tulis atomik + fsync: file lama tetap utuh bila crash/listrik mati/disk penuh di tengah penulisan.
    Kegagalan flush/fsync DILAPORKAN (OSError) dan file lama dipertahankan: data yang belum pasti sampai ke media
    tidak boleh menggantikan file yang sudah aman. File sementara bernama UNIK (pid + uuid) dan SELALU dihapus
    bila penulisan atau penggantian gagal.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            _fsync_strict(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


_PROJECT_ID_RE = re.compile(r"[0-9a-f]{32}")


def is_valid_project_id(value) -> bool:
    """ID project SELALU 32 karakter hex huruf kecil (uuid4().hex). ID dipakai membentuk path file, jadi apa pun
    selain format ini (kosong, pemisah path, '..', huruf besar, panjang salah, bukan string) harus ditolak."""
    return isinstance(value, str) and _PROJECT_ID_RE.fullmatch(value) is not None


def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.abspath(p)) if p else ""


def _pid_alive(pid: int) -> bool:
    """
    Cek apakah proses `pid` masih hidup TANPA mengirim sinyal apa pun.
    PENTING: di Windows `os.kill(pid, 0)` memanggil TerminateProcess -> MEMBUNUH proses itu
    (termasuk aplikasi lain bila PID-nya sudah dipakai ulang oleh Windows).
    """
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = k32.OpenProcess(0x1000, 0, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
            if handle is None or handle == 0:
                return ctypes.get_last_error() == 5            # ERROR_ACCESS_DENIED: ada, tapi tak boleh diakses
            try:
                code = ctypes.c_uint32()
                if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259                    # STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return False       # gagal mengecek -> anggap tidak hidup; TIDAK pernah mengirim sinyal
    try:
        os.kill(pid, 0)        # POSIX: signal 0 hanya mengecek keberadaan proses
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---------- lock antar-proses berbasis kunci OS ----------
_IS_WIN = sys.platform.startswith("win")
_O_BINARY = getattr(os, "O_BINARY", 0)
# Windows: kunci byte-range menghalangi proses lain MEMBACA byte yang terkunci. Kunci dipasang pada byte
# jauh di luar isi file sehingga info pemilik (PID) di awal file tetap bisa dibaca proses lain.
_LOCK_BYTE_OFFSET = 1 << 30
# Menghapus file lock hanya aman di POSIX (pembukaan ulang diverifikasi lewat inode, lihat acquire()). Di Windows file
# lock DIPERTAHANKAN permanen dan hanya kunci OS-nya yang dilepas: menghapusnya membuka celah (proses lain bisa sudah
# membuka/menunggu file itu, lalu proses ketiga membuat file baru bernama sama -> dua file lock berbeda).
_CAN_REMOVE_LOCK_FILE = not _IS_WIN
_CONTENTION_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK,
                      getattr(errno, "EDEADLK", -1), getattr(errno, "EDEADLOCK", -1)}


def _os_try_lock(fd: int) -> bool:
    """True = kunci didapat; False = dipegang pihak lain; OSError = kunci OS tidak bisa dipakai (fail-closed)."""
    if _IS_WIN:
        import msvcrt
        os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            if e.errno in _CONTENTION_ERRNOS:
                return False
            raise
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
        return True
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in _CONTENTION_ERRNOS:
            return False
        raise
    return True


def _os_unlock(fd: int) -> None:
    try:
        if _IS_WIN:
            import msvcrt
            os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass                      # menutup fd tetap melepas kunci


class InterProcessLock:
    """
    Lock antar-proses memakai KUNCI OS (flock di POSIX, LockFile/msvcrt di Windows) pada sebuah file.

    Kenapa bukan 'file ada = terkunci' (O_CREAT|O_EXCL + PID di dalamnya):
      * file baru bisa terbaca KOSONG oleh proses lain sebelum PID selesai ditulis -> dianggap rusak/basi
        lalu dihapus -> dua pemegang lock;
      * dua proses yang sama-sama 'membersihkan lock basi' bisa saling menghapus lock BARU milik lawannya.
    Dengan kunci OS tidak ada konsep 'basi': kunci dilepas OS saat proses mati/crash, jadi tak ada file yang
    perlu dinilai atau dihapus oleh pihak lain. Isi file (PID + waktu) hanya informasi untuk tampilan/log;
    keputusan memegang lock TIDAK pernah bergantung pada isinya.

    acquire(): True = dipegang | False = dipegang pihak lain (proses lain ATAU thread lain di proses ini)
               | OSError = lock tidak bisa dibuat/dikunci (pemanggil harus fail-closed).
    """

    def __init__(self, path: Path | str, remove_on_release: bool = False) -> None:
        self.path = Path(path)
        self.remove_on_release = remove_on_release
        self._fd: int | None = None

    def __del__(self) -> None:
        """Jaring pengaman: deskriptor berkas mentah (os.open) TIDAK ikut tertutup saat objek dibuang, sehingga lock yang
        lupa di-release() menahan berkasnya (di Windows: WinError 32 saat folder dihapus). Hanya MENUTUP fd (melepas kunci
        OS); tidak menghapus berkas. Tidak boleh melempar apa pun (dipanggil GC / saat interpreter keluar)."""
        try:
            fd, self._fd = self._fd, None
            if fd is not None:
                try:
                    _os_unlock(fd)
                finally:
                    os.close(fd)
        except Exception:  # noqa: BLE001
            pass

    @property
    def held(self) -> bool:
        return self._fd is not None

    def _same_file(self, fd: int) -> bool:
        """POSIX: pastikan path masih menunjuk file yang sama dengan fd (pemegang sebelumnya bisa menghapusnya)."""
        try:
            sp = os.stat(self.path)
        except OSError:
            return False
        sf = os.fstat(fd)
        return (sp.st_ino, sp.st_dev) == (sf.st_ino, sf.st_dev)

    def acquire(self, write_info: bool = True, create: bool = True) -> bool:
        """write_info=False: jangan menimpa isi file (untuk probe yang perlu membaca isi lama pemiliknya).
        create=False: JANGAN membuat file; bila file tidak ada (mis. baru dihapus pihak lain) return False. Wajib untuk
        pemindai/pembersih -- dengan O_CREAT mereka menghidupkan kembali file yang baru saja dihapus sebagai file kosong."""
        if self._fd is not None:
            return True
        flags = os.O_RDWR | _O_BINARY | (os.O_CREAT if create else 0)
        for _attempt in range(5):
            try:
                fd = os.open(str(self.path), flags, 0o644)
            except FileNotFoundError:
                if create:
                    raise
                return False
            try:
                got = _os_try_lock(fd)
                if got and not _IS_WIN and not self._same_file(fd):
                    _os_unlock(fd)                     # file dihapus/diganti saat kita membukanya -> ulangi
                    got = None
            except BaseException:
                os.close(fd)
                raise
            if got is None:
                os.close(fd)
                continue
            if not got:
                os.close(fd)
                return False
            self._fd = fd
            if write_info:
                self._write_info()
            return True
        raise OSError(errno.EAGAIN, "file lock terus berganti saat diambil")

    def _write_info(self) -> None:
        """Info pemilik (informasi saja). Gagal menulis TIDAK membatalkan lock: kunci OS-lah yang melindungi."""
        self.set_info(f"{os.getpid()}\n{datetime.now().isoformat(timespec='seconds')}\n")

    def set_info(self, text: str) -> bool:
        """Ganti isi file lock (hanya saat dipegang). Dipakai untuk info pemilik dan penanda 'selesai normal'."""
        if self._fd is None:
            return False
        data = text.encode("utf-8")
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, data)
            os.ftruncate(self._fd, len(data))
            return True
        except OSError as e:
            log.warning("Info lock tidak bisa ditulis (%s): %s", self.path.name, e)
            return False

    def read_info(self, limit: int = 256) -> str | None:
        """Baca isi file lock lewat fd yang SEDANG dipegang (bukan lewat path), sehingga yang dibaca pasti file yang
        sama dengan yang terkunci. None bila lock tidak dipegang / gagal membaca."""
        if self._fd is None:
            return None
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            return os.read(self._fd, limit).decode("utf-8", errors="replace")
        except OSError:
            return None

    def mtime(self) -> float | None:
        """Waktu modifikasi file yang dipegang (lewat fd). None bila lock tidak dipegang / gagal."""
        if self._fd is None:
            return None
        try:
            return os.fstat(self._fd).st_mtime
        except OSError:
            return None

    def still_current(self) -> bool:
        """True bila path masih menunjuk file yang sedang kita kunci (POSIX; di Windows file tak bisa dihapus
        selagi terbuka, jadi selalu True)."""
        if self._fd is None:
            return False
        return True if _IS_WIN else self._same_file(self._fd)

    def release(self) -> None:
        """Lepas lock. POSIX + remove_on_release: file dihapus SAAT lock masih dipegang, pemegang berikutnya
        memverifikasi ulang file-nya setelah mengunci (lihat acquire). Windows: file TIDAK dihapus."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        if self.remove_on_release and _CAN_REMOVE_LOCK_FILE and self._same_file(fd):
            try:
                os.unlink(self.path)
            except OSError:
                pass
        _os_unlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass

    @staticmethod
    def read_owner_pid(path: Path | str) -> int | None:
        """PID pemilik dari info di file (informasi saja; BUKAN penentu apakah lock dipegang)."""
        try:
            return int(Path(path).read_text(encoding="utf-8").split("\n", 1)[0].strip())
        except (OSError, ValueError):
            return None


@contextlib.contextmanager
def interprocess_critical_section(lock_path: Path | str, timeout: float = 10.0, poll: float = 0.02):
    """
    Bagian kritis antar-proses (dan antar-thread): `with interprocess_critical_section(path) as held:`.

    Dipakai untuk read-modify-write file bersama (mis. recent_projects.json): pembacaan data TERBARU dan penulisan
    hasilnya harus terjadi di dalam bagian ini, supaya perubahan instance lain tidak tertimpa (lost update).
    Menunggu sampai `timeout` detik. `held` = True bila lock didapat. False = timeout / lock tidak bisa dibuat:
    pemanggil memutuskan sendiri (fail-open atau fail-closed) -- helper ini tidak melempar exception.
    File lock tidak pernah dihapus (file kecil permanen) agar tidak ada celah dua inode berbeda untuk satu nama.
    """
    lk = InterProcessLock(lock_path)
    held = False
    try:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if lk.acquire(write_info=False):
                held = True
                break
            if time.monotonic() >= deadline:
                log.warning("Menunggu lock %s melewati %.1f dtk", Path(lock_path).name, timeout)
                break
            time.sleep(poll)
    except OSError as e:
        log.warning("Lock %s tidak bisa dibuat/dikunci: %s", Path(lock_path).name, e)
    try:
        yield held
    finally:
        if held:
            lk.release()


class WorkerRegistry:
    """
    Catatan SEMUA thread worker yang dimulai (AI / export), tanpa bergantung pada GUI.

    Membatalkan job melepas 'job aktif' di UI seketika, tetapi thread-nya baru benar-benar berhenti beberapa saat
    kemudian. Satu-satunya bukti worker sudah berhenti adalah thread.is_alive() == False -- BUKAN join(timeout)
    yang habis. Pemanggil penutupan aplikasi memakai alive() / wait_all() untuk memutuskan kapan resource aman
    dilepas.
    """

    def __init__(self) -> None:
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def start(self, thread: threading.Thread) -> None:
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()

    def alive(self) -> list[threading.Thread]:
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            return list(self._threads)

    def wait_all(self, timeout: float) -> bool:
        """Tunggu semua worker berhenti paling lama `timeout` detik. True HANYA bila tidak ada yang tersisa."""
        deadline = time.monotonic() + max(0.0, timeout)
        for t in self.alive():
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        return not self.alive()


class CancelledError(Exception):
    """Dilempar saat user menekan Cancel."""


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("Dibatalkan user")
