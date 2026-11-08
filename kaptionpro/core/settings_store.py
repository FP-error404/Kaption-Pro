"""SettingsStore: pengaturan aplikasi kecil (JSON atomik, aman antar-instance)."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import APP_DIR, log
from .utils import _atomic_write, interprocess_critical_section


class SettingsStore:
    """Pengaturan aplikasi kecil (volume, penerjemah, opsi export terakhir) — JSON atomik, toleran rusak, dan
    aman antar-instance: set() membaca ulang file di dalam lock lalu hanya mengubah kunci yang diset, sehingga
    pengaturan lain yang diubah instance lain tidak tertimpa."""

    DEFAULTS = {"volume": 80, "muted": False, "translator": "google"}
    LOCK_TIMEOUT = 5.0

    def __init__(self, root: Path | None = None) -> None:
        self.path = (root or APP_DIR) / "settings.json"
        self.lock_path = self.path.with_name("settings.json.lock")
        self.data: dict = dict(self.DEFAULTS)
        raw = self._read_file()
        if raw:
            self.data.update(raw)

    def _read_file(self) -> dict | None:
        """Isi file sebagai dict; None bila tidak ada / rusak / bukan objek JSON."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.warning("settings.json rusak, memakai default: %s", e)
            return None
        return raw if isinstance(raw, dict) else None

    def get(self, key: str, default=None):
        return self.data.get(key, self.DEFAULTS.get(key, default))

    def set(self, key: str, value) -> bool:
        """Simpan satu pengaturan. FAIL-CLOSED: tanpa lock TIDAK menulis ke disk (tak menimpa perubahan instance
        lain dengan data basi); nilai tetap berlaku di sesi ini. Return True bila tersimpan ke disk."""
        self.data[key] = value
        with interprocess_critical_section(self.lock_path, self.LOCK_TIMEOUT) as held:
            if not held:
                log.error("Lock settings tidak didapat: pengaturan '%s' hanya berlaku di sesi ini", key)
                return False
            fresh = self._read_file()                            # data TERBARU dari disk (instance lain)
            merged = dict(self.DEFAULTS)
            merged.update(fresh if fresh is not None else self.data)
            merged[key] = value
            try:
                _atomic_write(self.path, json.dumps(merged, indent=2, ensure_ascii=False))
            except OSError as e:
                log.warning("Gagal menyimpan settings: %s", e)
                return False
            self.data = merged
            return True
