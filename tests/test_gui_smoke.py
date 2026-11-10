"""
Tes GUI otomatis (smoke) untuk KaptionPro -- dirancang untuk GitHub Actions (Linux + xvfb, Windows).

Menjalankan aplikasi ASLI (CustomTkinter + Tk) dengan data dipisah ke folder rumah sementara, lalu memeriksa alur
utama: start, project baru, edit + simpan, status job, dialog Panduan/Privasi, alur persetujuan privasi, antrean UI
tetap responsif, tata letak pada 1366x768, buka-ulang project, dan penutupan bersih.
Screenshot disimpan ke folder artefak (KAPTION_GUI_ARTIFACTS, default ./gui-artifacts) agar bisa dilihat dari tab
Actions tanpa perlu menjalankan aplikasi di PC sendiri.

Cara menjalankan:
  CI      : KAPTION_REQUIRE_GUI=1 xvfb-run -a python -m unittest tests.test_gui_smoke -v
  Lokal   : python -m unittest tests.test_gui_smoke -v      (dilewati otomatis bila GUI/display tidak tersedia)
Jalankan MODUL INI TERPISAH (bukan lewat `discover` bersama tes lain): data aplikasi harus dialihkan sebelum
`kaptionpro` pertama kali diimpor. KAPTION_REQUIRE_GUI=1 mengubah 'dilewati' menjadi GAGAL agar CI tidak diam-diam hijau.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REQUIRE_GUI = os.environ.get("KAPTION_REQUIRE_GUI") == "1"
ARTIFACTS = Path(os.environ.get("KAPTION_GUI_ARTIFACTS") or (ROOT / "gui-artifacts"))

_TMP_DIR: str | None = None          # str (bukan TemporaryDirectory): finalizer-nya melempar PermissionError di Windows
_SAVED_ENV: dict[str, str | None] = {}


def _probe_gui() -> str | None:
    """None bila GUI bisa dipakai; selain itu alasan singkat."""
    try:
        import customtkinter  # noqa: F401
        import tkinter
    except Exception as e:  # noqa: BLE001
        return f"tkinter/customtkinter tidak tersedia: {e}"
    try:
        root = tkinter.Tk()
        root.destroy()
    except Exception as e:  # noqa: BLE001
        return f"tidak ada display: {e}"
    return None


def _release_log_files() -> None:
    """Tutup SEMUA handler berkas log milik aplikasi. Di Windows berkas yang masih terbuka tidak bisa dihapus
    (WinError 32), sehingga folder sementara gagal dibersihkan."""
    import logging
    seen = [logging.getLogger("KaptionPro"), logging.getLogger()]
    for logger in seen:
        for handler in list(logger.handlers):
            if isinstance(handler, logging.FileHandler):
                try:
                    handler.close()
                finally:
                    logger.removeHandler(handler)


def _rmtree_retry(path: str, attempts: int = 5) -> None:
    for i in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.2 * (i + 1))                              # antivirus/indeks Windows kadang menahan berkas sebentar


def setUpModule() -> None:
    global _TMP_DIR
    problem = _probe_gui()
    if problem is None and "kaptionpro.config" in sys.modules:
        problem = ("modul kaptionpro sudah diimpor oleh tes lain; jalankan terpisah: "
                   "python -m unittest tests.test_gui_smoke")
    if problem:
        if REQUIRE_GUI:
            raise AssertionError(f"GUI wajib tersedia (KAPTION_REQUIRE_GUI=1) tetapi: {problem}")
        raise unittest.SkipTest(problem)
    _TMP_DIR = tempfile.mkdtemp(prefix="kp-gui-home-")
    for key in ("HOME", "USERPROFILE"):                       # Path.home() -> APP_DIR (~/.kaptionpro)
        _SAVED_ENV[key] = os.environ.get(key)
        os.environ[key] = _TMP_DIR
    ARTIFACTS.mkdir(parents=True, exist_ok=True)


def tearDownModule() -> None:
    for key, old in _SAVED_ENV.items():
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old
    if _TMP_DIR is not None:
        _release_log_files()
        try:                                                   # log aplikasi ikut jadi artefak (bahan diagnosis)
            shutil.copy(Path(_TMP_DIR) / ".kaptionpro" / "kaptionpro.log", ARTIFACTS / "kaptionpro.log")
        except OSError:
            pass
        _rmtree_retry(_TMP_DIR)


class GuiSmokeTests(unittest.TestCase):
    app = None
    yes = True                    # jawaban untuk dialog ya/tidak (askyesno / askyesnocancel)

    # ------------------------------------------------------------------ fixture
    @classmethod
    def setUpClass(cls) -> None:
        import tkinter.filedialog as filedialog
        import tkinter.messagebox as messagebox

        cls.dialogs: list[tuple[str, str, str]] = []
        cls.errors: list[str] = []
        cls._patches: list = []

        def recorder(kind: str, answer):
            def fake(*args, **kwargs):
                title = kwargs.get("title", args[0] if args else "")
                message = kwargs.get("message", args[1] if len(args) > 1 else "")
                cls.dialogs.append((kind, str(title), str(message)))
                return answer() if callable(answer) else answer
            return fake

        for kind, answer in (("showinfo", "ok"), ("showwarning", "ok"), ("showerror", "ok"),
                             ("askyesno", lambda: cls.yes),
                             ("askyesnocancel", lambda: True if cls.yes else None),
                             ("askokcancel", lambda: cls.yes)):
            patcher = mock.patch.object(messagebox, kind, recorder(kind, answer))
            patcher.start()
            cls._patches.append(patcher)
        for name in ("askopenfilename", "asksaveasfilename", "askdirectory"):
            patcher = mock.patch.object(filedialog, name, lambda *a, **k: "")
            patcher.start()
            cls._patches.append(patcher)

        from kaptionpro import config
        from kaptionpro.main import KaptionProApp
        cls.config = config
        if not str(config.APP_DIR).startswith(_TMP_DIR):
            raise AssertionError(f"APP_DIR tidak dialihkan ke folder sementara: {config.APP_DIR}")
        cls.app = KaptionProApp()
        cls.app.report_callback_exception = lambda exc, val, tb: cls.errors.append(f"{exc.__name__}: {val}")
        cls.app.geometry("1366x768+0+0")
        cls.pump(0.4)

    @classmethod
    def tearDownClass(cls) -> None:
        for patcher in cls._patches:
            patcher.stop()
        try:
            if cls.app is not None and cls.alive():
                cls.app.destroy()
        except Exception:  # noqa: BLE001
            pass

    def setUp(self) -> None:
        type(self).yes = True
        self.dialogs.clear()
        self.errors.clear()

    def tearDown(self) -> None:
        if self.alive():
            self.pump(0.05)
        self.assertEqual(self.errors, [], "ada error tak tertangani pada callback Tk")

    # ------------------------------------------------------------------ helper
    @classmethod
    def alive(cls) -> bool:
        import tkinter
        try:
            return bool(cls.app.winfo_exists())
        except tkinter.TclError:
            return False

    @classmethod
    def pump(cls, seconds: float = 0.05, until=None, timeout: float = 5.0) -> bool:
        """Jalankan event loop Tk secukupnya (tanpa mainloop). Dengan `until`: berhenti begitu kondisi terpenuhi."""
        end = time.monotonic() + (timeout if until is not None else seconds)
        while True:
            cls.app.update()
            if until is not None and until():
                return True
            if time.monotonic() >= end:
                return until is None
            time.sleep(0.01)

    def shot(self, name: str, widget=None) -> None:
        """Screenshot best-effort ke folder artefak (tidak pernah menggagalkan tes)."""
        try:
            from PIL import ImageGrab
            target = widget or self.app
            self.app.update_idletasks()
            self.app.update()
            x, y, w, h = target.winfo_rootx(), target.winfo_rooty(), target.winfo_width(), target.winfo_height()
            ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(ARTIFACTS / f"{name}.png")
        except Exception as e:  # noqa: BLE001
            print(f"[screenshot '{name}' dilewati: {type(e).__name__}: {e}]")

    def bad_dialogs(self) -> list[tuple[str, str, str]]:
        return [d for d in self.dialogs if d[0] in ("showerror", "showwarning")]

    def ensure_workspace(self):
        if self.app.current_screen != "workspace":
            self.app.start_new_project()                      # filedialog dipalsukan -> "workspace kosong" -> Ya
            self.pump(0.2)
        return self.app.screens["workspace"]

    @staticmethod
    def _segments(*texts: str):
        from kaptionpro.models import SubtitleSegment
        return [SubtitleSegment(i * 2.5, i * 2.5 + 2.0, t, []) for i, t in enumerate(texts)]

    # ------------------------------------------------------------------ tes
    def test_01_starts_on_welcome_with_isolated_data_dir(self):
        app = self.app
        self.assertEqual(app.current_screen, "welcome")
        self.assertIn("KaptionPro", app.title())
        self.assertEqual(set(app.screens), {"welcome", "workspace"})
        self.assertTrue(str(self.config.APP_DIR).startswith(_TMP_DIR), "data pengguna asli TIDAK boleh tersentuh")
        self.assertEqual(len(app.screens["welcome"]._cards), 0)
        self.assertEqual(self.bad_dialogs(), [])
        self.shot("01_welcome")

    def test_02_new_empty_project_opens_workspace(self):
        app = self.app
        app.start_new_project()
        self.pump(0.2)
        ws = app.screens["workspace"]
        self.assertEqual(app.current_screen, "workspace")
        self.assertTrue(ws.project.name.startswith("Untitled Project"))
        self.assertIn(ws.project.id, [p.id for p in app.store.projects])
        self.assertTrue(ws._lock_held, "lock project harus dipegang")
        self.assertEqual(self.bad_dialogs(), [])
        self.shot("02_workspace_empty")

    def test_03_editing_is_saved_to_disk_and_shown_in_views(self):
        ws = self.ensure_workspace()
        wanted = ["Halo dunia", "Apa kabar?", "Sampai jumpa"]
        ws.editor.replace_all(self._segments(*wanted), push_undo=False)
        self.pump(0.1)
        self.assertEqual([s.text for s in ws.segments], wanted)
        self.assertEqual(len(ws.timeline.segments), 3)
        self.assertTrue(ws._dirty)
        self.assertTrue(ws.flush())
        self.assertFalse(ws._dirty)
        self.assertEqual([s.text for s in self.app.store.load_segments_ex(ws.project).segments], wanted)
        self.assertNotIn("Gagal", str(ws.save_label.cget("text")))
        ws.seek_time(3.0, "timeline")                          # 3,0 dtk jatuh di baris ke-2 (2,5 - 4,5)
        self.assertEqual(ws._segment_at(3.0).text, "Apa kabar?")
        self.assertEqual(ws.video._caption, "Apa kabar?")
        self.shot("03_workspace_with_subtitles")

    def test_04_job_state_locks_and_restores_controls(self):
        ws = self.ensure_workspace()
        token = ws._begin_job("ai", "AI test")
        self.pump(0.05)
        self.assertEqual((ws._job_kind, ws._is_current(token)), ("ai", True))
        self.assertEqual(str(ws.btn_mp4.cget("state")), "disabled")
        self.assertEqual(str(ws.side.generate_btn.cget("state")), "disabled")
        ws._abort_job_silently()                               # ganti project / tutup aplikasi
        self.assertIsNone(ws._job_kind)
        self.assertFalse(ws._is_current(token), "hasil terlambat job lama harus dianggap basi")
        self.assertEqual(str(ws.btn_mp4.cget("state")), "normal")
        self.assertEqual(str(ws.side.generate_btn.cget("state")), "normal")

        token = ws._begin_job("export", "Export test")         # export hanya mengunci tombol export
        self.assertEqual(str(ws.btn_mp4.cget("state")), "disabled")
        self.assertEqual(str(ws.side.generate_btn.cget("state")), "normal")
        ws._end_job()
        self.assertEqual(str(ws.btn_mp4.cget("state")), "normal")

    def test_05_guide_dialog_privacy_controls_work(self):
        from kaptionpro.core.privacy import ACK_KEY
        from kaptionpro.ui.dialogs import GuideDialog
        app = self.app
        app.settings.set(ACK_KEY, {})
        dlg = GuideDialog(app)
        self.pump(0.3)
        try:
            self.assertIn("belum ada", str(dlg.privacy_label.cget("text")))
            self.shot("05_guide_dialog", dlg)

            app.settings.set(ACK_KEY, {"google": True})
            dlg._refresh_privacy()
            self.assertIn("Google", str(dlg.privacy_label.cget("text")))

            app.ai.cache.put_many({"kunci": "nilai"})
            self.assertGreater(app.ai.cache.count(), 0)
            dlg._clear_cache()                                 # konfirmasi dipalsukan -> Ya
            self.assertEqual(app.ai.cache.count(), 0)

            dlg._revoke_online()
            self.assertEqual(app.settings.get(ACK_KEY, {}), {})
            self.assertIn("belum ada", str(dlg.privacy_label.cget("text")))
            self.assertEqual(self.bad_dialogs(), [])
        finally:
            dlg.destroy()
            self.pump(0.05)

    def _prepare_generate(self, ws, translator: str):
        """Siapkan kondisi agar _generate_subtitles sampai ke pemeriksaan privasi, tanpa Whisper/jaringan asli."""
        video = Path(_TMP_DIR) / "dummy.mp4"
        video.write_bytes(b"x" * 2048)
        ws.project.video_path = str(video)
        ws.project.translator = translator

    def test_06_privacy_notice_is_per_provider_and_decline_starts_nothing(self):
        from kaptionpro.core.privacy import ACK_KEY
        from kaptionpro.services.ai_service import GoogleBackend, MyMemoryBackend
        app, ws = self.app, self.ensure_workspace()
        app.settings.set(ACK_KEY, {})
        self._prepare_generate(ws, "google")
        with mock.patch("kaptionpro.ui.workspace.HAS_FASTER_WHISPER", True), \
                mock.patch.object(GoogleBackend, "available", staticmethod(lambda: (True, ""))), \
                mock.patch.object(MyMemoryBackend, "available", staticmethod(lambda: (True, ""))):
            type(self).yes = False                             # TOLAK pada dialog privasi
            ws._generate_subtitles()
            self.pump(0.1)
            privacy = [d for d in self.dialogs if "Privasi" in d[1]]
            self.assertEqual(len(privacy), 1, self.dialogs)
            self.assertIn("ONLINE", privacy[0][2])
            self.assertIn(str(app.ai.cache.path), privacy[0][2])
            self.assertIsNone(ws._job_kind, "menolak = tidak ada job yang dimulai")
            self.assertEqual(app.settings.get(ACK_KEY, {}), {}, "menolak = tidak ada persetujuan tersimpan")

    def test_07_privacy_accept_is_remembered_per_provider(self):
        from kaptionpro.core.privacy import ACK_KEY
        from kaptionpro.core.utils import CancelledError
        from kaptionpro.services.ai_service import GoogleBackend, MyMemoryBackend
        app, ws = self.app, self.ensure_workspace()
        app.settings.set(ACK_KEY, {})
        self._prepare_generate(ws, "google")

        def run_generate() -> list:
            self.dialogs.clear()
            ws._generate_subtitles()
            self.pump(until=lambda: ws._job_kind is None and not ws.alive_workers(), timeout=10)
            return [d for d in self.dialogs if "Privasi" in d[1]]

        with mock.patch("kaptionpro.ui.workspace.HAS_FASTER_WHISPER", True), \
                mock.patch.object(GoogleBackend, "available", staticmethod(lambda: (True, ""))), \
                mock.patch.object(MyMemoryBackend, "available", staticmethod(lambda: (True, ""))), \
                mock.patch.object(app.ai, "transcribe", side_effect=CancelledError("uji")):
            self.assertEqual(len(run_generate()), 1, "Google: harus ditanya sekali")
            self.assertEqual(app.settings.get(ACK_KEY, {}), {"google": True})
            self.assertEqual(len(run_generate()), 0, "Google sudah disetujui: tidak ditanya lagi")

            ws.project.translator = "mymemory"
            type(self).yes = False                             # MyMemory belum disetujui -> ditanya, lalu ditolak
            self.assertEqual(len(run_generate()), 1, "MyMemory: persetujuan Google tidak berlaku")
            self.assertEqual(app.settings.get(ACK_KEY, {}), {"google": True})
        self.assertEqual([d for d in self.dialogs if d[0] == "showerror"], [])
        self.assertIsNone(ws._job_kind)

    def test_08_ui_queue_keeps_event_loop_responsive(self):
        """Banjir callback dari worker: timer UI tetap berdetak SELAMA antrean diproses (bukan setelahnya)."""
        app = self.app
        total, processed, ticks_during_backlog = 300, [0], [0]

        def work() -> None:
            time.sleep(0.002)
            processed[0] += 1

        def tick() -> None:
            if app._ui_queue.qsize() > 0:
                ticks_during_backlog[0] += 1
            if processed[0] < total:
                app.after(10, tick)

        def flood() -> None:
            for _ in range(total):
                app.post(work)

        app.after(10, tick)
        threading.Thread(target=flood, daemon=True).start()
        self.assertTrue(self.pump(until=lambda: processed[0] >= total, timeout=30),
                        f"hanya {processed[0]}/{total} callback terproses")
        self.assertGreaterEqual(ticks_during_backlog[0], 5,
                                "event loop tidak sempat berjalan di tengah antrean (UI tampak membeku)")

    def _resize(self, width: int, height: int, timeout: float = 3.0) -> bool:
        """Minta ukuran jendela lalu TUNGGU sampai window manager benar-benar menerapkannya.

        True bila ukuran nyata mendekati yang diminta. Di runner CI (terutama Windows) permintaan ukuran bisa
        diabaikan/ditunda/dijepit; pemanggil tidak boleh berasumsi ukuran sudah berubah hanya karena geometry() dipanggil.
        """
        app = self.app
        try:
            app.state("normal")                                # jangan sampai jendela sedang zoomed/maximized
        except Exception:  # noqa: BLE001
            pass
        app.geometry(f"{width}x{height}+0+0")
        app.update_idletasks()

        def honored() -> bool:
            return abs(app.winfo_width() - width) <= 40 and abs(app.winfo_height() - height) <= 40

        ok = self.pump(until=honored, timeout=timeout)
        self.pump(0.3)                                          # beri waktu debounce <Configure> (80 ms) bekerja
        return ok

    def _expected_mode(self, ws) -> str:
        w, h = ws.winfo_width(), ws.winfo_height()
        return "portrait" if (w < ws.PORTRAIT_BREAKPOINT or w < h) else "landscape"

    def test_09_layout_is_usable_at_1366x768_and_in_portrait(self):
        app, ws = self.app, self.ensure_workspace()
        try:
            honored = self._resize(1366, 768)
            ws._apply_layout()                                  # deterministik: tak menunggu event <Configure> dari OS
            self.pump(0.2)
            print(f"[test_09 landscape] diminta 1366x768, nyata {app.winfo_width()}x{app.winfo_height()}, "
                  f"diterapkan={honored}, mode={ws._mode}")
            self.assertEqual(ws._mode, self._expected_mode(ws),
                             "mode layout tidak konsisten dengan ukuran workspace yang sebenarnya")
            if honored:
                self.assertEqual(ws._mode, "landscape")
            bottom_limit = app.winfo_rooty() + app.winfo_height() + 2
            for name in ("export_bar", "jobbar"):
                widget = getattr(ws, name)
                self.assertTrue(widget.winfo_ismapped(), f"{name} tidak tampil")
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom_limit,
                                     f"{name} meluap keluar jendela 1366x768")
            self.shot("09_workspace_1366x768")

            honored = self._resize(700, 900)
            ws._apply_layout()
            self.pump(0.2)
            print(f"[test_09 portrait] diminta 700x900, nyata {app.winfo_width()}x{app.winfo_height()}, "
                  f"diterapkan={honored}, mode={ws._mode}")
            self.assertEqual(ws._mode, self._expected_mode(ws),
                             "mode layout tidak konsisten dengan ukuran workspace yang sebenarnya")
            if honored:
                self.assertEqual(ws._mode, "portrait")
            else:
                # Runner CI tidak mengubah ukuran jendela (bukan bug aplikasi). Cek tata letak portrait tetap
                # dijalankan dengan memaksa mode-nya, supaya cabang portrait tidak lolos tanpa diuji.
                ws._set_mode("portrait")
                self.pump(0.3)
                self.assertEqual(ws._mode, "portrait")
            self.assertTrue(ws.export_bar.winfo_ismapped())
            self.assertTrue(ws.jobbar.winfo_ismapped())
            self.shot("09_workspace_portrait_700x900")
        finally:
            self._resize(1366, 768)
            ws._apply_layout()
            self.pump(0.3)

    def test_10_back_home_lists_project_and_reopen_restores_subtitles(self):
        app, ws = self.app, self.ensure_workspace()
        ws.editor.replace_all(self._segments("Satu", "Dua", "Tiga"), push_undo=False)
        project_id = ws.project.id
        app.go_home()                                          # menyimpan + melepas lock
        self.pump(0.3)
        welcome = app.screens["welcome"]
        self.assertEqual(app.current_screen, "welcome")
        self.assertEqual(len(welcome._cards), len(app.store.projects))
        self.assertGreaterEqual(len(welcome._cards), 1)
        self.shot("10_welcome_with_recent")

        project = next(p for p in app.store.projects if p.id == project_id)
        app.open_project(project)
        self.pump(0.3)
        self.assertEqual(app.current_screen, "workspace")
        self.assertEqual([s.text for s in ws.segments], ["Satu", "Dua", "Tiga"], "subtitle harus pulih dari disk")
        self.assertEqual(self.bad_dialogs(), [])

    def test_11_subtitle_table_and_export_stay_visible_on_small_screens(self):
        """
        v1.9.14/15: di 1366x768 dulu tabel subtitle menyusut jadi hanya header; tombol Export tertutup di 1024x768.

        Tes ini TIDAK boleh lolos tanpa mengukur apa pun: penilaian memakai ukuran jendela yang NYATA (bukan yang diminta),
        jadi runner yang menolak resize tetap menghasilkan pengukuran. Skenario pertama ("apa adanya") tidak meminta
        resize sama sekali. Minimal satu skenario landscape wajib terukur.

        Catatan pengukuran: ws.side adalah CTkScrollableFrame; ws.side.winfo_height() adalah tinggi ISI di dalam kanvas
        (bisa 496 px dan itu normal, isinya di-scroll), BUKAN tinggi panel di grid. Tinggi panel yang sebenarnya adalah
        milik bingkai luarnya (_parent_frame).
        """
        from kaptionpro.core import layout
        app, ws = self.app, self.ensure_workspace()
        ws.editor.replace_all(self._segments(*[f"Baris {i}" for i in range(12)]), push_undo=False)
        measured: list[tuple[int, int]] = []
        try:
            for requested in (None, (1366, 768), (1024, 768), (1024, 680)):
                if requested is not None:
                    self._resize(*requested)
                ws._apply_layout()
                self.pump(0.6)                    # beri waktu penyetelan akhir tinggi panel (beberapa putaran @ 60 ms)
                aw, ah = app.winfo_width(), app.winfo_height()
                tree = ws.editor.tree
                outer = getattr(ws.side, "_parent_frame", ws.side)        # bingkai luar yang ada di grid
                outside = ws.winfo_height() - outer.winfo_height() - ws.editor.winfo_height()
                print(f"[test_11] diminta={requested or 'apa adanya'}, nyata {aw}x{ah}, mode={ws._mode}, "
                      f"panel preset: diminta={ws._side_h} nyata={outer.winfo_height()} (isi di dalam kanvas="
                      f"{ws.side.winfo_height()}), editor={ws.editor.winfo_height()}, tabel={tree.winfo_height()}, "
                      f"elemen lain={outside} (perkiraan {layout.OUTSIDE_H}), "
                      f"extra panel={outer.winfo_height() - (ws._side_h or 0)} (perkiraan {layout.SCROLLABLE_EXTRA_H})")
                self.shot(f"11_workspace_{aw}x{ah}")
                if ws._mode != "landscape" or ah < layout.MIN_WINDOW[1]:
                    continue                      # portrait / lebih pendek dari minsize: bukan sasaran tes ini
                want = layout.guaranteed_table_px(ah)
                self.assertGreaterEqual(tree.winfo_height(), want - 10,
                                        f"tabel subtitle {tree.winfo_height()}px di {aw}x{ah}, dijamin >= {want}px")
                self.assertGreater(outside, 100, f"elemen lain {outside}px: panel/editor tumpang-tindih atau salah ukur")
                self.assertLess(outside, 300, f"elemen lain {outside}px tidak masuk akal")
                bottom = app.winfo_rooty() + app.winfo_height() + 2
                for name, widget in (("export_bar", ws.export_bar), ("jobbar", ws.jobbar),
                                     ("editor", ws.editor), ("text_box", ws.editor.text_box)):
                    self.assertTrue(widget.winfo_ismapped(), f"{name} tidak tampil di {aw}x{ah}")
                    self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom,
                                         f"{name} meluap keluar jendela {aw}x{ah}")
                editor_bottom = ws.editor.winfo_rooty() + ws.editor.winfo_height() + 2
                self.assertLessEqual(ws.editor.text_box.winfo_rooty() + ws.editor.text_box.winfo_height(),
                                     editor_bottom, "form editor terpotong")
                measured.append((aw, ah))
        finally:
            self._resize(1366, 768)
            ws._apply_layout()
            self.pump(0.3)
        self.assertTrue(measured, "tidak ada satu pun skenario landscape yang terukur: tes ini tidak boleh lolos kosong")
        print(f"[test_11] skenario landscape yang terukur: {measured}")

    def test_12_theme_radius_is_applied_everywhere(self):
        import customtkinter as ctk
        from kaptionpro.config import Theme
        self.assertEqual(Theme.RADIUS, 4)
        for name in ("CTkButton", "CTkEntry", "CTkTextbox", "CTkOptionMenu", "CTkComboBox", "CTkFrame"):
            self.assertEqual(ctk.ThemeManager.theme[name]["corner_radius"], Theme.RADIUS, name)
        ws = self.ensure_workspace()
        # ws.side (CTkScrollableFrame) tidak mendukung cget('corner_radius'): dicakup lewat default tema di atas.
        for widget in (ws.video, ws.timeline, ws.editor, ws.btn_mp4, ws.btn_srt, ws.btn_ass):
            self.assertEqual(widget.cget("corner_radius"), Theme.RADIUS, type(widget).__name__)

    def test_99_close_saves_pending_edit_releases_lock_and_marks_clean_shutdown(self):
        from kaptionpro.core.storage import ProjectStore
        app, ws = self.app, self.ensure_workspace()
        ws.editor.replace_all(self._segments("Terakhir A", "Terakhir B"), push_undo=False)   # belum di-flush
        self.assertTrue(ws._dirty)
        project = ws.project
        app._on_close()
        self.assertTrue(self.pump(until=lambda: not self.alive(), timeout=20), "aplikasi tidak menutup")
        self.assertFalse(app.exit_hard, "tanpa worker tersisa: tidak boleh keluar paksa")

        fresh = ProjectStore(self.config.APP_DIR)
        self.assertEqual([s.text for s in fresh.load_segments_ex(project).segments], ["Terakhir A", "Terakhir B"],
                         "perubahan yang belum disimpan harus tersimpan saat menutup")

        # Lock project: lock di proses yang sama bersifat re-entrant (registri kelas), jadi ProjectStore baru tidak bisa
        # dipakai untuk memeriksanya. Periksa registri DAN coba kunci OS lewat deskriptor file terpisah.
        from kaptionpro.core.utils import InterProcessLock, _norm_path
        lock_path = app.store._lock_path(project)
        self.assertNotIn(_norm_path(str(lock_path)), ProjectStore._held_locks, "lock project tidak dilepas saat menutup")
        probe = InterProcessLock(lock_path)
        try:
            self.assertTrue(probe.acquire(write_info=False), "kunci OS project masih dipegang setelah aplikasi ditutup")
        finally:
            probe.release()

        # Penanda sesi: kunci dilepas dan penanda 'CLEAN' (POSIX: file dihapus; Windows: file dibiarkan berisi CLEAN).
        self.assertIsNone(app.store._session_lock, "session_end() tidak dipanggil saat menutup")
        for marker in (self.config.APP_DIR / "sessions").glob("*.session"):
            self.assertTrue(marker.read_text(encoding="utf-8").startswith("CLEAN"),
                            f"penutupan normal harus meninggalkan penanda CLEAN, bukan: {marker.name}")


if __name__ == "__main__":
    unittest.main()
