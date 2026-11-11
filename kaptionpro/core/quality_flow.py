"""
Alur aman perbaikan kualitas subtitle (v1.9.18): Analyze -> Preview Changes -> Confirm -> Apply -> Undo.

MURNI Python (tanpa Tk): UI hanya memanggil metode di sini dan menampilkan hasilnya, sehingga URUTAN LANGKAH dijaga oleh
kode yang diuji, bukan oleh urutan tombol. Aturannya:
  * preview() hanya setelah analyze();            confirm() hanya setelah preview() yang punya perubahan;
  * apply() hanya setelah confirm() dan hanya SEKALI per konfirmasi (konfirmasi habis dipakai);
  * apply() menolak (StalePlanError) bila subtitle sudah berubah sejak pratinjau dibuat: tidak pernah menerapkan rencana basi;
  * undo() hanya setelah apply() dan diverifikasi: subtitle harus kembali PERSIS seperti sebelum apply.
Cara menerapkan dan membatalkan (backup, riwayat Undo editor) disuntikkan sebagai fungsi, jadi modul ini tidak tahu soal GUI.
"""

from __future__ import annotations

from typing import Callable, Iterable

from . import quality_fix as qf
from . import subtitle_quality as sq

IDLE, ANALYZED, PREVIEW, CONFIRMED, APPLIED, UNDONE = "idle", "analyzed", "preview", "confirmed", "applied", "undone"


class FlowError(RuntimeError):
    """Langkah dipanggil di luar urutan yang diizinkan."""


class StalePlanError(FlowError):
    """Subtitle berubah sejak pratinjau dibuat; rencana tidak boleh diterapkan."""


class QualityFlow:
    def __init__(self, rules: sq.QualityRules | None = None) -> None:
        self.rules = rules or sq.DEFAULT_RULES
        self.stage = IDLE
        self.report: sq.QualityReport | None = None
        self.plan: qf.FixPlan | None = None
        self.applied_count = 0
        self.history: list[str] = [IDLE]

    def _go(self, stage: str) -> None:
        self.stage = stage
        self.history.append(stage)

    def _require(self, *stages: str) -> None:
        if self.stage not in stages:
            raise FlowError(f"langkah ini hanya boleh dari tahap {'/'.join(stages)}, bukan '{self.stage}'")

    # 1) Analyze --------------------------------------------------------------------------------------------------------
    def analyze(self, segments: Iterable[object], rules: sq.QualityRules | None = None) -> sq.QualityReport:
        """Boleh dipanggil kapan saja (mis. 'Analisis ulang' atau ganti aturan); membuang rencana/konfirmasi lama."""
        if rules is not None:
            self.rules = rules
        self.report = sq.check_subtitles(segments, self.rules)
        self.plan = None
        self.applied_count = 0
        self._go(ANALYZED)
        return self.report

    # 2) Preview --------------------------------------------------------------------------------------------------------
    def preview(self, segments: Iterable[object]) -> qf.FixPlan:
        """Susun rencana dari kondisi TERKINI. Tanpa perubahan -> tetap di tahap ANALYZED (tidak ada yang bisa dikonfirmasi)."""
        self._require(ANALYZED)
        plan = qf.plan_fixes(segments, self.rules)
        self.plan = plan
        if plan.has_changes:
            self._go(PREVIEW)
        return plan

    def cancel_preview(self) -> None:
        """Kembali dari pratinjau/konfirmasi ke hasil analisis (tidak mengubah apa pun)."""
        self._require(PREVIEW, CONFIRMED)
        self.plan = None
        self._go(ANALYZED)

    # 3) Confirm --------------------------------------------------------------------------------------------------------
    def confirm(self) -> None:
        self._require(PREVIEW)
        if self.plan is None or not self.plan.has_changes:
            raise FlowError("tidak ada perubahan untuk dikonfirmasi")
        self._go(CONFIRMED)

    # 4) Apply ----------------------------------------------------------------------------------------------------------
    def apply(self, segments: Iterable[object], apply_fn: Callable[[qf.FixPlan], object]) -> int:
        """
        Terapkan rencana lewat `apply_fn(plan)` (UI: backup + satu langkah Undo). Mengembalikan jumlah baris yang diubah.
        Konfirmasi habis dipakai; bila apply_fn gagal, tahap kembali ke ANALYZED dan galat diteruskan (tidak setengah jadi
        dari sisi alur; tanggung jawab apply_fn untuk tidak mengubah data bila gagal).
        """
        self._require(CONFIRMED)
        plan = self.plan
        assert plan is not None
        if not plan.matches(segments):
            self.plan = None
            self._go(ANALYZED)
            raise StalePlanError("subtitle berubah sejak pratinjau; analisis ulang dulu")
        try:
            apply_fn(plan)
        except Exception:
            self.plan = None
            self._go(ANALYZED)
            raise
        self.applied_count = len(plan.changes)
        self._go(APPLIED)
        return self.applied_count

    # 5) Undo -----------------------------------------------------------------------------------------------------------
    @property
    def can_undo(self) -> bool:
        return self.stage == APPLIED and self.plan is not None

    def undo(self, get_segments: Callable[[], Iterable[object]], undo_fn: Callable[[], object]) -> bool:
        """
        Batalkan lewat `undo_fn()` (UI: editor.undo). True bila subtitle kembali PERSIS seperti sebelum apply; False bila
        tidak (mis. riwayat Undo habis); tahap kembali ke ANALYZED agar pengguna menganalisis ulang kondisi sebenarnya.
        """
        self._require(APPLIED)
        plan = self.plan
        assert plan is not None
        undo_fn()
        restored = plan.matches(get_segments())
        self.plan = None
        self.applied_count = 0
        self._go(UNDONE if restored else ANALYZED)
        return restored
