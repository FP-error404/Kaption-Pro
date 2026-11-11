"""
Subtitle Quality Checker (fondasi v2.0): pemeriksaan MURNI Python, tanpa Tk/CustomTkinter dan tanpa mengubah data.

Yang diperiksa (sengaja sedikit dan stabil):
  * CPS (karakter per detik): terlalu cepat dibaca.
  * Panjang baris: satu baris terlalu panjang, atau terlalu banyak baris.
  * Overlap: subtitle mulai sebelum subtitle sebelumnya selesai.
  * Waktu tidak valid: akhir <= awal, bukan angka terhingga, atau bukan bilangan (bool/teks ditolak); tanpa ini
    CPS tak bisa dihitung.
  * (v1.9.17, OPSIONAL, mati secara default) durasi terlalu singkat/panjang dan jeda antar-subtitle terlalu pendek:
    aktif bila QualityRules.min_duration / max_duration / min_gap diisi.

Modul ini hanya MELAPORKAN. Rencana perbaikan ada di core/quality_fix.py, alur aman (Analyze/Preview/Confirm/Apply/Undo)
di core/quality_flow.py, dan UI-nya di ui/quality_dialog.py (menu Alat > Laporan kualitas).
Segmen cukup punya atribut `start`, `end`, `text` (SubtitleSegment cocok), jadi modul ini tidak mengimpor models/config.

Kompleksitas O(n log n) (pengurutan untuk deteksi overlap); aman untuk ribuan baris (video berjam-jam).
"""

from __future__ import annotations

import math
import numbers
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Protocol

# Kode masalah (stabil: dipakai tes, log, dan nanti UI).
CPS_HIGH = "cps_high"                  # lebih cepat dari max_cps
CPS_VERY_HIGH = "cps_very_high"        # lebih cepat dari hard_max_cps
LINE_TOO_LONG = "line_too_long"
TOO_MANY_LINES = "too_many_lines"
OVERLAP = "overlap"
INVALID_TIMING = "invalid_timing"
DURATION_TOO_SHORT = "duration_too_short"   # hanya bila QualityRules.min_duration diisi
DURATION_TOO_LONG = "duration_too_long"     # hanya bila QualityRules.max_duration diisi
GAP_TOO_SMALL = "gap_too_small"             # hanya bila QualityRules.min_gap diisi

ALL_CODES = (INVALID_TIMING, OVERLAP, GAP_TOO_SMALL, CPS_VERY_HIGH, CPS_HIGH, DURATION_TOO_SHORT, DURATION_TOO_LONG,
             LINE_TOO_LONG, TOO_MANY_LINES)

_TIME_EPS = 1e-6                       # detik; menyerap noise float pada durasi (2.83 - 2.0 = 0.8300000000000001)

ERROR = "error"
WARNING = "warning"

_TAG_RE = re.compile(r"<[^>]*>|\{[^}]*\}")        # tag HTML sederhana (<i>) dan blok override ASS ({\an8}) tidak terlihat
_BREAK_RE = re.compile(r"\r\n|\r|\n|\\N|\\n")      # pemisah baris: newline asli dan penanda ASS


MAX_RULE_INT = 10_000                  # batas wajar untuk max_chars_per_line / max_lines (di atasnya pasti salah ketik)


def _safe_repr(value: object, limit: int = 40) -> str:
    """repr aman untuk pesan galat: int raksasa (>4300 digit) melempar ValueError di Python 3.11+, sangat panjang di 3.10."""
    try:
        text = repr(value)
    except (ValueError, OverflowError, RecursionError):
        text = f"<{type(value).__name__} terlalu besar>"
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _as_seconds(value: object) -> float | None:
    """
    Nilai waktu/durasi -> float terhingga, atau None. Hanya bilangan nyata (int/float/Fraction/numpy float): bool
    (True == 1 di Python) dan teks ditolak, NaN/inf ditolak, int raksasa yang tak muat di float (OverflowError) ditolak.
    """
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    try:
        seconds = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return seconds if math.isfinite(seconds) else None


class SegmentLike(Protocol):
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class QualityRules:
    """
    Ambang batas. Default mengikuti angka yang umum dipakai untuk subtitle bahasa Indonesia
    (kira-kira pedoman Netflix: 17 CPS, 42 karakter per baris, 2 baris); semuanya bisa diganti per pemanggilan.
    """

    max_cps: float = 17.0               # di atas ini: peringatan
    hard_max_cps: float = 25.0          # di atas ini: error (hampir mustahil dibaca)
    max_chars_per_line: int = 42
    max_lines: int = 2
    overlap_tolerance: float = 0.001    # detik; menyerap noise pembulatan float pada overlap dan jeda (1.0000001 vs 1.0)
    # Opsional (v1.9.17): None = pemeriksaan MATI (perilaku v1.9.16 tidak berubah). Angka umum sebagai patokan kasar:
    # min_duration ~0.83 (5/6 detik), max_duration ~7, min_gap ~0.083 (2 frame pada 24 fps).
    min_duration: float | None = None   # durasi lebih singkat dari ini -> peringatan
    max_duration: float | None = None   # durasi lebih panjang dari ini -> peringatan
    min_gap: float | None = None        # jeda ke subtitle sebelumnya lebih pendek dari ini (tapi tidak overlap) -> peringatan

    def __post_init__(self) -> None:
        # v1.9.18: SEMUA angka lewat _as_seconds (bilangan nyata terhingga yang muat di float). Dulu math.isfinite(10**10000)
        # melempar OverflowError, bukan ValueError. Pesan galat memakai _safe_repr agar int raksasa tidak ikut meledak.
        for name in ("max_cps", "hard_max_cps", "overlap_tolerance"):
            value = getattr(self, name)
            if _as_seconds(value) is None:
                raise ValueError(f"{name} harus angka terhingga (bukan bool/teks/NaN/inf/terlalu besar), "
                                 f"bukan {_safe_repr(value)}")
        for name in ("max_chars_per_line", "max_lines"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} harus bilangan bulat, bukan {_safe_repr(value)}")
            if not 1 <= value <= MAX_RULE_INT:
                raise ValueError(f"{name} harus antara 1 dan {MAX_RULE_INT}, bukan {_safe_repr(value)}")
        if self.max_cps <= 0:
            raise ValueError("max_cps harus > 0")
        if self.hard_max_cps < self.max_cps:
            raise ValueError("hard_max_cps tidak boleh lebih kecil dari max_cps")
        if self.overlap_tolerance < 0:
            raise ValueError("overlap_tolerance tidak boleh negatif")
        for name in ("min_duration", "max_duration", "min_gap"):
            value = getattr(self, name)
            if value is not None and _as_seconds(value) is None:
                raise ValueError(f"{name} harus None atau angka terhingga (bukan bool/teks/terlalu besar), "
                                 f"bukan {_safe_repr(value)}")
        if self.min_duration is not None and self.min_duration <= 0:
            raise ValueError("min_duration harus > 0 (atau None)")
        if self.max_duration is not None and self.max_duration <= 0:
            raise ValueError("max_duration harus > 0 (atau None)")
        if self.min_duration is not None and self.max_duration is not None and self.max_duration < self.min_duration:
            raise ValueError("max_duration tidak boleh lebih kecil dari min_duration")
        if self.min_gap is not None and self.min_gap < 0:
            raise ValueError("min_gap tidak boleh negatif (atau None)")


DEFAULT_RULES = QualityRules()


@dataclass(frozen=True)
class QualityIssue:
    index: int                          # posisi segmen di daftar masukan (0-based)
    code: str
    severity: str
    message: str                        # bahasa Indonesia, siap tampil
    value: float | None = None          # nilai terukur (CPS, panjang baris, detik overlap, jumlah baris)
    limit: float | None = None          # batas yang dilanggar
    other_index: int | None = None      # untuk overlap: segmen yang tumpang tindih dengan ini


@dataclass
class QualityReport:
    total: int = 0
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[QualityIssue]:
        return [i for i in self.issues if i.severity == ERROR]

    @property
    def warnings(self) -> list[QualityIssue]:
        return [i for i in self.issues if i.severity == WARNING]

    @property
    def ok(self) -> bool:
        return not self.issues

    def counts(self) -> dict[str, int]:
        """Jumlah masalah per kode (hanya yang ada), dalam urutan ALL_CODES."""
        c = Counter(i.code for i in self.issues)
        return {code: c[code] for code in ALL_CODES if c[code]}

    def by_index(self) -> dict[int, list[QualityIssue]]:
        out: dict[int, list[QualityIssue]] = {}
        for issue in self.issues:
            out.setdefault(issue.index, []).append(issue)
        return out

    def summary(self) -> str:
        if self.total == 0:
            return "Tidak ada subtitle untuk diperiksa."
        if self.ok:
            return f"Semua {self.total} subtitle lolos pemeriksaan kualitas."
        bad = len({i.index for i in self.issues})
        return (f"{bad} dari {self.total} subtitle bermasalah: {len(self.errors)} error, "
                f"{len(self.warnings)} peringatan.")


def split_lines(text: str | None) -> list[str]:
    """Pecah teks jadi baris tampil (newline asli atau penanda ASS \\N); tag tak terlihat dibuang; baris kosong dilewati."""
    cleaned = _TAG_RE.sub("", str(text or ""))
    return [line.strip() for line in _BREAK_RE.split(cleaned) if line.strip()]


def visible_char_count(text: str | None) -> int:
    """Karakter yang dibaca penonton: spasi antar-kata dihitung, pemisah baris dan tag tak terlihat tidak."""
    return sum(len(line) for line in split_lines(text))


def cps(text: str | None, duration: float) -> float | None:
    """Karakter per detik; None bila durasi tidak valid (<= 0 atau bukan angka terhingga)."""
    seconds = _as_seconds(duration)
    if seconds is None or seconds <= 0:
        return None
    return visible_char_count(text) / seconds


def _timing(seg: SegmentLike) -> tuple[float, float] | None:
    """(awal, akhir) dalam detik bila valid (keduanya bilangan terhingga, bukan bool/teks, dan akhir > awal), selain itu None."""
    start, end = _as_seconds(getattr(seg, "start", None)), _as_seconds(getattr(seg, "end", None))
    if start is None or end is None or end <= start:
        return None
    return start, end


def _valid_timing(seg: SegmentLike) -> bool:
    return _timing(seg) is not None


def check_subtitles(segments: Iterable[SegmentLike], rules: QualityRules | None = None) -> QualityReport:
    """
    Periksa daftar subtitle; urutan masukan boleh acak (overlap dicari setelah diurutkan menurut waktu mulai).
    Tidak pernah melempar karena data segmen jelek (waktu NaN/None dilaporkan sebagai INVALID_TIMING) dan tidak
    mengubah segmen. Hasil terurut menurut indeks, lalu tingkat keparahan.
    """
    rules = rules or DEFAULT_RULES
    segs = list(segments)
    issues: list[QualityIssue] = []
    valid: list[tuple[float, float, int]] = []                    # (start, end, indeks asli) hanya untuk waktu valid

    for index, seg in enumerate(segs):
        text = getattr(seg, "text", "")
        timing = _timing(seg)
        if timing is None:
            issues.append(QualityIssue(index, INVALID_TIMING, ERROR,
                                       "Waktu tidak valid (akhir harus setelah awal dan berupa angka)."))
        else:
            start, end = timing
            duration = end - start
            valid.append((start, end, index))
            if rules.min_duration is not None and duration < rules.min_duration - _TIME_EPS:
                issues.append(QualityIssue(index, DURATION_TOO_SHORT, WARNING,
                                           f"Terlalu singkat: {duration:.3f} detik (minimal {rules.min_duration:g}).",
                                           duration, rules.min_duration))
            if rules.max_duration is not None and duration > rules.max_duration + _TIME_EPS:
                issues.append(QualityIssue(index, DURATION_TOO_LONG, WARNING,
                                           f"Terlalu lama tampil: {duration:.3f} detik (maksimal {rules.max_duration:g}).",
                                           duration, rules.max_duration))
            rate = cps(text, duration)
            if rate is not None and rate > rules.max_cps:
                hard = rate > rules.hard_max_cps
                issues.append(QualityIssue(
                    index, CPS_VERY_HIGH if hard else CPS_HIGH, ERROR if hard else WARNING,
                    f"Terlalu cepat dibaca: {rate:.1f} karakter/detik (batas {rules.hard_max_cps if hard else rules.max_cps:g}).",
                    rate, rules.hard_max_cps if hard else rules.max_cps))

        lines = split_lines(text)
        longest = max((len(line) for line in lines), default=0)
        if longest > rules.max_chars_per_line:
            where = next(n for n, line in enumerate(lines, 1) if len(line) == longest)
            issues.append(QualityIssue(index, LINE_TOO_LONG, WARNING,
                                       f"Baris {where} terlalu panjang: {longest} karakter (batas {rules.max_chars_per_line}).",
                                       longest, rules.max_chars_per_line))
        if len(lines) > rules.max_lines:
            issues.append(QualityIssue(index, TOO_MANY_LINES, WARNING,
                                       f"Terlalu banyak baris: {len(lines)} (batas {rules.max_lines}).",
                                       len(lines), rules.max_lines))

    issues.extend(_scan_neighbours(valid, rules))
    severity_rank = {ERROR: 0, WARNING: 1}
    issues.sort(key=lambda i: (i.index, severity_rank[i.severity], ALL_CODES.index(i.code)))
    return QualityReport(total=len(segs), issues=issues)


def _scan_neighbours(valid: list[tuple[float, float, int]], rules: QualityRules) -> list[QualityIssue]:
    """
    Sapuan satu arah pada segmen yang diurutkan menurut waktu mulai: tiap segmen dibandingkan dengan segmen yang
    sejauh ini berakhir PALING AKHIR (bukan hanya tetangganya), jadi segmen panjang yang menelan beberapa segmen
    pendek tetap terdeteksi. Satu masalah per segmen yang mulai terlalu dini; `other_index` menunjuk segmen yang tertimpa.

    jeda = awal segmen ini - akhir terjauh sebelumnya. jeda < -toleransi: OVERLAP (error). Selain itu, bila min_gap
    diisi dan jeda < min_gap - toleransi: GAP_TOO_SMALL (peringatan; segmen yang menempel/jedanya nol ikut terhitung).
    Keduanya saling meniadakan, jadi satu pasangan tidak dilaporkan ganda.
    """
    out: list[QualityIssue] = []
    tolerance, min_gap = rules.overlap_tolerance, rules.min_gap
    ordered = sorted(valid, key=lambda t: (t[0], t[1], t[2]))
    reach_end, reach_index = -math.inf, -1
    for start, end, index in ordered:
        if reach_index >= 0:
            gap = start - reach_end
            if gap < -tolerance:
                overlap = -gap
                out.append(QualityIssue(index, OVERLAP, ERROR,
                                        f"Tumpang tindih {overlap:.3f} detik dengan subtitle #{reach_index + 1}.",
                                        overlap, tolerance, reach_index))
            elif min_gap is not None and gap < min_gap - tolerance:
                out.append(QualityIssue(index, GAP_TOO_SMALL, WARNING,
                                        f"Jeda ke subtitle #{reach_index + 1} terlalu pendek: {max(gap, 0.0):.3f} detik "
                                        f"(minimal {min_gap:g}).", max(gap, 0.0), min_gap, reach_index))
        if end > reach_end:
            reach_end, reach_index = end, index
    return out
