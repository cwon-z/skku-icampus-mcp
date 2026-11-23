"""One index over every file the server keeps under var/: lecture videos, the audio cut from them, announcement and
assignment attachments, transcripts and (later) slide frames.

Each kind has a cap, and all of them together have another (cache_total_gb). Past a cap the least recently used
entries go first. Three things are never removed to make room: an entry you pinned, an entry a queued or running
job still needs (the job queue says which), and transcripts, which cost money to make.

The index lives in SQLite (table cache_entries). The files themselves, and the JSON sidecar a video or attachment
has always had beside it, stay where earlier versions put them, so an upgrade adopts what is on disk instead of
downloading it again, and a lost index can be rebuilt (pins excepted). Nothing here talks to iCampus: it only
looks at the disk.
"""

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import KST
from .store import Store

log = logging.getLogger(__name__)
KINDS = ("video", "audio", "attachment", "transcript", "frames")
# The setting that caps each kind, in GB. Transcripts have none: they are never removed automatically.
CAPS = {"video": "video_cache_gb", "audio": "audio_cache_gb", "attachment": "attachment_cache_gb",
        "frames": "frames_cache_gb"}
GB = 2**30
Ref = tuple[str, str]  # (kind, key)


class CacheFull(Exception):
    """There is no room, and nothing left that may be removed to make some."""


@dataclass
class Entry:
    kind: str
    key: str
    files: list[str]  # relative to the data directory; the first is the main file
    size: int = 0
    course_id: str | None = None
    item_id: str | None = None
    title: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    last_used: str = ""
    pinned: bool = False


def _now() -> str:
    return datetime.now(KST).isoformat()


def _size(path: Path) -> int:
    try:
        if path.is_dir():
            return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        return path.stat().st_size
    except OSError:
        return 0


class Cache:
    def __init__(self, settings, store: Store):
        self.settings = settings
        self.store = store
        self.root = Path(settings.data_dir)
        self._in_use: Callable[[], set[Ref]] = set
        self._on_delete: dict[str, Callable[[Entry], None]] = {}
        self._adopters: list[Callable[[], None]] = []

    # --- wiring ------------------------------------------------------------

    def protect(self, in_use: Callable[[], set[Ref]]) -> None:
        """The job queue names the entries its queued and running jobs still need."""
        self._in_use = in_use

    def on_delete(self, kind: str, hook: Callable[[Entry], None]) -> None:
        """Called before an entry of this kind goes, so its owner can drop what hangs off it (a transcript's rows)."""
        self._on_delete[kind] = hook

    def adopter(self, adopt: Callable[[], None]) -> None:
        """A kind's way of indexing files that are on disk without a row (from an earlier version, or a lost index)."""
        self._adopters.append(adopt)

    # --- entries -----------------------------------------------------------

    def _entry(self, row) -> Entry:
        return Entry(kind=row["kind"], key=row["key"], files=json.loads(row["files"]), size=row["size"],
                     course_id=row["course_id"], item_id=row["item_id"], title=row["title"],
                     meta=json.loads(row["meta"]), created_at=row["created_at"], last_used=row["last_used"],
                     pinned=bool(row["pinned"]))

    def path(self, entry: Entry) -> Path:
        return self.root / entry.files[0]

    def get(self, kind: str, key: str) -> Entry | None:
        """The entry, if its main file is still there. A row whose file has gone is dropped."""
        row = self.store.one("SELECT * FROM cache_entries WHERE kind=? AND key=?", (kind, key))
        if row is None:
            return None
        entry = self._entry(row)
        if not self.path(entry).exists():
            self.store.execute("DELETE FROM cache_entries WHERE kind=? AND key=?", (kind, key))
            return None
        return entry

    def put(self, kind: str, key: str, files: Iterable[Path], *, course_id: str | None = None,
            item_id: str | None = None, title: str | None = None, meta: dict | None = None,
            last_used: str | None = None, created_at: str | None = None) -> Entry:
        """Indexes files that are already written. Replacing an entry keeps its pin."""
        if kind not in KINDS:
            raise ValueError(f"unknown cache kind {kind!r}")
        paths = [Path(p) for p in files]
        relative = [str(p.resolve().relative_to(self.root.resolve())) for p in paths]  # ValueError outside var/
        now = _now()
        size = sum(_size(p) for p in paths)
        self.store.execute(
            """INSERT INTO cache_entries (kind, key, course_id, item_id, title, size, files, meta, created_at, last_used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (kind, key) DO UPDATE SET course_id=excluded.course_id, item_id=excluded.item_id,
                 title=excluded.title, size=excluded.size, files=excluded.files, meta=excluded.meta,
                 created_at=excluded.created_at, last_used=excluded.last_used""",
            (kind, key, course_id, item_id, title, size, json.dumps(relative),
             json.dumps(meta or {}, ensure_ascii=False), created_at or now, last_used or now))
        return self.get(kind, key)

    def entries(self, kind: str | None = None, *, course_ids: set[str] | None = None,
             pinned: bool | None = None) -> list[Entry]:
        """What is on disk according to the index, most recently used first."""
        sql, args = "SELECT * FROM cache_entries WHERE 1=1", []
        if kind:
            sql, args = sql + " AND kind=?", [kind]
        if pinned is not None:
            sql, args = sql + " AND pinned=?", [*args, int(pinned)]
        rows = self.store.query(sql + " ORDER BY last_used DESC, kind, key", args)
        return [self._entry(r) for r in rows if course_ids is None or r["course_id"] in course_ids]

    def touch(self, kind: str, key: str) -> None:
        self.store.execute("UPDATE cache_entries SET last_used=? WHERE kind=? AND key=?", (_now(), kind, key))

    def pin(self, kind: str, key: str, pinned: bool = True) -> bool:
        cur = self.store.execute("UPDATE cache_entries SET pinned=? WHERE kind=? AND key=?", (int(pinned), kind, key))
        return cur.rowcount > 0

    def delete(self, kind: str, key: str) -> bool:
        """Removes the entry and its files. The row goes first, so a half-deleted entry never looks finished."""
        row = self.store.one("SELECT * FROM cache_entries WHERE kind=? AND key=?", (kind, key))
        if row is None:
            return False
        entry = self._entry(row)
        if hook := self._on_delete.get(kind):
            hook(entry)
        self.store.execute("DELETE FROM cache_entries WHERE kind=? AND key=?", (kind, key))
        root = self.root.resolve()
        for name in reversed(entry.files):  # the main file last
            path = (self.root / name).resolve()
            if root not in path.parents:
                continue  # never outside the data directory, whatever the row says
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        return True

    # --- sizes and caps ----------------------------------------------------

    def cap(self, kind: str | None = None) -> int | None:
        """The cap in bytes for a kind, or for everything together; None when the kind has no cap."""
        name = CAPS.get(kind) if kind else "cache_total_gb"
        return int(getattr(self.settings, name) * GB) if name else None

    def used(self, kind: str | None = None) -> int:
        sql, args = "SELECT COALESCE(SUM(size), 0) AS n FROM cache_entries", ()
        if kind:
            sql, args = sql + " WHERE kind=?", (kind,)
        return self.store.one(sql, args)["n"]

    def totals(self) -> dict[str, dict]:
        """Per kind and for all of them: how many entries, bytes used, the cap, and bytes that may not be evicted."""
        held = self._in_use()
        out = {k: {"count": 0, "bytes": 0, "cap_bytes": self.cap(k), "pinned_bytes": 0, "in_use_bytes": 0}
               for k in KINDS}
        for row in self.store.query("SELECT kind, key, size, pinned FROM cache_entries"):
            t = out.setdefault(row["kind"], {"count": 0, "bytes": 0, "cap_bytes": None, "pinned_bytes": 0,
                                             "in_use_bytes": 0})
            t["count"] += 1
            t["bytes"] += row["size"]
            if row["pinned"]:
                t["pinned_bytes"] += row["size"]
            elif (row["kind"], row["key"]) in held:
                t["in_use_bytes"] += row["size"]
        out["all"] = {"count": sum(t["count"] for t in out.values()), "bytes": sum(t["bytes"] for t in out.values()),
                      "cap_bytes": self.cap(), "pinned_bytes": sum(t["pinned_bytes"] for t in out.values()),
                      "in_use_bytes": sum(t["in_use_bytes"] for t in out.values())}
        return out

    def protected(self) -> set[Ref]:
        """The entries queued and running jobs still need."""
        return self._in_use()

    def state(self, entry: Entry, held: set[Ref] | None = None) -> str:
        """Why an entry would survive eviction: pinned, in use (a job needs it), kept (a transcript), or nothing."""
        if entry.pinned:
            return "pinned"
        if (entry.kind, entry.key) in (self._in_use() if held is None else held):
            return "in use"
        return "kept" if entry.kind not in CAPS else ""

    # --- eviction ----------------------------------------------------------

    def _victims(self, kind: str | None, keep: set[Ref]) -> list[Entry]:
        """Entries that may be removed, least recently used first."""
        held = self._in_use() | keep
        rows = self.store.query("SELECT * FROM cache_entries WHERE pinned=0" + (" AND kind=?" if kind else "")
                                + " ORDER BY last_used, created_at, key", (kind,) if kind else ())
        return [e for e in map(self._entry, rows) if e.kind in CAPS and (e.kind, e.key) not in held]

    def evict(self, kind: str | None, needed: int = 0, keep: Iterable[Ref] = ()) -> bool:
        """Removes the least recently used entries of a kind (or of every kind, for the total) until `needed` more
        bytes fit under the cap. Pinned and in-use entries and transcripts stay. False if it still doesn't fit."""
        cap = self.cap(kind)
        if cap is None:
            return True
        victims = self._victims(kind, set(keep))
        while self.used(kind) + needed > cap and victims:
            old = victims.pop(0)
            log.info("%s cache full: removing %s", kind or "total", old.key)
            self.delete(old.kind, old.key)
        return self.used(kind) + needed <= cap

    def make_room(self, kind: str, needed: int, keep: Iterable[Ref] = ()) -> None:
        """Room for `needed` bytes of this kind, under its own cap and the total. Raises CacheFull when what is
        left is pinned or still needed by a job."""
        keep = set(keep)
        cap = self.cap(kind)
        if cap is not None and needed > cap:
            raise CacheFull(f"{needed / GB:.1f} GB is more than the whole {kind} cache ({cap / GB:g} GB)")
        if not self.evict(kind, needed, keep):
            stuck = self.used(kind)
            raise CacheFull(f"the {kind} cache is full: {stuck / GB:.1f} GB of it is pinned or waiting for a job")
        if not self.evict(None, needed, keep):
            raise CacheFull("the storage cap for everything (cache_total_gb) is reached, and what is left is "
                            "pinned, waiting for a job, or a transcript")

    def enforce(self, keep: Iterable[Ref] = ()) -> None:
        """After something was added whose size wasn't known beforehand, or a cap was lowered: trim every kind and
        the total back under their caps as far as that is allowed."""
        keep = set(keep)
        for kind in CAPS:
            self.evict(kind, 0, keep)
        self.evict(None, 0, keep)

    # --- startup -----------------------------------------------------------

    def reconcile(self) -> None:
        """Brings the index in line with the disk: adopts files that have no row yet, drops rows without files, and
        corrects sizes. Run at startup; reads the disk only."""
        for adopt in self._adopters:
            try:
                adopt()
            except Exception as exc:  # a damaged sidecar must not stop the server
                log.warning("cache: could not adopt existing files (%s)", type(exc).__name__)
        for row in self.store.query("SELECT * FROM cache_entries"):
            entry = self._entry(row)
            if not self.path(entry).exists():
                self.store.execute("DELETE FROM cache_entries WHERE kind=? AND key=?", (entry.kind, entry.key))
                continue
            size = sum(_size(self.root / name) for name in entry.files)
            if size != entry.size:
                self.store.execute("UPDATE cache_entries SET size=? WHERE kind=? AND key=?",
                                   (size, entry.kind, entry.key))
