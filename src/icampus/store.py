"""SQLite cache. Everything here can be rebuilt by syncing again.

Records live in scopes (e.g. dataset "lectures", scope "course:1001"). A scope is only
replaced after it synced successfully, so one failing course never hides another's data.
Rows that disappear are marked inactive, never deleted.
"""

import json
import sqlite3
import threading
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
PRAGMA user_version = 1;
"""


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
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

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
        rows = self._db.execute(sql + " ORDER BY due_at IS NULL, due_at, key", (dataset,)).fetchall()
        return [self._row(r) for r in rows if course_ids is None or r["course_id"] in course_ids]

    def record(self, dataset: str, key: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM records WHERE dataset=? AND key=?", (dataset, key)).fetchone()
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
        return [dict(r) for r in self._db.execute(sql + " ORDER BY dataset, scope", args)]

    def synced_at(self, dataset: str, expected: list[str] | None = None) -> str | None:
        """Oldest successful sync among the dataset's scopes (what a caller can rely on).
        With `expected`, a scope that never synced makes the whole dataset count as not synced."""
        rows = {r["scope"]: r["synced_at"] for r in
                self._db.execute("SELECT scope, synced_at FROM scopes WHERE dataset=?", (dataset,))}
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
        rows = self._db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None} for r in rows]

    # --- small key/value state --------------------------------------------

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                             (key, json.dumps(value, ensure_ascii=False)))
