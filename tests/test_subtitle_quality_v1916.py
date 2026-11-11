"""
Tes v1.9.16: Subtitle Quality Checker (core/subtitle_quality.py): CPS, panjang baris, overlap, waktu tidak valid,
ketahanan terhadap data jelek, dan performa untuk subtitle besar (video 2 jam).

Murni Python: tidak membutuhkan CustomTkinter/GUI.
"""

from __future__ import annotations

import copy
import math
import random
import sys
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kaptionpro.core import subtitle_quality as sq  # noqa: E402


@dataclass
class Seg:
    start: float
    end: float
    text: str = "Halo"


def codes(report: sq.QualityReport, index: int | None = None) -> list[str]:
    return [i.code for i in report.issues if index is None or i.index == index]


class TextMeasureTests(unittest.TestCase):
    def test_split_lines_handles_newlines_and_ass_breaks(self):
        self.assertEqual(sq.split_lines("satu\ndua"), ["satu", "dua"])
        self.assertEqual(sq.split_lines("satu\\Ndua"), ["satu", "dua"])
        self.assertEqual(sq.split_lines("satu\r\ndua"), ["satu", "dua"])

    def test_split_lines_strips_blank_lines_and_padding(self):
        self.assertEqual(sq.split_lines("  satu  \n\n   \n dua "), ["satu", "dua"])
        self.assertEqual(sq.split_lines(""), [])
        self.assertEqual(sq.split_lines(None), [])

    def test_invisible_tags_are_ignored(self):
        self.assertEqual(sq.split_lines("<i>Halo</i> {\\an8}dunia"), ["Halo dunia"])
        self.assertEqual(sq.visible_char_count("<b>ab</b>{\\k20}cd"), 4)

    def test_visible_char_count_counts_spaces_but_not_line_breaks(self):
        self.assertEqual(sq.visible_char_count("ab cd"), 5)
        self.assertEqual(sq.visible_char_count("ab\ncd"), 4)
        self.assertEqual(sq.visible_char_count("ab \n cd"), 4)          # spasi tepi baris tidak dihitung

    def test_non_string_text_is_tolerated(self):
        self.assertEqual(sq.visible_char_count(None), 0)
        self.assertEqual(sq.visible_char_count(123), 3)

    def test_cps_value(self):
        self.assertEqual(sq.cps("x" * 34, 2.0), 17.0)
        self.assertAlmostEqual(sq.cps("halo", 0.5), 8.0)

    def test_cps_is_none_for_invalid_duration(self):
        for duration in (0, -1, math.nan, math.inf, -math.inf, None, "2"):
            self.assertIsNone(sq.cps("halo", duration), duration)


class RulesTests(unittest.TestCase):
    def test_defaults(self):
        r = sq.DEFAULT_RULES
        self.assertEqual((r.max_cps, r.hard_max_cps, r.max_chars_per_line, r.max_lines, r.overlap_tolerance),
                         (17.0, 25.0, 42, 2, 0.001))

    def test_rules_are_immutable(self):
        with self.assertRaises(Exception):
            sq.DEFAULT_RULES.max_cps = 99                                # type: ignore[misc]

    def test_invalid_rules_are_rejected(self):
        bad = (dict(max_cps=0), dict(max_cps=-1), dict(max_cps=math.nan), dict(max_cps=math.inf),
               dict(hard_max_cps=10.0), dict(max_chars_per_line=0), dict(max_lines=0), dict(max_lines=2.5),
               dict(max_chars_per_line=True), dict(overlap_tolerance=-0.1), dict(max_cps="17"))
        for kwargs in bad:
            with self.assertRaises(ValueError, msg=str(kwargs)):
                sq.QualityRules(**kwargs)

    def test_hard_equal_to_soft_is_allowed(self):
        sq.QualityRules(max_cps=20.0, hard_max_cps=20.0)


class CpsCheckTests(unittest.TestCase):
    def test_exactly_at_the_limit_passes(self):
        report = sq.check_subtitles([Seg(0, 2, "x" * 34)])                 # 17.0 CPS persis
        self.assertTrue(report.ok, report.issues)

    def test_slightly_over_is_a_warning(self):
        report = sq.check_subtitles([Seg(0, 2, "x" * 36)])                 # 18 CPS
        self.assertEqual(codes(report), [sq.CPS_HIGH])
        issue = report.issues[0]
        self.assertEqual(issue.severity, sq.WARNING)
        self.assertAlmostEqual(issue.value, 18.0)
        self.assertEqual(issue.limit, 17.0)
        self.assertEqual(report.errors, [])

    def test_far_over_is_an_error(self):
        report = sq.check_subtitles([Seg(0, 1, "x" * 30)])                 # 30 CPS
        self.assertEqual(codes(report), [sq.CPS_VERY_HIGH])
        self.assertEqual(report.issues[0].severity, sq.ERROR)
        self.assertEqual(report.issues[0].limit, 25.0)

    def test_hard_limit_boundary(self):
        self.assertEqual(codes(sq.check_subtitles([Seg(0, 1, "x" * 25)])), [sq.CPS_HIGH])        # 25.0 belum error
        self.assertEqual(codes(sq.check_subtitles([Seg(0, 1, "x" * 26)])), [sq.CPS_VERY_HIGH])

    def test_only_one_cps_code_per_segment(self):
        report = sq.check_subtitles([Seg(0, 0.1, "x" * 40)])
        self.assertEqual(codes(report).count(sq.CPS_HIGH) + codes(report).count(sq.CPS_VERY_HIGH), 1)

    def test_custom_rules(self):
        rules = sq.QualityRules(max_cps=10.0, hard_max_cps=12.0)
        self.assertEqual(codes(sq.check_subtitles([Seg(0, 1, "x" * 11)], rules)), [sq.CPS_HIGH])
        self.assertEqual(codes(sq.check_subtitles([Seg(0, 1, "x" * 13)], rules)), [sq.CPS_VERY_HIGH])
        self.assertTrue(sq.check_subtitles([Seg(0, 1, "x" * 11)]).ok)

    def test_invisible_tags_do_not_inflate_cps(self):
        self.assertTrue(sq.check_subtitles([Seg(0, 1, "<i>" + "x" * 15 + "</i>{\\an8}")]).ok)

    def test_empty_text_has_no_cps_issue(self):
        self.assertTrue(sq.check_subtitles([Seg(0, 1, "")]).ok)
        self.assertTrue(sq.check_subtitles([Seg(0, 1, "  \n ")]).ok)


class LineChecksTests(unittest.TestCase):
    def test_line_at_the_limit_passes_and_one_over_warns(self):
        self.assertTrue(sq.check_subtitles([Seg(0, 5, "x" * 42)]).ok)
        report = sq.check_subtitles([Seg(0, 5, "x" * 43)])
        self.assertEqual(codes(report), [sq.LINE_TOO_LONG])
        self.assertEqual((report.issues[0].value, report.issues[0].limit), (43, 42))
        self.assertEqual(report.issues[0].severity, sq.WARNING)

    def test_reports_the_longest_line_and_its_number(self):
        report = sq.check_subtitles([Seg(0, 9, "pendek\n" + "x" * 50)])
        self.assertEqual(codes(report), [sq.LINE_TOO_LONG])
        self.assertEqual(report.issues[0].value, 50)
        self.assertIn("Baris 2", report.issues[0].message)

    def test_one_issue_per_segment_even_if_several_lines_are_long(self):
        report = sq.check_subtitles([Seg(0, 9, "x" * 50 + "\n" + "y" * 50)])
        self.assertEqual(codes(report), [sq.LINE_TOO_LONG])

    def test_two_lines_are_fine_three_are_not(self):
        self.assertTrue(sq.check_subtitles([Seg(0, 5, "a\nb")]).ok)
        report = sq.check_subtitles([Seg(0, 5, "a\nb\nc")])
        self.assertEqual(codes(report), [sq.TOO_MANY_LINES])
        self.assertEqual((report.issues[0].value, report.issues[0].limit), (3, 2))

    def test_blank_lines_do_not_count_as_lines(self):
        self.assertTrue(sq.check_subtitles([Seg(0, 5, "a\n\n\nb")]).ok)

    def test_ass_line_break_counts_as_a_line(self):
        self.assertEqual(codes(sq.check_subtitles([Seg(0, 5, "a\\Nb\\Nc")])), [sq.TOO_MANY_LINES])

    def test_custom_line_rules(self):
        rules = sq.QualityRules(max_chars_per_line=10, max_lines=1)
        report = sq.check_subtitles([Seg(0, 9, "x" * 11 + "\nb")], rules)
        self.assertEqual(sorted(codes(report)), sorted([sq.LINE_TOO_LONG, sq.TOO_MANY_LINES]))

    def test_line_checks_also_apply_when_timing_is_invalid(self):
        report = sq.check_subtitles([Seg(5, 1, "x" * 60)])
        self.assertEqual(sorted(codes(report)), sorted([sq.INVALID_TIMING, sq.LINE_TOO_LONG]))


class OverlapTests(unittest.TestCase):
    def test_back_to_back_is_not_an_overlap(self):
        self.assertTrue(sq.check_subtitles([Seg(0, 1), Seg(1, 2), Seg(2, 3)]).ok)

    def test_gap_is_not_an_overlap(self):
        self.assertTrue(sq.check_subtitles([Seg(0, 1), Seg(1.5, 2)]).ok)

    def test_overlap_is_reported_on_the_later_segment(self):
        report = sq.check_subtitles([Seg(0, 2), Seg(1.5, 3)])
        self.assertEqual([(i.index, i.code) for i in report.issues], [(1, sq.OVERLAP)])
        issue = report.issues[0]
        self.assertEqual(issue.severity, sq.ERROR)
        self.assertEqual(issue.other_index, 0)
        self.assertAlmostEqual(issue.value, 0.5)
        self.assertIn("#1", issue.message)

    def test_float_noise_within_tolerance_is_ignored(self):
        self.assertTrue(sq.check_subtitles([Seg(0, 1.0004), Seg(1.0, 2)]).ok)
        self.assertEqual(codes(sq.check_subtitles([Seg(0, 1.002), Seg(1.0, 2)])), [sq.OVERLAP])

    def test_custom_tolerance(self):
        rules = sq.QualityRules(overlap_tolerance=0.25)
        self.assertTrue(sq.check_subtitles([Seg(0, 1.2), Seg(1.0, 2)], rules).ok)
        self.assertEqual(codes(sq.check_subtitles([Seg(0, 1.3), Seg(1.0, 2)], rules)), [sq.OVERLAP])

    def test_long_segment_swallowing_several_short_ones(self):
        segs = [Seg(0, 10), Seg(1, 2), Seg(3, 4), Seg(5, 6), Seg(11, 12)]
        report = sq.check_subtitles(segs)
        self.assertEqual([i.index for i in report.issues if i.code == sq.OVERLAP], [1, 2, 3])
        self.assertTrue(all(i.other_index == 0 for i in report.issues), "semuanya menabrak segmen panjang #0")

    def test_neighbour_only_comparison_would_miss_this(self):
        # #2 tidak menabrak #1 (tetangganya) tetapi menabrak #0 yang berakhir paling akhir.
        report = sq.check_subtitles([Seg(0, 10), Seg(1, 2), Seg(5, 6)])
        self.assertEqual([(i.index, i.other_index) for i in report.issues], [(1, 0), (2, 0)])

    def test_unsorted_input_is_handled_and_indexes_refer_to_the_input_order(self):
        segs = [Seg(5, 6), Seg(0, 2), Seg(1, 3)]                      # dua yang tumpang tindih ada di indeks 1 dan 2
        report = sq.check_subtitles(segs)
        self.assertEqual([(i.index, i.other_index) for i in report.issues], [(2, 1)])

    def test_identical_segments_overlap(self):
        report = sq.check_subtitles([Seg(1, 2), Seg(1, 2)])
        self.assertEqual(codes(report), [sq.OVERLAP])

    def test_invalid_segments_are_excluded_from_the_overlap_sweep(self):
        report = sq.check_subtitles([Seg(0, 1), Seg(0.5, 0.2), Seg(2, 3)])      # #1 waktunya terbalik
        self.assertEqual(codes(report), [sq.INVALID_TIMING])
        self.assertEqual(report.issues[0].index, 1)

    def test_single_and_empty_inputs(self):
        self.assertTrue(sq.check_subtitles([Seg(0, 1)]).ok)
        self.assertEqual(sq.check_subtitles([]).total, 0)


class InvalidTimingTests(unittest.TestCase):
    def test_bad_timings_are_reported_not_raised(self):
        bad = (Seg(2, 2), Seg(3, 1), Seg(math.nan, 1), Seg(0, math.nan), Seg(0, math.inf), Seg(-math.inf, 0),
               Seg(None, 1), Seg(0, None), Seg("abc", 1))                  # type: ignore[arg-type]
        for seg in bad:
            report = sq.check_subtitles([seg])
            self.assertEqual(codes(report), [sq.INVALID_TIMING], (seg.start, seg.end))
            self.assertEqual(report.issues[0].severity, sq.ERROR)

    def test_numeric_strings_are_rejected_since_v1917(self):
        # v1.9.16 menerima "0"/"2"; v1.9.17 menolaknya (waktu harus bilangan; konsisten dengan cps() dan QualityRules).
        report = sq.check_subtitles([Seg("0", "2", "halo")])               # type: ignore[arg-type]
        self.assertEqual(codes(report), [sq.INVALID_TIMING])

    def test_missing_text_attribute_does_not_crash(self):
        class NoText:
            start, end = 0.0, 1.0
        self.assertTrue(sq.check_subtitles([NoText()]).ok)                 # type: ignore[list-item]

    def test_none_text(self):
        self.assertTrue(sq.check_subtitles([Seg(0, 1, None)]).ok)          # type: ignore[arg-type]


class PurityAndOrderingTests(unittest.TestCase):
    def test_input_is_never_modified(self):
        segs = [Seg(0, 2, "x" * 50), Seg(1, 3, "a\nb\nc"), Seg(5, 4, "bad")]
        before = copy.deepcopy(segs)
        sq.check_subtitles(segs)
        self.assertEqual(segs, before)

    def test_accepts_any_iterable(self):
        report = sq.check_subtitles(s for s in [Seg(0, 1, "x" * 40)])
        self.assertEqual(report.total, 1)
        self.assertEqual(codes(report), [sq.CPS_VERY_HIGH])                # 40 karakter / 1 detik

    def test_issues_are_sorted_by_index_then_severity(self):
        segs = [Seg(0, 0.5, "x" * 50), Seg(0.2, 5, "a\nb\nc")]
        report = sq.check_subtitles(segs)
        keys = [(i.index, 0 if i.severity == sq.ERROR else 1) for i in report.issues]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual([i.code for i in report.issues if i.index == 0], [sq.CPS_VERY_HIGH, sq.LINE_TOO_LONG])

    def test_result_is_deterministic(self):
        rng = random.Random(7)
        segs = [Seg(rng.uniform(0, 100), rng.uniform(0, 100), "x" * rng.randint(0, 80)) for _ in range(300)]
        a = sq.check_subtitles(segs).issues
        b = sq.check_subtitles(list(reversed(list(reversed(segs))))).issues
        self.assertEqual(a, b)

    def test_message_texts_are_in_indonesian_and_mention_the_numbers(self):
        report = sq.check_subtitles([Seg(0, 1, "x" * 30), Seg(0.5, 2, "y" * 50)])
        text = " ".join(i.message for i in report.issues)
        for needle in ("karakter/detik", "Tumpang tindih", "terlalu panjang"):
            self.assertIn(needle, text)


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.segs = [Seg(0, 2, "Halo"), Seg(1, 3, "x" * 50), Seg(4, 3, "bad"), Seg(5, 6, "a\nb\nc")]
        self.report = sq.check_subtitles(self.segs)

    def test_counts_follow_all_codes_order(self):
        counts = self.report.counts()
        self.assertEqual(list(counts), [c for c in sq.ALL_CODES if c in counts])
        self.assertEqual(counts[sq.INVALID_TIMING], 1)
        self.assertEqual(counts[sq.OVERLAP], 1)
        self.assertEqual(counts[sq.TOO_MANY_LINES], 1)

    def test_errors_and_warnings_partition_the_issues(self):
        self.assertEqual(len(self.report.errors) + len(self.report.warnings), len(self.report.issues))
        self.assertTrue(all(i.severity == sq.ERROR for i in self.report.errors))
        self.assertTrue(all(i.severity == sq.WARNING for i in self.report.warnings))

    def test_by_index_groups_issues(self):
        grouped = self.report.by_index()
        self.assertEqual(sorted(grouped), [1, 2, 3])
        self.assertEqual(sum(len(v) for v in grouped.values()), len(self.report.issues))

    def test_summary_texts(self):
        self.assertIn("3 dari 4", self.report.summary())
        self.assertEqual(sq.check_subtitles([Seg(0, 1)]).summary(), "Semua 1 subtitle lolos pemeriksaan kualitas.")
        self.assertEqual(sq.check_subtitles([]).summary(), "Tidak ada subtitle untuk diperiksa.")

    def test_ok_flag(self):
        self.assertFalse(self.report.ok)
        self.assertTrue(sq.check_subtitles([Seg(0, 2, "halo")]).ok)


class AppModelCompatibilityTests(unittest.TestCase):
    def test_works_with_the_real_subtitle_segment(self):
        from kaptionpro.models import SubtitleSegment
        segs = [SubtitleSegment(0.0, 2.0, "Halo dunia"), SubtitleSegment(1.0, 2.5, "x" * 60)]
        report = sq.check_subtitles(segs)
        # segmen 1: 60 karakter / 1,5 detik = 40 CPS (error), baris 60 > 42, dan menimpa segmen 0 selama 1 detik
        self.assertEqual(sorted(codes(report)), sorted([sq.OVERLAP, sq.CPS_VERY_HIGH, sq.LINE_TOO_LONG]))
        self.assertEqual(segs[1].text, "x" * 60)

    def test_module_does_not_import_ui_or_models(self):
        src = (ROOT / "kaptionpro" / "core" / "subtitle_quality.py").read_text(encoding="utf-8")
        for forbidden in ("import tkinter", "customtkinter", "from ..models", "from .. import", "kaptionpro.ui"):
            self.assertNotIn(forbidden, src)


class PerformanceTests(unittest.TestCase):
    """Batas sengaja longgar (runner CI lambat); yang dijaga adalah bentuknya: tidak kuadratik dan tidak meledak."""

    @staticmethod
    def _movie(count: int, seconds_each: float = 3.0, text: str = "Ini contoh kalimat subtitle yang wajar.") -> list[Seg]:
        return [Seg(i * seconds_each, i * seconds_each + seconds_each - 0.1, text) for i in range(count)]

    def test_1000_subtitles(self):
        segs = self._movie(1000)
        t0 = time.perf_counter()
        report = sq.check_subtitles(segs)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertTrue(report.ok, report.summary())
        self.assertEqual(report.total, 1000)

    def test_two_hour_video_worth_of_subtitles(self):
        segs = self._movie(2400)                                          # 2 jam = 7200 s, ~2400 baris x 3 s
        self.assertGreaterEqual(segs[-1].end, 7100)
        t0 = time.perf_counter()
        report = sq.check_subtitles(segs)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertTrue(report.ok, report.summary())

    def test_worst_case_every_segment_overlaps_and_breaks_every_rule(self):
        segs = [Seg(i * 0.1, i * 0.1 + 5.0, "x" * 60 + "\n" + "y" * 60 + "\nz") for i in range(5000)]
        t0 = time.perf_counter()
        report = sq.check_subtitles(segs)
        self.assertLess(time.perf_counter() - t0, 4.0)
        self.assertEqual(report.counts()[sq.OVERLAP], 4999)               # semua kecuali yang pertama
        self.assertEqual(report.counts()[sq.LINE_TOO_LONG], 5000)
        self.assertEqual(report.counts()[sq.TOO_MANY_LINES], 5000)

    def test_scaling_is_not_quadratic(self):
        small, large = self._movie(2000), self._movie(16000)             # 8x lebih banyak
        t0 = time.perf_counter()
        sq.check_subtitles(small)
        t_small = max(time.perf_counter() - t0, 1e-4)
        t0 = time.perf_counter()
        sq.check_subtitles(large)
        t_large = time.perf_counter() - t0
        # linear ~8x; kuadratik ~64x. Batas 30x menyerap noise runner tanpa meloloskan O(n^2).
        self.assertLess(t_large / t_small, 30, f"{t_small:.4f}s -> {t_large:.4f}s")

    def test_shuffled_input_gives_the_same_overlaps(self):
        segs = self._movie(500)
        segs[100] = Seg(segs[100].start, segs[101].end + 1)               # satu tumpukan buatan
        rng = random.Random(3)
        order = list(range(len(segs)))
        rng.shuffle(order)
        shuffled = [segs[i] for i in order]
        got = {(order[i.index], order[i.other_index]) for i in sq.check_subtitles(shuffled).issues if i.code == sq.OVERLAP}
        want = {(i.index, i.other_index) for i in sq.check_subtitles(segs).issues if i.code == sq.OVERLAP}
        self.assertEqual(got, want)
        self.assertTrue(want)


if __name__ == "__main__":
    unittest.main()
