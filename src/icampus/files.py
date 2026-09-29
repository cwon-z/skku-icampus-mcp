"""Attachments: the files synced announcements and assignments point at, downloaded only when asked for.

Only files attached to or linked from an announcement or an assignment description can be fetched, never lecture
items (opening those marks them complete). A download counts as you opening the file in iCampus: course access
reports show it. Download URLs carry a verifier token, so they are looked up fresh each time, used once and never
stored or logged. Downloads go to var/files (0600) and are reused until a sync sees a new version of the file
(for CACHE_HOURS when it saw none). Text is read out of them by a child process (see extract.py).
"""

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .browser import CanvasError, NotLoggedIn, Session, TooLarge, exclusive, open_session
from .config import KST, Settings
from .dates import from_canvas, iso
from .store import Store

MAX_BYTES = 30 * 2**20
CACHE_HOURS = 12
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


def _cached(settings: Settings, ref: dict) -> Fetched | None:
    base = _dir(settings) / ref["id"]
    try:
        meta = json.loads(base.with_suffix(".json").read_text())
        text = base.with_suffix(".txt").read_text()
    except (OSError, ValueError):
        return None
    if not base.with_suffix(".bin").exists():
        return None
    if ref.get("updated_at"):  # the sync saw a version: keep the download until the sync sees another one
        fresh = meta.get("synced_updated_at") == ref["updated_at"]
    else:
        fresh = datetime.fromisoformat(meta["fetched_at"]) > datetime.now(KST) - timedelta(hours=CACHE_HOURS)
    return Fetched(meta, text, base.with_suffix(".bin")) if fresh else None


async def _extract(path: Path, name: str | None, content_type: str | None) -> tuple[str, str]:
    """(text, how) from the reader child process: a hostile or broken file can only take that process down."""
    args = [str(path), (name or "").replace("\0", ""), (content_type or "").replace("\0", "")]
    env = {k: v for k, v in os.environ.items() if not k.startswith("ICAMPUS_")}  # it needs no password or token
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


async def _download(s: Session, ref: dict) -> tuple[dict, bytes]:
    """Look the file up the way the sync saw it, then download it. Called right after a login check, so a
    refusal here means the file is off limits (hidden, locked, deleted), not that the session ended."""
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
    if (info.get("size") or 0) > MAX_BYTES:
        raise Unavailable(f"the file is {info['size'] / 2**20:.0f} MB; only files up to "
                          f"{MAX_BYTES // 2**20} MB are downloaded")
    try:
        data = await s.download(info["url"], MAX_BYTES)
    except TooLarge:
        raise Unavailable(f"the file is over {MAX_BYTES // 2**20} MB; bigger files aren't downloaded") from None
    except CanvasError as exc:
        if exc.status in (401, 403, 404):
            raise refused from None
        raise
    return {"id": ref["id"], "name": info.get("display_name") or info.get("filename") or ref.get("name"),
            "content_type": info.get("content-type") or ref.get("content_type"), "size": len(data),
            "updated_at": iso(from_canvas(info.get("updated_at"))) or ref.get("updated_at"),
            "fetched_at": datetime.now(KST).isoformat()}, data


async def get(settings: Settings, store: Store, ref: dict) -> Fetched:
    """The file and its text: from the cache, or downloaded now with the saved login (never a password login).
    Downloading, reading and caching all happen under the lock, so a second request for the same file waits
    for the cache instead of downloading it again."""
    if (hit := _cached(settings, ref)) is not None:
        return hit
    async with exclusive(settings.data_dir, LOCK_WAIT_S) as held:
        if not held:
            raise Busy("a sync is running; try again in a minute")
        if (hit := _cached(settings, ref)) is not None:  # fetched while we waited
            return hit
        async with open_session(settings, store) as s:
            await s.ensure_login(allow_credentials=False)
            meta, data = await _download(s, ref)
        base = _dir(settings) / ref["id"]
        base.with_suffix(".json").unlink(missing_ok=True)  # written last: without it the entry is incomplete
        _write_private(base.with_suffix(".bin"), data)
        text, meta["extract"] = await _extract(base.with_suffix(".bin"), meta["name"], meta["content_type"])
        meta["synced_updated_at"] = ref.get("updated_at")
        _write_private(base.with_suffix(".txt"), text.encode())
        _write_private(base.with_suffix(".json"), json.dumps(meta, ensure_ascii=False).encode())
    return Fetched(meta, text, base.with_suffix(".bin"))
