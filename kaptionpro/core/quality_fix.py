"""
Perencana perbaikan kualitas subtitle (v1.9.18): MURNI Python, tanpa Tk dan tanpa mengubah data masukan.

`plan_fixes(segments, rules)` hanya MEMBUAT RENCANA: daftar perubahan per baris (sebelum -> sesudah, beserta alasannya)
dan daftar masalah yang tidak bisa diperbaiki otomatis. Tidak ada yang diterapkan di sini; menerapkan (dengan konfirmasi,
backup, dan Undo) adalah tugas lapisan UI (ui/quality_dialog.py + ui/editor.py) lewat core/quality_flow.py.

Prinsip: konservatif dan sejajar baris (tidak menghapus, memecah, atau menggabung baris; jumlah dan urutan baris tetap).
Yang diperbaiki otomatis:
  * baris terlalu panjang / terlalu banyak baris -> teks dibungkus ulang seimbang (hanya spasi/pemisah baris yang berubah);
  * overlap dan jeda terlalu pendek (bila min_gap diisi) -> akhir baris sebelumnya dipendekkan; bila tak muat, awal baris
    berikutnya digeser; keduanya hanya jika durasi hasilnya tidak jadi terlalu singkat;
  * CPS terlalu tinggi dan durasi terlalu singkat -> akhir baris diperpanjang ke ruang kosong sebelum baris berikutnya
    (hanya bila SELURUH kebutuhan terpenuhi dan tidak melewati max_duration; tidak ada perpanjangan setengah-setengah).
Tidak diperbaiki otomatis (dilaporkan apa adanya): waktu tidak valid, durasi terlalu panjang, teks bertag, kata yang lebih
panjang dari satu baris, dialog bergaris-awal yang tidak muat, dan kasus yang butuh memotong durasi di bawah batas aman.

Setelah rencana disusun, hasilnya DIVERIFIKASI dengan pemeriksa kualitas yang sama: `resolved` (masalah yang hilang),
`introduced` (masalah BARU akibat perbaikan, mis. CPS naik karena akhir dipendekkan demi menghilangkan overlap) dan
`remaining` (masalah yang masih ada, dengan alasan).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable

from . import subtitle_quality as sq

SAFE_MIN_DURATION = 0.3           # detik; batas aman bila min_duration tidak diisi: baris tidak dipendekkan di bawah ini
_EPS = 1e-6                       # detik; toleransi noise float
_MARGIN = 0.0005                  # detik; kelebihan kecil saat memperpanjang agar CPS tidak tepat di batas (noise float)
_BREAK_RE = re.compile(r"\r\n|\r|\n|\\N|\\n")
_DASHES = "-–—"

REASON_DEFAULT = "perlu diedit manual"
_CODE_REASON = {
    sq.INVALID_TIMING: "waktu tidak valid; perbaiki awal/akhir secara manual",
    sq.DURATION_TOO_LONG: "durasi terlalu panjang; pecah baris atau persingkat secara manual",
}


# ---------------------------------------------------------------------------------------------------------- struktur data
@dataclass(frozen=True)
class Row:
    """Salinan beku (awal, akhir, teks) satu baris; dipakai sebagai 'sebelum', 'sesudah', dan sidik jari sumber."""

    start: object
    end: object
    text: str


@dataclass(frozen=True)
class FixChange:
    index: int                        # posisi baris di daftar masukan (0-based)
    before: Row
    after: Row
    reasons: tuple[str, ...]          # kode masalah (subtitle_quality.*) yang dituju perubahan ini
    notes: tuple[str, ...]            # keterangan Indonesia siap tampil, mis. "Akhir dipendekkan ..."
    timing_changed: bool
    text_changed: bool


@dataclass(frozen=True)
class Remaining:
    """Masalah yang masih ada setelah rencana diterapkan (atau tidak disentuh)."""

    index: int
    code: str
    severity: str
    message: str
    reason: str                       # kenapa tidak diperbaiki otomatis


@dataclass
class FixPlan:
    rules: sq.QualityRules
    source: tuple[tuple[object, object, str], ...]          # sidik jari masukan: (awal, akhir, teks) per baris
    changes: list[FixChange] = field(default_factory=list)
    before: sq.QualityReport = field(default_factory=sq.QualityReport)
    after: sq.QualityReport = field(default_factory=sq.QualityReport)
    resolved: list[tuple[int, str]] = field(default_factory=list)
    introduced: list[tuple[int, str]] = field(default_factory=list)
    remaining: list[Remaining] = field(default_factory=list)
    rows_after: tuple[Row, ...] = ()

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    @property
    def is_safe(self) -> bool:
        """True bila perbaikan tidak memunculkan masalah baru."""
        return not self.introduced

    def matches(self, segments: Iterable[object]) -> bool:
        """Apakah `segments` masih PERSIS sama dengan yang dianalisis (rencana basi bila tidak)."""
        return fingerprint(segments) == self.source

    def summary(self) -> str:
        if not self.changes:
            tail = f" {len(self.remaining)} masalah perlu diedit manual." if self.remaining else ""
            return "Tidak ada yang bisa diperbaiki otomatis." + tail
        text = (f"{len(self.changes)} baris akan diubah; {len(self.resolved)} masalah selesai, "
                f"{len(self.remaining)} masih tersisa (perlu diedit manual).")
        if self.introduced:
            text += f" ⚠ {len(self.introduced)} masalah BARU muncul akibat perbaikan (lihat pratinjau)."
        return text


def fingerprint(segments: Iterable[object]) -> tuple[tuple[object, object, str], ...]:
    return tuple((getattr(s, "start", None), getattr(s, "end", None), str(getattr(s, "text", "") or "")) for s in segments)


# ------------------------------------------------------------------------------------------------------- bungkus teks
def _greedy(words: list[str], width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _balanced(line: str, max_chars: int) -> list[str] | None:
    """Bungkus satu paragraf jadi baris seimbang (jumlah baris minimum, selebar mungkin sama); None bila ada kata > max_chars."""
    words = line.split()
    if not words:
        return []
    if max(len(w) for w in words) > max_chars:
        return None
    total = len(" ".join(words))
    if total <= max_chars:
        return [" ".join(words)]
    count = len(_greedy(words, max_chars))
    lo, hi = max(max(len(w) for w in words), math.ceil(total / count)), max_chars
    while lo < hi:                                   # lebar terkecil yang masih muat dalam `count` baris
        mid = (lo + hi) // 2
        if len(_greedy(words, mid)) <= count:
            hi = mid
        else:
            lo = mid + 1
    return _greedy(words, lo)


def wrap_text(text: str | None, max_chars: int, max_lines: int) -> str | None:
    """
    Bungkus ulang teks agar tiap baris <= max_chars dan jumlah baris <= max_lines, atau None bila tidak bisa dengan aman
    (mengandung tag <..>/{..}, ada kata lebih panjang dari satu baris, atau tidak muat). Pemisah baris yang sudah ada
    dihormati lebih dulu; bila total baris melebihi batas, baris digabung hanya bila tak ada yang diawali tanda dialog (-).
    """
    raw = str(text or "")
    if "<" in raw or "{" in raw:
        return None
    lines = [ln.strip() for ln in _BREAK_RE.split(raw) if ln.strip()]
    if not lines:
        return ""
    out: list[str] = []
    for ln in lines:
        wrapped = _balanced(ln, max_chars)
        if wrapped is None:
            return None
        out.extend(wrapped)
    if len(out) <= max_lines:
        return "\n".join(out)
    if len(lines) > 1 and not any(ln[:1] in _DASHES for ln in lines):
        merged = _balanced(" ".join(lines), max_chars)
        if merged is not None and len(merged) <= max_lines:
            return "\n".join(merged)
    return None


# ----------------------------------------------------------------------------------------------------------- pembulatan
def _floor_ms(x: float) -> float:
    return math.floor(x * 1000 + 1e-7) / 1000


def _ceil_ms(x: float) -> float:
    return math.ceil(x * 1000 - 1e-7) / 1000


# ---------------------------------------------------------------------------------------------------------- perencana
def plan_fixes(segments: Iterable[object], rules: sq.QualityRules | None = None) -> FixPlan:
    """Susun rencana perbaikan (tanpa menerapkan apa pun). Lihat docstring modul untuk apa yang diperbaiki."""
    rules = rules or sq.DEFAULT_RULES
    segs = list(segments)
    source = fingerprint(segs)
    before = sq.check_subtitles(segs, rules)
    n = len(segs)

    texts = [src[2] for src in source]
    timings = [sq._timing(s) for s in segs]
    starts = [t[0] if t else 0.0 for t in timings]
    ends = [t[1] if t else 0.0 for t in timings]
    valid = [i for i in range(n) if timings[i] is not None]
    reasons: dict[int, set[str]] = {}
    notes: dict[int, list[str]] = {}
    blocked: dict[tuple[int, str], str] = {}
    timing_touched: set[int] = set()
    by_index = before.by_index()

    def note(i: int, code: str | None, message: str) -> None:
        if code:
            reasons.setdefault(i, set()).add(code)
        notes.setdefault(i, []).append(message)

    # 1) teks: baris terlalu panjang / terlalu banyak baris
    text_codes = {sq.LINE_TOO_LONG, sq.TOO_MANY_LINES}
    for i, issues in by_index.items():
        hit = {x.code for x in issues} & text_codes
        if not hit:
            continue
        new = wrap_text(texts[i], rules.max_chars_per_line, rules.max_lines)
        if new is None:
            for code in hit:
                blocked[(i, code)] = ("teks bertag, dialog bergaris-awal, atau ada kata yang lebih panjang dari satu baris; "
                                      "edit manual")
        elif new != texts[i]:
            texts[i] = new
            for code in hit:
                reasons.setdefault(i, set()).add(code)
            notes.setdefault(i, []).append(f"Teks dirapikan menjadi {len(new.splitlines())} baris")

    gap_target = float(rules.min_gap or 0.0)
    floor = float(rules.min_duration) if rules.min_duration is not None else SAFE_MIN_DURATION
    tol = float(rules.overlap_tolerance)
    order = sorted(valid, key=lambda i: (starts[i], ends[i], i))

    # 2) overlap / jeda terlalu pendek: pendekkan akhir baris sebelumnya; bila tak muat, geser awal baris berikutnya
    for k in range(1, len(order)):
        prev, cur = order[k - 1], order[k]
        if ends[prev] - (starts[cur] - gap_target) <= tol:
            continue
        code = sq.OVERLAP if ends[prev] - starts[cur] > tol else sq.GAP_TOO_SMALL
        new_end = _floor_ms(starts[cur] - gap_target)
        if new_end - starts[prev] >= floor - _EPS:
            note(prev, code, f"Akhir dipendekkan dari {ends[prev]:.3f} ke {new_end:.3f} detik")
            ends[prev] = new_end
            timing_touched.add(prev)
            continue
        new_start = _ceil_ms(ends[prev] + gap_target)
        if ends[cur] - new_start >= floor - _EPS:
            note(cur, code, f"Awal digeser dari {starts[cur]:.3f} ke {new_start:.3f} detik")
            starts[cur] = new_start
            timing_touched.add(cur)
        else:
            blocked[(cur, code)] = (f"tidak muat tanpa membuat subtitle lebih singkat dari {floor:g} detik; "
                                    "atur ulang waktu secara manual")

    # 3) CPS terlalu tinggi / durasi terlalu singkat: perpanjang akhir ke ruang kosong (hanya bila kebutuhan terpenuhi penuh)
    extend_codes = {sq.CPS_HIGH, sq.CPS_VERY_HIGH, sq.DURATION_TOO_SHORT}
    for k, i in enumerate(order):
        need = float(rules.min_duration) if rules.min_duration is not None else 0.0
        chars = sq.visible_char_count(texts[i])
        need = max(need, chars / float(rules.max_cps))
        duration = ends[i] - starts[i]
        if duration >= need - _EPS:
            continue
        target = _ceil_ms(starts[i] + need + _MARGIN)
        room = starts[order[k + 1]] - gap_target if k + 1 < len(order) else math.inf
        cap_duration = starts[i] + float(rules.max_duration) if rules.max_duration is not None else math.inf
        wanted = {x.code for x in by_index.get(i, [])} & extend_codes or {sq.CPS_HIGH}
        if target <= min(room, cap_duration) + _EPS:
            for code in wanted:
                reasons.setdefault(i, set()).add(code)
            notes.setdefault(i, []).append(f"Akhir diperpanjang dari {ends[i]:.3f} ke {target:.3f} detik")
            ends[i] = target
            timing_touched.add(i)
        else:
            why = ("tidak ada ruang kosong sebelum subtitle berikutnya" if room < target - _EPS
                   else "perpanjangan akan melewati durasi maksimum")
            for code in wanted:
                blocked[(i, code)] = f"{why}; persingkat teks atau atur ulang waktu secara manual"

    # 4) susun perubahan per baris (hanya baris yang benar-benar berubah)
    rows_after: list[Row] = []
    changes: list[FixChange] = []
    for i in range(n):
        orig = Row(*source[i])
        start_after = starts[i] if i in timing_touched else orig.start
        end_after = ends[i] if i in timing_touched else orig.end
        after = Row(start_after, end_after, texts[i])
        rows_after.append(after)
        if after == orig:
            continue
        changes.append(FixChange(i, orig, after, tuple(sorted(reasons.get(i, ()))), tuple(notes.get(i, ())),
                                 i in timing_touched, after.text != orig.text))

    # 5) verifikasi dengan pemeriksa yang sama
    after_report = sq.check_subtitles(rows_after, rules)
    before_keys = {(x.index, x.code) for x in before.issues}
    after_keys = {(x.index, x.code) for x in after_report.issues}
    remaining = [Remaining(x.index, x.code, x.severity, x.message,
                           blocked.get((x.index, x.code)) or _CODE_REASON.get(x.code, REASON_DEFAULT))
                 for x in after_report.issues]
    return FixPlan(rules=rules, source=source, changes=changes, before=before, after=after_report,
                   resolved=sorted(before_keys - after_keys), introduced=sorted(after_keys - before_keys),
                   remaining=remaining, rows_after=tuple(rows_after))
