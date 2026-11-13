"""
Integritas word timestamp (v1.9.20): setelah timing sebuah baris berubah, tidak boleh ada kata (karaoke) yang tertinggal
di luar rentang baris itu.

MURNI Python (tanpa Tk). Dua fungsi inti:
  find_word_violations(segments)   -> daftar pelanggaran (tidak mengubah apa pun)
  enforce_word_integrity(segments) -> pulihkan IN-PLACE memakai aturan yang sama dengan saat memuat project
                                      (sanitize_word_list: meleset <= 0,75 dtk dijepit ke batas baris, lebih jauh atau tidak
                                      masuk akal = timestamp kata DIBUANG, karaoke dihitung ulang dari teks), lalu jaring
                                      pengaman: sisa pelanggaran apa pun membuat words baris itu dikosongkan.
Hanya baris yang MELANGGAR yang disentuh; baris sehat tidak diubah sama sekali (objek Word-nya tetap).
Editor memanggilnya di dalam transaksi sebelum tampilan/penyimpanan diberi tahu, jadi hasil yang terlihat dan tersimpan
selalu memenuhi invarian ini.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from ..models import sanitize_word_list

EPS = 1e-6                                   # toleransi pembulatan (waktu disimpan 3 desimal, float punya derau)

NON_FINITE = "non_finite"                    # NaN / inf / bukan angka
REVERSED = "reversed"                        # akhir kata < awal kata
OUT_OF_RANGE = "out_of_range"                # kata keluar dari [awal, akhir] baris
DISORDER = "disorder"                        # awal kata lebih dulu dari kata sebelumnya
OVERLAP = "overlap"                          # kata menimpa kata sebelumnya


class WordIntegrityError(RuntimeError):
    """Pelanggaran tersisa setelah pemulihan (tidak seharusnya terjadi); penerapan dibatalkan (rollback)."""


@dataclass(frozen=True)
class WordViolation:
    index: int                               # indeks baris pada daftar yang diperiksa
    word_index: int                          # indeks kata dalam baris
    kind: str
    detail: str


@dataclass(frozen=True)
class WordRepair:
    dropped: tuple[int, ...] = ()            # baris yang words-nya dibuang (karaoke dihitung ulang dari teks)
    clamped: tuple[int, ...] = ()            # baris yang words-nya dijepit/dirapikan (masih punya timestamp kata)

    @property
    def total(self) -> int:
        return len(self.dropped) + len(self.clamped)


def _segment_violations(index: int, seg: object, eps: float) -> list[WordViolation]:
    words = getattr(seg, "words", None) or ()
    if not words:
        return []
    try:
        lo, hi = float(seg.start), float(seg.end)
    except (TypeError, ValueError, OverflowError):
        lo = hi = math.nan                   # waktu baris sendiri rusak: itu urusan normalize; di sini hanya periksa kata
    check_range = math.isfinite(lo) and math.isfinite(hi)
    out: list[WordViolation] = []
    prev_start = prev_end = None
    for k, word in enumerate(words):
        try:
            s, e = float(word.start), float(word.end)
        except (TypeError, ValueError, OverflowError, AttributeError):
            out.append(WordViolation(index, k, NON_FINITE, f"kata {k}: waktu bukan angka atau terlalu besar"))
            continue
        if not (math.isfinite(s) and math.isfinite(e)):
            out.append(WordViolation(index, k, NON_FINITE, f"kata {k}: waktu tidak terbatas (NaN/inf)"))
            continue
        label = str(getattr(word, "text", ""))
        if e < s - eps:
            out.append(WordViolation(index, k, REVERSED, f"kata {k} '{label}': akhir {e:.3f} < awal {s:.3f}"))
        if check_range and (s < lo - eps or e > hi + eps):
            out.append(WordViolation(index, k, OUT_OF_RANGE,
                                     f"kata {k} '{label}' {s:.3f}-{e:.3f} di luar baris {lo:.3f}-{hi:.3f}"))
        if prev_start is not None and prev_end is not None:
            if s < prev_start - eps:
                out.append(WordViolation(index, k, DISORDER, f"kata {k} '{label}' mulai sebelum kata sebelumnya"))
            elif s < prev_end - eps:
                out.append(WordViolation(index, k, OVERLAP, f"kata {k} '{label}' menimpa kata sebelumnya"))
        prev_start, prev_end = s, e
    return out


def find_word_violations(segments: Iterable[object], eps: float = EPS) -> list[WordViolation]:
    """Semua pelanggaran word timestamp pada `segments` (daftar kosong = bersih). Tidak mengubah apa pun."""
    found: list[WordViolation] = []
    for index, seg in enumerate(segments):
        found.extend(_segment_violations(index, seg, eps))
    return found


def enforce_word_integrity(segments: Sequence[object], eps: float = EPS) -> WordRepair:
    """Pulihkan IN-PLACE; setelah kembali, find_word_violations(segments) dijamin kosong."""
    dropped: list[int] = []
    clamped: list[int] = []
    for index, seg in enumerate(segments):
        if not _segment_violations(index, seg, eps):
            continue
        try:
            clean = sanitize_word_list(seg.words, seg.start, seg.end)
        except (AttributeError, TypeError, ValueError, OverflowError):
            clean = None                                        # isi kata bukan Word yang utuh: tidak bisa dipercaya
        if clean is None:
            seg.words = []
            dropped.append(index)
        else:
            seg.words = clean
            clamped.append(index)
    for violation in find_word_violations(segments, eps):       # jaring pengaman: sisa pelanggaran -> buang words baris itu
        seg = segments[violation.index]
        if seg.words:
            seg.words = []
        if violation.index in clamped:
            clamped.remove(violation.index)
        if violation.index not in dropped:
            dropped.append(violation.index)
    return WordRepair(tuple(sorted(dropped)), tuple(sorted(clamped)))
