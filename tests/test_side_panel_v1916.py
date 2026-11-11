"""
Tes v1.9.16: panel Language Presets dirapikan supaya menu Whisper model tampak UTUH (dulu terpotong di bawah panel).

Murni aritmetika + pemeriksaan sumber: tidak membutuhkan CustomTkinter/GUI.
(Pengukuran di aplikasi nyata: test_gui_smoke.py::test_15.)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kaptionpro.config import LANGUAGE_PRESETS  # noqa: E402
from kaptionpro.core import layout  # noqa: E402

WORKSPACE = (ROOT / "kaptionpro" / "ui" / "workspace.py").read_text(encoding="utf-8")
COMPONENTS = (ROOT / "kaptionpro" / "ui" / "components.py").read_text(encoding="utf-8")


class WhisperFitsTests(unittest.TestCase):
    def test_constants_are_pinned(self):
        # Mengubah angka apa pun WAJIB disengaja: perbarui tes ini dan komentar di core/layout.py.
        self.assertEqual(
            (layout.SIDE_HEADER_LABEL_H, layout.SIDE_HEADER_PAD, layout.SIDE_RULE_H, layout.SIDE_RULE_PAD,
             layout.SIDE_PRESET_BTN_H, layout.SIDE_PRESET_PAD, layout.SIDE_FIELD_LABEL_H, layout.SIDE_FIELD_LABEL_PAD,
             layout.SIDE_MENU_H, layout.SIDE_MENU_PAD, layout.SIDE_FRAME_ALLOWANCE),
            (18, (4, 1), 1, (0, 3), 28, (1, 2), 18, (3, 0), 26, (0, 2), 12))

    def test_whisper_bottom_is_the_sum_of_its_parts(self):
        header = 18 + 4 + 1 + 1 + 0 + 3
        self.assertEqual(header, 27)
        self.assertEqual(layout.side_whisper_bottom(0), header + (18 + 3) + (26 + 2))
        self.assertEqual(layout.side_whisper_bottom(3), 27 + 3 * (28 + 3) + 21 + 28)
        self.assertEqual(layout.side_whisper_bottom(3), 169)

    def test_each_extra_preset_costs_one_button(self):
        self.assertEqual(layout.side_whisper_bottom(4) - layout.side_whisper_bottom(3), 28 + 1 + 2)

    def test_whisper_menu_fits_in_the_shortest_panel_that_is_shown(self):
        """Panel terpendek yang masih ditampilkan = tinggi perkiraan tepat di batas autohide (jendela SIDE_AUTOHIDE_H)."""
        shortest = layout.side_panel_height(layout.SIDE_AUTOHIDE_H)
        need = layout.side_whisper_bottom(len(LANGUAGE_PRESETS)) + layout.SIDE_FRAME_ALLOWANCE
        self.assertLessEqual(need, shortest,
                             f"menu Whisper butuh {need}px, panel terpendek {shortest}px: menambah preset/jarak? "
                             f"sesuaikan SIDE_* di core/layout.py")

    def test_whisper_menu_fits_for_every_window_that_shows_the_panel(self):
        need = layout.side_whisper_bottom(len(LANGUAGE_PRESETS)) + layout.SIDE_FRAME_ALLOWANCE
        for window in range(layout.SIDE_AUTOHIDE_H, 1600):
            self.assertGreaterEqual(layout.side_panel_height(window), need, f"jendela {window}px")

    def test_the_old_spacing_did_not_fit(self):
        """Dokumentasi masalahnya: header 47 + 3 x (34 + 6) + label 34 + menu 32 = 233 > panel 183."""
        old_bottom = (28 + 10 + 2 + 1 + 6) + 3 * (34 + 6) + (28 + 6) + (28 + 4)
        self.assertEqual(old_bottom, 233)
        self.assertGreater(old_bottom + layout.SIDE_FRAME_ALLOWANCE, layout.side_panel_height(layout.SIDE_AUTOHIDE_H))

    def test_there_are_three_presets_today(self):
        self.assertEqual(len(LANGUAGE_PRESETS), 3)

    def test_geometry_with_dpi_scaling_is_in_logical_pixels(self):
        # CustomTkinter mengalikan ukuran widget dengan skala; panel (side_box) juga: rasio keduanya tetap.
        for scale in (1.0, 1.25, 1.5, 2.0):
            panel_px = round(layout.side_panel_height(layout.SIDE_AUTOHIDE_H * scale, scale) * scale)
            need_px = round((layout.side_whisper_bottom(3) + layout.SIDE_FRAME_ALLOWANCE) * scale)
            self.assertGreaterEqual(panel_px + 1, need_px, scale)


class SourceWiringTests(unittest.TestCase):
    def test_side_panel_uses_the_layout_constants(self):
        for needle in ('section_header(self, "LANGUAGE PRESETS", SIDE_HEADER_LABEL_H, SIDE_HEADER_PAD, SIDE_RULE_PAD)',
                       "height=SIDE_PRESET_BTN_H",
                       "pady=SIDE_PRESET_PAD",
                       'text="Whisper model", text_color=Theme.TEXT_MUTED, anchor="w", height=SIDE_FIELD_LABEL_H',
                       "variable=self.model_var, height=SIDE_MENU_H",
                       "self.model_menu.pack(fill=\"x\", padx=14, pady=SIDE_MENU_PAD)",
                       'text="Penerjemah", text_color=Theme.TEXT_MUTED, anchor="w", height=SIDE_FIELD_LABEL_H',
                       "variable=self.translator_var, height=SIDE_MENU_H",
                       'section_header(self, "TEXT STYLES", SIDE_HEADER_LABEL_H, SIDE_HEADER_PAD, SIDE_RULE_PAD)'):
            self.assertIn(needle, WORKSPACE)
        self.assertGreaterEqual(WORKSPACE.count("pady=SIDE_FIELD_LABEL_PAD"), 2, "Whisper dan Penerjemah memakai jarak yang sama")

    def test_old_hardcoded_spacing_is_gone_from_the_presets_block(self):
        first = WORKSPACE.index('section_header(self, "LANGUAGE PRESETS"')
        block = WORKSPACE[first:WORKSPACE.index("self.generate_btn = gold_button(")]
        self.assertNotIn("height=34", block)
        self.assertNotIn("pady=3)", block)
        self.assertNotIn("pady=(6, 0)", block)
        self.assertNotIn("pady=(0, 4)", block)

    def test_layout_constants_are_imported_not_copied(self):
        first = WORKSPACE.index("from ..core.layout import (")
        head = WORKSPACE[first:WORKSPACE.index(")\n", first)]
        for name in ("SIDE_FIELD_LABEL_H", "SIDE_FIELD_LABEL_PAD", "SIDE_HEADER_LABEL_H", "SIDE_HEADER_PAD", "SIDE_MENU_H",
                     "SIDE_MENU_PAD", "SIDE_PRESET_BTN_H", "SIDE_PRESET_PAD", "SIDE_RULE_PAD"):
            self.assertIn(name, head)

    def test_section_header_keeps_the_old_defaults_for_other_callers(self):
        self.assertIn("label_h: int | None = None, pad: tuple[int, int] = (10, 2)", COMPONENTS)
        self.assertIn("rule_pad: tuple[int, int] = (0, 6)", COMPONENTS)

    def test_generate_button_and_style_controls_are_untouched(self):
        self.assertIn('gold_button(self, "✨  Generate Subtitles (AI)", on_generate, filled=False, height=36)', WORKSPACE)
        self.assertIn('small_button(self, "⚙  Pengaturan font/posisi…", on_style_dialog, 220)', WORKSPACE)


if __name__ == "__main__":
    unittest.main()
