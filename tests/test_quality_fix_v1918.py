"""
Tes v1.9.18 (pure Python, tanpa GUI):
  * P0: QualityRules menolak angka ekstrem dengan ValueError (bukan OverflowError);
  * core/quality_fix.py: bungkus teks dan perencana perbaikan (rencana per baris, diverifikasi);
  * core/quality_flow.py: Analyze -> Preview -> Confirm -> Apply -> Undo dijaga urutannya dan tidak pernah memakai rencana basi.
"""

from __future__ import annotations

import copy
import math
import random
import sys
import time
import unittest
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kaptionpro.core import quality_fix as qf  # noqa: E402
from kaptionpro.core import quality_flow as flow  # noqa: E402
from kaptionpro.core import subtitle_quality as sq  # noqa: E402


@dataclass
class Seg:
    start: object
    end: object
    text: str = "Halo"


def make(*triples) -> list[Seg]:
    return [Seg(*t) for t in triples]


def keys(report: sq.QualityReport) -> set[tuple[int, str]]:
    return {(i.index, i.code) for i in report.issues}


# ===================================================================================================== P0
class ExtremeRuleValueTests(unittest.TestCase):
    HUGE = 10 ** 10000

    def test_the_reported_case_raises_valueerror_not_overflowerror(self):
        with self.assertRaises(ValueError):
            sq.QualityRules(max_cps=self.HUGE)

    def test_every_float_rule_rejects_huge_ints_fractions_and_infinities(self):
        for name in ("max_cps", "hard_max_cps", "overlap_tolerance", "min_duration", "max_duration", "min_gap"):
            for value in (self.HUGE, -self.HUGE, Fraction(self.HUGE, 1), 10 ** 400, math.inf, -math.inf, math.nan, 1e400):
                with self.assertRaises(ValueError, msg=f"{name}={sq._safe_repr(value)}"):
                    sq.QualityRules(**{name: value})

    def test_integer_rules_are_capped(self):
        for name in ("max_chars_per_line", "max_lines"):
            for value in (self.HUGE, -self.HUGE, 0, -1, sq.MAX_RULE_INT + 1):
                with self.assertRaises(ValueError, msg=f"{name}={sq._safe_repr(value)}"):
                    sq.QualityRules(**{name: value})
            sq.QualityRules(**{name: sq.MAX_RULE_INT})
            sq.QualityRules(**{name: 1})

    def test_error_messages_never_contain_the_giant_number(self):
        for kwargs in (dict(max_cps=self.HUGE), dict(max_lines=self.HUGE), dict(min_gap=self.HUGE),
                       dict(hard_max_cps=-self.HUGE)):
            with self.assertRaises(ValueError) as ctx:
                sq.QualityRules(**kwargs)
            self.assertLess(len(str(ctx.exception)), 200, str(ctx.exception)[:80])

    def test_safe_repr(self):
        self.assertEqual(sq._safe_repr(12), "12")
        self.assertLessEqual(len(sq._safe_repr(self.HUGE)), 40)
        self.assertLessEqual(len(sq._safe_repr("x" * 500)), 40)

    def test_only_overflow_candidates_are_rejected_large_but_finite_floats_stay_valid(self):
        sq.QualityRules(max_cps=1e300, hard_max_cps=1e301)

    def test_valid_extreme_but_sane_values_still_work(self):
        rules = sq.QualityRules(max_cps=Fraction(35, 2), hard_max_cps=25, max_chars_per_line=10_000, max_lines=3)
        self.assertTrue(sq.check_subtitles([Seg(0, 2, "halo")], rules).ok)


# ===================================================================================================== wrap_text
class WrapTextTests(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(qf.wrap_text("Halo dunia", 42, 2), "Halo dunia")

    def test_existing_line_breaks_are_respected(self):
        self.assertEqual(qf.wrap_text("Baris satu\nBaris dua", 42, 2), "Baris satu\nBaris dua")

    def test_long_line_is_wrapped_into_balanced_lines(self):
        text = "Ini kalimat yang sangat panjang sekali sehingga tidak muat di satu baris subtitle"
        out = qf.wrap_text(text, 42, 2)
        lines = out.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(x) <= 42 for x in lines))
        self.assertLessEqual(abs(len(lines[0]) - len(lines[1])), 12, "harus seimbang, bukan 42 + sisa")
        self.assertEqual(" ".join(lines), text)

    def test_words_are_never_split_or_lost(self):
        text = " ".join(f"kata{i}" for i in range(14))
        out = qf.wrap_text(text, 30, 3)
        self.assertEqual(out.split(), text.split())

    def test_too_many_lines_are_merged_when_possible(self):
        self.assertEqual(qf.wrap_text("a\nb\nc", 42, 2), "a b c")
        self.assertEqual(qf.wrap_text("a\nb", 42, 1), "a b")

    def test_dialogue_lines_are_not_merged(self):
        self.assertIsNone(qf.wrap_text("- Halo\n- Hai\n- Apa kabar", 42, 2))
        self.assertEqual(qf.wrap_text("- Halo\n- Apa kabar", 42, 2), "- Halo\n- Apa kabar")

    def test_unsafe_cases_return_none(self):
        self.assertIsNone(qf.wrap_text("x" * 50, 42, 2), "satu kata lebih panjang dari baris")
        self.assertIsNone(qf.wrap_text("<i>halo</i> dunia", 42, 2), "tag HTML")
        self.assertIsNone(qf.wrap_text("{\\an8}halo", 42, 2), "tag ASS")
        self.assertIsNone(qf.wrap_text(" ".join(["kata"] * 40), 20, 2), "tidak muat dalam 2 baris")

    def test_empty_and_none(self):
        self.assertEqual(qf.wrap_text("", 42, 2), "")
        self.assertEqual(qf.wrap_text(None, 42, 2), "")
        self.assertEqual(qf.wrap_text("  \n \n ", 42, 2), "")

    def test_ass_breaks_are_understood(self):
        self.assertEqual(qf.wrap_text("satu\\Ndua", 42, 2), "satu\ndua")

    def test_idempotent(self):
        text = "Ini kalimat yang sangat panjang sekali sehingga tidak muat di satu baris subtitle"
        once = qf.wrap_text(text, 42, 2)
        self.assertEqual(qf.wrap_text(once, 42, 2), once)

    def test_width_boundaries(self):
        self.assertEqual(qf.wrap_text("x" * 42, 42, 1), "x" * 42)
        two = "a" * 20 + " " + "b" * 21
        self.assertEqual(qf.wrap_text(two, 42, 2), two)                 # persis 42: satu baris
        self.assertEqual(qf.wrap_text(two + "c", 42, 2).count("\n"), 1)


# ===================================================================================================== plan_fixes
class PlanBasicsTests(unittest.TestCase):
    def test_clean_input_has_no_changes(self):
        plan = qf.plan_fixes(make((0, 2, "Halo dunia"), (3, 5, "Apa kabar")))
        self.assertFalse(plan.has_changes)
        self.assertTrue(plan.is_safe)
        self.assertEqual(plan.remaining, [])
        self.assertIn("Tidak ada yang bisa diperbaiki", plan.summary())

    def test_empty_input(self):
        plan = qf.plan_fixes([])
        self.assertFalse(plan.has_changes)
        self.assertEqual(plan.source, ())

    def test_input_is_never_modified(self):
        segs = make((0, 2.5, "Halo dunia"), (2, 4, "x " * 60), (4, 4.2, "Cepat sekali dibaca ya"))
        before = copy.deepcopy(segs)
        qf.plan_fixes(segs)
        self.assertEqual(segs, before)

    def test_matches_detects_any_change(self):
        segs = make((0, 2.5, "Halo"), (2, 4, "Dunia"))
        plan = qf.plan_fixes(segs)
        self.assertTrue(plan.matches(segs))
        self.assertTrue(plan.matches(copy.deepcopy(segs)))
        for mutate in (lambda s: setattr(s[0], "text", "X"), lambda s: setattr(s[1], "end", 9), lambda s: s.pop(),
                       lambda s: s.append(Seg(9, 10))):
            other = copy.deepcopy(segs)
            mutate(other)
            self.assertFalse(plan.matches(other))

    def test_untouched_rows_keep_their_original_objects_and_types(self):
        plan = qf.plan_fixes(make((0, 2, "Halo"), (3, 5, "x" * 50 + " y"), (7, 9, "Selesai")))
        self.assertEqual(plan.rows_after[0], qf.Row(0, 2, "Halo"))
        self.assertIsInstance(plan.rows_after[0].start, int)                   # tidak diubah jadi float
        self.assertEqual(len(plan.rows_after), 3)


class PlanTextTests(unittest.TestCase):
    def test_long_line_is_wrapped(self):
        text = "Ini kalimat yang sangat panjang sekali sehingga tidak muat di satu baris subtitle"
        plan = qf.plan_fixes(make((0, 6, text)))
        (change,) = plan.changes
        self.assertEqual((change.index, change.text_changed, change.timing_changed), (0, True, False))
        self.assertEqual(change.reasons, (sq.LINE_TOO_LONG,))
        self.assertEqual(change.after.text.split(), text.split())
        self.assertEqual((change.after.start, change.after.end), (0, 6))
        self.assertTrue(plan.after.ok, plan.after.summary())
        self.assertEqual(plan.resolved, [(0, sq.LINE_TOO_LONG)])

    def test_too_many_lines_are_merged(self):
        plan = qf.plan_fixes(make((0, 5, "a\nb\nc")))
        self.assertEqual(plan.changes[0].after.text, "a b c")
        self.assertEqual(plan.changes[0].reasons, (sq.TOO_MANY_LINES,))

    def test_unwrappable_text_is_reported_not_touched(self):
        plan = qf.plan_fixes(make((0, 6, "x" * 60)))
        self.assertFalse(plan.has_changes)
        (rem,) = plan.remaining
        self.assertEqual(rem.code, sq.LINE_TOO_LONG)
        self.assertIn("edit manual", rem.reason)

    def test_tagged_text_is_not_rewrapped(self):
        plan = qf.plan_fixes(make((0, 9, "<i>" + "kata " * 20 + "</i>")))
        self.assertFalse(plan.has_changes)
        self.assertEqual(plan.remaining[0].code, sq.LINE_TOO_LONG)

    def test_only_whitespace_changes_in_text(self):
        text = "satu dua tiga empat lima enam tujuh delapan sembilan sepuluh sebelas dua belas"
        plan = qf.plan_fixes(make((0, 9, text)))
        self.assertEqual("".join(plan.changes[0].after.text.split()), "".join(text.split()))


class PlanOverlapTests(unittest.TestCase):
    def test_overlap_trims_the_previous_end(self):
        plan = qf.plan_fixes(make((0, 2.5, "Halo dunia"), (2, 4, "Apa kabar")))
        (change,) = plan.changes
        self.assertEqual(change.index, 0)
        self.assertEqual(change.after.end, 2.0)
        self.assertEqual(change.reasons, (sq.OVERLAP,))
        self.assertTrue(change.timing_changed and not change.text_changed)
        self.assertIn("dipendekkan", change.notes[0])
        self.assertEqual(plan.resolved, [(1, sq.OVERLAP)])
        self.assertTrue(plan.after.ok)

    def test_trim_keeps_the_requested_minimum_gap(self):
        rules = sq.QualityRules(min_gap=0.083)
        plan = qf.plan_fixes(make((0, 2.5, "Halo dunia"), (2, 4, "Apa kabar")), rules)
        self.assertAlmostEqual(plan.changes[0].after.end, 1.917, places=3)
        self.assertTrue(plan.after.ok, plan.after.summary())

    def test_tiny_gap_with_min_gap_is_fixed(self):
        rules = sq.QualityRules(min_gap=0.083)
        plan = qf.plan_fixes(make((0, 2, "Halo dunia"), (2, 4, "Apa kabar")), rules)
        self.assertEqual(plan.changes[0].reasons, (sq.GAP_TOO_SMALL,))
        self.assertTrue(plan.after.ok)

    def test_when_trimming_would_be_too_short_the_next_start_is_pushed(self):
        plan = qf.plan_fixes(make((0, 5, "Halo dunia"), (0.1, 6, "Apa kabar")))
        (change,) = plan.changes
        self.assertEqual(change.index, 1)
        self.assertEqual(change.after.start, 5.0)
        self.assertIn("digeser", change.notes[0])
        self.assertTrue(plan.after.ok, plan.after.summary())

    def test_when_neither_fits_it_is_reported(self):
        plan = qf.plan_fixes(make((0, 0.5, "Halo"), (0.1, 0.5, "Dunia")))
        self.assertFalse(plan.has_changes)
        (rem,) = plan.remaining
        self.assertEqual((rem.index, rem.code), (1, sq.OVERLAP))
        self.assertIn("lebih singkat", rem.reason)

    def test_min_duration_is_the_floor_when_set(self):
        rules = sq.QualityRules(min_duration=1.5)
        plan = qf.plan_fixes(make((0, 3, "Halo dunia"), (1.0, 6, "Apa kabar")), rules)
        self.assertEqual(plan.changes[0].index, 1, "memangkas baris 0 ke 1.0 s < 1.5 s tidak boleh; geser awal baris 1")
        self.assertEqual(plan.changes[0].after.start, 3.0)

    def test_min_duration_blocks_the_shift_too_when_it_would_leave_the_next_row_too_short(self):
        rules = sq.QualityRules(min_duration=1.5)
        plan = qf.plan_fixes(make((0, 3, "Halo dunia"), (1.0, 4, "Apa kabar")), rules)      # geser -> 1.0 s < 1.5 s
        self.assertFalse(plan.has_changes)
        self.assertEqual((plan.remaining[0].index, plan.remaining[0].code), (1, sq.OVERLAP))

    def test_long_segment_swallowing_short_ones(self):
        segs = make((0, 10, "Satu"), (1, 2, "Dua"), (3, 4, "Tiga"), (5, 6, "Empat"))
        plan = qf.plan_fixes(segs)
        self.assertEqual([c.index for c in plan.changes], [0])
        self.assertEqual(plan.changes[0].after.end, 1.0)
        self.assertTrue(plan.after.ok, plan.after.summary())

    def test_unsorted_input_reports_input_indexes(self):
        plan = qf.plan_fixes(make((5, 6, "Z"), (0, 2.5, "A"), (2, 4, "B")))
        self.assertEqual([c.index for c in plan.changes], [1])
        self.assertEqual(plan.rows_after[1].end, 2.0)


class PlanExtensionTests(unittest.TestCase):
    def test_fast_cps_is_fixed_by_extending_into_free_time(self):
        plan = qf.plan_fixes(make((0, 1, "x" * 20), (10, 12, "Selesai")))
        (change,) = plan.changes
        self.assertEqual((change.index, change.reasons), (0, (sq.CPS_HIGH,)))
        self.assertGreater(change.after.end, 20 / 17)
        self.assertIn("diperpanjang", change.notes[0])
        self.assertTrue(plan.after.ok, plan.after.summary())
        self.assertLessEqual(sq.cps(change.after.text, change.after.end - change.after.start), 17.0)

    def test_extension_stops_before_the_next_subtitle(self):
        plan = qf.plan_fixes(make((0, 1, "x" * 18), (1.1, 3, "Selesai")))        # butuh 18/17 = 1.059 s: muat sebelum 1.1
        self.assertTrue(plan.has_changes)
        self.assertLessEqual(plan.changes[0].after.end, 1.1 + 1e-9)
        self.assertTrue(plan.after.ok, plan.after.summary())

    def test_no_room_means_no_partial_extension(self):
        plan = qf.plan_fixes(make((0, 1, "x" * 20), (1.0, 3, "Selesai")))
        self.assertFalse(plan.has_changes, "perpanjangan setengah-setengah tidak boleh")
        self.assertEqual(plan.remaining[0].code, sq.CPS_HIGH)
        self.assertIn("ruang", plan.remaining[0].reason)

    def test_min_gap_is_kept_when_extending(self):
        rules = sq.QualityRules(min_gap=0.5)
        plan = qf.plan_fixes(make((0, 1, "x" * 20), (3, 5, "Selesai")), rules)
        self.assertLessEqual(plan.changes[0].after.end, 3 - 0.5 + 1e-9)
        self.assertTrue(plan.after.ok, plan.after.summary())

    def test_min_duration_is_fixed_by_extending(self):
        rules = sq.QualityRules(min_duration=1.0)
        plan = qf.plan_fixes(make((0, 0.4, ""), (5, 6, "Selesai")), rules)
        self.assertEqual(plan.changes[0].reasons, (sq.DURATION_TOO_SHORT,))
        self.assertGreaterEqual(plan.changes[0].after.end, 1.0)
        self.assertTrue(plan.after.ok)

    def test_max_duration_caps_the_extension(self):
        rules = sq.QualityRules(max_duration=1.1)
        plan = qf.plan_fixes(make((0, 1, "x" * 20), (30, 31, "Selesai")), rules)
        self.assertFalse(plan.has_changes)
        self.assertIn("maksimum", plan.remaining[0].reason)

    def test_last_segment_can_extend_freely(self):
        plan = qf.plan_fixes(make((0, 1, "x" * 20)))
        self.assertTrue(plan.has_changes and plan.after.ok)

    def test_extension_never_creates_an_overlap(self):
        rng = random.Random(11)
        for _ in range(200):
            starts = sorted(rng.uniform(0, 60) for _ in range(8))
            segs = [Seg(s, s + rng.uniform(0.2, 2.0), "x" * rng.randint(1, 45)) for s in starts]
            plan = qf.plan_fixes(segs)
            new_overlaps = {k for k in plan.introduced if k[1] == sq.OVERLAP}
            self.assertFalse(new_overlaps, new_overlaps)


class PlanUnfixableTests(unittest.TestCase):
    def test_invalid_timing_is_left_alone_and_reported(self):
        segs = make((5, 4, "Salah"), (0, 2, "Halo"), (True, 3, "Bool"))
        plan = qf.plan_fixes(segs)
        self.assertFalse(plan.has_changes)
        self.assertEqual({r.index for r in plan.remaining}, {0, 2})
        self.assertTrue(all("tidak valid" in r.reason for r in plan.remaining))

    def test_invalid_rows_do_not_block_fixing_valid_ones(self):
        plan = qf.plan_fixes(make((None, None, "Rusak"), (0, 2.5, "Halo"), (2, 4, "Dunia")))
        self.assertEqual([c.index for c in plan.changes], [1])

    def test_too_long_duration_is_reported(self):
        rules = sq.QualityRules(max_duration=7.0)
        plan = qf.plan_fixes(make((0, 20, "Halo")), rules)
        self.assertFalse(plan.has_changes)
        self.assertEqual(plan.remaining[0].code, sq.DURATION_TOO_LONG)
        self.assertIn("pecah", plan.remaining[0].reason)

    def test_introduced_issues_are_flagged(self):
        # memangkas baris 0 demi menghilangkan overlap menaikkan CPS-nya: harus terlihat sebagai masalah BARU
        segs = make((0, 2, "x" * 30), (1, 3, "Dunia"))
        plan = qf.plan_fixes(segs)
        self.assertIn((0, sq.CPS_VERY_HIGH), plan.introduced)
        self.assertFalse(plan.is_safe)
        self.assertIn("BARU", plan.summary())
        self.assertIn((1, sq.OVERLAP), plan.resolved)


class PlanInvariantTests(unittest.TestCase):
    def test_random_inputs_keep_every_invariant(self):
        rng = random.Random(2024)
        words = ["halo", "dunia", "apa", "kabar", "selamat", "pagi", "semuanya", "terima", "kasih", "banyak"]
        for _ in range(300):
            n = rng.randint(0, 12)
            segs = []
            for _i in range(n):
                s = rng.uniform(0, 40)
                e = s + rng.uniform(-0.5, 8.0)
                text = "\n".join(" ".join(rng.choice(words) for _ in range(rng.randint(0, 14)))
                                 for _ in range(rng.randint(1, 4)))
                segs.append(Seg(s, e, text))
            snapshot = copy.deepcopy(segs)
            plan = qf.plan_fixes(segs)
            self.assertEqual(segs, snapshot, "masukan tidak boleh berubah")
            self.assertEqual(len(plan.rows_after), n)
            self.assertTrue(plan.matches(segs))
            for ch in plan.changes:
                self.assertEqual("".join(ch.before.text.split()), "".join(ch.after.text.split()), "kata tidak boleh berubah")
                if ch.timing_changed:
                    self.assertLess(ch.after.start, ch.after.end)
                    for field_name in ("start", "end"):
                        old, new = getattr(ch.before, field_name), getattr(ch.after, field_name)
                        if new != old:                                   # kolom yang diubah dibulatkan ke milidetik
                            self.assertAlmostEqual(new, round(new, 3), places=9, msg=field_name)
                self.assertTrue(ch.reasons or ch.notes)
                self.assertFalse(ch.before == ch.after)
            invalid_before = {i.index for i in plan.before.issues if i.code == sq.INVALID_TIMING}
            self.assertEqual(invalid_before, {i.index for i in plan.after.issues if i.code == sq.INVALID_TIMING})
            self.assertEqual(set(plan.resolved) | (keys(plan.before) & keys(plan.after)), keys(plan.before))
            self.assertEqual(set(plan.introduced), keys(plan.after) - keys(plan.before))
            self.assertEqual(len(plan.remaining), len(plan.after.issues))

    def test_plan_is_deterministic(self):
        rng = random.Random(5)
        segs = [Seg(rng.uniform(0, 50), rng.uniform(0, 50), "x" * rng.randint(0, 80)) for _ in range(100)]
        a, b = qf.plan_fixes(segs), qf.plan_fixes(list(segs))
        self.assertEqual(a.changes, b.changes)
        self.assertEqual(a.remaining, b.remaining)

    def test_fixable_sample_is_idempotent(self):
        segs = make((0, 2.5, "Halo dunia"), (2, 4, "Ini kalimat yang sangat panjang sekali sehingga tidak muat di satu baris"),
                    (8, 9, "a\nb\nc"), (20, 20.5, "x" * 12))
        plan = qf.plan_fixes(segs)
        again = qf.plan_fixes([Seg(r.start, r.end, r.text) for r in plan.rows_after])
        self.assertFalse(again.has_changes, again.summary())

    def test_performance_two_hour_video(self):
        text = ("abcdef " * 7).strip()                         # 48 karakter: perlu dibungkus; CPS aman setelah dipangkas
        segs = [Seg(i * 3.0, i * 3.0 + 3.2, text) for i in range(2400)]
        t0 = time.perf_counter()
        plan = qf.plan_fixes(segs, sq.QualityRules(min_gap=0.083))
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertTrue(plan.has_changes)
        self.assertTrue(plan.after.ok, plan.after.summary())


# ===================================================================================================== flow
class FakeEditor:
    """Meniru kontrak editor: apply = satu langkah undo; undo mengembalikan daftar sebelumnya."""

    def __init__(self, segs):
        self.segments = segs
        self.undo_stack: list[list[Seg]] = []
        self.applied = 0

    def apply(self, plan):
        self.undo_stack.append(copy.deepcopy(self.segments))
        self.segments = [Seg(r.start, r.end, r.text) for r in plan.rows_after]
        self.applied += 1

    def undo(self):
        if self.undo_stack:
            self.segments = self.undo_stack.pop()


def fixable() -> list[Seg]:
    return make((0, 2.5, "Halo dunia"), (2, 4, "Apa kabar"), (8, 9, "a\nb\nc"))


class FlowOrderTests(unittest.TestCase):
    def test_happy_path_in_order(self):
        ed = FakeEditor(fixable())
        original = copy.deepcopy(ed.segments)
        f = flow.QualityFlow()
        self.assertEqual(f.stage, flow.IDLE)
        report = f.analyze(ed.segments)
        self.assertFalse(report.ok)
        plan = f.preview(ed.segments)
        self.assertTrue(plan.has_changes)
        self.assertEqual(ed.segments, original, "pratinjau TIDAK boleh mengubah apa pun")
        f.confirm()
        self.assertEqual(ed.segments, original, "konfirmasi saja belum mengubah apa pun")
        count = f.apply(ed.segments, ed.apply)
        self.assertEqual(count, len(plan.changes))
        self.assertNotEqual(ed.segments, original)
        self.assertTrue(f.can_undo)
        self.assertTrue(f.undo(lambda: ed.segments, ed.undo))
        self.assertEqual(ed.segments, original, "Undo mengembalikan PERSIS")
        self.assertEqual(f.history, [flow.IDLE, flow.ANALYZED, flow.PREVIEW, flow.CONFIRMED, flow.APPLIED, flow.UNDONE])

    def test_applied_result_passes_the_checker(self):
        ed = FakeEditor(fixable())
        f = flow.QualityFlow()
        f.analyze(ed.segments)
        f.preview(ed.segments)
        f.confirm()
        f.apply(ed.segments, ed.apply)
        self.assertTrue(sq.check_subtitles(ed.segments).ok)

    def test_steps_out_of_order_are_refused(self):
        ed = FakeEditor(fixable())
        f = flow.QualityFlow()
        with self.assertRaises(flow.FlowError):
            f.preview(ed.segments)                            # sebelum analyze
        with self.assertRaises(flow.FlowError):
            f.confirm()
        with self.assertRaises(flow.FlowError):
            f.apply(ed.segments, ed.apply)
        with self.assertRaises(flow.FlowError):
            f.undo(lambda: ed.segments, ed.undo)
        f.analyze(ed.segments)
        with self.assertRaises(flow.FlowError):
            f.confirm()                                       # belum preview
        with self.assertRaises(flow.FlowError):
            f.apply(ed.segments, ed.apply)                    # belum confirm
        f.preview(ed.segments)
        with self.assertRaises(flow.FlowError):
            f.apply(ed.segments, ed.apply)                    # preview saja TIDAK cukup
        self.assertEqual(ed.applied, 0)

    def test_confirmation_is_single_use(self):
        ed = FakeEditor(fixable())
        f = flow.QualityFlow()
        f.analyze(ed.segments)
        f.preview(ed.segments)
        f.confirm()
        f.apply(ed.segments, ed.apply)
        with self.assertRaises(flow.FlowError):
            f.apply(ed.segments, ed.apply)
        self.assertEqual(ed.applied, 1)

    def test_nothing_to_fix_cannot_be_confirmed(self):
        ed = FakeEditor(make((0, 2, "Halo"), (3, 5, "Dunia")))
        f = flow.QualityFlow()
        f.analyze(ed.segments)
        plan = f.preview(ed.segments)
        self.assertFalse(plan.has_changes)
        self.assertEqual(f.stage, flow.ANALYZED)
        with self.assertRaises(flow.FlowError):
            f.confirm()

    def test_stale_plan_is_refused(self):
        ed = FakeEditor(fixable())
        f = flow.QualityFlow()
        f.analyze(ed.segments)
        f.preview(ed.segments)
        f.confirm()
        ed.segments[0].text = "Diedit setelah pratinjau"
        with self.assertRaises(flow.StalePlanError):
            f.apply(ed.segments, ed.apply)
        self.assertEqual(ed.applied, 0)
        self.assertEqual(f.stage, flow.ANALYZED)
        self.assertEqual(ed.segments[0].text, "Diedit setelah pratinjau")

    def test_failing_apply_returns_to_analyzed_and_reraises(self):
        ed = FakeEditor(fixable())
        f = flow.QualityFlow()
        f.analyze(ed.segments)
        f.preview(ed.segments)
        f.confirm()

        def boom(_plan):
            raise OSError("disk penuh")
        with self.assertRaises(OSError):
            f.apply(ed.segments, boom)
        self.assertEqual(f.stage, flow.ANALYZED)
        self.assertFalse(f.can_undo)

    def test_cancel_preview(self):
        ed = FakeEditor(fixable())
        f = flow.QualityFlow()
        f.analyze(ed.segments)
        f.preview(ed.segments)
        f.cancel_preview()
        self.assertEqual(f.stage, flow.ANALYZED)
        self.assertIsNone(f.plan)
        f.preview(ed.segments)
        f.confirm()
        f.cancel_preview()
        self.assertEqual(f.stage, flow.ANALYZED)
        with self.assertRaises(flow.FlowError):
            f.cancel_preview()

    def test_analyze_again_resets_everything(self):
        ed = FakeEditor(fixable())
        f = flow.QualityFlow()
        f.analyze(ed.segments)
        f.preview(ed.segments)
        f.confirm()
        f.analyze(ed.segments)
        self.assertEqual(f.stage, flow.ANALYZED)
        self.assertIsNone(f.plan)
        with self.assertRaises(flow.FlowError):
            f.apply(ed.segments, ed.apply)

    def test_undo_that_does_not_restore_reports_false(self):
        ed = FakeEditor(fixable())
        f = flow.QualityFlow()
        f.analyze(ed.segments)
        f.preview(ed.segments)
        f.confirm()
        f.apply(ed.segments, ed.apply)
        self.assertFalse(f.undo(lambda: ed.segments, lambda: None), "undo_fn yang tidak berbuat apa-apa harus terdeteksi")
        self.assertEqual(f.stage, flow.ANALYZED)
        self.assertFalse(f.can_undo)

    def test_undo_twice_is_refused(self):
        ed = FakeEditor(fixable())
        f = flow.QualityFlow()
        f.analyze(ed.segments)
        f.preview(ed.segments)
        f.confirm()
        f.apply(ed.segments, ed.apply)
        f.undo(lambda: ed.segments, ed.undo)
        with self.assertRaises(flow.FlowError):
            f.undo(lambda: ed.segments, ed.undo)

    def test_custom_rules_are_used_by_preview(self):
        ed = FakeEditor(make((0, 2, "Halo"), (2, 4, "Dunia")))
        f = flow.QualityFlow()
        f.analyze(ed.segments)
        self.assertFalse(f.preview(ed.segments).has_changes)
        f.analyze(ed.segments, sq.QualityRules(min_gap=0.083))
        self.assertTrue(f.preview(ed.segments).has_changes)

    def test_pure_modules_do_not_import_the_ui(self):
        for name in ("quality_fix.py", "quality_flow.py"):
            src = (ROOT / "kaptionpro" / "core" / name).read_text(encoding="utf-8")
            for forbidden in ("tkinter", "customtkinter", "kaptionpro.ui", "from ..models", "from ..ui", "from ..config"):
                self.assertNotIn(forbidden, src, f"{name} harus tetap pure Python")


if __name__ == "__main__":
    unittest.main()
