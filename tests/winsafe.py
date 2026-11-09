"""
Folder sementara untuk tes yang AMAN di Windows + penjaga kebocoran handle.

Masalah: Windows menolak menghapus berkas yang masih terbuka (WinError 32); Linux tidak. Kebocoran handle (lock, SQLite,
penanda sesi) karena itu tak terlihat di Linux dan baru meledak di CI Windows.

SafeTemporaryDirectory:
  * Linux/macOS: sebelum menghapus, periksa /proc/self/fd -- bila masih ada handle terbuka di dalam folder, tes GAGAL
    dengan daftar berkas yang bocor (kebocoran yang sama akan membuat Windows gagal). Folder tetap dibersihkan.
  * Windows: coba hapus beberapa kali (antivirus/indeks kadang menahan berkas sebentar), lalu menyerah TANPA error
    (sisa folder sementara tidak berbahaya). Kebocoran sungguhan sudah ditangkap oleh pemeriksaan di Linux.
Matikan penjaga Linux dengan KAPTION_STRICT_HANDLES=0 (mis. saat menyelidiki hal lain).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time

STRICT = os.name != "nt" and os.path.isdir("/proc/self/fd") and os.environ.get("KAPTION_STRICT_HANDLES", "1") != "0"


def open_handles_under(path: str) -> list[str]:
    """Berkas di dalam `path` yang masih dibuka proses ini (hanya Linux; kosong bila tidak bisa diperiksa)."""
    if not os.path.isdir("/proc/self/fd"):
        return []
    root = os.path.realpath(path)
    found: list[str] = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith(root + os.sep):
            found.append(os.path.relpath(target, root))
    return sorted(set(found))


class SafeTemporaryDirectory(tempfile.TemporaryDirectory):
    def cleanup(self) -> None:
        leaked = open_handles_under(self.name) if STRICT and os.path.isdir(self.name) else []
        for attempt in range(6):
            shutil.rmtree(self.name, ignore_errors=True)
            if not os.path.exists(self.name):
                break
            time.sleep(0.15 * (attempt + 1))
        try:
            self._finalizer.detach()
        except Exception:  # noqa: BLE001
            pass
        if leaked:
            raise AssertionError("handle berkas BOCOR saat folder sementara dibersihkan (di Windows ini = WinError 32): "
                                 + ", ".join(leaked) + " -- tutup/lepas resource-nya (close()/unlock()/session_end()) "
                                 "sebelum tes selesai")
