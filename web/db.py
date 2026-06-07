"""
Persistent cache for GardePro media metadata, thumbnails, and full files.
DB:     ~/.gardepro/cache.db
Thumbs: ~/.gardepro/thumbs/{id}_{kind}.jpg
Files:  ~/.gardepro/files/{id}_{kind}
Saved:  ~/.gardepro/saved/{timestamp}_{id}_{kind}[_thumb].jpg
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

CACHE_DIR = Path.home() / ".gardepro"
THUMB_DIR = CACHE_DIR / "thumbs"
FILES_DIR = CACHE_DIR / "files"
SAVED_DIR = CACHE_DIR / "saved"
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
    captured_at   TEXT,
    PRIMARY KEY (id, kind)
);
CREATE TABLE IF NOT EXISTS saved_media (
    saved_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    cam_id        INTEGER NOT NULL,
    kind          TEXT    NOT NULL,
    saved_at      TEXT    NOT NULL,
    thumb_path    TEXT,
    file_path     TEXT,
    analyzed      INTEGER NOT NULL DEFAULT 0,
    analysis_json TEXT
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
        SAVED_DIR.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(DB_PATH), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        # Migrate existing DBs that predate newer columns
        for col, defn in [("file_cached",    "INTEGER NOT NULL DEFAULT 0"),
                          ("file_path",      "TEXT"),
                          ("captured_at",    "TEXT"),
                          ("pending_delete", "INTEGER NOT NULL DEFAULT 0")]:
            try:
                self._conn.execute(f"ALTER TABLE media ADD COLUMN {col} {defn}")
            except Exception:
                pass  # already exists
        for col, defn in [("analyzed",      "INTEGER NOT NULL DEFAULT 0"),
                          ("analysis_json", "TEXT")]:
            try:
                self._conn.execute(f"ALTER TABLE saved_media ADD COLUMN {col} {defn}")
            except Exception:
                pass  # already exists

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def get_all_media(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, kind, thumb_cached, thumb_path FROM media"
            " WHERE pending_delete=0 ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_media(self, id: int, kind: str, captured_at: Optional[str] = None):
        # If the camera reports a timestamp that differs from the stored one, the
        # camera has reused this ID for a new file (e.g. after on-camera deletion).
        # Reset all cache flags so the new file gets re-downloaded and re-analyzed.
        if captured_at is not None:
            row = self._conn.execute(
                "SELECT captured_at FROM media WHERE id=? AND kind=?", (id, kind)
            ).fetchone()
            if row and row["captured_at"] and row["captured_at"] != captured_at:
                self._conn.execute(
                    """UPDATE media
                       SET captured_at=?, thumb_cached=0, thumb_path=NULL,
                           file_cached=0, file_path=NULL, analyzed=0, analysis_json=NULL
                       WHERE id=? AND kind=?""",
                    (captured_at, id, kind),
                )
                return
        # New rows: use camera-provided timestamp or fall back to discovery time.
        # Existing rows: only update if camera provides a better (non-null) value.
        self._conn.execute(
            """INSERT INTO media (id, kind, captured_at)
               VALUES (?, ?, COALESCE(?, datetime('now')))
               ON CONFLICT(id, kind) DO UPDATE SET
                 captured_at = COALESCE(?, media.captured_at)""",
            (id, kind, captured_at, captured_at),
        )

    def get_last_event_time(self) -> Optional[str]:
        row = self._conn.execute(
            "SELECT MAX(captured_at) AS t FROM media"
        ).fetchone()
        return row["t"] if row else None

    def get_max_media_id(self) -> Optional[int]:
        row = self._conn.execute("SELECT MAX(id) AS m FROM media").fetchone()
        return row["m"] if row else None

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

    def mark_for_deletion(self, id: int, kind: str):
        self._conn.execute(
            "UPDATE media SET pending_delete=1 WHERE id=? AND kind=?", (id, kind)
        )

    def get_pending_deletions(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT id, kind FROM media WHERE pending_delete=1"
        ).fetchall()]

    def get_uncached_thumbs(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, kind FROM media WHERE thumb_cached=0 AND pending_delete=0"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_unanalyzed_media(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, kind, thumb_path FROM media"
            " WHERE analyzed=0 AND thumb_cached=1 AND pending_delete=0"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_analysis(self, id: int, kind: str, analysis_json: str) -> None:
        self._conn.execute(
            "UPDATE media SET analyzed=1, analysis_json=? WHERE id=? AND kind=?",
            (analysis_json, id, kind),
        )

    def get_media_with_analysis(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, kind, analysis_json FROM media WHERE analyzed=1"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Saved media ───────────────────────────────────────────────────────────

    def save_media(self, cam_id: int, kind: str, saved_at: str,
                   thumb_path: str, file_path: str) -> int:
        cur = self._conn.execute(
            """INSERT INTO saved_media (cam_id, kind, saved_at, thumb_path, file_path)
               VALUES (?, ?, ?, ?, ?)""",
            (cam_id, kind, saved_at, thumb_path, file_path),
        )
        return cur.lastrowid

    def get_saved_media(self) -> list[dict]:
        rows = self._conn.execute(
            """SELECT saved_id, cam_id, kind, saved_at, thumb_path, file_path,
                      analyzed, analysis_json
               FROM saved_media ORDER BY saved_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_saved_by_id(self, saved_id: int) -> Optional[dict]:
        row = self._conn.execute(
            """SELECT saved_id, cam_id, kind, saved_at, thumb_path, file_path,
                      analyzed, analysis_json
               FROM saved_media WHERE saved_id=?""",
            (saved_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_saved_analysis(self, saved_id: int, analysis_json: str) -> None:
        self._conn.execute(
            "UPDATE saved_media SET analyzed=1, analysis_json=? WHERE saved_id=?",
            (analysis_json, saved_id),
        )

    def delete_saved(self, saved_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT thumb_path, file_path FROM saved_media WHERE saved_id=?", (saved_id,)
        ).fetchone()
        if not row:
            return None
        self._conn.execute("DELETE FROM saved_media WHERE saved_id=?", (saved_id,))
        return dict(row)

    # ── Meta ──────────────────────────────────────────────────────────────────

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
