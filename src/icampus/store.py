"""SQLite store. The synced records can be rebuilt by syncing again; settings, the cache index, STT providers,
jobs, transcripts and the video download queue (later schema versions) cannot, so migrations only ever add to the
file.

Records live in scopes (e.g. dataset "lectures", scope "course:1001"). A scope is only
replaced after it synced successfully, so one failing course never hides another's data.
Rows that disappear are marked inactive, never deleted.
"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    dataset TEXT NOT NULL, key TEXT NOT NULL, scope TEXT NOT NULL, course_id TEXT,
    due_at TEXT, data TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (dataset, key));
CREATE INDEX IF NOT EXISTS records_scope ON records(dataset, scope, active);
CREATE TABLE IF NOT EXISTS scopes (
    dataset TEXT NOT NULL, scope TEXT NOT NULL, synced_at TEXT NOT NULL, count INTEGER NOT NULL,
    PRIMARY KEY (dataset, scope));
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, trigger TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
    status TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Version 2: runtime settings changed in the admin UI (they override the ICAMPUS_* defaults) and the index of every
# file kept under var/ (videos, audio, attachments, transcripts, frames). The index can be rebuilt from the sidecar
# files beside the downloads (cache.py), except for pins.
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cache_entries (
    kind TEXT NOT NULL, key TEXT NOT NULL, course_id TEXT, item_id TEXT, title TEXT,
    size INTEGER NOT NULL DEFAULT 0, files TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, last_used TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, key));
CREATE INDEX IF NOT EXISTS cache_lru ON cache_entries(kind, last_used);
"""

# Version 3: speech-to-text providers and what they cost, the transcription job queue, and transcripts with their
# segments. The full-text table over segments is made by transcripts.py, because it needs an SQLite with FTS5 and
# the trigram tokenizer and the server must start without one.
SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS stt_providers (
    id TEXT PRIMARY KEY, config TEXT NOT NULL, health TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stt_usage (
    id INTEGER PRIMARY KEY, provider_id TEXT NOT NULL, at TEXT NOT NULL, audio_seconds REAL NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'transcribe', job_id INTEGER, item_id TEXT);
CREATE INDEX IF NOT EXISTS stt_usage_at ON stt_usage(provider_id, at);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, asset_id TEXT NOT NULL, course_id TEXT, title TEXT,
    video_version TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', language TEXT,
    state TEXT NOT NULL, retryable INTEGER NOT NULL DEFAULT 0, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    not_before TEXT, progress TEXT NOT NULL DEFAULT '{}', stages TEXT NOT NULL DEFAULT '{}',
    provider_id TEXT, reason TEXT, transcript_id INTEGER, hours REAL, priority INTEGER NOT NULL DEFAULT 0,
    trigger TEXT NOT NULL DEFAULT 'api', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    started_at TEXT, finished_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active ON jobs(asset_id, video_version, model)
    WHERE state IN ('queued', 'downloading', 'extracting', 'transcribing', 'paused_budget');
CREATE INDEX IF NOT EXISTS jobs_item ON jobs(item_id);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, asset_id TEXT NOT NULL, course_id TEXT, title TEXT,
    video_version TEXT NOT NULL DEFAULT '', provider_id TEXT NOT NULL, model TEXT, language TEXT,
    duration_s REAL, chars INTEGER NOT NULL DEFAULT 0, segment_count INTEGER NOT NULL DEFAULT 0,
    timestamps TEXT NOT NULL DEFAULT 'segment', status TEXT NOT NULL DEFAULT 'ready',
    preferred INTEGER NOT NULL DEFAULT 0, reason TEXT, job_id INTEGER, cost_usd REAL, path TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS transcripts_item ON transcripts(item_id);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY, transcript_id INTEGER NOT NULL, idx INTEGER NOT NULL,
    start_s REAL NOT NULL, end_s REAL NOT NULL, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS segments_transcript ON segments(transcript_id, idx);
"""

# Version 4: the queue of lecture video downloads (videos.py). One row per video that is waiting, downloading or
# failed; a finished download's row goes, because the cache index is what says a video is here. `lecture` is what
# the download needs from the synced row (ids, title, the Commons asset), never an address with a query string.
SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS video_jobs (
    id INTEGER PRIMARY KEY, asset_id TEXT NOT NULL UNIQUE, item_id TEXT, lecture TEXT NOT NULL,
    state TEXT NOT NULL, keep_trying INTEGER NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0,
    not_before TEXT, error TEXT, transient INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER, downloaded_bytes INTEGER NOT NULL DEFAULT 0, part_version TEXT, validator TEXT, boot TEXT,
    queued_at TEXT NOT NULL, started_at TEXT, last_progress_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS video_jobs_queue ON video_jobs(state, queued_at, id);
"""

# One script per schema version, applied in order; PRAGMA user_version records how far a file has come. Every
# statement is safe to repeat (IF NOT EXISTS) and nothing is ever dropped: version 1 of this program set
# user_version back to 1 at each start, so after a downgrade and a second upgrade the scripts run again.
MIGRATIONS = [SCHEMA, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4]


@dataclass(frozen=True)
class Record:
    key: str
    course_id: str | None
    due_at: datetime | None
    data: dict[str, Any]


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self.upgraded_from = self._migrate()

    def _migrate(self) -> int | None:
        """Brings the file to the newest schema in place. Returns the version it had when that was older (0 for
        a new file), or None when nothing changed."""
        had = self._db.execute("PRAGMA user_version").fetchone()[0]
        if had >= len(MIGRATIONS):
            return None
        for version, script in enumerate(MIGRATIONS, 1):
            if version > had or version == 1:
                self._db.executescript(script)
        self._db.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")
        return had

    @property
    def schema_version(self) -> int:
        return self.one("PRAGMA user_version")[0]

    def close(self) -> None:
        self._db.close()

    # --- plain SQL for the modules that own their tables (cache, settings, stt, jobs, transcripts) ---

    def execute(self, sql: str, args: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    # Reads take the lock too. One connection is shared, and the sqlite3 module reuses prepared statements, so
    # two threads running the same query at once fail with "API misuse". Nearly everything runs on the event
    # loop's thread, but a single plain-function FastAPI dependency (they run in a thread pool) is enough.

    def query(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple | list = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    @contextmanager
    def transaction(self):
        """Several statements as one change: all of them, or none."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    # --- records ---------------------------------------------------------

    def replace_scope(self, dataset: str, scope: str, records: list[Record], now: datetime) -> tuple[int, int]:
        """Upsert a scope's records and deactivate the ones no longer present."""
        ts = now.isoformat()
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for r in records:
                    self._db.execute(
                        """INSERT INTO records (dataset, key, scope, course_id, due_at, data, first_seen, last_seen, active)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                           ON CONFLICT (dataset, key) DO UPDATE SET scope=excluded.scope,
                             course_id=excluded.course_id, due_at=excluded.due_at, data=excluded.data,
                             last_seen=excluded.last_seen, active=1""",
                        (dataset, r.key, scope, r.course_id, r.due_at.isoformat() if r.due_at else None,
                         json.dumps(r.data, ensure_ascii=False), ts, ts),
                    )
                keys = [r.key for r in records]
                marks = ",".join("?" * len(keys))
                gone = self._db.execute(
                    f"UPDATE records SET active=0 WHERE dataset=? AND scope=? AND active=1"
                    + (f" AND key NOT IN ({marks})" if keys else ""),
                    (dataset, scope, *keys),
                ).rowcount
                self._db.execute(
                    "INSERT OR REPLACE INTO scopes (dataset, scope, synced_at, count) VALUES (?, ?, ?, ?)",
                    (dataset, scope, ts, len(records)),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return len(records), gone

    def retire_scopes(self, dataset: str, keep: set[str]) -> None:
        """Drop scopes that are no longer collected (e.g. last term's courses)."""
        with self._lock:
            for row in self._db.execute("SELECT scope FROM scopes WHERE dataset=?", (dataset,)).fetchall():
                if row["scope"] not in keep:
                    self._db.execute("UPDATE records SET active=0 WHERE dataset=? AND scope=?", (dataset, row["scope"]))
                    self._db.execute("DELETE FROM scopes WHERE dataset=? AND scope=?", (dataset, row["scope"]))

    def records(self, dataset: str, *, course_ids: set[str] | None = None,
                include_inactive: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM records WHERE dataset=?"
        if not include_inactive:
            sql += " AND active=1"
        rows = self.query(sql + " ORDER BY due_at IS NULL, due_at, key", (dataset,))
        return [self._row(r) for r in rows if course_ids is None or r["course_id"] in course_ids]

    def record(self, dataset: str, key: str) -> dict[str, Any] | None:
        row = self.one("SELECT * FROM records WHERE dataset=? AND key=?", (dataset, key))
        return self._row(row) if row else None

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {**json.loads(row["data"]), "active": bool(row["active"]),
                "first_seen": row["first_seen"], "last_seen": row["last_seen"]}

    # --- freshness -------------------------------------------------------

    def scopes(self, dataset: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM scopes", ()
        if dataset:
            sql, args = sql + " WHERE dataset=?", (dataset,)
        return [dict(r) for r in self.query(sql + " ORDER BY dataset, scope", args)]

    def synced_at(self, dataset: str, expected: list[str] | None = None) -> str | None:
        """Oldest successful sync among the dataset's scopes (what a caller can rely on).
        With `expected`, a scope that never synced makes the whole dataset count as not synced."""
        rows = {r["scope"]: r["synced_at"] for r in
                self.query("SELECT scope, synced_at FROM scopes WHERE dataset=?", (dataset,))}
        if expected is not None:
            if any(s not in rows for s in expected):
                return None
            rows = {s: rows[s] for s in expected}
        return min(rows.values()) if rows else None

    # --- runs ------------------------------------------------------------

    def start_run(self, trigger: str, now: datetime) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO runs (trigger, started_at, status) VALUES (?, ?, 'running')",
                                   (trigger, now.isoformat()))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, detail: dict[str, Any], now: datetime) -> None:
        with self._lock:
            self._db.execute("UPDATE runs SET status=?, detail=?, finished_at=? WHERE id=?",
                             (status, json.dumps(detail, ensure_ascii=False), now.isoformat(), run_id))

    def mark_interrupted(self) -> None:
        with self._lock:
            self._db.execute("UPDATE runs SET status='interrupted' WHERE status='running'")

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None} for r in rows]

    # --- small key/value state --------------------------------------------

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM state WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                             (key, json.dumps(value, ensure_ascii=False)))
