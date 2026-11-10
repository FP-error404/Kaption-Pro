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
        self.assertEqual(layout.RIGHT_COLUMN_FIXED_H, layout.OUTSIDE_H)
        self.assertEqual(layout.EDITOR_MIN_H, layout.EDITOR_CHROME_H + layout.TABLE_MIN_PX)
        self.assertEqual(layout.CHROME_H, layout.RIGHT_COLUMN_FIXED_H + layout.EDITOR_MIN_H)

    def test_chrome_value_is_pinned(self):
        # Mengubah komponen apa pun WAJIB disengaja: perbarui angka ini bersama komentar di core/layout.py.
        self.assertEqual(
            (layout.TOP_BAR_H, layout.GRID_PADDING_H, layout.EXPORT_BAR_H, layout.JOB_BAR_H, layout.CI_OUTSIDE_H,
             layout.EDITOR_CHROME_H, layout.EDITOR_TOOLBAR_SAVED_H, layout.ROW_H, layout.TABLE_MIN_PX, layout.SIDE_H_FLOOR),
            (48, 46, 42, 33, 164, 298, 30, 26, 90, 120))
        self.assertEqual(layout.OUTSIDE_H, 169)
        self.assertEqual(layout.RIGHT_COLUMN_FIXED_H, 169)
        self.assertEqual(layout.EDITOR_MIN_H, 388)
        self.assertEqual(layout.CHROME_H, 557)

    def test_other_elements_height_matches_ci_in_both_oses(self):
        # Log CI v1.9.15 dengan panel yang NYATA 210 px (bukan 165/146 yang diminta): hasilnya sama di Linux dan Windows.
        self.assertEqual(768 - 210 - 394, layout.CI_OUTSIDE_H)        # Linux
        self.assertEqual(749 - 210 - 375, layout.CI_OUTSIDE_H)        # Windows
        self.assertGreaterEqual(layout.RIGHT_COLUMN_FIXED_H, layout.CI_OUTSIDE_H)
        self.assertLessEqual(layout.RIGHT_COLUMN_FIXED_H - layout.CI_OUTSIDE_H, 10)

    def test_editor_chrome_matches_ci_measurements(self):
        # editor - tabel: 394 - 96 (Linux) dan 375 - 77 (Windows)
        self.assertEqual(394 - 96, layout.EDITOR_CHROME_H)
        self.assertEqual(375 - 77, layout.EDITOR_CHROME_H)

    def test_grid_padding_matches_the_pady_values_in_workspace(self):
        # 10 + 6 (panel preset) + 6 + 6 (editor) + 6 + 2 (export bar) + 2 + 8 (job bar) = 46
        src = (ROOT / "kaptionpro" / "ui" / "workspace.py").read_text(encoding="utf-8")
        self.assertIn('self.side_box.grid(row=1, column=1, sticky="nsew", padx=(6, 12), pady=(10, 6))', src)
        self.assertIn('self.editor.grid(row=2, column=1, rowspan=2, sticky="nsew", padx=(6, 12), pady=6)', src)
        self.assertIn('self.export_bar.grid(row=4, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 2))', src)
        self.assertIn('self.jobbar.grid(row=5, column=0, columnspan=2, sticky="ew", padx=16, pady=(2, 8))', src)
        self.assertEqual(10 + 6 + 6 + 6 + 6 + 2 + 2 + 8, layout.GRID_PADDING_H)

    def test_side_panel_lives_in_a_fixed_size_container(self):
        # CTkScrollableFrame tidak mau di bawah ~210 px (scrollbar meminta 200 px): tinggi dipaksa lewat wadah tetap.
        src = (ROOT / "kaptionpro" / "ui" / "workspace.py").read_text(encoding="utf-8")
        self.assertIn("self.side_box.grid_propagate(False)", src)
        self.assertIn("self.side = SidePanel(self.side_box,", src)
        self.assertNotIn("self.side.configure(height", src, "configure(height=) pada CTkScrollableFrame tidak berpengaruh")
        self.assertIn("self.side_box.configure(height=", src)

    def test_row_height_matches_the_treeview_style(self):
        src = (ROOT / "kaptionpro" / "ui" / "editor.py").read_text(encoding="utf-8")
        self.assertIn(f"rowheight={layout.ROW_H}", src)

    def test_documented_boundaries(self):
        lo, hi = layout.CHROME_H + layout.SIDE_H_MIN, layout.CHROME_H + layout.SIDE_H_MAX
        self.assertEqual((lo, hi), (677, 807))
        self.assertEqual(layout.side_panel_height(lo - 1), layout.SIDE_H_MIN)
        self.assertEqual(layout.side_panel_height(lo), layout.SIDE_H_MIN)
        self.assertEqual(layout.side_panel_height(lo + 1), layout.SIDE_H_MIN + 1)
        self.assertEqual(layout.side_panel_height(hi), layout.SIDE_H_MAX)
        self.assertEqual(layout.side_panel_height(hi + 100), layout.SIDE_H_MAX)
        self.assertEqual(layout.side_panel_height(768), 211)

    def test_estimate_leaves_the_editor_its_minimum_when_the_window_is_tall_enough(self):
        for height in range(layout.CHROME_H + layout.SIDE_H_MIN, 1400):
            side = layout.side_panel_height(height)
            editor = height - layout.RIGHT_COLUMN_FIXED_H - side
            self.assertGreaterEqual(editor, layout.EDITOR_MIN_H, f"jendela {height}px: editor hanya {editor}px")

    def test_below_the_threshold_the_estimate_is_at_its_minimum(self):
        for height in range(300, layout.CHROME_H + layout.SIDE_H_MIN):
            self.assertEqual(layout.side_panel_height(height), layout.SIDE_H_MIN)

    def test_ci_windows_get_the_table_minimum_with_the_new_panel_height(self):
        # Kasus yang gagal di CI v1.9.15 (tabel 96 dan 77 px): panel kini benar-benar setinggi perkiraan.
        for window in (768, 749):
            side = layout.side_panel_height(window)
            table = window - layout.CI_OUTSIDE_H - side - layout.EDITOR_CHROME_H
            self.assertGreaterEqual(table, layout.TABLE_MIN_PX, f"jendela {window}px: tabel {table}px")


class EditorAndTimelineCompactnessTests(unittest.TestCase):
    """Penghematan tinggi v1.9.15.2 (dijaga lewat sumber agar tidak diam-diam kembali)."""

    def setUp(self) -> None:
        self.editor = (ROOT / "kaptionpro" / "ui" / "editor.py").read_text(encoding="utf-8")
        self.workspace = (ROOT / "kaptionpro" / "ui" / "workspace.py").read_text(encoding="utf-8")

    def test_toolbar_is_a_single_row_with_a_tools_menu(self):
        self.assertNotIn("row=i // 5", self.editor)
        self.assertIn("grid(row=0, column=i, padx=2, pady=2, sticky=\"w\")", self.editor)
        self.assertIn("self._tools_menu = tk.Menu(", self.editor)
        self.assertIn("def _open_tools_menu", self.editor)

    def test_tools_menu_groups_the_rarely_used_actions(self):
        block = self.editor[self.editor.index("TOOLS_MENU = ("):self.editor.index("    FILTERS = {")]
        for method in ("_import_srt", "_shift_all", "_bulk_edit", "_split_selected", "_merge_selected"):
            self.assertIn(f'"{method}"', block)
            self.assertIn(f"def {method}(", self.editor, f"{method} harus ada")

    def test_frequent_actions_stay_as_buttons(self):
        for text in ("↶ Undo", "↷ Redo", "🔍 Cari/Ganti", "✔ Auto-Fix"):
            self.assertIn(f'("{text}"', self.editor)

    def test_toolbar_fits_the_editor_width(self):
        # Lebar editor minimum ~ 500 - 18 (padx grid) - 20 (padx toolbar); tombol: 62 + 62 + 100 + 82 + menu 80 + padding 4 x 5.
        widths = 62 + 62 + 100 + 82 + 80 + 4 * 5
        self.assertLessEqual(widths, 500 - 18 - 20)

    def test_timeline_is_compact(self):
        # ruler 20 + (6 + 1) + 3 track x (34 + 2 x 2) + spacer 2 + border 2 = 145 px (dulu ~160)
        self.assertIn('pady=(6, 1))', self.workspace)
        self.assertEqual(self.workspace.count("pady=2)"), 2)          # label track + canvas track
        self.assertIn('height=2, fg_color="transparent").grid(row=4, column=0)', self.workspace)
        self.assertIn("TRACK_HEIGHT = 34", self.workspace)
        self.assertEqual(20 + 7 + 3 * (34 + 4) + 2 + 2, 145)


class RebalanceTests(unittest.TestCase):
    """Penyetelan akhir dari ukuran tabel NYATA: perkiraan konstanta boleh meleset, tabel tetap dijaga."""

    def test_nothing_changes_when_the_table_is_big_enough(self):
        self.assertEqual(layout.rebalanced_side_height(165, 90), 165)
        self.assertEqual(layout.rebalanced_side_height(165, 240), 165)

    def test_panel_shrinks_by_the_deficit(self):
        self.assertEqual(layout.rebalanced_side_height(165, 80), 155)
        self.assertEqual(layout.rebalanced_side_height(146, 77), 133)

    def test_panel_never_goes_below_the_floor(self):
        self.assertEqual(layout.rebalanced_side_height(130, 10), layout.SIDE_H_FLOOR)
        self.assertEqual(layout.rebalanced_side_height(120, 10), layout.SIDE_H_FLOOR)
        self.assertEqual(layout.SIDE_H_FLOOR, layout.SIDE_H_MIN, "panel tidak dipaksa lebih kecil dari ukuran nyamannya")

    def test_panel_is_never_enlarged(self):
        for table in (2, 40, 89, 90, 91, 400):
            self.assertLessEqual(layout.rebalanced_side_height(150, table), 150)

    def test_unmeasured_table_is_ignored(self):
        self.assertEqual(layout.rebalanced_side_height(150, 0), 150)
        self.assertEqual(layout.rebalanced_side_height(150, 1), 150)

    def test_dpi_scaling_converts_physical_deficit_to_logical_pixels(self):
        self.assertEqual(layout.rebalanced_side_height(165, 70, scaling=1.25), 165 - 16)   # 20 px fisik = 16 logis

    def test_guaranteed_table_px(self):
        self.assertEqual(layout.guaranteed_table_px(768), layout.TABLE_MIN_PX)
        self.assertEqual(layout.guaranteed_table_px(700), layout.TABLE_MIN_PX)
        self.assertEqual(layout.guaranteed_table_px(680), layout.TABLE_MIN_PX)
        self.assertEqual(layout.guaranteed_table_px(670), 83)
        self.assertEqual(layout.guaranteed_table_px(640), 53)
        self.assertEqual(layout.guaranteed_table_px(300), 0)
        self.assertEqual(layout.guaranteed_table_px(1200), layout.TABLE_MIN_PX)

    def test_closed_loop_meets_the_guarantee_whatever_the_real_fixed_height_is(self):
        """Simulasi UI: tabel = tinggi jendela - K - panel - 298, K (elemen lain) tidak diketahui persis."""
        for fixed in range(140, 221, 5):                                  # CI: 164; rentang lebar sebagai uji ketahanan
            for window in range(640, 1201, 8):
                side = layout.side_panel_height(window)
                for _ in range(4):                                        # maksimal 4 putaran seperti di aplikasi
                    table = window - fixed - side - layout.EDITOR_CHROME_H
                    side = layout.rebalanced_side_height(side, max(table, 2))
                table = window - fixed - side - layout.EDITOR_CHROME_H
                reachable = window - fixed - layout.SIDE_H_FLOOR - layout.EDITOR_CHROME_H
                want = min(layout.TABLE_MIN_PX, reachable)
                self.assertGreaterEqual(table, want, f"K={fixed}, jendela {window}: tabel {table} < {want}")

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
