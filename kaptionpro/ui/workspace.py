"""Layar workspace: preview video, panel samping, timeline, job bar, WorkspaceScreen."""

from __future__ import annotations

import hashlib
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable, TYPE_CHECKING

from ..config import (
    EXPORT_QUALITIES,
    HAS_FASTER_WHISPER,
    HAS_VLC,
    LANGUAGE_PRESETS,
    LanguagePreset,
    TEXT_STYLES,
    TRANSLATORS,
    Theme,
    WHISPER_MODELS,
    _vlc,
    cached_font,
    ctk,
    log,
)
from ..core.storage import BackupInfo
from ..core.jobs import JobTracker
from ..core.layout import rebalanced_side_height, side_panel_height, side_panel_visible
from ..core.privacy import ACK_KEY, is_acknowledged, with_acknowledged
from ..core.utils import CancelledError, fmt_clock, normalize_hex, safe_filename
from ..models import (
    BLOCKING_ISSUES,
    ExportOptions,
    Project,
    SegmentIndex,
    SubtitleIO,
    SubtitleSegment,
    SubtitleStyle,
    VideoInfo,
)
from ..services.ai_service import BACKENDS, GoogleBackend, TRANSLATOR_SRC, needs_online_notice
from ..services.vlc_player import MediaPlayer
from .components import gold_button, section_header, small_button
from .dialogs import BackupDialog, ExportOptionsDialog, StyleDialog, ask_text
from .editor import SubtitleEditorPanel

if TYPE_CHECKING:
    from ..main import KaptionProApp


class VideoPreviewPanel(ctk.CTkFrame):
    """
    Preview: embed VLC. Subtitle dirender VLC dari ASS yang SAMA dengan export (preview = export);
    overlay Tk hanya dipakai bila VLC/slave tidak aktif (mode simulasi) dan saat dialog gaya
    dibuka (live preview). Tombol "🎯 Reload subs VLC" ada di top bar workspace.
    """

    SAMPLE = "Aku tidak akan melepaskanmu lagi."

    def __init__(self, master,
                 on_user_seek: Callable[[float], None],
                 on_tick: Callable[[float], None],
                 on_duration: Callable[[float], None],
                 on_vlc_ready: Callable[[], None] | None = None,
                 on_volume: Callable[[int, bool], None] | None = None,
                 on_player_error: Callable[[str], None] | None = None) -> None:
        super().__init__(master, fg_color=Theme.VIDEO_BG, corner_radius=Theme.RADIUS,
                         border_width=2, border_color=Theme.GOLD_DIM)
        self.on_user_seek = on_user_seek
        self.on_tick = on_tick
        self.on_duration = on_duration
        self.on_vlc_ready = on_vlc_ready
        self.on_volume = on_volume
        self.on_player_error = on_player_error
        self._fps = 0.0
        self._base_info = ""
        self._verify_id = None
        self._error_reported = False
        self._volume, self._muted = 80, False

        self._style = "karaoke"
        self._subtitle_style = SubtitleStyle()
        self._caption: str | None = None
        self._progress = 0.5
        self._pos, self._dur = 0.0, 60.0
        self._playing = False
        self._last_tick = 0.0
        self._tick_id = None
        self._dur_reported = False       # durasi dari VLC dilaporkan SEKALI per media
        self._vlc_ready_notified = False  # on_vlc_ready dipanggil SEKALI per media (saat pertama Playing)
        self._style_preview = False      # True saat dialog gaya terbuka (paksa overlay Tk)

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.surface = tk.Frame(self, bg=Theme.VIDEO_BG, highlightthickness=0)
        self.surface.grid(row=0, column=0, sticky="nsew", padx=2, pady=(2, 0))
        self.surface.grid_rowconfigure(0, weight=1)
        self.surface.grid_columnconfigure(0, weight=1)

        self.placeholder = ctk.CTkFrame(self.surface, fg_color="transparent")
        self.placeholder.grid(row=0, column=0, sticky="nsew")
        self.placeholder.grid_rowconfigure((0, 4), weight=1)
        self.placeholder.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.placeholder, text="▶", text_color=Theme.GOLD, font=Theme.font(40)).grid(row=1, column=0)
        self.mode_label = ctk.CTkLabel(
            self.placeholder,
            text="VIDEO PREVIEW (VLC)" if HAS_VLC else "VIDEO PREVIEW (simulasi — install python-vlc)",
            text_color=Theme.GOLD, font=Theme.font(12, "bold", serif=True))
        self.mode_label.grid(row=2, column=0)
        self.file_label = ctk.CTkLabel(self.placeholder, text="No video loaded", text_color=Theme.TEXT_MUTED,
                                       font=Theme.font(11), wraplength=420)
        self.file_label.grid(row=3, column=0, pady=(2, 8))

        # Overlay Tk (feedback cepat)
        self.caption_box = ctk.CTkFrame(self, corner_radius=Theme.RADIUS, fg_color="transparent")
        self.caption_box.grid(row=0, column=0, pady=(0, 60), sticky="s")
        self.cap_left = ctk.CTkLabel(self.caption_box, text="", font=Theme.font(15, "bold"))
        self.cap_right = ctk.CTkLabel(self.caption_box, text="", font=Theme.font(15, "bold"))
        self.cap_left.pack(side="left", padx=(10, 0), pady=4)
        self.cap_right.pack(side="left", padx=(0, 10), pady=4)

        self.player = MediaPlayer(self.surface)

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
        controls.grid_columnconfigure(1, weight=1)
        self.play_btn = ctk.CTkButton(controls, text="▶", width=38, height=32, corner_radius=Theme.RADIUS,
                                      fg_color=Theme.GOLD, hover_color=Theme.GOLD_HOVER, text_color="#121212",
                                      command=self._toggle_play)
        self.play_btn.grid(row=0, column=0, padx=(0, 10))
        self.slider = ctk.CTkSlider(controls, from_=0, to=1, command=self._on_slider,
                                    progress_color=Theme.GOLD, button_color=Theme.GOLD_BRIGHT,
                                    button_hover_color=Theme.GOLD_HOVER, fg_color=Theme.SURFACE_2)
        self.slider.set(0)
        self.slider.grid(row=0, column=1, sticky="ew")
        self.time_label = ctk.CTkLabel(controls, text="00:00 / 01:00", text_color=Theme.TEXT_MUTED,
                                       font=Theme.font(11), width=96)
        self.time_label.grid(row=0, column=2, padx=(10, 0))

        # Baris kontrol kedua: frame-by-frame, loncat 1 dtk, volume, info video
        ctrl2 = ctk.CTkFrame(self, fg_color="transparent")
        ctrl2.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 8))
        ctrl2.grid_columnconfigure(7, weight=1)
        for col, (text, cmd, width) in enumerate((("⏮ -1f", lambda: self.step_frame(-1), 54),
                                                  ("+1f ⏭", lambda: self.step_frame(+1), 54),
                                                  ("−1s", lambda: self.step_seconds(-1.0), 42),
                                                  ("+1s", lambda: self.step_seconds(+1.0), 42))):
            small_button(ctrl2, text, cmd, width).grid(row=0, column=col, padx=2)
        self.mute_btn = small_button(ctrl2, "🔊", self._toggle_mute, 34)
        self.mute_btn.grid(row=0, column=4, padx=(10, 2))
        self.vol_slider = ctk.CTkSlider(ctrl2, from_=0, to=100, number_of_steps=100, width=90,
                                        command=self._on_volume_slider, progress_color=Theme.GOLD,
                                        button_color=Theme.GOLD_BRIGHT, button_hover_color=Theme.GOLD_HOVER,
                                        fg_color=Theme.SURFACE_2)
        self.vol_slider.set(80)
        self.vol_slider.grid(row=0, column=5, padx=2)
        self.info_label = ctk.CTkLabel(ctrl2, text="", text_color=Theme.TEXT_MUTED, font=Theme.font(10), anchor="e")
        self.info_label.grid(row=0, column=7, sticky="e")

        self._render_caption()

    # ---------- API ----------
    def set_video(self, path: str) -> None:
        exists = bool(path) and Path(path).exists()
        if path and not exists:
            self.file_label.configure(text=f"⚠ File hilang: {Path(path).name}", text_color=Theme.DANGER_TEXT)
        else:
            self.file_label.configure(text=Path(path).name if path else "No video loaded",
                                      text_color=Theme.TEXT_MUTED)
        self._dur_reported = False
        self._vlc_ready_notified = False
        self._error_reported = False
        self.player.load(path if exists else "")      # path kosong = bersihkan media lama
        if self.player.available and exists:
            self.placeholder.grid_remove()
        else:
            self.placeholder.grid()
        self._render_caption()

    def set_position(self, t: float, duration: float) -> None:
        self._pos, self._dur = t, max(0.1, duration)
        # Slider digeser tanpa memicu callback (tidak menyebabkan set_time berulang).
        self.slider.set(min(1.0, t / self._dur))
        self.time_label.configure(text=f"{fmt_clock(t)} / {fmt_clock(self._dur)}")

    def apply_style(self, style_key: str) -> None:
        self._style = style_key
        self._render_caption()

    def apply_subtitle_style(self, style: SubtitleStyle) -> None:
        self._subtitle_style = style
        self._render_caption()

    def show_caption(self, text: str | None, progress: float = 0.5) -> None:
        self._caption, self._progress = text, progress
        self._render_caption()

    def stop(self) -> None:
        self._playing = False
        self.play_btn.configure(text="▶")
        if self._tick_id:
            self.after_cancel(self._tick_id)
            self._tick_id = None
        self.player.pause()

    # ---------- info, volume, frame-by-frame, verifikasi seek ----------
    def set_fps(self, fps: float) -> None:
        self._fps = fps if fps and fps > 0 else 0.0
        self._refresh_info()

    def set_info(self, text: str) -> None:
        self._base_info = text
        self._refresh_info()

    def _refresh_info(self, state_text: str = "") -> None:
        fps = self._fps or self.player.get_fps()
        parts = [p for p in (self._base_info, f"{fps:.2f} fps" if fps else "", state_text) if p]
        self.info_label.configure(text=" · ".join(parts))

    def set_volume_ui(self, volume: int, muted: bool) -> None:
        self._volume, self._muted = int(max(0, min(100, volume))), bool(muted)
        self.vol_slider.set(self._volume)
        self.mute_btn.configure(text="🔇" if self._muted else "🔊")
        self.player.set_volume(self._volume)
        self.player.set_mute(self._muted)

    def _notify_volume(self) -> None:
        if self.on_volume:
            self.on_volume(self._volume, self._muted)

    def _on_volume_slider(self, v: float) -> None:
        self._volume = int(round(float(v)))
        if self._muted and self._volume > 0:
            self._muted = False
            self.mute_btn.configure(text="🔊")
            self.player.set_mute(False)
        self.player.set_volume(self._volume)
        self._notify_volume()

    def _toggle_mute(self) -> None:
        self._muted = not self._muted
        self.mute_btn.configure(text="🔇" if self._muted else "🔊")
        self.player.set_mute(self._muted)
        self._notify_volume()

    def _frame_seconds(self) -> float:
        fps = self._fps or self.player.get_fps() or 24.0
        return 1.0 / fps if fps > 1.0 else 1.0 / 24.0

    def step_frame(self, direction: int) -> None:
        """Maju/mundur satu frame (video dijeda). VLC tidak punya 'prev frame' -> mundur = seek -1 frame."""
        if self._playing:
            self._stop_internal()
            self.player.pause()
        if direction > 0 and self.player.available and self.player.has_media and self.player.next_frame():
            self.after(80, self._sync_after_step)             # baca posisi nyata setelah VLC melangkah
            return
        self.on_user_seek(max(0.0, min(self._dur, self._pos + direction * self._frame_seconds())))

    def _sync_after_step(self) -> None:
        t = self.player.get_time()
        if t >= 0:
            self._pos = t
            self.on_tick(t)

    def step_seconds(self, delta: float) -> None:
        self.on_user_seek(max(0.0, min(self._dur, self._pos + delta)))

    def verify_seek(self, target: float) -> None:
        """
        Sinkronisasi A/V setelah seek: bila VLC meleset >1,5 dtk dari target, kirim ulang seek (sekali).
        Saat media masih Opening/Buffering, tunggu sebentar (maks. 6x) lalu cek lagi.
        """
        if not (self.player.available and self.player.has_media):
            return
        if self._verify_id:
            self.after_cancel(self._verify_id)
        self._verify_id = self.after(500, lambda: self._do_verify(target, 0))

    def _do_verify(self, target: float, attempt: int) -> None:
        self._verify_id = None
        if not self.player.available or _vlc is None:
            return
        state = self.player.get_state()
        if state in (_vlc.State.Playing, _vlc.State.Paused):
            cur = self.player.get_time()
            expected = target + (0.5 if state == _vlc.State.Playing else 0.0)
            if cur >= 0 and abs(cur - expected) > 1.5 and attempt < 2:
                log.info("Seek meleset (target %.1f, VLC %.1f) -> kirim ulang", target, cur)
                self.player.set_time(target)
                self._verify_id = self.after(500, lambda: self._do_verify(target, attempt + 1))
        elif attempt < 6:
            self._verify_id = self.after(500, lambda: self._do_verify(target, attempt + 1))

    def shutdown(self) -> None:
        """Lepaskan resource pemutar (dipanggil saat aplikasi ditutup)."""
        self.stop()
        self.player.close()

    def begin_style_preview(self) -> None:
        """Dialog gaya dibuka: matikan subtitle VLC & tampilkan overlay Tk untuk live preview."""
        self._style_preview = True
        self.player.set_spu(-1)
        self._render_caption()

    def end_style_preview(self) -> None:
        self._style_preview = False
        self._render_caption()

    def refresh_overlay(self) -> None:
        self._render_caption()

    def _stop_internal(self) -> None:
        self._playing = False
        self.play_btn.configure(text="▶")
        if self._tick_id:
            self.after_cancel(self._tick_id)
            self._tick_id = None

    # ---------- internal ----------
    def _render_caption(self) -> None:
        text = self.SAMPLE if self._caption is None else self._caption
        if (self.player.vlc_subs_active and not self._style_preview) or not text.strip():
            self.caption_box.grid_remove()      # VLC sudah merender subtitle -> jangan tampil dua kali
            return
        self.caption_box.grid()
        one = " ".join(text.split())
        if len(one) > 70:
            one = one[:67] + "…"

        st = self._subtitle_style
        fs = max(12, int(st.font_size_pct * 4))
        weight = "bold" if st.bold else "normal"
        font = cached_font(st.font_name, fs, weight)

        if self._style == "karaoke":
            self.caption_box.configure(fg_color="transparent")
            words = one.split(" ")
            n = max(1, len(words) // 2) if self._caption is None else int(round(len(words) * self._progress))
            left, right = " ".join(words[:n]), " ".join(words[n:])
            if left and right:
                left += " "
            self.cap_left.configure(text=left, text_color=normalize_hex(st.primary_hex), font=font)
            self.cap_right.configure(text=right, text_color=normalize_hex(st.secondary_hex), font=font)
        else:
            self.caption_box.configure(fg_color="#000000")
            self.cap_left.configure(text=one, text_color=normalize_hex(st.secondary_hex), font=font)
            self.cap_right.configure(text="")

    def _on_slider(self, fraction: float) -> None:
        # Slider = user intent → teruskan ke workspace yang akan set_time ke VLC.
        self.on_user_seek(fraction * self._dur)

    def _toggle_play(self) -> None:
        if self._playing:
            self._stop_internal()
            self.player.pause()
            return
        if self._pos >= self._dur - 0.05:
            self.on_user_seek(0.0)
            self.player.set_time(0.0)
        self._playing = True
        self.play_btn.configure(text="⏸")
        self._last_tick = time.monotonic()
        if self.player.available:
            self.player.play()
        self._tick()

    def _tick(self) -> None:
        if not self._playing:
            return
        now = time.monotonic()
        if self.player.available and _vlc is not None:
            state = self.player.get_state()
            if state == _vlc.State.Error:
                self._stop_internal()
                if not self._error_reported:                  # laporkan sekali; jangan spam dialog
                    self._error_reported = True
                    log.error("VLC melaporkan error saat memutar %s", self.player.current_path)
                    if self.on_player_error:
                        self.on_player_error("VLC gagal memutar video ini (codec/berkas tidak didukung?). "
                                             "Subtitle tetap bisa diedit; coba format lain atau pasang VLC terbaru.")
                return
            if state == _vlc.State.Ended:
                self.on_user_seek(self._dur)
                self._stop_internal()
                return
            name = self.player.state_name()
            self._refresh_info(name if name in ("membuka", "buffering") else "")
            self.player.apply_pending_seek()
            if state == _vlc.State.Playing:
                self.player.verify_subtitle_track()
            if not self._vlc_ready_notified and state == _vlc.State.Playing:
                self._vlc_ready_notified = True           # media sudah benar-benar jalan
                if self.on_vlc_ready and not self.player.vlc_subs_active:
                    self.on_vlc_ready()                   # coba lagi daftarkan subtitle (add_slave tadi gagal)
            t = self.player.get_time()
            if t >= 0:
                self._pos = t
            if not self._dur_reported:           # workspace pemilik durasi; lapor sekali saja
                dur = self.player.get_duration()
                if dur > 0:
                    self._dur_reported = True
                    self.on_duration(dur)
        else:
            self._pos += now - self._last_tick
        self._last_tick = now
        if self._dur > 0 and self._pos >= self._dur:
            self.on_user_seek(self._dur)
            self._stop_internal()
            return
        # Tick → on_tick (workspace akan update tampilan TANPA set_time VLC).
        self.on_tick(self._pos)
        self._tick_id = self.after(80, self._tick)


class SidePanel(ctk.CTkScrollableFrame):
    def __init__(self, master, on_preset, on_style, on_generate, on_style_dialog, on_model_changed,
                 on_translator_changed=None) -> None:
        super().__init__(master, fg_color=Theme.SURFACE, corner_radius=Theme.RADIUS, border_width=1,
                         border_color=Theme.BORDER, scrollbar_button_color=Theme.GOLD_DIM,
                         scrollbar_button_hover_color=Theme.GOLD)
        self.on_preset, self.on_style = on_preset, on_style
        self.on_style_dialog = on_style_dialog
        self.on_model_changed = on_model_changed
        self.on_translator_changed = on_translator_changed
        self._preset_buttons: dict[str, ctk.CTkButton] = {}

        section_header(self, "LANGUAGE PRESETS")
        for key, preset in LANGUAGE_PRESETS.items():
            btn = ctk.CTkButton(self, text=preset.label, height=34, corner_radius=Theme.RADIUS, anchor="w",
                                font=Theme.font(13, "bold"), command=lambda k=key: self._select_preset(k))
            btn.pack(fill="x", padx=14, pady=3)
            self._preset_buttons[key] = btn

        ctk.CTkLabel(self, text="Whisper model", text_color=Theme.TEXT_MUTED, anchor="w",
                     font=Theme.font(11)).pack(fill="x", padx=14, pady=(6, 0))
        self.model_var = ctk.StringVar(value="small")
        self.model_menu = ctk.CTkOptionMenu(
            self, values=WHISPER_MODELS, variable=self.model_var,
            fg_color=Theme.SURFACE_2, button_color=Theme.GOLD_DIM, button_hover_color=Theme.GOLD,
            text_color=Theme.TEXT, font=Theme.font(12),
            command=lambda v: self.on_model_changed(v))
        self.model_menu.pack(fill="x", padx=14, pady=(0, 4))

        ctk.CTkLabel(self, text="Penerjemah", text_color=Theme.TEXT_MUTED, anchor="w",
                     font=Theme.font(11)).pack(fill="x", padx=14, pady=(4, 0))
        self.translator_var = ctk.StringVar(value=TRANSLATORS["google"])
        self.translator_menu = ctk.CTkOptionMenu(
            self, values=list(TRANSLATORS.values()), variable=self.translator_var,
            fg_color=Theme.SURFACE_2, button_color=Theme.GOLD_DIM, button_hover_color=Theme.GOLD,
            text_color=Theme.TEXT, font=Theme.font(12), command=self._on_translator_menu)
        self.translator_menu.pack(fill="x", padx=14, pady=(0, 4))

        self.generate_btn = gold_button(self, "✨  Generate Subtitles (AI)", on_generate, filled=False, height=36)
        self.generate_btn.pack(fill="x", padx=14, pady=(6, 2))

        section_header(self, "TEXT STYLES")
        self.style_var = ctk.StringVar(value="karaoke")
        for key, label in TEXT_STYLES.items():
            ctk.CTkRadioButton(self, text=label, value=key, variable=self.style_var,
                               command=lambda: self.on_style(self.style_var.get()),
                               fg_color=Theme.GOLD, hover_color=Theme.GOLD_HOVER, border_color=Theme.GOLD_DIM,
                               text_color=Theme.TEXT, font=Theme.font(13)).pack(anchor="w", padx=18, pady=5)

        small_button(self, "⚙  Pengaturan font/posisi…", on_style_dialog, 220).pack(padx=14, pady=(6, 12))
        self.set_active_preset("kdrama")

    def _select_preset(self, key: str) -> None:
        self.set_active_preset(key)
        self.on_preset(key)

    def set_active_preset(self, key: str) -> None:
        for k, btn in self._preset_buttons.items():
            if k == key:
                btn.configure(fg_color=Theme.GOLD, hover_color=Theme.GOLD_HOVER, text_color="#121212", border_width=0)
            else:
                btn.configure(fg_color="transparent", hover_color=Theme.SURFACE_2, text_color=Theme.GOLD,
                              border_width=1, border_color=Theme.GOLD_DIM)

    def set_style(self, key: str) -> None:
        self.style_var.set(key)

    def _on_translator_menu(self, label: str) -> None:
        key = next((k for k, v in TRANSLATORS.items() if v == label), "google")
        if self.on_translator_changed:
            self.on_translator_changed(key)

    def set_translator(self, key: str) -> None:
        self.translator_var.set(TRANSLATORS.get(key, TRANSLATORS["google"]))

    def set_model(self, key: str) -> None:
        self.model_var.set(key if key in WHISPER_MODELS else "small")

    def set_busy(self, busy: bool) -> None:
        self.generate_btn.configure(state="disabled" if busy else "normal",
                                    text="⏳  Working..." if busy else "✨  Generate Subtitles (AI)")


class TimelineBar(ctk.CTkFrame):
    TRACKS = (("video", "🎞  VIDEO"), ("audio", "🔊  AUDIO"), ("subtitle", "💬  SUBTITLE"))
    TRACK_HEIGHT = 34

    def __init__(self, master, on_seek_time: Callable[[float], None]) -> None:
        super().__init__(master, fg_color=Theme.SURFACE, corner_radius=Theme.RADIUS, border_width=1,
                         border_color=Theme.BORDER)
        self.on_seek_time = on_seek_time
        self.segments: list[SubtitleSegment] = []
        self.duration = 60.0
        self.playhead = 0.0
        self.video_name = "Video clip (placeholder)"
        self._redraw_id = None
        rng = __import__("random").Random(7)
        self._wave = [rng.uniform(0.2, 1.0) for _ in range(600)]

        self.grid_columnconfigure(1, weight=1)
        self.ruler = tk.Canvas(self, height=20, bg=Theme.SURFACE, highlightthickness=0)
        self.ruler.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=(6, 1))
        self.canvases: dict[str, tk.Canvas] = {}
        for row, (key, label) in enumerate(self.TRACKS, start=1):
            ctk.CTkLabel(self, text=label, text_color=Theme.GOLD, width=96, anchor="w",
                         font=Theme.font(11, "bold")).grid(row=row, column=0, padx=(12, 6), pady=2)
            cv = tk.Canvas(self, height=self.TRACK_HEIGHT, bg=Theme.SURFACE_2, highlightthickness=0)
            cv.grid(row=row, column=1, sticky="ew", padx=(0, 12), pady=2)
            self.canvases[key] = cv
        ctk.CTkFrame(self, height=2, fg_color="transparent").grid(row=4, column=0)

        for cv in (self.ruler, *self.canvases.values()):
            cv.bind("<Button-1>", self._on_click)
            cv.bind("<B1-Motion>", self._on_click)
        self.ruler.bind("<Configure>", self._schedule_redraw)

    def set_segments(self, segments: list[SubtitleSegment]) -> None:
        self.segments = segments
        self._schedule_redraw()

    def set_duration(self, duration: float) -> None:
        self.duration = max(1.0, duration)
        self._schedule_redraw()

    def set_playhead(self, seconds: float) -> None:
        """Geser playhead saja (murah) — tidak menggambar ulang seluruh timeline."""
        self.playhead = seconds
        self._move_playhead()

    def _move_playhead(self) -> None:
        w = self.ruler.winfo_width()
        if w <= 1:
            return
        x = max(0.0, min(1.0, self.playhead / self.duration)) * w
        for canvas in (self.ruler, *self.canvases.values()):
            items = canvas.find_withtag("playhead")
            if items:
                canvas.coords(items[0], x, 0, x, 60)
            else:
                canvas.create_line(x, 0, x, 60, fill=Theme.GOLD_BRIGHT, width=2, tags="playhead")

    def set_video_name(self, name: str) -> None:
        self.video_name = name or "Video clip (placeholder)"
        self._schedule_redraw()

    def _on_click(self, event) -> None:
        w = event.widget.winfo_width()
        if w > 1:
            self.on_seek_time(max(0.0, min(self.duration, event.x / w * self.duration)))

    def _schedule_redraw(self, _event=None) -> None:
        if self._redraw_id:
            self.after_cancel(self._redraw_id)
        self._redraw_id = self.after(30, self._redraw)

    def _redraw(self) -> None:
        w = self.ruler.winfo_width()
        if w <= 1:
            return
        pps = w / self.duration
        mid = self.TRACK_HEIGHT / 2

        self.ruler.delete("all")
        step = next((s for s in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600) if s * pps >= 70), 1200)
        t = 0
        while t <= self.duration:
            x = t * pps
            self.ruler.create_line(x, 12, x, 20, fill=Theme.GOLD_DIM)
            self.ruler.create_text(x + 3, 6, anchor="w", fill=Theme.TEXT_MUTED, font=("Segoe UI", 8),
                                   text=fmt_clock(t))
            t += step

        cv = self.canvases["video"]
        cv.delete("all")
        cv.create_rectangle(2, 4, w - 2, self.TRACK_HEIGHT - 4, fill=Theme.GOLD_DIM, outline=Theme.GOLD)
        cv.create_text(10, mid, anchor="w", fill="#121212", text=self.video_name[:60],
                       font=("Segoe UI", 9, "bold"))

        cv = self.canvases["audio"]
        cv.delete("all")
        for i, x in enumerate(range(2, w - 2, 4)):
            amp = self._wave[i % len(self._wave)] * (mid - 4)
            cv.create_line(x, mid - amp, x, mid + amp, fill=Theme.GOLD, width=2)

        cv = self.canvases["subtitle"]
        cv.delete("all")
        if not self.segments:
            cv.create_text(10, mid, anchor="w", fill=Theme.TEXT_MUTED, font=("Segoe UI", 9),
                           text="No subtitles yet - Generate (AI) atau Import .srt")
        for seg in self.segments:
            x1, x2 = seg.start * pps, max(seg.end * pps, seg.start * pps + 2)
            cv.create_rectangle(x1, 4, x2, self.TRACK_HEIGHT - 4, fill=Theme.GOLD, outline=Theme.GOLD_BRIGHT)
            if x2 - x1 > 46:
                cv.create_text(x1 + 5, mid, anchor="w", fill="#121212", font=("Segoe UI", 8, "bold"),
                               text=seg.text.replace("\n", " ")[: int((x2 - x1) / 6)])

        self._move_playhead()


class JobBar(ctk.CTkFrame):
    def __init__(self, master, on_cancel) -> None:
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        self._label = ""
        self._indeterminate = False
        self.status = ctk.CTkLabel(self, text="Ready", text_color=Theme.TEXT_MUTED, anchor="w",
                                   font=Theme.font(11))
        self.status.grid(row=0, column=0, sticky="ew")
        self.progress = ctk.CTkProgressBar(self, width=170, height=8, progress_color=Theme.GOLD,
                                           fg_color=Theme.SURFACE_2)
        self.cancel_btn = ctk.CTkButton(self, text="✕ Cancel", width=80, height=26, corner_radius=Theme.RADIUS,
                                        fg_color=Theme.SURFACE_2, hover_color=Theme.DANGER,
                                        text_color=Theme.TEXT, font=Theme.font(11, "bold"), command=on_cancel)

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def begin(self, label: str) -> None:
        self._label = label
        self._indeterminate = True
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self.progress.grid(row=0, column=1, padx=8)
        self.cancel_btn.grid(row=0, column=2)
        self.status.configure(text=label)

    def set_phase(self, label: str) -> None:
        """Ganti fase proses (mis. Transkripsi -> Terjemahan): progres dikembalikan ke indeterminate."""
        self._label = label
        self._indeterminate = True
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self.status.configure(text=label)

    def update_progress(self, fraction: float | None, text: str = "") -> None:
        if fraction is not None:
            if self._indeterminate:
                self.progress.stop()
                self.progress.configure(mode="determinate")
                self._indeterminate = False
            self.progress.set(fraction)
            self.status.configure(text=f"{self._label} · {int(fraction * 100)}% · {text[:48]}")
        else:
            self.status.configure(text=f"{self._label} · {text[:48]}")

    def end(self) -> None:
        self.progress.stop()
        self.progress.grid_remove()
        self.cancel_btn.grid_remove()
        self._indeterminate = False


class WorkspaceScreen(ctk.CTkFrame):
    PORTRAIT_BREAKPOINT = 980
    AUTOSAVE_MS = 800
    VLC_SUB_DEBOUNCE_MS = 1000
    MAX_VLC_SLAVES = 25              # setelah ini media dimuat ulang agar track subtitle tidak menumpuk

    def __init__(self, master, app: "KaptionProApp") -> None:
        super().__init__(master, fg_color=Theme.BG, corner_radius=0)
        self.app = app
        self.project: Project | None = None
        self.segments: list[SubtitleSegment] = []
        self.video_info: VideoInfo | None = None
        self.duration = 60.0
        self._mode: str | None = None
        self._side_h: int | None = None      # tinggi panel preset yang sedang diterapkan (mode landscape)
        self._side_est: int | None = None    # perkiraan awal terakhir dari side_panel_height (sebelum penyetelan akhir)
        self._rebalance_id = None
        self._rebalance_passes = 0
        self._after_id = None
        self._autosave_id = None
        self._vlc_sub_id = None
        self._dirty = False
        self._pending_snapshot = False      # snapshot menunggu untuk dihapus setelah flush sukses
        self._last_vlc_digest = ""          # hash ASS terakhir yang BENAR-BENAR diterima VLC
        self._job_thread: threading.Thread | None = None
        self._side_visible = True            # keadaan EFEKTIF panel preset (otomatis atau pilihan pengguna)
        self._side_user: bool | None = None  # pilihan pengguna (tombol Panel); None = otomatis (jendela pendek -> sembunyi)
        self._vol_save_id = None
        self._save_failed = False
        self._discard_pending = False          # True sesaat setelah user memilih membuang perubahan (bisa dibatalkan)
        self._lock_held = False                # True bila lock project benar-benar dipegang
        self._save_error = ""                  # alasan simpan terakhir yang gagal (untuk dialog)
        self._index = SegmentIndex()           # pencarian subtitle aktif O(log n)
        self._jobs = JobTracker()                      # status job aktif (jenis, token, cancel_event, worker)

        # Top bar
        top = ctk.CTkFrame(self, fg_color=Theme.SURFACE, corner_radius=0)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(top, text="← Home", width=80, height=32, fg_color="transparent", hover_color=Theme.SURFACE_2,
                      text_color=Theme.GOLD, border_width=1, border_color=Theme.GOLD_DIM,
                      command=app.go_home).grid(row=0, column=0, padx=10, pady=8)
        self.title_label = ctk.CTkLabel(top, text="Untitled Project", text_color=Theme.GOLD_BRIGHT, anchor="w",
                                        font=Theme.font(15, "bold", serif=True))
        self.title_label.grid(row=0, column=1, sticky="ew", padx=6)
        self.save_label = ctk.CTkLabel(top, text="", text_color=Theme.TEXT_MUTED, font=Theme.font(11))
        self.save_label.grid(row=0, column=2, padx=8)
        small_button(top, "✎ Rename", self._rename_project, 80).grid(row=0, column=3, padx=3)
        small_button(top, "🗂 Backup", self._open_backups, 82).grid(row=0, column=4, padx=3)
        small_button(top, "▣ Panel", self._toggle_side, 70).grid(row=0, column=5, padx=3)
        small_button(top, "🎯 Subs VLC", lambda: self._reload_vlc_subs(force=True), 96).grid(row=0, column=6, padx=3)
        small_button(top, "📖 Panduan", app.show_guide, 90).grid(row=0, column=7, padx=(3, 10))
        # Jalan pintas Generate: tombol aslinya ada DI DALAM panel preset, jadi saat panel disembunyikan (jendela pendek)
        # aksi utama ini tetap harus terjangkau. Hanya tampil ketika panel tersembunyi (_sync_generate_shortcut).
        self.btn_generate_top = small_button(top, "✨ Generate", self._generate_subtitles, 92)
        self.btn_generate_top.grid(row=0, column=8, padx=(0, 10))
        self.btn_generate_top.grid_remove()

        # Komponen utama
        self.video = VideoPreviewPanel(
            self,
            on_user_seek=lambda t: self.seek_time(t, "slider"),
            on_tick=lambda t: self.seek_time(t, "tick"),
            on_duration=self._on_player_duration,
            on_vlc_ready=self._on_vlc_ready,
            on_volume=self._on_volume,
            on_player_error=self._on_player_error,
        )
        # Panel preset dibungkus wadah berukuran TETAP: CTkScrollableFrame tidak mau lebih pendek dari ~210 px karena
        # scrollbar-nya meminta 200 px; wadah (grid_propagate(False)) memaksa tinggi yang dihitung core/layout.py.
        self.side_box = tk.Frame(self, bg=Theme.BG, highlightthickness=0, bd=0, width=500, height=250)
        self.side_box.grid_propagate(False)
        self.side_box.grid_rowconfigure(0, weight=1)
        self.side_box.grid_columnconfigure(0, weight=1)
        self.side = SidePanel(self.side_box, self._on_preset, self._on_style, self._generate_subtitles,
                              self._open_style_dialog, self._on_model_changed, self._on_translator_changed)
        self.side.grid(row=0, column=0, sticky="nsew")
        self.timeline = TimelineBar(self, on_seek_time=lambda t: self.seek_time(t, "timeline"))
        self.editor = SubtitleEditorPanel(self, on_changed=self._on_segments_changed,
                                          on_select=lambda seg: self.seek_time(seg.start, "editor"),
                                          get_playhead=lambda: self.video._pos,
                                          before_op=self._backup)
        self.segments = self.editor.segments
        self.video.set_volume_ui(int(app.settings.get("volume", 80)), bool(app.settings.get("muted", False)))

        self.export_bar = ctk.CTkFrame(self, fg_color="transparent")
        self.export_bar.grid_columnconfigure((0, 1, 2), weight=1, uniform="export")
        self.btn_mp4 = gold_button(self.export_bar, "", self._export_video)
        self.btn_srt = gold_button(self.export_bar, "", self._export_srt, filled=False)
        self.btn_ass = gold_button(self.export_bar, "", self._export_ass, filled=False)
        for col, btn in enumerate((self.btn_mp4, self.btn_srt, self.btn_ass)):
            btn.grid(row=0, column=col, padx=3, sticky="ew")
        self.jobbar = JobBar(self, self._cancel_job)

        self._set_mode("landscape")
        self.bind("<Configure>", self._on_configure)
        self._bind_shortcuts()

    # ---------- Layout ----------
    def _on_configure(self, event) -> None:
        if event.widget is not self:
            return
        if self._after_id:
            self.after_cancel(self._after_id)
        self._after_id = self.after(80, self._apply_layout)

    def _apply_layout(self) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            return
        mode = "portrait" if (w < self.PORTRAIT_BREAKPOINT or w < h) else "landscape"
        want = side_panel_visible(mode, h, self._widget_scaling(), self._side_user)
        if want != self._side_visible:                           # jendela memendek/memanjang melewati batas autohide
            self._side_visible = want
            self._mode = None                                    # paksa susun ulang grid
        self._set_mode(mode)
        self._fit_side_height()

    def _widget_scaling(self) -> float:
        try:
            return float(self._get_widget_scaling())             # API privat CTk; gagal -> skala 1
        except Exception:  # noqa: BLE001
            return 1.0

    def _set_side_height(self, logical: int) -> None:
        """Tinggi panel preset (px logis) -> tinggi wadah tetap (px fisik)."""
        self.side_box.configure(height=max(1, int(round(logical * self._widget_scaling()))))

    def _fit_side_height(self) -> None:
        """
        Landscape: tinggi panel preset mengikuti tinggi jendela supaya tabel subtitle & form editor tidak terjepit
        (dulu tetap 250 px sehingga di layar 1366x768 tabel menyusut jadi hanya baris header).
        Dua tahap: (1) perkiraan dari core/layout.py, (2) penyetelan akhir dari ukuran tabel yang NYATA
        (_rebalance_side), karena perkiraan konstanta pernah meleset puluhan piksel.
        """
        if self._mode != "landscape" or not self._side_visible:
            return                                               # panel tersembunyi: tidak ada yang perlu disetel
        estimate = side_panel_height(self.winfo_height(), self._widget_scaling())
        if estimate != self._side_est:                           # jendela berubah tinggi: hitung ulang dari awal
            self._side_est = estimate
            self._rebalance_passes = 0
            if estimate != self._side_h:
                self._side_h = estimate
                self._set_side_height(estimate)
        self._schedule_rebalance()

    def _schedule_rebalance(self) -> None:
        if self._rebalance_id is None:
            self._rebalance_id = self.after(60, self._rebalance_side)

    def _rebalance_side(self) -> None:
        """Ukur tinggi tabel subtitle yang sebenarnya; bila di bawah TABLE_MIN_PX, kecilkan panel preset sebesar kekurangannya."""
        self._rebalance_id = None
        try:
            if self._mode != "landscape" or not self._side_visible or not self.winfo_exists():
                return
            self.update_idletasks()
            table_px = self.editor.tree.winfo_height()
        except tk.TclError:                                      # widget sudah dihancurkan (aplikasi ditutup)
            return
        target = rebalanced_side_height(self._side_h or 0, table_px, self._widget_scaling())
        if self._side_h is not None and target != self._side_h:
            self._side_h = target
            self._set_side_height(target)
            self._rebalance_passes += 1
            if self._rebalance_passes < 4:                       # cukup beberapa putaran; hindari loop tak berujung
                self._schedule_rebalance()

    def _set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        for widget in (self.video, self.side_box, self.timeline, self.editor, self.export_bar, self.jobbar):
            widget.grid_forget()
        for r in range(8):
            self.grid_rowconfigure(r, weight=0, minsize=0)

        if mode == "landscape":
            self.grid_columnconfigure(0, weight=1, minsize=380)
            self.grid_columnconfigure(1, weight=0, minsize=500)
            self.grid_rowconfigure(2, weight=1)
            self._side_h = self._side_est = None   # paksa hitung ulang untuk ukuran jendela saat ini
            self._fit_side_height()
            self.video.grid(row=1, column=0, rowspan=2, sticky="nsew", padx=(12, 6), pady=(10, 6))
            if self._side_visible:
                self.side_box.grid(row=1, column=1, sticky="nsew", padx=(6, 12), pady=(10, 6))
                self.editor.grid(row=2, column=1, rowspan=2, sticky="nsew", padx=(6, 12), pady=6)
            else:           # panel presets disembunyikan: editor memakai seluruh kolom kanan
                self.editor.grid(row=1, column=1, rowspan=3, sticky="nsew", padx=(6, 12), pady=(10, 6))
            self.timeline.grid(row=3, column=0, sticky="ew", padx=(12, 6), pady=6)
            self.export_bar.grid(row=4, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 2))
            self.jobbar.grid(row=5, column=0, columnspan=2, sticky="ew", padx=16, pady=(2, 8))
            labels = ("⬇  Export Video (.mp4)", "⬇  Subtitle (.srt)", "⬇  Karaoke (.ass)")
        else:
            self.grid_columnconfigure(0, weight=1, minsize=0)
            self.grid_columnconfigure(1, weight=0, minsize=0)
            self.grid_rowconfigure(1, weight=3, minsize=140)
            self.grid_rowconfigure(4, weight=4, minsize=260)
            self._side_h = self._side_est = None
            self._set_side_height(150)
            for row, widget in enumerate((self.video, self.side_box, self.timeline, self.editor), start=1):
                if widget is self.side_box and not self._side_visible:
                    continue
                widget.grid(row=row, column=0, columnspan=2, sticky="nsew" if row in (1, 4) else "ew",
                            padx=12, pady=5)
            self.export_bar.grid(row=5, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 2))
            self.jobbar.grid(row=6, column=0, columnspan=2, sticky="ew", padx=16, pady=(2, 8))
            labels = ("⬇ MP4", "⬇ SRT", "⬇ ASS")
        for btn, text in zip((self.btn_mp4, self.btn_srt, self.btn_ass), labels):
            btn.configure(text=text)
        self._sync_generate_shortcut()

    def _sync_generate_shortcut(self) -> None:
        """Tombol Generate di top bar tampil hanya saat panel preset (tempat tombol aslinya) tersembunyi."""
        if self._side_visible:
            self.btn_generate_top.grid_remove()
        else:
            self.btn_generate_top.grid()

    # ---------- Project lifecycle ----------
    def load_project(self, project: Project) -> bool:
        """
        Buka project. Pindah project = reset state sepenuhnya.
        Return False bila user membatalkan (mis. project sedang dibuka di instance KaptionPro lain).
        """
        # 1) Simpan project lama — bila GAGAL, user memutuskan (coba lagi / lanjut tanpa simpan / batal)
        if not self.ensure_saved("membuka project lain"):
            return False
        self._abort_job_silently()
        old = self.project
        if old is not None and old.id != project.id:
            self.app.store.unlock(old)

        # 2) Lock ATOMIK project baru; bila dipegang proses lain yang masih hidup, minta keputusan user
        self._lock_held = self.app.store.lock(project)
        if not self._lock_held:
            err = self.app.store.last_lock_error
            if err:      # lock TIDAK bisa dibuat -> tidak ada perlindungan akses bersamaan
                prompt = (f"Lock project TIDAK bisa dibuat ({err}).\n\nTanpa lock, KaptionPro tidak bisa "
                          "memastikan project ini tidak sedang dibuka di instance lain — perubahan bisa saling "
                          "menimpa.\n\nTetap buka TANPA perlindungan lock?")
            else:
                prompt = (f"Project '{project.name}' sedang dibuka di instance KaptionPro lain.\n\n"
                          "Melanjutkan berisiko saling menimpa perubahan. Tetap buka?")
            if not messagebox.askyesno("KaptionPro", prompt):
                # Batal: user tetap di project lama. Ambil lagi lock lama DAN sinkronkan status internal;
                # _lock_held sebelumnya berisi hasil lock project BARU (False) sehingga salah bila tak diperbarui.
                if old is not None and old.id != project.id:
                    self._lock_held = self.app.store.lock(old)
                    if not self._lock_held:
                        log.warning("Lock project lama tidak bisa diambil kembali: %s",
                                    self.app.store.last_lock_error or "dipegang proses lain")
                        self.set_status("⚠ Lock project tidak bisa diambil kembali — tanpa perlindungan lock")
                self._undo_discard()
                self._update_save_indicator()
                return False
        self.video.stop()

        # 3) Reset state
        self.project = project
        self.video_info = None
        self._dirty = False
        self._pending_snapshot = False
        self._discard_pending = False                 # aksi berlanjut -> pembuangan final
        self._last_vlc_digest = ""
        self.app.store.last_save_error = None
        self._save_failed = False

        # 4) Muat subtitle (validasi penuh) + pemulihan snapshot
        res = self.app.store.load_segments_ex(project)
        segments = res.segments
        notes = list(res.notes)
        snapshot = self.app.store.load_snapshot(project)
        if snapshot is not None:
            when = self.app.store.snapshot_time(project)
            use_snapshot = messagebox.askyesno(
                "KaptionPro",
                f"Ditemukan snapshot pemulihan ({when}).\n\n"
                "Yes = muat snapshot (versi sebelum operasi besar terakhir).\n"
                "No  = pakai file subtitle terbaru."
            )
            if use_snapshot:
                segments = snapshot
                try:
                    self.app.store.save_segments(project, segments)   # snapshot jadi state resmi
                except OSError as e:
                    # JANGAN hapus snapshot: bisa jadi ini satu-satunya salinan pemulihan. Ia baru dihapus
                    # setelah autosave pertama yang berhasil (flush = commit point).
                    log.warning("Gagal menulis snapshot sebagai state resmi: %s", e)
                    notes.append(f"Snapshot dimuat, tetapi GAGAL ditulis sebagai file resmi ({e}).\n"
                                 "Snapshot TIDAK dihapus; KaptionPro akan mencoba menyimpan lagi. "
                                 "Periksa ruang disk / izin folder ~/.kaptionpro.")
                    self._pending_snapshot = True
                    self._dirty = True
                    self._schedule_autosave()
                else:
                    self.app.store.delete_snapshot(project)
            else:
                self.app.store.delete_snapshot(project)          # user memilih file terbaru

        # 5) Muat isi tampilan
        self.title_label.configure(text=project.name)
        self.video.set_video(project.video_path)
        self.video.player.set_time(0.0)      # eksplisit: project baru mulai dari 0 (ditunda bila belum Playing)
        self.video.apply_subtitle_style(project.subtitle_style())
        self.timeline.set_video_name(Path(project.video_path).name if project.video_path else "")
        self.side.set_active_preset(project.preset_key)
        self.side.set_style(project.style_key)
        self.side.set_model(project.whisper_model)
        self.side.set_translator(project.translator)
        self.video.apply_style(project.style_key)
        self.video.set_info("")
        self.video.set_fps(0.0)

        self.editor.set_segments(segments)
        self.segments = self.editor.segments
        self._refresh_views()
        self.seek_time(0.0, "load")
        self._schedule_vlc_subs()  # tulis preview ASS + daftarkan ke VLC

        # 6) Perbaikan otomatis saat memuat -> tulis ulang file yang bersih; beri tahu user
        if res.migrated or res.source == "backup" or res.report.notes():
            self._dirty = True
            self._schedule_autosave()
        if notes:
            messagebox.showwarning("KaptionPro", "Catatan saat memuat subtitle:\n\n- " + "\n- ".join(notes))
        integ = SubtitleIO.integrity_report(self.editor.segments)
        if project.video_path and not project.video_exists:
            self.set_status(f"⚠ Video tidak ditemukan: {project.video_path}")
            messagebox.showwarning("KaptionPro", f"File video tidak ditemukan:\n{project.video_path}\n\n"
                                                 "Project tetap dibuka, tapi export video tidak akan jalan.")
        else:
            extra = f" · ⚠ {integ['blocking']} baris bermasalah (gunakan Auto-Fix)" if integ["blocking"] else ""
            if not self._lock_held:
                extra += " · ⚠ tanpa perlindungan lock"
            self.set_status(f"Project loaded: {project.name} · {len(self.segments)} lines{extra}")
            if project.video_exists:
                self._probe_async(project)
        self._update_save_indicator()
        log.info("Project loaded: %s (%s) source=%s", project.name, project.id, res.source)
        return True

    def _rename_project(self) -> None:
        if not self.project:
            return
        name = ask_text(self, "Rename project", "Nama project:", self.project.name)
        if name:
            self.project.name = name
            self.title_label.configure(text=name)
            self.app.store.add(self.project)

    def _probe_async(self, project: Project) -> None:
        pid, path, app = project.id, project.video_path, self.app

        def worker() -> None:
            app.post(self._on_probed, pid, app.ffmpeg.probe(path))

        threading.Thread(target=worker, daemon=True, name="kp-probe").start()

    def _on_probed(self, pid: str, info: VideoInfo | None) -> None:
        if not (self.project and self.project.id == pid) or info is None:
            return
        self.video_info = info
        self.video.set_info(f"{info.width}×{info.height}")
        self.video.set_fps(info.fps)
        self._refresh_views()
        self.set_status(f"Video: {info.width}×{info.height} · {fmt_clock(info.duration)} · {len(self.segments)} lines")

    def set_status(self, text: str) -> None:
        self.jobbar.set_status(text)
        log.info("Status: %s", text)

    # ---------- Sync ----------
    def _compute_duration(self) -> float:
        last_end = max((s.end for s in self.segments), default=0.0)
        base = self.video_info.duration if self.video_info and self.video_info.duration > 0 else 0.0
        return max(base, last_end + 5.0) if base else max(60.0, last_end + 5.0)

    def _refresh_views(self) -> None:
        self.duration = self._compute_duration()
        self.timeline.set_duration(self.duration)
        self.timeline.set_segments(self.segments)
        self._index.rebuild(self.segments)

    def _on_player_duration(self, duration: float) -> None:
        if self.video_info is None:
            w, h = self.video.player.get_video_size()
            self.video_info = VideoInfo(width=w or 1920, height=h or 1080, duration=duration,
                                        dims_known=bool(w and h))
        elif self.video_info.duration <= 0:
            self.video_info.duration = duration
        self._refresh_views()

    def _segment_at(self, t: float) -> SubtitleSegment | None:
        return self._index.at(t)             # binary search (bisect), bukan scan linear

    def seek_time(self, t: float, source: str = "") -> None:
        """Satu pintu untuk semua perpindahan waktu.

        source "tick"/"refresh"/"load" → JANGAN set_time VLC (posisi berasal dari VLC sendiri, atau
        hanya penyegaran tampilan setelah edit — re-seek akan membuat video tersendat).
        Sumber lain (slider/timeline/editor) → set_time VLC supaya video ikut berpindah.
        """
        t = max(0.0, min(self.duration, t))
        self.timeline.set_playhead(t)
        self.video.set_position(t, self.duration)
        if source not in ("tick", "refresh", "load") and self.video.player.available:
            self.video.player.set_time(t)
            self.video.verify_seek(t)          # pastikan VLC benar-benar mendarat di target (sinkron A/V)
        seg = self._segment_at(t)
        if seg:
            self.video.show_caption(seg.text, (t - seg.start) / max(0.01, seg.duration))
            if source in ("timeline", "slider", "tick"):
                self.editor.select_segment(seg)
        else:
            self.video.show_caption("" if self.segments else None)

    def _on_segments_changed(self, segments: list[SubtitleSegment]) -> None:
        self.segments = segments
        self._dirty = True
        self._update_save_indicator()
        self._refresh_views()
        self.seek_time(self.video._pos, "refresh")
        self._schedule_autosave()
        self._schedule_vlc_subs()
        self.set_status(f"{len(segments)} lines")

    # ---------- Indikator simpan, backup, panel ----------
    def _update_save_indicator(self) -> None:
        st = self.app.store
        if st.last_save_error or self._save_failed:
            text, color = "⚠ Gagal menyimpan", Theme.DANGER_TEXT
        elif self._dirty:
            text, color = "● Belum tersimpan", Theme.WARN_TEXT
        elif st.last_saved_at:
            text, color = f"💾 Tersimpan {st.last_saved_at}", Theme.OK
        else:
            text, color = "", Theme.TEXT_MUTED
        self.save_label.configure(text=text, text_color=color)

    def _backup(self, reason: str) -> None:
        """Backup berversi (dipanggil editor sebelum operasi besar). Disk dipastikan up-to-date dulu."""
        if self.project and self.segments:
            self.flush()
            self.app.store.create_backup(self.project, self.segments, reason, force=True)

    def _open_backups(self) -> None:
        if not self.project:
            return
        BackupDialog(self, self.app.store.list_backups(self.project), self._restore_backup)

    def _restore_backup(self, info: BackupInfo) -> None:
        try:
            res = self.app.store.load_backup(info.path)
        except (OSError, ValueError) as e:
            messagebox.showerror("KaptionPro", f"Backup tidak bisa dibaca:\n{e}")
            return
        if self.segments and not messagebox.askyesno(
                "KaptionPro", f"Pulihkan backup {info.saved_at}?\n\nSubtitle saat ini ({len(self.segments)} baris) "
                              f"diganti dengan {len(res.segments)} baris. Versi sekarang dibackup dulu dan bisa di-Undo."):
            return
        self._backup("pre-restore")
        self.editor.replace_all(res.segments)
        self.set_status(f"✓ Backup {info.saved_at} dipulihkan ({len(res.segments)} baris)")

    def _toggle_side(self) -> None:
        """
        Sembunyikan/tampilkan panel presets (layar pendek: editor jadi lebih lega). Pilihan pengguna menang atas
        otomatis; bila pilihannya sama dengan yang akan dipilih otomatis, kembali mengikuti otomatis (None).
        """
        want = not self._side_visible
        auto = side_panel_visible(self._mode, self.winfo_height(), self._widget_scaling(), None)
        self._side_user = None if want == auto else want
        self._side_visible = want
        self._mode = None                      # paksa susun ulang layout
        self._apply_layout()
        if self._mode is None:
            self._set_mode("landscape")

    def _on_translator_changed(self, key: str) -> None:
        if self.project:
            self.project.translator = key
            self.app.store.add(self.project)
        self.app.settings.set("translator", key)
        cls = BACKENDS.get(key, GoogleBackend)
        ok, hint = cls.available()
        self.set_status(f"Penerjemah: {cls.label}" if ok else f"⚠ {cls.label} belum terpasang — {hint}")

    def _on_volume(self, volume: int, muted: bool) -> None:
        """Volume disimpan ke settings dengan jeda (slider memicu banyak event)."""
        if self._vol_save_id:
            self.after_cancel(self._vol_save_id)
        def save() -> None:
            self._vol_save_id = None
            self.app.settings.set("volume", int(volume))
            self.app.settings.set("muted", bool(muted))
        self._vol_save_id = self.after(400, save)

    def _on_player_error(self, message: str) -> None:
        self.set_status("⚠ " + message)
        messagebox.showwarning("KaptionPro", message)

    # ---------- Shortcut keyboard ----------
    def _typing(self) -> bool:
        """True bila fokus ada di kolom teks (spasi/panah harus diketik, bukan jadi shortcut)."""
        try:
            w = self.focus_get()
        except (KeyError, tk.TclError):
            return False
        return bool(w is not None and w.winfo_class() in ("Entry", "Text", "TEntry", "TCombobox"))

    def _bind_shortcuts(self) -> None:
        top = self.winfo_toplevel()

        def kb(fn: Callable[[], None], allow_typing: bool = False):
            def handler(_event=None):
                if self.app.current_screen != "workspace" or (self._typing() and not allow_typing):
                    return None
                fn()
                return "break"
            return handler

        for seq, fn, typing_ok in (
                ("<space>", self.video._toggle_play, False),
                ("<Left>", lambda: self.video.step_frame(-1), False),
                ("<Right>", lambda: self.video.step_frame(+1), False),
                ("<Shift-Left>", lambda: self.video.step_seconds(-1.0), False),
                ("<Shift-Right>", lambda: self.video.step_seconds(+1.0), False),
                ("<Control-Left>", lambda: self._jump_subtitle(-1), False),
                ("<Control-Right>", lambda: self._jump_subtitle(+1), False),
                ("<Control-s>", self._save_now, True)):
            top.bind(seq, kb(fn, typing_ok), add="+")

    def _jump_subtitle(self, direction: int) -> None:
        pos = self.video._pos
        seg = self._index.next_after(pos) if direction > 0 else self._index.prev_before(pos)
        if seg is not None:
            self.seek_time(seg.start, "editor")
            self.editor.select_segment(seg)

    def _save_now(self) -> None:
        """Ctrl+S: simpan sekarang + backup berversi."""
        if self.project:
            self._dirty = self._dirty or bool(self.segments)
            if not self.flush():                  # JANGAN tampilkan "Tersimpan" bila simpan gagal
                err = self._save_error or self.app.store.last_save_error or "penyebab tidak diketahui"
                self.set_status(f"⚠ GAGAL menyimpan: {err}")
                messagebox.showwarning("KaptionPro", f"Subtitle BELUM tersimpan:\n{err}\n\n"
                                                     "KaptionPro akan terus mencoba menyimpan ulang otomatis.")
                return
            self.app.store.create_backup(self.project, self.segments, "manual-save", force=True)
            self.set_status("💾 Tersimpan")

    # ---------- Autosave + snapshot lifecycle ----------
    def _schedule_autosave(self) -> None:
        if self._autosave_id:
            self.after_cancel(self._autosave_id)
        self._autosave_id = self.after(self.AUTOSAVE_MS, self.flush)

    def _schedule_vlc_subs(self) -> None:
        if self._vlc_sub_id:
            self.after_cancel(self._vlc_sub_id)
        self._vlc_sub_id = self.after(self.VLC_SUB_DEBOUNCE_MS, self._reload_vlc_subs)

    def _reload_vlc_subs(self, force: bool = False) -> None:
        """Tulis preview ASS lalu daftarkan ke VLC (preview = export).

        Dilewati bila isi ASS tidak berubah DAN slave sebelumnya benar-benar aktif (kecuali force).
        Digest hanya disimpan bila VLC menerima slave — bila ditolak (mis. media belum Playing)
        percobaan berikutnya tidak boleh dilewati. Tiap add_slave menambah 1 track, jadi setelah
        MAX_VLC_SLAVES media dimuat ulang (posisi dipertahankan) supaya tidak menumpuk."""
        self._vlc_sub_id = None
        player = self.video.player
        if not (self.project and player.has_media):
            return
        try:
            w, h = self._play_res()
            ass = SubtitleIO.build_ass(self.segments, self.project.style_key,
                                       self.project.subtitle_style(), w, h)
            digest = hashlib.md5(ass.encode("utf-8")).hexdigest()
            if not force and digest == self._last_vlc_digest and player.vlc_subs_active:
                return
            path = self.app.store.preview_ass_path(self.project)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(ass, encoding="utf-8")
            if player.slave_count >= self.MAX_VLC_SLAVES:
                player.reload_keep_position()
                self._last_vlc_digest = ""
            ok = player.add_subtitle_slave(str(path), select=True)
            self._last_vlc_digest = digest if ok else ""
            self.video.refresh_overlay()          # overlay Tk tampil lagi bila slave gagal
            log.info("VLC subtitle slave %s: %s (slaves=%d)", "diperbarui" if ok else "BELUM terdaftar",
                     path, player.slave_count)
        except OSError as e:
            log.warning("Gagal menulis preview ASS: %s", e)

    def _on_vlc_ready(self) -> None:
        """VLC baru mulai Playing. Bila subtitle belum terdaftar (add_slave tadi ditolak), coba lagi."""
        if self.project and not self.video.player.vlc_subs_active:
            self._reload_vlc_subs(force=True)

    def flush(self) -> bool:
        """
        Simpan subtitle sekarang bila ada perubahan.
        True  = tidak ada yang perlu disimpan, atau berhasil disimpan.
        False = GAGAL (indikator merah + coba lagi otomatis). Pemanggil yang akan meninggalkan data
                (pindah project / Home / tutup aplikasi) wajib memeriksa nilai ini -> lihat ensure_saved().
        """
        if self._autosave_id:
            self.after_cancel(self._autosave_id)
            self._autosave_id = None
        if not (self.project and self._dirty):
            return True
        try:
            self.app.store.save_segments(self.project, self.segments)
            self.app.store.add(self.project)
            self._dirty = False
            self._save_failed = False
            self._save_error = ""
            # Hapus snapshot SETELAH autosave sukses — inilah "commit point".
            if self._pending_snapshot:
                self.app.store.delete_snapshot(self.project)
                self._pending_snapshot = False
            self.app.store.create_backup(self.project, self.segments, "autosave")   # dibatasi interval & perubahan
            log.info("Autosaved %d lines for %s", len(self.segments), self.project.name)
            ok = True
        except OSError as e:
            log.error("Autosave gagal: %s", e)
            self._save_failed = True
            self._save_error = str(e)
            self.set_status(f"⚠ Autosave gagal: {e} — mencoba lagi…")
            self._autosave_id = self.after(5000, self.flush)          # jangan menyerah diam-diam
            ok = False
        self._update_save_indicator()
        return ok

    def ensure_saved(self, action: str) -> bool:
        """
        Pastikan perubahan tersimpan SEBELUM `action` ("berpindah ke Home", "membuka project lain",
        "menutup aplikasi"). True = aman lanjut (tersimpan, atau user SENGAJA memilih membuang).
        False = batal, tetap di sini. Bila simpan gagal: coba lagi / lanjut tanpa menyimpan / batal.
        """
        self._discard_pending = False
        while True:
            if self.flush():
                return True
            err = self._save_error or self.app.store.last_save_error or "penyebab tidak diketahui"
            ans = messagebox.askyesnocancel(
                "KaptionPro",
                f"Sebelum {action}, perubahan terakhir GAGAL disimpan:\n{err}\n\n"
                "Ya     = coba simpan lagi\n"
                "Tidak  = lanjut TANPA menyimpan (perubahan terakhir akan hilang)\n"
                "Batal  = tetap di sini")
            if ans is None:
                return False
            if ans is False:
                self._offer_emergency_copy()
                self._discard_unsaved_changes()
                return True
            # ans True -> ulangi flush

    def _discard_unsaved_changes(self) -> None:
        """
        User SENGAJA membuang perubahan yang gagal tersimpan. Batalkan timer autosave (retry 5 dtk dari
        flush() yang gagal) dan reset status dirty/gagal supaya flush() berikutnya TIDAK menyimpan lagi
        perubahan yang sudah ditolak. Snapshot pemulihan di disk TIDAK dihapus (bisa jadi satu-satunya
        salinan); hanya penanda 'hapus setelah flush' yang dilepas.
        Ditandai sementara (_discard_pending): bila aksi pemintanya batal setelahnya, _undo_discard()
        mengembalikan keadaan karena perubahan masih ada di memori.
        """
        if self._autosave_id:
            self.after_cancel(self._autosave_id)
            self._autosave_id = None
        self._dirty = False
        self._save_failed = False
        self._save_error = ""
        self._pending_snapshot = False
        self.app.store.last_save_error = None
        self._discard_pending = True
        log.warning("Perubahan yang gagal tersimpan DIBUANG atas pilihan user (%s)",
                    self.project.name if self.project else "-")
        self._update_save_indicator()

    def _undo_discard(self) -> None:
        """Dipanggil bila aksi yang tadi memilih 'lanjut tanpa menyimpan' ternyata dibatalkan: perubahan
        masih ada di memori, jadi tandai dirty lagi dan jadwalkan autosave (jangan diam-diam kehilangan)."""
        if not self._discard_pending:
            return
        self._discard_pending = False
        if self.project:
            self._dirty = True
            self._schedule_autosave()
            self._update_save_indicator()

    def _offer_emergency_copy(self) -> None:
        """Sebelum membuang perubahan yang gagal tersimpan: tawarkan salinan SRT ke lokasi lain."""
        if not self.segments:
            return
        if not messagebox.askyesno("KaptionPro", "Simpan salinan darurat subtitle (.srt) ke lokasi lain "
                                                 "(mis. folder Dokumen) sebelum lanjut?"):
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".srt", filetypes=[("SubRip Subtitle", "*.srt")],
            initialfile=safe_filename((self.project.name if self.project else "subtitle") + "_darurat") + ".srt")
        if not path:
            return
        try:
            SubtitleIO.write_srt(self.segments, path)
            self.set_status(f"✓ Salinan darurat disimpan: {path}")
        except OSError as e:
            messagebox.showerror("KaptionPro", f"Salinan darurat juga gagal disimpan:\n{e}")

    def leave(self) -> bool:
        """
        Kembali ke Home: simpan, jeda video (agar audio tidak terus jalan), lepas lock.
        Bila AI/Export masih berjalan, minta konfirmasi (proses tidak boleh berjalan diam-diam
        di belakang dengan lock yang sudah dilepas). Return False bila user memilih tetap di sini.
        """
        if not self.ensure_saved("berpindah ke Home"):
            return False                         # simpan gagal & user membatalkan -> tetap di workspace
        if self._job_kind:
            label = "AI" if self._job_kind == "ai" else "Export"
            if not messagebox.askyesno("KaptionPro", f"Proses {label} masih berjalan.\n"
                                                     "Batalkan dan kembali ke Home?"):
                self._undo_discard()             # user tetap di sini -> perubahan yang tadi dibuang dipulihkan
                return False
            self._abort_job_silently()           # AI: snapshot ikut dibuang; Export: FFmpeg dihentikan
        self.video.stop()
        if self.project:
            self.app.store.unlock(self.project)
        return True

    def _discard_snapshot(self) -> None:
        """Hapus snapshot yang menunggu (mis. AI dibatalkan/gagal) agar tidak jadi 'alarm palsu' saat dibuka lagi."""
        if self.project and self._pending_snapshot:
            self.app.store.delete_snapshot(self.project)
            self._pending_snapshot = False

    def _snapshot(self) -> bool:
        """Ambil snapshot sebelum operasi besar. True bila berhasil tertulis (pemanggil memeriksa hasilnya)."""
        if not self.project:
            return False
        if self.app.store.save_snapshot(self.project, self.segments):
            self._pending_snapshot = True
            return True
        return False

    def _on_preset(self, key: str) -> None:
        if self.project:
            self.project.preset_key = key
            self.app.store.add(self.project)
        self.set_status(f"Language preset: {LANGUAGE_PRESETS[key].label}")

    def _on_style(self, style_key: str) -> None:
        if self.project:
            self.project.style_key = style_key
            self.app.store.add(self.project)
        self.video.apply_style(style_key)
        self._schedule_vlc_subs()
        self.set_status(f"Text style: {TEXT_STYLES[style_key]}")

    def _on_model_changed(self, model_key: str) -> None:
        if self.project:
            self.project.whisper_model = model_key
            self.app.store.add(self.project)
        self.set_status(f"Whisper model: {model_key}")

    def _open_style_dialog(self) -> None:
        if not self.project:
            return
        current = self.project.subtitle_style()

        def apply_style(style: SubtitleStyle) -> None:
            self.project.set_subtitle_style(style)
            self.video.apply_subtitle_style(style)
            self.app.store.add(self.project)
            self.set_status("Style applied")

        def finish() -> None:
            # Dialog ditutup (Terapkan ATAU Batal): kembalikan subtitle VLC dengan ASS terbaru.
            self.video.end_style_preview()
            self._reload_vlc_subs(force=True)

        self.video.begin_style_preview()
        StyleDialog(self, current, on_apply=apply_style,
                    preview_cb=lambda s: self.video.apply_subtitle_style(s), on_close=finish)

    # ---------- Job management ----------
    def _start_worker(self, thread: threading.Thread) -> None:
        """Mulai worker dan CATAT. Job yang dibatalkan melepas 'job aktif' segera, tetapi thread-nya baru benar-benar
        berhenti beberapa saat kemudian (FFmpeg di-terminate/kill, panggilan jaringan selesai). Daftar ini
        satu-satunya cara tahu bahwa worker memang sudah berhenti (join(timeout) yang habis BUKAN bukti berhenti)."""
        self._job_thread = thread
        self._jobs.start_worker(thread)

    def alive_workers(self) -> list[threading.Thread]:
        """Worker yang masih berjalan (termasuk yang pekerjaannya sudah dibatalkan tapi belum selesai berhenti)."""
        return self._jobs.alive_workers()

    def _workers_still_stopping(self) -> bool:
        """True bila job baru belum boleh dimulai karena worker lama belum berhenti (mencegah dua worker saling
        menimpa state layanan AI/FFmpeg)."""
        if self.alive_workers():
            self.set_status("Proses sebelumnya masih dihentikan — coba lagi sesaat lagi…")
            return True
        return False

    # Status job didelegasikan ke JobTracker; properti ini menjaga kode/pemanggil lama tetap berfungsi.
    @property
    def _job_kind(self) -> str | None:
        return self._jobs.kind

    @property
    def cancel_event(self) -> threading.Event:
        return self._jobs.cancel_event

    def _begin_job(self, kind: str, label: str) -> int:
        token = self._jobs.begin(kind)
        self.jobbar.begin(label)
        self._set_controls_busy(True)
        return token

    def _end_job(self) -> None:
        self._jobs.end()
        self.jobbar.end()
        self._set_controls_busy(False)

    def _is_current(self, token: int) -> bool:
        return self._jobs.is_current(token)

    def _cancel_job(self) -> None:
        kind = self._jobs.request_cancel()
        if kind:
            self.jobbar.set_status("Cancelling...")
            log.info("User requested cancel (%s)", kind)

    def _abort_job_silently(self) -> None:
        kind = self._jobs.kind
        if kind:
            if kind == "ai":
                self._discard_snapshot()
            self._jobs.abort()                         # cancel + token naik + tidak aktif
            self.jobbar.end()
            self._set_controls_busy(False)

    def _set_controls_busy(self, busy: bool) -> None:
        ai_busy = busy and self._job_kind == "ai"
        self.side.set_busy(ai_busy)
        self.btn_generate_top.configure(state="disabled" if ai_busy else "normal",
                                        text="⏳ Working..." if ai_busy else "✨ Generate")
        state = "disabled" if busy else "normal"
        for btn in (self.btn_mp4, self.btn_srt, self.btn_ass):
            btn.configure(state=state)

    def _on_job_progress(self, token: int, fraction: float | None, text: str) -> None:
        if self._is_current(token):
            self.jobbar.update_progress(fraction, text)

    def _on_job_cancelled(self, token: int) -> None:
        if self._is_current(token):
            kind = self._job_kind
            self._end_job()
            if kind == "ai":
                self._discard_snapshot()
            self.set_status("Cancelled")

    def _on_job_failed(self, token: int, error: Exception) -> None:
        if self._is_current(token):
            kind = self._job_kind
            self._end_job()
            if kind == "ai":
                self._discard_snapshot()
            self.set_status("Process failed")
            messagebox.showerror("KaptionPro", f"Proses gagal:\n{error}")

    # ---------- AI ----------
    @staticmethod
    def _decide_source(preset: LanguagePreset, lang: str | None, prob: float) -> tuple[str | None, str, bool]:
        """
        Tentukan bahasa sumber terjemahan dari deteksi Whisper (konsisten untuk seluruh file).
        Return (kode_penerjemah, catatan, lewati_terjemahan).
        """
        det = (lang or "").lower()[:2]
        preset_code = preset.whisper_code
        if det == "id":
            return None, "Sudah bahasa Indonesia — terjemahan dilewati.", True
        if preset_code is None:                                   # Auto Detect
            if det:
                note = f"Bahasa terdeteksi: {det} ({prob:.0%})" + (" — deteksi kurang yakin" if prob < 0.5 else "")
                return TRANSLATOR_SRC.get(det, det), note, False
            return None, "Bahasa tidak terdeteksi — penerjemah menebak sendiri.", False
        if det and det != preset_code and prob >= 0.8:             # preset salah pilih
            note = f"Preset {preset.source_name}, tetapi terdeteksi '{det}' ({prob:.0%}) — memakai '{det}'."
            return TRANSLATOR_SRC.get(det, det), note, False
        return TRANSLATOR_SRC.get(preset_code, preset_code), "", False

    def _on_job_phase(self, token: int, label: str) -> None:
        if self._is_current(token):
            self.jobbar.set_phase(label)

    def _generate_subtitles(self) -> None:
        if self._job_kind or not self.project or self._workers_still_stopping():
            return
        if not HAS_FASTER_WHISPER:
            messagebox.showerror("KaptionPro",
                                 "faster-whisper belum terpasang.\n\n"
                                 "Install: pip install faster-whisper\nLalu restart KaptionPro.")
            return
        if not self.project.video_path or not self.project.video_exists:
            messagebox.showwarning("KaptionPro", "Tidak ada video untuk ditranskripsi.")
            return
        tr_cls = BACKENDS.get(self.project.translator, GoogleBackend)
        tr_ok, tr_hint = tr_cls.available()
        if not tr_ok and not messagebox.askyesno(
                "KaptionPro", f"Penerjemah '{tr_cls.label}' belum tersedia ({tr_hint}).\n\n"
                              "Lanjut hanya transkripsi? (Teks tetap bahasa asli.)"):
            return
        ack = self.app.settings.get(ACK_KEY, {})
        if tr_ok and needs_online_notice(tr_cls, is_acknowledged(ack, tr_cls.key)):
            if not messagebox.askyesno(
                    "KaptionPro — Privasi terjemahan",
                    f"Penerjemah '{tr_cls.label}' bekerja ONLINE: teks subtitle hasil transkripsi akan dikirim ke "
                    "layanan pihak ketiga lewat internet. Suara dan video TIDAK dikirim (Whisper berjalan lokal).\n\n"
                    "Hasil terjemahan disimpan di cache lokal:\n"
                    f"{self.app.ai.cache.path}\n"
                    "(hapus file itu kapan saja untuk membersihkan cache).\n\n"
                    "Untuk memakai pilihan 100% offline, ganti ke 'Argos (offline)' di panel samping.\n\n"
                    f"Lanjutkan dan jangan tanyakan lagi untuk '{tr_cls.label}'?\n"
                    "(Bisa dicabut di 📖 Panduan → Privasi terjemahan.)"):
                return
            self.app.settings.set(ACK_KEY, with_acknowledged(ack, tr_cls.key))
        if self.segments and not messagebox.askyesno(
                "KaptionPro", f"Ganti {len(self.segments)} baris yang ada dengan hasil AI?\n(Bisa di-Undo.)"):
            return
        self.flush()          # pastikan kondisi terbaru ada di disk SEBELUM snapshot diambil
        if not self._snapshot() and not messagebox.askyesno(
                "KaptionPro", "Snapshot pemulihan GAGAL dibuat (disk penuh / tidak ada izin tulis?).\n\n"
                              "Lanjutkan tanpa snapshot? Hasil AI akan menggantikan subtitle saat ini "
                              "(backup berversi tetap dicoba)."):
            return
        self._backup("before-ai")
        preset = LANGUAGE_PRESETS[self.project.preset_key]
        model = self.project.whisper_model
        backend_key = self.project.translator
        token = self._begin_job("ai", f"AI · {model}")
        cancel, video, app = self.cancel_event, self.project.video_path, self.app

        def progress(frac: float | None, text: str) -> None:
            app.post(self._on_job_progress, token, frac, text)

        def worker() -> None:
            try:
                app.post(self._on_job_phase, token, f"Transkripsi · {model}")
                segs = app.ai.transcribe(video, preset.whisper_code, model, progress, cancel)
                code, note, skip = self._decide_source(preset, app.ai.last_language, app.ai.last_language_prob)
                failures, translated, stats = 0, False, {}
                if tr_ok and not skip:
                    app.post(self._on_job_phase, token, f"Terjemahan · {tr_cls.label}")
                    segs = app.ai.translate_to_indonesian(segs, preset.source_name, progress, cancel,
                                                          backend_key=backend_key, source_code=code)
                    failures, stats, translated = (app.ai.last_translate_failures,
                                                   dict(app.ai.last_translate_stats), True)
                app.post(self._on_generated, token, segs, translated, failures, note, stats, skip)
            except CancelledError:
                app.post(self._on_job_cancelled, token)
            except Exception as exc:  # noqa: BLE001
                log.exception("Generate gagal")
                app.post(self._on_job_failed, token, exc)

        self._start_worker(threading.Thread(target=worker, daemon=True, name="kp-ai"))

    def _on_generated(self, token: int, segments: list[SubtitleSegment], translated: bool,
                      failures: int = 0, note: str = "", stats: dict | None = None,
                      already_id: bool = False) -> None:
        if not self._is_current(token):
            return
        self._end_job()
        self.editor.replace_all(segments, push_undo=True)
        stats = stats or {}
        if already_id:
            suffix = "(sudah bahasa Indonesia)"
        elif not translated:
            suffix = "(terjemahan dilewati: penerjemah belum terpasang)"
        elif failures:
            suffix = f"— ⚠ {failures} baris belum terjemah (masih bahasa asli)"
        else:
            suffix = "dengan terjemahan Indonesia"
        cache = f" · {stats['cached']} dari cache" if stats.get("cached") else ""
        self.set_status(f"✓ {len(segments)} lines generated {suffix}{cache}" + (f" · {note}" if note else ""))
        if (not translated and not already_id) or failures:
            reason = ("Penerjemah belum terpasang atau tidak dipilih." if not translated else
                      f"{failures} baris gagal diterjemahkan" +
                      (" — proses dihentikan karena gagal beruntun (koneksi/kuota penerjemah)."
                       if stats.get("aborted") else " (periksa koneksi / coba lagi nanti)."))
            messagebox.showwarning(
                "KaptionPro",
                "Subtitle berhasil dibuat, tapi sebagian/seluruh teks belum berbahasa Indonesia.\n\n" + reason +
                "\nHasil yang sudah diterjemahkan tersimpan di cache — Generate ulang akan melanjutkan "
                "tanpa mengulang. Baris lain tetap bahasa asli dan bisa diedit manual.")

    # ---------- Export ----------
    def _play_res(self) -> tuple[int, int]:
        """Resolusi untuk PlayRes ASS: ffprobe -> ukuran dari VLC -> default 1920x1080."""
        vi = self.video_info
        if vi is not None and vi.dims_known:
            return vi.width, vi.height
        w, h = self.video.player.get_video_size()
        if w > 0 and h > 0:
            return w, h
        return (vi.width, vi.height) if vi else (1920, 1080)

    def _confirm_issues(self) -> bool:
        """Hanya memblokir empty/bad/overlap. short/long tidak memaksa Auto-Fix."""
        issues = SubtitleIO.find_issues(SubtitleIO.sorted_copy(self.segments))
        blocking = [i for i, k in issues.items() if k in BLOCKING_ISSUES]
        if not blocking:
            if issues:
                # Ada peringatan tapi bukan penghalang. Info saja, tidak memaksa.
                self.set_status(f"{len(issues)} peringatan (short/long) — tidak memblokir export.")
            return True
        ans = messagebox.askyesnocancel("KaptionPro",
                                        f"{len(blocking)} baris bermasalah (kosong/waktu salah/overlap).\n\n"
                                        "Yes = Auto-Fix lalu export\nNo = tetap export\nCancel = batal")
        if ans is None:
            return False
        if ans:
            self._backup("before-autofix")
            fixed, _ = SubtitleIO.auto_fix(self.segments)
            self.editor.replace_all(fixed)
        return True

    def _export_srt(self) -> None:
        if not self.segments:
            messagebox.showwarning("KaptionPro", "Belum ada subtitle untuk di-export.")
            return
        if not self._confirm_issues():
            return
        default = safe_filename(self.project.name if self.project else "subtitle") + ".srt"
        path = filedialog.asksaveasfilename(defaultextension=".srt", initialfile=default,
                                            filetypes=[("SubRip Subtitle", "*.srt")])
        if not path:
            return
        try:
            SubtitleIO.write_srt(self.segments, path)
            self.set_status(f"✓ SRT saved: {path}")
        except OSError as e:
            messagebox.showerror("KaptionPro", f"Gagal menyimpan:\n{e}")

    def _export_ass(self) -> None:
        if not self.segments:
            messagebox.showwarning("KaptionPro", "Belum ada subtitle untuk di-export.")
            return
        if not self._confirm_issues():
            return
        style_key = self.project.style_key if self.project else "karaoke"
        style = self.project.subtitle_style() if self.project else SubtitleStyle()
        default = safe_filename(self.project.name if self.project else "subtitle") + ".ass"
        path = filedialog.asksaveasfilename(defaultextension=".ass", initialfile=default,
                                            filetypes=[("ASS Subtitle", "*.ass")])
        if not path:
            return
        w, h = self._play_res()
        try:
            SubtitleIO.write_ass(self.segments, path, style_key, style, w, h)
            self.set_status(f"✓ ASS saved ({TEXT_STYLES[style_key]}): {path}")
        except OSError as e:
            messagebox.showerror("KaptionPro", f"Gagal menyimpan:\n{e}")

    def _ask_export_options(self) -> ExportOptions | None:
        info = ""
        if self.video_info:
            vi = self.video_info
            info = f"Sumber: {vi.width}×{vi.height} · {fmt_clock(vi.duration)}"
        return ExportOptionsDialog(self, self.project.export_options(), info).ask()

    def _export_video(self) -> None:
        p = self.project
        if self._job_kind or not p or self._workers_still_stopping():
            return
        if not p.video_path or not p.video_exists:
            messagebox.showwarning("KaptionPro", "Video project tidak ditemukan.")
            return
        if not self.segments:
            messagebox.showwarning("KaptionPro", "Belum ada subtitle untuk di-burn ke video.")
            return
        if not self.app.ffmpeg.ffmpeg_path():
            messagebox.showerror("KaptionPro", "FFmpeg tidak ditemukan di PATH.\n"
                                               "Install FFmpeg lalu restart KaptionPro.")
            return
        if not self._confirm_issues():
            return
        opts = self._ask_export_options()
        if opts is None:
            return
        out = filedialog.asksaveasfilename(defaultextension=".mp4", initialfile=f"{safe_filename(p.name, 'video')}_subbed.mp4",
                                           initialdir=str(Path(p.video_path).parent),
                                           filetypes=[("MP4 Video", "*.mp4")])
        if not out:
            return
        if Path(out).resolve() == Path(p.video_path).resolve():
            messagebox.showerror("KaptionPro", "File output tidak boleh sama dengan video sumber.")
            return
        p.export = opts.as_dict()                  # ingat pilihan terakhir untuk project ini
        self.app.store.add(p)

        w, h = self._play_res()
        ass = SubtitleIO.build_ass(self.segments, p.style_key, p.subtitle_style(), w, h)
        duration = self.video_info.duration if self.video_info else 0.0
        token = self._begin_job("export", f"Export MP4 · {EXPORT_QUALITIES[opts.quality]}")
        cancel, video, app = self.cancel_event, p.video_path, self.app

        def progress(frac: float | None, text: str) -> None:
            app.post(self._on_job_progress, token, frac, text)

        def worker() -> None:
            try:
                result = app.ffmpeg.export_video(video, ass, out, duration, progress, cancel, opts)
                app.post(self._on_export_done, token, result)
            except CancelledError:
                app.post(self._on_job_cancelled, token)
            except Exception as exc:  # noqa: BLE001
                log.exception("Export gagal")
                app.post(self._on_job_failed, token, exc)

        self._start_worker(threading.Thread(target=worker, daemon=True, name="kp-export"))

    def _on_export_done(self, token: int, result) -> None:
        if not self._is_current(token):
            return
        self._end_job()
        path = getattr(result, "path", result)
        summary = result.summary() if hasattr(result, "summary") else ""
        self.set_status(f"✓ Video exported: {path}" + (f" ({summary})" if summary else ""))
        notes = getattr(result, "notes", [])
        messagebox.showinfo("KaptionPro", f"Export selesai:\n{path}" + (f"\n{summary}" if summary else "") +
                            ("\n\n" + "\n".join(notes) if notes else ""))
