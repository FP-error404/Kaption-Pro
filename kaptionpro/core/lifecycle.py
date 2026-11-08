"""Koordinator penutupan aplikasi (tanpa GUI, sehingga bisa diuji): menunggu worker, lalu melepas resource dengan aman."""

from __future__ import annotations

import queue
import time
from typing import Callable, Sequence

from .utils import log


class ShutdownCoordinator:
    """
    Mesin status penutupan yang digerakkan lewat poll() (mis. dari Tk `after`), tanpa memblokir UI.

    Alur:
      wait    : tunggu semua worker BENAR-BENAR berhenti (alive() kosong) sampai `stop_timeout`. Bila habis, tanya user
                (ask_wait_more): tunggu lagi (+extension) atau tutup paksa.
      killed  : tutup paksa -> kill_active() mematikan proses eksternal (FFmpeg), lalu beri `post_kill_grace` detik
                agar worker yang bergantung padanya selesai sendiri.
      selesai : worker kosong  -> release_safe()   (semua resource: flush, unlock project, VLC, cache SQLite)
                worker TERSISA -> release_forced() (hanya yang tidak dipakai worker; cache SQLite dan lock project
                                  SENGAJA tidak dilepas -- worker mungkin masih memakainya; OS melepas lock saat
                                  proses keluar). Pemanggil lalu keluar paksa agar interpreter tidak tersangkut.
      finish() SELALU dipanggil terakhir (tandai sesi bersih, hancurkan jendela), apa pun yang terjadi.
    """

    def __init__(self, *, alive: Callable[[], Sequence], kill_active: Callable[[], int],
                 ask_wait_more: Callable[[Sequence], bool], release_safe: Callable[[], None],
                 release_forced: Callable[[], None], finish: Callable[[], None],
                 stop_timeout: float, extension: float, post_kill_grace: float = 3.0,
                 poll_interval: float = 0.1, clock: Callable[[], float] = time.monotonic) -> None:
        self._alive, self._kill_active, self._ask = alive, kill_active, ask_wait_more
        self._release_safe, self._release_forced, self._finish = release_safe, release_forced, finish
        self.stop_timeout, self.extension, self.post_kill_grace = stop_timeout, extension, post_kill_grace
        self.poll_interval = poll_interval
        self._clock = clock
        self._deadline = 0.0
        self.phase = "idle"             # idle | wait | killed | done
        self.forced = False             # True = ada worker yang tidak berhenti walau sudah dipaksa
        self.killed = 0                 # jumlah proses eksternal yang dimatikan paksa

    @property
    def done(self) -> bool:
        return self.phase == "done"

    def begin(self) -> None:
        self.phase = "wait"
        self._deadline = self._clock() + self.stop_timeout

    def poll(self) -> float | None:
        """Satu langkah. Return jeda (detik) sampai poll berikutnya, atau None bila penutupan sudah selesai."""
        if self.phase in ("idle", "done"):
            return None
        alive = list(self._alive())
        if not alive:
            self._complete(forced=False)
            return None
        if self._clock() < self._deadline:
            return self.poll_interval
        if self.phase == "wait":
            try:
                wait_more = bool(self._ask(alive))
            except Exception:  # noqa: BLE001
                log.exception("Dialog tunggu worker gagal; menunggu lagi")
                wait_more = True
            if wait_more:
                self._deadline = self._clock() + self.extension
                return self.poll_interval
            try:
                self.killed = int(self._kill_active())
            except Exception:  # noqa: BLE001
                log.exception("kill_active gagal")
            log.error("Penutupan paksa: %d proses eksternal dimatikan, worker aktif: %s", self.killed,
                      ", ".join(getattr(t, "name", "?") for t in alive))
            self.phase = "killed"
            self._deadline = self._clock() + self.post_kill_grace
            return self.poll_interval
        self._complete(forced=True, alive=alive)             # sudah dipaksa & masa tenggang habis: masih ada worker
        return None

    def _complete(self, forced: bool, alive: Sequence = ()) -> None:
        self.forced = forced
        try:
            if forced:
                log.error("Worker tidak berhenti (%s): cache SQLite dan lock project TIDAK dilepas; proses akan "
                          "keluar paksa", ", ".join(getattr(t, "name", "?") for t in alive))
                self._release_forced()
            else:
                self._release_safe()
        except Exception:  # noqa: BLE001
            log.exception("Pelepasan resource saat menutup gagal")
        finally:
            self.phase = "done"
            try:
                self._finish()
            except Exception:  # noqa: BLE001
                log.exception("finish() penutupan gagal")


def drain_queue(q: "queue.Queue", max_tasks: int, on_error: Callable[[], None],
                time_budget: float | None = None, clock: Callable[[], float] = time.monotonic) -> tuple[int, bool]:
    """
    Proses callback (fn, args) dari antrean UI dengan DUA batas, lalu KEMBALI supaya event loop Tk sempat memproses
    klik/gambar ulang: paling banyak `max_tasks` callback DAN (bila diberikan) `time_budget` detik. Batas jumlah saja
    tidak cukup -- 100 callback berat bisa menahan UI ratusan milidetik. Minimal SATU callback selalu diproses per
    panggilan (antrean tidak boleh kelaparan walau satu callback melebihi anggaran). Satu callback yang sedang berjalan
    tidak bisa dihentikan di tengah; pekerjaan berat tetap harus di worker. Exception sebuah callback tidak
    menghentikan sisanya. Return (jumlah diproses, masih_ada_sisa).
    """
    processed = 0
    deadline = None if time_budget is None else clock() + time_budget
    while processed < max_tasks:
        if processed and deadline is not None and clock() >= deadline:
            break
        try:
            fn, args = q.get_nowait()
        except queue.Empty:
            return processed, False
        processed += 1
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            on_error()
    return processed, not q.empty()
