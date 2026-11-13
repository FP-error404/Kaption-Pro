"""
Perhitungan tata letak MURNI (tanpa Tk/CustomTkinter): bisa diuji di CI core dan dipakai ulang oleh UI.

Satuan: piksel LOGIS CustomTkinter (sebelum dikali faktor skala DPI), kecuali dinyatakan lain.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------------------------------------------
# Tinggi panel preset (kolom kanan, mode landscape)
# ---------------------------------------------------------------------------------------------------------------
# Satuan di sini = tinggi panel preset yang DIMINTA (px logis). v1.9.15.2 membungkus panel dalam wadah tetap
# (WorkspaceScreen.side_box, grid_propagate(False)) dengan dugaan penyebabnya CTkScrollableFrame (scrollbar-nya meminta
# 200 px). Dugaan itu belum cukup: log CI v1.9.15.2 di 1024x680 mencatat `diminta=120 nyata=210` untuk side_box SENDIRI
# (bukan isi scroll-nya), jadi baris grid-nya tetap >= ~210 px apa pun yang diminta (penyebab pastinya belum terbukti;
# test_11 kini mencetak tinggi tiap baris grid agar log berikutnya menunjukkannya). Sampai itu jelas, v1.9.15.3 TIDAK
# bergantung pada tinggi di bawah ~210 px: pada jendela pendek (< SIDE_AUTOHIDE_H) panel preset disembunyikan.
# Bukti lama dari CI v1.9.15: dengan panel = 210, elemen lain di Linux (768 - 210 - 394) dan Windows (749 - 210 - 375)
# sama-sama 164 px.
SIDE_H_MAX = 250             # tinggi panel preset pada jendela lega
SIDE_H_MIN = 120             # perkiraan awal tidak pernah di bawah ini
SIDE_H_FLOOR = SIDE_H_MIN    # penyetelan akhir juga tidak pernah memaksa panel lebih kecil dari ini (isinya di-scroll)

# Tinggi terkecil yang NYATA diberikan grid kepada panel preset di Linux/xvfb (log CI v1.9.15.2: diminta 120, nyata 210).
# Di Windows belum teramati; angka ini dipakai konservatif untuk menurunkan SIDE_AUTOHIDE_H.
OBSERVED_SIDE_MIN_H = 210
# Tinggi editor tanpa tabel yang terukur di CI dengan toolbar 1 baris (768 -> editor 394, tabel 126).
EDITOR_CHROME_MEASURED_H = 268
# Jendela lebih pendek dari ini (px logis): panel preset disembunyikan otomatis di mode landscape, editor memakai seluruh
# kolom kanan. Turunan: 164 (elemen lain) + 210 (panel nyata) + 268 (editor tanpa tabel) + 90 (tabel) = 732; dibulatkan
# ke 740 sebagai margin. Jendela CI Windows 749 px dan Linux 768 px tetap menampilkan panel (tabel 126 px di sana).
# Tombol "Panel" di top bar tetap bisa menampilkannya kapan saja (pilihan pengguna menang atas otomatis).
SIDE_AUTOHIDE_H = 740

# Kolom kanan (landscape) ditumpuk dari atas ke bawah: top bar, panel preset, editor, tombol export, job bar.
# Komponen di bawah = elemen LAIN (di luar panel preset dan editor).
TOP_BAR_H = 48               # bar atas: tombol Home, nama project, status simpan, tombol-tombol kecil
GRID_PADDING_H = 46          # jumlah pady antar-baris grid kolom kanan: 10 + 6 + 6 + 6 + 6 + 2 + 2 + 8 (pasti, dari kode)
EXPORT_BAR_H = 42            # tombol Export Video / SRT / ASS
JOB_BAR_H = 33               # job bar (label status, progress, Cancel)
OUTSIDE_H = TOP_BAR_H + GRID_PADDING_H + EXPORT_BAR_H + JOB_BAR_H                 # 169 (perkiraan komponen)
CI_OUTSIDE_H = 164           # hasil ukur CI v1.9.15 (Linux dan Windows sama): perkiraan di atas hanya 5 px lebih besar
RIGHT_COLUMN_FIXED_H = OUTSIDE_H                                                  # konservatif: nilai yang lebih besar

# Editor subtitle. Tinggi editor TANPA widget tabel = 298 px (CI: editor 394 - tabel 96 di Linux, 375 - 77 di Windows):
# header, kolom cari, 2 baris toolbar, form, dan padding. Sejak v1.9.15.2 toolbar diringkas menjadi 1 baris (hemat ~30 px);
# angka 298 SENGAJA tidak dikurangi untuk perhitungan jaminan (konservatif), nilai nyata dicetak oleh test_11.
# Tabel (header + baris, Treeview rowheight=26) minimal 90 px = header + >= 2-3 baris data yang terlihat.
EDITOR_CHROME_H = 298
EDITOR_TOOLBAR_SAVED_H = 30  # satu baris toolbar (tombol 26 + pady 2 x 2) yang dihemat v1.9.15.2
ROW_H = 26
TABLE_MIN_PX = 90
EDITOR_MIN_H = EDITOR_CHROME_H + TABLE_MIN_PX                                      # 388

# Tinggi jendela dikurangi CHROME_H = tinggi yang boleh dipakai panel preset (lalu dijepit SIDE_H_MIN..SIDE_H_MAX).
# 557 = 169 (elemen lain) + 388 (editor dengan tabel 90 px).
# Riwayat: v1.9.14 memakai 592 dan v1.9.15 memakai 603 (keduanya salah karena panel sebenarnya 210 px, bukan 173),
# v1.9.15.1 memakai 626 (menambal selisih tanpa tahu penyebabnya).
CHROME_H = RIGHT_COLUMN_FIXED_H + EDITOR_MIN_H

# ---------------------------------------------------------------------------------------------------------------
# Jendela awal
# ---------------------------------------------------------------------------------------------------------------
STARTUP_SIZE = (1280, 800)   # ukuran awal yang diinginkan
MIN_WINDOW = (420, 640)      # batas bawah normal (KaptionProApp.minsize) pada layar yang cukup besar
MIN_USABLE = (320, 240)      # lantai mutlak: di bawah ini jendela tak bisa dipakai sama sekali
SCREEN_MARGIN = (40, 110)    # sisakan ruang untuk taskbar + title bar agar jendela tidak melebihi layar


# ---------------------------------------------------------------------------------------------------------------
# Isi panel preset (v1.9.16): menu Whisper model harus tampak UTUH di panel terpendek yang masih menampilkannya
# ---------------------------------------------------------------------------------------------------------------
# Satuan px logis; widget memakai konstanta ini langsung (ui/workspace.py: SidePanel), jadi angka di sini = angka di layar.
# Sebelum v1.9.16: header 28+12+1+6 = 47, tiga tombol preset 3 x (34+6) = 120, label Whisper 28+6, menu 28+4 -> menu Whisper
# berakhir di ~233 px, sedangkan panel hanya 183-250 px sehingga menu terpotong di bawah.
SIDE_HEADER_LABEL_H = 18
SIDE_HEADER_PAD = (4, 1)         # pady label judul seksi (atas, bawah)
SIDE_RULE_H = 1                  # garis emas di bawah judul
SIDE_RULE_PAD = (0, 3)
SIDE_PRESET_BTN_H = 28
SIDE_PRESET_PAD = (1, 2)         # pady tiap tombol preset
SIDE_FIELD_LABEL_H = 18          # label "Whisper model" / "Penerjemah"
SIDE_FIELD_LABEL_PAD = (3, 0)
SIDE_MENU_H = 26                 # option menu Whisper / Penerjemah
SIDE_MENU_PAD = (0, 2)
SIDE_FRAME_ALLOWANCE = 12        # border 1 + padding dalam CTkScrollableFrame (radius 4) atas-bawah + kelonggaran


def side_whisper_bottom(preset_count: int) -> int:
    """Tinggi isi panel preset dari atas sampai tepi bawah menu Whisper model (px logis), untuk `preset_count` preset."""
    header = SIDE_HEADER_LABEL_H + sum(SIDE_HEADER_PAD) + SIDE_RULE_H + sum(SIDE_RULE_PAD)
    presets = preset_count * (SIDE_PRESET_BTN_H + sum(SIDE_PRESET_PAD))
    whisper = SIDE_FIELD_LABEL_H + sum(SIDE_FIELD_LABEL_PAD) + SIDE_MENU_H + sum(SIDE_MENU_PAD)
    return header + presets + whisper


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
    Karena panel kini berukuran tetap (side_box), pengurangan ini benar-benar sampai ke tabel.
    Tidak pernah memperbesar panel (menghindari osilasi). `current` dan hasilnya dalam piksel logis.
    """
    deficit = target - table_px
    if table_px <= 1 or deficit <= 0:
        return current
    shrink = int(-(-deficit // _scale(scaling)))              # pembulatan ke atas ke piksel logis
    return max(SIDE_H_FLOOR, current - shrink)


def side_panel_autohide(window_h: float, scaling: float = 1.0) -> bool:
    """
    True bila jendela (landscape) terlalu pendek untuk menampilkan panel preset DAN tabel subtitle >= TABLE_MIN_PX.
    `window_h` dalam piksel FISIK (winfo_height); belum terpasang (<=1) -> False (jangan menyembunyikan apa pun dulu).
    """
    if window_h <= 1:
        return False
    return window_h / _scale(scaling) < SIDE_AUTOHIDE_H


def side_panel_visible(mode: str | None, window_h: float, scaling: float = 1.0, user_choice: bool | None = None) -> bool:
    """
    Apakah panel preset tampil. Pilihan pengguna (tombol Panel) selalu menang; selain itu otomatis: tersembunyi hanya di
    mode landscape pada jendela pendek (portrait memakai panel 150 px yang bisa di-scroll, tidak ikut disembunyikan).
    """
    if user_choice is not None:
        return user_choice
    if mode != "landscape":
        return True
    return not side_panel_autohide(window_h, scaling)


def guaranteed_table_px(window_h: float, scaling: float = 1.0) -> int:
    """
    Tinggi tabel subtitle yang DIJAMIN pada jendela setinggi `window_h` (fisik), dengan pengaturan otomatis (tanpa
    pilihan pengguna): TABLE_MIN_PX bila ruang cukup, selain itu sisa terbesar yang masih mungkin. Jendela pendek
    (side_panel_autohide) tanpa panel preset; jendela tinggi dengan panel di SIDE_H_FLOOR. Dihitung konservatif
    (RIGHT_COLUMN_FIXED_H memakai nilai CI terbesar), jadi hasil nyata biasanya sama atau lebih besar.
    """
    logical = window_h / _scale(scaling)
    panel = 0 if side_panel_autohide(window_h, scaling) else SIDE_H_FLOOR
    room = logical - RIGHT_COLUMN_FIXED_H - panel - EDITOR_CHROME_H
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
