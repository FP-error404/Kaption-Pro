# Menguji KaptionPro tanpa PC yang kuat

Semua pengujian berjalan otomatis di GitHub Actions saat kamu `push` / membuka pull request
(atau klik **Actions -> tests -> Run workflow**). Tidak perlu menjalankan apa pun di laptop sendiri.

## Apa yang diuji

| Job | Isi | Platform |
| --- | --- | --- |
| `core-tests` | logika inti: penyimpanan, lock, recovery, FFmpeg (palsu), AI (palsu), shutdown, pemeriksa kualitas subtitle (CPS, panjang baris, overlap; termasuk tes performa 1000-5000 baris), perencana perbaikan, alur Analyze/Preview/Confirm/Apply/Undo, dan logika dialog Laporan Kualitas dengan GUI palsu | Linux + Windows, Python 3.10 / 3.12 |
| `gui-smoke` | aplikasi ASLI dijalankan di layar virtual: start, project baru, edit + simpan, status job, dialog Panduan/Privasi, persetujuan privasi per penyedia, antrean UI responsif, tata letak 1366x768 & portrait, buka-ulang project, penutupan bersih | Linux (xvfb) + Windows |
| `package` | membangun wheel + sdist, `twine check`, memasang wheel di venv bersih | Linux |
| `lint` | Ruff, **wajib** sejak v1.9.12: temuan menggagalkan workflow | Linux |

## Cara membaca hasil

1. Buka tab **Actions** di repo, pilih run terbaru.
2. Centang hijau = lolos. Silang merah = klik job yang gagal lalu buka langkah yang berwarna merah.
3. Di bagian bawah halaman run ada **Artifacts**: `gui-artifacts-<os>-py<versi>` (os = `ubuntu-24.04` atau `windows-latest`) berisi
   screenshot (`01_welcome.png`, `03_workspace_with_subtitles.png`, `09_workspace_1366x768.png`, ...)
   dan `kaptionpro.log`. Dengan ini kamu bisa **melihat tampilan aplikasi** tanpa menjalankannya.

## Yang TIDAK bisa diuji otomatis

Pemutaran video lewat VLC, kualitas/kecepatan Whisper pada video sungguhan, dan kenyamanan visual (warna, font)
tetap perlu mata manusia. Tes GUI memastikan alur dan logika tidak rusak, bukan menilai keindahan.

## Jika ada yang merah

| Gejala di log | Artinya | Perbaikan |
| --- | --- | --- |
| `berkas wajib TIDAK ADA di repo` (tes `test_00_repo_structure`) | folder/berkas belum ter-upload | upload ulang yang disebut di pesan, ke lokasi yang sama |
| `ModuleNotFoundError: No module named 'kaptionpro.ui'` | folder `kaptionpro/ui` tidak ada di repo | upload folder `ui` (6 berkas `.py`) ke `kaptionpro/ui/` |
| tab Actions kosong | `.github/workflows/tests.yml` tidak ada | buat lewat GitHub: Add file -> Create new file -> ketik `.github/workflows/tests.yml` |
| `PermissionError: [WinError 32]` di job Windows | berkas masih terbuka saat folder sementara dihapus (Windows menolak; Linux tidak) | sejak v1.9.10 kebocoran serupa gagal di **Linux** dengan pesan `handle berkas BOCOR: <nama berkas>`; tutup resource-nya (`close()`/`unlock()`/`session_end()`) |
| Folder `~/.kaptionpro/deleting/` berisi file `.tomb` | penghapusan project terputus (crash / daftar terkunci) | normal: dituntaskan otomatis saat KaptionPro dibuka; bila menetap, cek log baris `Pemulihan hapus ... belum tuntas` (berkas/folder backup terkunci) |
| Peringatan `Node.js 20 is deprecated` / `ubuntu-latest label will migrate` di Actions | action lama / label berjalan | sejak v1.9.14: checkout v6, setup-python v6, upload-artifact v7 dan Linux dipatok `ubuntu-24.04`; pindah ke `ubuntu-26.04` secara sengaja setelah tes hijau di sana |
| Tabel subtitle hanya menampilkan header / tombol Export tertutup di layar kecil | panel preset terlalu tinggi, jendela awal lebih besar dari layar | sejak v1.9.14 tinggi panel mengikuti jendela dan jendela awal dibatasi layar; cek baris `[test_11 ...]` di log gui-smoke untuk ukuran nyata |
| `test_11` gagal: `tabel subtitle ...px ... dijamin >= ...px` / `panel preset tampil=... seharusnya ...` / `panel preset ...px < diminta ...px` | tabel di bawah jaminan, panel preset tampil/hilang tidak sesuai ukuran jendela, atau panel lebih rendah dari yang diminta | baca baris `[test_11 ...]` di log: `tabel=`, `panel preset: tampil=/diminta=/nyata=`, `elemen lain=`, `editor tanpa tabel=`, dan baris `[test_11 grid]` (tinggi tiap baris grid + reqheight). Sejak v1.9.15.3 jendela lebih pendek dari `layout.SIDE_AUTOHIDE_H` (740) menyembunyikan panel preset (tombol `Panel` di top bar menampilkannya; jalan pintas `Generate` muncul di top bar). Catatan: `isi di dalam kanvas` (mis. 496) adalah isi panel yang di-scroll, bukan tinggi panel; di Linux/xvfb panel yang diminta 120 px pernah tercatat 210 px (penyebab belum terbukti) |
| `test_14` / `test_15` gagal: `menu Whisper terpotong ...px di bawah panel` / `panel preset ...px < kebutuhan ...px` / `jendela ...px < batas autohide` | menu Whisper model melewati tepi bawah panel preset, atau runner tidak memberi jendela cukup tinggi (sejak v1.9.17 tes pengukur GAGAL, tidak lagi di-skip) | baca baris `[test_15]` di log (panel=, butuh>=, tepi bawah menu/panel); sesuaikan `SIDE_*` di `core/layout.py` bersama `tests/test_side_panel_v1916.py` |
| `test_16` gagal: `dialog laporan kualitas tidak terbuka` / `pratinjau TIDAK boleh mengubah subtitle` / `Undo harus mengembalikan PERSIS` | alur Laporan Kualitas > Perbaiki otomatis di aplikasi nyata tidak berjalan sesuai urutan aman | lihat traceback; logika yang sama diuji tanpa GUI di `tests/test_quality_dialog_v1918.py` (kalau itu hijau, masalahnya di tata letak/widget `ui/quality_dialog.py`) |
| `test_08` gagal: `hanya N/300 callback terproses (macet ...)` / `(melewati batas atas ...)` | antrean UI berhenti berproses (macet: bug nyata) atau mesin sangat lambat (batas atas) | sejak v1.9.18.1 tes memakai pekerjaan busy-wait 2 ms (bukan `time.sleep`, yang di Windows bisa ~15,6 ms) dan gagal hanya bila tanpa kemajuan 10 detik / lewat 90 detik; baca baris `[test_08] ...` di log untuk jumlah dan waktunya |
| `tests/test_reliability_v1919.py` gagal | atomicity (rollback isi + riwayat Undo/Redo), identitas Undo terarah, atau pemberitahuan pemotongan 5.000 baris rusak | traceback menyebut tahap yang gagal (`normalize` / `verifikasi` / `clear_form` / `refresh` / `on_changed`); logikanya ada di `ui/editor.py` (`_transaction`, `undo_operation`) dan `ui/quality_dialog.py` (`limit_notice`) |
| `tests/test_consistency_v1920.py` gagal | transaksi Add/Delete/Split/Merge/Apply, pemulihan sinkron setelah rollback (retry, jalur darurat workspace, peringatan), atau integritas word timestamp rusak | traceback menyebut skenario (`add/refresh`, `R4`, `W6`, ...); logikanya di `ui/editor.py` (`_transaction`, `_resync_after_rollback`, `_seal_content`), `ui/workspace.py` (`_on_segments_changed`, `_resync_after_editor_rollback`) dan `core/word_integrity.py` |
| `tests/test_overflow_v19201.py` gagal | integer ekstrem (`10**400`, `10**10000`) pada timestamp kata/baris lolos sebagai `OverflowError`, `Word.from_dict` melempar, atau status retry workspace tidak memberi tahu | traceback menyebut tes yang gagal; `float()` dan `math.isfinite()` pada int raksasa melempar `OverflowError` (bukan `ValueError`), jadi setiap `except (TypeError, ValueError)` di sekitarnya harus memuat `OverflowError`; label `subTest` jangan memakai `str()` angka raksasa |
| job `lint` merah | Ruff wajib sejak v1.9.12: gaya kode menyimpang menggagalkan workflow (temuan juga muncul sebagai anotasi di halaman run) | jalankan `pip install ruff && ruff check .` lokal dan perbaiki temuan |
| Fitur AI/VLC nonaktif padahal paket sudah dipasang | paket gagal dimuat (bukan 'belum terpasang') | buka `~/.kaptionpro/kaptionpro.log`, cari baris `nonaktif: paket ... gagal dimuat` |
| `AssertionError: 'landscape' != 'portrait'` di `test_09` (Windows) | runner CI tidak menerapkan ukuran jendela yang diminta | sejak v1.9.11 tes menunggu ukuran berubah dan memaksa mode portrait bila OS menolak; cek baris `[test_09 ...]` di log untuk ukuran nyata |

Catatan: job `package` bisa hijau walau folder `ui` hilang, karena hanya mengimpor `core` dan `services`.

## Menjalankan lokal (opsional)

    python -m unittest discover -s tests            # tes inti (tes GUI dilewati otomatis bila tidak ada display)
    python -m unittest tests.test_gui_smoke -v      # tes GUI saja (butuh tkinter + customtkinter + display)

Jalankan tes GUI **terpisah**: data aplikasi dialihkan ke folder sementara sebelum modul `kaptionpro` diimpor,
sehingga data aslimu (`~/.kaptionpro`) tidak pernah tersentuh.
