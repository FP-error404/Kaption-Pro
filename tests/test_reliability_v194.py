"""
Tes regresi v1.9.4 - v1.9.6 (Reliability Update, Hardening, Stabilization) untuk temuan P0/P1:
  1. Shutdown worker (WorkerRegistry, registri proses FFmpeg, penutupan paksa)
  2. Race pembersihan penanda sesi (kunci OS dulu, verifikasi ulang, hapus selagi terkunci)
  3. Lost update pada recent_projects.json / settings.json antar-instance
  4. Timeout + pembatalan responsif pada backend terjemahan

Tes ini TIDAK membutuhkan CustomTkinter/GUI.
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kaptionpro.core.utils import (CancelledError, InterProcessLock, WorkerRegistry,   # noqa: E402
                                   interprocess_critical_section)

IS_WIN = sys.platform.startswith("win")
CHILD_ENV = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])}


# ============================================================ 1. shutdown worker
class WorkerRegistryTests(unittest.TestCase):
    def test_join_timeout_is_not_proof_that_worker_stopped(self):
        """Inti temuan: join(timeout) yang habis TIDAK berarti worker berhenti. wait_all() harus jujur soal itu."""
        release = threading.Event()
        reg = WorkerRegistry()
        reg.start(threading.Thread(target=release.wait, daemon=True, name="kp-slow"))
        self.assertFalse(reg.wait_all(0.2), "worker masih berjalan: wait_all harus mengembalikan False")
        self.assertEqual([t.name for t in reg.alive()], ["kp-slow"])
        release.set()
        self.assertTrue(reg.wait_all(5.0))
        self.assertEqual(reg.alive(), [])

    def test_registry_keeps_orphan_workers_after_a_new_one_starts(self):
        """Job lama yang dibatalkan tapi belum mati tidak boleh 'hilang dari catatan' saat job baru dimulai."""
        a_release, b_release = threading.Event(), threading.Event()
        reg = WorkerRegistry()
        reg.start(threading.Thread(target=a_release.wait, daemon=True, name="a"))
        reg.start(threading.Thread(target=b_release.wait, daemon=True, name="b"))
        self.assertEqual(sorted(t.name for t in reg.alive()), ["a", "b"])
        a_release.set()
        self.assertTrue(reg.alive() == [t for t in reg.alive() if t.name == "b"] or reg.wait_all(0.0) is False)
        b_release.set()
        self.assertTrue(reg.wait_all(5.0))

    def test_finished_workers_are_forgotten(self):
        reg = WorkerRegistry()
        reg.start(threading.Thread(target=lambda: None, daemon=True))
        self.assertTrue(reg.wait_all(5.0))
        self.assertEqual(reg.alive(), [])


class FFmpegStopTests(unittest.TestCase):
    def setUp(self) -> None:
        from kaptionpro.services import ffmpeg_service as fs
        self.fs = fs
        self.svc = fs.FFmpegService
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for p in self.procs:
            try:
                p.kill()
                p.wait(timeout=10)
            except OSError:
                pass
        with self.svc._active_lock:
            self.svc._active_procs.clear()

    def test_stop_budget_covers_terminate_kill_and_validation(self):
        """Batas tunggu penutupan harus >= terminate + kill, supaya join 2 detik lama tidak terulang."""
        self.assertGreaterEqual(self.svc.MAX_STOP_SECONDS, self.svc.TERMINATE_WAIT + self.svc.KILL_WAIT)
        self.assertGreater(self.svc.MAX_STOP_SECONDS, 2.0)

    def test_kill_all_active_stops_orphan_processes_for_forced_close(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.procs.append(p)
        with self.svc._active_lock:
            self.svc._active_procs.add(p)
        self.assertEqual(self.svc.active_process_count(), 1)
        self.assertEqual(self.svc.kill_all_active(), 1)
        p.wait(timeout=10)
        self.assertIsNotNone(p.poll())
        self.assertEqual(self.svc.active_process_count(), 0)

    def test_kill_all_active_is_safe_when_nothing_runs(self):
        self.assertEqual(self.svc.kill_all_active(), 0)


# ============================================================ 2. session cleanup
class SessionCleanupRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        from kaptionpro.core.storage import ProjectStore
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = ProjectStore(self.root)
        self.d = self.store.sessions_dir
        self.d.mkdir(parents=True, exist_ok=True)

    def _old_marker(self, name: str, text: str = "CLEAN\n") -> Path:
        f = self.d / f"{name}.session"
        f.write_text(text, encoding="utf-8")
        old = time.time() - 3 * 24 * 3600
        os.utime(f, (old, old))
        return f

    def test_old_clean_marker_is_removed(self):
        f = self._old_marker("old")
        self.store._cleanup_session_markers()
        self.assertFalse(f.exists())

    def test_marker_locked_by_another_holder_is_never_removed(self):
        f = self._old_marker("busy")
        holder = InterProcessLock(f)
        self.assertTrue(holder.acquire(write_info=False))
        try:
            old = time.time() - 3 * 24 * 3600
            os.utime(f, (old, old))
            before = os.stat(f).st_ino
            self.store._cleanup_session_markers()
            self.assertTrue(f.exists(), "marker yang kuncinya dipegang pihak lain tidak boleh dihapus")
            self.assertEqual(os.stat(f).st_ino, before)
        finally:
            holder.release()

    def test_state_is_rechecked_after_lock_is_taken(self):
        """Marker tampak 'CLEAN & lama' saat disaring, tetapi dihidupkan lagi sebelum kunci didapat."""
        f = self._old_marker("revived")
        real_acquire = InterProcessLock.acquire

        def acquire_then_revive(lk, write_info=True, create=True):
            got = real_acquire(lk, write_info, create)
            if got and lk.path == f:                       # pemilik baru memperbarui marker tepat sebelum kita mengunci
                lk.set_info(f"{os.getpid()}\n2026-11-07T00:00:00\n")
            return got

        with mock.patch.object(InterProcessLock, "acquire", acquire_then_revive):
            self.store._cleanup_session_markers()
        self.assertTrue(f.exists(), "status/umur harus diperiksa ULANG saat kunci dipegang")
        self.assertTrue(f.read_text(encoding="utf-8").startswith(str(os.getpid())))

    @unittest.skipIf(IS_WIN, "POSIX: penghapusan dilakukan selagi kunci dipegang")
    def test_unlink_happens_while_lock_is_still_held(self):
        f = self._old_marker("order")
        observed: list[bool] = []
        real_unlink = os.unlink

        def checking_unlink(path, *a, **k):
            if str(path).endswith("order.session"):
                probe = InterProcessLock(path)
                try:
                    observed.append(probe.acquire(write_info=False))   # False = masih dikunci pembersih
                finally:
                    probe.release()
            return real_unlink(path, *a, **k)

        with mock.patch("os.unlink", checking_unlink):
            self.store._cleanup_session_markers()
        self.assertEqual(observed, [False], "unlink harus terjadi SELAGI kunci dipegang, bukan sesudahnya")
        self.assertFalse(f.exists())

    def test_acquire_without_create_never_resurrects_a_deleted_file(self):
        """Akar bug: pembersih membuka dengan O_CREAT lalu menghidupkan lagi marker yang baru dihapus (file kosong)."""
        gone = self.d / "gone.session"
        lk = InterProcessLock(gone)
        self.assertFalse(lk.acquire(write_info=False, create=False))
        self.assertFalse(gone.exists(), "acquire(create=False) tidak boleh membuat file")
        self.assertTrue(lk.acquire(write_info=False))                       # perilaku default tetap membuat file
        lk.release()
        self.assertTrue(gone.exists())

    def test_cleanup_does_not_resurrect_marker_deleted_between_scan_and_lock(self):
        """Deterministik: marker lolos saringan umur, lalu dihapus proses lain tepat sebelum kita mengunci."""
        f = self._old_marker("race")
        real_acquire = InterProcessLock.acquire

        def delete_then_acquire(lk, *args, **kwargs):
            if lk.path == f and f.exists():
                f.unlink()                                          # proses lain menghapusnya lebih dulu
            return real_acquire(lk, *args, **kwargs)

        with mock.patch.object(InterProcessLock, "acquire", delete_then_acquire):
            self.store._cleanup_session_markers()
        self.assertFalse(f.exists(), "file yang sudah dihapus tidak boleh dibuat ulang sebagai file kosong")

    def test_session_scan_does_not_recreate_vanished_markers(self):
        vanished = self.d / "vanished.session"
        with mock.patch.object(Path, "glob", lambda self_, pattern: iter([vanished])):
            self.assertFalse(self.store.session_begin())
        self.assertFalse(vanished.exists())
        self.store.session_end()

    def test_concurrent_cleanups_leave_held_marker_intact(self):
        """Beberapa proses membersihkan bersamaan; marker yang dipegang tetap utuh dan satu inode."""
        held = self._old_marker("held")
        for i in range(20):
            self._old_marker(f"junk{i}")
        holder = InterProcessLock(held)
        self.assertTrue(holder.acquire(write_info=False))
        old = time.time() - 3 * 24 * 3600
        os.utime(held, (old, old))
        inode = os.stat(held).st_ino
        code = ("import sys\nfrom pathlib import Path\nfrom kaptionpro.core.storage import ProjectStore\n"
                "st = ProjectStore(Path(sys.argv[1]))\nfor _ in range(30):\n    st._cleanup_session_markers()\n")
        try:
            procs = [subprocess.Popen([sys.executable, "-c", code, str(self.root)], env=CHILD_ENV,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
            outs = [p.communicate(timeout=120) for p in procs]
            self.assertEqual([p.returncode for p in procs], [0] * 4, [o[1][-300:] for o in outs])
            self.assertTrue(held.exists())
            self.assertEqual(os.stat(held).st_ino, inode)
            self.assertEqual(sorted(p.name for p in self.d.glob("junk*.session")), [])
        finally:
            holder.release()


# ============================================================ 3. lost update
class RecentListSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        from kaptionpro.core.storage import ProjectStore, SettingsStore
        from kaptionpro.models import Project
        self.ProjectStore, self.SettingsStore, self.Project = ProjectStore, SettingsStore, Project
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _names(self) -> list[str]:
        return sorted(p.name for p in self.ProjectStore(self.root).projects)

    def test_second_instance_does_not_overwrite_first_instances_addition(self):
        a, b = self.ProjectStore(self.root), self.ProjectStore(self.root)      # keduanya membaca daftar KOSONG
        a.add(self.Project(name="dari-A"))
        b.add(self.Project(name="dari-B"))                                      # b tidak tahu soal dari-A
        self.assertEqual(self._names(), ["dari-A", "dari-B"])
        self.assertEqual(sorted(p.name for p in b.projects), ["dari-A", "dari-B"])

    def test_removal_is_not_resurrected_by_stale_instance(self):
        a, b = self.ProjectStore(self.root), self.ProjectStore(self.root)
        p1 = self.Project(name="p1")
        a.add(p1)
        b.reload()
        a.remove_from_recent(p1)
        b.add(self.Project(name="p2"))                                          # b masih punya p1 di memori
        self.assertEqual(self._names(), ["p2"])

    def test_reload_shows_other_instances_changes(self):
        a, b = self.ProjectStore(self.root), self.ProjectStore(self.root)
        a.add(self.Project(name="baru"))
        self.assertEqual(b.projects, [])
        b.reload()
        self.assertEqual([p.name for p in b.projects], ["baru"])

    def test_reload_keeps_memory_when_file_is_unreadable(self):
        a = self.ProjectStore(self.root)
        a.add(self.Project(name="aman"))
        a.list_path.write_text("{ rusak", encoding="utf-8")
        a.reload()                                                              # tidak boleh mengosongkan / karantina
        self.assertEqual([p.name for p in a.projects], ["aman"])
        self.assertTrue(a.list_path.exists())

    def test_many_processes_adding_concurrently_lose_nothing(self):
        code = ("import sys\nfrom pathlib import Path\nfrom kaptionpro.core.storage import ProjectStore\n"
                "from kaptionpro.models import Project\nst = ProjectStore(Path(sys.argv[1]))\n"
                "for i in range(5):\n    st.add(Project(name=f'{sys.argv[2]}-{i}'))\n")
        procs = [subprocess.Popen([sys.executable, "-c", code, str(self.root), f"w{n}"], env=CHILD_ENV,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for n in range(4)]
        outs = [p.communicate(timeout=180) for p in procs]
        self.assertEqual([p.returncode for p in procs], [0] * 4, [o[1][-300:] for o in outs])
        expected = sorted(f"w{n}-{i}" for n in range(4) for i in range(5))      # 20 <= MAX_ITEMS
        self.assertEqual(self._names(), expected, "tidak boleh ada penambahan yang hilang (lost update)")
        json.loads((self.root / "recent_projects.json").read_text(encoding="utf-8"))   # JSON tetap valid

    def test_list_write_is_failclosed_when_lock_cannot_be_obtained(self):
        """v1.9.5: tanpa lock TIDAK menulis ke disk (tak menimpa perubahan instance lain dengan data basi)."""
        a, other = self.ProjectStore(self.root), self.ProjectStore(self.root)
        other.add(self.Project(name="dari-instance-lain"))
        a.LIST_LOCK_TIMEOUT = 0.2
        blocker = InterProcessLock(self.root / "recent_projects.json.lock")
        self.assertTrue(blocker.acquire(write_info=False))
        try:
            before = a.list_path.read_bytes()
            t0 = time.monotonic()
            ok = a.add(self.Project(name="tertunda"))
            self.assertLess(time.monotonic() - t0, 5.0, "tidak boleh menggantung selamanya menunggu lock")
        finally:
            blocker.release()
        self.assertFalse(ok)
        self.assertIn("Lock daftar", a.last_save_error or "")
        self.assertEqual(a.list_path.read_bytes(), before, "disk tidak boleh disentuh tanpa lock")
        self.assertEqual(self._names(), ["dari-instance-lain"])
        self.assertIn("tertunda", [p.name for p in a.projects])            # memori tetap konsisten untuk sesi ini
        self.assertTrue(a.add(self.Project(name="berikutnya")))             # begitu lock tersedia, tersimpan normal
        self.assertEqual(self._names(), ["berikutnya", "dari-instance-lain"])
        self.assertIsNone(a.last_save_error)

    def test_remove_and_delete_report_failure_when_list_lock_unavailable(self):
        a = self.ProjectStore(self.root)
        p = self.Project(name="x")
        self.assertTrue(a.add(p))
        a.LIST_LOCK_TIMEOUT = 0.2
        blocker = InterProcessLock(self.root / "recent_projects.json.lock")
        self.assertTrue(blocker.acquire(write_info=False))
        try:
            self.assertFalse(a.remove_from_recent(p))
            self.assertFalse(a.delete_project(p))
            self.assertIn("daftar belum bisa diperbarui", a.last_delete_error or "")
        finally:
            blocker.release()
        self.assertEqual(self._names(), ["x"], "daftar di disk tidak boleh berubah tanpa lock")

    def test_load_without_lock_is_readonly_and_does_not_repair(self):
        """_load() tanpa lock: tidak boleh karantina/pemulihan/penulisan (hanya baca)."""
        good = self.ProjectStore(self.root)
        good.add(self.Project(name="ada-di-bak"))
        good.add(self.Project(name="ada-di-bak-2"))                         # -> .bak berisi versi sebelumnya
        good.list_path.write_text("{ rusak", encoding="utf-8")
        blocker = InterProcessLock(self.root / "recent_projects.json.lock")
        self.assertTrue(blocker.acquire(write_info=False))
        try:
            with mock.patch.object(self.ProjectStore, "LIST_LOCK_TIMEOUT", 0.2):
                st = self.ProjectStore(self.root)
        finally:
            blocker.release()
        self.assertEqual(good.list_path.read_text(encoding="utf-8"), "{ rusak", "file tidak boleh ditulis/dipindah")
        self.assertEqual(list(self.root.glob("recent_projects.json.corrupt-*")), [], "tidak boleh ada karantina")
        self.assertEqual([p.name for p in st.projects], ["ada-di-bak"])      # dibaca dari .bak
        self.assertTrue(any("ditunda" in n for n in st.notes))
        repaired = self.ProjectStore(self.root)                              # dengan lock: pemulihan permanen
        self.assertEqual([p.name for p in repaired.projects], ["ada-di-bak"])
        self.assertEqual(len(list(self.root.glob("recent_projects.json.corrupt-*"))), 1)

    def test_load_without_lock_writes_no_rejected_file_or_migration(self):
        good = self.ProjectStore(self.root)
        good.list_path.write_text(json.dumps([{"name": "tanpa-id"}, {"name": "id-jahat", "id": "../../x"}]),
                                  encoding="utf-8")
        before = good.list_path.read_bytes()
        blocker = InterProcessLock(self.root / "recent_projects.json.lock")
        self.assertTrue(blocker.acquire(write_info=False))
        try:
            with mock.patch.object(self.ProjectStore, "LIST_LOCK_TIMEOUT", 0.2):
                st = self.ProjectStore(self.root)
        finally:
            blocker.release()
        self.assertEqual(good.list_path.read_bytes(), before)
        self.assertEqual(list(self.root.glob("recent_projects.rejected-*")), [])
        self.assertEqual([p.name for p in st.projects], ["tanpa-id"])

    def test_settings_set_is_failclosed_without_lock(self):
        a, b = self.SettingsStore(self.root), self.SettingsStore(self.root)
        b.set("translator", "argos")
        a.LOCK_TIMEOUT = 0.2
        blocker = InterProcessLock(self.root / "settings.json.lock")
        self.assertTrue(blocker.acquire(write_info=False))
        try:
            before = a.path.read_bytes()
            self.assertFalse(a.set("volume", 5))
        finally:
            blocker.release()
        self.assertEqual(a.path.read_bytes(), before, "settings di disk tidak boleh ditimpa tanpa lock")
        self.assertEqual(a.get("volume"), 5)                                 # tetap berlaku di sesi ini
        self.assertTrue(a.set("muted", True))                                # lock tersedia: tersimpan + merge
        data = json.loads(a.path.read_text(encoding="utf-8"))
        self.assertEqual((data["translator"], data["muted"]), ("argos", True))

    def test_critical_section_reports_timeout_without_raising(self):
        path = self.root / "x.lock"
        blocker = InterProcessLock(path)
        self.assertTrue(blocker.acquire(write_info=False))
        try:
            with interprocess_critical_section(path, timeout=0.1) as held:
                self.assertFalse(held)
        finally:
            blocker.release()
        with interprocess_critical_section(path, timeout=0.1) as held:
            self.assertTrue(held)

    def test_settings_set_merges_with_other_instances_keys(self):
        a, b = self.SettingsStore(self.root), self.SettingsStore(self.root)
        a.set("volume", 10)
        b.set("translator", "argos")                                           # b belum tahu volume diubah a
        data = json.loads((self.root / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual((data["volume"], data["translator"]), (10, "argos"))
        self.assertEqual((b.get("volume"), b.get("translator")), (10, "argos"))

    def test_settings_survive_corrupt_file(self):
        (self.root / "settings.json").write_text("bukan json", encoding="utf-8")
        st = self.SettingsStore(self.root)
        self.assertEqual(st.get("volume"), 80)
        st.set("volume", 33)
        self.assertEqual(json.loads((self.root / "settings.json").read_text(encoding="utf-8"))["volume"], 33)


# ============================================================ 4. timeout terjemahan
class TranslationTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        from kaptionpro.services import ai_service
        self.ai = ai_service
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.svc = ai_service.AiService(Path(self._tmp.name) / "cache.sqlite")
        self.addCleanup(self.svc.close)
        self.release = threading.Event()
        self.addCleanup(self.release.set)                 # bebaskan thread yang ditinggalkan agar tes bersih

    def _segs(self, n: int = 3):
        from kaptionpro.models import SubtitleSegment
        return [SubtitleSegment(float(i), float(i) + 0.9, f"kalimat {i}", []) for i in range(n)]

    def _register(self, cls) -> str:
        self.addCleanup(lambda k=cls.key: self.ai.BACKENDS.pop(k, None))
        self.ai.BACKENDS[cls.key] = cls
        return cls.key

    def test_call_with_deadline_times_out_on_hung_call(self):
        t0 = time.monotonic()
        with self.assertRaises(self.ai.RequestTimeout):
            self.ai.call_with_deadline(lambda: self.release.wait(30), timeout=0.3)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertGreaterEqual(self.ai.abandoned_calls(), 1)
        self.release.set()
        deadline = time.monotonic() + 5
        while self.ai.abandoned_calls() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.ai.abandoned_calls(), 0)

    def test_call_with_deadline_is_cancellable_while_network_call_hangs(self):
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        t0 = time.monotonic()
        with self.assertRaises(CancelledError):
            self.ai.call_with_deadline(lambda: self.release.wait(30), timeout=60, cancel_event=cancel)
        self.assertLess(time.monotonic() - t0, 3.0, "Cancel harus tanggap walau panggilan jaringan menggantung")

    def test_call_with_deadline_returns_value_and_propagates_errors(self):
        self.assertEqual(self.ai.call_with_deadline(lambda: "ok", timeout=5), "ok")
        with self.assertRaises(ValueError):
            self.ai.call_with_deadline(lambda: (_ for _ in ()).throw(ValueError("x")), timeout=5)
        self.assertEqual(self.ai.call_with_deadline(lambda: 7, timeout=None), 7)     # jalur offline (tanpa thread)

    def test_translation_cancel_is_responsive_when_backend_hangs(self):
        release = self.release

        class Hanging(self.ai.TranslatorBackend):
            key, label, max_workers, call_timeout = "hang-test", "hang", 2, 60.0

            def translate(self, text, src, tgt):
                release.wait(30)
                return "x"

        key = self._register(Hanging)
        cancel, result = threading.Event(), {}

        def run():
            try:
                self.svc.translate_to_indonesian(self._segs(4), "Korean", None, cancel, backend_key=key, source_code="ko")
            except BaseException as e:                         # noqa: BLE001
                result["exc"] = e

        t = threading.Thread(target=run, daemon=True)
        t.start()
        time.sleep(0.4)
        cancel.set()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive(), "terjemahan harus berhenti cepat setelah Cancel walau backend menggantung")
        self.assertIsInstance(result.get("exc"), CancelledError)

    def test_hung_requests_become_failures_not_a_frozen_job(self):
        release = self.release

        class Slow(self.ai.TranslatorBackend):
            key, label, max_workers, call_timeout = "slow-test", "slow", 2, 0.15

            def translate(self, text, src, tgt):
                release.wait(30)
                return "x"

        key = self._register(Slow)
        with mock.patch.object(self.ai.time, "sleep", lambda *_: None), \
                mock.patch.object(self.ai.random, "random", lambda: 0.0):
            out = self.svc.translate_to_indonesian(self._segs(2), "Korean", None, None, backend_key=key,
                                                   source_code="ko")
        self.assertEqual(self.svc.last_translate_failures, 2)
        self.assertEqual([s.text for s in out], ["kalimat 0", "kalimat 1"])      # teks asli dipertahankan

    def test_total_time_budget_stops_translation_and_keeps_partial_state(self):
        class Fast(self.ai.TranslatorBackend):
            key, label, max_workers, call_timeout = "fast-test", "fast", 1, None

            def translate(self, text, src, tgt):
                return "terjemahan"

        key = self._register(Fast)
        self.svc.TRANSLATE_TOTAL_TIMEOUT = 0.0
        out = self.svc.translate_to_indonesian(self._segs(3), "Korean", None, None, backend_key=key, source_code="ko")
        self.assertTrue(self.svc.last_translate_stats["aborted"])
        self.assertTrue(self.svc.last_translate_stats["timed_out"])
        self.assertEqual(self.svc.last_translate_failures, 3)
        self.assertEqual(len(out), 3)

    def test_normal_translation_still_works(self):
        class Ok(self.ai.TranslatorBackend):
            key, label, max_workers, call_timeout = "ok-test", "ok", 3, 5.0

            def translate(self, text, src, tgt):
                return text.upper()

        key = self._register(Ok)
        out = self.svc.translate_to_indonesian(self._segs(5), "Korean", None, None, backend_key=key, source_code="ko")
        self.assertEqual([s.text for s in out], [f"KALIMAT {i}" for i in range(5)])
        self.assertEqual(self.svc.last_translate_failures, 0)

    def test_cache_can_be_closed_and_stays_safe_afterwards(self):
        self.svc.close()
        self.svc.close()                                         # aman dipanggil berulang
        self.assertEqual(self.svc.cache.get_many(["a"]), {})
        self.svc.cache.put_many({"a": "b"})                      # tidak melempar exception
        self.assertEqual(self.svc.cache.count(), 0)

# ============================================================ SQLite: close() vs operasi cache
class CacheCloseRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        from kaptionpro.core.storage import TranslationCache
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = TranslationCache(Path(self._tmp.name) / "c.sqlite")
        self.cache.put_many({"a": "1"})

    def _closing_lock(self):
        """Lock yang meniru close() yang menyelinap TEPAT sebelum worker masuk ke bagian terkunci."""
        cache, real = self.cache, self.cache._lock

        class ClosingLock:
            def __enter__(self_inner):
                real.acquire()
                conn, cache._conn = cache._conn, None
                conn.close()
                return self_inner

            def __exit__(self_inner, *exc):
                real.release()
                return False

        return ClosingLock()

    def test_operations_survive_close_sneaking_in_before_lock(self):
        for call in (lambda: self.cache.get_many(["a"]), lambda: self.cache.put_many({"b": "2"}),
                     self.cache.count, self.cache.clear):
            with self.subTest(call=call):
                from kaptionpro.core.storage import TranslationCache
                self.setUp()
                self.cache._lock = self._closing_lock()
                call()                                           # tidak boleh AttributeError ('NoneType'.execute)

    def test_concurrent_close_and_use_never_raises(self):
        errors: list[BaseException] = []
        stop = threading.Event()

        def user():
            try:
                while not stop.is_set():
                    self.cache.get_many(["a"])
                    self.cache.put_many({"k": "v"})
                    self.cache.count()
            except BaseException as e:                           # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=user, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        self.cache.close()
        time.sleep(0.2)
        stop.set()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(errors, [])


# ============================================================ v1.9.6: queue UI berbatas
class DrainQueueTests(unittest.TestCase):
    def test_processes_at_most_max_tasks_and_reports_backlog(self):
        import queue
        from kaptionpro.core.lifecycle import drain_queue
        q, seen = queue.Queue(), []
        for i in range(250):
            q.put((seen.append, (i,)))
        done, more = drain_queue(q, 100, lambda: None)
        self.assertEqual((done, more, len(seen)), (100, True, 100))
        done, more = drain_queue(q, 100, lambda: None)
        self.assertEqual((done, more), (100, True))
        done, more = drain_queue(q, 100, lambda: None)
        self.assertEqual((done, more, len(seen)), (50, False, 250))
        self.assertEqual(seen, list(range(250)), "urutan callback harus dijaga")

    def test_empty_queue_and_exact_batch(self):
        import queue
        from kaptionpro.core.lifecycle import drain_queue
        q = queue.Queue()
        self.assertEqual(drain_queue(q, 10, lambda: None), (0, False))
        for _ in range(10):
            q.put((lambda: None, ()))
        self.assertEqual(drain_queue(q, 10, lambda: None), (10, False))

    def test_failing_callback_does_not_stop_the_rest(self):
        import queue
        from kaptionpro.core.lifecycle import drain_queue
        q, seen, errors = queue.Queue(), [], []

        def boom():
            raise RuntimeError("x")

        q.put((boom, ()))
        q.put((seen.append, ("setelah",)))
        done, more = drain_queue(q, 10, lambda: errors.append(1))
        self.assertEqual((done, more, seen, errors), (2, False, ["setelah"], [1]))


# ============================================================ v1.9.6: validasi MP4 tanpa ffprobe
class Mp4StructureTests(unittest.TestCase):
    def setUp(self) -> None:
        from kaptionpro.services import ffmpeg_service as fs
        from tests.test_data_safety import FFmpegSafetyTests
        self.fs, self.mp4_bytes = fs, FFmpegSafetyTests.mp4_bytes
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _check(self, data: bytes, name: str = "a.mp4"):
        f = self.dir / name
        f.write_bytes(data)
        return self.fs.FFmpegService.check_mp4_structure(f)

    def test_valid_structure_passes(self):
        self.assertIsNone(self._check(self.mp4_bytes()))

    def test_truncated_file_is_detected(self):
        self.assertIn("terpotong", self._check(self.mp4_bytes()[:3000]))

    def test_garbage_and_empty_are_rejected(self):
        self.assertIsNotNone(self._check(b"x" * 4096))
        self.assertIsNotNone(self._check(b""))
        self.assertIsNotNone(self._check(b"\x00" * 4096))

    def test_missing_moov_or_mdat_is_rejected(self):
        self.assertIn("moov", self._check(self.mp4_bytes(moov=False)))
        self.assertIn("mdat", self._check(self.mp4_bytes(mdat=False)))

    def test_64bit_and_to_eof_boxes_are_handled(self):
        head = b"\x00\x00\x00\x14ftypisom\x00\x00\x02\x00isom" + b"\x00\x00\x00\x10moov" + b"\x00" * 8
        big = b"\x00\x00\x00\x01mdat" + (16 + 100).to_bytes(8, "big") + b"\x00" * 100        # largesize
        self.assertIsNone(self._check(head + big))
        eof = b"\x00\x00\x00\x00mdat" + b"\x00" * 100                                        # size 0 = sampai EOF
        self.assertIsNone(self._check(head + eof))

    def test_invalid_box_size_is_rejected(self):
        bad = b"\x00\x00\x00\x14ftypisom\x00\x00\x02\x00isom" + b"\x00\x00\x00\x04moov"   # size < header
        self.assertIn("tidak valid", self._check(bad))

    def test_validate_output_without_ffprobe_uses_structure_check_only_for_mp4(self):
        svc = self.fs.FFmpegService()
        opts = self.fs.ExportOptions()
        with mock.patch.object(self.fs.FFmpegService, "ffprobe_path", return_value=None):
            good, bad, other = self.dir / "g.mp4", self.dir / "b.mp4", self.dir / "o.mkv"
            good.write_bytes(self.mp4_bytes())
            bad.write_bytes(b"x" * 4096)
            other.write_bytes(b"x" * 4096)
            self.assertEqual(svc.validate_output(good, None, 10.0, opts), [])
            self.assertTrue(any("struktur MP4" in p for p in svc.validate_output(bad, None, 10.0, opts)))
            self.assertEqual(svc.validate_output(other, None, 10.0, opts), [], "bukan MP4: tidak diperiksa strukturnya")


# ============================================================ v1.9.6: privasi + retensi cache
class PrivacyAndRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        from kaptionpro.core.storage import TranslationCache
        from kaptionpro.services import ai_service
        self.ai, self.TranslationCache = ai_service, TranslationCache
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "c.sqlite"

    def test_online_backends_are_flagged_and_offline_is_not(self):
        self.assertTrue(self.ai.GoogleBackend.online)
        self.assertTrue(self.ai.MyMemoryBackend.online)
        self.assertFalse(self.ai.ArgosBackend.online)

    def test_online_notice_is_shown_once_only_for_online_backends(self):
        n = self.ai.needs_online_notice
        self.assertTrue(n(self.ai.GoogleBackend, False))
        self.assertFalse(n(self.ai.GoogleBackend, True))
        self.assertFalse(n(self.ai.ArgosBackend, False))

    def test_subtitle_text_never_appears_in_logs_on_translation_failure(self):
        secret = "KALIMAT-RAHASIA-PRIBADI-12345"

        class Failing(self.ai.TranslatorBackend):
            key, label, max_workers, call_timeout = "fail-privacy", "fail", 1, None

            def translate(self, text, src, tgt):
                raise RuntimeError("jaringan putus")

        from kaptionpro.models import SubtitleSegment
        self.addCleanup(lambda: self.ai.BACKENDS.pop("fail-privacy", None))
        self.ai.BACKENDS["fail-privacy"] = Failing
        svc = self.ai.AiService(self.path)
        self.addCleanup(svc.close)
        with mock.patch.object(self.ai.time, "sleep", lambda *_: None), \
                self.assertLogs("KaptionPro", level="DEBUG") as cm:
            svc.translate_to_indonesian([SubtitleSegment(0.0, 1.0, secret, [])], "Korean", None, None,
                                        backend_key="fail-privacy", source_code="ko")
        joined = "\n".join(cm.output)
        self.assertNotIn(secret, joined)
        self.assertIn("karakter", joined)                          # masih ada petunjuk diagnostik (panjang teks)

    def test_prune_removes_entries_older_than_max_age(self):
        c = self.TranslationCache(self.path)
        c.put_many({"baru": "1", "lama": "2"})
        with c._lock:
            c._conn.execute("UPDATE tr SET ts = ? WHERE k = 'lama'", (time.time() - 400 * 86400,))
            c._conn.commit()
        self.assertEqual(c.prune(max_age_days=180), 1)
        self.assertEqual(c.get_many(["baru", "lama"]), {"baru": "1"})

    def test_prune_enforces_entry_cap_by_dropping_oldest(self):
        c = self.TranslationCache(self.path)
        c.put_many({f"k{i}": str(i) for i in range(10)})
        with c._lock:
            for i in range(10):
                c._conn.execute("UPDATE tr SET ts = ? WHERE k = ?", (time.time() - 100 + i, f"k{i}"))
            c._conn.commit()
        self.assertEqual(c.prune(max_age_days=180, max_entries=4), 6)
        self.assertEqual(sorted(c.get_many([f"k{i}" for i in range(10)])), ["k6", "k7", "k8", "k9"])

    def test_prune_keeps_everything_within_limits_and_has_timestamp_index(self):
        c = self.TranslationCache(self.path)
        c.put_many({"a": "1", "b": "2"})
        self.assertEqual(c.prune(), 0)
        self.assertEqual(c.count(), 2)
        with c._lock:
            names = [r[1] for r in c._conn.execute("PRAGMA index_list(tr)").fetchall()]
        self.assertIn("idx_tr_ts", names)

    def test_prune_after_close_is_safe_and_is_noop(self):
        c = self.TranslationCache(self.path)
        c.close()
        self.assertEqual(c.prune(), 0)



# ============================================================ koordinator penutupan
class ShutdownCoordinatorTests(unittest.TestCase):
    class FakeWorker:
        def __init__(self, name: str, alive: bool = True) -> None:
            self.name, self.alive = name, alive

    def build(self, workers, answers=(), on_kill=None, timeout=10.0, extension=5.0):
        from kaptionpro.core.lifecycle import ShutdownCoordinator
        self.t = [0.0]
        self.calls: list[str] = []
        answers = list(answers)

        def ask(alive):
            self.calls.append("ask")
            return answers.pop(0) if answers else False

        def kill():
            self.calls.append("kill")
            if on_kill:
                on_kill()
            return 1

        self.coord = ShutdownCoordinator(
            alive=lambda: [w for w in workers if w.alive], kill_active=kill, ask_wait_more=ask,
            release_safe=lambda: self.calls.append("release_safe"),
            release_forced=lambda: self.calls.append("release_forced"),
            finish=lambda: self.calls.append("finish"),
            stop_timeout=timeout, extension=extension, post_kill_grace=3.0, clock=lambda: self.t[0])
        self.coord.begin()
        return self.coord

    def run_until_done(self, step: float = 0.5, limit: int = 400) -> None:
        for _ in range(limit):
            if self.coord.poll() is None:
                return
            self.t[0] += step
        self.fail("koordinator tidak selesai")

    def test_no_workers_releases_everything_immediately(self):
        self.build([])
        self.assertIsNone(self.coord.poll())
        self.assertEqual(self.calls, ["release_safe", "finish"])
        self.assertFalse(self.coord.forced)

    def test_waits_for_worker_before_releasing_resources(self):
        w = self.FakeWorker("kp-export")
        self.build([w])
        for _ in range(5):                                       # worker masih jalan: tidak boleh melepas apa pun
            self.assertIsNotNone(self.coord.poll())
            self.t[0] += 1.0
        self.assertEqual(self.calls, [])
        w.alive = False
        self.assertIsNone(self.coord.poll())
        self.assertEqual(self.calls, ["release_safe", "finish"])

    def test_user_can_wait_longer_then_worker_stops(self):
        w = self.FakeWorker("kp-ai")
        self.build([w], answers=[True])
        self.t[0] = 11.0                                         # lewat stop_timeout
        self.assertIsNotNone(self.coord.poll())
        self.assertEqual(self.calls, ["ask"])
        w.alive = False
        self.assertIsNone(self.coord.poll())
        self.assertEqual(self.calls, ["ask", "release_safe", "finish"])
        self.assertNotIn("kill", self.calls)

    def test_forced_close_kills_ffmpeg_and_then_releases_everything_if_workers_die(self):
        w = self.FakeWorker("kp-export")
        self.build([w], answers=[False], on_kill=lambda: setattr(w, "alive", False))
        self.run_until_done()
        self.assertEqual(self.calls, ["ask", "kill", "release_safe", "finish"])
        self.assertFalse(self.coord.forced)

    def test_stuck_whisper_worker_keeps_sqlite_and_project_lock_untouched(self):
        """Inferensi Whisper tak bisa dihentikan: JANGAN tutup cache SQLite / lepas lock yang mungkin dipakainya."""
        w = self.FakeWorker("kp-ai")
        self.build([w], answers=[False])
        self.run_until_done()
        self.assertEqual(self.calls, ["ask", "kill", "release_forced", "finish"])
        self.assertNotIn("release_safe", self.calls)
        self.assertTrue(self.coord.forced)

    def test_finish_runs_even_if_release_fails(self):
        from kaptionpro.core.lifecycle import ShutdownCoordinator
        calls = []

        def boom():
            raise RuntimeError("gagal")

        c = ShutdownCoordinator(alive=lambda: [], kill_active=lambda: 0, ask_wait_more=lambda a: False,
                                release_safe=boom, release_forced=boom, finish=lambda: calls.append("finish"),
                                stop_timeout=1.0, extension=1.0)
        c.begin()
        self.assertIsNone(c.poll())
        self.assertEqual(calls, ["finish"])
        self.assertTrue(c.done)

    def test_dialog_failure_means_keep_waiting_not_force_close(self):
        from kaptionpro.core.lifecycle import ShutdownCoordinator
        w = self.FakeWorker("kp-ai")
        t, calls = [0.0], []

        def ask(_):
            raise RuntimeError("dialog gagal")

        c = ShutdownCoordinator(alive=lambda: [w] if w.alive else [], kill_active=lambda: calls.append("kill") or 0,
                                ask_wait_more=ask, release_safe=lambda: calls.append("safe"),
                                release_forced=lambda: calls.append("forced"), finish=lambda: calls.append("finish"),
                                stop_timeout=1.0, extension=1.0, clock=lambda: t[0])
        c.begin()
        t[0] = 2.0
        self.assertIsNotNone(c.poll())
        self.assertEqual(calls, [])                              # tidak ada kill / force
        w.alive = False
        self.assertIsNone(c.poll())
        self.assertEqual(calls, ["safe", "finish"])


if __name__ == "__main__":
    unittest.main()
