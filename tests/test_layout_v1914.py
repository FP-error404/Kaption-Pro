"""
Tes v1.9.14: matematika tata letak, konsistensi radius sudut, dan versi action CI.

Tidak membutuhkan CustomTkinter/GUI. (Tes tampilan nyata ada di test_gui_smoke.py::test_11.)
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kaptionpro.core import layout  # noqa: E402


class SidePanelHeightTests(unittest.TestCase):
    def test_roomy_window_gets_the_full_panel(self):
        self.assertEqual(layout.side_panel_height(1000), layout.SIDE_H_MAX)

    def test_height_shrinks_monotonically_as_the_window_gets_shorter(self):
        heights = [layout.side_panel_height(h) for h in range(1000, 500, -20)]
        self.assertEqual(heights, sorted(heights, reverse=True))
        self.assertEqual(min(heights), layout.SIDE_H_MIN)

    def test_laptop_1366x768_keeps_room_for_the_subtitle_table(self):
        # Dulu panel selalu 250 px: di 768 px tabel subtitle menyusut jadi hanya baris header.
        self.assertLess(layout.side_panel_height(768), layout.SIDE_H_MAX)
        self.assertGreaterEqual(layout.side_panel_height(768), layout.SIDE_H_MIN)

    def test_small_window_uses_the_minimum_and_never_less(self):
        self.assertEqual(layout.side_panel_height(560), layout.SIDE_H_MIN)
        self.assertEqual(layout.side_panel_height(100), layout.SIDE_H_MIN)

    def test_unmapped_window_uses_the_maximum(self):
        self.assertEqual(layout.side_panel_height(1), layout.SIDE_H_MAX)
        self.assertEqual(layout.side_panel_height(0), layout.SIDE_H_MAX)

    def test_dpi_scaling_is_taken_into_account(self):
        # 960 px fisik pada skala 125% = 768 px logis: hasilnya sama dengan jendela 768 px pada skala 100%.
        self.assertEqual(layout.side_panel_height(960, 1.25), layout.side_panel_height(768, 1.0))

    def test_invalid_scaling_falls_back_to_one(self):
        self.assertEqual(layout.side_panel_height(768, 0), layout.side_panel_height(768, 1.0))
        self.assertEqual(layout.side_panel_height(768, -2), layout.side_panel_height(768, 1.0))


class StartupGeometryTests(unittest.TestCase):
    def test_big_screen_gets_the_preferred_size(self):
        w, h, x, y = layout.startup_geometry(1920, 1080)
        self.assertEqual((w, h), layout.STARTUP_SIZE)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)

    def test_1024x768_screen_window_fits_inside_the_screen(self):
        w, h, x, y = layout.startup_geometry(1024, 768)
        self.assertLessEqual(x + w, 1024)
        self.assertLessEqual(y + h, 768 - 40, "harus menyisakan ruang untuk taskbar")
        self.assertGreaterEqual(w, 980, "tetap mode landscape (PORTRAIT_BREAKPOINT = 980)")

    def test_1366x768_screen_window_fits_inside_the_screen(self):
        w, h, x, y = layout.startup_geometry(1366, 768)
        self.assertLessEqual(y + h, 768 - 40)
        self.assertLessEqual(x + w, 1366)

    def test_normal_screens_respect_the_window_minimum(self):
        # Layar yang cukup besar: batas bawah aplikasi tetap berlaku. (Layar sangat kecil: lihat test_ui_hardening_v1915.)
        for screen in ((1024, 768), (1366, 768), (1920, 1080)):
            w, h, _x, _y = layout.startup_geometry(*screen)
            self.assertGreaterEqual(w, layout.MIN_WINDOW[0], screen)
            self.assertGreaterEqual(h, layout.MIN_WINDOW[1], screen)

    def test_position_never_negative(self):
        for screen in ((300, 300), (1024, 768), (3840, 2160)):
            _w, _h, x, y = layout.startup_geometry(*screen)
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)

    def test_dpi_scaling_reduces_logical_size_but_fits_physical_screen(self):
        w, h, x, y = layout.startup_geometry(1920, 1080, 1.5)
        self.assertLessEqual(w * 1.5 + x, 1920)
        self.assertLessEqual(h * 1.5 + y, 1080)

    def test_app_minsize_comes_from_window_minsize(self):
        src = (ROOT / "kaptionpro" / "main.py").read_text(encoding="utf-8")
        self.assertIn("self.minsize(*window_minsize(w, h))", src)


class RadiusConsistencyTests(unittest.TestCase):
    """Semua sudut memakai Theme.RADIUS (atau 0 untuk bingkai layar penuh): tidak ada angka radius yang tercecer."""

    def test_theme_radius_is_sharp(self):
        src = (ROOT / "kaptionpro" / "config.py").read_text(encoding="utf-8")
        self.assertRegex(src, r"(?m)^    RADIUS = 4\b")

    def test_no_hardcoded_corner_radius_in_ui(self):
        offenders = []
        for path in sorted((ROOT / "kaptionpro").rglob("*.py")):
            for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for value in re.findall(r"corner_radius=([^,)\s]+)", line):
                    if value not in ("Theme.RADIUS", "0"):
                        offenders.append(f"{path.relative_to(ROOT)}:{no} corner_radius={value}")
        self.assertEqual(offenders, [])

    def test_radius_defaults_helper_exists_and_is_called_at_startup(self):
        self.assertIn("def apply_radius_defaults", (ROOT / "kaptionpro" / "ui" / "components.py").read_text("utf-8"))
        self.assertIn("apply_radius_defaults()", (ROOT / "kaptionpro" / "main.py").read_text("utf-8"))


class WorkflowActionVersionTests(unittest.TestCase):
    """Action berbasis Node 20 sudah usang (peringatan di tiap run): wajib rilis Node 24."""

    MINIMUM = {"actions/checkout": 6, "actions/setup-python": 6, "actions/upload-artifact": 6}

    def setUp(self) -> None:
        self.text = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

    def test_actions_use_node24_releases(self):
        found = re.findall(r"uses:\s*(actions/[\w-]+)@v(\d+)", self.text)
        self.assertTrue(found)
        for name, major in found:
            self.assertGreaterEqual(int(major), self.MINIMUM.get(name, 1), f"{name}@v{major} berbasis Node 20 (usang)")

    def test_every_expected_action_is_used(self):
        used = {name for name, _ in re.findall(r"uses:\s*(actions/[\w-]+)@v(\d+)", self.text)}
        self.assertEqual(used, set(self.MINIMUM))

    def test_linux_runner_is_pinned_not_a_moving_label(self):
        active = "\n".join(line for line in self.text.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("ubuntu-latest", active, "ubuntu-latest berpindah ke Ubuntu 26.04: patok versinya")
        self.assertRegex(active, r"ubuntu-\d\d\.04")


if __name__ == "__main__":
    unittest.main()
