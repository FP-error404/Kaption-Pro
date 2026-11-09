"""
Tes v1.9.13: pemulihan crash saat hapus project + konkurensi (hapus vs simpan, daftar terkunci).

Crash disimulasikan SUNGGUHAN: proses anak dimatikan mendadak (os._exit) di titik tertentu dalam delete_project(),
lalu store dibuka ulang di proses induk. Tidak membutuhkan CustomTkinter/GUI.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kaptionpro.core.utils import InterProcessLock  # noqa: E402

try:
    from tests.winsafe import SafeTemporaryDirectory
except ImportError:          # dijalankan langsung dari folder tests/
    from winsafe import SafeTemporaryDirectory

CHILD_ENV = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])}
CRASH_CODE = 77

# Proses anak: buka store, lalu panggil delete_project() dengan satu titik yang MEMATIKAN proses seketika.
CRASHING_DELETER = """
import os, sys
from pathlib import Path
from unittest import mock
from kaptionpro.core import storage as st
from kaptionpro.core.storage import ProjectStore

root, pid, where = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
store = ProjectStore(root)
project = [p for p in store.projects if p.id == pid][0]

def die(*a, **k):
    os._exit(77)

targets = {
    "after_tombstone": (ProjectStore, "_purge_project_data"),     # penanda tertulis, belum ada data yang dihapus
    "mid_purge": (st.shutil, "rmtree"),                           # subtitle/snapshot/preview hilang, backup tersisa
    "before_list_update": (ProjectStore, "_update_list"),         # semua data hilang, daftar belum dicabut
}
owner, name = targets[where]
with mock.patch.object(owner, name, die):
    store.delete_project(project)
os._exit(0)                                                       # tidak boleh tercapai
"""

HOLD_AND_SAVE = """
import sys, time
from pathlib import Path
from kaptionpro.core.storage import ProjectStore

store = ProjectStore(Path(sys.argv[1]))
project = [p for p in store.projects if p.id == sys.argv[2]][0]
assert store.lock(project)
print("LOCKED", flush=True)
segs = store.load_segments(project)
while True:
    store.save_segments(project, segs)
    time.sleep(0.005)
"""

DELETER = """
import sys
from pathlib import Path
from kaptionpro.core.storage import ProjectStore

store = ProjectStore(Path(sys.argv[1]))
project = [p for p in store.projects if p.id == sys.argv[2]][0]
print(store.delete_project(project), flush=True)
"""


def _segments(n: int, tag: str = "a"):
    from kaptionpro.models import SubtitleSegment
    return [SubtitleSegment(float(i), float(i) + 0.9, f"{tag}-baris {i}") for i in range(n)]


def _run(code: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code, *map(str, args)], capture_output=True, text=True,
                          env=CHILD_ENV, timeout=120)


def _spawn(code: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code, *map(str, args)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=CHILD_ENV)


def _kill(p: subprocess.Popen) -> None:
    try:
        p.kill()
    except OSError:
        pass
    p.communicate(timeout=10)


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        from kaptionpro.core.storage import ProjectStore
        from kaptionpro.models import Project
        self.ProjectStore, self.Project = ProjectStore, Project
        self._tmp = SafeTemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "app"
        self.addCleanup(lambda: ProjectStore._held_locks.clear())
        self.store = self._open()

    def _open(self):
        store = self.ProjectStore(self.root)
        self.addCleanup(store.close)             # lepas lock + penanda sesi sebelum folder dihapus (Windows)
        return store

    def _make_project(self, name: str = "demo"):
        project = self.Project(name=name)
        self.store.add(project)
        segs = _segments(3)
        self.store.save_segments(project, segs)
        self.store.save_snapshot(project, segs)
        ass = self.store.preview_ass_path(project)
        ass.parent.mkdir(parents=True, exist_ok=True)
        ass.write_text("x", encoding="utf-8")
        self.assertIsNotNone(self.store.create_backup(project, segs, reason="manual", force=True))
        return project

    def _raw_ids(self) -> list[str]:
        return [x["id"] for x in json.loads((self.root / "recent_projects.json").read_text(encoding="utf-8"))]

    def _assert_all_data_gone(self, project) -> None:
        self.assertFalse(self.store.subs_path(project).exists())
        self.assertFalse(self.store.snapshot_path(project).exists())
        self.assertFalse(self.store.preview_ass_path(project).exists())
        self.assertFalse(self.store.backup_dir(project).exists())

    def _crash_during_delete(self, project, where: str) -> None:
        r = _run(CRASHING_DELETER, self.root, project.id, where)
        self.assertEqual(r.returncode, CRASH_CODE, f"anak tidak mati di titik {where}: {r.stderr[-400:]}")


# ============================================================ crash di tengah penghapusan
class CrashDuringDeleteTests(_StoreCase):
    def test_crash_after_tombstone_before_any_data_deleted(self):
        project = self._make_project()
        self._crash_during_delete(project, "after_tombstone")
        self.assertTrue(self.store._tomb_path(project.id).exists())
        self.assertTrue(self.store.subs_path(project).exists())              # belum ada yang terhapus
        reopened = self._open()
        self.assertNotIn(project.id, [p.id for p in reopened.projects])
        self._assert_all_data_gone(project)                                   # dituntaskan saat startup
        self.assertFalse(reopened._tomb_path(project.id).exists())
        self.assertNotIn(project.id, self._raw_ids())

    def test_crash_mid_purge_leaves_backups_that_are_cleaned_on_startup(self):
        """Skenario ChatGPT: subtitle/snapshot/preview sudah terhapus, folder backup belum -> TIDAK boleh jadi yatim."""
        project = self._make_project()
        self._crash_during_delete(project, "mid_purge")
        self.assertFalse(self.store.subs_path(project).exists())
        self.assertTrue(self.store.backup_dir(project).exists(), "prasyarat: backup masih tersisa setelah crash")
        self.assertTrue(self.store._tomb_path(project.id).exists())
        reopened = self._open()
        self.assertNotIn(project.id, [p.id for p in reopened.projects])
        self._assert_all_data_gone(project)
        self.assertEqual(reopened._tombstoned_ids(), set())
        self.assertNotIn(project.id, self._raw_ids())

    def test_crash_after_data_deleted_before_list_updated(self):
        project = self._make_project()
        self._crash_during_delete(project, "before_list_update")
        self._assert_all_data_gone(project)
        self.assertIn(project.id, self._raw_ids())                            # daftar di disk belum dicabut
        reopened = self._open()
        self.assertNotIn(project.id, [p.id for p in reopened.projects])
        self.assertNotIn(project.id, self._raw_ids())
        self.assertEqual(reopened._tombstoned_ids(), set())

    def test_other_projects_are_untouched_by_recovery(self):
        keep = self._make_project("tetap")
        gone = self._make_project("dihapus")
        self._crash_during_delete(gone, "mid_purge")
        reopened = self._open()
        self.assertEqual([p.id for p in reopened.projects], [keep.id])
        self.assertTrue(reopened.subs_path(keep).exists())
        self.assertTrue(reopened.backup_dir(keep).exists())
        self.assertEqual(len(reopened.load_segments(keep)), 3)

    def test_recovery_is_idempotent(self):
        project = self._make_project()
        self._crash_during_delete(project, "mid_purge")
        reopened = self._open()
        self.assertEqual(reopened.recover_pending_deletes(), 0)               # tak ada lagi yang tertunda
        self._assert_all_data_gone(project)

    def test_recovery_is_skipped_while_another_instance_holds_the_project(self):
        project = self._make_project()
        self._crash_during_delete(project, "mid_purge")
        lock_path = self.store._lock_path(project)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = _spawn("import sys,time\nfrom kaptionpro.core.utils import InterProcessLock\n"
                        "lk = InterProcessLock(sys.argv[1])\nprint('GOT' if lk.acquire() else 'BUSY', flush=True)\n"
                        "time.sleep(60)\n", lock_path)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "GOT")
            reopened = self._open()
            self.assertTrue(reopened.backup_dir(project).exists(), "data instance lain tidak boleh dihapus")
            self.assertTrue(reopened._tomb_path(project.id).exists())
            self.assertNotIn(project.id, [p.id for p in reopened.projects])    # tetap tersembunyi
        finally:
            _kill(holder)
        self.assertEqual(reopened.recover_pending_deletes(), 1)                # lock bebas -> dituntaskan
        self._assert_all_data_gone(project)
        self.assertEqual(reopened._tombstoned_ids(), set())

    def test_recovery_that_cannot_finish_keeps_project_hidden_and_retries_later(self):
        project = self._make_project()
        self._crash_during_delete(project, "mid_purge")
        with mock.patch("kaptionpro.core.storage.shutil.rmtree", side_effect=OSError(errno.EACCES, "terkunci")):
            reopened = self._open()
        self.assertTrue(reopened.backup_dir(project).exists())
        self.assertTrue(reopened._tomb_path(project.id).exists(), "penanda harus bertahan sampai data benar-benar bersih")
        self.assertNotIn(project.id, [p.id for p in reopened.projects])
        self.assertEqual(reopened.recover_pending_deletes(), 1)                # percobaan berikutnya berhasil
        self._assert_all_data_gone(project)
        self.assertEqual(reopened._tombstoned_ids(), set())

    def test_recovery_with_invalid_tombstone_names_is_ignored_safely(self):
        tomb_dir = self.root / "deleting"
        tomb_dir.mkdir(parents=True, exist_ok=True)
        (tomb_dir / "..evil.tomb").write_text("{}", encoding="utf-8")
        (tomb_dir / "bukan-penanda.txt").write_text("x", encoding="utf-8")
        outside = self.root / "keep.json"
        outside.write_text("[]", encoding="utf-8")
        reopened = self._open()
        self.assertEqual(reopened.recover_pending_deletes(), 0)
        self.assertTrue(outside.exists())


# ============================================================ konkurensi
class DeleteVsSaveTests(_StoreCase):
    def test_delete_is_refused_while_this_process_keeps_saving(self):
        project = self._make_project()
        other = self._open()                                    # "jendela" kedua di proses yang sama
        self.assertTrue(self.store.lock(project))
        stop, saved, errors = threading.Event(), [], []

        def editor() -> None:
            n = 3
            while not stop.is_set():
                n += 1
                try:
                    self.store.save_segments(project, _segments(n))
                    saved.append(n)
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                time.sleep(0.002)

        t = threading.Thread(target=editor)
        t.start()
        try:
            for _ in range(25):
                self.assertFalse(other.delete_project(project))
                self.assertIn("dibuka", other.last_delete_error or "")
                time.sleep(0.003)
        finally:
            stop.set()
            t.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertTrue(saved)
        self.assertEqual(len(self.store.load_segments(project)), saved[-1], "simpanan terakhir tidak boleh hilang diam-diam")
        self.assertIn(project.id, self._raw_ids())
        self.assertEqual(self.store._tombstoned_ids(), set(), "penolakan tidak boleh meninggalkan penanda")
        self.store.unlock(project)
        self.assertTrue(other.delete_project(project))           # setelah ditutup, hapus berhasil
        self._assert_all_data_gone(project)

    def test_delete_is_refused_while_another_process_edits_and_saves(self):
        project = self._make_project()
        editor = _spawn(HOLD_AND_SAVE, self.root, project.id)
        try:
            self.assertEqual(editor.stdout.readline().strip(), "LOCKED", editor.stderr.read() if editor.poll() else "")
            other = self._open()
            for _ in range(10):
                self.assertFalse(other.delete_project(project))
                self.assertIn("instance", other.last_delete_error or "")
                time.sleep(0.01)
            self.assertTrue(other.subs_path(project).exists())
            self.assertEqual(len(other.load_segments(project)), 3)
            self.assertEqual(other._tombstoned_ids(), set())
        finally:
            _kill(editor)
        self.assertTrue(self._open().delete_project(project))    # editor mati -> lock bebas -> hapus berhasil


class ConcurrentDifferentProjectsTests(_StoreCase):
    def test_two_processes_deleting_different_projects_do_not_interfere(self):
        a, b = self._make_project("a"), self._make_project("b")
        procs = [_spawn(DELETER, self.root, a.id), _spawn(DELETER, self.root, b.id)]
        outs = [p.communicate(timeout=120) for p in procs]
        self.assertEqual([p.returncode for p in procs], [0, 0], [o[1][-300:] for o in outs])
        self.assertEqual([o[0].strip() for o in outs], ["True", "True"])
        self._assert_all_data_gone(a)
        self._assert_all_data_gone(b)
        self.assertEqual([p.id for p in self._open().projects], [])
        self.assertEqual(self.store._tombstoned_ids(), set())


# ============================================================ daftar terkunci setelah data terhapus
class ListLockFailsAfterDataDeletedTests(_StoreCase):
    def test_tombstone_stays_valid_and_entry_is_cleaned_once_list_is_writable(self):
        project = self._make_project()
        store_cls = self.ProjectStore
        with mock.patch.object(store_cls, "LIST_LOCK_TIMEOUT", 0.2):
            blocker = InterProcessLock(self.root / "recent_projects.json.lock")
            self.assertTrue(blocker.acquire(write_info=False))
            try:
                raw_before = (self.root / "recent_projects.json").read_bytes()
                self.assertFalse(self.store.delete_project(project))
                self.assertIn("disembunyikan", self.store.last_delete_error or "")
                self._assert_all_data_gone(project)
                self.assertTrue(self.store._tomb_path(project.id).exists(), "penanda wajib bertahan")
                self.assertEqual((self.root / "recent_projects.json").read_bytes(), raw_before,
                                 "daftar di disk tidak boleh ditulis tanpa lock")
                during = self._open()                              # dibuka saat daftar masih terkunci
                self.assertNotIn(project.id, [p.id for p in during.projects])
                self.assertTrue(during._tomb_path(project.id).exists())
                self.assertEqual((self.root / "recent_projects.json").read_bytes(), raw_before)
            finally:
                blocker.release()
        after = self._open()                                       # daftar sudah bisa ditulis
        self.assertNotIn(project.id, [p.id for p in after.projects])
        self.assertNotIn(project.id, self._raw_ids())
        self.assertEqual(after._tombstoned_ids(), set())

    def test_failed_partial_delete_keeps_entry_even_when_list_is_locked(self):
        project = self._make_project()
        real_unlink = Path.unlink

        def flaky(self_path, *a, **k):
            if self_path.name == project.id + ".json" and self_path.parent.name == "projects":
                raise PermissionError(errno.EACCES, "terkunci")
            return real_unlink(self_path, *a, **k)

        with mock.patch.object(self.ProjectStore, "LIST_LOCK_TIMEOUT", 0.2):
            blocker = InterProcessLock(self.root / "recent_projects.json.lock")
            self.assertTrue(blocker.acquire(write_info=False))
            try:
                with mock.patch.object(Path, "unlink", flaky):
                    self.assertFalse(self.store.delete_project(project))
            finally:
                blocker.release()
        self.assertFalse(self.store._tomb_path(project.id).exists())
        self.assertIn(project.id, self._raw_ids())
        self.assertIn(project.id, [p.id for p in self._open().projects], "penghapusan gagal: project harus tetap tampil")


if __name__ == "__main__":
    unittest.main()
