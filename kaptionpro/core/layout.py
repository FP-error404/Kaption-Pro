"""
Perhitungan tata letak MURNI (tanpa Tk/CustomTkinter): bisa diuji di CI core dan dipakai ulang oleh UI.

Satuan: piksel LOGIS CustomTkinter (sebelum dikali faktor skala DPI), kecuali dinyatakan lain.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------------------------------------------
# Tinggi panel preset (kolom kanan, mode landscape)
# ---------------------------------------------------------------------------------------------------------------
SIDE_H_MAX = 250             # tinggi panel preset pada jendela lega
SIDE_H_MIN = 120             # panel preset tetap bisa di-scroll; jangan lebih kecil dari ini

# Kolom kanan (landscape) ditumpuk dari atas ke bawah: top bar, panel preset, editor, tombol export, job bar.
# Tinggi jendela harus cukup untuk SEMUANYA; sisa setelah semua elemen lain dihitung diberikan ke panel preset.
# Angka di bawah DIUKUR dari screenshot CI v1.9.13 (jendela 1366x768, panel preset 250 px) dan dicocokkan dengan
# nilai padding di ui/workspace.py (_set_mode). Tes GUI test_11 mengukur ulang di runner nyata dan mencetak hasilnya,
# jadi bila UI berubah dan angka ini menyimpang, kelihatan di log CI.
TOP_BAR_H = 48               # bar atas: tombol Home, nama project, status simpan, tombol-tombol kecil
GRID_PADDING_H = 46          # jumlah pady antar-baris grid kolom kanan: 10 + 6 + 6 + 6 + 6 + 2 + 2 + 8
EXPORT_BAR_H = 42            # tombol Export Video / SRT / ASS
JOB_BAR_H = 33               # job bar (label status, progress, Cancel)
# CTkScrollableFrame menambah padding vertikal sebesar corner_radius di atas dan di bawah kanvasnya, jadi tinggi
# nyata panel preset = tinggi yang diminta + 2 x Theme.RADIUS (tes menjaga agar angka ini sama dengan 2 x RADIUS).
SCROLLABLE_EXTRA_H = 8

# Editor subtitle: tinggi tanpa baris tabel (header, kolom cari, 2 baris toolbar, header tabel, form) diukur 322 px
# (saat tabel menyusut jadi hanya header); ditambah ruang untuk TABLE_ROWS_MIN baris @ ROW_H px (Treeview rowheight=26).
EDITOR_BASE_H = 322
ROW_H = 26
TABLE_ROWS_MIN = 4
EDITOR_MIN_H = EDITOR_BASE_H + TABLE_ROWS_MIN * ROW_H            # 426

# Tinggi elemen LAIN (di luar panel preset dan editor) pada kolom kanan.
OUTSIDE_H = TOP_BAR_H + GRID_PADDING_H + EXPORT_BAR_H + JOB_BAR_H                 # 169

# Tinggi jendela dikurangi CHROME_H = tinggi yang boleh dipakai panel preset (lalu dijepit SIDE_H_MIN..SIDE_H_MAX).
# 603 = 169 (elemen lain) + 8 (padding CTkScrollableFrame) + 426 (editor dengan 4 baris tabel).
# Catatan: v1.9.14 memakai 592 (perkiraan awal, selisih 11 px); v1.9.15 menguraikannya dari komponen di atas.
CHROME_H = OUTSIDE_H + SCROLLABLE_EXTRA_H + EDITOR_MIN_H

# ---------------------------------------------------------------------------------------------------------------
# Jendela awal
# ---------------------------------------------------------------------------------------------------------------
STARTUP_SIZE = (1280, 800)   # ukuran awal yang diinginkan
MIN_WINDOW = (420, 640)      # batas bawah normal (KaptionProApp.minsize) pada layar yang cukup besar
MIN_USABLE = (320, 240)      # lantai mutlak: di bawah ini jendela tak bisa dipakai sama sekali
SCREEN_MARGIN = (40, 110)    # sisakan ruang untuk taskbar + title bar agar jendela tidak melebihi layar


def _scale(scaling: float) -> float:
    return scaling if scaling and scaling > 0 else 1.0


def side_panel_height(window_h: float, scaling: float = 1.0) -> int:
    """
    Tinggi panel preset (landscape). Makin pendek jendela, makin kecil panelnya, supaya tabel subtitle dan form
    editor tetap punya ruang (dulu panel selalu 250 sehingga tabel di layar 1366x768 menyusut jadi hanya header).
    `window_h` dalam piksel FISIK (winfo_height); belum terpasang (<=1) -> ukuran maksimum.
    """
    if window_h <= 1:
        return SIDE_H_MAX
    logical = window_h / _scale(scaling)
    return int(max(SIDE_H_MIN, min(SIDE_H_MAX, logical - CHROME_H)))


def startup_geometry(screen_w: int, screen_h: int, scaling: float = 1.0) -> tuple[int, int, int, int]:
    """
    (lebar, tinggi, x, y) jendela awal. Lebar/tinggi dalam satuan logis (CTk.geometry() mengalikannya dengan skala),
    x/y dalam piksel fisik.

    Ukuran SELALU dibatasi layar supaya tombol Export di bawah tidak tertutup taskbar. Pada layar kecil (mis. 640x480)
    batas bawah aplikasi (MIN_WINDOW) ikut mengecil mengikuti layar, tetapi tidak di bawah MIN_USABLE; pasangannya
    window_minsize() dipakai untuk minsize agar Tk tidak memaksa jendela lebih besar dari layar. Dimensi layar
    tak valid (<= 0, mis. server X yang belum siap) -> ukuran awal biasa di pojok kiri-atas.
    """
    if screen_w <= 0 or screen_h <= 0:
        return STARTUP_SIZE[0], STARTUP_SIZE[1], 0, 0
    s = _scale(scaling)
    max_w = int(screen_w / s) - SCREEN_MARGIN[0]
    max_h = int(screen_h / s) - SCREEN_MARGIN[1]
    floor_w = max(MIN_USABLE[0], min(MIN_WINDOW[0], max_w))
    floor_h = max(MIN_USABLE[1], min(MIN_WINDOW[1], max_h))
    w = max(floor_w, min(STARTUP_SIZE[0], max_w))
    h = max(floor_h, min(STARTUP_SIZE[1], max_h))
    x = max(0, int((screen_w - w * s) / 2))
    y = max(0, int((screen_h - h * s) / 3))
    return w, h, x, y


def window_minsize(width: int, height: int) -> tuple[int, int]:
    """minsize jendela: MIN_WINDOW pada layar normal, tetapi tidak pernah lebih besar dari jendela itu sendiri."""
    return min(MIN_WINDOW[0], width), min(MIN_WINDOW[1], height)
