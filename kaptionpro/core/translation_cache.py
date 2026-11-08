"""TranslationCache: cache terjemahan permanen (SQLite) dengan retensi."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path

from ..config import log


class TranslationCache:
    """Cache terjemahan permanen (SQLite). Bila gagal dibuka, cache dimatikan (hanya dicatat di log)."""

    BUSY_TIMEOUT = 5.0           # detik menunggu database yang dikunci proses lain sebelum menyerah (hanya cache)
    MAX_AGE_DAYS = 180           # entri yang TIDAK DIPAKAI selama ini dianggap basi (ts = terakhir dipakai)
    MAX_ENTRIES = 200_000        # batas jumlah entri; kelebihannya (yang paling lama tak dipakai) dihapus
    TOUCH_INTERVAL = 86400.0     # penanda 'terakhir dipakai' diperbarui paling sering sekali per hari per entri
    VACUUM_MIN_REMOVED = 5000    # DELETE tidak mengecilkan file; VACUUM hanya bila pemangkasan cukup besar

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), check_same_thread=False, timeout=self.BUSY_TIMEOUT)
            conn.execute("CREATE TABLE IF NOT EXISTS tr (k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tr_ts ON tr(ts)")      # untuk prune() berdasarkan umur
            conn.commit()
            self._conn = conn
        except (sqlite3.Error, OSError) as e:
            log.warning("Translation cache dimatikan: %s", e)

    def prune(self, max_age_days: float | None = None, max_entries: int | None = None) -> int:
        """
        Retensi cache berdasarkan 'terakhir dipakai' (ts diperbarui saat entri ditulis ATAU dibaca): hapus entri yang
        tak dipakai lebih dari `max_age_days`, lalu pangkas yang paling lama tak dipakai bila jumlahnya melebihi
        `max_entries`. Pemangkasan besar diikuti VACUUM agar ukuran file benar-benar mengecil.
        Cache hanya mempercepat; entri yang terhapus cukup diterjemahkan ulang. Return jumlah terhapus.
        """
        age = self.MAX_AGE_DAYS if max_age_days is None else max_age_days
        cap = self.MAX_ENTRIES if max_entries is None else max_entries
        removed = 0
        with self._lock:
            conn = self._conn
            if conn is None:
                return 0
            try:
                removed += conn.execute("DELETE FROM tr WHERE ts < ?", (time.time() - age * 86400.0,)).rowcount
                total = int(conn.execute("SELECT COUNT(*) FROM tr").fetchone()[0])
                if total > cap:
                    removed += conn.execute("DELETE FROM tr WHERE k IN (SELECT k FROM tr ORDER BY ts ASC LIMIT ?)",
                                            (total - cap,)).rowcount
                conn.commit()
            except sqlite3.Error as e:
                log.warning("Cache prune gagal: %s", e)
                return 0
        if removed:
            log.info("Cache terjemahan dipangkas: %d entri dihapus", removed)
        if removed >= self.VACUUM_MIN_REMOVED:
            self.vacuum()
        return removed

    def vacuum(self) -> bool:
        """Kecilkan ukuran file setelah banyak penghapusan. Butuh kunci database eksklusif sesaat: bila dipakai instance
        lain, dilewati (False) -- tidak apa-apa, dicoba lagi pada pemangkasan berikutnya."""
        with self._lock:
            conn = self._conn
            if conn is None:
                return False
            try:
                conn.commit()
                conn.execute("VACUUM")
                return True
            except sqlite3.Error as e:
                log.warning("Cache VACUUM dilewati: %s", e)
                return False

    def size_bytes(self) -> int:
        """Ukuran file database di disk (0 bila tidak ada)."""
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def close(self) -> None:
        """Tutup koneksi SQLite (dipanggil saat aplikasi ditutup). Aman dipanggil berulang; cache lalu nonaktif."""
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as e:
                log.warning("Cache close gagal: %s", e)

    @staticmethod
    def key(backend: str, src: str, tgt: str, text: str) -> str:
        return hashlib.sha1(f"{backend}\x1f{src}\x1f{tgt}\x1f{text}".encode("utf-8")).hexdigest()

    def get_many(self, keys: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        if not keys:
            return out
        with self._lock:
            conn = self._conn                          # DIBACA di dalam lock: close() bisa terjadi sebelum kita masuk
            if conn is None:
                return out
            try:
                for i in range(0, len(keys), 500):
                    chunk = keys[i:i + 500]
                    q = "SELECT k, v FROM tr WHERE k IN (%s)" % ",".join("?" * len(chunk))
                    out.update(conn.execute(q, chunk).fetchall())
                self._touch(conn, list(out))
            except sqlite3.Error as e:
                log.warning("Cache read gagal: %s", e)
        return out

    def _touch(self, conn: sqlite3.Connection, hit_keys: list[str]) -> None:
        """Tandai entri yang BARU DIPAKAI (ts = terakhir dipakai), paling sering sekali per TOUCH_INTERVAL per entri
        supaya pembacaan biasa hampir tidak menimbulkan penulisan. Pemanggil memegang lock. Kegagalan tidak fatal."""
        if not hit_keys:
            return
        now = time.time()
        for i in range(0, len(hit_keys), 500):
            chunk = hit_keys[i:i + 500]
            conn.execute("UPDATE tr SET ts = ? WHERE ts < ? AND k IN (%s)" % ",".join("?" * len(chunk)),
                         [now, now - self.TOUCH_INTERVAL, *chunk])
        conn.commit()

    def put_many(self, items: dict[str, str]) -> None:
        if not items:
            return
        now = time.time()
        with self._lock:
            conn = self._conn
            if conn is None:
                return
            try:
                conn.executemany("INSERT OR REPLACE INTO tr (k, v, ts) VALUES (?, ?, ?)",
                                 [(k, v, now) for k, v in items.items()])
                conn.commit()
            except sqlite3.Error as e:
                log.warning("Cache write gagal: %s", e)

    def count(self) -> int:
        with self._lock:
            conn = self._conn
            if conn is None:
                return 0
            try:
                return int(conn.execute("SELECT COUNT(*) FROM tr").fetchone()[0])
            except sqlite3.Error:
                return 0

    def clear(self) -> None:
        with self._lock:
            conn = self._conn
            if conn is None:
                return
            try:
                conn.execute("DELETE FROM tr")
                conn.commit()
            except sqlite3.Error as e:
                log.warning("Cache clear gagal: %s", e)
