"""
Tes v1.9.7: JobTracker (ekstraksi dari WorkspaceScreen), drain_queue berbatas waktu, privasi per penyedia,
retensi cache 'terakhir dipakai' + VACUUM, fasad storage, dan konsistensi metadata dependensi.
Tidak membutuhkan GUI.
"""

from __future__ import annotations

import queue
import re
import sys
import threading
import time
import unittest
from pathlib import Path

try:
    from tests.winsafe import SafeTemporaryDirectory
except ImportError:          # dijalankan langsung dari folder tests/
    from winsafe import SafeTemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import tomllib
except ModuleNotFoundError:          # Python 3.10
    tomllib = None


# ============================================================ drain_queue: batas waktu
class DrainQueueTimeBudgetTests(unittest.TestCase):
    def _fill(self, n: int) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        for i in range(n):
            q.put((lambda: None, ()))
        return q

    def test_time_budget_stops_before_task_cap(self):
        from kaptionpro.core.lifecycle import drain_queue
        ticks = iter(i * 0.004 for i in range(10_000))               # tiap pembacaan jam maju 4 ms
        q = self._fill(50)
        done, more = drain_queue(q, 100, lambda: None, time_budget=0.010, clock=lambda: next(ticks))
        self.assertEqual(done, 3, "callback berat harus dipotong oleh batas WAKTU, bukan hanya jumlah")
        self.assertTrue(more)
        self.assertEqual(q.qsize(), 47)

    def test_always_processes_at_least_one_even_if_budget_is_zero(self):
        from kaptionpro.core.lifecycle import drain_queue
        q = self._fill(5)
        done, more = drain_queue(q, 100, lambda: None, time_budget=0.0, clock=lambda: 100.0)
        self.assertEqual((done, more), (1, True), "antrean tidak boleh kelaparan")

    def test_task_cap_still_applies_and_budget_is_optional(self):
        from kaptionpro.core.lifecycle import drain_queue
        q = self._fill(30)
        self.assertEqual(drain_queue(q, 10, lambda: None), (10, True))
        self.assertEqual(drain_queue(q, 10, lambda: None, time_budget=60.0), (10, True))
        self.assertEqual(drain_queue(q, 10, lambda: None, time_budget=60.0), (10, False))

    def test_real_clock_slow_callbacks_yield_quickly(self):
        from kaptionpro.core.lifecycle import drain_queue
        q: queue.Queue = queue.Queue()
        for _ in range(40):
            q.put((time.sleep, (0.005,)))                            # 40 x 5 ms = 200 ms bila tanpa batas waktu
        t0 = time.monotonic()
        done, more = drain_queue(q, 100, lambda: None, time_budget=0.020)
        self.assertLess(time.monotonic() - t0, 0.15)
        self.assertTrue(more and 1 <= done < 40)


# ============================================================ JobTracker
class JobTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        from kaptionpro.core.jobs import JobTracker
        self.jt = JobTracker()

    def test_initial_state_is_idle(self):
        self.assertIsNone(self.jt.kind)
        self.assertFalse(self.jt.is_current(0))
        self.assertFalse(self.jt.cancel_event.is_set())

    def test_begin_issues_increasing_tokens_and_fresh_cancel_event(self):
        t1 = self.jt.begin("ai")
        ev1 = self.jt.cancel_event
        self.jt.request_cancel()
        self.assertTrue(ev1.is_set())
        self.jt.end()
        t2 = self.jt.begin("export")
        self.assertGreater(t2, t1)
        self.assertIsNot(self.jt.cancel_event, ev1)
        self.assertFalse(self.jt.cancel_event.is_set(), "job baru tidak boleh mewarisi pembatalan job lama")
        self.assertEqual(self.jt.kind, "export")

    def test_only_latest_active_token_is_current(self):
        t = self.jt.begin("ai")
        self.assertTrue(self.jt.is_current(t))
        self.assertFalse(self.jt.is_current(t + 1))
        self.jt.end()
        self.assertFalse(self.jt.is_current(t), "job selesai: hasil susulan harus diabaikan")

    def test_abort_makes_late_results_stale_even_after_a_new_job_starts(self):
        """Skenario ganti project: hasil terlambat job lama tidak boleh diterapkan ke job/project baru."""
        old = self.jt.begin("ai")
        old_cancel = self.jt.cancel_event
        self.assertEqual(self.jt.abort(), "ai")
        self.assertTrue(old_cancel.is_set())
        new = self.jt.begin("ai")
        self.assertFalse(self.jt.is_current(old))
        self.assertTrue(self.jt.is_current(new))

    def test_abort_when_idle_is_a_noop(self):
        before = self.jt.token
        self.assertIsNone(self.jt.abort())
        self.assertEqual(self.jt.token, before)
        self.assertFalse(self.jt.cancel_event.is_set())

    def test_request_cancel_keeps_job_active_until_worker_reports(self):
        t = self.jt.begin("export")
        self.assertEqual(self.jt.request_cancel(), "export")
        self.assertTrue(self.jt.cancel_event.is_set())
        self.assertTrue(self.jt.is_current(t))                       # baru berakhir saat worker melapor (end)
        self.jt.end()
        self.assertIsNone(self.jt.request_cancel())

    def test_workers_survive_abort_until_they_really_stop(self):
        release = threading.Event()
        self.jt.begin("ai")
        self.jt.start_worker(threading.Thread(target=release.wait, daemon=True, name="kp-ai"))
        self.jt.abort()
        self.assertEqual([t.name for t in self.jt.alive_workers()], ["kp-ai"])
        release.set()
        self.assertTrue(self.jt.workers.wait_all(5.0))

    def test_workspace_has_no_duplicate_job_state(self):
        """Penjaga regresi: status job hanya boleh hidup di JobTracker, bukan disalin lagi di WorkspaceScreen."""
        src = (ROOT / "kaptionpro" / "ui" / "workspace.py").read_text(encoding="utf-8")
        self.assertNotIn("self._job_token", src)
        self.assertNotRegex(src, r"self\._job_kind\s*=(?!=)")          # penugasan, bukan perbandingan '=='
        self.assertNotRegex(src, r"self\.cancel_event\s*=(?!=)")
        self.assertIn("JobTracker()", src)


# ============================================================ WorkspaceScreen nyata + GUI palsu
FAKE_GUI_SCRIPT = r"""
import sys, types
from unittest import mock

def mk(name):
    return type(name, (), {"__init__": lambda s, *a, **k: None, "__getattr__": lambda s, n: (lambda *a, **k: None)})

def fakemod(name, extra=()):
    m = types.ModuleType(name)
    m.__getattr__ = lambda n: mk(n)
    for n in extra:
        setattr(m, n, mk(n))
    sys.modules[name] = m
    return m

ctk = fakemod("customtkinter", ("CTkToplevel", "CTkFrame", "CTkScrollableFrame", "CTkLabel", "CTkButton", "CTkTextbox",
                                "CTkSlider", "StringVar", "CTk", "CTkOptionMenu", "CTkRadioButton", "CTkProgressBar",
                                "CTkEntry", "CTkCheckBox", "CTkSwitch", "CTkFont", "BooleanVar", "IntVar", "DoubleVar"))
ctk.set_appearance_mode = lambda *a, **k: None
ctk.set_default_color_theme = lambda *a, **k: None
tk = fakemod("tkinter", ("Tk", "Frame", "Canvas", "Toplevel", "Label", "Text", "Entry", "StringVar", "IntVar",
                         "BooleanVar", "Misc", "Widget", "Event"))
tk.TclError = type("TclError", (Exception,), {})
for sub in ("messagebox", "filedialog", "font", "ttk", "colorchooser", "simpledialog"):
    setattr(tk, sub, fakemod("tkinter." + sub))
sys.path.insert(0, sys.argv[1])

from kaptionpro.core.jobs import JobTracker
from kaptionpro.ui.workspace import WorkspaceScreen as W

ws = object.__new__(W)                       # tanpa membangun widget: hanya logika job yang diuji
ws._jobs = JobTracker()
ws.jobbar, ws.side = mock.MagicMock(), mock.MagicMock()
ws.btn_mp4 = ws.btn_srt = ws.btn_ass = mock.MagicMock()
ws.project, ws._pending_snapshot = None, False

t1 = ws._begin_job("ai", "AI")
assert ws._job_kind == "ai" and ws._is_current(t1)
ws.jobbar.begin.assert_called_once_with("AI")
ws.side.set_busy.assert_called_with(True)

ws._abort_job_silently()                     # ganti project / tutup app
assert ws._job_kind is None and not ws._is_current(t1)
ws.jobbar.end.assert_called()
ws.side.set_busy.assert_called_with(False)

t2 = ws._begin_job("export", "Export")
ws._on_job_cancelled(t1)                     # laporan basi dari job lama: harus diabaikan
assert ws._job_kind == "export" and ws._is_current(t2)
ws._on_job_cancelled(t2)                     # laporan job saat ini: job selesai
assert ws._job_kind is None

t3 = ws._begin_job("ai", "AI")
ws._cancel_job()
assert ws.cancel_event.is_set() and ws._job_kind == "ai" and ws._is_current(t3)   # batal != selesai
ws._on_job_failed.__func__  # ada
print("DELEGATION OK")
"""


class WorkspaceDelegationTests(unittest.TestCase):
    def test_real_workspace_job_logic_with_fake_gui(self):
        """Menjalankan metode job WorkspaceScreen ASLI (GUI dipalsukan) -> delegasi ke JobTracker terbukti bekerja."""
        import subprocess
        res = subprocess.run([sys.executable, "-c", FAKE_GUI_SCRIPT, str(ROOT)], capture_output=True, text=True,
                             timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr[-1500:])
        self.assertIn("DELEGATION OK", res.stdout)


# ============================================================ privasi per penyedia
class PrivacyConsentTests(unittest.TestCase):
    def test_consent_is_per_provider(self):
        from kaptionpro.core.privacy import is_acknowledged, with_acknowledged
        v = with_acknowledged({}, "google")
        self.assertTrue(is_acknowledged(v, "google"))
        self.assertFalse(is_acknowledged(v, "mymemory"), "persetujuan Google tidak berlaku untuk MyMemory")
        v = with_acknowledged(v, "mymemory")
        self.assertTrue(is_acknowledged(v, "google") and is_acknowledged(v, "mymemory"))

    def test_legacy_global_bool_is_not_treated_as_consent(self):
        from kaptionpro.core.privacy import is_acknowledged, with_acknowledged
        self.assertFalse(is_acknowledged(True, "google"))
        self.assertFalse(is_acknowledged(None, "google"))
        self.assertEqual(with_acknowledged(True, "google"), {"google": True})

    def test_with_acknowledged_does_not_mutate_input(self):
        from kaptionpro.core.privacy import with_acknowledged
        original = {"google": True}
        out = with_acknowledged(original, "mymemory")
        self.assertEqual(original, {"google": True})
        self.assertEqual(out, {"google": True, "mymemory": True})

    def test_consent_roundtrips_through_settings_store(self):
        from kaptionpro.core.privacy import ACK_KEY, is_acknowledged, with_acknowledged
        from kaptionpro.core.settings_store import SettingsStore
        with SafeTemporaryDirectory() as d:
            st = SettingsStore(Path(d))
            st.set(ACK_KEY, with_acknowledged(st.get(ACK_KEY, {}), "google"))
            again = SettingsStore(Path(d))
            self.assertTrue(is_acknowledged(again.get(ACK_KEY, {}), "google"))
            again.set(ACK_KEY, {})                                   # 'cabut persetujuan'
            self.assertFalse(is_acknowledged(SettingsStore(Path(d)).get(ACK_KEY, {}), "google"))

    def test_summary_text_lists_approved_providers_cache_and_size(self):
        from kaptionpro.core.privacy import format_size, summary_text
        labels = {"google": "Google", "mymemory": "MyMemory"}
        none = summary_text({}, labels, "/x/cache.sqlite", 0)
        self.assertIn("belum ada", none)
        some = summary_text({"google": True}, labels, "/x/cache.sqlite", 3 * 1024 * 1024)
        self.assertIn("Google", some)
        self.assertNotIn("MyMemory", some.split("\n")[0])
        self.assertIn("/x/cache.sqlite", some)
        self.assertIn("3.0 MB", some)
        self.assertEqual(format_size(512), "512 B")


# ============================================================ cache: terakhir dipakai + vacuum
class CacheLastUsedTests(unittest.TestCase):
    def setUp(self) -> None:
        from kaptionpro.core.translation_cache import TranslationCache
        self._tmp = SafeTemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "c.sqlite"
        self.cache = TranslationCache(self.path)
        self.addCleanup(self.cache.close)

    def _ts(self, key: str) -> float:
        with self.cache._lock:
            return self.cache._conn.execute("SELECT ts FROM tr WHERE k = ?", (key,)).fetchone()[0]

    def _set_ts(self, key: str, ts: float) -> None:
        with self.cache._lock:
            self.cache._conn.execute("UPDATE tr SET ts = ? WHERE k = ?", (ts, key))
            self.cache._conn.commit()

    def test_reading_an_old_entry_marks_it_as_recently_used(self):
        self.cache.put_many({"dipakai": "1", "tak-dipakai": "2"})
        old = time.time() - 100 * 86400
        self._set_ts("dipakai", old)
        self._set_ts("tak-dipakai", old)
        self.assertEqual(self.cache.get_many(["dipakai"]), {"dipakai": "1"})
        self.assertGreater(self._ts("dipakai"), time.time() - 60)
        self.assertAlmostEqual(self._ts("tak-dipakai"), old, delta=1)

    def test_used_entry_survives_prune_while_unused_one_is_removed(self):
        """Inti temuan LRU: terjemahan lama yang rutin dipakai tidak boleh kalah dari yang tak pernah dipakai."""
        self.cache.put_many({"A-lama-tapi-dipakai": "a", "B-lama-tak-dipakai": "b"})
        for k in ("A-lama-tapi-dipakai", "B-lama-tak-dipakai"):
            self._set_ts(k, time.time() - 200 * 86400)
        self.cache.get_many(["A-lama-tapi-dipakai"])
        self.assertEqual(self.cache.prune(max_age_days=180), 1)
        self.assertEqual(sorted(self.cache.get_many(["A-lama-tapi-dipakai", "B-lama-tak-dipakai"])),
                         ["A-lama-tapi-dipakai"])

    def test_recent_entries_are_not_rewritten_on_every_read(self):
        self.cache.put_many({"k": "v"})
        recent = time.time() - 120
        self._set_ts("k", recent)
        self.cache.get_many(["k"])
        self.assertAlmostEqual(self._ts("k"), recent, delta=0.5, msg="dibaca < 1 hari lalu: tanpa penulisan ulang")

    def test_cap_drops_least_recently_used(self):
        self.cache.put_many({f"k{i}": str(i) for i in range(6)})
        for i in range(6):
            self._set_ts(f"k{i}", time.time() - 10_000 + i)          # k0 paling lama tak dipakai
        self.cache.get_many(["k0"])                                  # ... kecuali k0 baru dibaca (ts jauh > 1 hari? tidak)
        self._set_ts("k0", time.time() - 3 * 86400)                  # pastikan lebih tua dari TOUCH_INTERVAL lalu pakai
        self.cache.get_many(["k0"])
        self.cache.prune(max_age_days=180, max_entries=3)
        left = sorted(self.cache.get_many([f"k{i}" for i in range(6)]))
        self.assertIn("k0", left, "k0 baru dipakai: tidak boleh ikut terpangkas")
        self.assertEqual(len(left), 3)

    def test_vacuum_actually_shrinks_the_file_after_large_prune(self):
        self.cache.put_many({f"k{i}": "x" * 2000 for i in range(6000)})
        before = self.cache.size_bytes()
        removed = self.cache.prune(max_age_days=180, max_entries=10)
        self.assertGreaterEqual(removed, self.cache.VACUUM_MIN_REMOVED)
        self.assertLess(self.cache.size_bytes(), before / 2, "VACUUM harus mengecilkan ukuran file")
        self.assertEqual(self.cache.count(), 10)

    def test_small_prune_does_not_vacuum(self):
        self.cache.put_many({f"k{i}": "x" for i in range(20)})
        calls = []
        self.cache.vacuum = lambda: calls.append(1) or True
        self.cache.prune(max_age_days=180, max_entries=5)
        self.assertEqual(calls, [])

    def test_clear_cache_via_service_empties_and_reports_size(self):
        from kaptionpro.services.ai_service import AiService
        svc = AiService(Path(self._tmp.name) / "svc.sqlite")
        self.addCleanup(svc.close)
        svc.cache.put_many({"a": "1", "b": "2"})
        svc.clear_cache()
        self.assertEqual(svc.cache.count(), 0)
        self.assertGreater(svc.cache.size_bytes(), 0)

    def test_vacuum_and_size_are_safe_after_close(self):
        self.cache.close()
        self.assertFalse(self.cache.vacuum())
        self.assertIsInstance(self.cache.size_bytes(), int)


# ============================================================ fasad storage + konsistensi metadata
class StructureAndMetadataTests(unittest.TestCase):
    def test_storage_facade_reexports_split_classes(self):
        from kaptionpro.core import settings_store, storage, translation_cache
        self.assertIs(storage.SettingsStore, settings_store.SettingsStore)
        self.assertIs(storage.TranslationCache, translation_cache.TranslationCache)

    def test_split_modules_do_not_import_the_gui(self):
        for name in ("settings_store.py", "translation_cache.py", "jobs.py", "privacy.py", "lifecycle.py"):
            src = (ROOT / "kaptionpro" / "core" / name).read_text(encoding="utf-8")
            self.assertNotRegex(src, r"(?m)^\s*(import|from)\s+(customtkinter|tkinter)", name)

    @staticmethod
    def _requirements() -> dict[str, str]:
        out = {}
        for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                m = re.match(r"([A-Za-z0-9_.\-]+)\s*(.*)$", line)
                out[m.group(1).lower()] = m.group(2).strip()
        return out

    @unittest.skipIf(tomllib is None, "tomllib butuh Python 3.11+")
    def test_requirements_and_pyproject_agree(self):
        py = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        req = self._requirements()
        core = {re.match(r"[A-Za-z0-9_.\-]+", d).group(0).lower(): d[len(re.match(r"[A-Za-z0-9_.\-]+", d).group(0)):]
                for d in py["project"]["dependencies"]}
        self.assertEqual(core, {"customtkinter": req["customtkinter"]},
                         "batas versi wajib sama di pyproject.toml dan requirements.txt")
        extras = {re.match(r"[A-Za-z0-9_.\-]+", d).group(0).lower()
                  for k, v in py["project"]["optional-dependencies"].items() if k != "dev" for d in v}
        self.assertEqual(extras, set(req) - {"customtkinter"})

    @unittest.skipIf(tomllib is None, "tomllib butuh Python 3.11+")
    def test_version_is_single_sourced_and_tests_are_not_packaged(self):
        py = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("version", py["project"]["dynamic"])
        self.assertEqual(py["tool"]["setuptools"]["dynamic"]["version"], {"attr": "kaptionpro.__version__"})
        self.assertEqual(py["tool"]["setuptools"]["packages"]["find"]["include"], ["kaptionpro*"])

    def test_guide_dialog_install_hint_matches_requirements(self):
        src = (ROOT / "kaptionpro" / "ui" / "dialogs.py").read_text(encoding="utf-8")
        self.assertIn(f'customtkinter{self._requirements()["customtkinter"]}', src)


if __name__ == "__main__":
    unittest.main()
