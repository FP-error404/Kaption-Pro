"""Persetujuan privasi penerjemah online PER PENYEDIA + ringkasan status (tanpa GUI, sehingga bisa diuji)."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

ACK_KEY = "online_translate_ack"      # kunci di settings.json: {"google": true, "mymemory": true}


def is_acknowledged(value, provider_key: str) -> bool:
    """True bila pengguna sudah menyetujui pengiriman teks ke penyedia ini. Nilai lama v1.9.6 (satu bool global)
    TIDAK dianggap persetujuan: pengguna ditanya sekali lagi per penyedia (default aman untuk privasi)."""
    return isinstance(value, dict) and bool(value.get(provider_key))


def with_acknowledged(value, provider_key: str) -> dict:
    """Salinan persetujuan dengan penyedia ini ditambahkan (nilai lama yang bukan dict dibuang)."""
    out = dict(value) if isinstance(value, dict) else {}
    out[provider_key] = True
    return out


def format_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def summary_text(value, online_labels: Mapping[str, str], cache_path: Path | str, cache_bytes: int) -> str:
    """Ringkasan untuk dialog privasi: penyedia yang disetujui, penerjemah offline, dan lokasi/ukuran cache."""
    approved = [label for key, label in online_labels.items() if is_acknowledged(value, key)]
    lines = [
        "Penyedia online yang sudah disetujui: " + (", ".join(approved) if approved else "belum ada") + ".",
        "Yang dikirim ke penyedia online: teks subtitle saja (suara/video tidak). Argos (offline) tidak mengirim apa pun.",
        f"Cache terjemahan: {cache_path}  ({format_size(cache_bytes)})",
    ]
    return "\n".join(lines)
