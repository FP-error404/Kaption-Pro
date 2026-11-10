"""Layar selamat datang dan kartu project terbaru."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import LANGUAGE_PRESETS, Theme, ctk
from ..models import Project
from .components import MonogramLogo, gold_button

if TYPE_CHECKING:
    from ..main import KaptionProApp


class RecentProjectCard(ctk.CTkFrame):
    def __init__(self, master, project: Project, on_open, on_remove) -> None:
        super().__init__(master, fg_color=Theme.SURFACE_2, corner_radius=Theme.RADIUS,
                         border_width=1, border_color=Theme.BORDER)
        self.project, self.on_open = project, on_open
        self.grid_columnconfigure(0, weight=1)

        preset = LANGUAGE_PRESETS.get(project.preset_key, LANGUAGE_PRESETS["auto"])
        try:
            when = datetime.fromisoformat(project.updated_at).strftime("%d %b %Y, %H:%M")
        except ValueError:
            when = "-"

        ctk.CTkLabel(self, text="🎬", font=Theme.font(26)).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 0))
        self.remove_btn = ctk.CTkButton(
            self, text="✕", width=26, height=26, corner_radius=Theme.RADIUS, fg_color="transparent",
            hover_color=Theme.DANGER, text_color=Theme.TEXT_MUTED, font=Theme.font(11, "bold"),
            command=lambda: on_remove(project))
        self.remove_btn.grid(row=0, column=1, padx=(0, 8), pady=(8, 0), sticky="ne")

        rows = [
            (project.name, Theme.GOLD_BRIGHT, Theme.font(13, "bold")),
            (preset.label.split("   ")[-1], Theme.TEXT, Theme.font(11)),
            (f"{when} · {project.line_count} lines", Theme.TEXT_MUTED, Theme.font(10)),
        ]
        if project.video_path:
            ok = project.video_exists
            rows.append((("✓ " if ok else "⚠ file hilang: ") + Path(project.video_path).name,
                         Theme.TEXT_MUTED if ok else Theme.DANGER_TEXT, Theme.font(10)))
        for i, (text, color, font) in enumerate(rows, start=1):
            ctk.CTkLabel(self, text=text, text_color=color, anchor="w", font=font, wraplength=210,
                         justify="left").grid(row=i, column=0, columnspan=2, sticky="ew", padx=14,
                                              pady=(0, 10 if i == len(rows) else 1))

        self._bind_click(self)
        self.bind("<Enter>", lambda _e: self.configure(border_color=Theme.GOLD))
        self.bind("<Leave>", lambda _e: self.configure(border_color=Theme.BORDER))

    def _bind_click(self, widget) -> None:
        if widget is self.remove_btn:
            return
        widget.bind("<Button-1>", lambda _e: self.on_open(self.project), add="+")
        for child in widget.winfo_children():
            self._bind_click(child)


class WelcomeScreen(ctk.CTkFrame):
    CARD_MIN_WIDTH = 250

    def __init__(self, master, app: "KaptionProApp") -> None:
        super().__init__(master, fg_color=Theme.BG, corner_radius=0)
        self.app = app
        self._after_id = None
        self._columns = 0
        self._cards: list[RecentProjectCard] = []

        self.scroll = ctk.CTkScrollableFrame(self, fg_color=Theme.BG, scrollbar_button_color=Theme.GOLD_DIM,
                                             scrollbar_button_hover_color=Theme.GOLD)
        self.scroll.pack(fill="both", expand=True)

        MonogramLogo(self.scroll, 130).pack(pady=(24, 8))
        ctk.CTkLabel(self.scroll, text="K A P T I O N   P R O", text_color=Theme.GOLD_BRIGHT,
                     font=Theme.font(28, "bold", serif=True)).pack()
        self.subtitle = ctk.CTkLabel(self.scroll, text="Specializing in K-Drama & C-Drama Subtitles with AI",
                                     text_color=Theme.TEXT_MUTED, font=Theme.font(14), justify="center")
        self.subtitle.pack(pady=(6, 16), padx=20)

        row = ctk.CTkFrame(self.scroll, fg_color="transparent")
        row.pack()
        self.btn_start = gold_button(row, "START NEW PROJECT", app.start_new_project, height=50, width=280)
        self.btn_start.pack(side="left", padx=4)
        self.btn_guide = gold_button(row, "📖 Panduan & Cek", app.show_guide, filled=False, height=50, width=180)
        self.btn_guide.pack(side="left", padx=4)

        ctk.CTkFrame(self.scroll, height=1, fg_color=Theme.GOLD_DIM).pack(fill="x", padx=40, pady=(28, 10))
        ctk.CTkLabel(self.scroll, text="RECENT PROJECTS", text_color=Theme.GOLD, anchor="w",
                     font=Theme.font(13, "bold")).pack(fill="x", padx=40, pady=(0, 8))
        self.cards_frame = ctk.CTkFrame(self.scroll, fg_color="transparent")
        self.cards_frame.pack(fill="x", padx=34, pady=(0, 30))

        self.bind("<Configure>", self._on_configure)
        self.refresh_recent()

    def refresh_recent(self) -> None:
        for child in self.cards_frame.winfo_children():
            child.destroy()
        self._columns = 0
        projects = self.app.store.projects
        if not projects:
            empty = ctk.CTkFrame(self.cards_frame, fg_color=Theme.SURFACE, corner_radius=Theme.RADIUS,
                                 border_width=1, border_color=Theme.BORDER)
            empty.pack(fill="x", padx=6, pady=6)
            ctk.CTkLabel(empty, text="No recent projects yet.\nPress START NEW PROJECT to begin.",
                         text_color=Theme.TEXT_MUTED, font=Theme.font(13)).pack(pady=26)
            self._cards = []
            return
        self._cards = [RecentProjectCard(self.cards_frame, p, self.app.open_project, self.app.remove_project)
                       for p in projects]
        self._reflow()

    def _on_configure(self, event) -> None:
        if event.widget is not self:
            return
        if self._after_id:
            self.after_cancel(self._after_id)
        self._after_id = self.after(60, self._reflow)

    def _reflow(self) -> None:
        width = self.winfo_width()
        if width <= 1:
            return
        self.subtitle.configure(wraplength=max(220, width - 80))
        narrow = width < 620          # tombol ditumpuk vertikal di jendela sempit (tidak meluap)
        for btn in (self.btn_start, self.btn_guide):
            btn.pack_configure(side="top" if narrow else "left", padx=4, pady=(0, 6) if narrow else 0)
        if not self._cards:
            return
        cols = max(1, min(4, (width - 80) // self.CARD_MIN_WIDTH))
        if cols == self._columns:
            return
        self._columns = cols
        for c in range(4):
            self.cards_frame.grid_columnconfigure(c, weight=1 if c < cols else 0, uniform="cards")
        for i, card in enumerate(self._cards):
            card.grid(row=i // cols, column=i % cols, padx=6, pady=6, sticky="nsew")
