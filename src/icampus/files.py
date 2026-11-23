"""Attachments: the files synced announcements and assignments point at, downloaded only when asked for.

Only files attached to or linked from an announcement or an assignment description can be fetched, never lecture
items (opening those marks them complete). A download counts as you opening the file in iCampus: course access
reports show it. Download URLs carry a verifier token, so they are looked up fresh each time, used once and never
stored or logged. Downloads go to var/files (0600) and are reused until a sync sees a new version of the file
(for attachment_reuse_hours when it saw none). The cache manager (cache.py) indexes them and removes the least
recently used once they pass attachment_cache_gb. Text is read out of them by a child process (see extract.py).
"""

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .browser import CanvasError, NotLoggedIn, Session, TooLarge, exclusive, open_session
from .cache import Cache
from .config import KST, Settings, child_env
from .dates import from_canvas, iso
from .store import Store

LOCK_WAIT_S = 45  # a sync takes seconds; wait for it rather than fail
EXTRACT_TIMEOUT_S = 90
READER = [sys.executable, "-m", "icampus.extract"]  # a child process, capped in memory by extract.main


class Busy(Exception):
    """A sync holds the browser session."""


class Unavailable(Exception):
    """iCampus won't hand this file over (locked, removed, too large)."""


@dataclass
class Fetched:
    meta: dict  # id, name, content_type, size, updated_at, fetched_at, extract
    text: str
    path: Path  # the downloaded bytes


def find(store: Store, file_id: str) -> dict | None:
    """The synced announcement or assignment that has this file, as a reference to fetch it with."""
    for dataset, title in (("announcements", "title"), ("assignments", "name")):
        for record in store.records(dataset):
            for f in record.get("attachments") or []:
                if isinstance(f, dict) and f.get("id") == file_id:  # older rows hold bare names
                    return {**f, "course_id": record["course_id"], "parent": {
                        "type": dataset.removesuffix("s"), "id": record["id"], "title": record.get(title),
                        "url": record.get("url"), "posted_at": record.get("posted_at")}}
    return None


def _dir(settings: Settings) -> Path:
    path = settings.data_dir / "files"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _write_private(path: Path, data: bytes) -> None:
    """Written beside the target and renamed over it, so a reader never sees half a file."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def peek(settings: Settings, file_id: str) -> Fetched | None:
    """What is on disk for this file, however old, or None. Never asks iCampus: this is what the cache listing
    and the admin UI read."""
    base = settings.data_dir / "files" / file_id
    try:
        meta = json.loads(base.with_suffix(".json").read_text())
        text = base.with_suffix(".txt").read_text()
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or not base.with_suffix(".bin").exists():
        return None
    return Fetched(meta, text, base.with_suffix(".bin"))


def _cached(settings: Settings, ref: dict) -> Fetched | None:
    hit = peek(settings, ref["id"])
    if hit is None:
        return None
    if ref.get("updated_at"):  # the sync saw a version: keep the download until the sync sees another one
        fresh = hit.meta.get("synced_updated_at") == ref["updated_at"]
    else:
        try:
            fetched = datetime.fromisoformat(hit.meta["fetched_at"])
        except (KeyError, ValueError, TypeError):
            return None
        fresh = fetched > datetime.now(KST) - timedelta(hours=settings.attachment_reuse_hours)
    return hit if fresh else None


def _index(cache: Cache, settings: Settings, file_id: str, meta: dict, *, course_id: str | None,
           parent: dict | None, last_used: str | None = None) -> None:
    base = settings.data_dir / "files" / file_id
    cache.put("attachment", file_id, [base.with_suffix(".bin"), base.with_suffix(".txt"), base.with_suffix(".json")],
              course_id=course_id, title=meta.get("name"), created_at=meta.get("fetched_at"), last_used=last_used,
              meta={**{k: meta.get(k) for k in ("name", "content_type", "updated_at", "fetched_at", "extract")},
                    "parent": {k: (parent or {}).get(k) for k in ("type", "id", "title")} if parent else None})


def adopt(cache: Cache, settings: Settings, store: Store) -> None:
    """Indexes attachments that are on disk without a row (downloaded before the cache manager, or after the index
    was lost), with the course and parent the synced records give. Reads the disk and the database only."""
    for sidecar in (settings.data_dir / "files").glob("*.json"):
        file_id = sidecar.stem
        if cache.get("attachment", file_id) is not None or (hit := peek(settings, file_id)) is None:
            continue
        ref = find(store, file_id) or {}
        _index(cache, settings, file_id, hit.meta, course_id=ref.get("course_id"), parent=ref.get("parent"),
               last_used=hit.meta.get("fetched_at"))


async def _extract(path: Path, name: str | None, content_type: str | None) -> tuple[str, str]:
    """(text, how) from the reader child process: a hostile or broken file can only take that process down."""
    args = [str(path), (name or "").replace("\0", ""), (content_type or "").replace("\0", "")]
    env = child_env()  # it needs no password, token or provider key
    proc = await asyncio.create_subprocess_exec(*READER, *args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL, env=env)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), EXTRACT_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return "", f"unreadable: gave up after {EXTRACT_TIMEOUT_S}s"
    try:
        result = json.loads(out)
        return result["text"], result["how"]
    except (ValueError, KeyError, TypeError):
        return "", "unreadable: the reader stopped (out of memory?)"


async def _download(s: Session, ref: dict, max_mb: int) -> tuple[dict, bytes]:
    """Look the file up the way the sync saw it, then download it. Called right after a login check, so a
    refusal here means the file is off limits (hidden, locked, deleted), not that the session ended."""
    max_bytes = max_mb * 2**20
    refused = Unavailable("iCampus won't hand this file over (it may be hidden, locked or deleted)")
    if ref.get("via") == "attached" and ref["parent"]["type"] == "announcement":
        # attachments come with the announcement list (the same request the sync makes, so nothing is marked read)
        posted = from_canvas(ref["parent"]["posted_at"]) or datetime.now(KST)
        raw = await s.canvas("/api/v1/announcements", {
            "context_codes[]": [f"course_{ref['course_id']}"],
            "start_date": (posted.date() - timedelta(days=1)).isoformat(),
            "end_date": (datetime.now(KST).date() + timedelta(days=1)).isoformat()})
        info = next((f for a in raw for f in a.get("attachments") or [] if str(f.get("id")) == ref["id"]), None)
        if info is None:
            raise Unavailable("the announcement no longer has this file")
    else:  # linked from HTML: the Files API describes it (a GET that changes nothing)
        try:
            info = await s.canvas(f"/api/v1/files/{ref['id']}")
        except NotLoggedIn:  # the Canvas API answers 401 for "not allowed" too
            raise refused from None
        except CanvasError as exc:
            if exc.status in (403, 404):
                raise refused from None
            raise
    if info.get("locked_for_user") or not info.get("url"):
        raise Unavailable("iCampus has this file locked for you")
    if (info.get("size") or 0) > max_bytes:
        raise Unavailable(f"the file is {info['size'] / 2**20:.0f} MB; only files up to "
                          f"{max_mb} MB are downloaded")
    try:
        data = await s.download(info["url"], max_bytes)
    except TooLarge:
        raise Unavailable(f"the file is over {max_mb} MB; bigger files aren't downloaded") from None
    except CanvasError as exc:
        if exc.status in (401, 403, 404):
            raise refused from None
        raise
    return {"id": ref["id"], "name": info.get("display_name") or info.get("filename") or ref.get("name"),
            "content_type": info.get("content-type") or ref.get("content_type"), "size": len(data),
            "updated_at": iso(from_canvas(info.get("updated_at"))) or ref.get("updated_at"),
            "fetched_at": datetime.now(KST).isoformat()}, data


async def get(settings: Settings, store: Store, ref: dict, cache: Cache | None = None) -> Fetched:
    """The file and its text: from the cache, or downloaded now with the saved login (never a password login).
    Downloading, reading and caching all happen under the lock, so a second request for the same file waits
    for the cache instead of downloading it again."""
    cache = cache or Cache(settings, store)
    if (hit := _cached(settings, ref)) is not None:
        cache.touch("attachment", ref["id"])
        return hit
    async with exclusive(settings.data_dir, LOCK_WAIT_S) as held:
        if not held:
            raise Busy("a sync is running; try again in a minute")
        if (hit := _cached(settings, ref)) is not None:  # fetched while we waited
            return hit
        async with open_session(settings, store) as s:
            await s.ensure_login(allow_credentials=False)
            meta, data = await _download(s, ref, settings.attachment_max_mb)
        base = _dir(settings) / ref["id"]
        base.with_suffix(".json").unlink(missing_ok=True)  # written last: without it the entry is incomplete
        _write_private(base.with_suffix(".bin"), data)
        text, meta["extract"] = await _extract(base.with_suffix(".bin"), meta["name"], meta["content_type"])
        meta["synced_updated_at"] = ref.get("updated_at")
        _write_private(base.with_suffix(".txt"), text.encode())
        _write_private(base.with_suffix(".json"), json.dumps(meta, ensure_ascii=False).encode())
        _index(cache, settings, ref["id"], meta, course_id=ref.get("course_id"), parent=ref.get("parent"))
        cache.enforce(keep={("attachment", ref["id"])})  # its size wasn't known before: trim the others now
    return Fetched(meta, text, base.with_suffix(".bin"))
