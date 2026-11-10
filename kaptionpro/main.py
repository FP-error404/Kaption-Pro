"""Aplikasi utama (KaptionProApp) dan titik masuk."""

from __future__ import annotations

import logging
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from . import __version__
from .config import (APP_DIR, HAS_DEEP_TRANSLATOR, HAS_FASTER_WHISPER, HAS_VLC, SESSION_ID, Theme, ctk, log,
                     require_gui)

require_gui()          # sebelum modul UI diimpor (kelasnya turunan widget CustomTkinter)

from .core.layout import startup_geometry, window_minsize
from .core.lifecycle import ShutdownCoordinator, drain_queue
from .core.storage import ProjectStore, SettingsStore
from .models import Project
from .services.ai_service import AiService
from .services.ffmpeg_service import FFmpegService
from .ui.components import apply_radius_defaults
from .ui.dialogs import GuideDialog
from .ui.welcome import WelcomeScreen
from .ui.workspace import WorkspaceScreen


class KaptionProApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        apply_radius_defaults()
        ttk.Style(self).theme_use("clam")
        self.title(f"KaptionPro v{__version__}")
        try:                       # faktor skala DPI CustomTkinter (API privat: jangan sampai menggagalkan startup)
            scaling = float(self._get_window_scaling())
        except Exception:  # noqa: BLE001
            scaling = 1.0
        w, h, x, y = startup_geometry(self.winfo_screenwidth(), self.winfo_screenheight(), scaling)
        self.geometry(f"{w}x{h}+{x}+{y}")          # dibatasi layar: tombol Export tidak boleh tertutup taskbar
        self.minsize(*window_minsize(w, h))        # layar kecil: jangan paksa jendela lebih besar dari layar
        self.configure(fg_color=Theme.BG)

        self.store = ProjectStore()
        self.settings = SettingsStore()
        self.crashed_last_session = self.store.session_begin()      # True bila sesi sebelumnya tidak ditutup normal
        self.current_screen = ""
        self._closing = False                     # True sejak penutupan dimulai (cegah penutupan ganda / re-entrancy)
        self._shutdown: ShutdownCoordinator | None = None
        self.exit_hard = False                    # True bila ada worker yang tak berhenti: keluar paksa setelah mainloop
        self.ai = AiService()
        self.ffmpeg = FFmpegService()
        # Retensi cache terjemahan di latar belakang (database bisa terkunci instance lain: jangan menahan startup).
        threading.Thread(target=self.ai.prune_cache, daemon=True, name="kp-cache-prune").start()

        self._ui_queue: queue.Queue = queue.Queue()
        self._drain_id = self.after(60, self._drain_ui_queue)

        container = ctk.CTkFrame(self, fg_color=Theme.BG, corner_radius=0)
        container.pack(fill="both", expand=True)
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)
        self.screens: dict[str, ctk.CTkFrame] = {
            "welcome": WelcomeScreen(container, self),
            "workspace": WorkspaceScreen(container, self),
        }
        for screen in self.screens.values():
            screen.grid(row=0, column=0, sticky="nsew")
        self.show_screen("welcome")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(400, self._show_startup_notes)
        log.info("KaptionPro v%s started (session=%s, pid=%s, whisper=%s, translator=%s, vlc=%s, ffmpeg=%s)",
                 __version__, SESSION_ID, os.getpid(), HAS_FASTER_WHISPER, HAS_DEEP_TRANSLATOR, HAS_VLC,
                 bool(self.ffmpeg.ffmpeg_path()))

    # ---------- thread -> UI ----------
    def post(self, fn: Callable, *args) -> None:
        self._ui_queue.put((fn, args))

    UI_DRAIN_BATCH = 100              # callback per putaran (batas jumlah)
    UI_DRAIN_BUDGET = 0.010           # detik per putaran (batas waktu); sisanya diproses putaran berikutnya

    def _drain_ui_queue(self) -> None:
        """Proses callback worker secara BERBATAS agar event loop Tk tetap sempat menangani klik dan gambar ulang.
        Bila masih ada sisa, putaran berikutnya dijadwalkan segera (1 ms); bila kosong, kembali ke jeda normal."""
        _done, backlog = drain_queue(self._ui_queue, self.UI_DRAIN_BATCH, lambda: log.exception("UI callback gagal"),
                                     time_budget=self.UI_DRAIN_BUDGET)
        self._drain_id = self.after(1 if backlog else 60, self._drain_ui_queue)

    def report_callback_exception(self, exc, val, tb) -> None:
        log.error("Unhandled UI error", exc_info=(exc, val, tb))
        try:
            messagebox.showerror("KaptionPro",
                                 f"Terjadi error tak terduga:\n{val}\n\nDetail ada di {APP_DIR / 'kaptionpro.log'}")
        except tk.TclError:
            pass

    # ---------- navigation ----------
    def show_screen(self, name: str) -> None:
        self.current_screen = name
        self.screens[name].tkraise()

    def go_home(self) -> None:
        if not self.screens["workspace"].leave():
            return                                # user memilih tetap di workspace (job masih jalan)
        self.store.reload()                       # tampilkan perubahan instance lain pada daftar project
        self.screens["welcome"].refresh_recent()
        self.show_screen("welcome")

    def start_new_project(self) -> None:
        path = filedialog.askopenfilename(
            title="Select video",
            filetypes=[("Video files", "*.mp4 *.mkv *.avi *.mov *.webm"), ("All files", "*.*")])
        if path:
            if not Path(path).exists():
                messagebox.showerror("KaptionPro", f"File tidak ditemukan:\n{path}")
                return
            existing = self.store.find_by_video(path)
            if existing:
                messagebox.showinfo("KaptionPro", f"Video ini sudah punya project: '{existing.name}'.\n"
                                                  "Project itu dibuka (subtitle tetap tersimpan).")
                self.open_project(existing)
                return
            project = Project(name=Path(path).stem, video_path=path, translator=self.settings.get("translator", "google"))
        elif messagebox.askyesno("KaptionPro", "No video selected.\nOpen an empty workspace instead?"):
            names = {p.name for p in self.store.projects}
            n = 1
            while (name := "Untitled Project" if n == 1 else f"Untitled Project {n}") in names:
                n += 1
            project = Project(name=name, translator=self.settings.get("translator", "google"))
        else:
            return
        if not self.store.add(project):
            messagebox.showwarning("KaptionPro", "Project dibuka, tetapi belum tercatat di daftar 'Recent':\n"
                                   f"{self.store.last_save_error or 'penyebab tidak diketahui'}\n\n"
                                   "Subtitle tetap tersimpan; daftar akan diperbarui otomatis pada simpan berikutnya.")
        self.open_project(project)

    def open_project(self, project: Project) -> None:
        if self.screens["workspace"].load_project(project):     # False = user membatalkan (project dipakai instance lain)
            self.show_screen("workspace")

    def remove_project(self, project: Project) -> None:
        """Dua tindakan terpisah: (1) hapus dari daftar saja, (2) hapus PERMANEN (setelah konfirmasi + cek lock)."""
        answer = messagebox.askyesnocancel(
            "KaptionPro",
            f"'{project.name}'\n\n"
            "Ya = Hapus dari daftar saja (subtitle & backup tetap tersimpan)\n"
            "Tidak = HAPUS PERMANEN subtitle, snapshot, preview & semua backup project ini\n"
            "Batal = tidak melakukan apa-apa\n\n(File video tidak pernah disentuh.)")
        if answer is None:
            return
        if answer:
            if not self.store.remove_from_recent(project):
                messagebox.showwarning("KaptionPro", "Project belum bisa dicabut dari daftar:\n"
                                       f"{self.store.last_save_error or 'penyebab tidak diketahui'}\n"
                                       "Coba lagi sebentar lagi.")
        else:
            if not messagebox.askyesno("KaptionPro", f"Hapus PERMANEN data '{project.name}'?\n"
                                                     "Tindakan ini tidak bisa dibatalkan.", icon="warning"):
                return
            if not self.store.delete_project(project):
                messagebox.showwarning("KaptionPro", "Penghapusan project tidak tuntas:\n"
                                       f"{self.store.last_delete_error or 'alasan tidak diketahui'}")
                self.screens["welcome"].refresh_recent()
                return
        self.screens["welcome"].refresh_recent()

    def show_guide(self) -> None:
        GuideDialog(self)

    def _show_startup_notes(self) -> None:
        """Pemberitahuan pemulihan: daftar project rusak dipulihkan / sesi sebelumnya tidak ditutup normal."""
        notes = list(self.store.notes)
        if self.crashed_last_session:
            notes.append("KaptionPro sebelumnya tidak ditutup dengan benar (crash / dimatikan paksa).\n"
                         "Subtitle di-autosave tiap perubahan dan backup berversi tersedia lewat tombol "
                         "'🗂 Backup' di workspace. Bila ada yang terasa hilang, cek di sana.")
        self.crashed_last_session = False         # tampilkan sekali saja
        if notes:
            self.store.notes.clear()
            messagebox.showinfo("KaptionPro", "\n\n".join(notes))

    def _on_close(self) -> None:
        if self._closing:
            return                                # penutupan sudah berjalan (menunggu worker berhenti)
        ws = self.screens["workspace"]
        try:
            if not ws.ensure_saved("menutup aplikasi"):
                return                            # simpan gagal & user membatalkan -> aplikasi tetap terbuka
        except Exception as exc:  # noqa: BLE001
            # Fail-closed: JANGAN menutup diam-diam bila memastikan penyimpanan sendiri gagal. Default = tetap terbuka.
            log.exception("ensure_saved saat menutup gagal")
            try:
                close_anyway = messagebox.askyesno(
                    "KaptionPro",
                    f"Terjadi kesalahan tak terduga saat memastikan perubahan tersimpan:\n{exc}\n\n"
                    "Tutup aplikasi TANPA menyimpan? Perubahan terakhir bisa hilang.\n\n"
                    "Ya     = tutup dan buang perubahan\n"
                    "Tidak  = tetap di sini (coba simpan lagi / Ctrl+S)",
                    icon="warning", default="no")
            except Exception:  # noqa: BLE001
                log.exception("Dialog konfirmasi tutup gagal; aplikasi tetap terbuka")
                return
            if not close_anyway:
                return
            try:
                ws._discard_unsaved_changes()     # keputusan sadar: cegah flush() akhir menyimpan/menjadwalkan ulang
            except Exception:  # noqa: BLE001
                log.exception("Gagal mereset status simpan sebelum menutup")
                ws._dirty = False
                ws._autosave_id = None
        # Tahap 1: minta semua pekerjaan berhenti. Tahap 2 (ShutdownCoordinator) BARU melepas resource setelah worker
        # benar-benar berhenti: join(timeout) yang habis bukan bukti worker berhenti. Bila ada yang bandel (mis.
        # inferensi Whisper), resource yang mungkin masih dipakainya (cache SQLite, lock project) TIDAK dilepas.
        self._closing = True
        try:
            ws._abort_job_silently()              # set cancel_event + buang snapshot AI yang menunggu
        except Exception:  # noqa: BLE001
            log.exception("Gagal membatalkan job saat menutup")
        self._shutdown = ShutdownCoordinator(
            alive=ws.alive_workers, kill_active=FFmpegService.kill_all_active, ask_wait_more=self._ask_wait_workers,
            release_safe=self._release_all, release_forced=self._release_unshared, finish=self._finish_destroy,
            stop_timeout=self.WORKER_STOP_TIMEOUT, extension=self.WORKER_STOP_EXTENSION)
        if ws.alive_workers():
            self.title(f"KaptionPro v{__version__} — menghentikan proses…")
        self._shutdown.begin()
        self._pump_shutdown()

    # Batas tunggu worker berhenti = waktu terpanjang FFmpeg berhenti (+ margin untuk worker AI).
    WORKER_STOP_TIMEOUT = FFmpegService.MAX_STOP_SECONDS + 5.0
    WORKER_STOP_EXTENSION = 20.0                  # tambahan bila user memilih "tunggu lagi"

    def _pump_shutdown(self) -> None:
        delay = self._shutdown.poll() if self._shutdown else None
        if delay is not None:
            self.after(max(1, int(delay * 1000)), self._pump_shutdown)

    def _ask_wait_workers(self, alive) -> bool:
        names = ", ".join(t.name for t in alive)
        log.error("Worker belum berhenti setelah menunggu: %s", names)
        return messagebox.askyesno(
            "KaptionPro",
            f"Proses latar belakang belum berhenti ({names}).\n\n"
            "Ya     = tunggu sebentar lagi (disarankan)\n"
            "Tidak  = tutup paksa sekarang (FFmpeg yang masih berjalan dimatikan)",
            icon="warning", default="yes")

    def _release_unshared(self) -> None:
        """Tutup paksa dengan worker tersisa: lepas HANYA resource yang tidak dipakai worker. Cache SQLite dan lock
        project sengaja dibiarkan (OS melepas lock saat proses keluar)."""
        ws = self.screens["workspace"]
        self.exit_hard = True
        ws.flush()
        ws.video.shutdown()

    def _release_all(self) -> None:
        """Semua worker sudah berhenti: aman melepas semuanya."""
        ws = self.screens["workspace"]
        try:
            ws.flush()
            if ws.project:
                self.store.unlock(ws.project)
            ws.video.shutdown()                   # lepaskan media + player + instance VLC
        finally:
            self.ai.close()                       # tutup koneksi cache SQLite (tak ada worker yang memakainya)

    def _finish_destroy(self) -> None:
        self.store.session_end()                  # tandai penutupan normal (deteksi crash sesi berikutnya)
        if self._drain_id:
            self.after_cancel(self._drain_id)
        self.destroy()


def main() -> None:
    try:
        app = KaptionProApp()
        app.mainloop()
        if app.exit_hard:                         # worker bandel (mis. inferensi) bisa menyangkut penutupan interpreter
            log.error("Keluar paksa karena worker tidak berhenti")
            logging.shutdown()
            os._exit(0)
    except Exception as exc:  # noqa: BLE001
        log.exception("Fatal error")
        try:       # tanpa console (double-click) user tidak melihat apa pun -> tampilkan dialog
            _root = tk.Tk()
            _root.withdraw()
            messagebox.showerror("KaptionPro", f"Aplikasi gagal dijalankan:\n{exc}\n\nDetail: {APP_DIR / 'kaptionpro.log'}")
            _root.destroy()
        except Exception:  # noqa: BLE001
            pass
        raise


if __name__ == "__main__":
    main()
