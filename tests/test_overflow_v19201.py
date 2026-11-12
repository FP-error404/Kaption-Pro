"""
Tes v1.9.20.1: integer ekstrem pada timestamp (TANPA Tk).

float(10**400) dan math.isfinite(10**400) melempar OverflowError, BUKAN ValueError, sehingga blok `except (TypeError, ValueError)`
tidak menangkapnya. Yang dikunci di sini:
  1. word_integrity / models / storage / utils menangani int raksasa di word.start, word.end, dan batas baris;
  2. Word.from_dict defensif (data rusak -> None) dan SubtitleSegment.from_dict mengosongkan words baris itu;
     pemuat project membuang baris yang waktunya rusak, tidak crash;
  3. Workspace memberi tahu pengguna bila retry sinkronisasi tampilan gagal DIJADWALKAN (bukan hanya log).
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
from test_quality_dialog_v1918 import FAKE_HEADER, run_script  # noqa: E402

BIG = 10 ** 400              # muat di json.loads, tak muat di float
HUGE = 10 ** 10000           # lebih dari 4300 digit: str()/repr() melempar ValueError di Python 3.11+


class BigNumberTests(unittest.TestCase):
    def setUp(self) -> None:
        from kaptionpro import models
        from kaptionpro.core import utils
        from kaptionpro.core import word_integrity as wi
        from kaptionpro.core.storage import ProjectStore
        self.m, self.u, self.wi, self.store = models, utils, wi, ProjectStore
        self.Word, self.Seg = models.Word, models.SubtitleSegment

    # ------------------------------------------------------------------ Word.from_dict
    def test_word_from_dict_valid_is_unchanged(self):
        word = self.Word.from_dict({"start": 1, "end": "2.5", "text": "halo"})
        self.assertEqual((word.start, word.end, word.text), (1.0, 2.5, "halo"))

    def test_word_from_dict_returns_none_and_never_raises(self):
        cases = [
            {"start": BIG, "end": 1, "text": "a"}, {"start": 0, "end": BIG, "text": "a"},
            {"start": HUGE, "end": 1, "text": "a"}, {"start": 0, "end": -HUGE, "text": "a"},
            {"start": 0, "end": 1, "text": HUGE},                       # str(10**10000) -> ValueError
            {"start": float("nan"), "end": 1, "text": "a"}, {"start": 0, "end": float("inf"), "text": "a"},
            {"start": "abc", "end": 1, "text": "a"}, {"start": None, "end": 1, "text": "a"},
            {"end": 1, "text": "a"}, {"start": 0, "text": "a"}, {"start": 0, "end": 1},
            None, "kata", 5, [], [1, 2, 3], {},
        ]
        for number, case in enumerate(cases):
            with self.subTest(case=number):                 # label angka: str(10**10000) sendiri melempar di Python 3.11+
                self.assertIsNone(self.Word.from_dict(case))

    # ------------------------------------------------------------------ SubtitleSegment.from_dict
    def seg_dict(self, words, **kw):
        return {"start": 0, "end": 2, "text": "a b", "words": words, **kw}

    def test_segment_with_one_corrupt_word_loses_all_words_but_survives(self):
        for bad in (BIG, HUGE, float("nan")):
            seg = self.Seg.from_dict(self.seg_dict([{"start": bad, "end": 1, "text": "a"},
                                                    {"start": 1, "end": 2, "text": "b"}]))
            self.assertEqual((seg.start, seg.end, seg.text, seg.words), (0.0, 2.0, "a b", []))

    def test_segment_with_healthy_words_keeps_them_exactly(self):
        seg = self.Seg.from_dict(self.seg_dict([{"start": 0, "end": 1, "text": "a"}, {"start": 1, "end": 2, "text": "b"}]))
        self.assertEqual([(w.start, w.end, w.text) for w in seg.words], [(0.0, 1.0, "a"), (1.0, 2.0, "b")])

    def test_words_that_are_not_a_list_become_empty(self):
        for raw in (None, "kata", 7, {"start": 0}, 3.5):
            self.assertEqual(self.Seg.from_dict(self.seg_dict(raw)).words, [], raw)
        self.assertEqual(self.Seg.from_dict({"start": 0, "end": 1, "text": "a"}).words, [])

    def test_word_entry_that_is_not_a_dict_empties_the_words(self):
        seg = self.Seg.from_dict(self.seg_dict([{"start": 0, "end": 1, "text": "a"}, None, "x"]))
        self.assertEqual(seg.words, [])

    def test_corrupt_segment_times_still_raise_for_the_loader_to_count(self):
        for key in ("start", "end"):
            for bad in (BIG, HUGE):
                with self.assertRaises((OverflowError, ValueError)):
                    self.Seg.from_dict({"start": 0, "end": 2, "text": "a", key: bad})

    # ------------------------------------------------------------------ pemuat project
    def test_parse_items_drops_only_the_corrupt_rows(self):
        items = [
            {"start": 0, "end": 2, "text": "ok", "words": [{"start": 0, "end": 1, "text": "ok"}]},
            {"start": BIG, "end": 2, "text": "baris rusak"},                       # OverflowError -> dibuang + dihitung
            {"start": 0, "end": HUGE, "text": "baris rusak 2"},
            {"start": 0, "end": 2, "text": "b c", "words": [{"start": BIG, "end": 1, "text": "b"}]},   # kata rusak: baris selamat
            {"start": 0, "end": 2, "text": "d", "words": None},
            "bukan dict",
        ]
        segs, dropped = self.store._parse_items(items)
        self.assertEqual((len(segs), dropped), (3, 3))
        self.assertEqual([s.text for s in segs], ["ok", "b c", "d"])
        self.assertEqual([len(s.words) for s in segs], [1, 0, 0])

    def test_decode_payload_from_real_json_text_does_not_crash(self):
        big = "1" + "0" * 400
        text = ('{"segments": [{"start": 0, "end": 2, "text": "ok", "words": [{"start": 0, "end": 1, "text": "ok"}]},'
                f' {{"start": {big}, "end": 2, "text": "x"}},'
                f' {{"start": 0, "end": 2, "text": "b c", "words": [{{"start": {big}, "end": 1, "text": "b"}},'
                ' {"start": 1, "end": 2, "text": "c"}]}]}')
        store = object.__new__(self.store)
        result = store._decode_payload(json.loads(text))
        self.assertEqual([s.text for s in result.segments], ["ok", "b c"])
        self.assertEqual(result.report.dropped, 1)
        self.assertEqual(result.segments[1].words, [])

    # ------------------------------------------------------------------ models
    def test_sanitize_word_list_rejects_huge_values_instead_of_raising(self):
        W = self.Word
        self.assertIsNone(self.m.sanitize_word_list([W(BIG, 1.0, "a")], 0.0, 2.0))
        self.assertIsNone(self.m.sanitize_word_list([W(0.0, HUGE, "a")], 0.0, 2.0))
        self.assertIsNone(self.m.sanitize_word_list([W(0.0, 1.0, "a")], BIG, BIG + 1))        # batas baris raksasa
        self.assertIsNone(self.m.sanitize_word_list([W(0.0, 1.0, "a")], 0.0, HUGE))
        self.assertIsNone(self.m.sanitize_word_list([object()], 0.0, 2.0))
        clean = self.m.sanitize_word_list([W(0.0, 1.0, "a")], 0.0, 2.0)
        self.assertEqual([(w.start, w.end) for w in clean], [(0.0, 1.0)])

    def test_normalize_drops_huge_rows_and_empties_corrupt_words(self):
        S, W = self.Seg, self.Word
        segs = [S(BIG, BIG + 1, "x"), S(0.0, HUGE, "y"), S(-HUGE, 1.0, "z"),
                S(0.0, 2.0, "a b", [W(BIG, 1.0, "a"), W(1.0, 2.0, "b")]),
                S(2.0, 4.0, "c d", [W(2.0, HUGE, "c"), W(3.0, 4.0, "d")]),
                S(4.0, 6.0, "e", [W(4.0, 5.0, "e")])]
        out, rep = self.m.SubtitleIO.normalize(segs)
        self.assertEqual([s.text for s in out], ["a b", "c d", "e"])
        self.assertEqual(rep.dropped, 3)
        self.assertEqual([len(s.words) for s in out], [0, 0, 1])

    def test_retime_words_ignores_huge_source_words(self):
        W = self.Word
        src = [W(BIG, BIG + 1, "x"), W(0.0, 1.0, "a"), W(1.0, HUGE, "b"), W(1.0, 2.0, "b")]
        out = self.m.retime_words("a b", src, 0.0, 2.0, 0.0, 2.0)
        self.assertEqual(len(out), 2)
        self.assertEqual(self.m.retime_words("a", [W(BIG, HUGE, "a")], 0.0, 1.0, 0.0, 1.0), [])

    def test_find_issues_marks_huge_times_as_bad_without_raising(self):
        S = self.Seg
        issues = self.m.SubtitleIO.find_issues([S(BIG, BIG + 1, "x"), S(0.0, HUGE, "y"), S(0.0, 2.0, "ok")])
        self.assertEqual(issues.get(0), "bad")
        self.assertEqual(issues.get(1), "bad")
        self.assertNotIn(2, issues)

    def test_clamp_num_returns_default_for_huge_ints(self):
        self.assertEqual(self.u._clamp_num(BIG, 5.0, 0.0, 10.0), 5.0)
        self.assertEqual(self.u._clamp_num(-HUGE, 5.0, 0.0, 10.0), 5.0)
        self.assertEqual(self.u._clamp_num(3, 5.0, 0.0, 10.0), 3.0)

    # ------------------------------------------------------------------ word_integrity
    def test_word_integrity_flags_and_repairs_huge_word_times(self):
        wi, S, W = self.wi, self.Seg, self.Word
        for number, bad_word in enumerate((W(BIG, 1.0, "a"), W(0.0, BIG, "a"), W(HUGE, 1.0, "a"), W(0.0, -HUGE, "a"))):
            with self.subTest(word=number):
                seg = S(0.0, 2.0, "a b", [bad_word, W(1.0, 2.0, "b")])
                kinds = {v.kind for v in wi.find_word_violations([seg])}
                self.assertEqual(kinds, {wi.NON_FINITE})
                repair = wi.enforce_word_integrity([seg])
                self.assertEqual((repair.dropped, repair.clamped, seg.words), ((0,), (), []))
                self.assertEqual(wi.find_word_violations([seg]), [])
                self.assertEqual(wi.enforce_word_integrity([seg]).total, 0)

    def test_word_integrity_tolerates_huge_segment_bounds(self):
        wi, S, W = self.wi, self.Seg, self.Word
        for seg in (S(BIG, BIG + 1, "x", [W(0.0, 1.0, "x")]), S(0.0, HUGE, "x", [W(0.0, 1.0, "x")])):
            wi.find_word_violations([seg])                          # tidak melempar
            wi.enforce_word_integrity([seg])                        # tidak melempar
            self.assertEqual(wi.find_word_violations([seg]), [])

    def test_word_integrity_leaves_healthy_neighbours_untouched(self):
        wi, S, W = self.wi, self.Seg, self.Word
        good = S(0.0, 2.0, "a b", [W(0.0, 1.0, "a"), W(1.0, 2.0, "b")])
        originals = list(good.words)
        bad = S(2.0, 4.0, "c", [W(BIG, 1.0, "c")])
        repair = wi.enforce_word_integrity([good, bad])
        self.assertEqual(repair.dropped, (1,))
        self.assertTrue(all(a is b for a, b in zip(good.words, originals)))

    # ------------------------------------------------------------------ pagar kode
    def test_no_float_except_block_left_without_overflowerror(self):
        pattern = re.compile(r"except \(([^)]*)\)")
        for rel in ("kaptionpro/models.py", "kaptionpro/core/word_integrity.py", "kaptionpro/core/utils.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            for match in pattern.finditer(src):
                names = match.group(1)
                if "ValueError" in names and "TypeError" in names:
                    self.assertIn("OverflowError", names, f"{rel}: {match.group(0)}")
        storage = (ROOT / "kaptionpro/core/storage.py").read_text(encoding="utf-8")
        parse = storage[storage.index("def _parse_items"):storage.index("def _decode_payload")]
        self.assertIn("OverflowError", parse)


WORKSPACE_SCRIPT = FAKE_HEADER + r"""
import types
from kaptionpro.ui.workspace import WorkspaceScreen as W

def flaky(fail=10 ** 9):
    n = [0]
    def f(*a, **k):
        n[0] += 1
        if n[0] <= fail: raise RuntimeError("boom")
    f.n = n
    return f

def new_ws():
    ws = object.__new__(W)
    ws.segments, ws._dirty = [], False
    ws.video = types.SimpleNamespace(_pos=0.0)
    ws._update_save_indicator, ws._refresh_views, ws.seek_time = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
    ws._schedule_vlc_subs, ws._schedule_autosave, ws.set_status = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
    ws.afters = []
    ws.after = lambda ms, fn=None: ws.afters.append((ms, fn)) or "id"
    return ws

def segs(): return [SubtitleSegment(0.0, 2.5, "Halo dunia"), SubtitleSegment(3.0, 5.0, "Apa kabar")]

def last_status(ws): return ws.set_status.call_args.args[0]

# 1) after() gagal menjadwalkan retry pertama: pengguna diberi tahu lewat status, bukan hanya log; data tetap aman
ws = new_ws(); ws._refresh_views = flaky(); ws.after = flaky()
ws._on_segments_changed(segs())                                  # tidak melempar
status = last_status(ws)
assert "Retry otomatis gagal dijadwalkan" in status and "timeline" in status and "tersimpan otomatis" in status, status
assert "mencoba lagi" not in status
assert ws._dirty is True and ws._schedule_autosave.called and ws.after.n[0] == 1

# 2) after() normal: pesan lama 'mencoba lagi' tetap, tanpa peringatan kegagalan jadwal
ws = new_ws(); ws._refresh_views = flaky()
ws._on_segments_changed(segs())
assert "mencoba lagi" in last_status(ws) and "Retry otomatis gagal" not in last_status(ws) and len(ws.afters) == 1

# 3) retry kedua gagal DIJADWALKAN: status ikut memberi tahu
ws = new_ws(); ws._refresh_views = flaky()
ws._on_segments_changed(segs())
assert len(ws.afters) == 1
ws.after = flaky()
ws.afters[0][1]()                                                 # percobaan ke-2 (timeline masih gagal) -> jadwal ke-3 gagal
assert "Retry otomatis gagal dijadwalkan" in last_status(ws), last_status(ws)

# 4) percobaan terakhir: tidak ada jadwal lagi, maka tidak ada pesan 'gagal dijadwalkan'
ws = new_ws(); ws._refresh_views = flaky()
ws._on_segments_changed(segs())
ws.afters[0][1](); ws.afters[1][1]()
assert len(ws.afters) == 2 and "Retry otomatis gagal" not in last_status(ws) and "tersimpan otomatis" in last_status(ws)

# 5) set_status ikut rusak: tetap tidak melempar, autosave tetap terjadwal
ws = new_ws(); ws._refresh_views = flaky(); ws.after = flaky(); ws.set_status = flaky()
ws._on_segments_changed(segs())
assert ws._dirty is True and ws._schedule_autosave.called

# 6) sukses penuh: perilaku lama (status 'N lines', tanpa jadwal ulang)
ws = new_ws(); ws._on_segments_changed(segs())
assert last_status(ws) == "2 lines" and not ws.afters
print("WORKSPACE19201 OK")
"""

EDITOR_SCRIPT = FAKE_HEADER + r"""
from kaptionpro.models import SubtitleIO
import kaptionpro.ui.editor as editor_mod
from kaptionpro.ui.editor import SubtitleEditorPanel as E
from kaptionpro.core.word_integrity import find_word_violations

editor_mod.messagebox = mock.MagicMock()
BIG, HUGE = 10 ** 400, 10 ** 10000

def new_editor(segs):
    ed = object.__new__(E)
    ed.segments, ed._undo, ed._redo = list(segs), [], []
    for name in ("_clear_form", "_refresh", "on_changed", "_populate", "select_segment", "on_select"):
        setattr(ed, name, mock.MagicMock())
    ed.calls, ed._pos, ed._current = [], {}, None
    ed.before_op = lambda reason: ed.calls.append(("backup", reason))
    return ed

def fp(segs): return [(s.start, s.end, s.text, len(s.words)) for s in segs]

good = lambda: [SubtitleSegment(0.0, 2.0, "a b", [Word(0.0, 1.0, "a"), Word(1.0, 2.0, "b")])]

# 1) replace_all dengan kata/baris raksasa: tidak melempar; baris rusak dibuang, kata rusak -> words kosong
ed = new_editor(good())
incoming = [SubtitleSegment(0.0, 2.0, "a b", [Word(BIG, 1.0, "a"), Word(1.0, 2.0, "b")]),
            SubtitleSegment(BIG, BIG + 1, "x"), SubtitleSegment(3.0, HUGE, "y"),
            SubtitleSegment(4.0, 6.0, "c", [Word(4.0, 5.0, "c")])]
ed.replace_all(incoming, label="impor")
assert [s.text for s in ed.segments] == ["a b", "c"] and [len(s.words) for s in ed.segments] == [0, 1]
assert not find_word_violations(ed.segments) and ed._undo[-1].label == "impor" and ed.on_changed.call_count == 1

# 2) segel menangkap kata raksasa walau normalize 'lolos' (pertahanan berlapis) dan operasi tetap sah
ed = new_editor(good())
real_norm = SubtitleIO.normalize
SubtitleIO.normalize = staticmethod(lambda segs: (list(segs), None))
bad = [SubtitleSegment(0.0, 2.0, "a b", [Word(0.0, HUGE, "a"), Word(1.0, 2.0, "b")])]
try:
    ed.replace_all(bad, label="x")
finally:
    SubtitleIO.normalize = staticmethod(real_norm)
assert ed.segments[0].words == [] and ed.last_word_repair.dropped == (0,) and not find_word_violations(ed.segments)

# 3) Merge dua baris yang salah satunya punya kata raksasa: tidak melempar, words kosong
ed = new_editor([SubtitleSegment(0.0, 2.0, "a b", [Word(BIG, 1.0, "a"), Word(1.0, 2.0, "b")]),
                 SubtitleSegment(2.0, 4.0, "c d", [Word(2.0, 3.0, "c"), Word(3.0, 4.0, "d")])])
ed._selected_indices = lambda: [0, 1]
ed._merge_selected()
assert len(ed.segments) == 1 and ed.segments[0].words == [] and ed._undo[-1].label == "merge-selected"

# 4) Split baris dengan kata raksasa: tidak melempar (karaoke dihitung ulang dari teks), dua baris hasil
ed = new_editor([SubtitleSegment(0.0, 4.0, "a b c d", [Word(BIG, 1.0, "a"), Word(1.0, 2.0, "b"), Word(2.0, 3.0, "c"),
                                                       Word(3.0, 4.0, "d")])])
ed._selected_indices = lambda: [0]
ed.get_playhead = lambda: 2.0
ed._split_selected()
assert len(ed.segments) == 2 and all(s.words == [] for s in ed.segments) and not find_word_violations(ed.segments)
print("EDITOR19201 OK")
"""


class RealCodeTests(unittest.TestCase):
    def _run(self, script: str, marker: str) -> None:
        res = run_script(script)
        self.assertEqual(res.returncode, 0, (res.stderr or res.stdout)[-3500:])
        self.assertIn(marker, res.stdout)

    def test_workspace_tells_the_user_when_retry_cannot_be_scheduled(self):
        self._run(WORKSPACE_SCRIPT, "WORKSPACE19201 OK")

    def test_editor_operations_survive_huge_word_timestamps(self):
        self._run(EDITOR_SCRIPT, "EDITOR19201 OK")


if __name__ == "__main__":
    unittest.main()
