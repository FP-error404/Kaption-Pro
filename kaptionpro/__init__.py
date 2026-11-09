"""
KaptionPro v1.9.3
=================
Spesialis pembuat/penerjemah subtitle K-Drama & C-Drama.
Tema UI: "Luxury Dark-Gold".

Instalasi:
    pip install "customtkinter>=5.2"
    pip install faster-whisper deep-translator python-vlc   # opsional, untuk AI & preview nyata
    pip install argostranslate                               # opsional: penerjemah offline
    # FFmpeg + ffprobe di PATH untuk Export MP4

Perubahan v1.9.3 (temuan audit ketiga)
--------------------------------------
 * Penanda sesi: tidak ada lagi unlink manual setelah lock dilepas. POSIX: release() menghapus selagi terkunci;
   Windows: penanda dibiarkan bertanda CLEAN dan hanya dibersihkan bila CLEAN DAN > 24 jam tak disentuh
   (_cleanup_session_markers) sehingga tidak ada celah dengan proses yang baru mengambil lock.
   Bug nyata ikut diperbaiki: probe sesi dulu menimpa isi file dengan PID-nya sendiri sehingga penanda CLEAN
   yang tertinggal (Windows) terbaca sebagai crash di SETIAP start; kini probe membaca isi lama dulu
   (InterProcessLock.acquire(write_info=False)). Pengambilan penanda juga dicoba ulang dengan nama lain bila
   nama bentrok/sedang dikunci pemindai, agar instance tak berjalan tanpa penanda.
 * ID project divalidasi ketat: 32 karakter hex huruf kecil (is_valid_project_id). Project(...) menolak ID tak valid;
   ProjectStore memvalidasi ULANG tiap membentuk path (projects/snapshots/locks/preview/backups); entri daftar
   ber-ID tak valid TIDAK dipakai dan disalin ke recent_projects.rejected-<waktu>.json + catatan ke user.
   Entri tanpa ID (format lama) tetap dimigrasikan dengan ID baru.
 * Pemulihan: bila semua backup juga rusak, pesan kini jujur ("semua N backup rusak") dan file rusak tetap
   disimpan (.corrupt-...). TranslationCache.BUSY_TIMEOUT dapat diatur (default 5 dtk).
 * Log: setiap baris memuat ID sesi ([s:xxxxxxxx]); startup mencatat versi, sesi, dan PID.
 * Tes: 67 tes (tambahan: sesi multi-proses & simulasi Windows, ID project berbahaya, hapus bersamaan dua+ proses,
   semua backup rusak, SQLite terkunci, ffprobe gagal/tak ada, FFmpeg disk penuh, penutupan app saat export).

Perubahan v1.9.2 (temuan audit lanjutan)
----------------------------------------
 * Hapus project dipisah: ProjectStore.remove_from_recent() (hanya daftar; data tetap) dan
   ProjectStore.delete_project() (permanen). delete_project mengambil lock EKSKLUSIF dulu; bila project dipakai
   instance lain (atau terbuka di proses ini) penghapusan DIBATALKAN tanpa menyentuh apa pun; bila sebagian file
   gagal dihapus, project tetap di daftar. API lama remove() dihapus. Dialog UI: Ya = dari daftar saja,
   Tidak = hapus permanen (konfirmasi kedua), Batal.
 * Windows: file lock TIDAK lagi dihapus saat release (hanya kunci OS dilepas), menutup celah dua file lock
   berbeda. Karena itu file reservasi export dipindah ke APP_DIR/export-locks (nama dari hash path tujuan),
   tidak lagi di sebelah video hasil. POSIX tetap menghapus file lock dengan verifikasi inode.
 * _atomic_write tidak lagi menelan kegagalan fsync (EIO/ENOSPC/...): OSError dilempar dan file lama dipertahankan;
   hanya "fsync tidak didukung filesystem" yang dimaklumi. Folder juga di-fsync (POSIX) setelah rename.
 * Backup divalidasi dengan membaca ulang dari disk (JSON valid, jumlah baris, checksum) SEBELUM rotasi;
   backup yang tak terbukti utuh dihapus dan tidak menggeser backup lama yang baik.
 * Penanda sesi per instance (sessions/<pid>-<id>.session, dikunci selama hidup) menggantikan session.json bersama:
   beberapa instance tak saling menimpa, instance hidup tak dianggap crash, PID yang dipakai ulang tak menipu.
   session.json lama dimigrasikan otomatis.
 * config.py tidak lagi SystemExit saat CustomTkinter tidak ada; GUI memanggil require_gui(). Core + tes bisa
   dijalankan tanpa GUI. Ditambah requirements.txt dan .github/workflows/tests.yml (Linux + Windows).
 * Tes: 50 tes (hapus project, fsync, validasi backup, sesi, crash saat commit, cache rusak, core tanpa GUI).

Perubahan v1.9.1 (perbaikan temuan audit v1.9)
----------------------------------------------
 * Lock project & reservasi export kini memakai KUNCI OS (flock di POSIX, msvcrt di Windows) lewat
   core.utils.InterProcessLock, bukan lagi "file ada = terkunci". Menutup dua race: (1) file lock baru
   terbaca kosong oleh proses lain sebelum PID tertulis -> dianggap rusak lalu dihapus -> dua pemegang
   lock; (2) dua proses pembersih lock "basi" saling menghapus lock BARU lawannya. Tidak ada lagi tahap
   menilai/menghapus lock milik proses lain; kunci dilepas OS bila pemilik crash.
 * Export: file .kp_partial SELALU dibersihkan bila os.replace() gagal (izin, file tujuan terkunci,
   filesystem) — sebelumnya `ok = True` sudah diset sebelum commit. Kegagalan commit dilaporkan sebagai
   FFmpegError yang jelas; file tujuan lama tidak disentuh.
 * Pembatalan: menunggu FFmpeg keluar kini tanggap terhadap Cancel; Cancel setelah FFmpeg selesai tidak
   meng-commit hasil; proses yang tak bisa dihentikan dilaporkan (bukan diam-diam dianggap batal).
 * _atomic_write memakai nama file sementara unik (pid+uuid) dan menghapusnya bila gagal (disk penuh).
 * Tes baru: tests/test_data_safety.py (lock multi-proses, crash, disk penuh, autosave bersamaan,
   pembatalan FFmpeg, pemulihan crash). Jalankan: python -m unittest discover -s tests -v

Perubahan v1.9 (audit keamanan data v1.8)
-----------------------------------------
 * "Lanjut tanpa menyimpan" kini benar-benar membuang perubahan: timer autosave dibatalkan dan status
   dirty/gagal-simpan di-reset, sehingga flush() berikutnya tidak menyimpan lagi perubahan yang sudah
   ditolak user. Bila aksi yang meminta keputusan itu dibatalkan kemudian (mis. user memilih tetap di
   workspace), pembuangan dibatalkan juga dan autosave dijadwalkan lagi.
 * Menutup aplikasi TIDAK lagi lanjut diam-diam bila ensure_saved() melempar exception: user ditanya
   (default = tetap terbuka); hanya bila user memilih menutup, perubahan dibuang dengan sadar.
 * Ctrl+S memeriksa hasil flush(): status "Tersimpan" hanya tampil bila penyimpanan benar-benar berhasil;
   bila gagal user diberi dialog dan indikator tetap merah.
 * Export ke tujuan yang sama kini memakai RESERVASI ATOMIK (O_CREAT|O_EXCL pada '<tujuan>.kp_lock')
   sebelum FFmpeg dimulai, menggantikan pola periksa-lalu-buat. Reservasi dilepas di finally.
 * Pembatalan buka project mengambil kembali lock project lama DAN memperbarui _lock_held sesuai hasilnya
   (status "tanpa perlindungan lock" muncul bila lock lama tak bisa diambil lagi).

Perubahan v1.8 (audit keamanan data v1.7)
-----------------------------------------
 * Snapshot pemulihan TIDAK lagi dihapus bila penyimpanan hasil pemulihan gagal (disk penuh / izin):
   snapshot dipertahankan, user diberi peringatan, dan baru dihapus setelah autosave berhasil.
 * flush() kini mengembalikan True/False. Pindah ke Home, membuka project lain, dan menutup aplikasi
   TIDAK lagi lanjut diam-diam bila simpan gagal: user memilih coba lagi / lanjut tanpa menyimpan
   (dengan tawaran salinan darurat) / batal.
 * Lock project fail-CLOSED: bila file/folder lock tidak bisa dibuat, lock() mengembalikan False dengan
   last_lock_error; user diperingatkan jelas dan project ditandai "tanpa perlindungan lock".
 * Timestamp kata (karaoke) divalidasi & dinormalisasi lengkap: di dalam rentang segmen, berurutan,
   durasi nol diperbaiki, tumpang tindih kecil dipotong; data yang tak bisa dipercaya dibuang dan
   renderer ASS memakai pembagian proporsional dari teks. Renderer tidak pernah memakai kata tak valid.
 * File partial FFmpeg bernama unik per proses (pid + UUID): export bersamaan ke tujuan yang sama
   tidak saling menghapus; export kedua ditolak dengan pesan jelas; partial basi milik proses mati dibersihkan.

(Perubahan v1.9.11 - Tes GUI toleran terhadap runner CI:
 * test_09 (tata letak 1366x768 & portrait) gagal di Windows CI dengan 'landscape' != 'portrait' karena runner tidak
   menerapkan ukuran jendela yang diminta. Tes kini menunggu sampai ukuran benar-benar berubah (_resize), memeriksa
   konsistensi mode dengan ukuran nyata, dan hanya menuntut 'portrait' ketat bila jendela memang berhasil diubah.
   Bila tidak, cabang portrait tetap diuji dengan memaksa mode-nya. Tidak ada perubahan perilaku aplikasi.)

(Perubahan v1.9.10 - Kebocoran handle berkas (WinError 32 di CI Windows):
 * Bug nyata: database cache yang RUSAK membuat cache "dimatikan" tetapi koneksinya dibiarkan terbuka -> berkas rusak itu
   tidak bisa dihapus/diganti di Windows selama aplikasi berjalan. Kini koneksi ditutup.
 * InterProcessLock.__del__ menutup fd bila objek dibuang tanpa release(); ProjectStore.close() melepas semua lock project
   di bawah foldernya + penanda sesi.
 * Tes: SafeTemporaryDirectory (tests/winsafe.py) -- di Linux GAGAL dengan daftar berkas bila ada handle bocor, di Windows
   mencoba ulang lalu toleran. 10 tes pelaku kini menutup resource secara eksplisit (store.close(), cache.close()).)

(Perubahan v1.9.9 - Perbaikan setelah run CI pertama:
 * tests/test_00_repo_structure.py: deteksi berkas/folder yang belum ter-upload dengan pesan jelas (penyebab
   'No module named kaptionpro.ui' pada run pertama).
 * Lint: 13 temuan ruff dibereskan (E741 x2, E501, F401 x2; E402 main.py diabaikan lewat pyproject).
 * Tes GUI: handler log ditutup + folder sementara dibersihkan dengan retry (WinError 32 di Windows).
 * Kode aplikasi lain TIDAK berubah.)

(Perubahan v1.9.8 - Tes GUI otomatis:
 * tests/test_gui_smoke.py: aplikasi ASLI dijalankan di CI (Linux xvfb + Windows) dengan data dialihkan ke folder
   sementara; screenshot + log diunggah sebagai artefak. Job CI 'gui-smoke' + workflow_dispatch. Lihat TESTING.md.
 * Kode aplikasi TIDAK berubah dibanding v1.9.7 selain nomor versi.)

(Perubahan v1.9.7 - Refactor awal (perilaku tetap) + hardening:
 * JobTracker (core/jobs.py): status job aktif (jenis/token/cancel_event/worker) diekstrak dari WorkspaceScreen.
 * storage.py dipecah: SettingsStore -> core/settings_store.py, TranslationCache -> core/translation_cache.py
   (nama lama tetap bisa diimpor dari core.storage).
 * drain_queue: batas WAKTU (10 ms) selain batas jumlah; minimal satu callback per putaran.
 * Cache terjemahan: ts = TERAKHIR DIPAKAI (disentuh saat dibaca, maks. sekali/hari), VACUUM setelah pemangkasan besar.
 * Privasi: persetujuan online PER PENYEDIA, dialog Privasi di Panduan (status, hapus cache, cabut persetujuan).
 * requirements.txt dan pyproject.toml disatukan (customtkinter>=5.2,<6); job CI 'package' membangun + memasang wheel.)

(Perubahan v1.9.6 - Stabilization:
 * Pembersih/pemindai marker sesi tidak lagi membuat file (acquire(create=False)): marker yang baru dihapus proses
   lain tidak dihidupkan kembali sebagai file kosong.
 * Antrean callback UI diproses berbatas (100 per putaran) agar UI tidak tampak membeku saat worker membanjiri.
 * Privasi: pemberitahuan sekali sebelum penerjemah ONLINE; teks subtitle tidak lagi muncul di log.
 * Cache terjemahan: indeks timestamp + retensi (180 hari / 200.000 entri), dipangkas di latar belakang saat start.
 * Export tanpa ffprobe: selain ukuran, struktur kontainer MP4 (ftyp/moov/mdat) kini diperiksa.
 * pyproject.toml (metadata, extras, konfigurasi ruff) + job lint informasional di CI.)

(Perubahan v1.9.5 - Reliability Hardening:
 * Penutupan: ShutdownCoordinator (core/lifecycle.py). Worker yang tak berhenti (mis. inferensi Whisper) TIDAK lagi
   membuat cache SQLite ditutup / lock project dilepas di bawahnya; proses keluar paksa setelah jendela ditutup.
 * TranslationCache: _conn dibaca di dalam lock pada semua metode (close() tidak bisa menyelinap -> tak ada AttributeError).
 * recent_projects.json dan settings.json FAIL-CLOSED: tanpa lock tidak ada yang ditulis ke disk; kegagalan dilaporkan.
 * Memuat daftar tanpa lock bersifat read-only (tanpa karantina/pemulihan/migrasi/penulisan).
 * Tes ConcurrentDeleteTests memakai barrier READY/GO.)

(Perubahan v1.9.4 - Reliability Update:
 * Shutdown: penutupan aplikasi menunggu worker BENAR-BENAR berhenti (WorkerRegistry) tanpa membekukan UI; resource
   dan lock dilepas sesudahnya. Bila tak berhenti: dialog tunggu lagi / tutup paksa (FFmpeg aktif dimatikan).
 * Penanda sesi: pembersihan memakai kunci OS lebih dulu, memverifikasi ulang status+umur, lalu menghapus selagi terkunci.
 * recent_projects.json dan settings.json: read-modify-write di dalam lock antar-proses (tidak ada lost update);
   daftar dimuat ulang saat kembali ke Home.
 * Terjemahan: batas waktu per-panggilan (30 dtk) + batas total (1 jam), Cancel tanggap walau jaringan menggantung.
 * Koneksi cache SQLite ditutup saat aplikasi ditutup.)

(Perubahan v1.7: validasi & binary search subtitle, cache/backend terjemahan, preset export + validasi
output, backup berversi, lock atomik, VLC volume/frame-step, editor cari/filter/undo-redo/bulk.)
"""

__version__ = "1.9.11"
