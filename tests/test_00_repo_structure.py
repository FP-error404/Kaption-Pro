"""
Pemeriksaan kelengkapan repo -- dijalankan PERTAMA (nama berawalan test_00) agar masalah upload langsung jelas.

Latar belakang: repo di-upload lewat HP/browser, dan upload manual mudah menghilangkan folder (mis. `kaptionpro/ui`) atau
folder tersembunyi (`.github`). Tanpa tes ini, kehilangan itu muncul sebagai error membingungkan di banyak tes
(`ModuleNotFoundError: No module named 'kaptionpro.ui'`) padahal penyebabnya cuma satu: berkas tidak sampai ke repo.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED = [
    "kaptionpro/__init__.py", "kaptionpro/__main__.py", "kaptionpro/config.py", "kaptionpro/main.py",
    "kaptionpro/models.py",
    "kaptionpro/core/__init__.py", "kaptionpro/core/jobs.py", "kaptionpro/core/layout.py", "kaptionpro/core/lifecycle.py",
    "kaptionpro/core/privacy.py", "kaptionpro/core/quality_fix.py", "kaptionpro/core/quality_flow.py",
    "kaptionpro/core/settings_store.py", "kaptionpro/core/storage.py",
    "kaptionpro/core/subtitle_quality.py", "kaptionpro/core/translation_cache.py", "kaptionpro/core/utils.py",
    "kaptionpro/services/__init__.py", "kaptionpro/services/ai_service.py",
    "kaptionpro/services/ffmpeg_service.py", "kaptionpro/services/vlc_player.py",
    "kaptionpro/ui/__init__.py", "kaptionpro/ui/components.py", "kaptionpro/ui/dialogs.py", "kaptionpro/ui/editor.py",
    "kaptionpro/ui/quality_dialog.py", "kaptionpro/ui/welcome.py", "kaptionpro/ui/workspace.py",
    "tests/winsafe.py", "tests/test_hardening_v1912.py",
    "tests/test_storage_recovery_v1913.py", "tests/test_layout_v1914.py", "tests/test_ui_hardening_v1915.py",
    "tests/test_side_panel_v1916.py", "tests/test_subtitle_quality_v1916.py", "tests/test_subtitle_quality_v1917.py",
    "tests/test_quality_fix_v1918.py", "tests/test_quality_dialog_v1918.py",
    "pyproject.toml", "requirements.txt", "requirements-full.txt", ".github/workflows/tests.yml",
]


class RepoStructureTests(unittest.TestCase):
    def test_all_required_files_are_present(self):
        missing = [rel for rel in REQUIRED if not (ROOT / rel).is_file()]
        if not missing:
            return
        folders: dict[str, list[str]] = {}
        for rel in missing:
            folders.setdefault(rel.rsplit("/", 1)[0] if "/" in rel else ".", []).append(rel.rsplit("/", 1)[-1])
        lines = [f"{len(missing)} berkas wajib TIDAK ADA di repo (kemungkinan besar belum ter-upload):"]
        for folder, names in sorted(folders.items()):
            whole = all(not (ROOT / folder / n).exists() for n in names) and not (ROOT / folder).is_dir()
            lines.append(f"  - {folder}/  ->  " + ("SELURUH FOLDER hilang" if whole else ", ".join(sorted(names))))
        lines.append("Perbaikan: upload ulang berkas/folder di atas ke lokasi yang sama (lihat TESTING.md).")
        self.fail("\n".join(lines))

    def test_every_package_folder_has_init(self):
        for pkg in ("kaptionpro", "kaptionpro/core", "kaptionpro/services", "kaptionpro/ui"):
            self.assertTrue((ROOT / pkg / "__init__.py").is_file(), f"{pkg}/__init__.py hilang")

    def test_no_python_module_is_empty_or_truncated(self):
        """Berkas 0 byte sering dilewati alat upload (dan folder yang hanya berisi berkas kosong tidak ada di git),
        serta upload terpotong menghasilkan berkas sangat kecil. Karena itu `__init__.py` pun diberi docstring."""
        for p in (ROOT / "kaptionpro").rglob("*.py"):
            floor = 10 if p.name == "__init__.py" else 50
            self.assertGreater(p.stat().st_size, floor, f"{p.relative_to(ROOT)} kosong/terpotong ({p.stat().st_size} byte)")


if __name__ == "__main__":
    unittest.main()
