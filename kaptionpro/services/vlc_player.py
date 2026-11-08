"""Pemutar media (python-vlc) dengan fallback simulasi."""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

from ..config import HAS_VLC, _vlc, log


class MediaPlayer:
    """Wrapper python-vlc. Fallback: simulasi (playhead berjalan sendiri)."""

    def __init__(self, attach_widget: tk.Widget) -> None:
        self.widget = attach_widget
        self.instance = None
        self.player = None
        self.media = None
        self.available = False
        self._duration = 0.0
        self._path = ""
        self._sub_slave_registered = False
        self._pending_seek: float | None = None   # seek yang diminta sebelum media Playing/Paused
        self._slave_count = 0
        self._last_sub_path = ""                  # slave subtitle terakhir (dipulihkan otomatis saat media dimuat ulang)
        self._sub_verify_pending = False          # track subtitle perlu dipastikan terpilih saat mulai Playing
        self._volume = 80
        self._muted = False
        if HAS_VLC:
            try:
                self.instance = _vlc.Instance("--quiet", "--no-video-title-show")
                self.player = self.instance.media_player_new()
                self.available = True
                log.info("VLC player aktif")
            except Exception as e:  # noqa: BLE001
                log.warning("VLC gagal diinisialisasi: %s", e)
                self.available = False

    def _release_media(self) -> None:
        """Lepaskan objek media lama (libvlc menahan resource sampai di-release)."""
        if self.media is not None:
            try:
                self.media.release()
            except Exception:  # noqa: BLE001
                pass
            self.media = None

    def load(self, path: str) -> None:
        """Muat media. path kosong = hentikan & lepaskan media lama."""
        if (path or "") != self._path:
            self._last_sub_path = ""                 # media berbeda -> subtitle lama tidak relevan
        self._path = path or ""
        self._sub_slave_registered = False
        self._sub_verify_pending = False
        self._slave_count = 0
        self._pending_seek = None
        self._duration = 0.0
        if not self.available:
            return
        try:
            self.player.stop()
            self._release_media()
            if not path:
                self.player.set_media(None)
                return
            self.media = self.instance.media_new(path)
            self.player.set_media(self.media)
            self._attach()
            self._apply_volume()
        except Exception as e:  # noqa: BLE001
            log.warning("Gagal memuat media di VLC: %s", e)

    @property
    def vlc_subs_active(self) -> bool:
        """True bila subtitle ASS sedang dirender oleh VLC (overlay Tk tidak perlu)."""
        return bool(self.available and self._path and self._sub_slave_registered)

    @property
    def slave_count(self) -> int:
        return self._slave_count

    @property
    def has_media(self) -> bool:
        """True bila VLC aktif dan sebuah media sedang dimuat."""
        return bool(self.available and self._path)

    @property
    def current_path(self) -> str:
        return self._path

    def get_video_size(self) -> tuple[int, int]:
        """Resolusi video menurut VLC ((0, 0) bila belum diketahui)."""
        if self.available and self.player:
            try:
                size = self.player.video_get_size(0)
                if size and size[0] > 0 and size[1] > 0:
                    return int(size[0]), int(size[1])
            except Exception:  # noqa: BLE001
                pass
        return 0, 0

    def _attach(self) -> None:
        wid = self.widget.winfo_id()
        if sys.platform.startswith("win"):
            self.player.set_hwnd(wid)
        elif sys.platform == "darwin":
            self.player.set_nsobject(wid)
        else:
            self.player.set_xwindow(wid)

    def play(self) -> None:
        if not self.available:
            return
        try:
            state = self.player.get_state()
            if state == _vlc.State.Paused:
                self.player.set_pause(0)
            else:
                self.player.play()
        except Exception as e:  # noqa: BLE001
            log.warning("VLC play gagal: %s", e)

    def pause(self) -> None:
        """Pause eksplisit (bukan toggle)."""
        if self.available:
            try:
                self.player.set_pause(1)
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        if self.available:
            try:
                self.player.stop()
            except Exception:  # noqa: BLE001
                pass

    def is_playing(self) -> bool:
        if self.available:
            try:
                return bool(self.player.is_playing())
            except Exception:  # noqa: BLE001
                return False
        return False

    def get_state(self):
        if self.available:
            try:
                return self.player.get_state()
            except Exception:  # noqa: BLE001
                return None
        return None

    def set_time(self, seconds: float) -> None:
        """Seek. Bila media belum Playing/Paused, seek DITUNDA (pending) sampai play benar-benar mulai."""
        if not (self.available and self.player and self._path):
            return
        seconds = max(0.0, seconds)
        try:
            if self.player.get_state() in (_vlc.State.Playing, _vlc.State.Paused):
                self.player.set_time(int(seconds * 1000))
                self._pending_seek = None
            else:
                self._pending_seek = seconds
        except Exception:  # noqa: BLE001
            pass

    def apply_pending_seek(self) -> None:
        """Dipanggil dari tick: terapkan seek tertunda begitu VLC siap."""
        if self._pending_seek is None or not self.available:
            return
        try:
            if self.player.get_state() in (_vlc.State.Playing, _vlc.State.Paused):
                self.player.set_time(int(self._pending_seek * 1000))
                self._pending_seek = None
        except Exception:  # noqa: BLE001
            pass

    def get_time(self) -> float:
        if self._pending_seek is not None:
            return self._pending_seek
        if self.available and self.player:
            try:
                ms = self.player.get_time()
                if ms and ms > 0:
                    return ms / 1000.0
            except Exception:  # noqa: BLE001
                pass
        return -1.0

    def get_duration(self) -> float:
        if self.available and self.player:
            try:
                ms = self.player.get_length()
                if ms and ms > 0:
                    return ms / 1000.0
            except Exception:  # noqa: BLE001
                pass
        return 0.0

    def _apply_volume(self) -> None:
        if self.available and self.player:
            try:
                self.player.audio_set_volume(0 if self._muted else int(self._volume))
            except Exception:  # noqa: BLE001
                pass

    def set_volume(self, v: int) -> None:
        self._volume = int(max(0, min(100, v)))
        self._apply_volume()

    def set_mute(self, muted: bool) -> None:
        self._muted = bool(muted)
        if self.available and self.player:
            try:
                self.player.audio_set_mute(self._muted)
            except Exception:  # noqa: BLE001
                pass
        self._apply_volume()

    def get_volume(self) -> int:
        return self._volume

    def get_fps(self) -> float:
        """FPS menurut VLC (0 bila belum diketahui)."""
        if self.available and self.player:
            try:
                return float(self.player.get_fps() or 0.0)
            except Exception:  # noqa: BLE001
                pass
        return 0.0

    def next_frame(self) -> bool:
        """Maju satu frame (VLC otomatis menjeda). True bila perintah terkirim."""
        if self.available and self.player and self._path:
            try:
                self.player.next_frame()
                return True
            except Exception:  # noqa: BLE001
                pass
        return False

    def state_name(self) -> str:
        """Nama status VLC yang ramah (membuka / buffering / memutar / jeda / berhenti / selesai / error)."""
        if not self.available or _vlc is None:
            return "simulasi"
        st = self.get_state()
        names = {"Opening": "membuka", "Buffering": "buffering", "Playing": "memutar", "Paused": "jeda",
                 "Stopped": "berhenti", "Ended": "selesai", "Error": "error"}
        for attr, label in names.items():
            if getattr(_vlc.State, attr, object()) == st:
                return label
        return "idle"

    def select_latest_subtitle_track(self) -> bool:
        """Pastikan track subtitle slave terbaru terpilih (track hanya muncul setelah media Playing)."""
        if not (self.available and self.player):
            return False
        try:
            ids = [d[0] for d in (self.player.video_get_spu_description() or []) if d[0] != -1]
            if not ids:
                return False
            if self.player.video_get_spu() != ids[-1]:
                self.player.video_set_spu(ids[-1])
            return True
        except Exception:  # noqa: BLE001
            return False

    def verify_subtitle_track(self) -> None:
        """Dipanggil dari tick: selesaikan pemilihan track subtitle yang tertunda."""
        if self._sub_verify_pending and self.select_latest_subtitle_track():
            self._sub_verify_pending = False

    def add_subtitle_slave(self, path: str, select: bool = True) -> bool:
        """
        Daftarkan file ASS sebagai slave VLC (preview = export). Return True bila VLC menerimanya.
        python-vlc TIDAK melempar exception saat gagal — ia mengembalikan -1 — jadi nilai
        kembaliannya harus dicek (mis. gagal bila media belum benar-benar diputar).
        """
        if not self.available or not path:
            return False
        try:
            uri = Path(path).absolute().as_uri()
            ret = self.player.add_slave(_vlc.MediaSlaveType.subtitle, uri, bool(select))
        except Exception as e:  # noqa: BLE001
            log.warning("add_slave gagal: %s", e)
            return False
        if ret not in (None, 0):
            log.warning("add_slave ditolak VLC (kode %s) — akan dicoba lagi saat video mulai diputar", ret)
            return False
        self._sub_slave_registered = True
        self._slave_count += 1
        self._last_sub_path = path
        self._sub_verify_pending = True
        return True

    def set_spu(self, index: int) -> None:
        """Pilih track subtitle (-1 = matikan)."""
        if self.available and self.player:
            try:
                self.player.video_set_spu(int(index))
            except Exception:  # noqa: BLE001
                pass

    def reload_keep_position(self) -> None:
        """Muat ulang media (membuang semua slave lama) sambil mempertahankan posisi & status play."""
        if not (self.available and self._path):
            return
        path, t, playing, sub = self._path, self.get_time(), self.is_playing(), self._last_sub_path
        self.load(path)
        if sub:
            self.add_subtitle_slave(sub)             # pulihkan track subtitle yang hilang saat media dimuat ulang
        if t >= 0:
            self._pending_seek = t
        if playing:
            self.play()

    def close(self) -> None:
        """Lepaskan SEMUA resource VLC: media, player, dan instance. Aman dipanggil berulang."""
        if not self.available:
            return
        for step in (lambda: self.player.stop(), self._release_media,
                     lambda: self.player.release(), lambda: self.instance.release()):
            try:
                step()
            except Exception:  # noqa: BLE001
                pass
        self.available = False

    def release(self) -> None:
        self.close()
