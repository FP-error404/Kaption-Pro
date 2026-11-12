"""
Tes v1.9.18: dialog Laporan Kualitas + integrasi editor, TANPA Tk.

Kode ASLI ui/quality_dialog.py dan ui/editor.py dijalankan di subproses dengan modul GUI dipalsukan (pola yang sama dengan
test_refactor_v197): widget diganti tiruan, tetapi logika (alur Analyze -> Preview -> Confirm -> Apply -> Undo, filter, lompat ke
baris, penerapan ke daftar segmen, backup, satu langkah Undo editor) adalah kode produksi.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAKE_HEADER = r"""
import sys, types, copy
from unittest import mock

def mk(name):
    return type(name, (), {"__init__": lambda s, *a, **k: None, "__getattr__": lambda s, n: (lambda *a, **k: None)})

def fakemod(name, extra=()):
    m = types.ModuleType(name)
    m.__getattr__ = lambda n: mk(n)
    for n in extra:
        setattr(m, n, mk(n))
    sys.modules[name] = m
    return m

ctk = fakemod("customtkinter", ("CTkToplevel", "CTkFrame", "CTkScrollableFrame", "CTkLabel", "CTkButton", "CTkTextbox",
                                "CTkSlider", "StringVar", "CTk", "CTkOptionMenu", "CTkRadioButton", "CTkProgressBar",
                                "CTkEntry", "CTkCheckBox", "CTkSwitch", "CTkFont", "BooleanVar", "IntVar", "DoubleVar",
                                "CTkScrollbar"))
ctk.set_appearance_mode = lambda *a, **k: None
ctk.set_default_color_theme = lambda *a, **k: None
tk = fakemod("tkinter", ("Tk", "Frame", "Canvas", "Toplevel", "Label", "Text", "Entry", "StringVar", "IntVar",
                         "BooleanVar", "Misc", "Widget", "Event"))
tk.TclError = type("TclError", (Exception,), {})
for sub in ("messagebox", "filedialog", "font", "ttk", "colorchooser", "simpledialog"):
    setattr(tk, sub, fakemod("tkinter." + sub))
sys.path.insert(0, sys.argv[1])

from kaptionpro.models import SubtitleSegment, Word
from kaptionpro.core import quality_fix as qf, quality_flow as flow, subtitle_quality as sq
"""

DIALOG_SCRIPT = FAKE_HEADER + r"""
import types
from kaptionpro.ui import quality_dialog as qd

class FakeTree:
    def __init__(self): self.rows, self.sel = {}, ()
    def get_children(self): return tuple(self.rows)
    def delete(self, *ids):
        for i in ids: self.rows.pop(i, None)
    def insert(self, parent, index, iid=None, values=(), tags=()): self.rows[iid] = (values, tags)
    def selection(self): return self.sel

def sample():
    return [SubtitleSegment(0.0, 2.5, "Halo dunia"), SubtitleSegment(2.0, 4.0, "Apa kabar"),
            SubtitleSegment(8.0, 9.0, "a\nb\nc"), SubtitleSegment(20.0, 20.5, "x" * 30)]

def fp(segs): return [(s.start, s.end, s.text) for s in segs]

def make_dialog(state, ask, strict=False, filt="Semua"):
    d = object.__new__(qd.QualityReportDialog)
    d._get_segments = lambda: state["segs"]
    def apply_plan(plan):
        state["undo"].append(copy.deepcopy(state["segs"]))
        state["applied"] += 1
        state["segs"] = [SubtitleSegment(float(r.start), float(r.end), r.text) for r in plan.rows_after]
    def undo():
        if state["undo"]:
            state["segs"] = state["undo"].pop()
    d._apply_plan, d._undo_fn = apply_plan, undo
    d._jump_to = lambda i: state["jumps"].append(i)
    d._ask = ask
    d.flow, d._report, d._view = flow.QualityFlow(), None, "report"
    d.strict_var = types.SimpleNamespace(get=lambda: strict)
    d.filter_var = types.SimpleNamespace(get=lambda: filt)
    d.report_tree, d.preview_tree = FakeTree(), FakeTree()
    for name in ("summary_label", "status_label", "preview_info", "report_frame", "preview_frame",
                 "btn_reanalyze", "btn_fix", "btn_undo", "btn_back", "btn_apply", "btn_close"):
        setattr(d, name, mock.MagicMock())
    return d

def last_text(label):
    return label.configure.call_args.kwargs.get("text", "")

# ---------------- 1) Analyze: laporan per baris, tidak mengubah apa pun
state = {"segs": sample(), "undo": [], "applied": 0, "jumps": []}
original = fp(state["segs"])
d = make_dialog(state, ask=lambda text: True)
d.analyze()
assert d._view == "report" and d.flow.stage == flow.ANALYZED
assert sorted(d.report_tree.rows) == ["1", "2", "3"], d.report_tree.rows          # baris 2 (overlap), 3 (3 baris), 4 (CPS)
values, tags = d.report_tree.rows["1"]
assert values[0] == 2 and "Tumpang tindih" in values[3] and tags == ("error",), (values, tags)
assert "bermasalah" in last_text(d.summary_label)
d.btn_fix.configure.assert_called_with(state="normal")
assert fp(state["segs"]) == original

# ---------------- 2) filter: hanya error / hanya peringatan
d_err = make_dialog(state, ask=lambda t: True, filt="Hanya error")
d_err.analyze()
assert all(t == ("error",) for _v, t in d_err.report_tree.rows.values()) and d_err.report_tree.rows, d_err.report_tree.rows
d_warn = make_dialog(state, ask=lambda t: True, filt="Hanya peringatan")
d_warn.analyze()
assert all(t == ("warning",) for _v, t in d_warn.report_tree.rows.values()) and "2" in d_warn.report_tree.rows

# ---------------- 3) lompat ke baris
d.report_tree.sel = ("3",)
d._on_open_row()
assert state["jumps"] == [3], state["jumps"]
d.report_tree.sel = ()
d._on_open_row()
assert state["jumps"] == [3]

# ---------------- 4) Preview: TIDAK mengubah subtitle
d._start_fix()
assert d.flow.stage == flow.PREVIEW and d._view == "preview"
assert sorted(d.preview_tree.rows, key=int) == ["0", "2", "3"], d.preview_tree.rows
before_col, after_col = d.preview_tree.rows["2"][0][1], d.preview_tree.rows["2"][0][2]
assert "a ⏎ b ⏎ c" in before_col and "a b c" in after_col, (before_col, after_col)
assert "→" in d.preview_tree.rows["0"][0][1] and "→" in d.preview_tree.rows["0"][0][2]
assert fp(state["segs"]) == original and state["applied"] == 0, "pratinjau tidak boleh mengubah apa pun"
d.btn_apply.configure.assert_called_with(text="✅ Terapkan 3 perubahan…")

# ---------------- 5) Confirm DITOLAK -> tetap di pratinjau, tidak ada perubahan
d._ask = lambda text: False
d._confirm_and_apply()
assert state["applied"] == 0 and fp(state["segs"]) == original and d.flow.stage == flow.PREVIEW

# ---------------- 6) kembali dari pratinjau
d._back_to_report()
assert d.flow.stage == flow.ANALYZED and d._view == "report"
d._start_fix()
assert d.flow.stage == flow.PREVIEW

# ---------------- 7) Confirm DITERIMA -> Apply -> laporan baru, Undo tersedia
asked = []
d._ask = lambda text: asked.append(text) or True
d._confirm_and_apply()
assert state["applied"] == 1 and fp(state["segs"]) != original
assert "3 perubahan" in asked[0] and "Backup" in asked[0] and "Undo" in asked[0]
assert d.flow.stage == flow.APPLIED and d.flow.can_undo and d._view == "report"
assert d.btn_undo.pack.called, "tombol Undo harus muncul setelah apply"
assert "3 baris diperbaiki" in last_text(d.status_label)
assert sq.check_subtitles(state["segs"]).ok, "hasil perbaikan harus lolos pemeriksa"

# ---------------- 8) Undo -> kembali PERSIS
d._undo()
assert fp(state["segs"]) == original, "Undo harus mengembalikan persis"
assert "dibatalkan" in last_text(d.status_label)
assert d.flow.stage == flow.ANALYZED and not d.flow.can_undo

# ---------------- 9) rencana basi: subtitle berubah antara pratinjau dan konfirmasi
state2 = {"segs": sample(), "undo": [], "applied": 0, "jumps": []}
d2 = make_dialog(state2, ask=lambda t: True)
d2.analyze(); d2._start_fix()
def ask_and_edit(text):
    state2["segs"][0].text = "Diedit di tengah jalan"
    return True
d2._ask = ask_and_edit
d2._confirm_and_apply()
assert state2["applied"] == 0 and "berubah sejak pratinjau" in last_text(d2.status_label)
assert d2.flow.stage == flow.ANALYZED and state2["segs"][0].text == "Diedit di tengah jalan"

# ---------------- 10) penerapan gagal -> tidak setengah jadi, dialog tetap hidup
state3 = {"segs": sample(), "undo": [], "applied": 0, "jumps": []}
d3 = make_dialog(state3, ask=lambda t: True)
def boom(plan): raise OSError("disk penuh")
d3._apply_plan = boom
d3.analyze(); d3._start_fix(); d3._confirm_and_apply()
assert fp(state3["segs"]) == original and d3.flow.stage == flow.ANALYZED and not d3.flow.can_undo
assert "gagal" in last_text(d3.status_label)

# ---------------- 11) tidak ada yang bisa diperbaiki otomatis
state4 = {"segs": [SubtitleSegment(0.0, 2.0, "Halo"), SubtitleSegment(3.0, 5.0, "Dunia")],
          "undo": [], "applied": 0, "jumps": []}
d4 = make_dialog(state4, ask=lambda t: True)
d4.analyze()
d4.btn_fix.configure.assert_called_with(state="disabled")
assert "lolos" in last_text(d4.summary_label)
d4._start_fix()
assert d4._view == "report" and d4.flow.stage == flow.ANALYZED and not d4.preview_tree.rows

# ---------------- 12) aturan ketat mengaktifkan pemeriksaan durasi/jeda
state5 = {"segs": [SubtitleSegment(0.0, 2.0, "Halo"), SubtitleSegment(2.0, 4.0, "Dunia")],
          "undo": [], "applied": 0, "jumps": []}
d5 = make_dialog(state5, ask=lambda t: True, strict=True)
d5.analyze()
assert d5.report_tree.rows and "Jeda" in d5.report_tree.rows["1"][0][3]

# ---------------- 13) helper tampilan
ch = qf.FixChange(0, qf.Row(0.0, 2.5, "A"), qf.Row(0.0, 2.0, "A"), ("overlap",), ("x",), True, False)
b, a = qd.change_columns(ch)
assert "00:00:02,500" in b and "00:00:02,000" in a and "A" not in b.replace("00:00", ""), (b, a)
assert qd.one_line("a\nb\n\nc", 70) == "a ⏎ b ⏎ c"
assert qd.one_line("x" * 100, 10).endswith("…") and len(qd.one_line("x" * 100, 10)) == 10
assert qd._span(None, None) == "(waktu tidak valid)"
print("DIALOG OK")
"""

EDITOR_SCRIPT = FAKE_HEADER + r"""
from kaptionpro.ui.editor import SubtitleEditorPanel as E

def new_editor(segs):
    ed = object.__new__(E)
    ed.segments, ed._undo, ed._redo = list(segs), [], []
    ed._clear_form, ed._refresh, ed.on_changed = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
    ed.calls, ed._quality_dialog = [], None
    ed.before_op = lambda reason: ed.calls.append(("backup", reason))
    return ed

def fp(segs): return [(s.start, s.end, s.text, tuple((w.start, w.end, w.text) for w in s.words)) for s in segs]

long_text = "satu dua tiga empat lima enam tujuh delapan sembilan sepuluh sebelas dua belas"
tokens = long_text.split()
step = 6.0 / len(tokens)                                       # 74 karakter / 6 detik = 12 CPS: hanya teks yang perlu dirapikan
long_words = [Word(8.0 + i * step, 8.0 + (i + 1) * step, t) for i, t in enumerate(tokens)]
words = [Word(0.0, 1.0, "Halo"), Word(1.0, 2.0, "dunia")]
segs = [SubtitleSegment(0.0, 2.5, "Halo dunia", words),
        SubtitleSegment(2.0, 4.0, "Apa kabar", [Word(2.0, 3.0, "Apa"), Word(3.0, 4.0, "kabar")]),
        SubtitleSegment(8.0, 14.0, long_text, long_words)]
ed = new_editor(segs)
original = fp(ed.segments)

# --- alur penuh dengan editor ASLI (replace_all, _push_undo, undo asli; hanya widget yang palsu)
f = flow.QualityFlow()
f.analyze(ed.segments)
plan = f.preview(ed.segments)
assert fp(ed.segments) == original, "pratinjau tidak boleh mengubah editor"
f.confirm()
assert fp(ed.segments) == original, "konfirmasi saja tidak mengubah editor"
real_replace_all = ed.replace_all
def spying_replace_all(new, push_undo=True, **kw):
    ed.calls.append(("replace_all", len(new), push_undo))
    assert kw.get("label") == "before-quality-fix", kw                         # v1.9.19: langkah Undo bertanda
    assert kw.get("expect") is not None, "hasil harus diverifikasi terhadap rencana"
    return real_replace_all(new, push_undo, **kw)
ed.replace_all = spying_replace_all
count = f.apply(ed.segments, ed._apply_quality_plan)
assert count == len(plan.changes) == 2, (count, [c.index for c in plan.changes])
assert ed.calls[0] == ("backup", "before-quality-fix"), ed.calls                   # backup SEBELUM perubahan
assert ed.calls[1] == ("replace_all", 3, True), ed.calls                           # satu replace_all dengan push_undo
assert len(ed._undo) == 1, "tepat SATU langkah Undo"
assert fp(ed.segments) != original and sq.check_subtitles(ed.segments).ok
assert ed.on_changed.called

# word timestamp: baris yang WAKTUNYA berubah kehilangan words; baris yang hanya teksnya dirapikan mempertahankannya
assert ed.segments[0].end == 2.0 and ed.segments[0].words == [], "waktu berubah -> words dibuang (seperti Auto-Fix lama)"
assert [w.text for w in ed.segments[1].words] == ["Apa", "kabar"], "baris tak berubah mempertahankan words"
assert "\n" in ed.segments[2].text and [w.text for w in ed.segments[2].words] == tokens, "hanya teks dirapikan -> words tetap"

# --- Undo asli mengembalikan PERSIS
assert f.undo(lambda: ed.segments, ed.undo) is True
assert fp(ed.segments) == original, "Undo editor harus mengembalikan persis"
assert f.stage == flow.UNDONE and not ed._undo and len(ed._redo) == 1

# --- rencana basi ditolak sebelum menyentuh apa pun
ed2 = new_editor(segs)
stale_plan = qf.plan_fixes(ed2.segments)
ed2.segments[0].text = "Diedit"
try:
    ed2._apply_quality_plan(stale_plan)
    raise SystemExit("seharusnya StalePlanError")
except flow.StalePlanError:
    pass
assert ed2.calls == [] and ed2._undo == [], "tidak ada backup/perubahan untuk rencana basi"

# --- _segments_from_plan tidak memutasi segmen asli dan menyalin words
ed3 = new_editor(copy.deepcopy(segs))
snap = fp(ed3.segments)
new = ed3._segments_from_plan(qf.plan_fixes(ed3.segments))
assert fp(ed3.segments) == snap and all(a is not b for a, b in zip(new, ed3.segments))
assert new[1].words is not ed3.segments[1].words

# --- lompat ke baris dari laporan
ed4 = new_editor(segs)
ed4.select_segment, ed4.on_select = mock.MagicMock(), mock.MagicMock()
ed4._jump_to_index(1)
ed4.select_segment.assert_called_once_with(ed4.segments[1]); ed4.on_select.assert_called_once_with(ed4.segments[1])
ed4._jump_to_index(99); ed4._jump_to_index(-1)
assert ed4.select_segment.call_count == 1, "indeks di luar jangkauan diabaikan"

# --- membuka laporan: kosong -> info saja; form kotor yang tak valid -> tidak membuka dialog
import kaptionpro.ui.editor as editor_mod
opened = []
editor_mod.QualityReportDialog = lambda *a, **k: opened.append((a, k)) or types_ns
import types
types_ns = types.SimpleNamespace(winfo_exists=lambda: True, lift=lambda: None)
ed5 = new_editor([])
ed5._open_quality_report()
assert not opened, "tanpa subtitle tidak ada dialog"
ed6 = new_editor(segs)
ed6._form_dirty, ed6._apply = lambda: True, lambda: False
ed6._open_quality_report()
assert not opened, "form tidak valid -> berhenti"
ed7 = new_editor(segs)
ed7._form_dirty = lambda: False
ed7._open_quality_report()
assert len(opened) == 1 and set(opened[0][1]) == {"get_segments", "apply_plan", "undo", "jump_to"}, opened
ed7._open_quality_report()
assert len(opened) == 1, "dialog yang sudah terbuka tidak dibuka dua kali"
print("EDITOR OK")
"""


def run_script(script: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", script, str(ROOT)], capture_output=True, text=True, timeout=180)


class QualityDialogLogicTests(unittest.TestCase):
    def test_real_dialog_logic_with_fake_gui(self):
        res = run_script(DIALOG_SCRIPT)
        self.assertEqual(res.returncode, 0, (res.stderr or res.stdout)[-2500:])
        self.assertIn("DIALOG OK", res.stdout)

    def test_real_editor_integration_with_fake_gui(self):
        res = run_script(EDITOR_SCRIPT)
        self.assertEqual(res.returncode, 0, (res.stderr or res.stdout)[-2500:])
        self.assertIn("EDITOR OK", res.stdout)


class WiringSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.editor = (ROOT / "kaptionpro" / "ui" / "editor.py").read_text(encoding="utf-8")
        self.dialog = (ROOT / "kaptionpro" / "ui" / "quality_dialog.py").read_text(encoding="utf-8")

    def test_menu_entry_exists_and_method_is_defined(self):
        self.assertIn('("📋 Laporan kualitas…", "_open_quality_report")', self.editor)
        for method in ("_open_quality_report", "_jump_to_index", "_segments_from_plan", "_apply_quality_plan"):
            self.assertIn(f"def {method}(", self.editor)

    def test_legacy_auto_fix_button_is_untouched(self):
        self.assertIn('("✔ Auto-Fix", self._auto_fix, 82)', self.editor)
        self.assertIn('self.before_op("before-autofix")', self.editor)

    def test_apply_does_backup_first_then_one_undoable_replace(self):
        body = self.editor[self.editor.index("def _apply_quality_plan("):]
        self.assertLess(body.index("if not plan.matches(self.segments)"), body.index("self.before_op(QUALITY_FIX_LABEL)"))
        self.assertLess(body.index("self.before_op(QUALITY_FIX_LABEL)"), body.index("self.replace_all(new_segments"))
        self.assertEqual(body.count("replace_all("), 1, "satu langkah Undo")
        self.assertNotIn("push_undo=False", body)

    def test_dialog_never_mutates_segments_itself(self):
        for forbidden in ("segments[", ".segments =", "replace_all", ".text =", ".start =", ".end ="):
            self.assertNotIn(forbidden, self.dialog)

    def test_dialog_uses_the_flow_for_every_step(self):
        for needle in ("self.flow.analyze(", "self.flow.preview(", "self.flow.confirm()", "self.flow.apply(", "self.flow.undo(",
                       "self.flow.cancel_preview()"):
            self.assertIn(needle, self.dialog)

    def test_apply_is_only_reachable_through_the_confirmation_question(self):
        body = self.dialog[self.dialog.index("def _confirm_and_apply("):self.dialog.index("def _after_apply(")]
        self.assertLess(body.index("self._ask(question)"), body.index("self.flow.confirm()"))
        self.assertLess(body.index("self.flow.confirm()"), body.index("self.flow.apply("))
        self.assertEqual(self.dialog.count("self.flow.apply("), 1, "satu-satunya jalan menuju Apply")

    def test_after_apply_does_not_reset_the_flow_so_undo_stays_available(self):
        body = self.dialog[self.dialog.index("def _after_apply("):self.dialog.index("def _undo(")]
        self.assertNotIn("self.flow.analyze(", body)
        self.assertNotIn("self.analyze(", body)

    def test_fix_button_label_mentions_the_preview_step(self):
        self.assertIn("🔧 Perbaiki otomatis…", self.dialog)
        self.assertIn("✅ Terapkan…", self.dialog)


if __name__ == "__main__":
    unittest.main()
