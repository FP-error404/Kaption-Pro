"""
Tes v1.9.15 (UI Hardening & Edge Cases): rincian CHROME_H, geometri awal di layar sangat kecil, dan sinkronisasi docs CI.

Tidak membutuhkan CustomTkinter/GUI. (Pengukuran di aplikasi nyata ada di test_gui_smoke.py::test_11.)
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


class ChromeBreakdownTests(unittest.TestCase):
    """CHROME_H bukan angka ajaib: terdiri dari komponen bernama, dan tiap komponen dijaga oleh tes."""

    def test_chrome_is_the_sum_of_its_named_components(self):
        self.assertEqual(layout.OUTSIDE_H, layout.TOP_BAR_H + layout.GRID_PADDING_H + layout.EXPORT_BAR_H + layout.JOB_BAR_H)
        self.assertEqual(layout.RIGHT_COLUMN_FIXED_H,
                         layout.OUTSIDE_H + layout.SCROLLABLE_EXTRA_H + layout.UNEXPLAINED_SLACK_H)
        self.assertEqual(layout.EDITOR_MIN_H, layout.EDITOR_CHROME_H + layout.TABLE_MIN_PX)
        self.assertEqual(layout.CHROME_H, layout.RIGHT_COLUMN_FIXED_H + layout.EDITOR_MIN_H)

    def test_chrome_value_is_pinned(self):
        # Mengubah komponen apa pun WAJIB disengaja: perbarui angka ini bersama komentar di core/layout.py.
        self.assertEqual(
            (layout.TOP_BAR_H, layout.GRID_PADDING_H, layout.EXPORT_BAR_H, layout.JOB_BAR_H, layout.SCROLLABLE_EXTRA_H,
             layout.UNEXPLAINED_SLACK_H, layout.EDITOR_CHROME_H, layout.ROW_H, layout.TABLE_MIN_PX, layout.SIDE_H_FLOOR),
            (48, 46, 42, 33, 8, 51, 298, 26, 100, 80))
        self.assertEqual(layout.OUTSIDE_H, 169)
        self.assertEqual(layout.RIGHT_COLUMN_FIXED_H, 228)
        self.assertEqual(layout.EDITOR_MIN_H, 398)
        self.assertEqual(layout.CHROME_H, 626)

    def test_fixed_height_covers_the_largest_ci_measurement(self):
        # CI v1.9.15: 768 - 165 - 394 = 209 (Linux), 749 - 146 - 375 = 228 (Windows). Perkiraan awal harus >= keduanya.
        self.assertGreaterEqual(layout.RIGHT_COLUMN_FIXED_H, 209)
        self.assertGreaterEqual(layout.RIGHT_COLUMN_FIXED_H, 228)

    def test_editor_chrome_matches_ci_measurements(self):
        # editor - tabel: 394 - 96 (Linux) dan 375 - 77 (Windows)
        self.assertEqual(394 - 96, layout.EDITOR_CHROME_H)
        self.assertEqual(375 - 77, layout.EDITOR_CHROME_H)

    def test_grid_padding_matches_the_pady_values_in_workspace(self):
        # 10 + 6 (panel preset) + 6 + 6 (editor) + 6 + 2 (export bar) + 2 + 8 (job bar) = 46
        src = (ROOT / "kaptionpro" / "ui" / "workspace.py").read_text(encoding="utf-8")
        self.assertIn('self.side.grid(row=1, column=1, sticky="nsew", padx=(6, 12), pady=(10, 6))', src)
        self.assertIn('self.editor.grid(row=2, column=1, rowspan=2, sticky="nsew", padx=(6, 12), pady=6)', src)
        self.assertIn('self.export_bar.grid(row=4, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 2))', src)
        self.assertIn('self.jobbar.grid(row=5, column=0, columnspan=2, sticky="ew", padx=16, pady=(2, 8))', src)
        self.assertEqual(10 + 6 + 6 + 6 + 6 + 2 + 2 + 8, layout.GRID_PADDING_H)

    def test_scrollable_frame_extra_is_twice_the_theme_radius(self):
        radius = int(re.search(r"(?m)^    RADIUS = (\d+)\b", (ROOT / "kaptionpro" / "config.py").read_text("utf-8")).group(1))
        self.assertEqual(layout.SCROLLABLE_EXTRA_H, 2 * radius)

    def test_row_height_matches_the_treeview_style(self):
        src = (ROOT / "kaptionpro" / "ui" / "editor.py").read_text(encoding="utf-8")
        self.assertIn(f"rowheight={layout.ROW_H}", src)

    def test_documented_boundaries(self):
        lo, hi = layout.CHROME_H + layout.SIDE_H_MIN, layout.CHROME_H + layout.SIDE_H_MAX
        self.assertEqual((lo, hi), (746, 876))
        self.assertEqual(layout.side_panel_height(lo - 1), layout.SIDE_H_MIN)
        self.assertEqual(layout.side_panel_height(lo), layout.SIDE_H_MIN)
        self.assertEqual(layout.side_panel_height(lo + 1), layout.SIDE_H_MIN + 1)
        self.assertEqual(layout.side_panel_height(hi), layout.SIDE_H_MAX)
        self.assertEqual(layout.side_panel_height(hi + 100), layout.SIDE_H_MAX)
        self.assertEqual(layout.side_panel_height(768), 142)

    def test_estimate_leaves_the_editor_its_minimum_when_the_window_is_tall_enough(self):
        for height in range(layout.CHROME_H + layout.SIDE_H_MIN, 1400):
            side = layout.side_panel_height(height)
            editor = height - layout.RIGHT_COLUMN_FIXED_H - side
            self.assertGreaterEqual(editor, layout.EDITOR_MIN_H, f"jendela {height}px: editor hanya {editor}px")

    def test_below_the_threshold_the_estimate_is_at_its_minimum(self):
        for height in range(300, layout.CHROME_H + layout.SIDE_H_MIN):
            self.assertEqual(layout.side_panel_height(height), layout.SIDE_H_MIN)


class RebalanceTests(unittest.TestCase):
    """Penyetelan akhir dari ukuran tabel NYATA (v1.9.15.1): perkiraan konstanta boleh meleset, tabel tetap dijaga."""

    def test_nothing_changes_when_the_table_is_big_enough(self):
        self.assertEqual(layout.rebalanced_side_height(165, 100), 165)
        self.assertEqual(layout.rebalanced_side_height(165, 240), 165)

    def test_panel_shrinks_by_the_deficit(self):
        self.assertEqual(layout.rebalanced_side_height(165, 96), 161)       # CI Linux: tabel 96
        self.assertEqual(layout.rebalanced_side_height(146, 77), 123)       # CI Windows: tabel 77

    def test_panel_never_goes_below_the_floor(self):
        self.assertEqual(layout.rebalanced_side_height(120, 10), layout.SIDE_H_FLOOR)
        self.assertEqual(layout.rebalanced_side_height(90, 30), layout.SIDE_H_FLOOR)

    def test_panel_is_never_enlarged(self):
        for table in (2, 40, 99, 100, 101, 400):
            self.assertLessEqual(layout.rebalanced_side_height(150, table), 150)

    def test_unmeasured_table_is_ignored(self):
        self.assertEqual(layout.rebalanced_side_height(150, 0), 150)
        self.assertEqual(layout.rebalanced_side_height(150, 1), 150)

    def test_dpi_scaling_converts_physical_deficit_to_logical_pixels(self):
        self.assertEqual(layout.rebalanced_side_height(165, 80, scaling=1.25), 165 - 16)   # 20 px fisik = 16 logis

    def test_guaranteed_table_px(self):
        self.assertEqual(layout.guaranteed_table_px(768), layout.TABLE_MIN_PX)
        self.assertEqual(layout.guaranteed_table_px(749), layout.TABLE_MIN_PX)
        self.assertEqual(layout.guaranteed_table_px(700), 94)
        self.assertEqual(layout.guaranteed_table_px(680), 74)
        self.assertEqual(layout.guaranteed_table_px(300), 0)
        self.assertEqual(layout.guaranteed_table_px(1200), layout.TABLE_MIN_PX)

    def test_closed_loop_meets_the_guarantee_whatever_the_real_fixed_height_is(self):
        """Simulasi UI: tabel = tinggi jendela - K - panel - 298, K (elemen lain) tidak diketahui persis (CI: 209..228)."""
        for fixed in range(190, 251, 5):                                  # K sebenarnya, termasuk di luar rentang CI
            for window in range(640, 1201, 8):
                side = layout.side_panel_height(window)
                for _ in range(4):                                        # maksimal 4 putaran seperti di aplikasi
                    table = window - fixed - side - layout.EDITOR_CHROME_H
                    side = layout.rebalanced_side_height(side, max(table, 2))
                table = window - fixed - side - layout.EDITOR_CHROME_H
                reachable = window - fixed - layout.SIDE_H_FLOOR - layout.EDITOR_CHROME_H
                want = min(layout.TABLE_MIN_PX, reachable)
                self.assertGreaterEqual(table, want, f"K={fixed}, jendela {window}: tabel {table} < {want}")

    def test_closed_loop_reproduces_the_ci_failures_and_fixes_them(self):
        # Linux: jendela 768, K=209, perkiraan lama menghasilkan tabel 96; Windows: jendela 749, K=228, tabel 77.
        for window, fixed, was in ((768, 209, 96), (749, 228, 77)):
            side = layout.side_panel_height(window)
            for _ in range(4):
                side = layout.rebalanced_side_height(side, window - fixed - side - layout.EDITOR_CHROME_H)
            self.assertGreaterEqual(window - fixed - side - layout.EDITOR_CHROME_H, layout.TABLE_MIN_PX)
        self.assertLess(96, layout.TABLE_MIN_PX)
        self.assertLess(77, layout.TABLE_MIN_PX)

    def test_workspace_wires_the_rebalance(self):
        src = (ROOT / "kaptionpro" / "ui" / "workspace.py").read_text(encoding="utf-8")
        self.assertIn("rebalanced_side_height", src)
        self.assertIn("def _rebalance_side", src)
        self.assertIn("self.editor.tree.winfo_height()", src)


class StartupGeometryEdgeCaseTests(unittest.TestCase):
    TINY_SCREENS = ((640, 480), (800, 600), (1024, 600), (1280, 600), (720, 400))

    def test_tiny_screen_window_is_not_cut_off(self):
        # Dulu 640x480 menghasilkan jendela 600x640 (lebih tinggi dari layar) sehingga bagian bawah terpotong.
        for sw, sh in self.TINY_SCREENS:
            w, h, x, y = layout.startup_geometry(sw, sh)
            self.assertLessEqual(x + w, sw, (sw, sh))
            self.assertLessEqual(y + h, sh - 40, f"{sw}x{sh}: harus menyisakan ruang taskbar")

    def test_640x480_exact_values(self):
        self.assertEqual(layout.startup_geometry(640, 480), (600, 370, 20, 36))
        self.assertEqual(layout.window_minsize(600, 370), (420, 370))

    def test_minsize_never_exceeds_the_window(self):
        for sw, sh in self.TINY_SCREENS + ((1024, 768), (1366, 768), (1920, 1080), (320, 240)):
            w, h, _x, _y = layout.startup_geometry(sw, sh)
            mw, mh = layout.window_minsize(w, h)
            self.assertLessEqual(mw, w, (sw, sh))
            self.assertLessEqual(mh, h, (sw, sh))

    def test_normal_screens_keep_the_regular_minsize(self):
        for screen in ((1024, 768), (1366, 768), (1920, 1080)):
            self.assertEqual(layout.window_minsize(*layout.startup_geometry(*screen)[:2]), layout.MIN_WINDOW, screen)

    def test_absolute_floor_is_respected(self):
        for screen in ((320, 240), (200, 150), (100, 100)):
            w, h, _x, _y = layout.startup_geometry(*screen)
            self.assertGreaterEqual(w, layout.MIN_USABLE[0], screen)
            self.assertGreaterEqual(h, layout.MIN_USABLE[1], screen)

    def test_invalid_screen_size_falls_back_to_the_default_window(self):
        for screen in ((0, 0), (-1, 768), (1366, 0)):
            self.assertEqual(layout.startup_geometry(*screen), (*layout.STARTUP_SIZE, 0, 0))

    def test_any_screen_big_enough_for_the_floor_fits(self):
        mw, mh = layout.SCREEN_MARGIN
        for sw in range(layout.MIN_USABLE[0] + mw, 2200, 37):
            for sh in range(layout.MIN_USABLE[1] + mh, 1300, 41):
                w, h, x, y = layout.startup_geometry(sw, sh)
                self.assertLessEqual(x + w, sw, (sw, sh))
                self.assertLessEqual(y + h, sh, (sw, sh))

    def test_dpi_scaling_on_a_small_screen_still_fits(self):
        w, h, x, y = layout.startup_geometry(1366, 768, 1.25)
        self.assertLessEqual(w * 1.25 + x, 1366)
        self.assertLessEqual(h * 1.25 + y, 768)


class DocsAreInSyncTests(unittest.TestCase):
    """Dokumentasi tidak boleh menyalahi CI: Ruff WAJIB sejak v1.9.12."""

    def setUp(self) -> None:
        self.testing = (ROOT / "TESTING.md").read_text(encoding="utf-8")
        self.workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

    def test_ci_enforces_ruff(self):
        active = "\n".join(line for line in self.workflow.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("continue-on-error", active)

    def test_testing_md_does_not_call_lint_informational(self):
        for no, line in enumerate(self.testing.splitlines(), 1):
            if re.search(r"(?i)\b(lint|ruff)\b", line):
                self.assertNotRegex(line, r"(?i)informasional|belum memblokir", f"TESTING.md:{no} usang: {line.strip()}")

    def test_testing_md_says_lint_is_mandatory(self):
        rows = [line for line in self.testing.splitlines() if line.startswith("| `lint`")]
        self.assertEqual(len(rows), 1)
        self.assertRegex(rows[0], r"(?i)wajib")

    def test_testing_md_has_a_single_lint_failure_row(self):
        rows = [line for line in self.testing.splitlines() if line.startswith("| job `lint` merah")]
        self.assertEqual(len(rows), 1, rows)


if __name__ == "__main__":
    unittest.main()
