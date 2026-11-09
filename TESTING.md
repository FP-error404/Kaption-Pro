# Menguji KaptionPro tanpa PC yang kuat

Semua pengujian berjalan otomatis di GitHub Actions saat kamu `push` / membuka pull request
(atau klik **Actions -> tests -> Run workflow**). Tidak perlu menjalankan apa pun di laptop sendiri.

## Apa yang diuji

| Job | Isi | Platform |
| --- | --- | --- |
| `core-tests` | logika inti: penyimpanan, lock, recovery, FFmpeg (palsu), AI (palsu), shutdown | Linux + Windows, Python 3.10 / 3.12 |
| `gui-smoke` | aplikasi ASLI dijalankan di layar virtual: start, project baru, edit + simpan, status job, dialog Panduan/Privasi, persetujuan privasi per penyedia, antrean UI responsif, tata letak 1366x768 & portrait, buka-ulang project, penutupan bersih | Linux (xvfb) + Windows |
| `package` | membangun wheel + sdist, `twine check`, memasang wheel di venv bersih | Linux |
| `lint` | Ruff (informasional, belum memblokir) | Linux |

## Cara membaca hasil

1. Buka tab **Actions** di repo, pilih run terbaru.
2. Centang hijau = lolos. Silang merah = klik job yang gagal lalu buka langkah yang berwarna merah.
3. Di bagian bawah halaman run ada **Artifacts**: `gui-artifacts-<os>-py<versi>` berisi
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
| job `lint` merah | Ruff menemukan gaya kode yang menyimpang | informasional; temuan muncul sebagai anotasi di halaman run |

Catatan: job `package` bisa hijau walau folder `ui` hilang, karena hanya mengimpor `core` dan `services`.

## Menjalankan lokal (opsional)

    python -m unittest discover -s tests            # tes inti (tes GUI dilewati otomatis bila tidak ada display)
    python -m unittest tests.test_gui_smoke -v      # tes GUI saja (butuh tkinter + customtkinter + display)

Jalankan tes GUI **terpisah**: data aplikasi dialihkan ke folder sementara sebelum modul `kaptionpro` diimpor,
sehingga data aslimu (`~/.kaptionpro`) tidak pernah tersentuh.
