"""
Tes keamanan data KaptionPro (lock, hapus project, ID project, fsync, export, backup, autosave, sesi, cache, crash).

Tes ini TIDAK membutuhkan CustomTkinter/GUI.

Jalankan dari folder yang berisi paket `kaptionpro/` dan `tests/`:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent          # folder yang berisi paket kaptionpro/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kaptionpro.core.utils import CancelledError, InterProcessLock, _atomic_write   # noqa: E402

IS_WIN = sys.platform.startswith("win")
CHILD_ENV = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])}

HOLDER = """
import os, sys, time
from kaptionpro.core.utils import InterProcessLock
go = sys.argv[2] if len(sys.argv) > 2 else ""
while go and not os.path.exists(go):
    time.sleep(0.005)
lk = InterProcessLock(sys.argv[1])
print("GOT" if lk.acquire() else "BUSY", flush=True)
time.sleep(60)
"""


def spawn_holder(path: Path, go: Path | None = None) -> subprocess.Popen:
    path.parent.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, "-c", HOLDER, str(path)] + ([str(go)] if go else [])
    return subprocess.Popen(args, stdout=subprocess.PIPE, text=True, env=CHILD_ENV)


def kill(p: subprocess.Popen) -> None:
    try:
        p.kill()
    except OSError:
        pass
    p.wait(timeout=10)
    if p.stdout:
        p.stdout.close()


# ============================================================ lock antar-proses
class InterProcessLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_second_holder_in_same_process_is_refused(self):
        a, b = InterProcessLock(self.dir / "x.lock"), InterProcessLock(self.dir / "x.lock")
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_empty_lock_file_is_not_treated_as_stale(self):
        """Inti bug lama: file lock yang masih kosong (PID belum tertulis) dianggap rusak lalu dihapus."""
        path = self.dir / "x.lock"
        a = InterProcessLock(path)
        self.assertTrue(a.acquire())
        os.ftruncate(a._fd, 0)                                   # simulasi: info pemilik belum/tidak terbaca
        self.assertEqual(path.stat().st_size, 0)
        self.assertFalse(InterProcessLock(path).acquire())       # tetap dianggap DIPEGANG
        self.assertTrue(path.exists())                           # dan file tidak dihapus siapa pun
        a.release()

    def test_many_processes_race_exactly_one_wins(self):
        path, go = self.dir / "race.lock", self.dir / "go"
        procs = [spawn_holder(path, go) for _ in range(8)]
        try:
            time.sleep(0.5)                                      # semua proses sudah menunggu aba-aba
            go.write_text("go")
            results = [p.stdout.readline().strip() for p in procs]
        finally:
            for p in procs:
                kill(p)
        self.assertEqual(results.count("GOT"), 1, results)
        self.assertEqual(results.count("BUSY"), 7, results)

    def test_lock_is_released_when_owner_crashes(self):
        path = self.dir / "crash.lock"
        p = spawn_holder(path)
        try:
            self.assertEqual(p.stdout.readline().strip(), "GOT")
            self.assertFalse(InterProcessLock(path).acquire())   # selama pemilik hidup -> ditolak
        finally:
            kill(p)                                              # crash / dimatikan paksa
        lk = InterProcessLock(path)
        self.assertTrue(lk.acquire())                            # tanpa pembersihan 'basi' apa pun
        lk.release()

    def test_threads_never_overlap_even_with_remove_on_release(self):
        """Acquire/release berulang + penghapusan file saat release tidak boleh membuat dua pemegang."""
        path = self.dir / "hammer.lock"
        inside, violations, wins = [0], [], [0]
        guard = threading.Lock()

        def worker() -> None:
            for _ in range(150):
                lk = InterProcessLock(path, remove_on_release=True)
                if not lk.acquire():
                    continue
                with guard:
                    inside[0] += 1
                    wins[0] += 1
                    if inside[0] > 1:
                        violations.append(inside[0])
                time.sleep(0.0005)
                with guard:
                    inside[0] -= 1
                lk.release()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(violations, [])
        self.assertGreater(wins[0], 0)

    @unittest.skipIf(IS_WIN, "Windows: file lock sengaja dipertahankan (lihat tes berikutnya)")
    def test_remove_on_release_deletes_file_on_posix(self):
        path = self.dir / "r.lock"
        lk = InterProcessLock(path, remove_on_release=True)
        self.assertTrue(lk.acquire())
        self.assertTrue(path.exists())
        lk.release()
        self.assertFalse(path.exists())

    def test_lock_file_is_kept_when_removal_is_not_allowed(self):
        """Kebijakan Windows: hanya kunci OS yang dilepas, file lock TIDAK dihapus (tak ada dua file lock berbeda)."""
        from kaptionpro.core import utils
        path = self.dir / "keep.lock"
        lk = InterProcessLock(path, remove_on_release=True)
        self.assertTrue(lk.acquire())
        with mock.patch.object(utils, "_CAN_REMOVE_LOCK_FILE", False):
            lk.release()
        self.assertTrue(path.exists())
        again = InterProcessLock(path, remove_on_release=True)
        self.assertTrue(again.acquire())                         # file yang sama dipakai ulang
        again.release()

    def test_set_info_overwrites_and_truncates(self):
        path = self.dir / "i.lock"
        lk = InterProcessLock(path)
        self.assertTrue(lk.acquire())
        self.assertEqual(InterProcessLock.read_owner_pid(path), os.getpid())
        self.assertTrue(lk.set_info("CLEAN\n"))
        self.assertEqual(path.read_text(encoding="utf-8"), "CLEAN\n")
        lk.release()

    def test_unusable_lock_file_raises_so_callers_fail_closed(self):
        with self.assertRaises(OSError):
            InterProcessLock(self.dir / "missing-dir" / "x.lock").acquire()


# ============================================================ ProjectStore
class ProjectStoreLockTests(unittest.TestCase):
    def setUp(self) -> None:
        from kaptionpro.core.storage import ProjectStore
        from kaptionpro.models import Project
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ProjectStore(Path(self._tmp.name))
        self.project = Project(name="demo")
        self.addCleanup(lambda: ProjectStore._held_locks.clear())

    def test_lock_is_reentrant_in_same_process_and_unlock_releases(self):
        self.assertTrue(self.store.lock(self.project))
        self.assertTrue(self.store.lock(self.project))
        self.store.unlock(self.project)
        if not IS_WIN:
            self.assertFalse(self.store._lock_path(self.project).exists())
        self.assertTrue(self.store.lock(self.project))           # bisa diambil lagi setelah unlock

    def test_lock_held_by_other_process_returns_false_without_error(self):
        p = spawn_holder(self.store._lock_path(self.project))
        try:
            self.assertEqual(p.stdout.readline().strip(), "GOT")
            self.assertFalse(self.store.lock(self.project))
            self.assertIsNone(self.store.last_lock_error)        # 'dipakai proses lain', bukan error
            self.store.unlock(self.project)                      # unlock tidak boleh menyentuh lock orang lain
            self.assertTrue(self.store._lock_path(self.project).exists())
            self.assertFalse(self.store.lock(self.project))
        finally:
            kill(p)

    def test_lock_after_owner_crash_succeeds(self):
        p = spawn_holder(self.store._lock_path(self.project))
        try:
            self.assertEqual(p.stdout.readline().strip(), "GOT")
        finally:
            kill(p)
        self.assertTrue(self.store.lock(self.project))

    def test_lock_failure_is_fail_closed_with_reason(self):
        with mock.patch.object(InterProcessLock, "acquire", side_effect=OSError(errno.EACCES, "ditolak")):
            self.assertFalse(self.store.lock(self.project))
        self.assertIn("ditolak", self.store.last_lock_error or "")

    def test_delete_is_refused_while_other_process_holds_project(self):
        self.store.add(self.project)
        self.store.save_segments(self.project, _segments(3))
        self.assertIsNotNone(self.store.create_backup(self.project, _segments(3), reason="manual", force=True))
        path = self.store._lock_path(self.project)
        p = spawn_holder(path)
        try:
            self.assertEqual(p.stdout.readline().strip(), "GOT")
            self.assertFalse(self.store.delete_project(self.project))
            self.assertIn("instance", self.store.last_delete_error or "")
            self.assertTrue(self.store.subs_path(self.project).exists())
            self.assertEqual(len(self.store.list_backups(self.project)), 1)
            self.assertIn(self.project.id, [x.id for x in self.store.projects])
            self.assertTrue(path.exists())
            self.assertFalse(InterProcessLock(path).acquire())
        finally:
            kill(p)

    def test_delete_is_refused_while_open_in_this_process(self):
        self.store.add(self.project)
        self.store.save_segments(self.project, _segments(2))
        self.assertTrue(self.store.lock(self.project))
        self.assertFalse(self.store.delete_project(self.project))
        self.assertTrue(self.store.subs_path(self.project).exists())
        self.assertTrue(self.store.lock(self.project))           # lock milik kita tidak ikut terlepas
        self.store.unlock(self.project)

    def test_delete_removes_everything_when_free(self):
        self.store.add(self.project)
        segs = _segments(3)
        self.store.save_segments(self.project, segs)
        self.store.save_snapshot(self.project, segs)
        self.store.preview_ass_path(self.project).parent.mkdir(parents=True, exist_ok=True)
        self.store.preview_ass_path(self.project).write_text("x", encoding="utf-8")
        self.assertIsNotNone(self.store.create_backup(self.project, segs, reason="manual", force=True))
        self.assertTrue(self.store.delete_project(self.project))
        self.assertFalse(self.store.subs_path(self.project).exists())
        self.assertFalse(self.store.snapshot_path(self.project).exists())
        self.assertFalse(self.store.preview_ass_path(self.project).exists())
        self.assertFalse(self.store.backup_dir(self.project).exists())
        self.assertNotIn(self.project.id, [x.id for x in self.store.projects])
        self.assertTrue(self.store.lock(self.project))           # lock dilepas setelah selesai
        self.store.unlock(self.project)

    def test_delete_keeps_project_in_list_when_some_file_cannot_be_deleted(self):
        self.store.add(self.project)
        self.store.save_segments(self.project, _segments(2))
        real_unlink = Path.unlink

        def flaky(self_path, *a, **k):
            if self_path.name == self.project.id + ".json" and self_path.parent.name == "projects":
                raise PermissionError(errno.EACCES, "terkunci")
            return real_unlink(self_path, *a, **k)

        with mock.patch.object(Path, "unlink", flaky):
            self.assertFalse(self.store.delete_project(self.project))
        self.assertIn("tetap di daftar", self.store.last_delete_error or "")
        self.assertIn(self.project.id, [x.id for x in self.store.projects])

    def test_remove_from_recent_only_touches_the_list(self):
        self.store.add(self.project)
        self.store.save_segments(self.project, _segments(2))
        self.store.remove_from_recent(self.project)
        self.assertNotIn(self.project.id, [x.id for x in self.store.projects])
        self.assertTrue(self.store.subs_path(self.project).exists())
        self.assertFalse(hasattr(self.store, "remove"), "API lama yang menghapus semuanya tidak boleh tersisa")


# ============================================================ backup / autosave / pemulihan
def _segments(n: int, tag: str = "a"):
    from kaptionpro.models import SubtitleSegment
    return [SubtitleSegment(float(i), float(i) + 0.9, f"{tag}-baris {i}") for i in range(n)]


class StorageSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        from kaptionpro.core.storage import ProjectStore
        from kaptionpro.models import Project
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = ProjectStore(self.root)
        self.project = Project(name="demo")
        self.store.add(self.project)

    def leftovers(self) -> list[Path]:
        return [p for p in self.root.rglob("*.tmp")]

    # ----- disk penuh -----
    def test_backup_when_disk_full_returns_none_and_keeps_old_backups(self):
        good = self.store.create_backup(self.project, _segments(3), reason="manual", force=True)
        self.assertIsNotNone(good)
        real_open = open

        def full_disk(file, mode="r", *a, **k):
            if "w" in mode and str(file).endswith(".tmp"):
                f = real_open(file, mode, *a, **k)
                f.close()
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_open(file, mode, *a, **k)

        with mock.patch("builtins.open", side_effect=full_disk):
            self.assertIsNone(self.store.create_backup(self.project, _segments(5), reason="manual", force=True))
        self.assertTrue(good.exists())
        self.assertEqual(self.leftovers(), [], "file .tmp tidak boleh tertinggal saat disk penuh")
        self.assertEqual(len(self.store.list_backups(self.project)), 1)

    def test_save_when_disk_full_keeps_previous_file_and_reports_error(self):
        self.store.save_segments(self.project, _segments(3, "lama"))
        before = self.store.subs_path(self.project).read_text(encoding="utf-8")
        with mock.patch("os.fsync", side_effect=OSError(errno.ENOSPC, "No space left on device")), \
                mock.patch("os.replace", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(OSError):
                self.store.save_segments(self.project, _segments(9, "baru"))
        self.assertEqual(self.store.subs_path(self.project).read_text(encoding="utf-8"), before)
        self.assertIn("space", self.store.last_save_error or "")
        self.assertEqual(self.leftovers(), [])

    # ----- autosave -----
    def test_concurrent_autosave_never_produces_corrupt_file(self):
        """Dua penyimpan (mis. dua instance/aplikasi ditutup bersamaan) ke file yang sama."""
        errors: list[Exception] = []

        def saver(tag: str) -> None:
            for i in range(60):
                try:
                    self.store.save_segments(self.project, _segments(1 + i % 5, tag))
                except OSError as e:           # Windows boleh menolak replace saat file dibuka; tak boleh merusak
                    errors.append(e)

        ts = [threading.Thread(target=saver, args=(t,)) for t in ("p", "q", "r")]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)
        res = self.store.load_segments_ex(self.project)
        self.assertEqual(res.source, "file", res.notes)
        self.assertFalse([n for n in res.notes if "Checksum" in n], res.notes)
        self.assertEqual(self.leftovers(), [])

    def test_atomic_write_uses_unique_temp_names(self):
        seen = []
        real_replace = os.replace
        with mock.patch("os.replace", side_effect=lambda a, b: (seen.append(Path(a).name), real_replace(a, b))[1]):
            _atomic_write(self.root / "f.json", "1")
            _atomic_write(self.root / "f.json", "2")
        self.assertEqual(len(set(seen)), 2)

    # ----- fsync -----
    def test_fsync_failure_is_reported_and_old_file_kept(self):
        self.store.save_segments(self.project, _segments(3, "lama"))
        before = self.store.subs_path(self.project).read_text(encoding="utf-8")
        with mock.patch("os.fsync", side_effect=OSError(errno.EIO, "I/O error")):
            with self.assertRaises(OSError):
                self.store.save_segments(self.project, _segments(8, "baru"))
        self.assertEqual(self.store.subs_path(self.project).read_text(encoding="utf-8"), before)
        self.assertIn("I/O", self.store.last_save_error or "")
        self.assertEqual(self.leftovers(), [])

    def test_backup_not_created_when_fsync_fails(self):
        with mock.patch("os.fsync", side_effect=OSError(errno.EIO, "I/O error")):
            self.assertIsNone(self.store.create_backup(self.project, _segments(2), reason="manual", force=True))
        self.assertEqual(self.store.list_backups(self.project), [])
        self.assertEqual(self.leftovers(), [])

    def test_fsync_unsupported_by_filesystem_is_tolerated(self):
        with mock.patch("os.fsync", side_effect=OSError(errno.EINVAL, "tidak didukung")):
            _atomic_write(self.root / "ok.json", "{}")
        self.assertEqual((self.root / "ok.json").read_text(encoding="utf-8"), "{}")

    # ----- validasi backup -----
    def test_corrupt_backup_is_discarded_and_does_not_rotate_good_ones(self):
        self.store.MAX_BACKUPS = 1
        good = self.store.create_backup(self.project, _segments(3), reason="manual", force=True)
        self.assertIsNotNone(good)
        import kaptionpro.core.storage as storage
        real = storage._atomic_write

        def write_garbage(path, text):
            real(path, text[: len(text) // 2])                   # tertulis terpotong, tanpa error
        with mock.patch.object(storage, "_atomic_write", write_garbage):
            self.assertIsNone(self.store.create_backup(self.project, _segments(6), reason="manual", force=True))
        self.assertTrue(good.exists(), "backup lama yang baik tidak boleh ikut terhapus oleh rotasi")
        self.assertEqual(len(self.store.list_backups(self.project)), 1)

    def test_everything_corrupt_starts_empty_with_clear_message_and_keeps_evidence(self):
        self.store.save_segments(self.project, _segments(3))
        self.assertIsNotNone(self.store.create_backup(self.project, _segments(3), reason="manual", force=True))
        self.assertIsNotNone(self.store.create_backup(self.project, _segments(5), reason="manual", force=True))
        self.assertEqual(len(self.store.list_backups(self.project)), 2)
        for info in self.store.list_backups(self.project):
            info.path.write_text("{ rusak", encoding="utf-8")
        self.store.subs_path(self.project).write_text("{ rusak juga", encoding="utf-8")
        res = self.store.load_segments_ex(self.project)
        self.assertEqual((res.source, res.segments), ("empty", []))
        self.assertTrue(any("semua 2 backup" in n for n in res.notes), res.notes)
        self.assertTrue(list(self.store.subs_path(self.project).parent.glob("*.corrupt-*")))

    # ----- sesi per instance & pemulihan crash -----
    def _crashing_instance(self) -> None:
        """Proses terpisah memulai sesi lalu dimatikan paksa (tanpa session_end)."""
        code = ("import sys, time\nfrom pathlib import Path\n"
                "from kaptionpro.core.storage import ProjectStore\n"
                "ProjectStore(Path(sys.argv[1])).session_begin()\nprint('UP', flush=True)\ntime.sleep(60)\n")
        p = subprocess.Popen([sys.executable, "-c", code, str(self.root)], stdout=subprocess.PIPE,
                             text=True, env=CHILD_ENV)
        try:
            self.assertEqual(p.stdout.readline().strip(), "UP")
        finally:
            kill(p)

    def test_clean_close_is_not_reported_as_crash(self):
        self.assertFalse(self.store.session_begin())
        self.store.session_end()
        self.assertFalse(self.store.session_begin())
        self.store.session_end()

    def test_crash_is_detected_on_next_start_and_reported_once(self):
        self._crashing_instance()
        self.assertTrue(self.store.session_begin())
        self.store.session_end()
        self.assertFalse(self.store.session_begin())             # sudah dikonsumsi
        self.store.session_end()

    def test_second_instance_does_not_overwrite_or_report_live_first_instance(self):
        from kaptionpro.core.storage import ProjectStore
        other = ProjectStore(self.root)
        self.assertFalse(self.store.session_begin())
        self.assertFalse(other.session_begin())                  # instance pertama masih hidup: bukan crash
        self.store.session_end()                                 # instance pertama tutup normal
        crashed_files = [p for p in self.store.sessions_dir.glob("*.session")
                         if InterProcessLock.read_owner_pid(p) is not None]
        self.assertEqual(len(crashed_files), 1, "penanda instance kedua harus tetap utuh")
        other.session_end()
        self.assertFalse(ProjectStore(self.root).session_begin())

    def test_crash_of_one_instance_is_detected_while_another_runs(self):
        from kaptionpro.core.storage import ProjectStore
        survivor = ProjectStore(self.root)
        self.assertFalse(survivor.session_begin())
        self._crashing_instance()
        self.assertTrue(self.store.session_begin())
        survivor.session_end()
        self.store.session_end()

    def test_legacy_shared_session_file_with_dead_owner_is_migrated(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.store.session_path.write_text(json.dumps({"pid": dead.pid, "clean": False}), encoding="utf-8")
        self.assertTrue(self.store.session_begin())
        self.assertFalse(self.store.session_path.exists())
        self.store.session_end()

    def test_corrupt_subtitle_file_is_quarantined_and_recovered_from_backup(self):
        segs = _segments(4)
        self.store.save_segments(self.project, segs)
        self.assertIsNotNone(self.store.create_backup(self.project, segs, reason="manual", force=True))
        self.store.subs_path(self.project).write_text('{"segments": [ {"start": 0', encoding="utf-8")   # terpotong
        res = self.store.load_segments_ex(self.project)
        self.assertEqual(res.source, "backup")
        self.assertEqual(len(res.segments), 4)
        self.assertTrue(list(self.store.subs_path(self.project).parent.glob("*.corrupt-*")))

    def test_stray_temp_file_from_crash_does_not_affect_load(self):
        segs = _segments(2)
        self.store.save_segments(self.project, segs)
        (self.store.subs_path(self.project).parent / (self.project.id + ".json.99999-deadbeef.tmp")).write_text("{")
        self.assertEqual(len(self.store.load_segments(self.project)), 2)


# ============================================================ FFmpeg: reservasi, commit, pembatalan
class FFmpegSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        from kaptionpro.services import ffmpeg_service as fs
        self.fs = fs
        self.svc = fs.FFmpegService()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.src = self.dir / "src.mp4"
        self.src.write_bytes(b"\0" * 2048)
        self.out = self.dir / "hasil.mp4"
        patcher = mock.patch.object(fs.FFmpegService, "LOCK_DIR", self.dir / "_locks")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patched(self, fake_cmd):
        """FFmpeg palsu: perintahnya program Python kecil yang menulis file partial."""
        stack = [
            mock.patch.object(self.fs.FFmpegService, "preflight", return_value=None),
            mock.patch.object(self.fs.FFmpegService, "ffmpeg_path", return_value="ffmpeg"),
            mock.patch.object(self.fs.FFmpegService, "validate_output", return_value=[]),
            mock.patch.object(self.fs.FFmpegService, "probe_streams", return_value=None),
            mock.patch.object(self.fs.FFmpegService, "build_export_command",
                              side_effect=lambda ff, src, partial, opts, info: fake_cmd(partial)),
        ]
        for m in stack:
            m.start()
            self.addCleanup(m.stop)

    @staticmethod
    def _quick(partial):
        return [sys.executable, "-c", "import sys; open(sys.argv[1],'wb').write(b'x'*4096)", str(partial)]

    @staticmethod
    def _slow(partial, ignore_sigterm=False):
        code = ("import sys, time, signal\n"
                + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_sigterm else "")
                + "open(sys.argv[1],'wb').write(b'x'*4096)\n"
                  "while True:\n print('out_time_us=1000000', flush=True); time.sleep(0.2)\n")
        return [sys.executable, "-c", code, str(partial)]

    def _no_leftovers(self):
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual([n for n in names if ".kp_partial" in n or n.endswith(".lock")], [], names)

    def _export(self, cancel=None):
        return self.svc.export_video(str(self.src), "ASS", str(self.out), 10.0, None,
                                     cancel or threading.Event())

    # ----- reservasi -----
    def test_second_reservation_for_same_destination_is_refused(self):
        lock = self.svc._reserve_destination(self.out)
        try:
            with self.assertRaises(self.fs.FFmpegError):
                self.svc._reserve_destination(self.out)
        finally:
            self.svc._release_destination(self.out, lock)
        if not IS_WIN:
            self.assertFalse(self.svc._reservation_path(self.out).exists())
        self.svc._release_destination(self.out, self.svc._reserve_destination(self.out))   # bisa dipesan lagi

    def test_reservation_file_is_not_placed_next_to_the_video(self):
        lock = self.svc._reserve_destination(self.out)
        try:
            self.assertNotEqual(self.svc._reservation_path(self.out).parent, self.out.parent)
            self.assertEqual([p for p in self.dir.iterdir() if p.name.endswith(".lock")], [])
        finally:
            self.svc._release_destination(self.out, lock)

    def test_reservation_survives_empty_file_and_is_never_stolen(self):
        lock = self.svc._reserve_destination(self.out)
        try:
            os.ftruncate(lock._fd, 0)                            # info pemilik belum terbaca oleh pihak lain
            with self.assertRaises(self.fs.FFmpegError):
                self.svc._reserve_destination(self.out)
            self.assertTrue(self.svc._reservation_path(self.out).exists())
        finally:
            self.svc._release_destination(self.out, lock)

    def test_reservation_recovers_after_owner_crash(self):
        path = self.svc._reservation_path(self.out)
        p = spawn_holder(path)
        try:
            self.assertEqual(p.stdout.readline().strip(), "GOT")
            with self.assertRaises(self.fs.FFmpegError):
                self.svc._reserve_destination(self.out)
        finally:
            kill(p)
        self.svc._release_destination(self.out, self.svc._reserve_destination(self.out))

    # ----- commit -----
    def test_successful_export_commits_and_cleans_everything(self):
        self._patched(self._quick)
        res = self._export()
        self.assertEqual(Path(res.path), self.out.resolve())
        self.assertEqual(self.out.stat().st_size, 4096)
        self._no_leftovers()

    def test_partial_removed_when_os_replace_fails(self):
        self._patched(self._quick)
        with mock.patch.object(self.fs.os, "replace", side_effect=PermissionError(errno.EACCES, "terkunci")):
            with self.assertRaises(self.fs.FFmpegError) as cm:
                self._export()
        self.assertIn("tidak bisa disimpan", str(cm.exception))
        self.assertFalse(self.out.exists())
        self._no_leftovers()

    def test_existing_output_untouched_when_commit_fails(self):
        self.out.write_bytes(b"VIDEO LAMA")
        self._patched(self._quick)
        with mock.patch.object(self.fs.os, "replace", side_effect=OSError(errno.EIO, "io")):
            with self.assertRaises(self.fs.FFmpegError):
                self._export()
        self.assertEqual(self.out.read_bytes(), b"VIDEO LAMA")
        self._no_leftovers()

    def test_validation_failure_removes_partial(self):
        self._patched(self._quick)
        with mock.patch.object(self.fs.FFmpegService, "validate_output", return_value=["rusak"]):
            with self.assertRaises(self.fs.FFmpegError):
                self._export()
        self._no_leftovers()

    # ----- pembatalan -----
    def _cancel_after(self, seconds: float) -> threading.Event:
        ev = threading.Event()
        threading.Timer(seconds, ev.set).start()
        return ev

    def _run_cancel(self, **slow_kw):
        procs = []
        real_popen = subprocess.Popen

        def tracking(*a, **k):
            p = real_popen(*a, **k)
            procs.append(p)
            return p

        self._patched(lambda partial: self._slow(partial, **slow_kw))
        with mock.patch.object(self.fs.subprocess, "Popen", side_effect=tracking):
            with self.assertRaises(CancelledError):
                self._export(self._cancel_after(1.0))
        self.assertEqual(len(procs), 1)
        self.assertIsNotNone(procs[0].poll(), "proses FFmpeg HARUS sudah berhenti setelah Cancel")
        self.assertFalse(self.out.exists())
        self._no_leftovers()

    def test_cancel_stops_ffmpeg_and_cleans_up(self):
        self._run_cancel()

    @unittest.skipIf(IS_WIN, "SIGTERM diabaikan hanya relevan di POSIX (Windows memakai TerminateProcess)")
    def test_cancel_kills_process_that_ignores_sigterm(self):
        self._run_cancel(ignore_sigterm=True)

    def test_unstoppable_process_is_reported_not_silently_cancelled(self):
        fake = mock.Mock()
        fake.poll.return_value = None
        fake.pid = 4242
        fake.wait.side_effect = subprocess.TimeoutExpired("x", 1)
        with self.assertRaises(self.fs.FFmpegError):
            self.fs.FFmpegService._stop_or_raise(fake)

    def test_cancel_during_exit_wait_is_honoured(self):
        proc = mock.Mock()
        proc.wait.side_effect = subprocess.TimeoutExpired("x", 0.5)
        proc.poll.return_value = 0
        ev = threading.Event()
        ev.set()
        with self.assertRaises(CancelledError):
            self.svc._wait_exit(proc, ev)

    # ----- ffprobe / disk penuh / penutupan aplikasi -----
    @staticmethod
    def mp4_bytes(total: int = 4096, moov: bool = True, mdat: bool = True) -> bytes:
        """MP4 minimal yang struktur kotaknya utuh (ftyp + moov + mdat) dengan ukuran total tertentu."""
        data = b"\x00\x00\x00\x14ftypisom\x00\x00\x02\x00isom"
        if moov:
            data += b"\x00\x00\x00\x10moov" + b"\x00" * 8
        if mdat:
            data += (total - len(data)).to_bytes(4, "big") + b"mdat" + b"\x00" * (total - len(data) - 8)
        return data

    def _valid_mp4_cmd(self, partial):
        return [sys.executable, "-c", "import sys; open(sys.argv[1],'wb').write(bytes.fromhex(sys.argv[2]))",
                str(partial), self.mp4_bytes().hex()]

    def _with_real_validation(self, ffprobe: str | None, cmd=None):
        real_validate = self.fs.FFmpegService.validate_output
        self._patched(cmd or self._quick)
        for m in (mock.patch.object(self.fs.FFmpegService, "validate_output", real_validate),
                  mock.patch.object(self.fs.FFmpegService, "ffprobe_path", return_value=ffprobe)):
            m.start()
            self.addCleanup(m.stop)

    def test_unreadable_output_by_ffprobe_is_rejected_and_cleaned(self):
        self._with_real_validation("ffprobe")                    # ffprobe ada, tapi probe_streams() -> None
        with self.assertRaises(self.fs.FFmpegError) as cm:
            self._export()
        self.assertIn("ffprobe", str(cm.exception))
        self.assertFalse(self.out.exists())
        self._no_leftovers()

    def test_export_still_works_when_ffprobe_is_missing(self):
        self._with_real_validation(None, self._valid_mp4_cmd)    # tanpa ffprobe: ukuran file + struktur MP4
        self._export()
        self.assertEqual(self.out.stat().st_size, 4096)
        self._no_leftovers()

    def test_garbage_output_is_rejected_even_when_ffprobe_is_missing(self):
        """v1.9.6: tanpa ffprobe, berkas >1 KB yang bukan MP4 tidak lagi dianggap valid."""
        self._with_real_validation(None)                         # _quick menulis 4096 byte 'x' (bukan MP4)
        with self.assertRaises(self.fs.FFmpegError) as cm:
            self._export()
        self.assertIn("struktur MP4", str(cm.exception))
        self.assertFalse(self.out.exists())
        self._no_leftovers()

    def test_ffmpeg_reporting_disk_full_fails_cleanly(self):
        def enospc(partial):
            code = ("import sys\nopen(sys.argv[1],'wb').write(b'x'*10)\n"
                    "print('Error writing trailer: No space left on device', flush=True)\nsys.exit(1)\n")
            return [sys.executable, "-c", code, str(partial)]
        self._patched(enospc)
        with self.assertRaises(self.fs.FFmpegError) as cm:
            self._export()
        self.assertIn("No space left", str(cm.exception))
        self.assertFalse(self.out.exists())
        self._no_leftovers()

    def test_preflight_rejects_when_disk_is_too_small(self):
        usage = mock.Mock(free=1)
        with mock.patch.object(self.fs.FFmpegService, "ffmpeg_path", return_value="ffmpeg"), \
                mock.patch.object(self.fs.FFmpegService, "probe_streams", return_value=None), \
                mock.patch.object(self.fs.FFmpegService, "estimate_output_bytes", return_value=10 ** 12), \
                mock.patch.object(self.fs.shutil, "disk_usage", return_value=usage):
            with self.assertRaises(self.fs.FFmpegError) as cm:
                self.svc.preflight(str(self.src), str(self.out), self.fs.ExportOptions())
        self.assertIn("Ruang disk", str(cm.exception))

    def test_closing_app_during_export_stops_worker_inside_join_window(self):
        """Meniru _on_close: set cancel_event lalu join(2 dtk). Worker + proses FFmpeg harus sudah berhenti."""
        procs, result = [], {}
        real_popen = subprocess.Popen

        def tracking(*a, **k):
            pr = real_popen(*a, **k)
            procs.append(pr)
            return pr

        self._patched(self._slow)
        ev = threading.Event()

        def worker():
            try:
                self._export(ev)
            except BaseException as e:                           # noqa: BLE001
                result["exc"] = e

        with mock.patch.object(self.fs.subprocess, "Popen", side_effect=tracking):
            t = threading.Thread(target=worker, daemon=True)
            t.start()
            time.sleep(1.0)
            ev.set()
            t.join(timeout=2.0)
        self.assertFalse(t.is_alive(), "worker export harus selesai dalam jendela join 2 detik saat aplikasi ditutup")
        self.assertIsInstance(result.get("exc"), CancelledError)
        self.assertIsNotNone(procs[0].poll())
        self._no_leftovers()

    # ----- proses mati saat commit -----
    def test_partial_left_by_dead_process_is_cleaned_by_next_export(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        stale = self.dir / f"hasil.kp_partial-{dead.pid}-deadbeef01.mp4"     # sisa export yang prosesnya mati
        stale.write_bytes(b"x" * 100)
        self._patched(self._quick)
        self._export()
        self.assertFalse(stale.exists())
        self.assertEqual(self.out.stat().st_size, 4096)
        self._no_leftovers()

    def test_live_foreign_partial_blocks_export_and_is_not_deleted(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            live = self.dir / f"hasil.kp_partial-{other.pid}-cafebabe01.mp4"
            live.write_bytes(b"x")
            self._patched(self._quick)
            with self.assertRaises(self.fs.FFmpegError):
                self._export()
            self.assertTrue(live.exists())
        finally:
            kill(other)


# ============================================================ cache terjemahan rusak
class TranslationCacheTests(unittest.TestCase):
    def test_corrupt_database_disables_cache_without_crashing(self):
        from kaptionpro.core.storage import TranslationCache
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cache.db"
            path.write_bytes(b"ini bukan database sqlite " * 50)
            cache = TranslationCache(path)
            self.assertEqual(cache.count(), 0)
            self.assertEqual(cache.get_many(["a"]), {})
            cache.put_many({"a": "b"})                           # tidak boleh melempar exception
            cache.clear()


# ============================================================ core tanpa GUI
class NoGuiTests(unittest.TestCase):
    def _run(self, code: str) -> subprocess.CompletedProcess:
        boot = "import sys; sys.modules['customtkinter'] = None\n"       # simulasi: CustomTkinter tidak terpasang
        return subprocess.run([sys.executable, "-c", boot + code], capture_output=True, text=True, env=CHILD_ENV)

    def test_core_modules_import_without_customtkinter(self):
        res = self._run("import kaptionpro.core.storage, kaptionpro.services.ffmpeg_service, kaptionpro.models\n"
                        "print('OK')")
        self.assertEqual(res.stdout.strip().splitlines()[-1:], ["OK"], res.stderr)

    def test_gui_entry_point_exits_with_clear_message(self):
        res = self._run("from kaptionpro.config import require_gui\nrequire_gui()")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("CustomTkinter", res.stderr)


# ============================================================ sesi: marker & banyak instance
class SessionMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        from kaptionpro.core.storage import ProjectStore
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ProjectStore = ProjectStore
        self.store = ProjectStore(self.root)

    def test_markers_are_never_unlinked_manually_when_removal_is_not_allowed(self):
        """Simulasi kebijakan Windows: tak ada unlink pada penanda sesi setelah lock dilepas."""
        from kaptionpro.core import utils
        removed: list[str] = []
        real_os_unlink, real_path_unlink = os.unlink, Path.unlink
        with mock.patch.object(utils, "_CAN_REMOVE_LOCK_FILE", False), \
                mock.patch("os.unlink", lambda p, *a, **k: (removed.append(str(p)), real_os_unlink(p, *a, **k))[1]), \
                mock.patch.object(Path, "unlink", lambda self_, *a, **k: (removed.append(str(self_)),
                                                                          real_path_unlink(self_, *a, **k))[1]):
            other = self.ProjectStore(self.root)
            self.assertFalse(self.store.session_begin())
            self.assertFalse(other.session_begin())
            self.store.session_end()
            other.session_end()
            third = self.ProjectStore(self.root)
            self.assertFalse(third.session_begin())
            third.session_end()
        self.assertEqual([r for r in removed if r.endswith(".session")], [])
        markers = list(self.store.sessions_dir.glob("*.session"))
        self.assertTrue(markers)
        for m in markers:
            self.assertTrue(m.read_text(encoding="utf-8").startswith("CLEAN"), m)

    def test_leftover_clean_marker_is_not_reported_as_crash_but_real_crash_is(self):
        """Regresi: probe dulu menimpa isi file dengan PID-nya sendiri sehingga CLEAN terbaca sebagai crash."""
        d = self.store.sessions_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / "1-clean.session").write_text("CLEAN\n", encoding="utf-8")
        self.assertFalse(self.store.session_begin())
        self.store.session_end()
        (d / "2-crash.session").write_text("424242\n2026-01-01T00:00:00\n", encoding="utf-8")   # pemilik tak menutup
        self.assertTrue(self.store.session_begin())
        self.store.session_end()
        self.assertFalse(self.ProjectStore(self.root).session_begin())                            # dikonsumsi sekali

    def test_cleanup_removes_only_old_clean_markers(self):
        d = self.store.sessions_dir
        d.mkdir(parents=True, exist_ok=True)
        old = time.time() - 3 * 24 * 3600
        files = {"old_clean": ("CLEAN\n", old), "fresh_clean": ("CLEAN\n", time.time()),
                 "old_active": (f"{os.getpid()}\n2026-01-01T00:00:00\n", old), "old_empty": ("", old)}
        for name, (text, mtime) in files.items():
            f = d / f"{name}.session"
            f.write_text(text, encoding="utf-8")
            os.utime(f, (mtime, mtime))
        self.store._cleanup_session_markers()
        left = sorted(p.stem for p in d.glob("*.session"))
        self.assertEqual(left, ["fresh_clean", "old_active", "old_empty"])

    def test_marker_is_obtained_even_if_preferred_name_is_locked_by_a_scanner(self):
        from kaptionpro.config import SESSION_ID
        d = self.store.sessions_dir
        d.mkdir(parents=True, exist_ok=True)
        blocker = InterProcessLock(d / f"{os.getpid()}-{SESSION_ID}.session")
        self.assertTrue(blocker.acquire())
        try:
            self.assertFalse(self.store.session_begin())
            self.assertIsNotNone(self.store._session_lock, "instance tidak boleh berjalan tanpa penanda sesi")
            self.assertNotEqual(self.store._session_lock.path, blocker.path)
        finally:
            blocker.release()
            self.store.session_end()

    def test_many_concurrent_instances_never_report_false_crash(self):
        """Banyak proses memulai/menutup sesi bersamaan (jalur yang sama dipakai CI Windows)."""
        code = ("import sys, time\nfrom pathlib import Path\nfrom kaptionpro.core.storage import ProjectStore\n"
                "st = ProjectStore(Path(sys.argv[1]))\ncrashed = 0\n"
                "for _ in range(15):\n    crashed += bool(st.session_begin())\n    time.sleep(0.01)\n    st.session_end()\n"
                "print(crashed, flush=True)\n")
        procs = [subprocess.Popen([sys.executable, "-c", code, str(self.root)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=CHILD_ENV) for _ in range(6)]
        outs = [p.communicate(timeout=120) for p in procs]
        self.assertEqual([p.returncode for p in procs], [0] * 6, [o[1][-300:] for o in outs])
        self.assertEqual([o[0].strip() for o in outs], ["0"] * 6, "tidak boleh ada crash palsu")
        checker = self.ProjectStore(self.root)
        self.assertFalse(checker.session_begin())
        checker.session_end()
        for m in self.store.sessions_dir.glob("*.session"):
            self.assertFalse(InterProcessLock.read_owner_pid(m), f"penanda aktif tertinggal: {m}")


# ============================================================ ID project
class ProjectIdTests(unittest.TestCase):
    BAD = ["", "../x", "..", "a/b", "a\\b", "../../victim", "G" * 32, "A" * 32, "a" * 31, "a" * 33, " " + "a" * 31,
           "a" * 31 + "\n", "a" * 16 + "/" + "a" * 15, None, 12345, ["a" * 32]]

    def test_project_rejects_invalid_ids(self):
        from kaptionpro.models import Project
        for bad in self.BAD:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Project(name="x", id=bad)

    def test_project_accepts_generated_and_hex_ids(self):
        import uuid
        from kaptionpro.models import Project
        self.assertEqual(len(Project(name="x").id), 32)
        self.assertEqual(Project(name="x", id=uuid.uuid4().hex).name, "x")

    def test_store_quarantines_invalid_entries_and_keeps_valid_ones(self):
        import dataclasses
        from kaptionpro.core.storage import ProjectStore
        from kaptionpro.models import Project
        good = dataclasses.asdict(Project(name="bagus"))
        bad_ids = [b for b in self.BAD if b is not None]          # id null = format lama -> dimigrasikan, bukan ditolak
        entries = [good] + [{**good, "id": bad, "name": f"jahat-{i}"} for i, bad in enumerate(bad_ids)]
        no_id = {k: v for k, v in good.items() if k != "id"}
        no_id["name"] = "tanpa-id"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "recent_projects.json").write_text(json.dumps(entries + [no_id]), encoding="utf-8")
            store = ProjectStore(root)
            self.assertEqual(sorted(p.name for p in store.projects), ["bagus", "tanpa-id"])
            quarantined = list(root.glob("recent_projects.rejected-*.json"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(len(json.loads(quarantined[0].read_text(encoding="utf-8"))), len(bad_ids))
            self.assertTrue(any("TIDAK dipakai" in n for n in store.notes), store.notes)
            saved = json.loads((root / "recent_projects.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(x["name"] for x in saved), ["bagus", "tanpa-id"])
            self.assertEqual(sorted(x.name for x in ProjectStore(root).projects), ["bagus", "tanpa-id"])

    def test_mutated_invalid_id_never_reaches_the_filesystem(self):
        from kaptionpro.core.storage import ProjectStore
        from kaptionpro.models import Project
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            victim = base / "victim.json"
            victim.write_text("JANGAN DIHAPUS", encoding="utf-8")
            store = ProjectStore(base / "app")
            pr = Project(name="x")
            store.add(pr)
            pr.id = "../../victim"                               # dimutasi setelah dibuat (id yang jahat)
            for fn in (store.subs_path, store.snapshot_path, store._lock_path, store.preview_ass_path, store.backup_dir):
                with self.subTest(fn=fn.__name__), self.assertRaises(ValueError):
                    fn(pr)
            self.assertFalse(store.lock(pr))
            self.assertIn("tidak valid", store.last_lock_error or "")
            self.assertFalse(store.delete_project(pr))
            self.assertIn("tidak valid", store.last_delete_error or "")
            with self.assertRaises(ValueError):
                store.save_segments(pr, _segments(1))
            with self.assertRaises(ValueError):
                store.create_backup(pr, _segments(1), reason="manual", force=True)
            self.assertEqual(victim.read_text(encoding="utf-8"), "JANGAN DIHAPUS")
            self.assertEqual(sorted(p.name for p in base.iterdir()), ["app", "victim.json"])


# ============================================================ dua proses menghapus bersamaan
class ConcurrentDeleteTests(unittest.TestCase):
    def test_two_or_more_processes_deleting_same_project(self):
        from kaptionpro.core.storage import ProjectStore
        from kaptionpro.models import Project
        with tempfile.TemporaryDirectory() as d:
            root, go = Path(d) / "app", Path(d) / "go"
            store = ProjectStore(root)
            pr = Project(name="rebutan")
            store.add(pr)
            store.save_segments(pr, _segments(3))
            self.assertIsNotNone(store.create_backup(pr, _segments(3), reason="manual", force=True))
            # BARRIER: semua proses memuat daftar & menemukan project LEBIH DULU, lalu memberi sinyal READY dan menunggu
            # GO. Tanpa ini proses yang lambat memuat daftar setelah proses lain selesai menghapus -> IndexError
            # (race pada tes, bukan pada aplikasi). Yang diuji di sini murni perebutan lock project.
            code = ("import sys, os, time\nfrom pathlib import Path\nfrom kaptionpro.core.storage import ProjectStore\n"
                    "st = ProjectStore(Path(sys.argv[1]))\n"
                    "pr = [p for p in st.projects if p.id == sys.argv[3]][0]\n"
                    "Path(sys.argv[4]).write_text('ready')\n"
                    "while not os.path.exists(sys.argv[2]):\n    time.sleep(0.003)\n"
                    "print(st.delete_project(pr), flush=True)\n")
            ready = [Path(d) / f"ready-{n}" for n in range(5)]
            procs = [subprocess.Popen([sys.executable, "-c", code, str(root), str(go), pr.id, str(r)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=CHILD_ENV)
                     for r in ready]
            deadline = time.monotonic() + 60
            while not all(r.exists() for r in ready):
                self.assertLess(time.monotonic(), deadline, "proses tidak siap tepat waktu")
                time.sleep(0.01)
            go.write_text("go")
            outs = [p.communicate(timeout=120) for p in procs]
            self.assertEqual([p.returncode for p in procs], [0] * 5, [o[1][-300:] for o in outs])
            results = [o[0].strip() for o in outs]
            self.assertTrue(set(results) <= {"True", "False"}, results)
            self.assertIn("True", results)
            self.assertFalse(store.subs_path(pr).exists())
            self.assertFalse(store.backup_dir(pr).exists())
            self.assertEqual([p.id for p in ProjectStore(root).projects], [])


# ============================================================ cache terjemahan terkunci
class TranslationCacheLockedTests(unittest.TestCase):
    def test_database_locked_by_another_connection_degrades_gracefully(self):
        import sqlite3
        from kaptionpro.core.storage import TranslationCache
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cache.db"
            with mock.patch.object(TranslationCache, "BUSY_TIMEOUT", 0.2):
                cache = TranslationCache(path)
                cache.put_many({"a": "b"})
                other = sqlite3.connect(str(path), isolation_level=None)
                try:
                    other.execute("BEGIN EXCLUSIVE")                    # proses lain memegang database
                    started = time.monotonic()
                    cache.put_many({"c": "d"})                          # tidak boleh melempar / menggantung
                    self.assertEqual(cache.get_many(["a"]), {})
                    self.assertLess(time.monotonic() - started, 5.0)
                finally:
                    other.execute("ROLLBACK")
                    other.close()
                cache.put_many({"c": "d"})
                self.assertEqual(cache.get_many(["a", "c"]), {"a": "b", "c": "d"})


if __name__ == "__main__":
    unittest.main()
