"""
Tes v1.9.12: jaring pengaman CI (timeout, lint wajib) dan diagnostik dependensi opsional.

Tidak membutuhkan CustomTkinter/GUI. (Tes entri project yatim ada di test_data_safety.py, requirements di
test_refactor_v197.py.)
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kaptionpro import config  # noqa: E402


def _job_block(name: str) -> str:
    """Teks satu job di tests.yml (tanpa baris komentar), dari '  <name>:' sampai job berikutnya."""
    text = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    m = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", text)
    assert m, f"job {name} tidak ditemukan di tests.yml"
    return "\n".join(line for line in m.group(1).splitlines() if not line.lstrip().startswith("#"))


class WorkflowGuardTests(unittest.TestCase):
    def test_every_test_job_has_a_timeout(self):
        for job in ("core-tests", "lint", "gui-smoke"):
            self.assertRegex(_job_block(job), r"(?m)^\s+timeout-minutes:\s*\d+", f"job {job} tanpa timeout-minutes")

    def test_core_tests_timeout_is_sane(self):
        minutes = int(re.search(r"timeout-minutes:\s*(\d+)", _job_block("core-tests")).group(1))
        self.assertTrue(5 <= minutes <= 30, minutes)

    def test_lint_is_enforced(self):
        self.assertNotIn("continue-on-error", _job_block("lint"), "lint wajib: tidak boleh continue-on-error")
        self.assertIn("ruff check", _job_block("lint"))


class OptionalDependencyDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        saved = dict(config.OPTIONAL_STATUS)
        self.addCleanup(lambda: (config.OPTIONAL_STATUS.clear(), config.OPTIONAL_STATUS.update(saved)))

    def test_package_not_installed_is_informational(self):
        with self.assertLogs("KaptionPro", level="INFO") as cap:
            config._optional_unavailable("vlc", "Preview video (VLC)", ModuleNotFoundError("No module", name="vlc"))
        self.assertEqual(config.OPTIONAL_STATUS["vlc"], "belum terpasang")
        self.assertEqual([r.levelname for r in cap.records], ["INFO"])

    def test_submodule_name_counts_as_not_installed(self):
        config._optional_unavailable("argostranslate", "Penerjemah offline",
                                     ModuleNotFoundError("x", name="argostranslate.translate"))
        self.assertEqual(config.OPTIONAL_STATUS["argostranslate"], "belum terpasang")

    def test_installed_but_failing_to_load_is_a_warning_with_the_reason(self):
        with self.assertLogs("KaptionPro", level="INFO") as cap:
            config._optional_unavailable("vlc", "Preview video (VLC)", OSError("libvlc.dll tidak ditemukan"))
        self.assertEqual(config.OPTIONAL_STATUS["vlc"], "gagal dimuat: OSError")
        self.assertEqual([r.levelname for r in cap.records], ["WARNING"])
        self.assertIn("libvlc.dll", cap.output[0])

    def test_missing_dependency_of_the_package_is_not_reported_as_not_installed(self):
        # faster_whisper ada, tetapi ctranslate2 (dependensinya) hilang: ini masalah instalasi, bukan 'belum terpasang'.
        config._optional_unavailable("faster_whisper", "Transkripsi AI",
                                     ModuleNotFoundError("No module", name="ctranslate2"))
        self.assertEqual(config.OPTIONAL_STATUS["faster_whisper"], "gagal dimuat: ModuleNotFoundError")


if __name__ == "__main__":
    unittest.main()
