"""
Dialog "Laporan Kualitas Subtitle" (v1.9.18): daftar error/peringatan per baris subtitle + Perbaiki Otomatis yang aman.

Alur Perbaiki Otomatis (urutannya dijaga oleh core/quality_flow.QualityFlow, bukan oleh urutan tombol):
  Analyze (laporan ini) -> Preview Changes (tabel sebelum/sesudah, tidak ada yang berubah) -> Confirm (dialog Ya/Tidak)
  -> Apply (backup + SATU langkah Undo editor) -> Undo (tombol di sini atau ↶ Undo di editor).
Dialog ini tidak pernah mengubah subtitle sendiri: penerapan dan pembatalan disuntikkan oleh editor lewat callback.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from ..config import Theme, ctk, log
from ..core import quality_fix as qf
from ..core import subtitle_quality as sq
from ..core.quality_flow import FlowError, QualityFlow, StalePlanError, UndoMismatchError
from ..core.utils import srt_time
from .components import gold_button

# Aturan ketat (opsional lewat saklar di dialog): durasi 5/6-7 detik dan jeda 2 frame @24fps; angka patokan kasar.
STRICT_RULES = sq.QualityRules(min_duration=0.83, max_duration=7.0, min_gap=0.083)
MAX_ROWS = 5000                     # batas baris yang ditampilkan agar dialog tetap responsif pada project raksasa
FILTERS = {"all": "Semua", "error": "Hanya error", "warning": "Hanya peringatan"}
LEVEL_TEXT = {sq.ERROR: "Error", sq.WARNING: "Peringatan"}


def fmt_int(n: int) -> str:
    """Angka dengan pemisah ribuan titik (gaya Indonesia): 5000 -> '5.000'."""
    return f"{n:,}".replace(",", ".")


def limit_notice(shown: int, total: int, noun: str, tail: str = "") -> str:
    """Pemberitahuan bila daftar dipotong (v1.9.19); string kosong bila semua ditampilkan. Tidak pernah memotong diam-diam."""
    if total <= shown:
        return ""
    text = f"⚠ Daftar dipotong: hanya {fmt_int(shown)} dari {fmt_int(total)} {noun} yang ditampilkan."
    return f"{text} {tail}" if tail else text


def one_line(text: object, limit: int = 70) -> str:
    flat = " ⏎ ".join(part.strip() for part in str(text or "").replace("\r", "").split("\n") if part.strip())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def _span(start: object, end: object) -> str:
    try:
        return f"{srt_time(float(start))} → {srt_time(float(end))}"
    except (TypeError, ValueError, OverflowError):
        return "(waktu tidak valid)"


def change_columns(change: qf.FixChange) -> tuple[str, str]:
    """Teks kolom 'Sebelum' dan 'Sesudah' untuk satu perubahan: hanya bagian yang BERUBAH yang ditampilkan."""
    before, after = [], []
    if change.timing_changed:
        before.append(_span(change.before.start, change.before.end))
        after.append(_span(change.after.start, change.after.end))
    if change.text_changed:
        before.append(one_line(change.before.text, 60))
        after.append(one_line(change.after.text, 60))
    return " · ".join(before), " · ".join(after)


class QualityReportDialog(ctk.CTkToplevel):
    def __init__(self, master, get_segments: Callable[[], list], apply_plan: Callable[[qf.FixPlan], object],
                 undo: Callable[[], object], jump_to: Callable[[int], object],
                 ask: Callable[[str], bool] | None = None) -> None:
        super().__init__(master)
        self._get_segments, self._apply_plan, self._undo_fn, self._jump_to = get_segments, apply_plan, undo, jump_to
        self._ask = ask or (lambda text: messagebox.askyesno("Terapkan perbaikan?", text, parent=self))
        self.flow = QualityFlow()
        self._report: sq.QualityReport | None = None
        self._view = "report"
        self.title("Laporan Kualitas Subtitle")
        self.configure(fg_color=Theme.SURFACE)
        self.geometry("900x580")
        self.minsize(680, 440)
        self.strict_var = ctk.BooleanVar(value=False)
        self.filter_var = ctk.StringVar(value=FILTERS["all"])
        self._build()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.transient(master.winfo_toplevel())
        self.analyze()
        self.after(150, self._activate)

    # ------------------------------------------------------------------------------------------------------------ UI
    def _build(self) -> None:
        style = ttk.Style(self)
        style.configure("KP.Treeview", background=Theme.SURFACE_2, fieldbackground=Theme.SURFACE_2, foreground=Theme.TEXT,
                        rowheight=26, borderwidth=0, font=("Segoe UI", 10))
        style.configure("KP.Treeview.Heading", background=Theme.BORDER, foreground=Theme.GOLD, relief="flat",
                        font=("Segoe UI", 10, "bold"))
        style.map("KP.Treeview", background=[("selected", Theme.GOLD)], foreground=[("selected", "#121212")])

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 4))
        self.summary_label = ctk.CTkLabel(top, text="", text_color=Theme.TEXT, anchor="w", justify="left",
                                          font=Theme.font(13, "bold"), wraplength=840)
        self.summary_label.pack(fill="x")
        options = ctk.CTkFrame(top, fg_color="transparent")
        options.pack(fill="x", pady=(6, 0))
        ctk.CTkSwitch(options, text="Aturan ketat (durasi 0,83–7 dtk, jeda ≥ 0,083 dtk)", variable=self.strict_var,
                      command=self.analyze, progress_color=Theme.GOLD, text_color=Theme.TEXT_MUTED,
                      font=Theme.font(11)).pack(side="left")
        self.filter_menu = ctk.CTkOptionMenu(options, values=list(FILTERS.values()), variable=self.filter_var, width=150,
                                             height=26, fg_color=Theme.SURFACE_2, button_color=Theme.GOLD_DIM,
                                             button_hover_color=Theme.GOLD, text_color=Theme.TEXT, font=Theme.font(11),
                                             command=self._on_filter)
        self.filter_menu.pack(side="right")

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=14, pady=4)
        self.report_frame = ctk.CTkFrame(self.body, fg_color=Theme.SURFACE_2, corner_radius=Theme.RADIUS)
        self.report_tree = self._make_tree(self.report_frame, (("n", "#", 48, "center", False),
                                                               ("time", "Waktu", 190, "w", False),
                                                               ("level", "Tingkat", 86, "w", False),
                                                               ("issue", "Masalah", 330, "w", True),
                                                               ("text", "Teks", 200, "w", True)))
        self.report_tree.bind("<Double-1>", self._on_open_row)
        self.report_tree.bind("<Return>", self._on_open_row)

        self.preview_frame = ctk.CTkFrame(self.body, fg_color="transparent")
        self.preview_info = ctk.CTkLabel(self.preview_frame, text="", text_color=Theme.TEXT, anchor="w", justify="left",
                                         font=Theme.font(12), wraplength=840)
        self.preview_info.pack(fill="x", pady=(0, 4))
        preview_wrap = ctk.CTkFrame(self.preview_frame, fg_color=Theme.SURFACE_2, corner_radius=Theme.RADIUS)
        preview_wrap.pack(fill="both", expand=True)
        self.preview_tree = self._make_tree(preview_wrap, (("n", "#", 48, "center", False),
                                                           ("before", "Sebelum", 300, "w", True),
                                                           ("after", "Sesudah", 300, "w", True),
                                                           ("note", "Perubahan", 220, "w", True)))

        self.status_label = ctk.CTkLabel(self, text="", text_color=Theme.TEXT_MUTED, anchor="w", justify="left",
                                         font=Theme.font(11), wraplength=860)
        self.status_label.pack(fill="x", padx=16, pady=(2, 0))
        self.button_row = ctk.CTkFrame(self, fg_color="transparent")
        self.button_row.pack(pady=(6, 14))
        self.btn_reanalyze = gold_button(self.button_row, "🔄 Analisis ulang", self.analyze, filled=False, height=34, width=140)
        self.btn_fix = gold_button(self.button_row, "🔧 Perbaiki otomatis…", self._start_fix, height=34, width=190)
        self.btn_undo = gold_button(self.button_row, "↶ Undo perubahan", self._undo, filled=False, height=34, width=160)
        self.btn_back = gold_button(self.button_row, "← Kembali", self._back_to_report, filled=False, height=34, width=120)
        self.btn_apply = gold_button(self.button_row, "✅ Terapkan…", self._confirm_and_apply, height=34, width=230)
        self.btn_close = gold_button(self.button_row, "Tutup", self.destroy, filled=False, height=34, width=90)

    def _make_tree(self, parent, columns) -> ttk.Treeview:
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        tree = ttk.Treeview(parent, columns=[c[0] for c in columns], show="headings", style="KP.Treeview",
                            selectmode="browse")
        for col, title, width, anchor, stretch in columns:
            tree.heading(col, text=title, anchor="w" if anchor != "center" else anchor)
            tree.column(col, width=width, minwidth=48 if not stretch else 100, stretch=stretch, anchor=anchor)
        tree.tag_configure(sq.ERROR, foreground=Theme.DANGER_TEXT)
        tree.tag_configure(sq.WARNING, foreground=Theme.WARN_TEXT)
        bar = ctk.CTkScrollbar(parent, command=tree.yview, button_color=Theme.GOLD_DIM, button_hover_color=Theme.GOLD,
                               fg_color=Theme.SURFACE_2)
        tree.configure(yscrollcommand=bar.set)
        tree.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        bar.grid(row=0, column=1, sticky="ns", pady=4, padx=(0, 2))
        return tree

    def _activate(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------------------------------------------ logika
    def _segments(self) -> list:
        return list(self._get_segments())

    def _rules(self) -> sq.QualityRules:
        return STRICT_RULES if self.strict_var.get() else sq.DEFAULT_RULES

    def _filter_key(self) -> str:
        return next((k for k, v in FILTERS.items() if v == self.filter_var.get()), "all")

    def analyze(self, status: str = "") -> None:
        """Langkah 1 (Analyze): periksa subtitle terkini dan tampilkan laporan. Tidak mengubah apa pun."""
        self._report = self.flow.analyze(self._segments(), self._rules())
        self._render_report()
        self._show("report")
        self.status_label.configure(text=status)

    def _on_filter(self, _value=None) -> None:
        self._render_report()

    def _render_report(self) -> None:
        report, segs = self._report, self._segments()
        assert report is not None
        tree = self.report_tree
        tree.delete(*tree.get_children())
        key, shown, matching = self._filter_key(), 0, 0
        for index, issues in sorted(report.by_index().items()):
            worst = sq.ERROR if any(i.severity == sq.ERROR for i in issues) else sq.WARNING
            if key != "all" and not any(i.severity == key for i in issues):
                continue
            matching += 1                                   # hitung SEMUA yang cocok filter, juga yang tidak ditampilkan
            if shown >= MAX_ROWS:
                continue
            seg = segs[index] if index < len(segs) else None
            tree.insert("", "end", iid=str(index), tags=(worst,), values=(
                index + 1, _span(getattr(seg, "start", None), getattr(seg, "end", None)), LEVEL_TEXT[worst],
                " • ".join(i.message for i in issues), one_line(getattr(seg, "text", ""), 50)))
            shown += 1
        notice = limit_notice(shown, matching, "baris bermasalah",
                              "Gunakan filter, atau perbaiki lalu klik Analisis ulang untuk melihat sisanya.")
        self.summary_label.configure(text=report.summary() + (("\n" + notice) if notice else ""),
                                     text_color=Theme.WARN_TEXT if notice else Theme.TEXT)

    def _show(self, view: str) -> None:
        self._view = view
        self.report_frame.pack_forget()
        self.preview_frame.pack_forget()
        for button in (self.btn_reanalyze, self.btn_fix, self.btn_undo, self.btn_back, self.btn_apply, self.btn_close):
            button.pack_forget()
        if view == "preview":
            self.preview_frame.pack(fill="both", expand=True)
            buttons = (self.btn_back, self.btn_apply)
        else:
            self.report_frame.pack(fill="both", expand=True)
            buttons = [self.btn_reanalyze, self.btn_fix]
            if self.flow.can_undo:
                buttons.append(self.btn_undo)
            buttons.append(self.btn_close)
            has_issues = bool(self._report and not self._report.ok)
            self.btn_fix.configure(state="normal" if has_issues else "disabled")
        for button in buttons:
            button.pack(side="left", padx=6)

    def _on_open_row(self, _event=None) -> None:
        selection = self.report_tree.selection()
        if selection:
            self._jump_to(int(selection[0]))

    # ------------------------------------------------------------------------------------------- Preview Changes
    def _start_fix(self) -> None:
        """Langkah 2 (Preview Changes): susun rencana dan tampilkan sebelum/sesudah. Subtitle TIDAK berubah."""
        try:
            self.analyze()                              # selalu dari kondisi terkini (mis. setelah edit di editor)
            plan = self.flow.preview(self._segments())
        except Exception as exc:  # noqa: BLE001
            log.exception("Pratinjau perbaikan kualitas gagal")
            messagebox.showerror("KaptionPro", f"Gagal menyusun perbaikan: {exc}", parent=self)
            return
        if not plan.has_changes:
            self.status_label.configure(text="Tidak ada yang bisa diperbaiki otomatis. " + plan.summary())
            return
        self._render_preview(plan)
        self._show("preview")

    def _render_preview(self, plan: qf.FixPlan) -> None:
        tree = self.preview_tree
        tree.delete(*tree.get_children())
        flagged = {index for index, _code in plan.introduced}          # baris yang perbaikannya memunculkan masalah baru
        shown_changes = plan.changes[:MAX_ROWS]
        for change in shown_changes:
            before, after = change_columns(change)
            tree.insert("", "end", iid=str(change.index), tags=(sq.WARNING,) if change.index in flagged else (),
                        values=(change.index + 1, before, after, "; ".join(change.notes)))
        all_count = fmt_int(len(plan.changes))
        notice = limit_notice(len(shown_changes), len(plan.changes), "perubahan",
                              f"Tombol Terapkan menerapkan SEMUA {all_count} perubahan, bukan hanya yang tampil.")
        self.preview_info.configure(
            text="Pratinjau: belum ada yang diubah. " + plan.summary() + (("\n" + notice) if notice else ""),
            text_color=Theme.WARN_TEXT if (notice or not plan.is_safe) else Theme.TEXT)
        self.btn_apply.configure(text=f"✅ Terapkan {len(plan.changes)} perubahan…")

    def _back_to_report(self) -> None:
        try:
            self.flow.cancel_preview()
        except FlowError:
            pass
        self.analyze()

    # ------------------------------------------------------------------------ Confirm -> Apply
    def _confirm_and_apply(self) -> None:
        plan = self.flow.plan
        if plan is None or not plan.has_changes:
            return
        question = (f"Terapkan {len(plan.changes)} perubahan ke subtitle?\n\n"
                    "• Backup dibuat otomatis sebelum diterapkan.\n"
                    "• Bisa dibatalkan: tombol \"Undo perubahan\" di sini atau ↶ Undo di editor.")
        if len(plan.changes) > MAX_ROWS:
            question += (f"\n\n⚠ Pratinjau hanya menampilkan {fmt_int(MAX_ROWS)} dari {fmt_int(len(plan.changes))} "
                         f"perubahan; SEMUA {fmt_int(len(plan.changes))} perubahan akan diterapkan.")
        if not plan.is_safe:
            question += (f"\n\n⚠ {len(plan.introduced)} masalah BARU akan muncul (mis. CPS naik karena akhir baris "
                         "dipendekkan demi menghilangkan overlap). Lihat kolom pratinjau.")
        if not self._ask(question):
            return                                       # pengguna menolak: tetap di pratinjau, tidak ada yang berubah
        try:
            self.flow.confirm()
            count = self.flow.apply(self._segments(), self._apply_plan)
        except StalePlanError:
            self.analyze("Subtitle berubah sejak pratinjau; tidak ada yang diterapkan. Silakan periksa ulang.")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Penerapan perbaikan kualitas gagal")
            verdict = self._verdict_after_failed_apply(plan)
            messagebox.showerror("KaptionPro", f"Perbaikan gagal diterapkan: {exc}\n\n{verdict}", parent=self)
            self.analyze(f"Perbaikan gagal diterapkan. {verdict}")
            return
        self._after_apply(count)

    def _verdict_after_failed_apply(self, plan: qf.FixPlan) -> str:
        """Kalimat JUJUR soal kondisi subtitle setelah penerapan gagal: dibandingkan dengan rencana, bukan diasumsikan."""
        try:
            unchanged = plan.matches(self._segments())
        except Exception:  # noqa: BLE001
            log.exception("Verifikasi kondisi subtitle setelah penerapan gagal ikut gagal")
            return "⚠ Kondisi subtitle tidak bisa diverifikasi; periksa hasilnya dan gunakan ↶ Undo di editor bila perlu."
        if unchanged:
            return "Subtitle TIDAK berubah (sudah diverifikasi sama dengan sebelum perbaikan)."
        return "⚠ Subtitle berbeda dari sebelum perbaikan. Periksa hasilnya dan gunakan ↶ Undo di editor bila perlu."

    def _after_apply(self, count: int) -> None:
        """Tampilkan laporan terbaru TANPA mereset alur (supaya Undo tetap tersedia)."""
        self._report = sq.check_subtitles(self._segments(), self.flow.rules)
        self._render_report()
        self._show("report")
        self.status_label.configure(
            text=f"✔ {count} baris diperbaiki. Belum puas? Klik \"Undo perubahan\" (atau ↶ Undo di editor).")

    # ------------------------------------------------------------------------------------------------- Undo
    def _undo(self) -> None:
        if not self.flow.can_undo:
            return
        try:
            restored = self.flow.undo(self._segments, self._undo_fn)
        except UndoMismatchError as exc:
            messagebox.showwarning(
                "KaptionPro", f"Undo tidak dijalankan: {exc}.\n\nSubtitle tidak diubah. Batalkan perubahan lain itu dulu "
                              "dengan ↶ Undo di editor, atau klik Analisis ulang.", parent=self)
            self.analyze("Undo tidak dijalankan: ada perubahan lain sesudah perbaikan. "
                         "Laporan di atas adalah kondisi sebenarnya.")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Undo perbaikan kualitas gagal")
            messagebox.showerror("KaptionPro", f"Undo gagal: {exc}", parent=self)
            self.analyze()
            return
        self.analyze("↶ Perubahan dibatalkan; subtitle kembali seperti sebelum diperbaiki." if restored else
                     "⚠ Undo tidak mengembalikan kondisi semula (riwayat Undo habis atau subtitle sudah berubah). "
                     "Laporan di atas adalah kondisi sebenarnya.")
