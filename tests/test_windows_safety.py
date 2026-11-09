"""
Tes keselamatan Windows (v1.9.10): kebocoran handle berkas yang membuat folder sementara gagal dihapus (WinError 32).

Di Windows berkas yang masih terbuka tidak bisa dihapus; di Linux bisa. Karena itu tes ini memeriksa penutupan resource
secara langsung (lewat /proc/self/fd di Linux, dan lewat operasi hapus berkas yang akan gagal di Windows).
"""

from __future__ import annotations

import gc
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kaptionpro.core.utils import InterProcessLock  # noqa: E402

try:
    from tests import winsafe
    from tests.winsafe import SafeTemporaryDirectory
except ImportError:          # dijalankan langsung dari folder tests/
    import winsafe
    from winsafe import SafeTemporaryDirectory

HAS_PROC_FD = os.path.isdir("/proc/self/fd")


class SafeTemporaryDirectoryTests(unittest.TestCase):
    @unittest.skipUnless(winsafe.STRICT, "pemeriksaan handle butuh /proc/self/fd (Linux)")
    def test_leaked_handle_fails_with_clear_message_but_dir_is_still_removed(self):
        tmp = SafeTemporaryDirectory()
        leak = open(Path(tmp.name) / "bocor.db", "wb")
        try:
            with self.assertRaises(AssertionError) as cm:
                tmp.cleanup()
            self.assertIn("bocor.db", str(cm.exception))
            self.assertIn("WinError 32", str(cm.exception))
        finally:
            leak.close()
        self.assertFalse(os.path.exists(tmp.name), "folder tetap harus dibersihkan walau ada kebocoran")

    @unittest.skipUnless(winsafe.STRICT, "pemeriksaan handle butuh /proc/self/fd (Linux)")
    def test_clean_directory_passes(self):
        with SafeTemporaryDirectory() as d:
            (Path(d) / "a.txt").write_text("x", encoding="utf-8")        # dibuka lalu DITUTUP: bukan kebocoran
        self.assertFalse(os.path.exists(d))

    def test_cleanup_retries_when_files_are_temporarily_locked(self):
        """Meniru antivirus/indeks Windows yang menahan berkas sebentar: dua percobaan gagal, ketiga berhasil."""
        real_rmtree, calls = shutil.rmtree, []

        def flaky(path, *a, **k):
            calls.append(path)
            if len(calls) <= 2:
                return None                                              # gagal diam-diam (ignore_errors) -> folder tersisa
            return real_rmtree(path, *a, **k)

        tmp = SafeTemporaryDirectory()
        with mock.patch.object(winsafe.shutil, "rmtree", flaky), \
                mock.patch.object(winsafe.time, "sleep", lambda s: None):
            tmp.cleanup()
        self.assertEqual(len(calls), 3)
        self.assertFalse(os.path.exists(tmp.name))

    def test_cleanup_gives_up_quietly_when_directory_cannot_be_removed(self):
        tmp = SafeTemporaryDirectory()
        try:
            with mock.patch.object(winsafe.shutil, "rmtree", lambda *a, **k: None), \
                    mock.patch.object(winsafe.time, "sleep", lambda s: None):
                tmp.cleanup()                       # tidak boleh melempar (sisa folder sementara tak berbahaya)
        finally:
            shutil.rmtree(tmp.name, ignore_errors=True)

    @unittest.skipUnless(HAS_PROC_FD, "butuh /proc/self/fd (Linux)")
    def test_open_handles_under_reports_only_files_inside_the_directory(self):
        with SafeTemporaryDirectory() as d, SafeTemporaryDirectory() as other:
            inside = open(Path(d) / "x.lock", "wb")
            outside = open(Path(other) / "y.lock", "wb")
            try:
                self.assertEqual(winsafe.open_handles_under(d), ["x.lock"])
            finally:
                inside.close()
                outside.close()
            self.assertEqual(winsafe.open_handles_under(d), [])


class ResourceReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = SafeTemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_interprocess_lock_closes_fd_when_object_is_dropped(self):
        """Jaring pengaman: fd mentah (os.open) tidak ikut tertutup oleh GC kecuali __del__ menutupnya."""
        path = self.dir / "x.lock"
        lk = InterProcessLock(path)
        self.assertTrue(lk.acquire())
        del lk
        gc.collect()
        probe = InterProcessLock(path)
        try:
            self.assertTrue(probe.acquire(), "kunci harus lepas setelah objek lock dibuang")
        finally:
            probe.release()
        if winsafe.STRICT:
            self.assertEqual(winsafe.open_handles_under(str(self.dir)), [])

    def test_lock_del_never_raises_even_when_fd_is_already_gone(self):
        lk = InterProcessLock(self.dir / "y.lock")
        self.assertTrue(lk.acquire())
        os.close(lk._fd)                                                 # fd sudah ditutup pihak lain
        lk.__del__()                                                     # tidak boleh melempar
        self.assertIsNone(lk._fd)

    def test_project_store_close_releases_locks_and_session_only_under_its_root(self):
        from kaptionpro.core.storage import ProjectStore
        from kaptionpro.models import Project
        with SafeTemporaryDirectory() as other_dir:
            a, b = ProjectStore(self.dir), ProjectStore(Path(other_dir))
            pa, pb = Project(name="a"), Project(name="b")
            try:
                self.assertTrue(a.lock(pa))
                self.assertTrue(b.lock(pb))
                a.session_begin()
                b.session_begin()
                a.close()
                a.close()                                                # idempoten
                probe = InterProcessLock(a._lock_path(pa))
                try:
                    self.assertTrue(probe.acquire(), "lock milik store a harus lepas")
                finally:
                    probe.release()
                probe_b = InterProcessLock(b._lock_path(pb))
                try:
                    self.assertFalse(probe_b.acquire(), "lock milik store LAIN tidak boleh ikut terlepas")
                finally:
                    probe_b.release()
                self.assertIsNone(a._session_lock)
                self.assertIsNotNone(b._session_lock)
            finally:
                b.close()
        if winsafe.STRICT:
            self.assertEqual(winsafe.open_handles_under(str(self.dir)), [])

    def test_corrupt_cache_database_is_closed_so_it_can_be_replaced(self):
        """Bug nyata: database rusak membuat cache 'dimatikan' tetapi koneksinya dibiarkan terbuka -> di Windows berkas
        rusak itu tidak bisa dihapus/diganti selama aplikasi berjalan."""
        from kaptionpro.core.translation_cache import TranslationCache
        path = self.dir / "cache.db"
        path.write_bytes(b"ini bukan database sqlite " * 50)
        cache = TranslationCache(path)
        try:
            self.assertEqual(cache.count(), 0)                          # cache nonaktif, tidak crash
            if winsafe.STRICT:
                self.assertEqual(winsafe.open_handles_under(str(self.dir)), [], "koneksi database rusak masih terbuka")
            path.unlink()                                                # di Windows gagal bila koneksi masih terbuka
            self.assertFalse(path.exists())
        finally:
            cache.close()

    def test_cache_close_releases_database_file(self):
        from kaptionpro.core.translation_cache import TranslationCache
        path = self.dir / "c.sqlite"
        cache = TranslationCache(path)
        cache.put_many({"a": "1"})
        cache.close()
        path.unlink()                                                    # di Windows gagal bila masih terbuka
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
