"""Konfigurasi: logging, deteksi dependensi opsional, tema, preset, dan konstanta."""

from __future__ import annotations

import logging
import sys
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path


# CustomTkinter hanya dibutuhkan oleh GUI. Modul core (storage, ffmpeg_service, models, utils) HARUS bisa
# diimpor & diuji tanpa GUI, jadi tidak ada SystemExit di sini; entry point GUI memanggil require_gui().
try:
    import customtkinter as ctk
    HAS_CTK = True
except ImportError:
    ctk = None          # type: ignore[assignment]
    HAS_CTK = False


def require_gui() -> None:
    """Dipanggil entry point GUI (main.py) sebelum membangun UI. Keluar dengan pesan jelas bila belum terpasang."""
    if not HAS_CTK:
        raise SystemExit('CustomTkinter belum terpasang. Jalankan: pip install -r requirements.txt '
                         '(atau: pip install "customtkinter>=5.2")')


APP_DIR = Path.home() / ".kaptionpro"

# Identitas PROSES/instance ini; muncul di setiap baris log agar log beberapa instance bisa dibedakan.
SESSION_ID = uuid.uuid4().hex[:8]


class _SessionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.session = SESSION_ID
        return True


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("KaptionPro")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] [s:%(session)s] %(threadName)s: %(message)s")
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        console.addFilter(_SessionFilter())
        logger.addHandler(console)
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(APP_DIR / "kaptionpro.log", maxBytes=512_000,
                                 backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(_SessionFilter())
        logger.addHandler(fh)
    except OSError:
        logger.warning("Log file tidak bisa dibuat; hanya log ke console")
    return logger


log = setup_logging()

# Fitur opsional yang TIDAK aktif beserta alasannya: "belum terpasang" | "gagal dimuat: <jenis error>".
OPTIONAL_STATUS: dict[str, str] = {}


def _optional_unavailable(package: str, feature: str, exc: BaseException) -> None:
    """Catat KENAPA fitur opsional tidak aktif. 'Belum terpasang' (normal) dibedakan dari 'terpasang tetapi gagal
    dimuat' (mis. libvlc tak ditemukan, DLL/dependensi rusak), supaya masalah instalasi tidak tersamar."""
    if isinstance(exc, ModuleNotFoundError) and (exc.name or "").split(".")[0] == package:
        OPTIONAL_STATUS[package] = "belum terpasang"
        log.info("%s nonaktif: paket '%s' belum terpasang", feature, package)
    else:
        OPTIONAL_STATUS[package] = f"gagal dimuat: {type(exc).__name__}"
        log.warning("%s nonaktif: paket '%s' ada tetapi gagal dimuat (%s: %s)", feature, package,
                    type(exc).__name__, exc)


try:
    from faster_whisper import WhisperModel as _FasterWhisperModel  # noqa
    HAS_FASTER_WHISPER = True
except Exception as exc:  # noqa: BLE001
    _optional_unavailable("faster_whisper", "Transkripsi AI", exc)
    _FasterWhisperModel = None
    HAS_FASTER_WHISPER = False

try:
    from deep_translator import GoogleTranslator as _GoogleTranslator  # noqa
    HAS_DEEP_TRANSLATOR = True
except Exception as exc:  # noqa: BLE001
    _optional_unavailable("deep_translator", "Penerjemah online", exc)
    _GoogleTranslator = None
    HAS_DEEP_TRANSLATOR = False

try:
    import argostranslate.translate as _argos_translate  # noqa
    HAS_ARGOS = True
except Exception as exc:  # noqa: BLE001
    _optional_unavailable("argostranslate", "Penerjemah offline", exc)
    _argos_translate = None
    HAS_ARGOS = False

try:
    import vlc as _vlc  # noqa
    HAS_VLC = True
except Exception as exc:  # noqa: BLE001
    _optional_unavailable("vlc", "Preview video (VLC)", exc)
    _vlc = None
    HAS_VLC = False


class Theme:
    RADIUS = 4               # satu-satunya radius sudut (tajam, kesan "pro"); 0 hanya untuk bingkai layar penuh
    BG = "#121212"
    SURFACE = "#1A1A1A"
    SURFACE_2 = "#222222"
    VIDEO_BG = "#0B0B0B"

    GOLD = "#D4AF37"
    GOLD_BRIGHT = "#FFD700"
    GOLD_DIM = "#8A7424"
    GOLD_HOVER = "#E6C24F"

    TEXT = "#F2EBD3"
    TEXT_MUTED = "#9A9484"
    BORDER = "#3A3320"
    DANGER = "#A33A3A"
    DANGER_TEXT = "#FF7B7B"
    WARN_TEXT = "#E0A458"
    OK = "#4FA35A"

    @staticmethod
    def font(size: int = 13, weight: str = "normal", serif: bool = False) -> "ctk.CTkFont":
        family = "Georgia" if serif else "Segoe UI"
        return ctk.CTkFont(family=family, size=size, weight=weight)


# ---- Cache font global (hindari buat CTkFont tiap tick preview) ----
_FONT_CACHE: dict[tuple, "ctk.CTkFont"] = {}


def cached_font(family: str, size: int, weight: str = "normal") -> "ctk.CTkFont":
    key = (family or "Segoe UI", int(size), weight)
    font = _FONT_CACHE.get(key)
    if font is None:
        try:
            font = ctk.CTkFont(family=key[0], size=key[1], weight=key[2])
        except Exception:  # noqa: BLE001
            font = Theme.font(key[1], key[2])
        _FONT_CACHE[key] = font
    return font


@dataclass(frozen=True)
class LanguagePreset:
    key: str
    label: str
    whisper_code: str | None
    source_name: str


LANGUAGE_PRESETS: dict[str, LanguagePreset] = {
    "kdrama": LanguagePreset("kdrama", "🇰🇷  →  🇮🇩   K-Drama Mode", "ko", "Korean"),
    "cdrama": LanguagePreset("cdrama", "🇨🇳  →  🇮🇩   C-Drama Mode", "zh", "Chinese"),
    "auto": LanguagePreset("auto", "🌐  →  🇮🇩   Auto Detect", None, "Auto"),
}

TEXT_STYLES: dict[str, str] = {
    "karaoke": "Gold Karaoke Highlight",
    "minimal": "Minimalist Banner",
}

# Default "small" (CPU-friendly). Bisa diganti dari side panel.
WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3"]

# Backend terjemahan (kunci -> label). Implementasi ada di bagian SERVICE: AI.
TRANSLATORS = {
    "google": "Google (deep-translator)",
    "mymemory": "MyMemory (gratis, kuota harian)",
    "argos": "Argos (offline, paket bahasa manual)",
}

# Kualitas export: kunci -> label, dan tinggi maksimum (None = resolusi asli)
EXPORT_QUALITIES = {"original": "Original", "1080p": "1080p", "720p": "720p", "480p": "480p"}
EXPORT_HEIGHTS = {"original": None, "1080p": 1080, "720p": 720, "480p": 480}
X264_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower"]
