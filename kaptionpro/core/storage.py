"""Penyimpanan: ProjectStore (project, backup, lock), SettingsStore, TranslationCache."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..config import APP_DIR, SESSION_ID, log
from ..models import LoadReport, Project, SubtitleIO, SubtitleSegment
from . import utils as utils_mod
from .utils import (InterProcessLock, _atomic_write, _norm_path, _pid_alive, interprocess_critical_section,
                    is_valid_project_id)


_MISSING = object()      # file tidak ada
_CORRUPT = object()      # file ada tapi tidak terbaca / bukan JSON yang valid


@dataclass
class BackupInfo:
    path: Path
    saved_at: str
    reason: str
    lines: int


@dataclass
class LoadResult:
    segments: list
    report: LoadReport
    source: str = "file"                 # file | backup | empty
    notes: list = field(default_factory=list)
    migrated: bool = False


class ProjectStore:
    """
    Recent projects + subtitle per project, dengan:
      * penulisan atomik + fsync, validasi JSON/struktur, checksum, migrasi versi
      * karantina file rusak + pemulihan otomatis dari backup
      * backup berversi (rotasi) per project
      * lock antar-proses berbasis kunci OS (tidak ada lock 'basi' yang perlu dinilai/dihapus)
      * penanda sesi PER INSTANCE (deteksi aplikasi tertutup tidak normal; beberapa instance tidak saling menimpa)
    """

    MAX_ITEMS = 24
    MAX_BACKUPS = 15
    BACKUP_MIN_INTERVAL = 300.0       # detik; backup "autosave" paling sering tiap 5 menit
    SCHEMA_VERSION = 3
    LIST_LOCK_TIMEOUT = 10.0          # detik menunggu instance lain selesai menulis recent_projects.json

    # Lock yang dipegang PROSES ini (kunci path -> lock). Satu proses = satu pemegang, sehingga lock()
    # berulang untuk project yang sama (mis. dua instance ProjectStore) tetap sukses seperti sebelumnya.
    _held_locks: dict[str, InterProcessLock] = {}
    _held_guard = threading.Lock()

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or APP_DIR
        self.list_path = self.root / "recent_projects.json"
        self.bak_path = self.root / "recent_projects.json.bak"
        self.session_path = self.root / "session.json"        # format lama (<= v1.9.1); hanya dibaca untuk migrasi
        self.sessions_dir = self.root / "sessions"            # satu file per instance aplikasi
        self.last_save_error: str | None = None
        self.last_lock_error: str | None = None      # alasan lock GAGAL dibuat (bukan 'dipegang proses lain')
        self.last_delete_error: str | None = None    # alasan delete_project() ditolak/gagal
        self._session_lock: InterProcessLock | None = None   # penanda sesi INSTANCE ini
        self.last_saved_at = ""
        self.notes: list[str] = []          # catatan pemulihan untuk ditampilkan ke user
        self._rejected: list = []           # entri daftar project dengan ID tidak valid (dikarantina saat load)
        self.projects: list[Project] = self._load()

    # ---------- util file ----------
    @staticmethod
    def _read_json(path: Path):
        """Return objek JSON, atau _MISSING (tidak ada) / _CORRUPT (tidak terbaca / bukan JSON)."""
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _MISSING
        except (OSError, ValueError) as e:
            log.warning("Gagal membaca %s: %s", path.name, e)
            return _CORRUPT

    @staticmethod
    def _quarantine(path: Path) -> Path | None:
        """Pindahkan file rusak ke <nama>.corrupt-<waktu> agar tidak tertimpa & bisa diperiksa manual."""
        try:
            dest = path.with_name(f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
            os.replace(path, dest)
            log.warning("File rusak dikarantina: %s", dest)
            return dest
        except OSError as e:
            log.warning("Karantina gagal: %s", e)
            return None

    @staticmethod
    def _checksum(items: list) -> str:
        blob = json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ---------- daftar project ----------
    def _parse_projects(self, data) -> tuple[list[Project], bool]:
        known = {f.name for f in fields(Project)}
        projects, migrated = [], False
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            if item.get("id") is None:                       # tidak ada / null (format lama) -> beri ID baru
                item["id"] = uuid.uuid4().hex
                migrated = True
            elif not is_valid_project_id(item["id"]):        # ID membentuk path file: TIDAK dipakai, dikarantina
                self._rejected.append(item)
                continue
            try:
                kw = {k: v for k, v in item.items() if k in known}
                if not isinstance(kw.get("style", {}), dict):
                    kw["style"] = {}
                if not isinstance(kw.get("export", {}), dict):
                    kw["export"] = {}
                if not isinstance(kw.get("line_count", 0), int):
                    kw["line_count"] = 0
                projects.append(Project(**kw))
            except (TypeError, ValueError) as e:
                log.warning("Item project dilewati: %s", e)
        return projects, migrated

    def _list_lock_path(self) -> Path:
        return self.root / "recent_projects.json.lock"

    def _read_list(self, repair: bool = True) -> tuple[list[Project], bool]:
        """Baca daftar dari disk. Return (daftar, perlu_ditulis_ulang).
        repair=True (WAJIB memegang lock daftar): file rusak dikarantina + dipulihkan dari .bak, entri ber-ID tidak
        valid disalin ke file karantina. repair=False (read-only, dipakai saat lock tidak didapat): TIDAK ada yang
        ditulis/dipindah di disk; file rusak hanya dibaca dari .bak, perbaikan ditunda sampai lock tersedia."""
        needs_write = False
        data = self._read_json(self.list_path)
        if data is _CORRUPT or (data is not _MISSING and not isinstance(data, list)):
            if repair:
                self._quarantine(self.list_path)
            data = self._read_json(self.bak_path)
            if isinstance(data, list):
                self.notes.append("Daftar project rusak — dipulihkan dari salinan cadangan (.bak)." if repair else
                                  "Daftar project rusak — dibaca dari salinan cadangan (.bak); perbaikan permanen "
                                  "ditunda karena lock daftar tidak tersedia.")
                needs_write = repair                           # list utama sudah dikarantina -> tulis ulang
            else:
                self.notes.append("Daftar project rusak dan tidak bisa dipulihkan; dimulai kosong. "
                                  "Subtitle tiap project tetap aman di folder 'projects'.")
                data = _MISSING
        if data is _MISSING:
            return [], needs_write
        projects, migrated = self._parse_projects(data)
        if not repair:
            self._rejected.clear()                             # tidak ada karantina tanpa lock
            return projects, False
        saved_rejected = self._quarantine_rejected()
        return projects, needs_write or migrated or saved_rejected

    def _load(self) -> list[Project]:
        with interprocess_critical_section(self._list_lock_path(), self.LIST_LOCK_TIMEOUT) as held:
            if not held:
                # Tanpa lock: HANYA baca. Karantina/pemulihan/migrasi/penulisan menunggu sampai lock tersedia.
                log.warning("Lock daftar project tidak didapat: daftar dimuat read-only, tanpa perbaikan/penulisan")
                projects, _ = self._read_list(repair=False)
                self.projects = projects
                return projects
            projects, needs_write = self._read_list()
            self.projects = projects
            if needs_write:
                self._write_list(projects)
        return projects

    def reload(self) -> None:
        """Muat ulang daftar dari disk (perubahan instance lain). Hanya MEMBACA: file yang tak terbaca/rusak
        diabaikan di sini (daftar di memori dipertahankan; pemulihan hanya dilakukan di bawah lock saat menulis)."""
        data = self._read_json(self.list_path)
        if not isinstance(data, list):
            return
        projects, _ = self._parse_projects(data)
        self._rejected.clear()
        self.projects = projects

    def _quarantine_rejected(self) -> bool:
        """Salin entri ber-ID tidak valid ke recent_projects.rejected-<waktu>.json (agar bisa diperiksa manual) lalu
        beri tahu user. True bila salinan tertulis (aman mencabut entri itu dari daftar utama)."""
        rejected, self._rejected = self._rejected, []
        if not rejected:
            return False
        dest = self.root / f"recent_projects.rejected-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        try:
            _atomic_write(dest, json.dumps(rejected, indent=2, ensure_ascii=False, default=str))
        except OSError as e:
            log.warning("Entri project ber-ID tidak valid tidak bisa dikarantina: %s", e)
            self.notes.append(f"{len(rejected)} entri project ber-ID tidak valid diabaikan (salinan gagal ditulis: {e}).")
            return False
        log.warning("%d entri project ber-ID tidak valid dikarantina ke %s", len(rejected), dest)
        self.notes.append(f"{len(rejected)} entri project memiliki ID tidak valid dan TIDAK dipakai "
                          f"(disalin ke {dest.name} untuk diperiksa).")
        return True

    def _write_list(self, projects: list[Project]) -> bool:
        """Tulis daftar (atomik; salinan .bak dari versi baik terakhir). Pemanggil memegang lock daftar."""
        try:
            if self.list_path.exists() and self._read_json(self.list_path) is not _CORRUPT:
                shutil.copy2(self.list_path, self.bak_path)       # salinan terakhir yang baik
            _atomic_write(self.list_path, json.dumps([asdict(p) for p in projects], indent=2, ensure_ascii=False))
            return True
        except OSError as e:
            log.error("Gagal menyimpan recent projects: %s", e)
            self.last_save_error = str(e)
            return False

    def _update_list(self, mutator: Callable[[list[Project]], list[Project]]) -> bool:
        """
        Read-modify-write daftar project yang aman antar-instance: lock -> BACA data terbaru dari disk -> terapkan
        `mutator` -> tulis -> lepas lock. Penulisan atomik saja tidak cukup: tanpa baca-ulang di dalam lock,
        instance B menimpa penambahan instance A (lost update).
        FAIL-CLOSED: bila lock tidak didapat (timeout/izin) TIDAK ADA yang ditulis ke disk: daftar di disk tidak
        pernah ditimpa dengan data basi dari memori. Return False + last_save_error. Daftar di memori tetap
        diperbarui agar sesi berjalan konsisten; perubahan itu ikut tertulis pada update berikutnya yang berhasil
        (mis. autosave berikutnya memanggil add() lagi).
        """
        with interprocess_critical_section(self._list_lock_path(), self.LIST_LOCK_TIMEOUT) as held:
            if not held:
                self.last_save_error = ("Lock daftar project tidak didapat (dipakai instance lain terlalu lama atau "
                                        "folder data tidak bisa dikunci): perubahan daftar belum disimpan.")
                log.error(self.last_save_error)
                self.projects = mutator(list(self.projects))      # hanya memori; disk tidak disentuh
                return False
            current, _ = self._read_list()
            updated = mutator(current)
            ok = self._write_list(updated)
            self.projects = updated                                 # UI tetap konsisten walau disk gagal
            if ok:
                self.last_save_error = None
            return ok

    def find_by_video(self, video_path: str) -> Project | None:
        target = _norm_path(video_path)
        return next((p for p in self.projects if target and _norm_path(p.video_path) == target), None)

    def add(self, project: Project) -> bool:
        project.touch()

        def mutate(current: list[Project]) -> list[Project]:
            rest = [p for p in current if p.id != project.id]
            return ([project] + rest)[: self.MAX_ITEMS]

        return self._update_list(mutate)

    def remove_from_recent(self, project: Project) -> bool:
        """Hanya mencabut project dari DAFTAR. Subtitle, snapshot, preview, dan backup TIDAK disentuh."""
        return self._update_list(lambda current: [p for p in current if p.id != project.id])

    def delete_project(self, project: Project) -> bool:
        """
        Hapus PERMANEN data project (subtitle, snapshot, preview, semua backup) lalu cabut dari daftar.
        Hanya dilakukan bila lock EKSKLUSIF berhasil diambil: bila project sedang dipakai instance lain (atau
        dibuka di proses ini) penghapusan DIBATALKAN dan tidak ada yang disentuh. Bila sebagian file gagal dihapus,
        project tetap ada di daftar agar bisa dicoba lagi. False -> lihat last_delete_error.
        """
        self.last_delete_error = None
        try:
            path = self._lock_path(project)               # ID tidak valid -> ValueError: tidak ada yang dihapus
            path.parent.mkdir(parents=True, exist_ok=True)
        except ValueError as e:
            self.last_delete_error = str(e)
            return False
        except OSError as e:
            self.last_delete_error = f"folder lock tidak bisa dibuat: {e}"
            return False
        with self._held_guard:
            if _norm_path(str(path)) in self._held_locks:
                self.last_delete_error = "Project sedang dibuka di jendela ini. Tutup project-nya dulu."
                return False
            lk = InterProcessLock(path, remove_on_release=True)
            try:
                got = lk.acquire()
            except OSError as e:
                self.last_delete_error = f"lock project tidak bisa diambil: {e}"
                return False
            if not got:
                self.last_delete_error = "Project sedang dibuka di instance KaptionPro lain."
                return False
        try:
            failures: list[str] = []
            for target in (self.subs_path(project), self.snapshot_path(project), self.preview_ass_path(project)):
                try:
                    target.unlink(missing_ok=True)
                except OSError as e:
                    failures.append(f"{target.name}: {e}")
            try:
                shutil.rmtree(self.backup_dir(project))
            except FileNotFoundError:
                pass
            except OSError as e:
                failures.append(f"backup: {e}")
            if failures:
                self.last_delete_error = "Sebagian data gagal dihapus (project tetap di daftar):\n" + "\n".join(failures)
                log.warning("delete_project gagal: %s", failures)
                return False
            if not self._update_list(lambda current: [p for p in current if p.id != project.id]):
                self.last_delete_error = ("Data project sudah dihapus, tetapi daftar belum bisa diperbarui "
                                          f"({self.last_save_error or 'lock daftar tidak tersedia'}).\n"
                                          "Hapus dari daftar lagi sebentar lagi.")
                return False
            return True
        finally:
            lk.release()

    # ---------- path per project ----------
    @staticmethod
    def _id(project: Project) -> str:
        """ID untuk membentuk path — divalidasi ULANG tiap dipakai (Project bisa dimutasi setelah dibuat)."""
        if not is_valid_project_id(project.id):
            raise ValueError(f"ID project tidak valid: {project.id!r}")
        return project.id

    def subs_path(self, project: Project) -> Path:
        return self.root / "projects" / f"{self._id(project)}.json"

    def snapshot_path(self, project: Project) -> Path:
        return self.root / "snapshots" / f"{self._id(project)}.json"

    def _lock_path(self, project: Project) -> Path:
        return self.root / "locks" / f"{self._id(project)}.lock"

    def preview_ass_path(self, project: Project) -> Path:
        return self.root / "preview" / f"{self._id(project)}.ass"

    def backup_dir(self, project: Project) -> Path:
        return self.root / "backups" / self._id(project)

    # ---------- subtitle: simpan / muat ----------
    def _payload(self, project: Project, segments: list[SubtitleSegment], kind: str = "subs",
                 reason: str = "") -> dict:
        items = [s.to_dict() for s in segments]
        payload = {"schema_version": self.SCHEMA_VERSION, "project_id": project.id, "kind": kind,
                   "saved_at": datetime.now().isoformat(timespec="seconds"), "count": len(items),
                   "sha256": self._checksum(items), "segments": items}
        if reason:
            payload["reason"] = reason
        return payload

    def save_segments(self, project: Project, segments: list[SubtitleSegment]) -> None:
        """Simpan atomik. Melempar OSError bila gagal (pemanggil menampilkan status)."""
        try:
            _atomic_write(self.subs_path(project),
                          json.dumps(self._payload(project, segments), ensure_ascii=False))
        except OSError as e:
            self.last_save_error = str(e)
            raise
        self.last_save_error = None
        self.last_saved_at = datetime.now().strftime("%H:%M:%S")
        project.line_count = len(segments)

    @staticmethod
    def _parse_items(items: list) -> tuple[list[SubtitleSegment], int]:
        segs, dropped = [], 0
        for d in items:
            try:
                segs.append(SubtitleSegment.from_dict(d))
            except (KeyError, TypeError, ValueError, AttributeError):
                dropped += 1
        return segs, dropped

    def _decode_payload(self, data, source: str = "file") -> LoadResult:
        if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
            raise ValueError("struktur data tidak valid")
        items = data["segments"]
        notes: list[str] = []
        version = data.get("schema_version", data.get("version", 1))
        migrated = version != self.SCHEMA_VERSION
        checksum = data.get("sha256")
        if checksum and checksum != self._checksum(items):
            notes.append("Checksum tidak cocok — file mungkin diedit di luar aplikasi atau rusak sebagian. "
                         "Periksa subtitle; versi sebelumnya ada di tombol Backup.")
        segs, dropped = self._parse_items(items)
        segs, rep = SubtitleIO.normalize(segs)
        rep.dropped += dropped
        notes.extend(rep.notes())
        return LoadResult(segs, rep, source, notes, migrated)

    def _recover(self, project: Project, reason: str) -> LoadResult:
        infos = self.list_backups(project)
        for info in infos:
            try:
                res = self.load_backup(info.path)
            except (OSError, ValueError):
                continue
            res.source = "backup"
            res.notes.insert(0, f"{reason} — dipulihkan dari backup {info.saved_at}.")
            return res
        if infos:
            msg = (f"{reason}, dan semua {len(infos)} backup juga rusak/tidak terbaca. Project dimulai kosong; "
                   "file yang rusak disimpan di folder data (berakhiran .corrupt-...) untuk diperiksa manual.")
        else:
            msg = f"{reason} dan tidak ada backup. Project dimulai kosong."
        return LoadResult([], LoadReport(), "empty", [msg])

    def load_segments_ex(self, project: Project) -> LoadResult:
        """Muat subtitle dengan validasi penuh; file rusak -> karantina + pulihkan dari backup terbaru."""
        path = self.subs_path(project)
        data = self._read_json(path)
        if data is _MISSING:
            return LoadResult([], LoadReport(), "empty")
        if data is _CORRUPT:
            self._quarantine(path)
            return self._recover(project, "File subtitle rusak")
        try:
            return self._decode_payload(data)
        except ValueError as e:
            self._quarantine(path)
            return self._recover(project, f"File subtitle tidak valid ({e})")

    def load_segments(self, project: Project) -> list[SubtitleSegment]:
        return self.load_segments_ex(project).segments

    # ---------- snapshot (sebelum operasi besar) ----------
    def save_snapshot(self, project: Project, segments: list[SubtitleSegment]) -> bool:
        """True bila snapshot benar-benar tertulis (pemanggil harus memeriksa sebelum operasi berisiko)."""
        try:
            _atomic_write(self.snapshot_path(project),
                          json.dumps(self._payload(project, segments, kind="snapshot"), ensure_ascii=False))
            return True
        except OSError as e:
            log.warning("Snapshot gagal: %s", e)
            return False

    def load_snapshot(self, project: Project) -> list[SubtitleSegment] | None:
        data = self._read_json(self.snapshot_path(project))
        if data is _MISSING or data is _CORRUPT:
            return None
        try:
            return self._decode_payload(data).segments
        except ValueError as e:
            log.warning("Snapshot tidak valid: %s", e)
            return None

    def snapshot_time(self, project: Project) -> str:
        data = self._read_json(self.snapshot_path(project))
        return data.get("saved_at", "") if isinstance(data, dict) else ""

    def delete_snapshot(self, project: Project) -> None:
        try:
            self.snapshot_path(project).unlink(missing_ok=True)
        except OSError as e:
            log.warning("Gagal hapus snapshot: %s", e)

    def has_snapshot(self, project: Project) -> bool:
        return self.snapshot_path(project).exists()

    # ---------- backup berversi ----------
    @staticmethod
    def _safe_reason(reason: str) -> str:
        return re.sub(r"[^a-z0-9-]", "", reason.lower().replace(" ", "-"))[:20] or "backup"

    def _backup_files(self, project: Project) -> list[Path]:
        d = self.backup_dir(project)
        try:
            files = [p for p in d.iterdir() if p.suffix == ".json" and p.name.count("__") == 3]
        except OSError:
            return []
        return sorted(files, key=lambda p: p.name, reverse=True)          # nama berawalan timestamp

    def create_backup(self, project: Project, segments: list[SubtitleSegment],
                      reason: str = "autosave", force: bool = False) -> Path | None:
        """
        Buat backup berversi. "autosave" dibatasi frekuensinya & dilewati bila isi sama dengan backup
        terbaru; alasan lain (sebelum AI/import/auto-fix/restore) selalu dibuat bila isi berubah.
        Mengembalikan path, atau None bila dilewati/gagal.
        """
        if not segments and reason == "autosave":
            return None
        items = [s.to_dict() for s in segments]
        digest = self._checksum(items)
        files = self._backup_files(project)
        if files:
            if files[0].stem.split("__")[3] == digest[:8]:
                return None                                            # tidak ada perubahan sejak backup terbaru
            if reason == "autosave" and not force:
                try:
                    if time.time() - files[0].stat().st_mtime < self.BACKUP_MIN_INTERVAL:
                        return None
                except OSError:
                    pass
        name = f"{datetime.now().strftime('%Y%m%d-%H%M%S%f')}__{self._safe_reason(reason)}__{len(items)}__{digest[:8]}.json"
        path = self.backup_dir(project) / name
        if path.exists():
            return None
        try:
            _atomic_write(path, json.dumps(self._payload(project, segments, kind="backup", reason=reason),
                                           ensure_ascii=False))
            self._verify_backup(path, digest, len(items))
        except (OSError, ValueError) as e:
            log.warning("Backup gagal: %s", e)
            try:
                path.unlink(missing_ok=True)               # backup yang tak terbukti utuh tidak boleh dipakai/merotasi
            except OSError:
                pass
            return None
        for old in self._backup_files(project)[self.MAX_BACKUPS:]:       # rotasi: simpan N terbaru
            try:
                old.unlink()
            except OSError:
                pass
        return path

    def _verify_backup(self, path: Path, digest: str, count: int) -> None:
        """Baca ulang backup dari disk: harus JSON valid, jumlah baris & checksum cocok. ValueError bila tidak."""
        data = self._read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
            raise ValueError("backup tidak terbaca setelah ditulis")
        if len(data["segments"]) != count or self._checksum(data["segments"]) != digest:
            raise ValueError("isi backup tidak cocok dengan yang ditulis")

    def list_backups(self, project: Project) -> list[BackupInfo]:
        infos = []
        for p in self._backup_files(project):
            ts, reason, lines, _dig = p.stem.split("__")
            try:
                saved = datetime.strptime(ts, "%Y%m%d-%H%M%S%f").strftime("%Y-%m-%d %H:%M:%S")
                infos.append(BackupInfo(p, saved, reason, int(lines)))
            except ValueError:
                continue
        return infos

    def load_backup(self, path: Path) -> LoadResult:
        data = json.loads(path.read_text(encoding="utf-8"))
        return self._decode_payload(data, source="backup")

    # ---------- lock antar-proses (kunci OS) ----------
    def lock_owner(self, project: Project) -> int | None:
        """PID yang tercatat di file lock (informasi saja; keputusan lock memakai kunci OS, bukan isi file)."""
        try:
            return InterProcessLock.read_owner_pid(self._lock_path(project))
        except ValueError:
            return None

    def lock(self, project: Project) -> bool:
        """
        Ambil lock project memakai KUNCI OS pada file lock. FAIL-CLOSED:
          True  = lock benar-benar dipegang proses ini.
          False = lock TIDAK dipegang. Bedakan lewat last_lock_error:
                  None  -> project dipakai proses lain yang masih hidup;
                  teks  -> lock tidak bisa dibuat/dikunci (folder/disk/izin) — TIDAK ada perlindungan akses bersamaan.
        Tidak ada tahap 'baca isi file lalu hapus bila dianggap rusak/basi': kunci dilepas OS saat proses
        pemilik mati, dan file yang baru dibuat tidak pernah bisa dianggap basi oleh proses lain.
        """
        self.last_lock_error = None
        try:
            path = self._lock_path(project)
            path.parent.mkdir(parents=True, exist_ok=True)
        except ValueError as e:
            self.last_lock_error = str(e)
            log.warning("Lock ditolak: %s", e)
            return False
        except OSError as e:
            self.last_lock_error = f"folder lock tidak bisa dibuat: {e}"
            log.warning("Lock gagal (folder): %s", e)
            return False
        key = _norm_path(str(path))
        with self._held_guard:
            if key in self._held_locks:
                return True                                # sudah dipegang proses ini
            lk = InterProcessLock(path, remove_on_release=True)
            try:
                got = lk.acquire()
            except OSError as e:
                self.last_lock_error = f"file lock tidak bisa dibuat/dikunci: {e}"
                log.warning("Lock gagal: %s", e)
                return False
            if not got:
                return False                               # dipegang pihak lain (last_lock_error tetap None)
            self._held_locks[key] = lk
            return True

    def unlock(self, project: Project) -> None:
        """Lepas lock — hanya yang dipegang proses ini (tidak pernah menyentuh lock milik proses lain)."""
        try:
            key = _norm_path(str(self._lock_path(project)))
        except ValueError:
            return
        with self._held_guard:
            lk = self._held_locks.pop(key, None)
        if lk is None:
            return
        try:
            lk.release()
        except OSError as e:
            log.warning("Unlock gagal: %s", e)

    def close(self) -> None:
        """Lepas SEMUA yang dipegang proses ini di bawah folder data store ini: lock project (registri lock bersifat
        per-proses/kelas, jadi dikenali lewat lokasinya) dan penanda sesi instance ini. Aman dipanggil berulang.
        Dipakai tes dan alat bantu agar folder data bisa dihapus (Windows menolak menghapus berkas yang masih terbuka)."""
        prefix = _norm_path(str(self.root)).rstrip(os.sep) + os.sep
        with self._held_guard:
            mine = [k for k in self._held_locks if k.startswith(prefix)]
            locks = [self._held_locks.pop(k) for k in mine]
        for lk in locks:
            try:
                lk.release()
            except OSError as e:
                log.warning("Close: lepas lock gagal: %s", e)
        self.session_end()

    def is_locked_by_other(self, project: Project) -> bool:
        """Informasi saja (berdasarkan PID tercatat). Penentu sebenarnya adalah hasil lock()."""
        try:
            if _norm_path(str(self._lock_path(project))) in self._held_locks:
                return False
        except ValueError:
            return False
        owner = self.lock_owner(project)
        return owner is not None and owner != os.getpid() and _pid_alive(owner)

    # ---------- penanda sesi per instance (deteksi crash) ----------
    @staticmethod
    def _session_crashed(path: Path) -> bool:
        """File sesi berisi PID (bukan 'CLEAN') = pemilik tak sempat menutup normal. File kosong = pemilik belum
        sempat menulis info (bukan crash)."""
        try:
            return int(path.read_text(encoding="utf-8").split("\n", 1)[0].strip()) > 0
        except (OSError, ValueError):
            return False

    SESSION_MARKER_TTL = 24 * 3600.0       # penanda CLEAN selebihnya baru dibersihkan setelah selama ini tak disentuh

    def _cleanup_session_markers(self) -> None:
        """
        Bersihkan sisa penanda lama (terutama Windows, di mana release() tidak menghapus file).
        Urutan yang aman terhadap proses lain: (1) saring kasar lewat umur file, (2) AMBIL KUNCI OS (tanpa menimpa
        isi), (3) periksa ULANG umur + status lewat fd yang dikunci, (4) hapus hanya bila masih memenuhi syarat.
        POSIX: penghapusan dilakukan selagi kunci dipegang (release() memverifikasi inode); Windows: file tidak bisa
        dihapus selagi dibuka proses lain, jadi penghapusan best-effort dilakukan segera setelah kunci dilepas.
        Penanda berisi PID (sesi hidup/crash) atau yang kuncinya dipegang pihak lain TIDAK pernah disentuh.
        """
        cutoff = time.time() - self.SESSION_MARKER_TTL
        try:
            files = list(self.sessions_dir.glob("*.session"))
        except OSError:
            return
        for path in files:
            try:
                if path.stat().st_mtime > cutoff:
                    continue                                   # saringan kasar; diperiksa ulang di bawah lock
            except OSError:
                continue
            probe = InterProcessLock(path, remove_on_release=False)
            try:
                if not probe.acquire(write_info=False, create=False):   # dipegang pihak lain / sudah dihapus
                    continue
            except OSError:
                continue
            delete_after_release = False
            try:
                mtime, info = probe.mtime(), probe.read_info()
                eligible = (mtime is not None and mtime <= cutoff and info is not None
                            and info.startswith("CLEAN") and probe.still_current())
                if eligible:
                    probe.remove_on_release = True             # POSIX: unlink selagi terkunci (identitas diverifikasi)
                    delete_after_release = (not utils_mod._CAN_REMOVE_LOCK_FILE)
            finally:
                probe.release()
            if delete_after_release:
                try:
                    path.unlink()                              # Windows: gagal bila sedang dibuka orang lain -> aman
                except OSError:
                    pass

    def session_begin(self) -> bool:
        """
        Tandai sesi INSTANCE ini dimulai. True bila ada sesi lain yang berakhir tidak normal (crash / dimatikan paksa).
        Tiap instance punya file sesi sendiri yang DIKUNCI selama hidup, jadi: beberapa instance tidak saling menimpa
        penanda, instance lain yang masih hidup tidak pernah dianggap crash, dan pemakaian ulang PID tidak
        menipu (yang dinilai adalah kunci, bukan PID). Penanda yang sudah dikonsumsi diberi tanda CLEAN dan
        TIDAK dihapus manual setelah lock dilepas (POSIX: dihapus oleh release() selagi terkunci; Windows: dibiarkan
        lalu dibersihkan lewat _cleanup_session_markers).
        """
        crashed = False
        legacy = self._read_json(self.session_path)               # migrasi session.json bersama (<= v1.9.1)
        if isinstance(legacy, dict):
            owner_alive = False
            if legacy.get("clean") is False:
                try:
                    owner_alive = _pid_alive(int(legacy.get("pid", 0)))
                except (TypeError, ValueError):
                    owner_alive = False
                crashed = crashed or not owner_alive
            if not owner_alive:
                try:
                    self.session_path.unlink(missing_ok=True)      # bukan file lock: aman dihapus
                except OSError:
                    pass
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            candidates = sorted(self.sessions_dir.glob("*.session"))
        except OSError as e:
            log.warning("Folder sesi tidak bisa dipakai: %s", e)
            return crashed
        own = self._session_lock.path if self._session_lock else None
        for path in candidates:
            if own is not None and path == own:
                continue
            probe = InterProcessLock(path, remove_on_release=True)
            try:
                # JANGAN menimpa isi (kita perlu membacanya) dan JANGAN membuat file (yang sudah dihapus pihak lain
                # tidak boleh dihidupkan lagi sebagai file kosong).
                if not probe.acquire(write_info=False, create=False):
                    continue                                       # pemiliknya masih hidup / file sudah tiada
            except OSError:
                continue
            try:
                if self._session_crashed(path):
                    crashed = True
                probe.set_info("CLEAN\n")                          # sudah 'dikonsumsi': tidak dilaporkan dua kali
            finally:
                probe.release()                                    # tidak ada unlink manual setelah ini
        self._cleanup_session_markers()
        if self._session_lock is None:
            base = f"{os.getpid()}-{SESSION_ID}"
            for attempt in range(5):                               # nama bentrok / terkunci pemindai -> coba nama lain
                name = base if attempt == 0 else f"{base}-{uuid.uuid4().hex[:4]}"
                lk = InterProcessLock(self.sessions_dir / f"{name}.session", remove_on_release=True)
                try:
                    if lk.acquire():
                        self._session_lock = lk
                        break
                except OSError as e:
                    log.warning("Penanda sesi tidak bisa dibuat: %s", e)
                    break
            else:
                log.warning("Penanda sesi tidak bisa diambil setelah beberapa percobaan")
        return crashed

    def session_end(self) -> None:
        """Tandai penutupan NORMAL sesi instance ini (CLEAN). File tidak dihapus manual setelah lock dilepas."""
        lk, self._session_lock = self._session_lock, None
        if lk is None:
            return
        lk.set_info("CLEAN\n")
        lk.release()


# Dipecah ke modul sendiri di v1.9.7; nama lama tetap bisa diimpor dari sini (fasad kompatibilitas).
from .settings_store import SettingsStore  # noqa: E402,F401
from .translation_cache import TranslationCache  # noqa: E402,F401
