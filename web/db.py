"""
Persistent cache for GardePro media metadata, thumbnails, and full files.
DB:     ~/.gardepro/cache.db
Thumbs: ~/.gardepro/thumbs/{id}_{kind}.jpg
Files:  ~/.gardepro/files/{id}_{kind}
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

CACHE_DIR = Path.home() / ".gardepro"
THUMB_DIR = CACHE_DIR / "thumbs"
FILES_DIR = CACHE_DIR / "files"
DB_PATH   = CACHE_DIR / "cache.db"

_DDL = """
CREATE TABLE IF NOT EXISTS media (
    id            INTEGER NOT NULL,
    kind          TEXT    NOT NULL,
    thumb_cached  INTEGER NOT NULL DEFAULT 0,
    thumb_path    TEXT,
    file_cached   INTEGER NOT NULL DEFAULT 0,
    file_path     TEXT,
    analyzed      INTEGER NOT NULL DEFAULT 0,
    analysis_json TEXT,
    PRIMARY KEY (id, kind)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class CacheDB:
    def __init__(self):
        self._conn: Optional[sqlite3.Connection] = None

    def open(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(DB_PATH), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        # Migrate existing DBs that predate file-caching columns
        for col, defn in [("file_cached", "INTEGER NOT NULL DEFAULT 0"),
                          ("file_path",   "TEXT")]:
            try:
                self._conn.execute(f"ALTER TABLE media ADD COLUMN {col} {defn}")
            except Exception:
                pass  # already exists

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def get_all_media(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, kind, thumb_cached, thumb_path FROM media ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_media(self, id: int, kind: str):
        self._conn.execute(
            "INSERT OR IGNORE INTO media (id, kind) VALUES (?, ?)", (id, kind)
        )

    def mark_thumb_cached(self, id: int, kind: str, path: str):
        self._conn.execute(
            "UPDATE media SET thumb_cached=1, thumb_path=? WHERE id=? AND kind=?",
            (path, id, kind),
        )

    def mark_file_cached(self, id: int, kind: str, path: str):
        self._conn.execute(
            "UPDATE media SET file_cached=1, file_path=? WHERE id=? AND kind=?",
            (path, id, kind),
        )

    def delete_media(self, id: int, kind: str):
        row = self._conn.execute(
            "SELECT thumb_path, file_path FROM media WHERE id=? AND kind=?", (id, kind)
        ).fetchone()
        if row:
            if row["thumb_path"]: Path(row["thumb_path"]).unlink(missing_ok=True)
            if row["file_path"]:  Path(row["file_path"]).unlink(missing_ok=True)
        self._conn.execute("DELETE FROM media WHERE id=? AND kind=?", (id, kind))

    def get_uncached_thumbs(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, kind FROM media WHERE thumb_cached=0"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_last_synced(self):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_synced', ?)",
            (ts,),
        )

    def get_last_synced(self) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='last_synced'"
        ).fetchone()
        return row["value"] if row else None
