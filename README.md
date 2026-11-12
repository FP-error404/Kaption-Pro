# KaptionPro 🎬⚡

> An enterprise-grade subtitle workstation. Features a fault-tolerant architecture with **499+ automated tests**, multi-model AI integration (Whisper/Translator), and atomic file locking. Fully tested against cross-platform OS differences (Linux/Windows) via GitHub Actions.

[![Build Status](https://github.com/FP-error404/KaptionPro/actions/workflows/python-app.yml/badge.svg)](https://github.com/FP-error404/KaptionPro/actions)
![Python Version](https://img.shields.io/badge/python-3.10%2B-blue)
![Tests](https://img.shields.io/badge/tests-499%20passed%20(1%20skipped)-brightgreen)
![Status](https://img.shields.io/badge/version-v1.9.20-blueviolet)

---

## 🚀 Key Features (v1.9.20)

### 🛡️ Transaction Consistency & Hardening
*   **Atomic Editor Mutations:** `split`, `merge`, `add`, `delete`, and `apply` operations are now wrapped in strict `_transaction()` blocks. If a critical failure occurs, the editor state automatically rolls back.
*   **Safe Recovery Mechanism:** If a rollback occurs or `on_changed()` fails post-commit, the system synchronizes the UI, forces an autosave (bypassing debounce), and marks the file as dirty to prevent data loss.
*   **Graceful Degradation:** UI rendering failures no longer crash the editor. The system degrades gracefully, flags `editor.sync_degraded`, and retries synchronization (up to 3 times) without losing the underlying data.

### ⏱️ Word Integrity & Auto-Fix
*   **Strict Word Timestamps:** A new `core/word_integrity.py` module validates word timestamps after Auto-Fix. 
*   **Smart Repair:** Words misaligned by ≤ 0.75 seconds are clamped; otherwise, they are discarded to maintain subtitle integrity.
*   **Zero-Tolerance for Overlaps:** The system now catches and rolls back operations that leave overlapping words or words extending past line boundaries.

### 🤖 Multi-Model AI Integration
*   Seamless integration with **Whisper** for audio transcription.
*   Multi-language translation support.

---

## 🧪 Testing & Reliability

We take reliability seriously. KaptionPro does not just run tests; we actively try to break them.

*   **499 Automated Tests** covering core logic, transactions, recovery paths, and workspace end-to-end scenarios.
*   **Mutation Testing:** Core features are sabotaged intentionally (12 distinct mutations) to ensure tests successfully catch real bugs.
*   **Cross-Platform CI:** Fully automated pipelines verify stability on both Linux and Windows environments.

*Note: GUI smoke tests require a display server (e.g., Xvfb on Linux) and are executed in our GitHub Actions CI pipeline.*

---

## 📦 Installation

KaptionPro requires Python 3.10 or higher.

1. Clone the repository:
   ```bash
   git clone https://github.com/FP-error404/KaptionPro.git
   cd KaptionPro
