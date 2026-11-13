"""Panel editor subtitle."""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from collections import Counter
from contextlib import contextmanager
from typing import Callable, NamedTuple

from ..config import Theme, ctk, log
from ..core import quality_fix as qf
from ..core.quality_flow import QUALITY_FIX_LABEL, ApplyVerificationError, StalePlanError, UndoMismatchError
from ..core.utils import srt_time
from ..core.word_integrity import WordIntegrityError, WordRepair, enforce_word_integrity, find_word_violations
from ..models import SubtitleIO, SubtitleSegment, Word, retime_words, sanitize_word_list
from .components import small_button
from .dialogs import BulkEditDialog, SearchReplaceDialog, ask_text
from .quality_dialog import QualityReportDialog


class HistoryEntry(NamedTuple):
    """Satu langkah riwayat Undo/Redo (v1.9.19): snapshot subtitle SEBELUM operasi + identitas operasinya."""

    label: str            # penanda operasi, mis. "before-quality-fix", "bulk-upper", "add-line"
    frozen: list          # snapshot ringkas (lihat _freeze)
    op_id: int            # nomor unik operasi; tetap sama saat langkah berpindah antara Undo dan Redo


class SubtitleEditorPanel(ctk.CTkFrame):
    """
    Editor subtitle (ttk.Treeview + form). Fitur:
      - cari real-time + filter (bermasalah / overlap / pendek / panjang / kosong-salah)
      - undo/redo (snapshot ringkas, bukan deepcopy), edit massal, split/merge yang menjaga word timestamp
      - navigasi baris berikutnya/sebelumnya (Alt+↓/↑), Ctrl+Enter = apply & next, Ctrl+F = cari
      - update baris inkremental (tidak membangun ulang seluruh daftar tiap edit) dan pengisian bertahap
        untuk project besar (>1500 baris) agar UI tetap responsif
      - baris yang sedang diedit otomatis di-apply saat pindah baris (tidak hilang diam-diam)
    Catatan: ini bukan virtualisasi penuh; Treeview Tk menangani ribuan baris dengan baik, dan
    optimasi di atas menjaga edit tetap O(1) per baris.
    """

    MAX_UNDO = 100
    _op_seq = 0                # penghitung nomor operasi riwayat (naik terus, tidak pernah dipakai ulang)
    _quality_receipt = None    # nomor langkah Undo milik Quality Fix terakhir yang diterapkan lewat dialog
    SYNC_ATTEMPTS = 3          # percobaan ulang tiap langkah sinkronisasi setelah rollback (v1.9.20)
    on_resync = None           # jalur darurat pemilik data (workspace) bila on_changed gagal terus setelah rollback
    sync_degraded = False      # True bila rollback sudah memulihkan data TETAPI sinkronisasi ke luar belum berhasil
    last_word_repair: WordRepair | None = None     # hasil pemulihan word timestamp pada operasi terakhir
    BIG = 1500                 # di atas ini daftar diisi bertahap
    CHUNK = 400
    TIME_EPS = 0.0006          # toleransi pembulatan milidetik (entri waktu hanya 3 desimal)
    # Menu "Alat" di toolbar (label, nama method): aksi yang jarang dipakai dikelompokkan agar toolbar cukup 1 baris.
    TOOLS_MENU = (("📋 Laporan kualitas…", "_open_quality_report"),
                  ("📂 Import subtitle…", "_import_srt"), ("⏱ Shift semua…", "_shift_all"),
                  ("🧰 Edit massal (Bulk)…", "_bulk_edit"), ("✂ Split baris", "_split_selected"),
                  ("🔗 Merge baris", "_merge_selected"))
    FILTERS = {"all": "Semua", "issues": "Bermasalah", "overlap": "Overlap", "short": "Pendek",
               "long": "Panjang", "blocking": "Kosong / waktu salah"}

    def __init__(self, master, on_changed: Callable[[list[SubtitleSegment]], None],
                 on_select: Callable[[SubtitleSegment], None],
                 get_playhead: Callable[[], float],
                 before_op: Callable[[str], None] | None = None,
                 on_resync: Callable[[list[SubtitleSegment]], bool] | None = None) -> None:
        super().__init__(master, fg_color=Theme.SURFACE, corner_radius=Theme.RADIUS, border_width=1,
                         border_color=Theme.BORDER)
        self.on_changed, self.on_select, self.get_playhead = on_changed, on_select, get_playhead
        self.on_resync = on_resync
        self.before_op = before_op or (lambda _reason: None)     # mis. membuat backup sebelum operasi besar
        self.segments: list[SubtitleSegment] = []
        self._undo: list[HistoryEntry] = []
        self._redo: list[HistoryEntry] = []
        self._current: SubtitleSegment | None = None
        self._suppress_select = False
        self._issues: dict[int, str] = {}
        self._visible: list[int] = []
        self._pos: dict[int, int] = {}
        self._search_id = None
        self._fill_gen = 0
        self.filter_key = "all"
        self._quality_dialog = None                          # dialog Laporan Kualitas yang sedang terbuka (satu saja)

        self.grid_rowconfigure(3, weight=1, minsize=80)      # tabel subtitle tidak boleh menyusut jadi hanya header
        self.grid_columnconfigure(0, weight=1)
        self._build_header()
        self._build_tree()
        self._build_form()
        self._bind_shortcuts()

    # ------------------------------------------------------------------ UI
    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(header, text="SUBTITLE EDITOR", text_color=Theme.GOLD,
                     font=Theme.font(12, "bold")).grid(row=0, column=0, sticky="w")
        self.count_label = ctk.CTkLabel(header, text="0 lines", text_color=Theme.TEXT_MUTED, font=Theme.font(11))
        self.count_label.grid(row=0, column=1, sticky="w", padx=10)

        # Baris cari + filter
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 2))
        bar.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(bar, text="🔍", font=Theme.font(12), width=24).grid(row=0, column=0)
        self.search_var = ctk.StringVar(value="")
        self.search_entry = ctk.CTkEntry(bar, textvariable=self.search_var, height=28, fg_color=Theme.SURFACE_2,
                                         border_color=Theme.BORDER, text_color=Theme.TEXT, font=Theme.font(11))
        self.search_entry.grid(row=0, column=1, sticky="ew")
        self.filter_var = ctk.StringVar(value=self.FILTERS["all"])
        ctk.CTkOptionMenu(bar, values=list(self.FILTERS.values()), variable=self.filter_var, width=150, height=28,
                          fg_color=Theme.SURFACE_2, button_color=Theme.GOLD_DIM, button_hover_color=Theme.GOLD,
                          text_color=Theme.TEXT, font=Theme.font(11), command=self._on_filter).grid(row=0, column=2, padx=4)
        small_button(bar, "✕", self._clear_search, 30).grid(row=0, column=3)

        # Toolbar 1 baris: aksi yang sering dipakai (Undo, Redo, Cari/Ganti, Auto-Fix) + menu "Alat" untuk sisanya.
        # (v1.9.15.2: dulu 2 baris x 9 tombol; baris ke-2 dihemat agar tabel subtitle mendapat ~30 px lebih tinggi.)
        tools = ctk.CTkFrame(self, fg_color="transparent")
        tools.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 2))
        buttons = (("↶ Undo", self.undo, 62), ("↷ Redo", self.redo, 62),
                   ("🔍 Cari/Ganti", self._search_replace, 100), ("✔ Auto-Fix", self._auto_fix, 82))
        for i, (text, command, width) in enumerate(buttons):
            small_button(tools, text, command, width).grid(row=0, column=i, padx=2, pady=2, sticky="w")
        self._tools_menu = tk.Menu(self, tearoff=0, bg=Theme.SURFACE_2, fg=Theme.TEXT, bd=0,
                                   activebackground=Theme.GOLD, activeforeground="#121212")
        for label, command in self.TOOLS_MENU:
            self._tools_menu.add_command(label=label, command=getattr(self, command))
        self._tools_button = small_button(tools, "🛠 Alat ▾", self._open_tools_menu, 80)
        self._tools_button.grid(row=0, column=len(buttons), padx=2, pady=2, sticky="w")

    def _open_tools_menu(self) -> None:
        button = self._tools_button
        try:
            self._tools_menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            self._tools_menu.grab_release()

    def _build_tree(self) -> None:
        style = ttk.Style(self)
        style.configure("KP.Treeview", background=Theme.SURFACE_2, fieldbackground=Theme.SURFACE_2,
                        foreground=Theme.TEXT, rowheight=26, borderwidth=0, font=("Segoe UI", 10))
        style.configure("KP.Treeview.Heading", background=Theme.BORDER, foreground=Theme.GOLD,
                        relief="flat", font=("Segoe UI", 10, "bold"))
        style.map("KP.Treeview", background=[("selected", Theme.GOLD)], foreground=[("selected", "#121212")])
        style.map("KP.Treeview.Heading", background=[("active", Theme.GOLD_DIM)])

        wrap = ctk.CTkFrame(self, fg_color=Theme.SURFACE_2, corner_radius=Theme.RADIUS)
        wrap.grid(row=3, column=0, sticky="nsew", padx=8, pady=(4, 6))
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(wrap, columns=("n", "start", "end", "dur", "text"), show="headings",
                                 style="KP.Treeview", selectmode="extended", height=3)
        for col, title, width, stretch, anchor in (("n", "#", 40, False, "center"),
                                                    ("start", "Start", 96, False, "w"),
                                                    ("end", "End", 96, False, "w"),
                                                    ("dur", "Dur", 54, False, "e"),
                                                    ("text", "Text", 220, True, "w")):
            self.tree.heading(col, text=title, anchor="w" if col not in ("n", "dur") else anchor)
            self.tree.column(col, width=width, minwidth=width if not stretch else 120, stretch=stretch, anchor=anchor)
        for tag, color in (("bad", Theme.DANGER_TEXT), ("empty", Theme.DANGER_TEXT), ("overlap", Theme.WARN_TEXT),
                           ("short", Theme.WARN_TEXT), ("long", Theme.WARN_TEXT)):
            self.tree.tag_configure(tag, foreground=color)
        bar = ctk.CTkScrollbar(wrap, command=self.tree.yview, button_color=Theme.GOLD_DIM,
                               button_hover_color=Theme.GOLD, fg_color=Theme.SURFACE_2)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        bar.grid(row=0, column=1, sticky="ns", pady=4, padx=(0, 2))
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

    def _build_form(self) -> None:
        form = ctk.CTkFrame(self, fg_color="transparent")
        form.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 8))
        form.grid_columnconfigure(4, weight=1)
        entry_kw = dict(height=28, width=112, fg_color=Theme.SURFACE_2, border_color=Theme.BORDER,
                        text_color=Theme.TEXT, font=Theme.font(11))
        ctk.CTkLabel(form, text="Start", text_color=Theme.TEXT_MUTED, font=Theme.font(11)).grid(row=0, column=0)
        self.start_entry = ctk.CTkEntry(form, **entry_kw)
        self.start_entry.grid(row=0, column=1, padx=(4, 10))
        ctk.CTkLabel(form, text="End", text_color=Theme.TEXT_MUTED, font=Theme.font(11)).grid(row=0, column=2)
        self.end_entry = ctk.CTkEntry(form, **entry_kw)
        self.end_entry.grid(row=0, column=3, padx=(4, 10))
        self.dur_label = ctk.CTkLabel(form, text="", text_color=Theme.TEXT_MUTED, font=Theme.font(11), anchor="e")
        self.dur_label.grid(row=0, column=4, sticky="e")

        self.text_box = ctk.CTkTextbox(form, height=48, fg_color=Theme.SURFACE_2, text_color=Theme.TEXT,
                                       border_width=1, border_color=Theme.BORDER, font=Theme.font(12), wrap="word")
        self.text_box.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(6, 4))

        actions = ctk.CTkFrame(form, fg_color="transparent")
        actions.grid(row=2, column=0, columnspan=5, sticky="ew")
        actions.grid_columnconfigure(5, weight=1)
        ctk.CTkButton(actions, text="✔ Apply", width=72, height=28, corner_radius=Theme.RADIUS, fg_color=Theme.GOLD,
                      hover_color=Theme.GOLD_HOVER, text_color="#121212", font=Theme.font(11, "bold"),
                      command=self._apply).grid(row=0, column=0, padx=(0, 3))
        small_button(actions, "＋ Add", self._add_line, 60).grid(row=0, column=1, padx=2)
        small_button(actions, "🗑 Delete", self._delete_selected, 70, danger=True).grid(row=0, column=2, padx=2)
        small_button(actions, "◀ Prev", self.go_prev, 62).grid(row=0, column=3, padx=2)
        small_button(actions, "Next ▶", self.go_next, 62).grid(row=0, column=4, padx=2)
        ctk.CTkLabel(actions, text="Ctrl+Enter = apply & next", text_color=Theme.TEXT_MUTED,
                     font=Theme.font(10), anchor="e").grid(row=0, column=5, sticky="e")
        self.error_label = ctk.CTkLabel(form, text="", text_color=Theme.DANGER_TEXT, font=Theme.font(11), anchor="w")
        self.error_label.grid(row=3, column=0, columnspan=5, sticky="ew")

    def _bind_shortcuts(self) -> None:
        def brk(fn):
            return lambda _e=None: (fn(), "break")[1]

        for w in (self.tree, self.search_entry, self.start_entry, self.end_entry, self.text_box):
            w.bind("<Alt-Down>", brk(self.go_next))
            w.bind("<Alt-Up>", brk(self.go_prev))
            w.bind("<Control-f>", brk(self.focus_search))
        self.text_box.bind("<Control-Return>", self._on_ctrl_enter)
        for w in (self.tree,):
            w.bind("<Delete>", brk(self._delete_selected))
            w.bind("<Control-z>", brk(self.undo))
            w.bind("<Control-y>", brk(self.redo))
            w.bind("<Control-Z>", brk(self.redo))                 # Ctrl+Shift+Z
        self.search_entry.bind("<KeyRelease>", self._on_search_key)
        self.search_entry.bind("<Escape>", brk(self._clear_search))
        self.search_entry.bind("<Return>", brk(lambda: self.tree.focus_set()))

    # ------------------------------------------------------------------ API publik
    def set_segments(self, segments: list[SubtitleSegment]) -> None:
        """Muat daftar baru (mis. buka project). Selalu diurutkan; tidak memanggil on_changed; reset Undo/Redo."""
        self.segments[:] = SubtitleIO.sorted_copy(segments)
        self._undo.clear()
        self._redo.clear()
        self._clear_form()
        self._refresh()

    def replace_all(self, segments: list[SubtitleSegment], push_undo: bool = True, label: str = "replace-all",
                    expect: Counter | None = None) -> int | None:
        """
        Ganti seluruh subtitle sebagai SATU transaksi (semua-atau-tidak-sama-sekali): bila langkah mana pun gagal
        (normalisasi, verifikasi `expect`, gambar ulang, on_changed), isi subtitle, riwayat Undo dan Redo dikembalikan persis
        seperti semula dan galat diteruskan -- tidak ada entri Undo hantu, tidak ada subtitle setengah berubah.
        `expect` (opsional): Counter sidik jari hasil yang diharapkan; beda -> ApplyVerificationError + rollback.
        Mengembalikan nomor operasi riwayat (None bila push_undo=False).
        """
        prepared = SubtitleIO.normalize(segments)[0]          # tahap siap: boleh gagal, belum ada yang berubah
        op_id = None
        with self._transaction():
            if push_undo:
                op_id = self._push_undo(label)
            self.segments[:] = prepared
            if expect is not None and self._content_counter(self.segments) != expect:
                raise ApplyVerificationError("hasil penerapan berbeda dari rencana yang dikonfirmasi")
            self._seal_content(self.segments)
            self._clear_form()
            self._refresh()
            self._notify_changed()
        return op_id

    def focus_search(self) -> None:
        self.search_entry.focus_set()

    def set_search_text(self, text: str) -> None:
        self.search_var.set(text)
        self._refresh()

    def go_next(self) -> None:
        self._move(+1)

    def go_prev(self) -> None:
        self._move(-1)

    def _form_has_focus(self) -> bool:
        """True bila user sedang mengetik di form — jangan ditimpa oleh auto-follow playhead."""
        try:
            widget = self.focus_get()
        except (KeyError, tk.TclError):
            return False
        if widget is None:
            return False
        path = str(widget)
        return any(path.startswith(str(w)) for w in (self.text_box, self.start_entry, self.end_entry))

    def select_segment(self, seg: SubtitleSegment) -> None:
        """Pilih baris dari luar (klik timeline / playback). Tidak memicu on_select."""
        if seg is self._current or self._form_has_focus():
            return
        idx = self._index_of(seg)
        if idx < 0:
            return
        self._select_tree([idx])
        self._populate(seg)

    # ------------------------------------------------------------------ undo / redo
    @staticmethod
    def _freeze(segments: list[SubtitleSegment]) -> list:
        """Snapshot ringkas (tuple) — jauh lebih murah daripada copy.deepcopy untuk ribuan baris."""
        return [(s.start, s.end, s.text, tuple((w.start, w.end, w.text) for w in s.words)) for s in segments]

    @staticmethod
    def _content_counter(rows) -> Counter:
        """Hitungan (awal, akhir, teks) TANPA urutan; waktu dibandingkan sebagai float agar 2 dan 2.0 dianggap sama."""
        return Counter((float(r.start), float(r.end), str(r.text)) for r in rows)

    @staticmethod
    def _thaw(frozen: list) -> list[SubtitleSegment]:
        return [SubtitleSegment(a, b, c, [Word(x, y, z) for x, y, z in w]) for a, b, c, w in frozen]

    def _push_undo(self, label: str = "edit") -> int:
        """Catat snapshot SEBELUM operasi `label`; mengembalikan nomor operasi uniknya."""
        self._op_seq += 1
        self._undo.append(HistoryEntry(label, self._freeze(self.segments), self._op_seq))
        self._redo.clear()
        if len(self._undo) > self.MAX_UNDO:
            self._undo.pop(0)
        return self._op_seq

    # ---- transaksi: semua-atau-tidak-sama-sekali untuk operasi yang mengganti isi subtitle ----
    def _begin(self) -> tuple:
        return self._freeze(self.segments), list(self.segments), list(self._undo), list(self._redo)

    def _restore(self, state: tuple) -> None:
        """Kembalikan isi subtitle + riwayat Undo/Redo ke `state`. UI hanya disegarkan bila isinya memang perlu dikembalikan."""
        frozen, objects, undo, redo = state
        self._undo[:] = undo
        self._redo[:] = redo
        try:
            current = self._freeze(self.segments)
        except Exception:  # noqa: BLE001
            current = None                                      # isi rusak di tengah operasi: pasti harus dipulihkan
        if current != frozen:
            self.segments[:] = self._thaw(frozen)               # isi sempat berubah (mis. edit massal setengah jalan)
        elif len(objects) == len(self.segments) and all(a is b for a, b in zip(objects, self.segments)):
            return                                              # tidak ada yang tersentuh
        else:
            self.segments[:] = objects                          # isi sama, objek diganti: pulihkan objek aslinya
        self._resync_after_rollback()

    def _notify_changed(self) -> None:
        """Beri tahu pemilik data (workspace: tampilan, autosave, status simpan). Sukses = sinkronisasi pulih."""
        self.on_changed(self.segments)
        self.sync_degraded = False

    def _attempt(self, name: str, step: Callable[[], object]) -> bool:
        """Jalankan `step` sampai SYNC_ATTEMPTS kali; True bila salah satu percobaan berhasil."""
        for attempt in range(1, self.SYNC_ATTEMPTS + 1):
            try:
                step()
                return True
            except Exception:  # noqa: BLE001
                log.warning("Sinkronisasi '%s' setelah rollback gagal (percobaan %d/%d)", name, attempt, self.SYNC_ATTEMPTS,
                            exc_info=attempt == self.SYNC_ATTEMPTS)
        return False

    def _resync_after_rollback(self) -> bool:
        """
        Setelah rollback, isi memori sudah benar; pastikan tampilan, workspace, autosave dan status simpan IKUT benar.
        Tiap langkah dicoba ulang; bila on_changed tetap gagal, jalur darurat `on_resync` dipakai; bila itu pun gagal
        pengguna DIBERI TAHU (bukan hanya dicatat di log) dan `sync_degraded` menyala sampai sinkronisasi berhasil lagi.
        """
        failed: list[str] = []
        for name, step in (("form", self._clear_form), ("tabel", self._refresh)):
            if not self._attempt(name, step):
                failed.append(name)
        if not self._attempt("on_changed", self._notify_changed) and not self._emergency_resync():
            failed.append("workspace/autosave")
        self.sync_degraded = bool(failed)
        if failed:
            log.error("Rollback selesai (data dipulihkan) tetapi sinkronisasi belum pulih: %s", ", ".join(failed))
            self._warn_sync_failed(failed)
        return not failed

    def _emergency_resync(self) -> bool:
        hook = self.on_resync
        if hook is None:
            return False
        try:
            ok = bool(hook(self.segments))
        except Exception:  # noqa: BLE001
            log.exception("Jalur darurat sinkronisasi gagal")
            return False
        if ok:
            self.sync_degraded = False
        return ok

    def _warn_sync_failed(self, failed: list[str]) -> None:
        try:
            messagebox.showerror(
                "KaptionPro", "Perubahan terakhir gagal dan sudah DIBATALKAN (isi subtitle dikembalikan seperti semula), "
                              f"tetapi {', '.join(failed)} belum bisa disinkronkan.\n\nSimpan project (atau ekspor SRT) "
                              "sekarang lalu buka ulang project bila tampilan terlihat tidak sesuai.")
        except Exception:  # noqa: BLE001
            log.exception("Gagal menampilkan peringatan sinkronisasi")

    def _seal_content(self, segments) -> None:
        """
        Dipanggil DI DALAM transaksi sebelum tampilan/penyimpanan diberi tahu: tidak boleh ada word timestamp di luar
        rentang barisnya setelah timing berubah. Dipulihkan (dijepit/dibuang); sisa pelanggaran -> galat + rollback.
        """
        repair = enforce_word_integrity(segments)
        leftover = find_word_violations(segments)
        if leftover:
            raise WordIntegrityError(f"{len(leftover)} pelanggaran word timestamp tersisa: {leftover[0].detail}")
        self.last_word_repair = repair
        if repair.total:
            log.info("Integritas word timestamp: %d baris dipulihkan (dijepit %d, dibuang %d)",
                     repair.total, len(repair.clamped), len(repair.dropped))

    @contextmanager
    def _transaction(self):
        state = self._begin()
        try:
            yield state
        except BaseException:
            self._restore(state)
            raise

    def undo(self) -> bool:
        return self._step_history(self._undo, self._redo)

    def redo(self) -> bool:
        return self._step_history(self._redo, self._undo)

    def _step_history(self, source: list[HistoryEntry], target: list[HistoryEntry]) -> bool:
        """Satu langkah Undo/Redo sebagai transaksi. True bila ada langkah yang dijalankan."""
        if not source:
            return False
        entry = source[-1]
        restored = self._thaw(entry.frozen)                     # siapkan dulu; gagal di sini -> belum ada yang berubah
        with self._transaction():
            source.pop()
            target.append(HistoryEntry(entry.label, self._freeze(self.segments), entry.op_id))
            self.segments[:] = restored
            self._after_history()
        return True

    def peek_undo(self) -> HistoryEntry | None:
        """Langkah yang akan dibatalkan Undo berikutnya (None bila riwayat kosong)."""
        return self._undo[-1] if self._undo else None

    def undo_operation(self, label: str, op_id: int | None = None) -> None:
        """
        Undo TERARAH: hanya membatalkan bila langkah Undo teratas adalah operasi `label` (dan bernomor `op_id` bila
        diberikan); selain itu UndoMismatchError dan TIDAK ada yang berubah.
        """
        top = self.peek_undo()
        if top is None:
            raise UndoMismatchError("riwayat Undo kosong; tidak ada yang bisa dibatalkan")
        if top.label != label or (op_id is not None and top.op_id != op_id):
            raise UndoMismatchError(f"ada perubahan lain sesudah operasi ini (langkah Undo teratas: '{top.label}', "
                                    f"bukan '{label}'); tidak ada yang dibatalkan")
        self.undo()

    def _after_history(self) -> None:
        self._clear_form()
        self._refresh()
        self._notify_changed()

    # ------------------------------------------------------------------ daftar (Treeview)
    def _index_of(self, seg: SubtitleSegment) -> int:
        i = self._pos.get(id(seg), -1)
        if 0 <= i < len(self.segments) and self.segments[i] is seg:
            return i
        return next((k for k, s in enumerate(self.segments) if s is seg), -1)      # peta basi -> cari linear

    def _row_values(self, i: int) -> tuple:
        seg = self.segments[i]
        return (i + 1, srt_time(seg.start), srt_time(seg.end), f"{seg.duration:.2f}s",
                seg.text.replace("\n", " ⏎ "))

    def _row_tags(self, i: int) -> tuple:
        return (self._issues[i],) if i in self._issues else ()

    def _filter_active(self) -> bool:
        return bool(self.search_var.get().strip()) or self.filter_key != "all"

    def _visible_indices(self) -> list[int]:
        q = self.search_var.get().strip().lower()
        fk = self.filter_key
        out = []
        for i, seg in enumerate(self.segments):
            if q and q not in seg.text.lower():
                continue
            kind = self._issues.get(i)
            if fk == "issues" and kind is None:
                continue
            if fk in ("overlap", "short", "long") and kind != fk:
                continue
            if fk == "blocking" and kind not in ("empty", "bad"):
                continue
            out.append(i)
        return out

    def _update_count(self) -> None:
        n = len(self.segments)
        shown = f" · {len(self._visible)} tampil" if len(self._visible) != n else ""
        warn = f" · ⚠ {len(self._issues)}" if self._issues else ""
        self.count_label.configure(text=f"{n} lines{shown}{warn}")

    def _insert_rows(self, indices: list[int]) -> None:
        for i in indices:
            self.tree.insert("", "end", iid=str(i), tags=self._row_tags(i), values=self._row_values(i))

    def _refresh(self, select: list[SubtitleSegment] | None = None) -> None:
        """Bangun ulang daftar (mengikuti cari/filter). Project besar diisi bertahap agar UI tidak membeku."""
        self._fill_gen += 1
        gen = self._fill_gen
        self.tree.delete(*self.tree.get_children())
        self._pos = {id(s): i for i, s in enumerate(self.segments)}
        self._issues = SubtitleIO.find_issues(self.segments)
        self._visible = self._visible_indices()
        self._update_count()
        if len(self._visible) <= self.BIG:
            self._insert_rows(self._visible)
            self._select_segments(select)
            return

        def step(pos: int) -> None:
            if gen != self._fill_gen:                  # refresh baru sudah dimulai -> hentikan pengisian lama
                return
            self._insert_rows(self._visible[pos:pos + self.CHUNK])
            if pos + self.CHUNK < len(self._visible):
                self.after(1, step, pos + self.CHUNK)
            else:
                self._select_segments(select)

        step(0)

    def _select_tree(self, indices: list[int]) -> None:
        ids = [str(i) for i in indices if self.tree.exists(str(i))]
        if not ids:
            return
        self._suppress_select = True
        try:
            self.tree.selection_set(*ids)
            self.tree.see(ids[0])
        finally:
            self._suppress_select = False

    def _select_segments(self, segs: list[SubtitleSegment] | None) -> None:
        if segs:
            self._select_tree([self._index_of(s) for s in segs if self._index_of(s) >= 0])

    def _update_rows_around(self, idx: int) -> None:
        """Edit satu baris tanpa mengubah urutan: perbarui hanya baris idx-1..idx+1 (issue tetangga bisa berubah)."""
        self._issues = SubtitleIO.find_issues(self.segments)
        for j in (idx - 1, idx, idx + 1):
            if 0 <= j < len(self.segments) and self.tree.exists(str(j)):
                self.tree.item(str(j), values=self._row_values(j), tags=self._row_tags(j))
        self._update_count()

    # ------------------------------------------------------------------ cari / filter
    def _on_search_key(self, _event=None) -> None:
        if self._search_id:
            self.after_cancel(self._search_id)
        self._search_id = self.after(150, self._do_search)

    def _do_search(self) -> None:
        self._search_id = None
        self._refresh([self._current] if self._current else None)

    def _clear_search(self) -> None:
        self.search_var.set("")
        self.filter_key = "all"
        self.filter_var.set(self.FILTERS["all"])
        self._refresh([self._current] if self._current else None)

    def _on_filter(self, label: str) -> None:
        self.filter_key = next((k for k, v in self.FILTERS.items() if v == label), "all")
        self._refresh([self._current] if self._current else None)

    # ------------------------------------------------------------------ form
    def _set_error(self, text: str) -> None:
        self.error_label.configure(text=text)

    def _clear_form(self) -> None:
        self._current = None
        for entry in (self.start_entry, self.end_entry):
            entry.delete(0, "end")
        self.text_box.delete("1.0", "end")
        self.dur_label.configure(text="")
        self._set_error("")

    def _populate(self, seg: SubtitleSegment) -> None:
        self._current = seg
        self.start_entry.delete(0, "end")
        self.start_entry.insert(0, srt_time(seg.start))
        self.end_entry.delete(0, "end")
        self.end_entry.insert(0, srt_time(seg.end))
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", seg.text)
        self.dur_label.configure(text=f"{seg.duration:.2f}s · {len(seg.text)} chars")
        self._set_error("")

    def _form_dirty(self) -> bool:
        """True bila isi form berbeda dari baris yang sedang diedit (toleransi pembulatan milidetik)."""
        seg = self._current
        if seg is None:
            return False
        if self.text_box.get("1.0", "end").strip() != seg.text.strip():
            return True
        try:
            start = SubtitleSegment.parse_time(self.start_entry.get())
            end = SubtitleSegment.parse_time(self.end_entry.get())
        except ValueError:
            return True                      # input tidak valid dianggap perubahan -> _apply menampilkan error
        return abs(start - seg.start) > self.TIME_EPS or abs(end - seg.end) > self.TIME_EPS

    def _on_tree_select(self, _event=None) -> None:
        if self._suppress_select:
            return
        sel = self.tree.selection()
        if len(sel) != 1:
            return
        try:
            clicked = self.segments[int(sel[0])]
        except (ValueError, IndexError):
            return
        if self._current is not None and self._current is not clicked and self._form_dirty():
            # Pindah baris saat ada edit yang belum di-apply: apply dulu (tidak hilang diam-diam).
            if not self._apply(select_after=clicked):
                self._select_segments([self._current])              # invalid -> tetap di baris lama
                return
        self._populate(clicked)
        self.on_select(clicked)

    def _on_ctrl_enter(self, _event=None) -> str:
        self._apply(go_next=True)
        return "break"

    def _move(self, step: int) -> None:
        """Pindah ke baris tampil berikutnya/sebelumnya; edit yang belum di-apply disimpan dulu."""
        if not self._visible:
            return
        if self._current is not None and self._form_dirty() and not self._apply():
            return
        cur = self._index_of(self._current) if self._current is not None else -1
        if step > 0:
            cands = [i for i in self._visible if i > cur]
            target = cands[0] if cands else None
        else:
            cands = [i for i in self._visible if i < cur]
            target = cands[-1] if cands else None
        if target is None:
            return
        seg = self.segments[target]
        self._select_tree([target])
        self._populate(seg)
        self.on_select(seg)

    # ------------------------------------------------------------------ aksi
    def _apply(self, go_next: bool = False, select_after: SubtitleSegment | None = None) -> bool:
        seg = self._current
        if seg is None:
            return False
        if self._index_of(seg) < 0:
            self._set_error("Baris ini sudah tidak ada. Pilih ulang dari daftar.")      # mis. akibat Split
            return False
        try:
            start = SubtitleSegment.parse_time(self.start_entry.get())
            end = SubtitleSegment.parse_time(self.end_entry.get())
        except ValueError as e:
            self._set_error(str(e))
            return False
        text = self.text_box.get("1.0", "end").strip()
        if end <= start:
            self._set_error("End harus lebih besar dari Start")
            return False
        if not text:
            self._set_error("Teks tidak boleh kosong")
            return False
        self._set_error("")
        times_changed = abs(start - seg.start) > self.TIME_EPS or abs(end - seg.end) > self.TIME_EPS
        text_changed = text != seg.text.strip()
        if times_changed or text_changed:
            # Karaoke: petakan ulang kata ke teks baru memakai amplop waktu bicara lama (jeda ucapan terjaga). Murni: dihitung
            # SEBELUM transaksi, jadi kegagalan di sini tidak meninggalkan jejak apa pun.
            new_words = retime_words(text, seg.words, seg.start, seg.end, start, end) if seg.words else []
            with self._transaction():
                self._push_undo("apply")
                before = self._index_of(seg)
                if times_changed:
                    seg.start, seg.end = start, end
                if text_changed:
                    seg.text = text
                seg.words = new_words
                self._seal_content([seg])
                self.segments.sort(key=lambda s: s.start)
                after = next((k for k, s in enumerate(self.segments) if s is seg), -1)
                if after == before and not self._filter_active() and select_after is None:
                    self._update_rows_around(after)                  # inkremental: O(1) per edit
                else:
                    self._refresh(select=[select_after or seg])
                self.dur_label.configure(text=f"{seg.duration:.2f}s · {len(seg.text)} chars")
                self._notify_changed()
        if go_next:
            idx = self._index_of(seg) + 1
            if idx < len(self.segments):
                nxt = self.segments[idx]
                self._select_tree([idx])
                self._populate(nxt)
                self.on_select(nxt)
                self.text_box.focus_set()
        return True

    def _add_line(self) -> None:
        if self._current is not None:
            anchor = self._current.end + 0.1
        else:
            anchor = max(self.get_playhead(), (self.segments[-1].end + 0.1) if self.segments else 0.0)
        seg = SubtitleSegment(round(anchor, 3), round(anchor + 2.0, 3), "Teks baru")
        with self._transaction():
            self._push_undo("add-line")
            self.segments.append(seg)
            self.segments.sort(key=lambda s: s.start)
            self._refresh(select=[seg])
            self._notify_changed()
        self._populate(seg)                       # navigasi/fokus sesudah commit: bukan bagian transaksi data
        self.on_select(seg)
        self.text_box.focus_set()
        self.text_box.tag_add("sel", "1.0", "end-1c")

    def _selected_indices(self) -> list[int]:
        out = []
        for iid in self.tree.selection():
            try:
                out.append(int(iid))
            except ValueError:
                pass
        return sorted(set(out))

    def _delete_selected(self) -> None:
        idxs = sorted(self._selected_indices(), reverse=True)
        if not idxs:
            return
        with self._transaction():
            self._push_undo("delete-selected")
            for i in idxs:
                del self.segments[i]
            self._clear_form()
            self._refresh()
            self._notify_changed()
        if self.segments:
            self.select_segment(self.segments[min(idxs[-1], len(self.segments) - 1)])

    def _shift_all(self) -> None:
        if not self.segments:
            return
        raw = ask_text(self, "Shift waktu", "Geser SEMUA subtitle (detik).\nContoh: 1.5 atau -0.5\n"
                                            "(Untuk baris terpilih saja: tombol Bulk.)", "0")
        if raw is None or not raw.strip():
            return
        self._run_bulk("shift", raw, "", list(range(len(self.segments))), "shift-all")

    def _bulk_edit(self) -> None:
        if not self.segments:
            return
        BulkEditDialog(self, len(self._selected_indices()), len(self.segments), self._on_bulk_dialog)

    def _on_bulk_dialog(self, op: str, p1: str, p2: str, scope: str) -> None:
        idxs = list(range(len(self.segments))) if scope == "all" else self._selected_indices()
        if not idxs:
            messagebox.showinfo("KaptionPro", "Tidak ada baris terpilih. Pilih baris dulu atau pilih 'Semua baris'.")
            return
        self._run_bulk(op, p1, p2, idxs, "bulk-" + op)

    def _run_bulk(self, op: str, p1: str, p2: str, idxs: list[int], reason: str) -> bool:
        """Jalankan edit massal sebagai transaksi: backup -> snapshot Undo -> operasi -> (rollback bila gagal/0 perubahan)."""
        self.before_op(reason)
        failure: str | None = None
        with self._transaction() as state:
            self._push_undo(reason)
            changed, err = SubtitleIO.bulk_edit(self.segments, idxs, op, p1, p2)
            if err or changed == 0:
                self._restore(state)                  # batalkan snapshot (dan isi, bila bulk_edit sempat menyentuhnya)
                failure = err or ""
            else:
                self.segments.sort(key=lambda s: s.start)
                self._seal_content(self.segments)
                self._clear_form()
                self._refresh()
                self._notify_changed()
        if failure is not None:
            if failure:
                messagebox.showerror("KaptionPro", failure)
            else:
                messagebox.showinfo("KaptionPro", "Tidak ada baris yang berubah.")
            return False
        return True

    def _import_srt(self) -> None:
        path = filedialog.askopenfilename(title="Import subtitle",
                                          filetypes=[("SubRip", "*.srt"), ("All", "*.*")])
        if not path:
            return
        try:
            segs = SubtitleIO.read_srt(path)
        except OSError as e:
            messagebox.showerror("KaptionPro", f"Gagal membaca file:\n{e}")
            return
        if not segs:
            messagebox.showerror("KaptionPro", "Tidak ada blok subtitle valid di file ini.")
            return
        if self.segments and not messagebox.askyesno(
                "KaptionPro", f"Ganti {len(self.segments)} baris yang ada dengan {len(segs)} baris dari file?\n"
                              "(Bisa di-Undo; backup dibuat otomatis.)"):
            return
        self.before_op("before-import")
        self.replace_all(segs)

    def _search_replace(self) -> None:
        SearchReplaceDialog(self, self.segments, self._apply_search_result, self._highlight_indices)

    def _apply_search_result(self, new_segments: list[SubtitleSegment]) -> None:
        self.before_op("search-replace")
        with self._transaction():
            self._push_undo("search-replace")
            self.segments[:] = new_segments
            self._seal_content(self.segments)
            self._clear_form()
            self._refresh()
            self._notify_changed()

    def _highlight_indices(self, idxs: list[int]) -> None:
        self._select_tree([i for i in idxs if 0 <= i < len(self.segments)])

    # ---- split / merge: menjaga word timestamp asli bila ada ----
    def _real_words(self, seg: SubtitleSegment) -> list[Word] | None:
        """Word timestamp asli bila masih cocok dengan teks; selain itu None."""
        tokens = seg.text.split()
        src = [w for w in seg.words if w.text.strip()]
        if not (src and [w.text.strip() for w in src] == tokens):
            return None
        return sanitize_word_list(src, seg.start, seg.end)          # None bila timestamp tak valid

    def _split_selected(self) -> None:
        sel = self._selected_indices()
        if not sel:
            messagebox.showinfo("KaptionPro", "Pilih satu baris untuk di-split.")
            return
        if len(sel) > 1:
            messagebox.showinfo("KaptionPro", "Split hanya untuk satu baris. Pilih satu baris dulu.")
            return
        i = sel[0]
        seg = self.segments[i]
        words = seg.text.split()
        if len(words) < 2:
            messagebox.showinfo("KaptionPro", "Tidak cukup kata untuk di-split.")
            return
        real = self._real_words(seg)
        playhead = self.get_playhead()
        rel = (playhead - seg.start) / max(0.01, seg.duration) if seg.start <= playhead <= seg.end else 0.5
        rel = max(0.15, min(0.85, rel))
        n_left = max(1, min(len(words) - 1, round(len(words) * rel)))
        if real:       # titik potong di tengah jeda antara dua kata sungguhan
            mid_time = (real[n_left - 1].end + real[n_left].start) / 2.0
        else:
            mid_time = seg.start + seg.duration * rel
        mid_time = min(max(mid_time, seg.start + 0.05), seg.end - 0.05)
        left = SubtitleSegment(seg.start, round(mid_time, 3), " ".join(words[:n_left]),
                               [Word(w.start, w.end, w.text) for w in real[:n_left]] if real else [])
        right = SubtitleSegment(round(mid_time, 3), seg.end, " ".join(words[n_left:]),
                                [Word(w.start, w.end, w.text) for w in real[n_left:]] if real else [])
        with self._transaction():
            self._push_undo("split-selected")
            self.segments[i:i + 1] = [left, right]
            self._seal_content([left, right])
            self._refresh(select=[left])
            self._notify_changed()
        self._populate(left)
        self._current = left

    def _merge_selected(self) -> None:
        idxs = self._selected_indices()
        if len(idxs) < 2:
            messagebox.showinfo("KaptionPro", "Pilih dua baris berurutan atau lebih untuk di-merge.")
            return
        if idxs != list(range(idxs[0], idxs[0] + len(idxs))):
            messagebox.showinfo("KaptionPro", "Baris yang dipilih harus berurutan.")
            return
        group = [self.segments[i] for i in idxs]
        merged_text = " ".join(s.text.strip() for s in group if s.text.strip())
        reals = [self._real_words(s) for s in group]
        words = [Word(w.start, w.end, w.text) for r in reals if r for w in r] if all(reals) else []
        merged = SubtitleSegment(group[0].start, group[-1].end, merged_text, words)
        with self._transaction():
            self._push_undo("merge-selected")
            self.segments[idxs[0]:idxs[-1] + 1] = [merged]
            self._seal_content([merged])
            self._refresh(select=[merged])
            self._notify_changed()
        self._populate(merged)
        self._current = merged

    def _auto_fix(self) -> None:
        if not self.segments:
            return
        fixed, changes = SubtitleIO.auto_fix(self.segments)
        if changes == 0:
            messagebox.showinfo("KaptionPro", "Tidak ada masalah yang perlu diperbaiki.")
            return
        if not messagebox.askyesno("KaptionPro",
                                   f"Auto-Fix akan memperbaiki {changes} hal (overlap, durasi, kosong, "
                                   "baris panjang akan dipecah).\nLanjutkan? (Bisa di-Undo; backup dibuat otomatis.)"):
            return
        self.before_op("before-autofix")
        self.replace_all(fixed)

    # ------------------------------------------------------------------ laporan kualitas (v1.9.18)
    def _open_quality_report(self) -> None:
        """Buka dialog Laporan Kualitas (daftar error/peringatan per baris + Perbaiki otomatis yang aman)."""
        if not self.segments:
            messagebox.showinfo("KaptionPro", "Belum ada subtitle untuk diperiksa.")
            return
        if self._form_dirty() and not self._apply():          # edit belum disimpan ikut diperiksa; form tak valid -> berhenti
            return
        dialog = self._quality_dialog
        try:
            alive = dialog is not None and bool(dialog.winfo_exists())
        except tk.TclError:
            alive = False
        if alive:
            dialog.lift()
            return
        self._quality_dialog = QualityReportDialog(self, get_segments=lambda: self.segments,
                                                   apply_plan=self._apply_quality_plan, undo=self._undo_quality_fix,
                                                   jump_to=self._jump_to_index)

    def _jump_to_index(self, index: int) -> None:
        if 0 <= index < len(self.segments):
            seg = self.segments[index]
            self.select_segment(seg)
            self.on_select(seg)

    def _segments_from_plan(self, plan: qf.FixPlan) -> list[SubtitleSegment]:
        """Daftar baru dari rencana: baris tak berubah disalin apa adanya; word timestamp hanya dibuang bila WAKTU berubah."""
        by_index = {change.index: change for change in plan.changes}
        out: list[SubtitleSegment] = []
        for i, seg in enumerate(self.segments):
            change = by_index.get(i)
            keep_words = change is None or not change.timing_changed
            words = [Word(w.start, w.end, w.text) for w in seg.words] if keep_words else []
            if change is None:
                out.append(SubtitleSegment(seg.start, seg.end, seg.text, words))
            else:
                out.append(SubtitleSegment(float(change.after.start), float(change.after.end), change.after.text, words))
        return out

    def _apply_quality_plan(self, plan: qf.FixPlan) -> int | None:
        """
        Dipanggil QualityFlow.apply SETELAH pratinjau dan konfirmasi pengguna: backup lalu SATU langkah Undo bertanda
        "before-quality-fix" (replace_all mendorong satu snapshot). Menolak rencana basi sebagai pertahanan kedua.
        Transaksional: hasil diverifikasi terhadap rencana; gagal di tahap mana pun -> subtitle dan riwayat Undo/Redo
        dikembalikan persis seperti semula. Mengembalikan nomor langkah Undo-nya (dipakai _undo_quality_fix).
        """
        if not plan.matches(self.segments):
            raise StalePlanError("subtitle berubah sejak pratinjau")
        new_segments = self._segments_from_plan(plan)
        expected = self._content_counter(plan.rows_after)
        self.before_op(QUALITY_FIX_LABEL)
        op_id = self.replace_all(new_segments, label=QUALITY_FIX_LABEL, expect=expected)
        self._quality_receipt = op_id
        return op_id

    def _undo_quality_fix(self) -> None:
        """Undo milik dialog: hanya membatalkan Quality Fix terakhir; ada perubahan lain sesudahnya -> UndoMismatchError."""
        self.undo_operation(QUALITY_FIX_LABEL, self._quality_receipt)
