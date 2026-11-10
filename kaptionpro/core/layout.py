"""
Perhitungan tata letak MURNI (tanpa Tk/CustomTkinter): bisa diuji di CI core dan dipakai ulang oleh UI.

Satuan: piksel LOGIS CustomTkinter (sebelum dikali faktor skala DPI), kecuali dinyatakan lain.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------------------------------------------
# Tinggi panel preset (kolom kanan, mode landscape)
# ---------------------------------------------------------------------------------------------------------------
SIDE_H_MAX = 250             # tinggi panel preset pada jendela lega
SIDE_H_MIN = 120             # tinggi NYAMAN panel preset (perkiraan awal tidak pernah di bawah ini)
SIDE_H_FLOOR = 80            # batas mutlak: bila tabel subtitle masih kekurangan tempat, panel boleh menyusut sampai sini
                             # (panel bisa di-scroll, sedangkan tabel subtitle adalah area kerja utama)

# Kolom kanan (landscape) ditumpuk dari atas ke bawah: top bar, panel preset, editor, tombol export, job bar.
# Tinggi jendela harus cukup untuk SEMUANYA; sisa setelah semua elemen lain dihitung diberikan ke panel preset.
#
# Angka di bawah adalah PERKIRAAN AWAL. Penyetelan akhir dilakukan di aplikasi (WorkspaceScreen._rebalance_side) dengan
# mengukur tinggi tabel subtitle yang sebenarnya, sehingga ketidaktepatan perkiraan tidak lagi membuat tabel terjepit.
TOP_BAR_H = 48               # bar atas: tombol Home, nama project, status simpan, tombol-tombol kecil
GRID_PADDING_H = 46          # jumlah pady antar-baris grid kolom kanan: 10 + 6 + 6 + 6 + 6 + 2 + 2 + 8 (pasti, dari kode)
EXPORT_BAR_H = 42            # tombol Export Video / SRT / ASS
JOB_BAR_H = 33               # job bar (label status, progress, Cancel)
# Perkiraan di atas diukur dari screenshot CI v1.9.13 (1366x768). CTkScrollableFrame menambah padding vertikal sebesar
# corner_radius di atas dan di bawah kanvasnya, jadi tinggi nyata panel preset = tinggi yang diminta + 2 x Theme.RADIUS.
SCROLLABLE_EXTRA_H = 8
# Pengukuran CI v1.9.15 (log test_11, panel preset diminta 165 dan 146 px): total elemen kolom kanan di luar editor dan
# di luar tinggi diminta panel ternyata 209 px (Linux, jendela 768) dan 228 px (Windows, jendela 749), sedangkan
# komponen di atas hanya menjelaskan 177 px (169 + 8). Selisih terbesar (51) dicatat di sini; penguraiannya
# menunggu log test_11 berikutnya (mencetak `elemen lain` dan `extra` dengan widget yang benar).
UNEXPLAINED_SLACK_H = 51

OUTSIDE_H = TOP_BAR_H + GRID_PADDING_H + EXPORT_BAR_H + JOB_BAR_H                 # 169 (perkiraan komponen)
RIGHT_COLUMN_FIXED_H = OUTSIDE_H + SCROLLABLE_EXTRA_H + UNEXPLAINED_SLACK_H       # 228 (konservatif: nilai terbesar CI)

# Editor subtitle. Tinggi editor TANPA widget tabel = 298 px (CI: editor 394 - tabel 96 di Linux, 375 - 77 di Windows):
# header, kolom cari, 2 baris toolbar, form, dan padding. Tabel (header + baris, Treeview rowheight=26) minimal 100 px
# = header + >= 3 baris data yang terlihat.
EDITOR_CHROME_H = 298
ROW_H = 26
TABLE_MIN_PX = 100
EDITOR_MIN_H = EDITOR_CHROME_H + TABLE_MIN_PX                                      # 398

# Tinggi jendela dikurangi CHROME_H = tinggi yang boleh dipakai panel preset (lalu dijepit SIDE_H_MIN..SIDE_H_MAX).
# 626 = 228 (kolom kanan di luar editor, konservatif) + 398 (editor dengan tabel 100 px).
# Riwayat: v1.9.14 memakai 592 dan v1.9.15 memakai 603; keduanya meremehkan tinggi elemen lain (lihat log CI).
CHROME_H = RIGHT_COLUMN_FIXED_H + EDITOR_MIN_H

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


def rebalanced_side_height(current: int, table_px: float, scaling: float = 1.0, target: int = TABLE_MIN_PX) -> int:
    """
    Penyetelan akhir berdasarkan UKURAN NYATA: tabel subtitle `table_px` (piksel fisik, winfo_height) kurang dari
    `target` -> panel preset dikecilkan sebesar kekurangannya (tabel mendapat tempat itu), minimal SIDE_H_FLOOR.
    Tidak pernah memperbesar panel (menghindari osilasi). `current` dan hasilnya dalam piksel logis.
    """
    deficit = target - table_px
    if table_px <= 1 or deficit <= 0:
        return current
    shrink = int(-(-deficit // _scale(scaling)))              # pembulatan ke atas ke piksel logis
    return max(SIDE_H_FLOOR, current - shrink)


def guaranteed_table_px(window_h: float, scaling: float = 1.0) -> int:
    """
    Tinggi tabel subtitle yang DIJAMIN pada jendela setinggi `window_h` (fisik): TABLE_MIN_PX bila jendela cukup tinggi,
    selain itu sisa terbesar yang masih mungkin dengan panel preset di SIDE_H_FLOOR. Dihitung konservatif
    (RIGHT_COLUMN_FIXED_H memakai nilai CI terbesar), jadi hasil nyata biasanya sama atau lebih besar.
    """
    logical = window_h / _scale(scaling)
    room = logical - RIGHT_COLUMN_FIXED_H - SIDE_H_FLOOR - EDITOR_CHROME_H
    return int(min(TABLE_MIN_PX, max(0, room)))


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
