"""JobTracker: status pekerjaan latar belakang aktif (AI / export), tanpa GUI sehingga bisa diuji."""

from __future__ import annotations

import threading

from .utils import WorkerRegistry


class JobTracker:
    """
    Satu-satunya pemilik status 'job aktif': jenis job, TOKEN, dan cancel_event -- diekstrak dari WorkspaceScreen (v1.9.7).

    Token membuat callback basi aman: worker mengirim hasil ke UI bersama token job-nya; UI hanya menerapkannya bila
    is_current(token). Job yang dibatalkan/diabaikan (abort) menaikkan token, sehingga hasil yang datang terlambat --
    termasuk dari project sebelumnya -- tidak pernah diterapkan ke project yang sedang terbuka.
    Metode status dipanggil dari thread UI; worker hanya memegang cancel_event miliknya sendiri.
    """

    def __init__(self) -> None:
        self._kind: str | None = None
        self._token = 0
        self._cancel = threading.Event()
        self.workers = WorkerRegistry()

    @property
    def kind(self) -> str | None:
        return self._kind

    @property
    def token(self) -> int:
        return self._token

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    def begin(self, kind: str) -> int:
        """Mulai job baru: token baru + cancel_event BARU (event job lama tetap set, tidak bisa 'hidup' lagi)."""
        self._token += 1
        self._kind = kind
        self._cancel = threading.Event()
        return self._token

    def end(self) -> None:
        self._kind = None

    def is_current(self, token: int) -> bool:
        return self._kind is not None and token == self._token

    def request_cancel(self) -> str | None:
        """Permintaan batal dari user; job tetap 'aktif' sampai worker melapor. Return jenis job (None = tak ada)."""
        kind = self._kind
        if kind:
            self._cancel.set()
        return kind

    def abort(self) -> str | None:
        """Hentikan job tanpa menunggu laporan worker (ganti project / tutup app): cancel + token naik + tidak aktif.
        Return jenis job yang dihentikan (None = tak ada job)."""
        kind = self._kind
        if kind:
            self._cancel.set()
            self._token += 1
            self._kind = None
        return kind

    # ---- worker (thread) ----
    def start_worker(self, thread: threading.Thread) -> None:
        self.workers.start(thread)

    def alive_workers(self) -> list[threading.Thread]:
        return self.workers.alive()
