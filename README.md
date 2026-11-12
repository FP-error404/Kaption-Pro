# 🎬 KaptionPro

<div align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?style=for-the-badge&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/CustomTkinter-5.2%2B-gold?style=for-the-badge" />
  <img src="https://img.shields.io/badge/Build-Passing-brightgreen?style=for-the-badge&logo=githubactions&logoColor=white" />
  <img src="https://img.shields.io/badge/Tests-499%20Passed-success?style=for-the-badge" />
  <img src="https://img.shields.io/badge/Version-1.9.20-gold?style=for-the-badge" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" />
</div>

<div align="center">
  <h3>An Enterprise-Grade Subtitle Workstation for K-Drama & C-Drama.</h3>
  <p>Dibangun dengan Python, CustomTkinter, FFmpeg, dan Faster-Whisper.</p>
</div>

---

## ✨ Tentang KaptionPro

**KaptionPro** adalah aplikasi desktop *offline-first* yang dirancang khusus untuk *fansubber* K-Drama dan C-Drama. Bukan sekadar editor subtitle biasa, KaptionPro memiliki sistem **Quality Checker**, **Transactional Auto-Fix**, dan **AI Integration** yang membuat proses penerjemahan dan penyesuaian waktu (timing) menjadi lebih cepat, akurat, dan aman.

Dengan tema **"Luxury Dark-Gold"**, KaptionPro tidak hanya berfungsi sebagai alat kerja profesional, tetapi juga memberikan pengalaman visual yang elegan dan tidak melelahkan mata.

---

## 📸 Tangkapan Layar

<div align="center">
  <img src="https://via.placeholder.com/800x450/1A1A1A/D4AF37?text=KaptionPro+Workspace+UI" alt="KaptionPro Workspace" width="800"/>
  <p><i>UI Utama KaptionPro dengan tema Luxury Dark-Gold.</i></p>
</div>

---

## 🚀 Fitur Utama

*   🧠 **AI Transcription & Translation** — Terintegrasi dengan Faster-Whisper (lokal/offline) dan berbagai backend penerjemah (Google, MyMemory, Argos).
*   🛡️ **Reliability Engine** — 499 *automated tests*. Dilengkapi dengan *Atomic File Writing*, *Inter-process Locking*, dan *Tombstone Recovery*. Data lu gak bakal hilang walau aplikasi *crash*.
*   ⚡ **Transactional Auto-Fix** — Perbaiki masalah subtitle (CPS tinggi, *overlap*, durasi terlalu pendek) dengan alur aman: **Analyze → Preview → Confirm → Apply → Undo**.
*   🎨 **Luxury UI** — Antarmuka *Dark-Gold* dengan *Auto-Hide Panel* yang responsif untuk layar kecil.
*   ⚙️ **Cross-Platform** — Sepenuhnya diuji di Linux dan Windows via GitHub Actions.

---

## 🛠️ Arsitektur & Engineering Highlights

Proyek ini tidak hanya dibangun untuk "jalan", tapi untuk **tahan banting**.
*   **Clean Architecture**: Pemisahan jelas antara `core` (logika), `services` (AI/FFmpeg), dan `ui` (CustomTkinter).
*   **Mutation Testing**: Memastikan setiap *test* benar-benar mendeteksi *bug* jika kode sengaja dirusak.
*   **State Machine**: Pengelolaan *lifecycle* pekerjaan (JobTracker) dan transaksi editor yang deterministik.
*   **AST Guard**: Menolak *commit* jika ada pola kode yang melanggar aturan reliabilitas.

---

## 📦 Instalasi

### 1. Prasyarat
*   Python 3.10 atau lebih baru.
*   FFmpeg dan FFprobe (pastikan ada di sistem `PATH` lu).
*   VLC Media Player (opsional, untuk *preview* video).

### 2. Langkah-langkah
```bash
# Clone repository ini
git clone https://github.com/FP-error404/Kaption-Pro.git
cd Kaption-Pro

# Buat virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# Install dependensi
pip install -r requirements.txt
pip install -r requirements-full.txt # Jika ingin fitur AI & VLC
