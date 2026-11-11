"""Komponen UI kecil dan pembuat tombol bertema."""

from __future__ import annotations

import tkinter as tk

from ..config import Theme, ctk


class MonogramLogo(tk.Canvas):
    def __init__(self, master, size: int = 150, bg: str = Theme.BG) -> None:
        super().__init__(master, width=size, height=size, bg=bg, highlightthickness=0)
        c = size / 2
        self.create_oval(6, 6, size - 6, size - 6, outline=Theme.GOLD, width=3)
        self.create_oval(16, 16, size - 16, size - 16, outline=Theme.GOLD_DIM, width=1)
        d = 6
        for cy in (6, size - 6):
            self.create_polygon(c, cy - d, c + d, cy, c, cy + d, c - d, cy,
                                fill=Theme.GOLD_BRIGHT, outline=Theme.GOLD)
        self.create_text(c, c, text="KP", fill=Theme.GOLD_BRIGHT,
                         font=("Georgia", int(size * 0.34), "bold"))


def apply_radius_defaults() -> None:
    """Samakan radius widget CustomTkinter yang TIDAK diberi corner_radius eksplisit (entry, textbox, menu, ...).
    Panggil sekali sebelum widget dibuat."""
    theme = ctk.ThemeManager.theme
    for name in ("CTkButton", "CTkEntry", "CTkTextbox", "CTkOptionMenu", "CTkComboBox", "CTkFrame"):
        entry = theme.get(name)
        if isinstance(entry, dict) and "corner_radius" in entry:
            entry["corner_radius"] = Theme.RADIUS


def gold_button(master, text: str, command, *, filled: bool = True, height: int = 42, **kw) -> ctk.CTkButton:
    if filled:
        return ctk.CTkButton(master, text=text, command=command, height=height, corner_radius=Theme.RADIUS,
                             fg_color=Theme.GOLD, hover_color=Theme.GOLD_HOVER, text_color="#121212",
                             font=Theme.font(14, "bold"), **kw)
    return ctk.CTkButton(master, text=text, command=command, height=height, corner_radius=Theme.RADIUS,
                         fg_color="transparent", hover_color=Theme.SURFACE_2, text_color=Theme.GOLD,
                         border_width=1, border_color=Theme.GOLD_DIM, font=Theme.font(13, "bold"), **kw)


def small_button(master, text: str, command, width: int = 64, danger: bool = False) -> ctk.CTkButton:
    return ctk.CTkButton(master, text=text, command=command, width=width, height=26, corner_radius=Theme.RADIUS,
                         fg_color=Theme.SURFACE_2, hover_color=Theme.DANGER if danger else Theme.GOLD_DIM,
                         text_color=Theme.GOLD, font=Theme.font(11, "bold"))


def section_header(master, text: str, label_h: int | None = None, pad: tuple[int, int] = (10, 2),
                   rule_pad: tuple[int, int] = (0, 6)) -> None:
    """Judul seksi + garis emas. Parameter opsional untuk versi kompak (panel preset); default = tampilan lama."""
    height = {} if label_h is None else {"height": label_h}
    ctk.CTkLabel(master, text=text, text_color=Theme.GOLD, anchor="w", font=Theme.font(12, "bold"),
                 **height).pack(fill="x", padx=14, pady=pad)
    ctk.CTkFrame(master, height=1, fg_color=Theme.GOLD_DIM).pack(fill="x", padx=14, pady=rule_pad)
