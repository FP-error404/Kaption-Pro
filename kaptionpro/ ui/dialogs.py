"""Semua dialog: prompt, cari/ganti, gaya, export, backup, edit massal, panduan."""

from __future__ import annotations

import copy
import re
import shutil
import tkinter as tk
from tkinter import messagebox
from typing import Callable

from ..config import (
    EXPORT_QUALITIES,
    HAS_ARGOS,
    HAS_DEEP_TRANSLATOR,
    HAS_FASTER_WHISPER,
    HAS_VLC,
    Theme,
    TRANSLATORS,
    X264_PRESETS,
    ctk,
    log,
)
from ..core.privacy import ACK_KEY, summary_text
from ..core.storage import BackupInfo
from ..services.ai_service import BACKENDS
from ..core.utils import is_valid_hex, normalize_hex
from ..models import ExportOptions, SubtitleSegment, SubtitleStyle
from .components import gold_button, small_button


class PromptDialog(ctk.CTkToplevel):
    def __init__(self, master, title: str, prompt: str, initial: str = "") -> None:
        super().__init__(master)
        self.title(title)
        self.configure(fg_color=Theme.SURFACE)
        self.resizable(False, False)
        self.result: str | None = None
        ctk.CTkLabel(self, text=prompt, text_color=Theme.TEXT, justify="left",
                     font=Theme.font(12)).pack(padx=20, pady=(18, 6), anchor="w")
        self.entry = ctk.CTkEntry(self, width=320, fg_color=Theme.SURFACE_2, border_color=Theme.GOLD_DIM,
                                  text_color=Theme.TEXT)
        self.entry.insert(0, initial)
        self.entry.pack(padx=20, pady=4)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=(10, 16))
        gold_button(row, "OK", self._ok, height=32, width=90).pack(side="left", padx=6)
        gold_button(row, "Cancel", self.destroy, filled=False, height=32, width=90).pack(side="left", padx=6)
        self.entry.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.transient(master.winfo_toplevel())
        top = master.winfo_toplevel()
        self.geometry(f"+{top.winfo_rootx() + 80}+{top.winfo_rooty() + 120}")
        self.after(150, self._activate)

    def _activate(self) -> None:
        try:
            self.grab_set()
            self.entry.focus_set()
        except tk.TclError:
            pass

    def _ok(self) -> None:
        self.result = self.entry.get().strip()
        self.destroy()

    def ask(self) -> str | None:
        self.wait_window()
        return self.result


def ask_text(master, title: str, prompt: str, initial: str = "") -> str | None:
    return PromptDialog(master, title, prompt, initial).ask()


# ---------- Search & Replace ----------
class SearchReplaceDialog(ctk.CTkToplevel):
    def __init__(self, master, segments: list[SubtitleSegment],
                 on_apply: Callable[[list[SubtitleSegment]], None],
                 on_highlight: Callable[[list[int]], None]) -> None:
        super().__init__(master)
        self.title("Cari & Ganti")
        self.configure(fg_color=Theme.SURFACE)
        self.resizable(False, False)
        self.segments = segments
        self.on_apply = on_apply
        self.on_highlight = on_highlight
        self.hits: list[int] = []

        ctk.CTkLabel(self, text="Cari", text_color=Theme.TEXT_MUTED, font=Theme.font(11)).grid(
            row=0, column=0, sticky="w", padx=16, pady=(16, 2))
        self.find_entry = ctk.CTkEntry(self, width=280, fg_color=Theme.SURFACE_2,
                                       border_color=Theme.GOLD_DIM, text_color=Theme.TEXT)
        self.find_entry.grid(row=1, column=0, padx=16)

        ctk.CTkLabel(self, text="Ganti dengan", text_color=Theme.TEXT_MUTED, font=Theme.font(11)).grid(
            row=2, column=0, sticky="w", padx=16, pady=(10, 2))
        self.repl_entry = ctk.CTkEntry(self, width=280, fg_color=Theme.SURFACE_2,
                                       border_color=Theme.GOLD_DIM, text_color=Theme.TEXT)
        self.repl_entry.grid(row=3, column=0, padx=16)

        self.case_var = ctk.BooleanVar(value=False)
        self.regex_var = ctk.BooleanVar(value=False)
        opts = ctk.CTkFrame(self, fg_color="transparent")
        opts.grid(row=4, column=0, sticky="ew", padx=16, pady=(10, 0))
        ctk.CTkCheckBox(opts, text="Case sensitive", variable=self.case_var, fg_color=Theme.GOLD,
                        hover_color=Theme.GOLD_HOVER, border_color=Theme.GOLD_DIM,
                        text_color=Theme.TEXT, font=Theme.font(11)).pack(anchor="w")
        ctk.CTkCheckBox(opts, text="Regex", variable=self.regex_var, fg_color=Theme.GOLD,
                        hover_color=Theme.GOLD_HOVER, border_color=Theme.GOLD_DIM,
                        text_color=Theme.TEXT, font=Theme.font(11)).pack(anchor="w", pady=(2, 0))

        self.info = ctk.CTkLabel(self, text="", text_color=Theme.TEXT_MUTED, font=Theme.font(11))
        self.info.grid(row=5, column=0, sticky="w", padx=16, pady=(8, 0))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=6, column=0, pady=(12, 16))
        gold_button(btns, "Cari", self._find, height=32, width=90).pack(side="left", padx=4)
        gold_button(btns, "Ganti", self._replace, filled=False, height=32, width=90).pack(side="left", padx=4)
        gold_button(btns, "Tutup", self.destroy, filled=False, height=32, width=90).pack(side="left", padx=4)

        self.bind("<Escape>", lambda _e: self.destroy())
        self.transient(master.winfo_toplevel())
        self.after(150, self._activate)

    def _activate(self) -> None:
        try:
            self.grab_set()
            self.find_entry.focus_set()
        except tk.TclError:
            pass

    def _pattern(self) -> re.Pattern | None:
        q = self.find_entry.get()
        if not q:
            return None
        flags = 0 if self.case_var.get() else re.IGNORECASE
        try:
            return re.compile(q if self.regex_var.get() else re.escape(q), flags)
        except re.error as e:
            self.info.configure(text=f"Regex invalid: {e}", text_color=Theme.DANGER_TEXT)
            return None

    def _find(self) -> None:
        pat = self._pattern()
        if not pat:
            self.hits = []
            self.info.configure(text="Kata kunci kosong.", text_color=Theme.TEXT_MUTED)
            self.on_highlight([])
            return
        self.hits = [i for i, s in enumerate(self.segments) if pat.search(s.text)]
        self.info.configure(text=f"{len(self.hits)} kecocokan.",
                            text_color=Theme.OK if self.hits else Theme.TEXT_MUTED)
        self.on_highlight(list(self.hits))

    def _replace(self) -> None:
        q = self.find_entry.get()
        if not q:
            return
        repl = self.repl_entry.get()
        count = 0
        new_list: list[SubtitleSegment] = []
        if self.regex_var.get():
            pat = self._pattern()
            if not pat:
                return
            for s in self.segments:
                new_text, n = pat.subn(repl, s.text)
                if n:
                    count += n
                    s = SubtitleSegment(s.start, s.end, new_text, [])
                new_list.append(s)
        else:
            # Plain text: pakai str.replace — aman dari backslash dan \1 pada repl.
            if self.case_var.get():
                for s in self.segments:
                    n = s.text.count(q)
                    if n:
                        count += n
                        s = SubtitleSegment(s.start, s.end, s.text.replace(q, repl), [])
                    new_list.append(s)
            else:
                pat = re.compile(re.escape(q), re.IGNORECASE)
                # Gunakan lambda agar repl diperlakukan sebagai literal.
                for s in self.segments:
                    new_text, n = pat.subn(lambda _m: repl, s.text)
                    if n:
                        count += n
                        s = SubtitleSegment(s.start, s.end, new_text, [])
                    new_list.append(s)
        if count:
            self.on_apply(new_list)
        self.info.configure(text=f"{count} penggantian diterapkan.",
                            text_color=Theme.OK if count else Theme.TEXT_MUTED)


# ---------- Style Dialog (dengan validasi + Cancel revert) ----------
class StyleDialog(ctk.CTkToplevel):
    def __init__(self, master, current: SubtitleStyle,
                 on_apply: Callable[[SubtitleStyle], None],
                 preview_cb: Callable[[SubtitleStyle], None] | None = None,
                 on_close: Callable[[], None] | None = None) -> None:
        super().__init__(master)
        self.title("Pengaturan Gaya Subtitle")
        self.on_close = on_close
        self.configure(fg_color=Theme.SURFACE)
        self.resizable(False, False)
        self.on_apply = on_apply
        self.preview_cb = preview_cb
        self.original = copy.copy(current)
        self.style = copy.copy(current)

        container = ctk.CTkFrame(self, fg_color="transparent")
        container.pack(padx=18, pady=(14, 4), fill="both")

        def row(label: str, widget_fn, row_idx: int) -> None:
            ctk.CTkLabel(container, text=label, text_color=Theme.TEXT_MUTED,
                         font=Theme.font(11), width=140, anchor="w").grid(
                row=row_idx, column=0, sticky="w", pady=4)
            widget_fn(row_idx)

        # Font
        self.font_var = ctk.StringVar(value=self.style.font_name)
        row("Font", lambda r: ctk.CTkEntry(container, width=200, textvariable=self.font_var,
                                            fg_color=Theme.SURFACE_2, border_color=Theme.GOLD_DIM,
                                            text_color=Theme.TEXT).grid(row=r, column=1, sticky="ew"), 0)

        self.size_var = ctk.DoubleVar(value=self.style.font_size_pct)
        row("Ukuran (% tinggi)", lambda r: self._slider(container, self.size_var, 2.0, 12.0, r), 1)

        self.outline_var = ctk.DoubleVar(value=self.style.outline_width)
        row("Outline", lambda r: self._slider(container, self.outline_var, 0.0, 6.0, r), 2)

        self.shadow_var = ctk.DoubleVar(value=self.style.shadow)
        row("Shadow", lambda r: self._slider(container, self.shadow_var, 0.0, 6.0, r), 3)

        self.margin_var = ctk.DoubleVar(value=self.style.margin_v_pct)
        row("Margin bawah (%)", lambda r: self._slider(container, self.margin_var, 0.0, 25.0, r), 4)

        self.pos_var = ctk.StringVar(value=self.style.position)
        pos_frame = ctk.CTkFrame(container, fg_color="transparent")
        pos_frame.grid(row=5, column=1, sticky="w")
        for key, label in (("bottom", "Bottom"), ("middle", "Middle"), ("top", "Top")):
            ctk.CTkRadioButton(pos_frame, text=label, value=key, variable=self.pos_var,
                               fg_color=Theme.GOLD, hover_color=Theme.GOLD_HOVER,
                               border_color=Theme.GOLD_DIM, text_color=Theme.TEXT,
                               font=Theme.font(11),
                               command=self._live_preview).pack(side="left", padx=4)
        ctk.CTkLabel(container, text="Posisi", text_color=Theme.TEXT_MUTED,
                     font=Theme.font(11), width=140, anchor="w").grid(row=5, column=0, sticky="w", pady=4)

        self.primary_var = ctk.StringVar(value=self.style.primary_hex)
        self.secondary_var = ctk.StringVar(value=self.style.secondary_hex)
        self.outline_hex_var = ctk.StringVar(value=self.style.outline_hex)
        for i, (label, var) in enumerate((("Warna utama", self.primary_var),
                                          ("Warna karaoke nonaktif", self.secondary_var),
                                          ("Warna outline", self.outline_hex_var)), start=6):
            ctk.CTkLabel(container, text=label, text_color=Theme.TEXT_MUTED,
                         font=Theme.font(11), width=140, anchor="w").grid(row=i, column=0, sticky="w", pady=4)
            ctk.CTkEntry(container, width=200, textvariable=var, fg_color=Theme.SURFACE_2,
                         border_color=Theme.GOLD_DIM, text_color=Theme.TEXT).grid(row=i, column=1, sticky="ew")

        self.bold_var = ctk.BooleanVar(value=self.style.bold)
        ctk.CTkCheckBox(container, text="Bold", variable=self.bold_var, fg_color=Theme.GOLD,
                        hover_color=Theme.GOLD_HOVER, border_color=Theme.GOLD_DIM,
                        text_color=Theme.TEXT, font=Theme.font(11),
                        command=self._live_preview).grid(row=9, column=1, sticky="w", pady=4)

        self.err_label = ctk.CTkLabel(self, text="", text_color=Theme.DANGER_TEXT,
                                      font=Theme.font(11))
        self.err_label.pack(anchor="w", padx=20)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=(4, 14))
        gold_button(btns, "Terapkan", self._apply, height=34, width=110).pack(side="left", padx=4)
        gold_button(btns, "Batal", self._cancel, filled=False, height=34, width=110).pack(side="left", padx=4)

        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.transient(master.winfo_toplevel())
        self.after(200, self._activate)

    def _slider(self, parent, var: ctk.DoubleVar, lo: float, hi: float, row: int) -> None:
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.grid(row=row, column=1, sticky="ew")
        f.grid_columnconfigure(0, weight=1)
        lbl = ctk.CTkLabel(f, text=f"{var.get():.2f}", text_color=Theme.TEXT,
                           font=Theme.font(11), width=52)
        s = ctk.CTkSlider(f, from_=lo, to=hi, variable=var, progress_color=Theme.GOLD,
                          button_color=Theme.GOLD_BRIGHT, button_hover_color=Theme.GOLD_HOVER,
                          fg_color=Theme.SURFACE_2)
        s.grid(row=0, column=0, sticky="ew")
        lbl.grid(row=0, column=1, padx=(6, 0))
        s.configure(command=lambda v, lb=lbl: (lb.configure(text=f"{float(v):.2f}"), self._live_preview()))

    _families_cache: set[str] | None = None

    def _font_installed(self, name: str) -> bool:
        """Cek nama font terhadap daftar font sistem (di-cache; True bila tidak bisa dicek)."""
        if StyleDialog._families_cache is None:
            try:
                from tkinter import font as tkfont
                StyleDialog._families_cache = {f.lower() for f in tkfont.families()}
            except Exception:  # noqa: BLE001
                StyleDialog._families_cache = set()
        fams = StyleDialog._families_cache
        return (not fams) or name.lower() in fams

    def _collect(self) -> SubtitleStyle | None:
        if not is_valid_hex(self.primary_var.get()):
            self.err_label.configure(text="Warna utama bukan #RRGGBB.")
            return None
        if not is_valid_hex(self.secondary_var.get()):
            self.err_label.configure(text="Warna karaoke nonaktif bukan #RRGGBB.")
            return None
        if not is_valid_hex(self.outline_hex_var.get()):
            self.err_label.configure(text="Warna outline bukan #RRGGBB.")
            return None
        font = self.font_var.get().strip() or "Arial"
        note = ""
        if "," in font:       # koma = pemisah kolom di baris Style ASS -> bersihkan, jangan diam-diam
            font = " ".join(p.strip() for p in font.split(",") if p.strip()) or "Arial"
            self.font_var.set(font)
            note = "Koma pada nama font dihapus (koma = pemisah kolom ASS)."
        elif not self._font_installed(font):
            note = f"Font '{font}' tidak ditemukan di sistem — export memakai font pengganti."
        self.err_label.configure(text=note)
        try:
            return SubtitleStyle(
                font_name=font,
                font_size_pct=float(self.size_var.get()),
                bold=bool(self.bold_var.get()),
                primary_hex=normalize_hex(self.primary_var.get()),
                secondary_hex=normalize_hex(self.secondary_var.get()),
                outline_hex=normalize_hex(self.outline_hex_var.get()),
                outline_width=float(self.outline_var.get()),
                shadow=float(self.shadow_var.get()),
                margin_v_pct=float(self.margin_var.get()),
                position=self.pos_var.get(),
                back_alpha_pct=self.original.back_alpha_pct,
            )
        except Exception:  # noqa: BLE001
            self.err_label.configure(text="Nilai tidak valid.")
            return None

    def _live_preview(self) -> None:
        if not self.preview_cb:
            return
        s = self._collect()
        if s is not None:
            self.preview_cb(s)

    def _activate(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _apply(self) -> None:
        s = self._collect()
        if s is None:
            return
        self.on_apply(s)
        self._finish()

    def _cancel(self) -> None:
        if self.preview_cb:
            self.preview_cb(self.original)   # kembalikan live preview ke style semula
        self._finish()

    def _finish(self) -> None:
        if self.on_close:
            try:
                self.on_close()
            except Exception:  # noqa: BLE001
                log.exception("on_close StyleDialog gagal")
        self.destroy()


# ---------- Guide ----------
BULK_OPS = {
    "shift": "Geser waktu (detik)",
    "replace": "Ganti teks (cari → ganti)",
    "spaces": "Rapikan spasi",
    "upper": "HURUF BESAR",
    "lower": "huruf kecil",
    "title": "Huruf Awal Kapital",
    "mindur": "Durasi minimum (detik)",
    "striptags": "Hapus tag <i> <b> <font> / {\\an..}",
}
_BULK_HINTS = {
    "shift": ("Detik (mis. 1.5 atau -0.5)", ""),
    "replace": ("Cari", "Ganti dengan"),
    "mindur": ("Detik (mis. 1.2)", ""),
}


class ExportOptionsDialog(ctk.CTkToplevel):
    """Opsi export MP4: preset kualitas, CRF, preset encoding, audio."""

    AUDIO_LABELS = {"auto": "Otomatis (copy bila kompatibel MP4)", "copy": "Selalu copy", "aac": "Encode AAC 192k"}

    def __init__(self, master, options: ExportOptions, info_text: str = "") -> None:
        super().__init__(master)
        self.title("Opsi Export MP4")
        self.configure(fg_color=Theme.SURFACE)
        self.resizable(False, False)
        self.result: ExportOptions | None = None
        o = options.validated()
        self.quality_var = ctk.StringVar(value=EXPORT_QUALITIES[o.quality])
        self.preset_var = ctk.StringVar(value=o.preset)
        self.audio_var = ctk.StringVar(value=self.AUDIO_LABELS[o.audio])
        self.crf_var = ctk.DoubleVar(value=o.crf)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(padx=20, pady=(16, 6), fill="x")
        body.grid_columnconfigure(1, weight=1)

        def label(text: str, row: int) -> None:
            ctk.CTkLabel(body, text=text, text_color=Theme.TEXT_MUTED, font=Theme.font(11), width=110,
                         anchor="w").grid(row=row, column=0, sticky="w", pady=5)

        menu_kw = dict(fg_color=Theme.SURFACE_2, button_color=Theme.GOLD_DIM, button_hover_color=Theme.GOLD,
                       text_color=Theme.TEXT, font=Theme.font(12), width=240)
        label("Kualitas", 0)
        ctk.CTkOptionMenu(body, values=list(EXPORT_QUALITIES.values()), variable=self.quality_var,
                          **menu_kw).grid(row=0, column=1, sticky="w")
        label("CRF (x264)", 1)
        crf_row = ctk.CTkFrame(body, fg_color="transparent")
        crf_row.grid(row=1, column=1, sticky="w")
        self.crf_label = ctk.CTkLabel(crf_row, text=str(int(o.crf)), text_color=Theme.TEXT, width=30,
                                      font=Theme.font(12, "bold"))
        ctk.CTkSlider(crf_row, from_=14, to=30, number_of_steps=16, variable=self.crf_var, width=190,
                      progress_color=Theme.GOLD, button_color=Theme.GOLD_BRIGHT,
                      button_hover_color=Theme.GOLD_HOVER, fg_color=Theme.SURFACE_2,
                      command=lambda v: self.crf_label.configure(text=str(int(round(float(v)))))).pack(side="left")
        self.crf_label.pack(side="left", padx=(8, 0))
        label("Preset encoding", 2)
        ctk.CTkOptionMenu(body, values=X264_PRESETS, variable=self.preset_var, **menu_kw).grid(row=2, column=1, sticky="w")
        label("Audio", 3)
        ctk.CTkOptionMenu(body, values=list(self.AUDIO_LABELS.values()), variable=self.audio_var,
                          **menu_kw).grid(row=3, column=1, sticky="w")
        ctk.CTkLabel(self, text="CRF kecil = kualitas lebih tinggi & file lebih besar (18 ≈ nyaris tanpa\n"
                                "kehilangan, 23 ≈ standar). Preset lambat = file lebih kecil, proses lebih lama.\n"
                                "Resolusi hanya diperkecil, tidak pernah di-upscale." + (f"\n\n{info_text}" if info_text else ""),
                     text_color=Theme.TEXT_MUTED, font=Theme.font(10), justify="left").pack(padx=20, pady=(2, 8), anchor="w")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=(4, 16))
        gold_button(row, "Lanjut Export", self._ok, height=34, width=130).pack(side="left", padx=6)
        gold_button(row, "Batal", self.destroy, filled=False, height=34, width=90).pack(side="left", padx=6)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.transient(master.winfo_toplevel())
        self.after(150, self._activate)

    def _activate(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _ok(self) -> None:
        q = next((k for k, v in EXPORT_QUALITIES.items() if v == self.quality_var.get()), "original")
        a = next((k for k, v in self.AUDIO_LABELS.items() if v == self.audio_var.get()), "auto")
        self.result = ExportOptions(q, int(round(float(self.crf_var.get()))), self.preset_var.get(), a).validated()
        self.destroy()

    def ask(self) -> ExportOptions | None:
        self.wait_window()
        return self.result


class BackupDialog(ctk.CTkToplevel):
    """Daftar backup berversi project; pilih satu untuk dipulihkan (bisa di-Undo)."""

    def __init__(self, master, backups: list[BackupInfo], on_restore: Callable[[BackupInfo], None]) -> None:
        super().__init__(master)
        self.title("Backup & Pemulihan")
        self.configure(fg_color=Theme.SURFACE)
        self.geometry("520x420")
        self.transient(master.winfo_toplevel())
        ctk.CTkLabel(self, text="BACKUP PROJECT", text_color=Theme.GOLD_BRIGHT, anchor="w",
                     font=Theme.font(15, "bold", serif=True)).pack(fill="x", padx=18, pady=(16, 2))
        ctk.CTkLabel(self, text="Backup dibuat otomatis (maks. tiap 5 menit bila ada perubahan) dan sebelum operasi "
                                "besar (AI, import, Auto-Fix, edit massal, pemulihan). Disimpan 15 versi terakhir.",
                     text_color=Theme.TEXT_MUTED, font=Theme.font(10), wraplength=470, justify="left",
                     anchor="w").pack(fill="x", padx=18, pady=(0, 8))
        box = ctk.CTkScrollableFrame(self, fg_color=Theme.SURFACE_2, corner_radius=10,
                                     scrollbar_button_color=Theme.GOLD_DIM)
        box.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        if not backups:
            ctk.CTkLabel(box, text="Belum ada backup untuk project ini.", text_color=Theme.TEXT_MUTED,
                         font=Theme.font(12)).pack(pady=24)
        for info in backups:
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", padx=6, pady=3)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(row, text=f"{info.saved_at}  ·  {info.reason}  ·  {info.lines} baris", anchor="w",
                         text_color=Theme.TEXT, font=Theme.font(11)).grid(row=0, column=0, sticky="ew")
            small_button(row, "Pulihkan", lambda i=info: (on_restore(i), self.destroy()), 80).grid(row=0, column=1)
        gold_button(self, "Tutup", self.destroy, filled=False, height=32, width=100).pack(pady=(0, 14))
        self.after(150, self._activate)

    def _activate(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            pass


class BulkEditDialog(ctk.CTkToplevel):
    """Edit massal: geser waktu, ganti teks, rapikan spasi, ubah huruf, durasi minimum, hapus tag."""

    def __init__(self, master, n_selected: int, n_total: int,
                 on_apply: Callable[[str, str, str, str], None]) -> None:
        super().__init__(master)
        self.title("Edit Massal")
        self.configure(fg_color=Theme.SURFACE)
        self.resizable(False, False)
        self.on_apply = on_apply
        self.op_var = ctk.StringVar(value=BULK_OPS["shift"])
        self.scope_var = ctk.StringVar(value="selection" if n_selected > 0 else "all")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(padx=20, pady=(16, 6), fill="x")
        ctk.CTkLabel(body, text="Operasi", text_color=Theme.TEXT_MUTED, font=Theme.font(11), anchor="w").pack(fill="x")
        ctk.CTkOptionMenu(body, values=list(BULK_OPS.values()), variable=self.op_var, width=300,
                          fg_color=Theme.SURFACE_2, button_color=Theme.GOLD_DIM, button_hover_color=Theme.GOLD,
                          text_color=Theme.TEXT, font=Theme.font(12), command=self._on_op).pack(fill="x", pady=(2, 8))
        self.p1_label = ctk.CTkLabel(body, text="", text_color=Theme.TEXT_MUTED, font=Theme.font(11), anchor="w")
        self.p1_label.pack(fill="x")
        self.p1 = ctk.CTkEntry(body, width=300, fg_color=Theme.SURFACE_2, border_color=Theme.GOLD_DIM, text_color=Theme.TEXT)
        self.p1.pack(fill="x", pady=(2, 6))
        self.p2_label = ctk.CTkLabel(body, text="", text_color=Theme.TEXT_MUTED, font=Theme.font(11), anchor="w")
        self.p2_label.pack(fill="x")
        self.p2 = ctk.CTkEntry(body, width=300, fg_color=Theme.SURFACE_2, border_color=Theme.GOLD_DIM, text_color=Theme.TEXT)
        self.p2.pack(fill="x", pady=(2, 8))
        scope = ctk.CTkFrame(body, fg_color="transparent")
        scope.pack(fill="x")
        for key, text in (("selection", f"Baris terpilih ({n_selected})"), ("all", f"Semua baris ({n_total})")):
            ctk.CTkRadioButton(scope, text=text, value=key, variable=self.scope_var, fg_color=Theme.GOLD,
                               hover_color=Theme.GOLD_HOVER, border_color=Theme.GOLD_DIM, text_color=Theme.TEXT,
                               font=Theme.font(11)).pack(anchor="w", pady=2)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=(8, 16))
        gold_button(row, "Terapkan", self._apply, height=34, width=110).pack(side="left", padx=6)
        gold_button(row, "Tutup", self.destroy, filled=False, height=34, width=90).pack(side="left", padx=6)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.transient(master.winfo_toplevel())
        self._on_op(self.op_var.get())
        self.after(150, self._activate)

    def _op_key(self) -> str:
        return next((k for k, v in BULK_OPS.items() if v == self.op_var.get()), "shift")

    def _on_op(self, _value=None) -> None:
        h1, h2 = _BULK_HINTS.get(self._op_key(), ("(tidak perlu parameter)", ""))
        self.p1_label.configure(text=h1)
        self.p2_label.configure(text=h2)

    def _activate(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _apply(self) -> None:
        self.on_apply(self._op_key(), self.p1.get(), self.p2.get(), self.scope_var.get())


class GuideDialog(ctk.CTkToplevel):
    def __init__(self, master) -> None:
        super().__init__(master)
        self.title("Panduan & Pemeriksaan")
        self.configure(fg_color=Theme.SURFACE)
        self.geometry("700x800")
        self.transient(master.winfo_toplevel())

        ctk.CTkLabel(self, text="PANDUAN INSTALASI", text_color=Theme.GOLD_BRIGHT, anchor="w",
                     font=Theme.font(15, "bold", serif=True)).pack(fill="x", padx=18, pady=(16, 4))
        guide = ctk.CTkTextbox(self, fg_color=Theme.SURFACE_2, text_color=Theme.TEXT,
                               font=Theme.font(12), wrap="word", height=230)
        guide.pack(fill="x", padx=18)
        guide.insert("1.0",
            "Wajib:\n"
            "    pip install \"customtkinter>=5.2,<6\"\n\n"
            "AI transkripsi (Whisper) & terjemahan ke Bahasa Indonesia:\n"
            "    pip install faster-whisper deep-translator\n\n"
            "Preview video nyata (embed):\n"
            "    pip install python-vlc  # + VLC desktop terpasang\n\n"
            "Export MP4 (FFmpeg + ffprobe di PATH):\n"
            "    Windows : https://www.gyan.dev/ffmpeg/builds/\n"
            "    macOS   : brew install ffmpeg\n"
            "    Linux   : sudo apt install ffmpeg\n\n"
            "Model Whisper diunduh saat pertama Generate. Untuk CPU, 'small' = seimbang.\n"
            "Untuk GPU kelas menengah, 'medium' atau 'large-v3' lebih akurat.\n\n"
            "Penerjemah (pilih di panel samping):\n"
            "    Google / MyMemory : pip install deep-translator (butuh internet; hasil di-cache permanen)\n"
            "    Argos (offline)   : pip install argostranslate + pasang paket bahasa (mis. ko→en, en→id)\n\n"
            "SHORTCUT KEYBOARD\n"
            "    Spasi            putar / jeda          Ctrl+S         simpan + backup\n"
            "    ← / →            mundur / maju 1 frame Shift+← / →    mundur / maju 1 detik\n"
            "    Ctrl+← / →       subtitle sebelumnya / berikutnya (di timeline)\n"
            "    Alt+↑ / ↓        baris sebelumnya / berikutnya di editor\n"
            "    Ctrl+Enter       apply & lanjut baris  Ctrl+F         cari teks\n"
            "    Ctrl+Z / Ctrl+Y  undo / redo           Delete         hapus baris terpilih\n"
            "(Spasi/panah tidak berfungsi sebagai shortcut saat kursor ada di kolom teks.)"
        )
        guide.configure(state="disabled")

        ctk.CTkLabel(self, text="PEMERIKSAAN DEPENDENSI", text_color=Theme.GOLD_BRIGHT, anchor="w",
                     font=Theme.font(15, "bold", serif=True)).pack(fill="x", padx=18, pady=(14, 4))
        self.checks_frame = ctk.CTkFrame(self, fg_color=Theme.SURFACE_2, corner_radius=10)
        self.checks_frame.pack(fill="both", expand=True, padx=18, pady=(0, 10))

        ctk.CTkLabel(self, text="PRIVASI TERJEMAHAN", text_color=Theme.GOLD_BRIGHT, anchor="w",
                     font=Theme.font(15, "bold", serif=True)).pack(fill="x", padx=18, pady=(4, 4))
        self.privacy_label = ctk.CTkLabel(self, text="", text_color=Theme.TEXT_MUTED, font=Theme.font(11),
                                          anchor="w", justify="left", wraplength=650)
        self.privacy_label.pack(fill="x", padx=18)
        prow = ctk.CTkFrame(self, fg_color="transparent")
        prow.pack(pady=(6, 10))
        gold_button(prow, "Hapus cache terjemahan", self._clear_cache, filled=False, height=30,
                    width=190).pack(side="left", padx=4)
        gold_button(prow, "Cabut persetujuan online", self._revoke_online, filled=False, height=30,
                    width=200).pack(side="left", padx=4)
        self._refresh_privacy()

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=(0, 16))
        gold_button(row, "Periksa Sekarang", self._run_checks, height=34, width=150).pack(side="left", padx=4)
        gold_button(row, "Tutup", self.destroy, filled=False, height=34, width=100).pack(side="left", padx=4)
        self._run_checks()

    def _app(self):
        return self.master.winfo_toplevel()          # KaptionProApp: punya .settings dan .ai

    def _refresh_privacy(self) -> None:
        try:
            app = self._app()
            labels = {k: TRANSLATORS.get(k, k) for k, b in BACKENDS.items() if getattr(b, "online", False)}
            text = summary_text(app.settings.get(ACK_KEY, {}), labels, app.ai.cache.path, app.ai.cache.size_bytes())
        except Exception:  # noqa: BLE001 - dialog informasi tidak boleh menjatuhkan aplikasi
            log.exception("Ringkasan privasi gagal")
            text = "Informasi privasi tidak tersedia."
        self.privacy_label.configure(text=text)

    def _clear_cache(self) -> None:
        if not messagebox.askyesno("KaptionPro", "Hapus SEMUA cache terjemahan?\n"
                                   "Subtitle project tidak terpengaruh; terjemahan hanya perlu dibuat ulang.",
                                   parent=self):
            return
        try:
            self._app().ai.clear_cache()
        except Exception:  # noqa: BLE001
            log.exception("Hapus cache gagal")
            messagebox.showerror("KaptionPro", "Cache tidak bisa dihapus (lihat log).", parent=self)
        self._refresh_privacy()

    def _revoke_online(self) -> None:
        self._app().settings.set(ACK_KEY, {})
        self._refresh_privacy()
        messagebox.showinfo("KaptionPro", "Persetujuan dicabut. Anda akan ditanya lagi sebelum memakai "
                            "penerjemah online berikutnya.", parent=self)

    def _run_checks(self) -> None:
        for w in self.checks_frame.winfo_children():
            w.destroy()

        def line(name: str, ok: bool, detail: str) -> None:
            f = ctk.CTkFrame(self.checks_frame, fg_color="transparent")
            f.pack(fill="x", padx=12, pady=3)
            ctk.CTkLabel(f, text=("✔" if ok else "✘"),
                         text_color=Theme.OK if ok else Theme.DANGER_TEXT,
                         font=Theme.font(14, "bold"), width=22).pack(side="left")
            ctk.CTkLabel(f, text=name, text_color=Theme.TEXT, font=Theme.font(12, "bold"),
                         width=170, anchor="w").pack(side="left")
            ctk.CTkLabel(f, text=detail, text_color=Theme.TEXT_MUTED, font=Theme.font(11),
                         anchor="w").pack(side="left", fill="x", expand=True)

        line("CustomTkinter", True, "OK")
        line("faster-whisper", HAS_FASTER_WHISPER,
             "OK" if HAS_FASTER_WHISPER else "pip install faster-whisper")
        line("deep-translator", HAS_DEEP_TRANSLATOR,
             "OK" if HAS_DEEP_TRANSLATOR else "pip install deep-translator")
        line("argostranslate", HAS_ARGOS,
             "OK (penerjemah offline)" if HAS_ARGOS else "opsional: pip install argostranslate")
        line("python-vlc", HAS_VLC, "OK (embed aktif)" if HAS_VLC
             else "pip install python-vlc  (fallback: simulasi)")
        ff = shutil.which("ffmpeg")
        fp = shutil.which("ffprobe")
        line("ffmpeg", bool(ff), ff or "tidak ditemukan di PATH")
        line("ffprobe", bool(fp), fp or "tidak ditemukan di PATH")
