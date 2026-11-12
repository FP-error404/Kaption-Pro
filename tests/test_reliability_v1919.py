"""
Tes v1.9.19 (Reliability Hardening), TANPA Tk.

Kode ASLI ui/editor.py dan ui/quality_dialog.py dijalankan di subproses dengan modul GUI dipalsukan (pola
test_quality_dialog_v1918): widget tiruan, logika produksi. Tiga temuan review yang dikunci di sini:
  1. Atomicity: kegagalan di tengah replace_all / undo / redo / edit massal / cari-ganti (normalisasi, verifikasi hasil,
     _clear_form, _refresh, on_changed) tidak boleh meninggalkan subtitle setengah berubah, entri Undo hantu, atau Redo hilang.
  2. Identitas Undo: langkah Undo punya penanda ("before-quality-fix") + nomor operasi unik; "Undo perubahan" di dialog hanya
     membatalkan Quality Fix itu sendiri, bukan operasi lain.
  3. Batas 5.000 baris: pemotongan daftar laporan DAN pratinjau selalu diberitahukan (Apply menerapkan SEMUA perubahan).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_quality_dialog_v1918 import FAKE_HEADER, run_script  # noqa: E402

EDITOR_SCRIPT = FAKE_HEADER + r"""
from kaptionpro.models import SubtitleIO
import kaptionpro.ui.editor as editor_mod
from kaptionpro.ui.editor import SubtitleEditorPanel as E, HistoryEntry

QF = "before-quality-fix"
editor_mod.messagebox = mock.MagicMock()

def new_editor(segs):
    ed = object.__new__(E)
    ed.segments, ed._undo, ed._redo = list(segs), [], []
    ed._clear_form, ed._refresh, ed.on_changed = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
    ed.calls, ed._quality_dialog = [], None
    ed.before_op = lambda reason: ed.calls.append(("backup", reason))
    return ed

def fp(segs): return [(s.start, s.end, s.text, tuple((w.start, w.end, w.text) for w in s.words)) for s in segs]

LONG = "satu dua tiga empat lima enam tujuh delapan sembilan sepuluh sebelas dua belas"
def base():
    return [SubtitleSegment(0.0, 2.5, "Halo dunia", [Word(0.0, 1.0, "Halo"), Word(1.0, 2.0, "dunia")]),
            SubtitleSegment(2.0, 4.0, "Apa kabar"),
            SubtitleSegment(8.0, 14.0, LONG)]

def edited(i, text):
    segs = base()
    segs[i].text = text
    return segs

def ready_editor():
    # riwayat nyata: Undo=[op-A], Redo=[op-B]; isi sekarang = hasil op-A (masih ada overlap + baris panjang)
    ed = new_editor(base())
    ed.replace_all(edited(1, "A-edit"), label="op-A")
    ed.replace_all(edited(1, "B-edit"), label="op-B")
    assert ed.undo() is True
    assert [e.label for e in ed._undo] == ["op-A"] and [e.label for e in ed._redo] == ["op-B"]
    ed.calls.clear(); ed.on_changed.reset_mock(); ed._refresh.reset_mock(); ed._clear_form.reset_mock()
    return ed

def snap(ed): return (fp(ed.segments), list(ed._undo), list(ed._redo))

def flaky(fail=1):
    n = [0]
    def f(*a, **k):
        n[0] += 1
        if n[0] <= fail:
            raise RuntimeError("boom")
    f.n = n
    return f

def quality_apply(ed):
    f = flow.QualityFlow()
    f.analyze(ed.segments); f.preview(ed.segments); f.confirm()
    return f, f.apply(ed.segments, ed._apply_quality_plan)

# ====================================================== 0) jalur sukses: satu langkah Undo bertanda + nomor unik
ed = ready_editor()
before_top = ed._undo[-1].op_id
f, count = quality_apply(ed)
assert count >= 1 and f.receipt is not None and f.receipt == ed._quality_receipt
assert len(ed._undo) == 2 and ed._undo[-1].label == QF and ed._undo[-1].op_id == f.receipt > before_top
assert ed.peek_undo() == ed._undo[-1] and not ed._redo, "operasi baru membersihkan Redo (perilaku lama)"
assert ed.calls == [("backup", QF)], ed.calls
assert sq.check_subtitles(ed.segments).ok
ids = []
ed2 = new_editor(base())
for _ in range(3): ids.append(ed2._push_undo())
assert ids == sorted(set(ids)) and [e.label for e in ed2._undo] == ["edit"] * 3, "label bawaan 'edit', nomor unik naik"

# ====================================================== 1) ATOMICITY: kegagalan di tiap tahap replace_all lewat alur Quality Fix
real_normalize = SubtitleIO.normalize
def inject_normalize(ed):
    def boom(segments): raise RuntimeError("boom-normalize")
    SubtitleIO.normalize = staticmethod(boom)
def inject_drop_row(ed):                       # normalize 'membuang' baris: hasil != rencana -> verifikasi harus menolak
    SubtitleIO.normalize = staticmethod(lambda segments: (real_normalize(segments)[0][:-1], None))
def inject_clear_form(ed): ed._clear_form = flaky()
def inject_refresh(ed): ed._refresh = flaky()
def inject_on_changed(ed): ed.on_changed = flaky()

FAULTS = (("normalize", inject_normalize, RuntimeError), ("verifikasi", inject_drop_row, flow.ApplyVerificationError),
          ("clear_form", inject_clear_form, RuntimeError), ("refresh", inject_refresh, RuntimeError),
          ("on_changed", inject_on_changed, RuntimeError))
for name, inject, expect_exc in FAULTS:
    ed = ready_editor()
    before = snap(ed)
    inject(ed)
    f = flow.QualityFlow()
    f.analyze(ed.segments); f.preview(ed.segments); f.confirm()
    try:
        f.apply(ed.segments, ed._apply_quality_plan)
        raise SystemExit(name + ": apply seharusnya gagal")
    except SystemExit:
        raise
    except Exception as exc:
        assert isinstance(exc, expect_exc), (name, repr(exc))
    finally:
        SubtitleIO.normalize = staticmethod(real_normalize)
    assert f.stage == flow.ANALYZED and f.receipt is None, (name, f.stage)
    assert snap(ed) == before, name + ": isi + Undo + Redo harus PERSIS seperti sebelum percobaan (tanpa entri hantu)"
    assert ed._quality_receipt is None, name
    # pengguna jujur: rencana lama masih cocok dengan isi sekarang (tidak berubah setengah-setengah)
    assert qf.plan_fixes(ed.segments).has_changes
    # Undo sesudah gagal membatalkan op-A yang NYATA (bukan langkah hantu), Redo op-B tetap tersedia
    for attr in ("_clear_form", "_refresh", "on_changed"):
        setattr(ed, attr, mock.MagicMock())
    assert ed.undo() is True and fp(ed.segments) == fp(base()), name
    assert [e.label for e in ed._redo] == ["op-B", "op-A"], name      # op-A kini di puncak Redo
    # dan percobaan ulang tanpa gangguan berhasil normal
    ed3 = ready_editor()
    quality_apply(ed3)
    assert ed3._undo[-1].label == QF and sq.check_subtitles(ed3.segments).ok, name

# on_changed gagal SETELAH data diganti: rollback juga menyegarkan tampilan + memberitahu pemilik data (autosave) dengan isi asli
ed = ready_editor()
original = fp(ed.segments)
seen = []
ed._refresh = mock.MagicMock()
ed.on_changed = lambda segs: (seen.append(fp(segs)), (_ for _ in ()).throw(RuntimeError("boom")) if len(seen) == 1 else None)[1]
try:
    ed.replace_all(edited(0, "baru"), label="x")
    raise SystemExit("seharusnya gagal")
except RuntimeError:
    pass
assert len(seen) == 2 and seen[0] != original and seen[1] == original, "panggilan kedua membawa isi ASLI"
assert ed._refresh.call_count == 2, "tampilan disegarkan lagi setelah rollback"

# kegagalan sebelum perubahan apa pun tidak menyentuh UI sama sekali
ed = ready_editor()
SubtitleIO.normalize = staticmethod(lambda segments: (_ for _ in ()).throw(RuntimeError("boom")))
try:
    ed.replace_all(base(), label="x")
except RuntimeError:
    pass
finally:
    SubtitleIO.normalize = staticmethod(real_normalize)
assert not ed.on_changed.called and not ed._refresh.called and not ed._clear_form.called

# push_undo=False tetap atomik dan tidak mengembalikan nomor operasi
ed = ready_editor(); before = snap(ed)
ed.on_changed = flaky()
try:
    ed.replace_all(edited(0, "z"), push_undo=False)
except RuntimeError:
    pass
assert snap(ed) == before
assert ed.replace_all(edited(0, "z"), push_undo=False) is None

# ====================================================== 2) ATOMICITY: undo / redo
ed = ready_editor(); before = snap(ed)
ed.on_changed = flaky()
try:
    ed.undo(); raise SystemExit("undo seharusnya gagal")
except RuntimeError:
    pass
assert snap(ed) == before, "Undo yang gagal tidak boleh memakai langkah Undo atau mengisi Redo"
ed.on_changed = mock.MagicMock()
assert ed.undo() is True and fp(ed.segments) == fp(base())
ed.on_changed = flaky()
before = snap(ed)
try:
    ed.redo(); raise SystemExit("redo seharusnya gagal")
except RuntimeError:
    pass
assert snap(ed) == before, "Redo yang gagal juga atomik"
ed.on_changed = mock.MagicMock()
assert ed.redo() is True and ed.redo() is True and ed.redo() is False and ed.undo() is True

ed = ready_editor(); before = snap(ed)
ed._undo[-1] = HistoryEntry("rusak", ["bukan-snapshot"], 1)          # snapshot korup: gagal di tahap SIAP
before = snap(ed)
try:
    ed.undo(); raise SystemExit("seharusnya gagal")
except SystemExit:
    raise
except Exception:
    pass
assert snap(ed) == before and not ed.on_changed.called
assert new_editor([]).undo() is False and new_editor([]).redo() is False

# ====================================================== 3) ATOMICITY: edit massal & cari-ganti
real_bulk = SubtitleIO.bulk_edit
ed = ready_editor(); before = snap(ed)
def evil(segments, idxs, op, p1, p2):
    segments[0].text = "RUSAK"; segments[1].start = 99.0
    raise RuntimeError("bulk-boom")
SubtitleIO.bulk_edit = staticmethod(evil)
try:
    ed._run_bulk("upper", "", "", [0], "bulk-upper"); raise SystemExit("seharusnya gagal")
except RuntimeError:
    pass
finally:
    SubtitleIO.bulk_edit = real_bulk
assert snap(ed) == before, "bulk_edit yang melempar di tengah jalan: isi dipulihkan dan snapshot Undo dibuang"

ed = ready_editor(); before = snap(ed)
SubtitleIO.bulk_edit = staticmethod(lambda segments, idxs, op, p1, p2: (setattr(segments[0], "text", "SENTUH"), (0, None))[1])
try:
    assert ed._run_bulk("upper", "", "", [0], "bulk-upper") is False
finally:
    SubtitleIO.bulk_edit = real_bulk
assert snap(ed) == before, "0 perubahan: isi (yang sempat tersentuh) dan Undo dipulihkan"
assert editor_mod.messagebox.showinfo.called

ed = ready_editor(); before = snap(ed)
SubtitleIO.bulk_edit = staticmethod(lambda segments, idxs, op, p1, p2: (setattr(segments[0], "text", "OK"), (1, None))[1])
try:
    assert ed._run_bulk("upper", "", "", [0], "bulk-upper") is True
finally:
    SubtitleIO.bulk_edit = real_bulk
assert ed.segments[0].text == "OK" and ed._undo[-1].label == "bulk-upper" and ("backup", "bulk-upper") in ed.calls

ed = ready_editor(); before = snap(ed)
ed.on_changed = flaky()
try:
    ed._apply_search_result(edited(2, "ganti")); raise SystemExit("seharusnya gagal")
except RuntimeError:
    pass
assert snap(ed) == before
ed.on_changed = mock.MagicMock()
ed._apply_search_result(edited(2, "ganti"))
assert ed._undo[-1].label == "search-replace" and ed.segments[2].text == "ganti"

# ====================================================== 4) IDENTITAS UNDO
# 4a) Undo langsung sesudah Quality Fix membatalkan tepat Quality Fix itu
ed = ready_editor(); original = fp(ed.segments)
f, _ = quality_apply(ed)
assert f.undo(lambda: ed.segments, ed._undo_quality_fix) is True and f.stage == flow.UNDONE
assert fp(ed.segments) == original and [e.label for e in ed._undo] == ["op-A"]

# 4b) ada operasi LAIN sesudah Quality Fix: ditolak SEBELUM mengubah apa pun (perubahan pengguna tidak ikut terbatalkan)
ed = ready_editor()
f, _ = quality_apply(ed)
ed._push_undo("edit-line"); ed.segments[0].text = "ketikan pengguna"
before = snap(ed)
try:
    f.undo(lambda: ed.segments, ed._undo_quality_fix); raise SystemExit("seharusnya ditolak")
except flow.UndoMismatchError as exc:
    assert "edit-line" in str(exc), str(exc)
assert snap(ed) == before and ed.segments[0].text == "ketikan pengguna", "tidak ada yang dibatalkan"
assert f.stage == flow.ANALYZED and f.plan is None and f.receipt is None and not f.can_undo

# 4c) label sama tetapi operasi BERBEDA (Quality Fix lama di dasar riwayat): nomor operasi menolaknya
ed = ready_editor()
quality_apply(ed)                                                  # QF#1 (tetap di riwayat)
ed.replace_all(base(), label="import")                              # isi bermasalah lagi
f2, _ = quality_apply(ed)                                          # QF#2
assert ed._quality_receipt == f2.receipt != ed._undo[-3].op_id
assert ed.undo() and ed.undo()                                     # Ctrl+Z biasa: batalkan QF#2 lalu import
assert ed.peek_undo().label == QF and ed.peek_undo().op_id != ed._quality_receipt
before = snap(ed)
try:
    ed._undo_quality_fix(); raise SystemExit("seharusnya ditolak")
except flow.UndoMismatchError:
    pass
assert snap(ed) == before
ed.undo_operation(QF)                                              # tanpa nomor: hanya label yang dicek (API terarah umum)
assert [e.label for e in ed._undo] == ["op-A"]

# 4d) Redo mempertahankan nomor operasi: Undo biasa -> Redo -> tombol Undo dialog tetap sah
ed = ready_editor()
f, _ = quality_apply(ed)
assert ed.undo() and ed.redo()
assert ed.peek_undo().label == QF and ed.peek_undo().op_id == ed._quality_receipt
ed._undo_quality_fix()
assert [e.label for e in ed._undo] == ["op-A"]

# 4e) riwayat kosong / label lain
for label in (QF, "apa-saja"):
    try:
        new_editor(base()).undo_operation(label); raise SystemExit("seharusnya ditolak")
    except flow.UndoMismatchError:
        pass
assert new_editor(base()).peek_undo() is None

# 4f) batas MAX_UNDO memangkas yang tertua tanpa merusak nomor operasi
ed = new_editor(base()); ed.MAX_UNDO = 3
ids = [ed._push_undo(f"op{i}") for i in range(5)]
assert [e.label for e in ed._undo] == ["op2", "op3", "op4"] and [e.op_id for e in ed._undo] == ids[2:]

# 4f2) tanpa nomor operasi, label saja yang menjaga: label lain ditolak, label benar jalan
ed = ready_editor(); before = snap(ed)
try:
    ed.undo_operation(QF); raise SystemExit("label salah seharusnya ditolak")
except flow.UndoMismatchError as exc:
    assert "op-A" in str(exc)
assert snap(ed) == before
ed.undo_operation("op-A")
assert not ed._undo and fp(ed.segments) == fp(base())

# 4g) label otomatis di titik-titik panggil lain: nama fungsi
src = open(sys.argv[1] + "/kaptionpro/ui/editor.py", encoding="utf-8").read()
assert "self._push_undo()" not in src, "setiap langkah Undo harus punya penanda"
for tag in ('_push_undo("add-line")', '_push_undo("delete-selected")', '_push_undo("split-selected")',
            '_push_undo("merge-selected")', '_push_undo(reason)', '_push_undo("search-replace")'):
    assert tag in src, tag
print("EDITOR19 OK")
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

def make_dialog(state, ask, filt="Semua", apply_plan=None, undo=None):
    d = object.__new__(qd.QualityReportDialog)
    d._get_segments = lambda: state["segs"]
    def default_apply(plan):
        state["applied"] += 1
        state["segs"] = [SubtitleSegment(float(r.start), float(r.end), r.text) for r in plan.rows_after]
    d._apply_plan = apply_plan or default_apply
    d._undo_fn = undo or (lambda: None)
    d._jump_to = lambda i: None
    d._ask = ask
    d.flow, d._report, d._view = flow.QualityFlow(), None, "report"
    d.strict_var = types.SimpleNamespace(get=lambda: False)
    d.filter_var = types.SimpleNamespace(get=lambda: filt)
    d.report_tree, d.preview_tree = FakeTree(), FakeTree()
    for name in ("summary_label", "status_label", "preview_info", "report_frame", "preview_frame",
                 "btn_reanalyze", "btn_fix", "btn_undo", "btn_back", "btn_apply", "btn_close"):
        setattr(d, name, mock.MagicMock())
    return d

def text_of(label): return label.configure.call_args.kwargs.get("text", "")
def color_of(label): return label.configure.call_args.kwargs.get("text_color")
qd.messagebox = mock.MagicMock()

# ====================================================== 0) helper angka & pemberitahuan
assert qd.fmt_int(5000) == "5.000" and qd.fmt_int(7321) == "7.321" and qd.fmt_int(12) == "12"
assert qd.fmt_int(1234567) == "1.234.567"
assert qd.limit_notice(5000, 5000, "baris") == "" and qd.limit_notice(3, 2, "baris") == ""
msg = qd.limit_notice(5000, 7321, "baris bermasalah", "Gunakan filter.")
assert msg.startswith("⚠") and "5.000 dari 7.321 baris bermasalah" in msg and msg.endswith("Gunakan filter."), msg
assert qd.MAX_ROWS == 5000

# ====================================================== 1) laporan > 5.000 baris: dipotong DAN diberitahukan (semua filter)
big = [SubtitleSegment(i * 3.0, i * 3.0 + 0.1, "halo") for i in range(6000)]
state = {"segs": big, "applied": 0}
rep = sq.check_subtitles(big)
by = rep.by_index()
assert len(by) == 6000, len(by)
for filt, key in (("Semua", None), ("Hanya error", sq.ERROR), ("Hanya peringatan", sq.WARNING)):
    d = make_dialog(state, ask=lambda t: True, filt=filt)
    d.analyze()
    matching = sum(1 for iss in by.values() if key is None or any(i.severity == key for i in iss))
    shown = len(d.report_tree.rows)
    assert shown == min(matching, 5000), (filt, shown, matching)
    text = text_of(d.summary_label)
    if matching > 5000:
        assert f"{qd.fmt_int(5000)} dari {qd.fmt_int(matching)} baris bermasalah" in text, (filt, text)
        assert "⚠ Daftar dipotong" in text, (filt, text)
        assert color_of(d.summary_label) == qd.Theme.WARN_TEXT
    else:
        assert "dipotong" not in text and color_of(d.summary_label) == qd.Theme.TEXT, (filt, text)
assert any(len(by) > 0 for _ in [0])

# tepat di batas: tidak ada pemberitahuan palsu
exact = [SubtitleSegment(i * 3.0, i * 3.0 + 0.1, "halo") for i in range(5000)]
d = make_dialog({"segs": exact, "applied": 0}, ask=lambda t: True); d.analyze()
assert len(d.report_tree.rows) == 5000 and "dipotong" not in text_of(d.summary_label)

# batas diperkecil: perilaku sama (cepat diuji)
qd.MAX_ROWS = 2
state = {"segs": sample(), "applied": 0}
d = make_dialog(state, ask=lambda t: True); d.analyze()
assert len(d.report_tree.rows) == 2 and "2 dari 3 baris bermasalah" in text_of(d.summary_label)

# ================================================= 2) pratinjau dipotong: diberitahukan di pratinjau + konfirmasi; Apply = SEMUA
qd.MAX_ROWS = 1
state = {"segs": sample(), "applied": 0}
asked = []
d = make_dialog(state, ask=lambda t: asked.append(t) or True)
d._start_fix()
plan = d.flow.plan
total = len(plan.changes)
assert total == 3 and len(d.preview_tree.rows) == 1
info = text_of(d.preview_info)
assert "1 dari 3 perubahan" in info and "SEMUA 3 perubahan" in info and color_of(d.preview_info) == qd.Theme.WARN_TEXT, info
d._confirm_and_apply()
assert "Pratinjau hanya menampilkan 1 dari 3 perubahan" in asked[0] and "SEMUA 3 perubahan akan diterapkan" in asked[0], asked[0]
assert state["applied"] == 1 and d.flow.stage == flow.APPLIED and d.flow.applied_count == 3, "semua perubahan diterapkan"
assert sq.check_subtitles(state["segs"]).ok

# tanpa pemotongan: tidak ada kalimat pemberitahuan di pratinjau maupun konfirmasi
qd.MAX_ROWS = 5000
state = {"segs": sample(), "applied": 0}
asked = []
d = make_dialog(state, ask=lambda t: asked.append(t) or True)
d._start_fix()
assert "dipotong" not in text_of(d.preview_info) and color_of(d.preview_info) == qd.Theme.TEXT
d._confirm_and_apply()
assert "hanya menampilkan" not in asked[0]

# ====================================================== 3) pesan gagal-terapkan JUJUR (diverifikasi, bukan diasumsikan)
qd.messagebox = mock.MagicMock()
state = {"segs": sample(), "applied": 0}
def apply_fail_clean(plan): raise RuntimeError("gagal-bersih")
d = make_dialog(state, ask=lambda t: True, apply_plan=apply_fail_clean)
d._start_fix(); d._confirm_and_apply()
status = text_of(d.status_label)
assert "TIDAK berubah" in status and "sudah diverifikasi" in status and d.flow.stage == flow.ANALYZED, status
shown_error = qd.messagebox.showerror.call_args.args[1]
assert "TIDAK berubah" in shown_error and "gagal-bersih" in shown_error

qd.messagebox = mock.MagicMock()
state = {"segs": sample(), "applied": 0}
def apply_fail_dirty(plan):
    state["segs"] = state["segs"][:-1]                                # data sempat berubah lalu gagal (rollback gagal)
    raise RuntimeError("gagal-kotor")
d = make_dialog(state, ask=lambda t: True, apply_plan=apply_fail_dirty)
d._start_fix(); d._confirm_and_apply()
status = text_of(d.status_label)
assert "berbeda dari sebelum perbaikan" in status and "TIDAK berubah" not in status, status
assert "Undo" in qd.messagebox.showerror.call_args.args[1]

qd.messagebox = mock.MagicMock()
state = {"segs": sample(), "applied": 0}
d = make_dialog(state, ask=lambda t: True, apply_plan=apply_fail_clean)
d._start_fix()
d._get_segments = lambda: (_ for _ in ()).throw(RuntimeError("tak terbaca"))
plan = d.flow.plan
assert "tidak bisa diverifikasi" in d._verdict_after_failed_apply(plan)

# ====================================================== 4) Undo ditolak: pengguna diberi tahu, subtitle tak berubah, alur direset
qd.messagebox = mock.MagicMock()
state = {"segs": sample(), "applied": 0}
def undo_mismatch(): raise flow.UndoMismatchError("ada perubahan lain sesudah operasi ini (langkah Undo teratas: 'edit-line')")
d = make_dialog(state, ask=lambda t: True, undo=undo_mismatch)
d._start_fix(); d._confirm_and_apply()
assert d.flow.can_undo
fixed = fp(state["segs"])
d._undo()
assert qd.messagebox.showwarning.called and "edit-line" in qd.messagebox.showwarning.call_args.args[1]
assert not qd.messagebox.showerror.called, "penolakan identitas bukan error tak terduga"
assert fp(state["segs"]) == fixed and d.flow.stage == flow.ANALYZED and not d.flow.can_undo
assert "Undo tidak dijalankan" in text_of(d.status_label)
print("DIALOG19 OK")
"""


class ReliabilityRealCodeTests(unittest.TestCase):
    def test_editor_atomicity_and_undo_identity(self):
        res = run_script(EDITOR_SCRIPT)
        self.assertEqual(res.returncode, 0, (res.stderr or res.stdout)[-3000:])
        self.assertIn("EDITOR19 OK", res.stdout)

    def test_dialog_limit_notice_and_honest_messages(self):
        res = run_script(DIALOG_SCRIPT)
        self.assertEqual(res.returncode, 0, (res.stderr or res.stdout)[-3000:])
        self.assertIn("DIALOG19 OK", res.stdout)


class FlowReliabilityTests(unittest.TestCase):
    """Murni Python: kontrak receipt / UndoMismatchError di QualityFlow."""

    def setUp(self) -> None:
        sys.path.insert(0, str(ROOT))
        from kaptionpro.core import quality_flow as flow
        from kaptionpro.models import SubtitleSegment
        self.flow = flow
        self.segs = [SubtitleSegment(0.0, 2.5, "Halo dunia"), SubtitleSegment(2.0, 4.0, "Apa kabar")]

    def _ready(self):
        f = self.flow.QualityFlow()
        f.analyze(self.segs)
        f.preview(self.segs)
        f.confirm()
        return f

    def test_receipt_is_stored_and_reset(self):
        f = self._ready()
        f.apply(self.segs, lambda plan: 42)
        self.assertEqual(f.receipt, 42)
        f.analyze(self.segs)
        self.assertIsNone(f.receipt)

    def test_failed_apply_has_no_receipt(self):
        f = self._ready()
        with self.assertRaises(RuntimeError):
            f.apply(self.segs, lambda plan: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual((f.stage, f.receipt, f.plan), (self.flow.ANALYZED, None, None))

    def test_undo_mismatch_resets_flow_and_propagates(self):
        f = self._ready()
        f.apply(self.segs, lambda plan: 7)

        def refuse():
            raise self.flow.UndoMismatchError("bukan operasi ini")

        with self.assertRaises(self.flow.UndoMismatchError):
            f.undo(lambda: self.segs, refuse)
        self.assertEqual((f.stage, f.receipt, f.plan, f.can_undo), (self.flow.ANALYZED, None, None, False))

    def test_constants_and_hierarchy(self):
        self.assertEqual(self.flow.QUALITY_FIX_LABEL, "before-quality-fix")
        self.assertTrue(issubclass(self.flow.UndoMismatchError, self.flow.FlowError))
        self.assertFalse(issubclass(self.flow.ApplyVerificationError, self.flow.FlowError))


class SourceWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.editor = (ROOT / "kaptionpro" / "ui" / "editor.py").read_text(encoding="utf-8")
        self.dialog = (ROOT / "kaptionpro" / "ui" / "quality_dialog.py").read_text(encoding="utf-8")

    def test_dialog_gets_targeted_undo_not_plain_undo(self):
        self.assertIn("undo=self._undo_quality_fix", self.editor)
        self.assertNotIn("undo=self.undo,", self.editor)

    def test_every_whole_content_operation_runs_in_a_transaction(self):
        for func in ("def replace_all(", "def _apply_search_result(", "def _run_bulk(", "def _step_history("):
            body = self.editor[self.editor.index(func):]
            body = body[:body.index("\n    def ", 10)]
            self.assertIn("self._transaction()", body, func)

    def test_replace_all_prepares_before_touching_anything(self):
        body = self.editor[self.editor.index("def replace_all("):]
        self.assertLess(body.index("SubtitleIO.normalize(segments)"), body.index("self._transaction()"))


if __name__ == "__main__":
    unittest.main()
