"""
Tes v1.9.20 (Editor Transaction Consistency & Hardening), TANPA Tk.

Kode ASLI ui/editor.py, ui/workspace.py dan core/word_integrity.py (GUI dipalsukan, pola test_quality_dialog_v1918).
Tiga temuan:
  1. Atomicity untuk Add / Delete / Split / Merge / Apply (edit baris): gagal di tahap mana pun = isi, Undo, Redo kembali.
  2. Pemulihan setelah rollback: on_changed yang gagal (juga berkali-kali) dicoba ulang, jalur darurat workspace dipakai, dan bila
     tetap gagal pengguna diberi tahu (bukan hanya log); workspace menyiapkan bagian kritis (autosave) SEBELUM tampilan.
  3. Word timestamp: setelah timing berubah tidak boleh ada kata di luar rentang barisnya.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
from test_quality_dialog_v1918 import FAKE_HEADER, run_script  # noqa: E402

EDITOR_PRELUDE = FAKE_HEADER + r"""
from kaptionpro.models import SubtitleIO
import kaptionpro.ui.editor as editor_mod
from kaptionpro.ui.editor import SubtitleEditorPanel as E
from kaptionpro.core.word_integrity import WordRepair, find_word_violations

editor_mod.messagebox = mock.MagicMock()
LONG = "satu dua tiga empat lima enam tujuh delapan sembilan sepuluh sebelas dua belas"

def base():
    return [SubtitleSegment(0.0, 2.5, "Halo dunia", [Word(0.0, 1.0, "Halo"), Word(1.0, 2.0, "dunia")]),
            SubtitleSegment(3.0, 5.0, "Apa kabar", [Word(3.0, 4.0, "Apa"), Word(4.0, 5.0, "kabar")]),
            SubtitleSegment(8.0, 14.0, LONG)]

def edited(i, text):
    segs = base()
    segs[i].text = text
    return segs

def fp(segs): return [(s.start, s.end, s.text, tuple((w.start, w.end, w.text) for w in s.words)) for s in segs]

def new_editor(segs):
    ed = object.__new__(E)
    ed.segments, ed._undo, ed._redo = list(segs), [], []
    for name in ("_clear_form", "_refresh", "on_changed", "_populate", "select_segment", "on_select", "_update_rows_around",
                 "_set_error", "text_box", "dur_label", "start_entry", "end_entry"):
        setattr(ed, name, mock.MagicMock())
    ed.calls, ed._quality_dialog, ed._pos, ed._current = [], None, {}, None
    ed.get_playhead, ed._filter_active = (lambda: 0.0), (lambda: False)
    ed.before_op = lambda reason: ed.calls.append(("backup", reason))
    return ed

def ready_editor():
    # riwayat nyata: Undo=[op-A], Redo=[op-B]; isi sekarang = hasil op-A
    ed = new_editor(base())
    ed.replace_all(edited(1, "A-edit"), label="op-A")
    ed.replace_all(edited(1, "B-edit"), label="op-B")
    assert ed.undo() is True
    # kembalikan teks baris 1 ke semula agar op yang diuji bekerja pada data bersih (riwayat tetap utuh)
    ed.segments[1].text = "A-edit"
    for name in ("_clear_form", "_refresh", "on_changed"):
        setattr(ed, name, mock.MagicMock())
    return ed

def snap(ed): return (fp(ed.segments), list(ed._undo), list(ed._redo))

def flaky(fail=1, exc_type=RuntimeError):
    n = [0]
    def f(*a, **k):
        n[0] += 1
        if n[0] <= fail:
            raise exc_type("boom")
    f.n = n
    return f

def always(exc_type=RuntimeError):
    return flaky(10 ** 9, exc_type)
"""

OPS_SCRIPT = EDITOR_PRELUDE + r"""
def run_add(ed): ed._current = None; ed._add_line()
def run_delete(ed): ed._selected_indices = lambda: [1]; ed._delete_selected()
def run_split(ed): ed._selected_indices = lambda: [0]; ed._split_selected()
def run_merge(ed): ed._selected_indices = lambda: [0, 1]; ed._merge_selected()
def run_apply(ed):
    ed._current = ed.segments[0]
    ed.start_entry.get, ed.end_entry.get = (lambda: "0"), (lambda: "2.5")
    ed.text_box.get = lambda *a: "Teks diubah\n"
    assert ed._apply() is True

OPS = (("add", run_add, "add-line", +1), ("delete", run_delete, "delete-selected", -1),
       ("split", run_split, "split-selected", +1), ("merge", run_merge, "merge-selected", -1),
       ("apply", run_apply, "apply", 0))

def f_refresh(ed): ed._refresh = flaky(); ed._update_rows_around = flaky(); ed.select_segment = flaky()
def f_on_changed(ed): ed.on_changed = flaky()
def f_seal(ed): ed._seal_content = flaky()
def f_push(ed): ed._push_undo = flaky()
def f_dur(ed): ed.dur_label = types.SimpleNamespace(configure=flaky())
import types

# ====================================================== 0) jalur sukses: satu langkah Undo bertanda, Redo dibersihkan
for name, run, label, delta in OPS:
    ed = ready_editor()
    n0 = len(ed.segments)
    run(ed)
    assert len(ed.segments) == n0 + delta, name
    assert [e.label for e in ed._undo] == ["op-A", label] and not ed._redo, (name, [e.label for e in ed._undo])
    assert ed.on_changed.call_count == 1 and not find_word_violations(ed.segments), name
    assert ed.undo() is True and [e.label for e in ed._undo] == ["op-A"], name

# ====================================================== 1) ATOMICITY: tiap operasi x tiap tahap gagal
FAULTS = (("refresh", f_refresh), ("on_changed", f_on_changed), ("seal", f_seal), ("push_undo", f_push))
for name, run, label, delta in OPS:
    for fault_name, inject in FAULTS:
        if fault_name == "seal" and name in ("add", "delete"):
            continue                                   # dua operasi ini tidak menyentuh word timestamp
        tag = name + "/" + fault_name
        ed = ready_editor()
        before = snap(ed)
        if fault_name == "refresh" and name == "apply":
            ed._update_rows_around = flaky()           # jalur inkremental apply
            ed._refresh = flaky()
        else:
            inject(ed)
        failed = False
        try:
            run(ed)
        except RuntimeError as exc:
            failed = "boom" in str(exc) or True
        except AssertionError:
            raise
        if not failed:
            raise SystemExit(tag + ": operasi seharusnya gagal")
        assert snap(ed) == before, tag + ": isi + Undo + Redo harus PERSIS seperti sebelum percobaan (tanpa entri hantu)"
        assert not ed.sync_degraded, tag + ": rollback + sinkronisasi pulih"
        # Undo sesudah gagal membatalkan op-A yang NYATA dan Redo (op-B) tetap ada
        for attr in ("_clear_form", "_refresh", "on_changed", "_update_rows_around", "select_segment"):
            setattr(ed, attr, mock.MagicMock())
        assert [e.label for e in ed._undo] == ["op-A"] and [e.label for e in ed._redo] == ["op-B"], tag
        assert ed.undo() is True and fp(ed.segments) == fp(base()), tag
        assert [e.label for e in ed._redo] == ["op-B", "op-A"], tag

# dur_label (UI di tengah apply) juga gagal -> rollback
ed = ready_editor(); before = snap(ed); f_dur(ed)
try:
    run_apply(ed); raise SystemExit("seharusnya gagal")
except RuntimeError:
    pass
assert snap(ed) == before

# retime_words melempar di tahap siap: belum ada yang berubah, tidak ada snapshot Undo
ed = ready_editor(); before = snap(ed)
real_retime = editor_mod.retime_words
editor_mod.retime_words = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom-retime"))
try:
    run_apply(ed); raise SystemExit("seharusnya gagal")
except RuntimeError:
    pass
finally:
    editor_mod.retime_words = real_retime
assert snap(ed) == before and not ed.on_changed.called

# navigasi sesudah commit (populate/select/on_select) bukan bagian transaksi: data tetap tersimpan, galat diteruskan
ed = ready_editor(); ed._populate = flaky()
try:
    run_add(ed); raise SystemExit("seharusnya melempar")
except RuntimeError:
    pass
assert [e.label for e in ed._undo] == ["op-A", "add-line"] and len(ed.segments) == 4, "commit tetap sah"

# tidak ada pemicu on_changed untuk jalur 'tidak ada yang dipilih' (tanpa efek samping)
ed = ready_editor(); before = snap(ed)
ed._selected_indices = lambda: []
ed._delete_selected(); ed._split_selected(); ed._merge_selected()
assert snap(ed) == before and not ed.on_changed.called
print("OPS19 OK")
"""

RECOVERY_SCRIPT = EDITOR_PRELUDE + r"""
def op(ed):
    ed.replace_all(edited(0, "baru"), label="x")

def attempt_op(ed):
    try:
        op(ed)
    except RuntimeError:
        return True
    raise SystemExit("operasi seharusnya gagal")

def shown(): return editor_mod.messagebox.showerror.call_count

# R1) on_changed gagal sekali (di operasi): pemulihan mengirim isi ASLI lewat percobaan pertama
ed = ready_editor(); before = snap(ed); original = fp(ed.segments)
seen = []
def once(segs):
    seen.append(fp(segs))
    if len(seen) == 1: raise RuntimeError("boom")
ed.on_changed = once
attempt_op(ed)
assert len(seen) == 2 and seen[1] == original and snap(ed) == before and not ed.sync_degraded and not shown()

# R2) gagal di operasi + percobaan pertama pemulihan: percobaan kedua berhasil (retry nyata, bukan sekadar log)
ed = ready_editor(); before = snap(ed); ed.on_changed = flaky(2)
attempt_op(ed)
assert ed.on_changed.n[0] == 3 and snap(ed) == before and not ed.sync_degraded and not shown()

# R3) on_changed gagal terus, jalur darurat workspace berhasil
ed = ready_editor(); before = snap(ed); ed.on_changed = always()
hook_calls = []
ed.on_resync = lambda segs: hook_calls.append(fp(segs)) or True
attempt_op(ed)
assert ed.on_changed.n[0] == 1 + ed.SYNC_ATTEMPTS, "percobaan ulang dibatasi, tidak tak berhingga"
assert len(hook_calls) == 1 and hook_calls[0] == before[0] and snap(ed) == before
assert not ed.sync_degraded and not shown()

# R4) gagal terus dan tanpa jalur darurat: pengguna DIBERI TAHU, data tetap benar, status 'degraded' sampai pulih
ed = ready_editor(); before = snap(ed); ed.on_changed = always()
attempt_op(ed)
assert snap(ed) == before and ed.sync_degraded is True and shown() == 1
msg = editor_mod.messagebox.showerror.call_args.args[1]
assert "DIBATALKAN" in msg and "workspace/autosave" in msg, msg
ed.on_changed = mock.MagicMock()
op(ed)                                                       # operasi berikutnya sukses -> sinkronisasi pulih
assert ed.sync_degraded is False and ed._undo[-1].label == "x"

# R5/R6) jalur darurat melempar / mengembalikan False: sama seperti R4
for hook in (lambda segs: (_ for _ in ()).throw(RuntimeError("hook-boom")), lambda segs: False):
    editor_mod.messagebox.reset_mock()
    ed = ready_editor(); before = snap(ed); ed.on_changed = always(); ed.on_resync = hook
    attempt_op(ed)
    assert snap(ed) == before and ed.sync_degraded is True and shown() == 1

# R7) _refresh gagal terus: rollback tetap memulihkan isi + riwayat, pengguna diberi tahu
editor_mod.messagebox.reset_mock()
ed = ready_editor(); before = snap(ed); ed._refresh = always()
attempt_op(ed)
assert snap(ed) == before and ed.sync_degraded is True and shown() == 1
assert "tabel" in editor_mod.messagebox.showerror.call_args.args[1]

# R8) isi RUSAK di tengah operasi (bukan segmen): rollback tetap memulihkan dari snapshot
editor_mod.messagebox.reset_mock()
ed = ready_editor(); before = snap(ed)
real_bulk = SubtitleIO.bulk_edit
def corrupt(segments, idxs, op_name, p1, p2):
    segments[0] = object(); raise RuntimeError("boom-korup")
SubtitleIO.bulk_edit = staticmethod(corrupt)
try:
    ed._run_bulk("upper", "", "", [0], "bulk-upper"); raise SystemExit("seharusnya gagal")
except RuntimeError:
    pass
finally:
    SubtitleIO.bulk_edit = real_bulk
assert snap(ed) == before and not ed.sync_degraded

# R9) undo/redo yang gagal juga dipulihkan sinkronnya
ed = ready_editor(); before = snap(ed); ed.on_changed = flaky(2)
try:
    ed.undo(); raise SystemExit("seharusnya gagal")
except RuntimeError:
    pass
assert snap(ed) == before and ed.on_changed.n[0] == 3 and not ed.sync_degraded

# R10) tidak ada sinkronisasi saat tidak ada yang berubah (kegagalan sebelum menyentuh isi)
ed = ready_editor(); before = snap(ed)
real_norm = SubtitleIO.normalize
SubtitleIO.normalize = staticmethod(lambda segs: (_ for _ in ()).throw(RuntimeError("boom")))
try:
    op(ed)
except RuntimeError:
    pass
finally:
    SubtitleIO.normalize = staticmethod(real_norm)
assert snap(ed) == before and not ed.on_changed.called and not ed._refresh.called
print("RECOVERY19 OK")
"""

WORDS_SCRIPT = EDITOR_PRELUDE + r"""
import math
from kaptionpro.core.word_integrity import WordIntegrityError
real_norm = SubtitleIO.normalize
passthrough = staticmethod(lambda segs: (sorted(segs, key=lambda s: (s.start, s.end)), None))   # normalize yang 'lolos'

# W1) Merge dua baris yang saling menimpa: words gabungan melanggar (overlap) -> dibuang, tidak ada pelanggaran tersisa
ed = new_editor([SubtitleSegment(0.0, 2.0, "satu dua", [Word(0.0, 1.0, "satu"), Word(1.0, 2.0, "dua")]),
                 SubtitleSegment(1.5, 3.5, "tiga empat", [Word(1.5, 2.5, "tiga"), Word(2.5, 3.5, "empat")])])
ed._selected_indices = lambda: [0, 1]
ed._merge_selected()
assert len(ed.segments) == 1 and not find_word_violations(ed.segments)
assert ed.last_word_repair.dropped == (0,) and ed.segments[0].words == [], "overlap besar -> timestamp kata dibuang"

# W2) Merge baris sehat berurutan: words dipertahankan, tidak ada pemulihan
ed = new_editor(base()[:2])
ed._selected_indices = lambda: [0, 1]
ed._merge_selected()
assert [w.text for w in ed.segments[0].words] == ["Halo", "dunia", "Apa", "kabar"] and ed.last_word_repair.total == 0

# W3) Inline Apply: retime menghasilkan kata di luar rentang baru -> dijepit (kecil) atau dibuang (jauh)
def apply_with(words, new_end):
    ed = new_editor(base())
    ed._current = ed.segments[0]
    ed.start_entry.get, ed.end_entry.get = (lambda: "0"), (lambda: str(new_end))
    ed.text_box.get = lambda *a: "Halo dunia\n"
    editor_mod.retime_words = lambda *a, **k: words
    try:
        assert ed._apply() is True
    finally:
        editor_mod.retime_words = real_retime
    return ed
real_retime = editor_mod.retime_words
ed = apply_with([Word(0.0, 1.0, "Halo"), Word(1.0, 1.9, "dunia")], 1.5)
assert ed.segments[0].end == 1.5 and not find_word_violations(ed.segments)
assert ed.segments[0].words[-1].end == 1.5 and ed.last_word_repair.clamped == (0,), "meleset kecil: dijepit ke batas baris"
ed = apply_with([Word(0.0, 1.0, "Halo"), Word(1.0, 5.0, "dunia")], 1.5)
assert not find_word_violations(ed.segments) and ed.segments[0].words == [] and ed.last_word_repair.dropped == (0,)

# W4) Auto-Fix: bahkan bila normalize tidak menjepit, hasil akhir tidak punya kata di luar baris (pertahanan berlapis)
def auto_fix_case(normalizer):
    segs = [SubtitleSegment(0.0, 3.0, "a b c", [Word(0.0, 1.0, "a"), Word(1.0, 2.0, "b"), Word(2.0, 3.0, "c")]),
            SubtitleSegment(2.5, 5.0, "d e", [Word(2.5, 3.5, "d"), Word(3.5, 5.0, "e")])]
    ed = new_editor(segs)
    editor_mod.messagebox = mock.MagicMock(); editor_mod.messagebox.askyesno.return_value = True
    SubtitleIO.normalize = normalizer
    try:
        ed._auto_fix()
    finally:
        SubtitleIO.normalize = staticmethod(real_norm)
    return ed
ed = auto_fix_case(staticmethod(real_norm))
assert not find_word_violations(ed.segments) and ed.segments[0].end < 2.5, "jalur normal: bersih"
ed = auto_fix_case(passthrough)
assert not find_word_violations(ed.segments), "normalize 'lolos' pun: segel editor yang menjepit"
assert ed.last_word_repair.total >= 1, "ada pemulihan nyata (kata terakhir baris pertama melewati akhir baru)"
assert all(w.end <= s.end + 1e-9 for s in ed.segments for w in s.words)
editor_mod.messagebox = mock.MagicMock()

# W5) Quality Fix (timing berubah) dan Shift massal: tidak ada pelanggaran, kata ikut bergeser
ed = new_editor([SubtitleSegment(0.0, 2.5, "Halo dunia", [Word(0.0, 1.0, "Halo"), Word(1.0, 2.0, "dunia")]),
                 SubtitleSegment(2.0, 4.0, "Apa kabar", [Word(2.0, 3.0, "Apa"), Word(3.0, 4.0, "kabar")])])
f = flow.QualityFlow(); f.analyze(ed.segments); f.preview(ed.segments); f.confirm()
f.apply(ed.segments, ed._apply_quality_plan)
assert not find_word_violations(ed.segments) and ed.segments[0].words == [], "timing berubah -> words dibuang (desain lama)"
assert [w.text for w in ed.segments[1].words] == ["Apa", "kabar"]
ed = new_editor(base())
ed._run_bulk("shift", "1.5", "", [0, 1], "shift-all")
assert not find_word_violations(ed.segments) and ed.segments[0].words[0].start == 1.5 and ed.last_word_repair.total == 0

# W6) Jaring terakhir: bila pemulihan gagal menuntaskan, operasi DIBATALKAN (rollback) dan galat diteruskan
ed = ready_editor(); before = snap(ed)
real_enforce = editor_mod.enforce_word_integrity
editor_mod.enforce_word_integrity = lambda segs: WordRepair()          # 'pemulihan' yang tidak berbuat apa-apa
SubtitleIO.normalize = passthrough
bad = base(); bad[0].words = [Word(0.0, 1.0, "Halo"), Word(1.0, 9.0, "dunia")]
try:
    ed.replace_all(bad, label="x"); raise SystemExit("seharusnya WordIntegrityError")
except WordIntegrityError:
    pass
finally:
    editor_mod.enforce_word_integrity = real_enforce
    SubtitleIO.normalize = staticmethod(real_norm)
assert snap(ed) == before and not ed.sync_degraded

# W7) undo/redo memulihkan snapshot apa adanya (tidak 'dipulihkan' ulang oleh segel)
ed = ready_editor()
ed.replace_all(edited(0, "ubah"), label="x")
assert ed.undo() and fp(ed.segments)[0][3] == ((0.0, 1.0, "Halo"), (1.0, 2.0, "dunia"))
print("WORDS20 OK")
"""

WORKSPACE_SCRIPT = FAKE_HEADER + r"""
import types
from kaptionpro.models import SubtitleIO
import kaptionpro.ui.editor as editor_mod
from kaptionpro.ui.editor import SubtitleEditorPanel as E
from kaptionpro.ui.workspace import WorkspaceScreen as W

editor_mod.messagebox = mock.MagicMock()

def base():
    return [SubtitleSegment(0.0, 2.5, "Halo dunia", [Word(0.0, 1.0, "Halo"), Word(1.0, 2.0, "dunia")]),
            SubtitleSegment(3.0, 5.0, "Apa kabar")]

def fp(segs): return [(s.start, s.end, s.text, tuple((w.start, w.end, w.text) for w in s.words)) for s in segs]

def flaky(fail=1):
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
    ws.flush = mock.MagicMock(return_value=True)
    ws.afters = []
    ws.after = lambda ms, fn=None: ws.afters.append((ms, fn)) or "id"
    return ws

# ---- A) langkah kritis lebih dulu: kegagalan tampilan tidak pernah membuat data kotor tanpa autosave
ws = new_ws(); ws._refresh_views = flaky(10 ** 9)
segs = base()
ws._on_segments_changed(segs)                              # tidak melempar
assert ws.segments is segs and ws._dirty is True
ws._schedule_autosave.assert_called_once()
ws._schedule_vlc_subs.assert_called_once(), "langkah tampilan lain tetap jalan walau timeline gagal"
ws.seek_time.assert_called_once()
status = ws.set_status.call_args.args[0]
assert "belum sinkron" in status and "timeline" in status and "mencoba lagi" in status, status
assert len(ws.afters) == 1 and ws.afters[0][0] == 400

# percobaan ulang dibatasi (3x), lalu pesan akhir tanpa 'mencoba lagi'
ws.afters[0][1]()
assert len(ws.afters) == 2 and ws.afters[1][0] == 800
ws.afters[1][1]()
assert len(ws.afters) == 2, "tidak menjadwalkan lagi setelah percobaan ke-3"
assert "mencoba lagi" not in ws.set_status.call_args.args[0] and "tersimpan otomatis" in ws.set_status.call_args.args[0]

# pulih di percobaan ke-2: status normal
ws = new_ws(); ws._refresh_views = flaky(1)
ws._on_segments_changed(base())
assert "belum sinkron" in ws.set_status.call_args.args[0]
ws.afters[0][1]()
assert ws.set_status.call_args.args[0] == "2 lines" and len(ws.afters) == 1

# semua langkah tampilan gagal sekaligus: tetap tidak melempar, autosave tetap terjadwal
ws = new_ws()
for name in ("_update_save_indicator", "_refresh_views", "seek_time", "_schedule_vlc_subs", "set_status"):
    setattr(ws, name, flaky(10 ** 9))
ws._on_segments_changed(base())
assert ws._dirty is True and ws._schedule_autosave.called

# jalur sukses: perilaku lama (status 'N lines', tanpa jadwal ulang)
ws = new_ws(); ws._on_segments_changed(base())
assert ws.set_status.call_args.args[0] == "2 lines" and not ws.afters and ws._dirty

# ---- B) bagian kritis gagal -> galat diteruskan ke editor (rollback), dirty sudah ter-set
ws = new_ws(); ws._schedule_autosave = flaky(10 ** 9)
try:
    ws._on_segments_changed(base()); raise SystemExit("seharusnya melempar")
except RuntimeError:
    pass
assert ws._dirty is True and not ws._refresh_views.called

# ---- C) jalur darurat
ws = new_ws(); segs = base()
assert ws._resync_after_editor_rollback(segs) is True and ws.segments is segs and ws._dirty
ws._schedule_autosave.assert_called_once(); assert not ws.flush.called
ws = new_ws(); ws._schedule_autosave = flaky(10 ** 9)
assert ws._resync_after_editor_rollback(base()) is True and ws.flush.called and ws._dirty, "autosave gagal -> simpan langsung"
ws = new_ws(); ws._schedule_autosave = flaky(10 ** 9); ws.flush = mock.MagicMock(return_value=False)
assert ws._resync_after_editor_rollback(base()) is False
ws = new_ws(); ws._schedule_autosave = flaky(10 ** 9); ws.flush = flaky(10 ** 9)
assert ws._resync_after_editor_rollback(base()) is False
assert ws._sync_views.__func__ is not None
ws = new_ws(); ws._resync_after_editor_rollback(base())
assert ws._refresh_views.called and ws._schedule_vlc_subs.called, "tampilan ikut disegarkan"

# ---- D) editor ASLI + workspace: on_changed gagal terus (autosave tak bisa dijadwalkan) -> darurat -> flush -> pulih
ws = new_ws()
ed = object.__new__(E)
ed.segments, ed._undo, ed._redo = list(base()), [], []
for name in ("_clear_form", "_refresh"):
    setattr(ed, name, mock.MagicMock())
ed.before_op = lambda reason: None
ed.on_changed, ed.on_resync = ws._on_segments_changed, ws._resync_after_editor_rollback
ed.replace_all(base(), label="seed")
ws._dirty = False
before = (fp(ed.segments), list(ed._undo), list(ed._redo))
ws._schedule_autosave = flaky(10 ** 9)
new = base(); new[1].text = "diubah"
try:
    ed.replace_all(new, label="x"); raise SystemExit("seharusnya gagal")
except RuntimeError:
    pass
assert (fp(ed.segments), list(ed._undo), list(ed._redo)) == before, "memori dipulihkan"
assert ws.segments is ed.segments and fp(ws.segments) == before[0], "workspace memegang isi yang dipulihkan"
assert ws._dirty is True and ws.flush.called, "data langsung disimpan lewat jalur darurat"
assert ed.sync_degraded is False and not editor_mod.messagebox.showerror.called

# E) autosave & flush gagal semua: pengguna diberi tahu, data memori tetap benar
ws = new_ws(); ws._schedule_autosave = flaky(10 ** 9); ws.flush = mock.MagicMock(return_value=False)
ed.on_changed, ed.on_resync = ws._on_segments_changed, ws._resync_after_editor_rollback
before = (fp(ed.segments), list(ed._undo), list(ed._redo))
try:
    ed.replace_all(new, label="x"); raise SystemExit("seharusnya gagal")
except RuntimeError:
    pass
assert (fp(ed.segments), list(ed._undo), list(ed._redo)) == before
assert ed.sync_degraded is True and editor_mod.messagebox.showerror.call_count == 1
print("WORKSPACE20 OK")
"""


class RealCodeTransactionTests(unittest.TestCase):
    def _run(self, script: str, marker: str) -> None:
        res = run_script(script)
        self.assertEqual(res.returncode, 0, (res.stderr or res.stdout)[-3500:])
        self.assertIn(marker, res.stdout)

    def test_split_merge_add_delete_apply_are_atomic(self):
        self._run(OPS_SCRIPT, "OPS19 OK")

    def test_recovery_after_rollback_retries_falls_back_and_tells_the_user(self):
        self._run(RECOVERY_SCRIPT, "RECOVERY19 OK")

    def test_word_timestamps_never_outlive_their_segment(self):
        self._run(WORDS_SCRIPT, "WORDS20 OK")

    def test_workspace_syncs_critical_state_first_and_recovers(self):
        self._run(WORKSPACE_SCRIPT, "WORKSPACE20 OK")


class WordIntegrityCoreTests(unittest.TestCase):
    """Murni Python: core/word_integrity."""

    def setUp(self) -> None:
        from kaptionpro.core import word_integrity as wi
        from kaptionpro.models import SubtitleSegment, Word
        self.wi, self.S, self.W = wi, SubtitleSegment, Word

    def seg(self, start, end, words, text="x"):
        return self.S(start, end, text, [self.W(*w) for w in words])

    def kinds(self, seg):
        return {v.kind for v in self.wi.find_word_violations([seg])}

    def test_clean_segments_have_no_violations_and_are_untouched(self):
        seg = self.seg(0.0, 2.0, [(0.0, 1.0, "a"), (1.0, 2.0, "b")])
        originals = list(seg.words)
        self.assertEqual(self.wi.find_word_violations([seg]), [])
        repair = self.wi.enforce_word_integrity([seg])
        self.assertEqual(repair.total, 0)
        self.assertTrue(all(a is b for a, b in zip(seg.words, originals)), "baris sehat tidak disentuh")

    def test_each_violation_kind_is_detected(self):
        nan = float("nan")
        self.assertEqual(self.kinds(self.seg(0, 2, [(0.0, 1.0, "a"), (1.0, 3.0, "b")])), {self.wi.OUT_OF_RANGE})
        self.assertEqual(self.kinds(self.seg(1, 3, [(0.5, 1.5, "a")])), {self.wi.OUT_OF_RANGE})
        self.assertEqual(self.kinds(self.seg(0, 3, [(0.0, 1.0, "a"), (0.5, 0.8, "b")])), {self.wi.OVERLAP})
        self.assertEqual(self.kinds(self.seg(0, 3, [(1.0, 2.0, "a"), (0.2, 0.5, "b")])), {self.wi.DISORDER})
        self.assertIn(self.wi.REVERSED, self.kinds(self.seg(0, 3, [(2.0, 1.0, "a")])))
        self.assertEqual(self.kinds(self.seg(0, 3, [(nan, 1.0, "a")])), {self.wi.NON_FINITE})

    def test_tolerance_for_float_noise_and_touching_words(self):
        seg = self.seg(0.0, 2.0, [(0.0, 1.0, "a"), (1.0 - 1e-9, 2.0 + 1e-9, "b")])
        self.assertEqual(self.wi.find_word_violations([seg]), [])

    def test_small_stray_is_clamped_and_large_stray_is_dropped(self):
        small = self.seg(0.0, 2.0, [(0.0, 1.0, "a"), (1.0, 2.4, "b")])
        large = self.seg(0.0, 2.0, [(0.0, 1.0, "a"), (1.0, 9.0, "b")])
        repair = self.wi.enforce_word_integrity([small, large])
        self.assertEqual((repair.clamped, repair.dropped, repair.total), ((0,), (1,), 2))
        self.assertEqual(small.words[-1].end, 2.0)
        self.assertEqual(large.words, [])
        self.assertEqual(self.wi.find_word_violations([small, large]), [])

    def test_enforce_is_idempotent(self):
        seg = self.seg(0.0, 2.0, [(0.0, 1.0, "a"), (1.0, 2.4, "b")])
        self.wi.enforce_word_integrity([seg])
        self.assertEqual(self.wi.enforce_word_integrity([seg]).total, 0)

    def test_safety_net_drops_words_when_sanitizer_cannot_help(self):
        seg = self.seg(0.0, 2.0, [(0.0, 1.0, "a"), (1.0, 3.0, "b")])
        original = self.wi.sanitize_word_list
        self.wi.sanitize_word_list = lambda words, s, e, **k: list(words)          # 'pemulih' yang gagal menjepit
        try:
            repair = self.wi.enforce_word_integrity([seg])
        finally:
            self.wi.sanitize_word_list = original
        self.assertEqual(seg.words, [])
        self.assertEqual((repair.dropped, repair.clamped), ((0,), ()))
        self.assertEqual(self.wi.find_word_violations([seg]), [])

    def test_segments_without_words_and_garbage_words(self):
        self.assertEqual(self.wi.find_word_violations([self.S(0.0, 1.0, "x", [])]), [])
        bad = self.S(0.0, 1.0, "x", [object()])
        self.assertEqual({v.kind for v in self.wi.find_word_violations([bad])}, {self.wi.NON_FINITE})
        self.wi.enforce_word_integrity([bad])
        self.assertEqual(bad.words, [])

    def test_violation_details_name_the_word(self):
        seg = self.seg(0.0, 2.0, [(0.0, 1.0, "halo"), (1.0, 3.0, "dunia")])
        (violation,) = self.wi.find_word_violations([seg])
        self.assertEqual((violation.index, violation.word_index), (0, 1))
        self.assertIn("dunia", violation.detail)


class SourceWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.editor = (ROOT / "kaptionpro" / "ui" / "editor.py").read_text(encoding="utf-8")
        self.workspace = (ROOT / "kaptionpro" / "ui" / "workspace.py").read_text(encoding="utf-8")

    def _functions(self) -> dict[str, str]:
        parts = re.split(r"\n    def ", self.editor)
        return {part.split("(", 1)[0]: part for part in parts[1:]}

    def test_every_mutation_runs_inside_a_transaction(self):
        for name, body in self._functions().items():
            if name in ("_push_undo",) or "self._push_undo(" not in body:
                continue
            self.assertIn("with self._transaction()", body, f"{name} memakai _push_undo tanpa transaksi")
            self.assertLess(body.index("with self._transaction()"), body.index("self._push_undo("), name)

    def test_on_changed_is_only_called_through_notify_changed(self):
        calls = [m.start() for m in re.finditer(r"self\.on_changed\(", self.editor)]
        self.assertEqual(len(calls), 1, "satu-satunya pemanggilan langsung ada di _notify_changed")
        notify = self.editor.index("def _notify_changed")
        self.assertLess(notify, calls[0])
        self.assertNotIn("\n    def ", self.editor[notify + 10:calls[0]], "pemanggilan itu berada di dalam _notify_changed")

    def test_every_content_changing_operation_seals_word_timestamps(self):
        for name in ("replace_all", "_run_bulk", "_apply_search_result", "_apply", "_split_selected", "_merge_selected"):
            self.assertIn("self._seal_content(", self._functions()[name], name)

    def test_seal_runs_before_the_ui_is_told(self):
        for name in ("replace_all", "_run_bulk", "_apply_search_result", "_split_selected", "_merge_selected", "_apply"):
            body = self._functions()[name]
            self.assertLess(body.index("self._seal_content("), body.index("self._notify_changed()"), name)

    def test_navigation_after_commit_is_outside_the_transaction(self):
        add = self._functions()["_add_line"]
        self.assertLess(add.index("self._notify_changed()"), add.index("self._populate(seg)"))
        self.assertLess(add.index("self._notify_changed()"), add.index("self.on_select(seg)"))

    def test_workspace_wires_the_emergency_hook_and_orders_critical_first(self):
        self.assertIn("on_resync=self._resync_after_editor_rollback", self.workspace)
        body = self.workspace[self.workspace.index("def _on_segments_changed"):]
        body = body[:body.index("def _sync_views")]
        self.assertLess(body.index("self._dirty = True"), body.index("self._schedule_autosave()"))
        self.assertLess(body.index("self._schedule_autosave()"), body.index("self._sync_views("))


if __name__ == "__main__":
    unittest.main()
