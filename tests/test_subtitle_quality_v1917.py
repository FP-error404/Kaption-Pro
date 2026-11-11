"""
Tes v1.9.17: penguatan pemeriksaan waktu (bool/teks/angka raksasa ditolak), aturan opsional durasi minimum/maksimum dan
jeda minimum (QualityRules.min_duration / max_duration / min_gap), serta penjaga agar tes GUI pengukuran tidak kembali
memakai skipTest (false pass, temuan review v1.9.16).

Murni Python: tidak membutuhkan CustomTkinter/GUI.
"""

from __future__ import annotations

import ast
import math
import sys
import time
import unittest
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kaptionpro.core import subtitle_quality as sq  # noqa: E402


@dataclass
class Seg:
    start: object
    end: object
    text: str = "Halo"


def codes(report: sq.QualityReport, index: int | None = None) -> list[str]:
    return [i.code for i in report.issues if index is None or i.index == index]


class AsSecondsTests(unittest.TestCase):
    def test_real_numbers_are_accepted(self):
        self.assertEqual(sq._as_seconds(2), 2.0)
        self.assertEqual(sq._as_seconds(2.5), 2.5)
        self.assertEqual(sq._as_seconds(Fraction(1, 2)), 0.5)
        self.assertEqual(sq._as_seconds(0), 0.0)
        self.assertEqual(sq._as_seconds(-3.5), -3.5)

    def test_bool_is_rejected(self):
        self.assertIsNone(sq._as_seconds(True))
        self.assertIsNone(sq._as_seconds(False))

    def test_non_numbers_are_rejected(self):
        for value in ("1", "", None, [1], (1,), {"a": 1}, Decimal("1.5"), 1 + 2j, object(), b"1"):
            self.assertIsNone(sq._as_seconds(value), repr(value))

    def test_non_finite_is_rejected(self):
        for value in (math.nan, math.inf, -math.inf, Fraction(1, 1) * 10 ** 400):
            self.assertIsNone(sq._as_seconds(value), repr(value))

    def test_integers_too_big_for_float_do_not_raise(self):
        self.assertIsNone(sq._as_seconds(10 ** 400))
        self.assertIsNone(sq._as_seconds(-(10 ** 400)))


class StrictTimingTests(unittest.TestCase):
    def test_bool_timings_are_invalid(self):
        for seg in (Seg(True, 5), Seg(0, True), Seg(False, 1), Seg(True, True), Seg(False, True)):
            self.assertEqual(codes(sq.check_subtitles([seg])), [sq.INVALID_TIMING], (seg.start, seg.end))
            self.assertFalse(sq._valid_timing(seg))

    def test_text_and_exotic_types_are_invalid(self):
        for seg in (Seg("0", "2"), Seg(0, "2"), Seg(Decimal(0), Decimal(2)), Seg([0], [2]), Seg(0j, 2j)):
            self.assertEqual(codes(sq.check_subtitles([seg])), [sq.INVALID_TIMING])

    def test_huge_ints_are_invalid_and_never_raise(self):
        for seg in (Seg(0, 10 ** 400), Seg(10 ** 400, 10 ** 401), Seg(-(10 ** 400), 0)):
            self.assertEqual(codes(sq.check_subtitles([seg])), [sq.INVALID_TIMING])

    def test_ints_floats_and_fractions_are_valid(self):
        for seg in (Seg(0, 2), Seg(0.0, 2.0), Seg(Fraction(0), Fraction(2)), Seg(0, 2.0), Seg(-1, 1)):
            self.assertTrue(sq._valid_timing(seg), (seg.start, seg.end))
            self.assertTrue(sq.check_subtitles([seg]).ok)

    def test_non_finite_is_invalid(self):
        for seg in (Seg(math.nan, 1), Seg(0, math.nan), Seg(0, math.inf), Seg(-math.inf, 0), Seg(math.inf, math.inf)):
            self.assertFalse(sq._valid_timing(seg))

    def test_end_must_be_after_start(self):
        self.assertFalse(sq._valid_timing(Seg(1, 1)))
        self.assertFalse(sq._valid_timing(Seg(2, 1)))
        self.assertTrue(sq._valid_timing(Seg(1, 1.000001)))

    def test_missing_attributes_are_invalid_not_a_crash(self):
        class Bare:
            text = "x"
        self.assertEqual(codes(sq.check_subtitles([Bare()])), [sq.INVALID_TIMING])           # type: ignore[list-item]

    def test_extreme_but_finite_values_do_not_crash(self):
        report = sq.check_subtitles([Seg(-1e308, 1e308), Seg(0, 1)])                          # durasi: inf setelah dikurangi
        self.assertIsInstance(report, sq.QualityReport)
        rules = sq.QualityRules(max_duration=10.0, min_gap=0.1)
        report = sq.check_subtitles([Seg(-1e308, 1e308), Seg(0, 1)], rules)
        self.assertIn(sq.DURATION_TOO_LONG, codes(report, 0))

    def test_invalid_segments_do_not_break_the_valid_ones(self):
        segs = [Seg(0, 1), Seg(True, 5), Seg(2, 3, "x" * 100), Seg(None, None), Seg(4, 5)]
        report = sq.check_subtitles(segs)
        self.assertEqual([(i.index, i.code) for i in report.issues],
                         [(1, sq.INVALID_TIMING), (2, sq.CPS_VERY_HIGH), (2, sq.LINE_TOO_LONG), (3, sq.INVALID_TIMING)])

    def test_cps_rejects_bool_duration(self):
        self.assertIsNone(sq.cps("halo", True))
        self.assertIsNone(sq.cps("halo", False))
        self.assertAlmostEqual(sq.cps("halo", Fraction(1, 2)), 8.0)
        self.assertIsNone(sq.cps("halo", 10 ** 400))


class RulesValidationTests(unittest.TestCase):
    def test_new_options_default_to_off(self):
        r = sq.DEFAULT_RULES
        self.assertEqual((r.min_duration, r.max_duration, r.min_gap), (None, None, None))

    def test_valid_values(self):
        sq.QualityRules(min_duration=0.83, max_duration=7, min_gap=0.083)
        sq.QualityRules(min_duration=1.0, max_duration=1.0)                  # sama besar diperbolehkan
        sq.QualityRules(min_gap=0)                                           # nol = tidak pernah memperingatkan
        sq.QualityRules(min_duration=Fraction(5, 6))

    def test_invalid_values_are_rejected(self):
        bad = (dict(min_duration=0), dict(min_duration=-1), dict(max_duration=0), dict(max_duration=-5),
               dict(min_gap=-0.01), dict(min_duration=math.nan), dict(max_duration=math.inf),
               dict(min_gap=math.nan), dict(min_gap=-math.inf), dict(min_duration=True), dict(max_duration=True),
               dict(min_gap=False), dict(min_duration="1"), dict(max_duration="7"), dict(min_gap="0.1"),
               dict(min_duration=2.0, max_duration=1.0), dict(min_duration=[1]))
        for kwargs in bad:
            with self.assertRaises(ValueError, msg=str(kwargs)):
                sq.QualityRules(**kwargs)                                     # type: ignore[arg-type]

    def test_rules_remain_immutable(self):
        with self.assertRaises(Exception):
            sq.DEFAULT_RULES.min_gap = 1.0                                    # type: ignore[misc]

    def test_defaults_do_not_change_v1916_behaviour(self):
        segs = [Seg(0, 0.01, "x"), Seg(0.01, 100, "halo"), Seg(100, 100.5, "a\nb\nc"), Seg(100.5, 101, "y" * 50)]
        report = sq.check_subtitles(segs)
        new_codes = {sq.DURATION_TOO_SHORT, sq.DURATION_TOO_LONG, sq.GAP_TOO_SMALL}
        self.assertFalse(new_codes & set(codes(report)))


class DurationTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse({sq.DURATION_TOO_SHORT, sq.DURATION_TOO_LONG} & set(codes(sq.check_subtitles(
            [Seg(0, 0.01, ""), Seg(5, 500, "")]))))

    def test_min_duration_boundary(self):
        rules = sq.QualityRules(min_duration=0.83)
        self.assertTrue(sq.check_subtitles([Seg(0, 0.83, "")], rules).ok)
        self.assertEqual(codes(sq.check_subtitles([Seg(0, 0.82, "")], rules)), [sq.DURATION_TOO_SHORT])

    def test_float_noise_does_not_trigger_min_duration(self):
        rules = sq.QualityRules(min_duration=0.83)
        for start in (0.0, 1.0, 2.0, 5.0, 12.34, 3600.1, 7199.17):
            self.assertTrue(sq.check_subtitles([Seg(start, start + 0.83, "")], rules).ok, start)

    def test_float_noise_does_not_trigger_max_duration(self):
        rules = sq.QualityRules(max_duration=7.0)
        for start in (0.0, 0.1, 1.1, 12.34, 3600.1, 7199.17):
            self.assertTrue(sq.check_subtitles([Seg(start, start + 7.0, "")], rules).ok, start)

    def test_min_duration_issue_fields(self):
        rules = sq.QualityRules(min_duration=0.83)
        issue = sq.check_subtitles([Seg(1, 1.3, "")], rules).issues[0]
        self.assertEqual((issue.code, issue.severity, issue.limit), (sq.DURATION_TOO_SHORT, sq.WARNING, 0.83))
        self.assertAlmostEqual(issue.value, 0.3)
        self.assertIn("singkat", issue.message)
        self.assertIn("0.300", issue.message)

    def test_max_duration(self):
        rules = sq.QualityRules(max_duration=7.0)
        self.assertTrue(sq.check_subtitles([Seg(0, 7.0, "")], rules).ok)
        report = sq.check_subtitles([Seg(0, 7.5, "")], rules)
        self.assertEqual(codes(report), [sq.DURATION_TOO_LONG])
        self.assertEqual((report.issues[0].severity, report.issues[0].limit), (sq.WARNING, 7.0))
        self.assertIn("lama", report.issues[0].message)

    def test_both_limits_together(self):
        rules = sq.QualityRules(min_duration=1.0, max_duration=5.0)
        segs = [Seg(0, 0.5, ""), Seg(1, 3, ""), Seg(4, 10, "")]
        report = sq.check_subtitles(segs, rules)
        self.assertEqual([(i.index, i.code) for i in report.issues],
                         [(0, sq.DURATION_TOO_SHORT), (2, sq.DURATION_TOO_LONG)])

    def test_equal_min_and_max(self):
        rules = sq.QualityRules(min_duration=2.0, max_duration=2.0)
        self.assertTrue(sq.check_subtitles([Seg(0, 2, "")], rules).ok)
        self.assertEqual(len(sq.check_subtitles([Seg(0, 3, "")], rules).issues), 1)

    def test_invalid_timing_gets_no_duration_issue(self):
        rules = sq.QualityRules(min_duration=1.0, max_duration=5.0)
        self.assertEqual(codes(sq.check_subtitles([Seg(5, 1, ""), Seg(True, 2, "")], rules)),
                         [sq.INVALID_TIMING, sq.INVALID_TIMING])

    def test_integer_limits_are_accepted(self):
        rules = sq.QualityRules(min_duration=1, max_duration=7)
        self.assertEqual(codes(sq.check_subtitles([Seg(0, 0.5, "")], rules)), [sq.DURATION_TOO_SHORT])

    def test_short_and_fast_segment_reports_both_errors_first(self):
        rules = sq.QualityRules(min_duration=0.83)
        report = sq.check_subtitles([Seg(0, 0.2, "x" * 10)], rules)                # 50 CPS dan 0,2 detik
        self.assertEqual(codes(report), [sq.CPS_VERY_HIGH, sq.DURATION_TOO_SHORT])
        self.assertEqual([i.severity for i in report.issues], [sq.ERROR, sq.WARNING])


class GapTests(unittest.TestCase):
    RULES = sq.QualityRules(min_gap=0.083)

    def test_off_by_default(self):
        self.assertTrue(sq.check_subtitles([Seg(0, 1), Seg(1, 2), Seg(2, 3)]).ok)

    def test_touching_segments_warn_when_enabled(self):
        report = sq.check_subtitles([Seg(0, 1), Seg(1, 2)], self.RULES)
        self.assertEqual([(i.index, i.code) for i in report.issues], [(1, sq.GAP_TOO_SMALL)])
        issue = report.issues[0]
        self.assertEqual((issue.severity, issue.other_index, issue.limit), (sq.WARNING, 0, 0.083))
        self.assertEqual(issue.value, 0.0)
        self.assertIn("#1", issue.message)
        self.assertIn("Jeda", issue.message)

    def test_gap_boundary(self):
        self.assertTrue(sq.check_subtitles([Seg(0, 1), Seg(1.083, 2)], self.RULES).ok)
        self.assertTrue(sq.check_subtitles([Seg(0, 1), Seg(1.5, 2)], self.RULES).ok)
        report = sq.check_subtitles([Seg(0, 1), Seg(1.05, 2)], self.RULES)
        self.assertEqual(codes(report), [sq.GAP_TOO_SMALL])
        self.assertAlmostEqual(report.issues[0].value, 0.05)

    def test_tolerance_absorbs_float_noise_at_the_boundary(self):
        # jeda 0,0825 vs minimal 0,083 dengan toleransi 1 ms -> lolos; jeda 0,081 -> peringatan
        self.assertTrue(sq.check_subtitles([Seg(0, 1), Seg(1.0825, 2)], self.RULES).ok)
        self.assertEqual(codes(sq.check_subtitles([Seg(0, 1), Seg(1.081, 2)], self.RULES)), [sq.GAP_TOO_SMALL])

    def test_zero_min_gap_never_warns(self):
        rules = sq.QualityRules(min_gap=0)
        self.assertTrue(sq.check_subtitles([Seg(0, 1), Seg(1, 2), Seg(2.0004, 3)], rules).ok)

    def test_overlap_takes_precedence_and_is_not_reported_twice(self):
        report = sq.check_subtitles([Seg(0, 2), Seg(1, 3)], self.RULES)
        self.assertEqual(codes(report), [sq.OVERLAP])

    def test_tiny_overlap_within_tolerance_counts_as_a_zero_gap(self):
        report = sq.check_subtitles([Seg(0, 1.0004), Seg(1.0, 2)], self.RULES)
        self.assertEqual(codes(report), [sq.GAP_TOO_SMALL])
        self.assertEqual(report.issues[0].value, 0.0)

    def test_gap_is_measured_from_the_furthest_end_not_the_neighbour(self):
        segs = [Seg(0, 10), Seg(1, 2), Seg(10.02, 11)]
        report = sq.check_subtitles(segs, self.RULES)
        self.assertEqual([(i.index, i.code, i.other_index) for i in report.issues],
                         [(1, sq.OVERLAP, 0), (2, sq.GAP_TOO_SMALL, 0)])

    def test_first_segment_never_has_a_gap_issue(self):
        self.assertTrue(sq.check_subtitles([Seg(0, 1)], self.RULES).ok)
        self.assertEqual(sq.check_subtitles([], self.RULES).total, 0)

    def test_unsorted_input_reports_input_indexes(self):
        segs = [Seg(5, 6), Seg(0, 1), Seg(1.02, 2)]
        report = sq.check_subtitles(segs, self.RULES)
        self.assertEqual([(i.index, i.other_index) for i in report.issues], [(2, 1)])

    def test_invalid_segments_are_excluded_from_the_gap_scan(self):
        segs = [Seg(0, 1), Seg(1.0, 0.5), Seg(True, 5), Seg(1.5, 2)]
        report = sq.check_subtitles(segs, self.RULES)
        self.assertEqual(sorted(codes(report)), sorted([sq.INVALID_TIMING, sq.INVALID_TIMING]))

    def test_gap_warning_does_not_make_the_report_an_error(self):
        report = sq.check_subtitles([Seg(0, 1), Seg(1, 2)], self.RULES)
        self.assertEqual(report.errors, [])
        self.assertEqual(len(report.warnings), 1)


class CombinedTests(unittest.TestCase):
    STRICT = sq.QualityRules(min_duration=5 / 6, max_duration=7.0, min_gap=0.083)

    def test_all_codes_are_unique_and_complete(self):
        self.assertEqual(len(sq.ALL_CODES), len(set(sq.ALL_CODES)))
        declared = {getattr(sq, n) for n in dir(sq) if n.isupper() and isinstance(getattr(sq, n), str)
                    and getattr(sq, n) not in (sq.ERROR, sq.WARNING)}
        self.assertEqual(declared, set(sq.ALL_CODES))

    def test_new_codes_are_warnings_only(self):
        segs = [Seg(0, 0.3, ""), Seg(0.3, 9, "")]
        report = sq.check_subtitles(segs, self.STRICT)
        self.assertEqual(set(codes(report)), {sq.DURATION_TOO_SHORT, sq.DURATION_TOO_LONG, sq.GAP_TOO_SMALL})
        self.assertEqual(report.errors, [])

    def test_counts_follow_all_codes_order(self):
        segs = [Seg(0, 0.3, "x" * 60), Seg(0.3, 9, ""), Seg(5, 6, "a\nb\nc")]
        report = sq.check_subtitles(segs, self.STRICT)
        counts = report.counts()
        self.assertEqual(list(counts), [c for c in sq.ALL_CODES if c in counts])
        self.assertGreaterEqual(len(counts), 5)

    def test_clean_subtitles_pass_even_with_strict_rules(self):
        segs = [Seg(i * 3.0, i * 3.0 + 2.9, "Ini kalimat yang wajar.") for i in range(100)]
        self.assertTrue(sq.check_subtitles(segs, self.STRICT).ok)

    def test_input_is_not_modified_by_new_rules(self):
        segs = [Seg(0, 0.3, "x"), Seg(0.3, 9, "y")]
        snapshot = [(s.start, s.end, s.text) for s in segs]
        sq.check_subtitles(segs, self.STRICT)
        self.assertEqual([(s.start, s.end, s.text) for s in segs], snapshot)

    def test_works_with_the_real_subtitle_segment(self):
        from kaptionpro.models import SubtitleSegment
        segs = [SubtitleSegment(0.0, 0.5, "Hai"), SubtitleSegment(0.5, 9.0, "Halo dunia")]
        report = sq.check_subtitles(segs, self.STRICT)
        self.assertEqual(set(codes(report)), {sq.DURATION_TOO_SHORT, sq.DURATION_TOO_LONG, sq.GAP_TOO_SMALL})


class PerformanceWithNewRulesTests(unittest.TestCase):
    STRICT = sq.QualityRules(min_duration=5 / 6, max_duration=7.0, min_gap=0.083)

    def test_two_hour_video_with_all_rules_enabled(self):
        segs = [Seg(i * 3.0, i * 3.0 + 2.9, "Ini contoh kalimat subtitle yang wajar.") for i in range(2400)]
        t0 = time.perf_counter()
        report = sq.check_subtitles(segs, self.STRICT)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertTrue(report.ok, report.summary())

    def test_worst_case_with_all_rules_enabled(self):
        segs = [Seg(i * 0.01, i * 0.01 + 0.05, "x" * 60 + "\ny\nz") for i in range(5000)]
        t0 = time.perf_counter()
        report = sq.check_subtitles(segs, self.STRICT)
        self.assertLess(time.perf_counter() - t0, 4.0)
        self.assertEqual(report.total, 5000)
        self.assertEqual(report.counts()[sq.DURATION_TOO_SHORT], 5000)
        self.assertGreater(report.counts()[sq.OVERLAP], 0)


class NoSilentSkipInMeasuringGuiTests(unittest.TestCase):
    """
    Temuan review v1.9.16 (P0): test_15 memakai skipTest, sehingga panel yang kekecilan tetap tampak 'lolos'. Tes yang
    tugasnya MENGUKUR harus gagal bila tak bisa mengukur. Pemeriksaan sumber ini berjalan di core-tests (tanpa GUI),
    jadi skipTest tidak bisa diam-diam kembali.
    """

    MEASURING = ("test_11", "test_14", "test_15", "test_16")

    def setUp(self) -> None:
        self.src = (ROOT / "tests" / "test_gui_smoke.py").read_text(encoding="utf-8")
        self.tree = ast.parse(self.src)

    def _functions(self, prefix: str) -> list[ast.FunctionDef]:
        return [n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef) and n.name.startswith(prefix)]

    def test_measuring_tests_exist(self):
        for prefix in self.MEASURING:
            self.assertEqual(len(self._functions(prefix)), 1, prefix)

    def test_measuring_tests_never_call_skiptest(self):
        for prefix in self.MEASURING:
            for fn in self._functions(prefix):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        self.assertNotIn(node.func.attr, ("skipTest", "skip"), f"{fn.name} memakai skip: false pass")

    def test_measuring_tests_have_no_skip_decorators(self):
        for prefix in self.MEASURING:
            for fn in self._functions(prefix):
                for deco in fn.decorator_list:
                    self.assertNotIn("skip", ast.dump(deco).lower(), fn.name)

    def test_test_15_asserts_its_preconditions_and_the_panel_size(self):
        fn = self._functions("test_15")[0]
        body = ast.get_source_segment(self.src, fn) or ""
        for needle in ('self.assertEqual(ws._mode, "landscape"',
                       "self.assertGreaterEqual(ws.winfo_height() / scale, layout.SIDE_AUTOHIDE_H",
                       "self.assertTrue(ws._side_visible",
                       "self.assertGreaterEqual(box.winfo_height(), need",
                       "self.assertLessEqual(menu_bottom, box_bottom"):
            self.assertIn(needle, body)

    def test_test_14_asserts_landscape(self):
        body = ast.get_source_segment(self.src, self._functions("test_14")[0]) or ""
        self.assertIn('self.assertEqual(ws._mode, "landscape"', body)


if __name__ == "__main__":
    unittest.main()
