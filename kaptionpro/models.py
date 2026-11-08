"""Model data (dataclass), indeks segmen, dan SubtitleIO (SRT/ASS)."""

from __future__ import annotations

import bisect
import math
import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

from .config import (
    EXPORT_HEIGHTS,
    LANGUAGE_PRESETS,
    TEXT_STYLES,
    TRANSLATORS,
    WHISPER_MODELS,
    X264_PRESETS,
    log,
)
from .core.utils import (
    _clamp_num,
    ass_alpha_from_percent,
    ass_time,
    escape_ass,
    hex_to_ass_bgr,
    is_valid_hex,
    is_valid_project_id,
    normalize_hex,
    read_text_guess,
    sanitize_ass_field,
    srt_time,
)


@dataclass
class SubtitleStyle:
    font_name: str = "Arial"
    font_size_pct: float = 5.0
    bold: bool = True
    primary_hex: str = "#FFD700"
    secondary_hex: str = "#FFFFFF"
    outline_hex: str = "#000000"
    outline_width: float = 2.0
    shadow: float = 1.0
    margin_v_pct: float = 6.0
    position: str = "bottom"
    back_alpha_pct: float = 60.0

    def alignment_code(self) -> int:
        return {"bottom": 2, "middle": 5, "top": 8}.get(self.position, 2)

    def as_dict(self) -> dict:
        return asdict(self)

    def validated(self) -> "SubtitleStyle":
        """Koersi tipe + batasi rentang nilai (data rusak/diedit manual tidak merusak render ASS)."""
        d = SubtitleStyle()

        def hexv(v, default: str) -> str:
            return normalize_hex(str(v)) if is_valid_hex(str(v)) else default

        return SubtitleStyle(
            font_name=sanitize_ass_field(str(self.font_name or d.font_name))[:80] or d.font_name,
            font_size_pct=_clamp_num(self.font_size_pct, d.font_size_pct, 1.5, 15.0),
            bold=bool(self.bold),
            primary_hex=hexv(self.primary_hex, d.primary_hex),
            secondary_hex=hexv(self.secondary_hex, d.secondary_hex),
            outline_hex=hexv(self.outline_hex, d.outline_hex),
            outline_width=_clamp_num(self.outline_width, d.outline_width, 0.0, 10.0),
            shadow=_clamp_num(self.shadow, d.shadow, 0.0, 10.0),
            margin_v_pct=_clamp_num(self.margin_v_pct, d.margin_v_pct, 0.0, 40.0),
            position=self.position if self.position in ("bottom", "middle", "top") else d.position,
            back_alpha_pct=_clamp_num(self.back_alpha_pct, d.back_alpha_pct, 0.0, 100.0),
        )

    @classmethod
    def from_dict(cls, d: dict | None) -> "SubtitleStyle":
        if not d or not isinstance(d, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        try:
            return cls(**{k: v for k, v in d.items() if k in known}).validated()
        except Exception:  # noqa: BLE001
            return cls()


@dataclass
class Word:
    start: float
    end: float
    text: str

    @classmethod
    def from_dict(cls, d: dict) -> "Word":
        return cls(float(d["start"]), float(d["end"]), str(d["text"]))


@dataclass
class SubtitleSegment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SubtitleSegment":
        return cls(float(d["start"]), float(d["end"]), str(d.get("text", "")),
                   [Word.from_dict(w) for w in d.get("words", [])])

    @property
    def duration(self) -> float:
        return self.end - self.start

    def shift(self, delta: float) -> None:
        self.start += delta
        self.end += delta
        for w in self.words:
            w.start += delta
            w.end += delta

    @staticmethod
    def parse_time(text: str) -> float:
        raw = text.strip().replace(",", ".")
        try:
            if not raw:
                raise ValueError
            parts = raw.split(":")
            if len(parts) > 3:
                raise ValueError
            seconds = 0.0
            for p in parts:
                seconds = seconds * 60 + float(p)
            if seconds < 0 or not math.isfinite(seconds):
                raise ValueError
            return seconds
        except ValueError:
            raise ValueError(f"Format waktu tidak valid: '{text}'") from None

    def karaoke_words(self) -> list[Word]:
        tokens = self.text.split()
        if not tokens:
            return []
        if self.words:
            src = [w for w in self.words if w.text.strip()]
            if [w.text.strip() for w in src] == tokens:
                clean = sanitize_word_list(src, self.start, self.end)
                if clean is not None:               # renderer HANYA memakai timestamp kata yang valid
                    return clean
        weights = [max(1, len(t)) for t in tokens]
        total = float(sum(weights))
        result, acc = [], 0.0
        for tok, wt in zip(tokens, weights):
            t0 = self.start + self.duration * acc / total
            acc += wt
            t1 = self.start + self.duration * acc / total
            result.append(Word(t0, t1, tok))
        return result

    def to_ass_text(self, karaoke: bool) -> str:
        if not karaoke:
            return escape_ass(self.text)
        words = self.karaoke_words()
        if not words:
            return escape_ass(self.text)
        parts, cursor = [], self.start
        for w in words:
            gap_cs = int(round((w.start - cursor) * 100))
            if gap_cs > 0:
                parts.append(f"{{\\k{gap_cs}}}")
            dur_cs = max(1, int(round((w.end - w.start) * 100)))
            parts.append(f"{{\\k{dur_cs}}}{escape_ass(w.text.strip())} ")
            cursor = max(cursor, w.end)
        return "".join(parts).strip()

    def to_ass_dialogue(self, style_name: str, karaoke: bool) -> str:
        return (f"Dialogue: 0,{ass_time(self.start)},{ass_time(self.end)},{style_name},,0,0,0,,"
                f"{self.to_ass_text(karaoke)}")

    def to_srt_block(self, index: int) -> str:
        body = "\n".join(line for line in self.text.splitlines() if line.strip())
        return f"{index}\n{srt_time(self.start)} --> {srt_time(self.end)}\n{body}\n"


def sanitize_word_list(words: list["Word"], seg_start: float, seg_end: float, tol: float = 0.75,
                       min_dur: float = 0.05, max_overlap: float = 0.1) -> list["Word"] | None:
    """
    Validasi + normalisasi timestamp kata untuk karaoke. Mengembalikan SALINAN yang bersih, atau None
    bila data tidak bisa dipercaya (pemanggil lalu memakai pembagian proporsional dari teks).
      - harus terbatas (bukan NaN/inf) dan berteks
      - harus berada dalam rentang segmen: meleset <= tol dijepit ke batas segmen, lebih jauh = tidak valid
      - harus urut menurut waktu mulai
      - durasi nol/negatif diperbaiki ke min_dur (bila tidak muat dalam segmen = tidak valid)
      - tumpang tindih kecil antar kata dipotong; tumpang tindih besar = tidak valid
    """
    out: list[Word] = []
    for w in words:
        try:
            s, e = float(w.start), float(w.end)
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(s) and math.isfinite(e)) or not str(w.text).strip():
            return None
        if s < seg_start - tol or e > seg_end + tol or e < seg_start - tol or s > seg_end + tol:
            return None                                  # di luar durasi segmen
        s, e = max(s, seg_start), min(e, seg_end)
        if e <= s:
            e = min(seg_end, s + min_dur)                # durasi nol -> perbaiki
            if e <= s:
                return None
        if out:
            prev = out[-1]
            if s < prev.start - 1e-6:
                return None                              # urutan terbalik
            if s < prev.end:
                if prev.end - s > max_overlap:
                    return None
                prev.end = s                             # potong overlap kecil
                if prev.end <= prev.start:
                    return None
        out.append(Word(s, e, str(w.text)))
    return out


def retime_words(text: str, src_words: list["Word"], old_start: float, old_end: float,
                 new_start: float, new_end: float) -> list["Word"]:
    """
    Sinkronisasi ulang kata (karaoke) setelah teks diterjemahkan / diedit.

    Alih-alih membagi durasi segmen secara proporsional (jeda ucapan diabaikan), fungsi ini
    memakai "amplop" waktu bicara dari kata-kata sumber (Whisper): waktu bicara kumulatif dipetakan
    ke posisi karakter kata-kata baru, sehingga jeda di tengah kalimat tetap berada di tempat yang
    sama. Hasil lalu diskalakan ke [new_start, new_end] bila segmen digeser/diregangkan.
    Mengembalikan [] bila tidak ada data sumber (pemanggil lalu memakai pembagian proporsional).
    """
    tokens = text.split()
    src = [w for w in src_words if w.text.strip() and w.end > w.start
           and math.isfinite(w.start) and math.isfinite(w.end)]
    if not tokens or not src:
        return []
    cum = [0.0]
    for w in src:
        cum.append(cum[-1] + (w.end - w.start))
    total = cum[-1]
    if total <= 0:
        return []
    weights = [max(1, len(t)) for t in tokens]
    wsum = float(sum(weights))
    old_span = old_end - old_start

    def speech_to_time(p: float, side: str) -> float:
        if side == "start":                      # di batas kata: mulai SETELAH jeda
            i = bisect.bisect_right(cum, p) - 1
        else:                                    # di batas kata: berakhir SEBELUM jeda
            i = bisect.bisect_left(cum, p) - 1
        i = min(max(i, 0), len(src) - 1)
        w = src[i]
        return w.start + min(max(p - cum[i], 0.0), w.end - w.start)

    def to_new(t: float) -> float:
        u = (t - old_start) / old_span if old_span > 0 else 0.0
        return min(max(new_start + u * (new_end - new_start), new_start), new_end)

    words: list[Word] = []
    acc, prev_end = 0.0, new_start
    for tok, wt in zip(tokens, weights):
        p0 = acc / wsum * total
        acc += wt
        p1 = acc / wsum * total
        t0 = max(to_new(speech_to_time(p0, "start")), prev_end)
        t1 = max(to_new(speech_to_time(p1, "end")), t0)
        words.append(Word(t0, t1, tok))
        prev_end = t1
    return words


class SegmentIndex:
    """
    Indeks pencarian subtitle aktif dengan binary search (bisect) — O(log n) per pencarian
    (sebelumnya linear). Overlap kecil ditangani dengan memeriksa beberapa kandidat ke belakang.
    """

    def __init__(self) -> None:
        self.segments: list[SubtitleSegment] = []
        self.starts: list[float] = []
        self.max_len = 0.0

    def rebuild(self, segments: list["SubtitleSegment"]) -> None:
        ordered = segments
        if any(segments[i].start > segments[i + 1].start for i in range(len(segments) - 1)):
            ordered = sorted(segments, key=lambda s: s.start)       # indeks selalu bekerja pada data terurut
        self.segments = ordered
        self.starts = [s.start for s in ordered]
        self.max_len = max((s.end - s.start for s in ordered), default=0.0)

    def at(self, t: float) -> "SubtitleSegment | None":
        i = bisect.bisect_right(self.starts, t) - 1
        while i >= 0 and self.starts[i] >= t - self.max_len - 1e-9:
            seg = self.segments[i]
            if seg.start <= t <= seg.end:
                return seg
            i -= 1
        return None

    def next_after(self, t: float) -> "SubtitleSegment | None":
        i = bisect.bisect_right(self.starts, t + 1e-6)
        return self.segments[i] if i < len(self.segments) else None

    def prev_before(self, t: float) -> "SubtitleSegment | None":
        i = bisect.bisect_left(self.starts, t - 1e-6) - 1
        return self.segments[i] if i >= 0 else None


@dataclass
class LoadReport:
    """Ringkasan perbaikan otomatis saat subtitle dimuat/diimpor."""
    dropped: int = 0
    fixed: int = 0
    sorted: bool = False

    def notes(self) -> list[str]:
        out = []
        if self.dropped:
            out.append(f"{self.dropped} baris rusak (waktu tidak valid) dibuang")
        if self.fixed:
            out.append(f"{self.fixed} baris dikoreksi (waktu negatif/terbalik/kata rusak)")
        if self.sorted:
            out.append("urutan baris diperbaiki (diurutkan berdasarkan waktu)")
        return out


@dataclass
class VideoInfo:
    width: int = 1920
    height: int = 1080
    duration: float = 0.0
    fps: float = 0.0
    dims_known: bool = True        # False bila width/height hanya tebakan default (belum diukur)


@dataclass
class Project:
    name: str
    video_path: str = ""
    preset_key: str = "kdrama"
    style_key: str = "karaoke"
    line_count: int = 0
    updated_at: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    whisper_model: str = "small"
    style: dict = field(default_factory=dict)
    translator: str = "google"
    export: dict = field(default_factory=dict)         # ExportOptions terakhir untuk project ini

    def __post_init__(self) -> None:
        if not is_valid_project_id(self.id):      # ID membentuk path file -> format ketat, tidak pernah 'dibersihkan'
            raise ValueError(f"ID project tidak valid: {self.id!r}")
        if self.preset_key not in LANGUAGE_PRESETS:
            self.preset_key = "kdrama"
        if self.style_key not in TEXT_STYLES:
            self.style_key = "karaoke"
        if self.whisper_model not in WHISPER_MODELS:
            self.whisper_model = "small"
        if self.translator not in TRANSLATORS:
            self.translator = "google"
        if not isinstance(self.style, dict):
            self.style = {}
        if not isinstance(self.export, dict):
            self.export = {}

    def subtitle_style(self) -> SubtitleStyle:
        return SubtitleStyle.from_dict(self.style)

    def set_subtitle_style(self, style: SubtitleStyle) -> None:
        self.style = style.validated().as_dict()

    def export_options(self) -> "ExportOptions":
        return ExportOptions.from_dict(self.export)

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat(timespec="seconds")

    @property
    def video_exists(self) -> bool:
        return bool(self.video_path) and Path(self.video_path).exists()


_SRT_TIME = re.compile(r"(\d+:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d+:\d{2}:\d{2}[,.]\d{1,3})")

BLOCKING_ISSUES = {"empty", "bad", "overlap"}


class SubtitleIO:

    @staticmethod
    def sorted_copy(segments: list[SubtitleSegment]) -> list[SubtitleSegment]:
        return sorted(segments, key=lambda s: s.start)

    @staticmethod
    def find_issues(segments: list[SubtitleSegment]) -> dict[int, str]:
        """
        Peta index -> 'empty' | 'bad' | 'overlap' | 'short' | 'long'.
        TIDAK bergantung pada urutan data: bila list tidak terurut, evaluasi dilakukan pada urutan waktu
        lalu hasilnya dipetakan kembali ke index asli.
        """
        n = len(segments)
        order = list(range(n))
        if any(segments[i].start > segments[i + 1].start for i in range(n - 1)):
            order = sorted(range(n), key=lambda i: (segments[i].start, i))
        issues: dict[int, str] = {}
        for pos, i in enumerate(order):
            seg = segments[i]
            if not seg.text.strip():
                issues[i] = "empty"
                continue
            if not (math.isfinite(seg.start) and math.isfinite(seg.end)) or seg.end <= seg.start:
                issues[i] = "bad"
                continue
            if seg.duration < 0.30:
                issues[i] = "short"
            elif seg.duration > 12.0:
                issues[i] = "long"
            if pos + 1 < n and segments[order[pos + 1]].start < seg.end - 0.001:
                issues[i] = "overlap"
        return issues

    @staticmethod
    def normalize(segments: list[SubtitleSegment]) -> tuple[list[SubtitleSegment], LoadReport]:
        """
        Validasi timestamp + urutkan. Dipakai saat memuat project, impor SRT, dan hasil AI.
        - NaN/inf atau teks bukan string  -> baris dibuang
        - waktu negatif                   -> dijadikan 0
        - end < start                     -> ditukar
        - timestamp kata rusak            -> words dikosongkan (karaoke dihitung ulang dari teks)
        - hasil selalu terurut menurut (start, end)
        """
        rep = LoadReport()
        kept: list[SubtitleSegment] = []
        for s in segments:
            if not isinstance(s.text, str) or not (math.isfinite(s.start) and math.isfinite(s.end)):
                rep.dropped += 1
                continue
            if s.start < 0 or s.end < 0:
                s.start, s.end = max(0.0, s.start), max(0.0, s.end)
                rep.fixed += 1
            if s.end < s.start:
                s.start, s.end = s.end, s.start
                s.words = []
                rep.fixed += 1
            if s.words:
                clean = sanitize_word_list(s.words, s.start, s.end)
                if clean is None:
                    s.words = []                     # tak bisa dipercaya -> karaoke dihitung ulang dari teks
                    rep.fixed += 1
                elif any(abs(a.start - b.start) > 1e-9 or abs(a.end - b.end) > 1e-9 for a, b in zip(clean, s.words)):
                    s.words = clean                  # dikoreksi (dijepit / durasi nol / overlap kecil)
                    rep.fixed += 1
            kept.append(s)
        ordered = sorted(kept, key=lambda s: (s.start, s.end))
        if any(a is not b for a, b in zip(kept, ordered)):
            rep.sorted = True
        return ordered, rep

    @staticmethod
    def integrity_report(segments: list[SubtitleSegment]) -> dict:
        """Hitung masalah integritas (untuk indikator status setelah memuat project)."""
        issues = SubtitleIO.find_issues(segments)
        kinds = list(issues.values())
        seen, dup = set(), 0
        for s in segments:
            key = (round(s.start, 2), s.text.strip())
            dup += key in seen
            seen.add(key)
        return {
            "lines": len(segments),
            "blocking": sum(1 for k in kinds if k in BLOCKING_ISSUES),
            "overlap": kinds.count("overlap"), "short": kinds.count("short"), "long": kinds.count("long"),
            "empty": kinds.count("empty"), "bad": kinds.count("bad"),
            "unsorted": any(segments[i].start > segments[i + 1].start for i in range(len(segments) - 1)),
            "duplicates": dup,
        }

    @staticmethod
    def bulk_edit(segments: list[SubtitleSegment], indices: list[int], op: str, p1: str = "", p2: str = "",
                  min_gap: float = 0.04) -> tuple[int, str]:
        """
        Operasi massal pada baris `indices` (index ke `segments`). Return (jumlah_berubah, pesan_error).
        Error != "" berarti TIDAK ada perubahan yang diterapkan. Perubahan teks memetakan ulang word
        timestamp (retime) sehingga karaoke tetap akurat.
        """
        idx = sorted({i for i in indices if 0 <= i < len(segments)})
        targets = [segments[i] for i in idx]
        changed = 0

        def set_text(s: SubtitleSegment, new: str) -> bool:
            if new == s.text:
                return False
            if s.words:
                s.words = retime_words(new, s.words, s.start, s.end, s.start, s.end)
            s.text = new
            return True

        if op == "shift":
            try:
                delta = float(p1.replace(",", "."))
            except ValueError:
                return 0, f"Angka tidak valid: {p1!r}"
            if not math.isfinite(delta):
                return 0, f"Angka tidak valid: {p1!r}"
            if targets and min(s.start for s in targets) + delta < 0:
                return 0, "Pergeseran membuat waktu mulai menjadi negatif."
            if delta != 0:
                for s in targets:
                    s.shift(delta)
                    changed += 1
        elif op == "replace":
            if not p1:
                return 0, "Isi kolom 'cari' terlebih dahulu."
            for s in targets:
                changed += set_text(s, s.text.replace(p1, p2))
        elif op == "spaces":
            for s in targets:
                changed += set_text(s, "\n".join(" ".join(line.split()) for line in s.text.splitlines() if line.strip()))
        elif op == "upper":
            for s in targets:
                changed += set_text(s, s.text.upper())
        elif op == "lower":
            for s in targets:
                changed += set_text(s, s.text.lower())
        elif op == "title":
            for s in targets:
                changed += set_text(s, re.sub(r"\S+", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), s.text))
        elif op == "striptags":
            for s in targets:
                new = re.sub(r"</?(?:i|b|u|font)[^>]*>", "", s.text, flags=re.IGNORECASE)
                new = re.sub(r"\{\\[^}]*\}", "", new)
                changed += set_text(s, new)
        elif op == "mindur":
            try:
                min_dur = float(p1.replace(",", "."))
            except ValueError:
                return 0, f"Angka tidak valid: {p1!r}"
            if not math.isfinite(min_dur) or min_dur <= 0:
                return 0, "Durasi minimum harus lebih dari 0."
            for i in idx:
                s = segments[i]
                if s.duration >= min_dur:
                    continue
                limit = segments[i + 1].start - min_gap if i + 1 < len(segments) else float("inf")
                new_end = min(s.start + min_dur, limit)
                if new_end > s.end + 1e-9:
                    s.end = new_end                     # hanya memperpanjang: word timestamp tetap valid
                    changed += 1
        else:
            return 0, f"Operasi tidak dikenal: {op}"
        return changed, ""

    @staticmethod
    def count_blocking(segments: list[SubtitleSegment]) -> int:
        return sum(1 for kind in SubtitleIO.find_issues(segments).values() if kind in BLOCKING_ISSUES)

    @staticmethod
    def _split_long(seg: SubtitleSegment, max_dur: float) -> list[SubtitleSegment]:
        """
        Pecah baris yang lebih panjang dari max_dur pada batas kata, REKURSIF sampai semua bagian
        <= max_dur (atau tinggal satu kata). Memakai timestamp kata asli bila masih cocok dengan
        teks; selain itu dibagi proporsional per panjang kata.
        """
        tokens = seg.text.split()
        if seg.duration <= max_dur or len(tokens) < 2:
            return [seg]
        words = seg.karaoke_words()
        has_real = bool(seg.words) and [w.text.strip() for w in seg.words if w.text.strip()] == tokens
        mid_t = seg.start + seg.duration / 2.0
        best_i, best_score = 1, None
        for i in range(1, len(words)):
            gap = max(0.0, words[i].start - words[i - 1].end)
            boundary = (words[i - 1].end + words[i].start) / 2.0
            score = abs(boundary - mid_t) - min(gap, 1.0) * 1.5     # dekat tengah + bonus jeda ucapan
            if best_score is None or score < best_score:
                best_i, best_score = i, score
        left_w, right_w = words[:best_i], words[best_i:]
        left_end = left_w[-1].end
        right_start = max(right_w[0].start, left_end)
        left_end = min(right_start, left_end + 0.2)                  # tahan sebentar agar tidak berkedip
        left = SubtitleSegment(seg.start, left_end, " ".join(tokens[:best_i]),
                               [Word(w.start, w.end, w.text) for w in left_w] if has_real else [])
        right = SubtitleSegment(right_start, seg.end, " ".join(tokens[best_i:]),
                                [Word(w.start, w.end, w.text) for w in right_w] if has_real else [])
        return SubtitleIO._split_long(left, max_dur) + SubtitleIO._split_long(right, max_dur)

    @staticmethod
    def _overlap_pass(fixed: list[SubtitleSegment], min_gap: float, min_dur: float,
                      allow_push: bool, max_push: float) -> tuple[int, bool]:
        """
        Satu lintasan pada list yang sudah terurut. Return (jumlah_perubahan, perlu_ulang).
        - Overlap biasa: baris sebelumnya dipendekkan (awal tiap baris tetap = sinkron dengan ucapan).
        - Dua baris mulai hampir bersamaan: yang LEBIH PENDEK digeser ke belakang dengan durasi
          dijaga (tidak memangkas baris panjang yang sehat). Urutan bisa berubah -> perlu_ulang.
        - allow_push=False: hanya memendekkan (mode jaminan akhir; selalu konvergen).
        """
        changes = 0
        for i in range(len(fixed) - 1):
            cur, nxt = fixed[i], fixed[i + 1]
            limit = nxt.start - min_gap
            if cur.end <= limit + 1e-9:
                continue
            changes += 1
            if limit >= cur.start + min_dur:
                cur.end = limit
                if cur.words and limit < cur.words[-1].start:
                    cur.words = []                       # buang timestamp HANYA bila pemangkasan memotong kata
                continue
            shift = cur.end + min_gap - nxt.start
            if allow_push and nxt.duration <= cur.duration and shift <= max_push:
                dur = max(min_dur, nxt.duration)
                nxt.start = cur.end + min_gap
                nxt.end = nxt.start + dur
                nxt.words = []
                return changes, True
            cur.end = cur.start + min_dur                # fallback: pendekkan cur, majukan awal nxt
            cur.words = []
            nxt.start = cur.end + min_gap
            if nxt.end < nxt.start + min_dur:
                nxt.end = nxt.start + min_dur
            nxt.words = []
        return changes, False

    @staticmethod
    def _fix_overlaps(fixed: list[SubtitleSegment], min_gap: float, min_dur: float,
                      max_push: float) -> int:
        """Hilangkan semua overlap in-place. Urutan asli dipakai sebagai tie-break saat sort ulang."""
        order = {id(s): k for k, s in enumerate(fixed)}

        def key(s: SubtitleSegment) -> tuple[float, int]:
            return (s.start, order[id(s)])

        changes = 0
        for _ in range(10 * (len(fixed) + 1)):
            fixed.sort(key=key)
            c, again = SubtitleIO._overlap_pass(fixed, min_gap, min_dur, True, max_push)
            changes += c
            if not again:
                break
        fixed.sort(key=key)
        c, _ = SubtitleIO._overlap_pass(fixed, min_gap, min_dur, False, max_push)   # jaminan akhir
        return changes + c

    @staticmethod
    def auto_fix(segments: list[SubtitleSegment], min_gap: float = 0.04,
                 min_dur: float = 0.40, max_dur: float = 8.0,
                 max_push: float = 12.0) -> tuple[list[SubtitleSegment], int]:
        """
        Perbaikan konservatif (hasil dijamin tanpa baris kosong / end<=start / overlap):
          - buang baris kosong
          - pecah baris >max_dur pada batas kata (rekursif, bukan dipotong)
          - betulkan end<=start dan durasi terlalu pendek
          - hilangkan overlap: pendekkan baris sebelumnya; bila tidak muat, majukan awal baris berikutnya
        Word timestamp hanya dibuang pada baris yang BENAR-BENAR berubah.
        """
        fixed = [SubtitleSegment(s.start, s.end, s.text, [Word(w.start, w.end, w.text) for w in s.words])
                 for s in segments if s.text.strip()]
        fixed.sort(key=lambda s: s.start)
        changes = len(segments) - len(fixed)

        # 1) Pecah baris panjang (rekursif)
        out: list[SubtitleSegment] = []
        for s in fixed:
            parts = SubtitleIO._split_long(s, max_dur)
            changes += len(parts) - 1
            out.extend(parts)
        fixed = out

        # 2) end<=start / terlalu pendek
        for s in fixed:
            if s.end <= s.start:
                s.end = s.start + max(min_dur, 1.0)
                s.words = []
                changes += 1
            elif s.duration < min_dur:
                s.end = s.start + min_dur
                s.words = []
                changes += 1

        # 3) Overlap (dijamin hilang)
        changes += SubtitleIO._fix_overlaps(fixed, min_gap, min_dur, max_push)

        return fixed, changes

    # ---------- SRT ----------
    @staticmethod
    def write_srt(segments: list[SubtitleSegment], path: str) -> None:
        blocks = [s.to_srt_block(i) for i, s in enumerate(SubtitleIO.sorted_copy(segments), start=1)]
        Path(path).write_text("\n".join(blocks), encoding="utf-8")
        log.info("SRT saved: %s (%d blocks)", path, len(blocks))

    @staticmethod
    def read_srt(path: str) -> list[SubtitleSegment]:
        content = read_text_guess(path).replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
        segments: list[SubtitleSegment] = []
        for block in re.split(r"\n\s*\n", content.strip()):
            lines = block.split("\n")
            for i, line in enumerate(lines):
                m = _SRT_TIME.search(line)
                if not m:
                    continue
                text = "\n".join(lines[i + 1:]).strip()
                try:
                    start = SubtitleSegment.parse_time(m.group(1))
                    end = SubtitleSegment.parse_time(m.group(2))
                except ValueError:
                    break
                if text:
                    segments.append(SubtitleSegment(start, end, text))
                break
        return SubtitleIO.normalize(segments)[0]          # validasi timestamp + urut waktu

    # ---------- ASS ----------
    @staticmethod
    def build_ass(segments: list[SubtitleSegment], style_key: str, style: SubtitleStyle,
                  width: int = 1920, height: int = 1080) -> str:
        width, height = max(320, int(width)), max(240, int(height))
        fs = max(16, int(height * style.font_size_pct / 100.0))
        margin_v = int(height * style.margin_v_pct / 100.0)
        margin_h = int(width * 0.05)
        outline = max(0, int(round(style.outline_width * fs / 20)))
        shadow = max(0, int(round(style.shadow * fs / 20)))
        align = style.alignment_code()
        is_karaoke = style_key == "karaoke"
        style_name = "Karaoke" if is_karaoke else "Minimal"

        # Font family: buang koma (kolom ASS) — penting untuk nama seperti "Arial Black, Bold".
        font_field = sanitize_ass_field(style.font_name)

        primary = hex_to_ass_bgr(style.primary_hex)
        secondary = hex_to_ass_bgr(style.secondary_hex)
        outline_col = hex_to_ass_bgr(style.outline_hex)
        box_col = hex_to_ass_bgr("#000000", ass_alpha_from_percent(style.back_alpha_pct))
        bold = -1 if style.bold else 0

        header = [
            "[Script Info]",
            "Title: KaptionPro Export",
            "ScriptType: v4.00+",
            "WrapStyle: 0",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
            "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Karaoke,{font_field},{fs},{primary},{secondary},{outline_col},&H64000000,{bold},0,0,0,"
            f"100,100,0,0,1,{outline},{shadow},{align},{margin_h},{margin_h},{margin_v},1",
            f"Style: Minimal,{font_field},{fs},{secondary},{secondary},{outline_col},{box_col},0,0,0,0,"
            f"100,100,0,0,3,{max(1, outline)},{shadow},{align},{margin_h},{margin_h},{margin_v},1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
        events = [s.to_ass_dialogue(style_name, is_karaoke) for s in SubtitleIO.sorted_copy(segments)]
        return "\n".join(header + events) + "\n"

    @staticmethod
    def write_ass(segments: list[SubtitleSegment], path: str, style_key: str,
                  style: SubtitleStyle, width: int = 1920, height: int = 1080) -> None:
        Path(path).write_text(SubtitleIO.build_ass(segments, style_key, style, width, height),
                              encoding="utf-8")
        log.info("ASS saved: %s (style=%s)", path, style_key)


@dataclass
class ExportOptions:
    """Opsi export MP4: preset kualitas, CRF x264, preset encoding, dan mode audio."""
    quality: str = "original"
    crf: int = 20
    preset: str = "medium"
    audio: str = "auto"                  # auto (copy bila kompatibel MP4, selain itu AAC) | copy | aac

    def validated(self) -> "ExportOptions":
        return ExportOptions(
            quality=self.quality if self.quality in EXPORT_HEIGHTS else "original",
            crf=int(_clamp_num(self.crf, 20, 14, 30)),
            preset=self.preset if self.preset in X264_PRESETS else "medium",
            audio=self.audio if self.audio in ("auto", "copy", "aac") else "auto",
        )

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "ExportOptions":
        if not d or not isinstance(d, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        try:
            return cls(**{k: v for k, v in d.items() if k in known}).validated()
        except Exception:  # noqa: BLE001
            return cls()
