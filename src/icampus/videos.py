"""Lecture videos: downloaded from SKKU's content server (Commons, lcms.skku.edu) on request and kept in var/videos.

The lecture list the sync reads names each hosted video's Commons asset. Commons describes the asset without a
login and serves the original file to a request with an iCampus Referer. No cookies are sent, so none of this is
tied to your account: no viewer or player opens, and no watch progress or attendance is recorded (see
docs/icampus-notes.md). Everything that is said to the content server is in commons.py; this module is the queue,
the cache and what turns a finished download into a cached video.

Downloads are a queue in SQLite (table video_jobs): asking for a video adds a row and returns. One worker loop,
started with the server, runs the queue in the order the videos were asked for, `download_concurrency` at a time,
whether or not anyone asks again:
- A restart keeps the queue, and what had arrived of an interrupted download stays in `<asset>.part`. The next
  start goes on by itself: with a Range request where the content server honours one, from zero where it doesn't.
- A try that fails in a way that may pass (the network, no data for STALL_S seconds, a full cache) is made again
  after a growing pause, `export_retries` times. Then the job is `failed` and says why.
- A failed job is never started again unasked: asking for the video again reports the failure, until `retry`.

A finished video is reused until a sync sees a new version of it. The cache manager (cache.py) indexes the files
and makes room: once the videos pass video_cache_gb, the ones used least recently are deleted first, except those
you pinned and those a transcription job still needs.

Another machine can do a download for the queue (a fetcher, fetcher.py; docs/fetchers.md): it takes the next due
job on a lease, downloads the video where it is, and uploads the file here. Such a job is claimed with the same
update and in the same order as a local one, so nothing is ever downloaded twice at once; the lease is renewed by
the fetcher's heartbeats and taken back when they stop. A fetcher is told an asset id and a host, nothing else.
The file it sends is checked (size, SHA-256, MP4) and then finished by the same code as a local download.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from . import commons
from .cache import Cache, CacheFull
from .commons import MAX_DOWNLOAD_S, MB, VideoError
from .config import KST, Settings
from .store import Store

log = logging.getLogger(__name__)
STALL_S = commons.STALL_S  # a try ends when the content server sends nothing for this long
SPEED_WINDOW_S = commons.SPEED_WINDOW_S  # the speed in a status is the average over this long
STALL_SHOWN_S = 15.0  # from here on a status says `stalled`
RETRY_AFTER_S = (30, 120, 300, 900, 1800, 3600)  # the pause before the nth retry of an export
TICK_S = 5.0  # how often the worker looks at the queue when nothing wakes it
PERSIST_S = 5.0  # how often a running download's progress goes into the database
DISK_MARGIN = 1 << 30  # leave this much free on the disk
OPEN = ("queued", "downloading")
_NO_LEASE = {"fetcher": None, "lease_id": None, "lease_expires_at": None, "lease_info": None}
LEASE_TTL_S = 90.0  # a fetcher's job is taken back when nothing was heard from it for this long
FETCHER_SEEN_S = 120.0  # local_downloads=fallback: the server downloads once no fetcher was heard from for this long
FETCHER_SLOTS = 4  # the most jobs one fetcher may hold at a time
FETCHERS_SAVE_S = 15.0  # how often a fetcher's "last seen" goes into the database
FETCHER_KEPT_DAYS = 30  # a fetcher not heard from for this long leaves the list
FETCHER_STRIKES = 3  # failed tries in a row after which a fetcher is given no work for a while,
FETCHER_REST_S = (60, 300, 900, 3600)  # for this long, growing with every further failure
LEASE_DESCRIBE_S = 15.0  # when a job is leased, the content server gets this long to describe the video,
LEASE_BUDGET_S = 20.0  # and a request for work is answered within about this long, whatever it met
UPLOAD_PIECE_MAX = 64 * MB  # the most one upload request may carry
UPLOAD_IDLE_S = 30.0  # an upload request that brings nothing for this long is ended; the upload can go on later
UPLOADED_KEPT_S = 900.0  # how long a finished lease is remembered, for a fetcher that missed the answer
# What the server keeps of its own description of a leased video: what finishing the upload needs. Never the
# uploader's name or a download address.
LEASE_INFO = ("extension", "type", "duration", "exists_caption", "exists_script", "original_file_length")
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_ADDRESS = re.compile(r"\S*(?:://|\?\S*=)\S*")


def _boot_id() -> str:
    """Names this run of the operating system. What a stopped process had written to a file is still in the
    kernel's cache, so a .part from this run can be continued after a restart of the server; after a power cut its
    last bytes can't be trusted. Where the kernel gives no id, only this process's own .part files are continued."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return f"process-{os.getpid()}-{time.time_ns()}"


_BOOT = _boot_id()


def _now() -> str:
    return datetime.now(KST).isoformat(timespec="microseconds")  # one width, so the strings sort by time


def _shown(stamp: str | None) -> str | None:
    """A stored time for an answer: to the second."""
    return datetime.fromisoformat(stamp).isoformat(timespec="seconds") if stamp else None


class StaleLease(Exception):
    """The lease a fetcher named is not that job's current one: it expired and was taken back, the job went to
    someone else, or it is gone. The fetcher drops the job."""


class OutOfStep(Exception):
    """An upload piece doesn't start where the file on the server ends. `received` is where it does."""

    def __init__(self, received: int):
        super().__init__(f"the server has {received} bytes of this file")
        self.received = received


class Refused(Exception):
    """An upload failed the server's checks. The try is over (the queue decides about another) and the fetcher
    drops the job. The message is safe to show."""


def find(store: Store, item_id: str) -> dict | None:
    """The synced lecture item with this module item id, if it is a Commons-hosted video."""
    suffix = f":modules/items:{item_id}"
    for row in store.records("lectures", include_inactive=True):
        if row.get("key", "").endswith(suffix) and row.get("video"):
            return row
    return None


def _safe_name(title: str | None, fallback: str) -> str:
    name = " ".join(_UNSAFE_NAME.sub(" ", title or "").split()).strip(". ")
    return name[:150] or fallback


def _safe_message(text: str) -> str:
    """A fetcher's own words for a failure, made fit to store and show: one short line, and no address (a
    download redirect's query must never be kept, wherever a message came from)."""
    line = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return _ADDRESS.sub("(address removed)", line)[:200] or "failed"


def _eta(left: int | None, speed: float | None) -> int | None:
    """Seconds until `left` bytes have arrived at `speed` bytes per second, when that is a number worth showing."""
    if left is None or not speed or speed < 1:
        return None
    return min(round(left / speed), 10**7)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(MB):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(lecture: dict) -> str:
    """What a download needs from a synced lecture row, kept with its job so the queue outlives a restart."""
    video = lecture["video"]
    return json.dumps({**{k: lecture.get(k) for k in ("key", "course_id", "item_id", "title")},
                       "video": {k: video[k] for k in ("asset_id", "host", "listed_size", "updated_at")
                                 if video.get(k) is not None}}, ensure_ascii=False)


@dataclass
class Running(commons.Meter):
    """A download this process is working on: the live numbers behind its status."""
    task: asyncio.Task | None = field(default=None, repr=False)
    removed: bool = False  # taken out of the queue by hand while it ran
    error: str | None = None  # how the try ended, for whoever waits on it: the message when it failed,
    transient: bool = False  # and whether that failure may pass
    note: str | None = None
    progress_at: str | None = None  # when bytes last arrived
    persisted: float = 0.0

    def arrived(self, n: int) -> bool:
        sampled = super().arrived(n)
        if sampled:
            self.progress_at = _now()
        return sampled


@dataclass
class Lease:
    """A download a fetcher is doing for this queue: what its heartbeats last said, and what the server found out
    about the file when it gave the job away."""
    job_id: int
    asset_id: str
    fetcher: str  # the name the fetcher goes by, for statuses and messages
    lease_id: str
    owner: str = ""  # who it is: the label of the token it called with
    confirmed: bool = False  # it has called about this lease, so the answer that gave the job away arrived
    info: dict = field(default_factory=dict)  # the server's own description of the video (LEASE_INFO)
    lecture: dict = field(default_factory=dict)  # the lecture as it was synced when the job was given away
    exact: int | None = None  # the size the file must have, when Commons said
    phase: str = "downloading"  # then "uploading"
    done: int = 0  # bytes the fetcher has downloaded
    total: int | None = None
    speed: float | None = None  # bytes per second of the phase it is in, as the fetcher measures it
    beat: float = field(default_factory=time.monotonic)  # when it was last heard from
    renewed: float = 0.0  # when the lease's end last went into the database
    progress_at: str | None = None  # when its numbers last moved
    error: str | None = None  # how the try ended, for whoever waits on it
    transient: bool = False
    ended: bool = False  # finished, failed, taken back or taken out of the queue: a request still running stops
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)  # one upload request at a time
    digest: Any = field(default_factory=hashlib.sha256, repr=False)  # of the uploaded bytes, while all went through it
    hashed: int = 0

    def live_speed(self) -> float | None:
        """The speed it last reported, unless that is too long ago to still be true."""
        return self.speed if time.monotonic() - self.beat < STALL_SHOWN_S else None


class Videos:
    def __init__(self, settings: Settings, cache: Cache, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.cache = cache
        self.store: Store = cache.store
        self.dir = settings.data_dir / "videos"
        self.active: dict[str, Running] = {}  # asset id -> the download running now
        self.leases: dict[str, Lease] = {}  # asset id -> the download a fetcher is doing
        # A fetcher is who its token says it is: everything here is kept by the token's label. The name a
        # fetcher gives itself is only what statuses call it.
        self.fetchers: dict[str, dict] = {}  # token label -> name, platform, version, last_seen
        self._fetchers_saved = 0.0
        self._free: dict[str, int | None] = {}  # token label -> the room it last said it has
        self._too_big: dict[str, dict[str, int]] = {}  # token label -> asset id -> the bytes it had no room for
        self._streak: dict[str, int] = {}  # token label -> its failed tries in a row
        self._rest: dict[str, datetime] = {}  # token label -> until when it is given no work
        self._server_only: dict[str, str] = {}  # asset id -> why no fetcher is given this job
        self._uploaded: dict[str, tuple[float, int]] = {}  # lease id -> when its upload was finished, and its size
        self.heartbeat_at: str | None = None  # when the worker loop last looked at the queue
        self._working = False
        self._stopped = False  # after stop(), until something is asked for or the worker loop starts again
        self._waiters: dict[str, list[asyncio.Future]] = {}  # asset id -> who waits for its next try to end
        self._reserved: dict[str, int] = {}  # asset id -> the bytes a running download will take
        self._no_resume: set[str] = set()  # hosts seen answering a Range request with the whole file
        self._wake = asyncio.Event()
        self._transport = transport
        self._recover()  # a restart: what was downloading goes on from the queue, nothing is dropped
        self._restore_fetchers()  # and what a fetcher was downloading is still that fetcher's
        wanted = {f"{row['asset_id']}.part" for row in self.store.query(
            "SELECT asset_id FROM video_jobs WHERE state='queued'")} | {f"{asset}.part" for asset in self.leases}
        for leftover in self.dir.glob("*.part"):  # of a job that is gone: nothing would ever continue it
            if leftover.name not in wanted:
                leftover.unlink(missing_ok=True)
        cache.adopter(self.adopt)
        cache.on_delete("video", self._copy_gone)

    # --- the cache ---------------------------------------------------------

    def _meta_path(self, asset_id: str) -> Path:
        return self.dir / f"{asset_id}.json"

    def _part(self, asset_id: str) -> Path:
        return self.dir / f"{asset_id}.part"

    def adopt(self) -> None:
        """Indexes videos that are on disk with their sidecar but without a row: downloads from before the cache
        manager, or all of them after the index was lost. Nothing is downloaded again."""
        for path in self.dir.glob("*.json"):
            if self.cache.get("video", path.stem) is not None:
                continue
            try:
                meta = json.loads(path.read_text())
                file = self.dir / meta["file"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if file.is_file() and file.parent == self.dir:
                self._index(path.stem, meta, file, last_used=meta.get("last_used"))

    def _index(self, asset_id: str, meta: dict, file: Path, last_used: str | None = None) -> None:
        self.cache.put("video", asset_id, [file, self._meta_path(asset_id)], course_id=meta.get("course_id"),
                       item_id=meta.get("item_id"), title=meta.get("title"),
                       meta={k: v for k, v in meta.items() if k != "last_used"},
                       created_at=meta.get("downloaded_at"), last_used=last_used)

    def meta(self, asset_id: str) -> dict | None:
        """The finished download's record, if its file is still there."""
        entry = self.cache.get("video", asset_id)
        if entry is None:
            return None
        return {**entry.meta, "last_used": entry.last_used, "pinned": entry.pinned}

    def _write_meta(self, asset_id: str, meta: dict) -> None:
        """The sidecar beside the file: what an older version reads, and what the index is rebuilt from."""
        tmp = self._meta_path(asset_id).with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(tmp, self._meta_path(asset_id))

    def cached(self) -> list[dict]:
        """Most recently used first."""
        return [{**e.meta, "last_used": e.last_used, "pinned": e.pinned} for e in self.cache.entries("video")]

    def used_bytes(self) -> int:
        return self.cache.used("video")

    def path(self, asset_id: str) -> Path | None:
        entry = self.cache.get("video", asset_id)
        return self.cache.path(entry) if entry else None

    def touch(self, asset_id: str) -> None:
        self.cache.touch("video", asset_id)

    def delete(self, asset_id: str) -> bool:
        return self.cache.delete("video", asset_id)

    def _copy_gone(self, entry: Any) -> None:
        """A copy leaves the cache (deleted, or evicted for room). A failure recorded while refreshing it has
        nothing left to describe, and must not turn "not downloaded" into "failed"."""
        self.store.execute("DELETE FROM video_jobs WHERE asset_id=? AND state='failed'", (entry.key,))

    def current(self, lecture: dict) -> bool:
        """Whether the copy on disk is of the version of the video the last sync saw."""
        video = lecture["video"]
        meta = self.meta(video["asset_id"])
        return bool(meta) and not (video.get("updated_at") and meta.get("synced_updated_at") != video["updated_at"])

    def _make_room(self, needed: int, keep: str, on_disk: int = 0) -> None:
        """Room for a download of `needed` bytes, `on_disk` of which are here already, beside what the other
        running downloads will still bring."""
        cap = int(self.settings.video_cache_gb * 2**30)
        if needed > cap:
            raise VideoError(f"the video ({needed / 2**30:.1f} GB) is bigger than the whole video cache "
                             f"({self.settings.video_cache_gb:g} GB, ICAMPUS_VIDEO_CACHE_GB)")
        others = {asset: size for asset, size in self._reserved.items() if asset != keep}
        # and what jobs waiting for their next try have on disk: not in the cache index, but under the same cap
        parked = sum(self._on_disk(row["asset_id"]) for row in self.store.query(
            "SELECT asset_id FROM video_jobs WHERE state='queued'") if row["asset_id"] != keep)
        claimed = sum(others.values()) + parked
        if needed + claimed > cap:
            raise VideoError("the video cache has no room for this video beside the downloads that are running "
                             "or waiting to be continued", transient=True)
        try:
            self.cache.make_room("video", needed + claimed, keep={("video", keep)})
        except CacheFull as exc:  # what is left is pinned or waiting for a transcription: room comes back later
            raise VideoError(str(exc), transient=True) from None
        coming = sum(max(0, size - self.active[asset].done) for asset, size in others.items() if asset in self.active)
        coming += sum(max(0, size - self._on_disk(asset)) for asset, size in others.items() if asset in self.leases)
        if shutil.disk_usage(self.dir).free < needed - on_disk + coming + DISK_MARGIN:
            raise VideoError("not enough free disk space for this video", transient=True)

    # --- the queue's rows ----------------------------------------------------

    def _job(self, asset_id: str) -> dict | None:
        row = self.store.one("SELECT * FROM video_jobs WHERE asset_id=?", (asset_id,))
        return dict(row) if row else None

    def _set(self, job_id: int, **fields: Any) -> None:
        fields["updated_at"] = _now()
        self.store.execute(f"UPDATE video_jobs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                           (*fields.values(), job_id))

    def _on_disk(self, asset_id: str) -> int:
        try:
            return self._part(asset_id).stat().st_size
        except OSError:
            return 0

    def _recover(self) -> None:
        """A job marked downloading that nothing here is downloading was cut off (a restart, a crash). It goes
        back into the queue, in its old place, with what it has on disk."""
        for row in self.store.query("SELECT id, asset_id FROM video_jobs WHERE state='downloading' "
                                    "AND fetcher IS NULL"):  # a fetcher's job is the lease's business
            if row["asset_id"] not in self.active:
                self._set(row["id"], state="queued", downloaded_bytes=self._on_disk(row["asset_id"]))

    def _slots(self) -> int:
        return max(1, int(self.settings.download_concurrency))

    def _around(self) -> list[str]:
        """The fetchers that are there to take work: heard from within FETCHER_SEEN_S seconds, and not resting
        after failures."""
        return [owner for owner, entry in self.fetchers.items() if self._online(entry) and not self._resting(owner)]

    def _local(self) -> bool:
        """Whether the server may start any waiting download itself now (`local_downloads`): always, never, or
        only while no fetcher is around."""
        mode = self.settings.local_downloads
        if mode == "fallback":
            return not self._around()
        return mode != "off"

    def _beyond_fetchers(self, asset_id: str, owners: list[str]) -> bool:
        """Whether none of these fetchers can take the job: it is kept from fetchers altogether, or each of them
        said it has less room than the video needs. With local_downloads=fallback the server then does it."""
        if asset_id in self._server_only or not commons.SAFE_ID.fullmatch(asset_id):
            return True
        return all(self._too_big.get(owner, {}).get(asset_id, 0) > (self._free.get(owner) or 0) for owner in owners)

    # --- status ----------------------------------------------------------

    def status(self, lecture: dict) -> dict:
        video = lecture["video"]
        asset_id = video["asset_id"]
        out: dict[str, Any] = {"item_id": lecture.get("item_id"), "title": lecture.get("title")}
        meta = self.meta(asset_id)
        job = self._job(asset_id)
        if job and job["state"] in OPEN:
            out.update(self._progress(job))
        elif meta:
            out.update(state="ready" if self.current(lecture) else "outdated",
                       **{k: meta.get(k) for k in ("format", "content_type", "downloaded_at", "captions", "script")},
                       size_mb=round((meta.get("size") or 0) / MB))
            if job:  # refreshing this copy failed; the old one is still here
                out.update(last_error=job["error"], failed_at=_shown(job["finished_at"]))
        elif job:
            out.update(state="failed", error=job["error"], attempts=job["attempts"],
                       retryable=bool(job["transient"]), failed_at=_shown(job["finished_at"]))
        else:
            out["state"] = "not_downloaded"
        if out.get("size_mb") is None and out.get("total_mb") is None and video.get("listed_size"):
            # What the iCampus listing says, until the content server has given the real size. It can be several
            # times the file (it seems to count more than the original), so it is named an estimate.
            out["listed_size_mb_estimate"] = round(video["listed_size"] / MB)
        return out

    def _progress(self, job: dict) -> dict:
        """The part of a status that follows a waiting or running download."""
        live = self.active.get(job["asset_id"]) if job["state"] == "downloading" else None
        lease = self.leases.get(job["asset_id"]) if job["state"] == "downloading" and not live else None
        done = live.done if live else lease.done if lease else job["downloaded_bytes"]
        total = (live.total if live else lease.total if lease else None) or job["total_bytes"]
        out: dict[str, Any] = {"state": job["state"], "downloaded_mb": round(done / MB),
                               "total_mb": round(total / MB) if total else None,
                               "queued_at": _shown(job["queued_at"]),
                               "attempts": job["attempts"] + int(job["state"] == "downloading")}
        if job["state"] == "queued":
            now = _now()  # ahead of it: the jobs asked for earlier that could start now (not those waiting out a pause)
            out["queue_position"] = 1 + self.store.one(
                "SELECT COUNT(*) AS n FROM video_jobs WHERE state='queued' AND (not_before IS NULL OR not_before<=?) "
                "AND (queued_at<? OR (queued_at=? AND id<?))", (now, job["queued_at"], job["queued_at"], job["id"]))["n"]
            if job["not_before"] and job["not_before"] > now:
                out["retry_at"] = _shown(job["not_before"])
        else:
            # who downloads it (this server, or a fetcher by its name) and what that one is doing: fetching the
            # file from the content server, or (a fetcher only) sending it here
            out["by"] = job["fetcher"] or "server"
            out["phase"] = lease.phase if lease else "downloading"
            out["started_at"] = _shown(job["started_at"])
            out["last_progress_at"] = _shown((live.progress_at if live else lease.progress_at if lease else None)
                                             or job["last_progress_at"])
            if lease:  # the speed and the time left are those of the phase it is in
                speed, left = lease.live_speed(), (total - done if total and total >= done else None)
                if lease.phase == "uploading":
                    here = self._on_disk(job["asset_id"])
                    out["uploaded_mb"], left = round(here / MB), (total - here if total and total >= here else None)
                out["speed_mbps"] = round(speed / MB, 2) if speed is not None else None
                out["eta_s"] = _eta(left, speed)
                if time.monotonic() - lease.beat >= STALL_SHOWN_S:
                    out["stalled"] = True  # nothing heard from the fetcher; LEASE_TTL_S of that ends its lease
            if live:
                speed = live.speed()
                out["speed_mbps"] = round(speed / MB, 2) if speed is not None else None  # megabytes per second
                out["eta_s"] = _eta(total - done if total and total >= done else None, speed)
                if time.monotonic() - live.seen >= STALL_SHOWN_S:
                    out["stalled"] = True
                if live.note:
                    out["note"] = live.note
        if job["error"]:
            out["last_error"] = job["error"]
        return out

    def progress(self, asset_id: str) -> dict | None:
        """Where a waiting or running download stands (megabytes, place in the queue, speed), or None."""
        job = self._job(asset_id)
        if job is None or job["state"] not in OPEN:
            return None
        return {k: v for k, v in self._progress(job).items()
                if k in ("downloaded_mb", "total_mb", "queue_position", "speed_mbps", "eta_s", "stalled", "by",
                         "phase", "uploaded_mb")}

    def queue(self) -> list[dict]:
        """Every job as its status, in the order they run: downloading, then waiting, then failed."""
        order = {"downloading": 0, "queued": 1}
        rows = sorted(self.store.query("SELECT * FROM video_jobs ORDER BY queued_at, id"),
                      key=lambda row: order.get(row["state"], 2))
        out = []
        for row in rows:
            lecture = json.loads(row["lecture"])
            out.append({"course_id": lecture.get("course_id"), **self.status(lecture)})
        return out

    def summary(self) -> dict:
        """The queue for /api/v1/status: how many downloads are in each state, what is running, what failed, and
        when the worker last looked at the queue."""
        rows = [dict(row) for row in self.store.query("SELECT * FROM video_jobs ORDER BY queued_at, id")]
        counts = Counter(row["state"] for row in rows)

        def named(row: dict) -> dict:
            return {"item_id": row["item_id"], "title": json.loads(row["lecture"]).get("title")}
        shown = ("by", "phase", "downloaded_mb", "total_mb", "uploaded_mb", "speed_mbps", "eta_s",
                 "last_progress_at", "attempts", "stalled", "last_error")
        active = [named(row) | {k: v for k, v in self._progress(row).items() if k in shown and v is not None}
                  for row in rows if row["state"] == "downloading"]
        failed = [named(row) | {"error": row["error"], "failed_at": _shown(row["finished_at"])}
                  for row in sorted((r for r in rows if r["state"] == "failed"), key=lambda r: r["finished_at"] or "")]
        now = _now()
        waiting = [row["item_id"] for row in rows if row["state"] == "queued"
                   and not (row["not_before"] and row["not_before"] > now)]  # a retry's pause isn't "next"
        return {"queued": counts["queued"], "downloading": counts["downloading"], "failed": counts["failed"],
                "ready": len(self.cache.entries("video")), "active": active, "next": waiting[:5],
                "recently_failed": failed[-5:],
                "worker": {"running": self._working, "heartbeat_at": _shown(self.heartbeat_at),
                           "concurrency": self._slots(), "local_downloads": self.settings.local_downloads},
                "fetchers": self.fetcher_list()}

    # --- asking for a download ---------------------------------------------

    def start(self, lecture: dict, refresh: bool = False, *, retry: bool = False, keep_trying: bool = True) -> dict:
        """Puts a download in the queue unless the video is cached and current, or already on its way, and returns
        its status. With refresh, a current copy is downloaded again; it stays (with its pin) until the new one has
        arrived. A download that failed is only queued again with retry (or refresh): asking again without it
        reports the failure, so nothing starts over unnoticed. keep_trying=False is for a caller that does its own
        retrying (a transcription job): the download then fails at its first failure."""
        asset_id = lecture["video"]["asset_id"]
        status = self.status(lecture)
        job = self._job(asset_id)
        if job and job["state"] in OPEN:
            changes: dict[str, Any] = {}
            if (asked := _snapshot(lecture)) != job["lecture"]:
                changes["lecture"] = asked  # what the last sync says about it
            if keep_trying and not job["keep_trying"]:
                changes["keep_trying"] = 1  # an export of a video a transcription asked for
            if retry and job["state"] == "queued" and job["not_before"]:
                changes["not_before"] = None  # asked for again by hand: its next try is now
            if changes:
                self._set(job["id"], **changes)
            self._kick()
            return self.status(lecture)
        if status["state"] == "ready" and not refresh:
            if job and retry:  # the copy is current: the failure of an earlier refresh has nothing left to say
                self.store.execute("DELETE FROM video_jobs WHERE id=?", (job["id"],))
                return self.status(lecture)
            return status
        if job and not (retry or refresh):
            failed_for = (json.loads(job["lecture"]).get("video") or {}).get("updated_at")
            if failed_for == lecture["video"].get("updated_at"):
                return status  # failed: shown as that (or as the outdated copy that is still here, with `last_error`)
            # a sync has seen another version of the video since: that is a new download, not a retry
        now = _now()
        fresh = {"lecture": _snapshot(lecture), "item_id": lecture.get("item_id"), "state": "queued",
                 "keep_trying": int(keep_trying), "attempts": 0, "not_before": None, "error": None, "transient": 0,
                 "total_bytes": None, "downloaded_bytes": 0, "part_version": None, "validator": None, "boot": None,
                 "queued_at": now, "started_at": None, "last_progress_at": None, "finished_at": None, **_NO_LEASE}
        if job:  # asked for again after it failed: to the back of the queue
            self._set(job["id"], **fresh)
        else:
            fresh |= {"asset_id": asset_id, "updated_at": now}
            self.store.execute(f"INSERT INTO video_jobs ({', '.join(fresh)}) VALUES ({', '.join('?' * len(fresh))})",
                               tuple(fresh.values()))
        self._kick()
        return self.status(lecture)

    def cancel(self, asset_id: str) -> bool:
        """Takes a download out of the queue, waiting, running or failed, with what it has on disk. A finished
        video stays. False when there was no such job."""
        job = self._job(asset_id)
        if job is None:
            return False
        self.store.execute("DELETE FROM video_jobs WHERE id=?", (job["id"],))
        self._part(asset_id).unlink(missing_ok=True)  # here, not in the task: one that hasn't run yet never would
        if lease := self.leases.get(asset_id):  # a fetcher has it: its next heartbeat is told the job is gone
            self._end_lease(lease)
            self._wake.set()
        live = self.active.get(asset_id)
        if live and live.task and not live.task.done():
            live.removed = True
            live.task.cancel()  # when it has ended, the next job starts
        else:
            self._release(asset_id, "the download was taken out of the queue", False)
        return True

    async def ensure(self, lecture: dict) -> Path:
        """The video's file, downloading it first unless a current copy is cached. The download goes through the
        same queue as exports, so /videos/{item_id} shows its progress, and this waits for the end of its next
        try. Raises VideoError; the caller decides about trying again."""
        asset_id = lecture["video"]["asset_id"]
        if not self.current(lecture):  # a current copy is used as it is, even while a fresh one is on its way
            self.start(lecture, retry=True, keep_trying=False)
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.setdefault(asset_id, []).append(waiter)
            try:
                error, transient = await waiter  # how the try ended, whatever was asked of the job since
            finally:
                if waiter in (waiting := self._waiters.get(asset_id, [])):
                    waiting.remove(waiter)
            if error is not None:
                raise VideoError(error, transient=transient)
            if not self.current(lecture):  # it arrived, but it is not the version this caller read about
                raise VideoError("a sync saw another version of the video while its download waited", transient=True)
        self.touch(asset_id)
        path = self.path(asset_id)
        if path is None:
            raise VideoError("the video left the cache right after its download", transient=True)
        return path

    # --- the worker --------------------------------------------------------

    async def run(self) -> None:
        """The worker: one loop for as long as the server runs. It starts what is due when the server comes up,
        when a download is asked for or ends, and every few seconds (a retry's pause running out), and it leaves
        a heartbeat for /api/v1/status. No request has to arrive for the queue to move."""
        self._working, self._stopped = True, False
        try:
            while True:
                self.heartbeat_at = _now()
                try:
                    self.tick()
                except Exception as exc:  # the queue must outlive a bad row
                    log.error("video queue: %s", type(exc).__name__)
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), TICK_S)
                except TimeoutError:
                    pass
        finally:
            self._working = False

    def tick(self) -> None:
        """Starts the downloads that are due, the one asked for first going first, while fewer than
        download_concurrency are running. A job waiting out a pause after a failed try doesn't hold up the ones
        behind it."""
        if self._stopped:
            return
        self._recover()
        self._expire()
        self._forget()
        room = self._slots() - len(self.active)
        if room <= 0 or self.settings.local_downloads == "off":
            return
        # fallback, and fetchers are around: the server leaves the queue to them, except for a job none of them
        # can take, which would otherwise wait for ever
        around = [] if self._local() else self._around()
        loop = asyncio.get_running_loop()
        due = self.store.query("SELECT * FROM video_jobs WHERE state='queued' AND (not_before IS NULL OR not_before<=?)"
                               " ORDER BY queued_at, id LIMIT ?", (_now(), 100 if around else room))
        for row in due:
            job = dict(row)
            if len(self.active) >= self._slots():
                break
            if around and not self._beyond_fetchers(job["asset_id"], around):
                continue
            now = _now()
            claimed = self._claim(job["id"], now)
            if not claimed or job["asset_id"] in self.active:
                continue
            live = self.active[job["asset_id"]] = Running(done=job["downloaded_bytes"], total=job["total_bytes"])
            live.task = loop.create_task(self._attempt(job, live))
            live.task.add_done_callback(lambda task, job=job, live=live: self._finished(job["asset_id"], live, task))

    def _claim(self, job_id: int, now: str, **lease: Any) -> bool:
        """Takes a waiting job, for this server or (with the lease's columns) for a fetcher. One atomic update:
        whoever's goes through has the job, and nobody else starts it."""
        fields = {"started_at": now, "updated_at": now, **_NO_LEASE, **lease}
        return bool(self.store.execute(
            f"UPDATE video_jobs SET state='downloading', not_before=NULL, {', '.join(f'{k}=?' for k in fields)} "
            "WHERE id=? AND state='queued'", (*fields.values(), job_id)).rowcount)

    def _kick(self) -> None:
        """Something changed in the queue: start what can start now, and let the worker loop know."""
        self._stopped = False
        self._wake.set()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # asked from outside the event loop: the worker loop starts it
        try:
            self.tick()
        except Exception as exc:  # the job is in the queue either way; the worker loop comes round again
            log.error("video queue: %s", type(exc).__name__)

    def _release(self, asset_id: str, error: str | None, transient: bool) -> None:
        """Tells whoever waits for this download how its try ended (error None: it is here)."""
        for waiter in self._waiters.pop(asset_id, []):
            if not waiter.done():
                waiter.set_result((error, transient))

    def _finished(self, asset_id: str, live: Running, task: asyncio.Task) -> None:
        if self.active.get(asset_id) is live:
            del self.active[asset_id]
        self._reserved.pop(asset_id, None)
        if live.removed:
            self._release(asset_id, "the download was taken out of the queue", False)
        elif task.cancelled():
            self._release(asset_id, "the server is stopping", True)
        else:
            self._release(asset_id, live.error, live.transient)
        self._wake.set()
        if task.cancelled() and not live.removed:
            return  # the server is stopping: nothing new starts
        try:
            self.tick()  # the next download starts at once, not at the worker loop's next round
        except Exception as exc:
            log.error("video queue: %s", type(exc).__name__)

    async def stop(self) -> None:
        """Server shutdown: running downloads stop where they are. The jobs stay in the database and what arrived
        stays on disk, so the next start goes on without anyone asking."""
        self._stopped = True  # a download that ends by itself right now must not start the next one
        tasks = [live.task for live in self.active.values() if live.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def drain(self, timeout: float = 30) -> None:
        """Runs the queue until nothing is downloading or due. For tests and the CLI."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.tick()
            tasks = [live.task for live in self.active.values() if live.task]
            if not tasks:
                return
            await asyncio.wait(tasks, timeout=0.2)
            await asyncio.sleep(0)
        raise TimeoutError("the video queue did not settle")

    def _lecture(self, job: dict) -> dict:
        """The lecture as the last sync has it (it may have seen a newer version of the video while this waited),
        else as it was when the download was asked for."""
        asked = json.loads(job["lecture"])
        current = find(self.store, asked["item_id"]) if asked.get("item_id") else None
        return current if current and current["video"]["asset_id"] == job["asset_id"] else asked

    async def _attempt(self, job: dict, live: Running) -> None:
        """One try at a job that tick() claimed. However it ends, the row says so."""
        asset_id = job["asset_id"]
        try:
            async with asyncio.timeout(MAX_DOWNLOAD_S):
                await self._download(self._lecture(job), job, live)
            self.store.execute("DELETE FROM video_jobs WHERE id=?", (job["id"],))  # done: the cache has it now
        except asyncio.CancelledError:
            if live.removed:
                self._part(asset_id).unlink(missing_ok=True)
            else:  # the server is stopping: back into the queue, in its place, with what has arrived
                self._set(job["id"], state="queued", downloaded_bytes=self._on_disk(asset_id),
                          last_progress_at=live.progress_at or job["last_progress_at"])
            raise
        except Exception as exc:  # what it says and whether it may pass: commons.failure, as for a fetcher's try
            if not isinstance(exc, (VideoError, httpx.HTTPError, OSError)):
                log.warning("video %s: %s", asset_id, type(exc).__name__)
            self._failed(job, live, *commons.failure(exc))
        finally:
            # With the try, not in the done-callback a moment later: by then the file is in the cache index, and
            # another download making room meanwhile would count it twice.
            self._reserved.pop(asset_id, None)

    def _failed(self, job: dict, live: Running | Lease, message: str, transient: bool, *,
                keep_part: bool = True) -> None:
        """A try failed. One that may pass is made again after a pause, as often as export_retries allows (never
        for a download whose caller retries by itself); anything else ends the job, visibly. `keep_part` is off
        for a fetcher's try: what it had uploaded is of no use to the next one."""
        asset_id = job["asset_id"]
        live.error, live.transient = message, transient
        current = self._job(asset_id)  # an export may have asked for it while it ran
        if current is None or current["id"] != job["id"]:
            self._part(asset_id).unlink(missing_ok=True)
            return  # taken out of the queue meanwhile
        attempts = job["attempts"] + 1
        retries = int(self.settings.export_retries) if current["keep_trying"] else 0
        host = (json.loads(job["lecture"]).get("video") or {}).get("host", commons.HOST)
        if transient and attempts <= retries:
            if host in self._no_resume or not keep_part:  # every download starts over: what arrived is of no use
                self._part(asset_id).unlink(missing_ok=True)
            pause = RETRY_AFTER_S[min(attempts, len(RETRY_AFTER_S)) - 1]
            again = datetime.now(KST) + timedelta(seconds=pause)
            self._set(job["id"], state="queued", attempts=attempts, error=message, transient=1,
                      not_before=again.isoformat(timespec="microseconds"), downloaded_bytes=self._on_disk(asset_id),
                      **_NO_LEASE)
            log.info("video %s: %s; trying again in %d s (retry %d of %d)", asset_id, message, pause, attempts, retries)
            return
        self._part(asset_id).unlink(missing_ok=True)
        if transient and retries:
            message = live.error = f"{message} (gave up after {attempts} tries)"
        self._set(job["id"], state="failed", attempts=attempts, error=message, transient=int(transient),
                  not_before=None, downloaded_bytes=0, finished_at=_now(), **_NO_LEASE)
        log.warning("video %s failed: %s", asset_id, message)

    def _kept(self, job: dict, version: str) -> int:
        """How much of an interrupted try can be continued: the .part's bytes, when they belong to this version of
        the video, were written during this run of the machine and leave something to fetch. Otherwise 0."""
        size = self._on_disk(job["asset_id"])
        if job["part_version"] != version or job["boot"] != _BOOT or not 0 < size < (job["total_bytes"] or 0):
            return 0
        return size

    async def _download(self, lecture: dict, job: dict, live: Running) -> None:
        """The server's own try: describe the video, make room, fetch it (commons.py), finish it."""
        video = lecture["video"]
        asset_id, host = video["asset_id"], video.get("host", commons.HOST)
        version = video.get("updated_at") or ""
        cap = int(self.settings.video_max_gb * 2**30)
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        part = self._part(asset_id)

        def alive() -> None:  # the content server answered or sent bytes
            live.seen = time.monotonic()

        def opened(file: commons.Opened) -> None:
            if file.range_ignored:  # a Range request answered with the whole file
                self._no_resume.add(host)
            if file.discarded:  # the whole file again. Said, so that it isn't a silent reset.
                live.note = (f"the content server did not continue the interrupted download: this try "
                             f"started over from zero ({file.discarded // MB} MB had arrived before)")
                log.info("video %s: the content server did not honour Range; starting over", asset_id)
            # the file first, then what the row says about it: never a row describing other bytes
            self._set(job["id"], total_bytes=file.total, downloaded_bytes=live.done, part_version=version,
                      validator=file.validator, boot=_BOOT, last_progress_at=_now())

        def progress() -> None:
            if time.monotonic() - live.persisted >= PERSIST_S:
                live.persisted = time.monotonic()
                self._set(job["id"], downloaded_bytes=live.done, last_progress_at=live.progress_at)

        async with commons.attempt(self._transport, stall_s=STALL_S, on_alive=alive) as content:
            info = await content.describe(host, asset_id, max_bytes=cap)
            original = commons.original_size(info)
            # Without a size from Commons, room is made for what iCampus lists, capped at the largest video
            # allowed: the listing can be several times the file, so it never refuses a download by itself.
            expected = original or min(video.get("listed_size") or 0, cap)
            kept = self._kept(job, version)
            live.restart(kept, original or job["total_bytes"])
            self._reserved[asset_id] = expected
            self._make_room(expected, keep=asset_id, on_disk=kept)
            resume = commons.Resume(kept, job["total_bytes"], job["validator"]) if kept else None
            got = await content.fetch(part, meter=live, expected=expected, resume=resume, opened=opened,
                                      progress=progress)
        self._finish(lecture, info, part, got.size, got.mp4)
        log.info("video %s downloaded (%.0f MB)", asset_id, got.size / MB)

    def _finish(self, lecture: dict, info: dict, part: Path, size: int, mp4: bool) -> None:
        """A complete .part becomes the cached video: its final name, the sidecar beside it, the cache index. A
        download by this server and a file a fetcher uploaded both end here. `info` is Commons' description."""
        video = lecture["video"]
        asset_id = video["asset_id"]
        ext = commons.extension(info, mp4)
        final = self.dir / f"{asset_id}{ext}"
        previous = self.meta(asset_id)  # an outdated copy being replaced
        os.replace(part, final)
        now = datetime.now(KST).isoformat()
        meta = {
            "asset_id": asset_id, "file": final.name, "size": size, "item_id": lecture.get("item_id"),
            "course_id": lecture.get("course_id"), "title": lecture.get("title"),
            "name": _safe_name(lecture.get("title"), asset_id) + ext,
            "format": "mp4" if mp4 else f"not mp4 (Commons type {info.get('type')})",
            "content_type": "video/mp4" if mp4 else "application/octet-stream",
            "duration_s": info.get("duration"), "captions": info.get("exists_caption"),
            "script": info.get("exists_script"), "synced_updated_at": video.get("updated_at"),
            "downloaded_at": now, "last_used": now}
        self._write_meta(asset_id, meta)
        self._index(asset_id, meta, final)
        if previous and previous["file"] != final.name:
            (self.dir / previous["file"]).unlink(missing_ok=True)

    # --- fetchers: downloads another machine does for the queue -------------------------------------------------

    def _online(self, entry: dict) -> bool:
        """Heard from within FETCHER_SEEN_S seconds."""
        return datetime.now(KST) - datetime.fromisoformat(entry["last_seen"]) < timedelta(seconds=FETCHER_SEEN_S)

    def _resting(self, owner: str) -> bool:
        """Given no work for now, because its tries kept failing."""
        until = self._rest.get(owner)
        return until is not None and datetime.now(KST) < until

    def _heard(self, owner: str, *, name: str | None = None, platform: str | None = None,
               version: str | None = None) -> None:
        """A fetcher called. Its "last seen" is kept in the database too (now and then), so a restart still knows
        who was around: with local_downloads=fallback the server must not start downloading just because it
        restarted."""
        known = self.fetchers.get(owner)
        entry = {**(known or {}), "last_seen": _now()}
        entry.update({k: v for k, v in (("name", name), ("platform", platform), ("version", version)) if v is not None})
        self.fetchers[owner] = entry
        new = known is None or any(known.get(k) != entry.get(k) for k in ("name", "platform", "version"))
        if new or time.monotonic() - self._fetchers_saved >= FETCHERS_SAVE_S:
            self._fetchers_saved = time.monotonic()
            self.store.set_state("fetchers", self.fetchers)

    def _strike(self, owner: str) -> None:
        """A try of this fetcher's failed. After FETCHER_STRIKES in a row it is given no work for a while, so a
        fetcher that can't reach the content server (or keeps falling asleep) doesn't use up every job's tries."""
        if not owner:
            return
        streak = self._streak[owner] = self._streak.get(owner, 0) + 1
        if streak >= FETCHER_STRIKES:
            pause = FETCHER_REST_S[min(streak - FETCHER_STRIKES, len(FETCHER_REST_S) - 1)]
            self._rest[owner] = datetime.now(KST) + timedelta(seconds=pause)
            log.warning("fetcher %s: %d failed tries in a row; no work for it for %d s", owner, streak, pause)

    def _forget(self) -> None:
        """What was noted about jobs that are gone."""
        if not (self._server_only or any(self._too_big.values())):
            return
        queue = {row["asset_id"] for row in self.store.query("SELECT asset_id FROM video_jobs")}
        self._server_only = {asset: why for asset, why in self._server_only.items() if asset in queue}
        self._too_big = {owner: {asset: size for asset, size in sizes.items() if asset in queue}
                         for owner, sizes in self._too_big.items()}

    def fetcher_list(self) -> list[dict]:
        """The fetchers heard from lately: what each is called and runs on, which token it is, when it last
        called, how many downloads it holds and how fast those are going together."""
        out = []
        for owner, entry in sorted(self.fetchers.items(), key=lambda kv: (kv[1].get("name") or kv[0], kv[0])):
            held = [lease for lease in self.leases.values() if lease.owner == owner]
            speeds = [speed for lease in held if (speed := lease.live_speed()) is not None]
            row = {"name": entry.get("name") or owner, "token": owner, "platform": entry.get("platform") or None,
                   "version": entry.get("version") or None, "last_seen": _shown(entry["last_seen"]),
                   "online": self._online(entry), "active_jobs": len(held),
                   "speed_mbps": round(sum(speeds) / MB, 2) if speeds else None}
            if self._resting(owner):  # its tries kept failing: it gets no work until then
                row.update(failed_in_a_row=self._streak.get(owner, 0),
                           resting_until=self._rest[owner].isoformat(timespec="seconds"))
            out.append(row)
        return out

    def _lease_until(self) -> str:
        return (datetime.now(KST) + timedelta(seconds=LEASE_TTL_S)).isoformat(timespec="microseconds")

    def _expected(self, lecture: dict, original: int) -> int:
        """The bytes to keep room for: the size Commons gave, else what iCampus lists, capped at the largest video
        allowed (the listing can be several times the file)."""
        cap = int(self.settings.video_max_gb * 2**30)
        return original or min(lecture["video"].get("listed_size") or 0, cap)

    def _restore_fetchers(self) -> None:
        """A start: who was around, and the downloads fetchers were doing. Those are still theirs, with what they
        had uploaded, and each gets a full lease time from now, because no heartbeat could arrive while the server
        was down. A job that was claimed but not yet described goes back into the queue."""
        long_ago = (datetime.now(KST) - timedelta(days=FETCHER_KEPT_DAYS)).isoformat()
        stored = self.store.get_state("fetchers") or {}
        self.fetchers = {owner: entry for owner, entry in stored.items()
                         if isinstance(entry, dict) and str(entry.get("last_seen") or "") > long_ago}
        for row in self.store.query("SELECT * FROM video_jobs WHERE state='downloading' AND fetcher IS NOT NULL"):
            job = dict(row)
            try:
                info = json.loads(job["lease_info"] or "null")
            except ValueError:
                info = None
            if not job["lease_id"] or not isinstance(info, dict):  # nothing was uploaded for it yet
                self._set(job["id"], state="queued", **_NO_LEASE)
                continue
            owner = info.pop("owner", None) or job["fetcher"]
            original, lecture = commons.original_size(info), json.loads(job["lecture"])
            self.leases[job["asset_id"]] = Lease(
                job_id=job["id"], asset_id=job["asset_id"], fetcher=job["fetcher"], lease_id=job["lease_id"],
                owner=owner, confirmed=True, info=info, lecture=lecture, exact=original or None,
                total=original or None, done=job["downloaded_bytes"], progress_at=job["last_progress_at"],
                hashed=-1)  # -1: its upload is hashed from the disk at the end
            self._reserved[job["asset_id"]] = self._expected(lecture, original)
            self._set(job["id"], lease_expires_at=self._lease_until())

    def _end_lease(self, lease: Lease) -> None:
        """The lease is over, however: its room is given back, and a request still running for it stops."""
        lease.ended = True
        if self.leases.get(lease.asset_id) is lease:
            del self.leases[lease.asset_id]
            self._reserved.pop(lease.asset_id, None)

    def _give_back(self, job: dict, lease: Lease) -> None:
        """The job goes back into its place in the queue as if it had never been given away: no failed try, no
        pause. For a job that turned out not to be for this fetcher, or that nobody ever took up."""
        self._end_lease(lease)
        self._set(job["id"], state="queued", started_at=None, **_NO_LEASE)
        self._wake.set()

    def _lease_failed(self, job: dict, lease: Lease, message: str, transient: bool, *,
                      keep_part: bool = False) -> None:
        """A fetcher's try at a job ended without the file: the same pauses, retries and giving up as for a try of
        the server's own (_failed), and whoever waits for the video hears how it ended. What it had uploaded goes
        (`keep_part` is for a job that failed before it was the fetcher's: the .part is then still the server's)."""
        self._end_lease(lease)
        self._failed(job, lease, message, transient, keep_part=keep_part)
        self._release(job["asset_id"], lease.error, lease.transient)
        self._wake.set()

    def _expire(self) -> None:
        """Takes back the jobs of fetchers that went quiet (a closed lid, a lost network): each counts as a failed
        try and goes back into the queue, for the server or another fetcher. A lease nobody ever called about
        (the answer that gave the job away never arrived) goes back without a failed try."""
        rows = self.store.query("SELECT * FROM video_jobs WHERE state='downloading' AND fetcher IS NOT NULL "
                                "AND (lease_expires_at IS NULL OR lease_expires_at<?)", (_now(),))
        for row in rows:
            job = dict(row)
            lease = self.leases.get(job["asset_id"])
            if lease is None or lease.job_id != job["id"]:
                lease = Lease(job_id=job["id"], asset_id=job["asset_id"], fetcher=job["fetcher"], lease_id="",
                              owner=job["fetcher"], confirmed=True)
            self._strike(lease.owner)
            if not lease.confirmed:
                log.info("video %s: the fetcher %s never took up its lease; back into the queue", job["asset_id"],
                         job["fetcher"])
                self._give_back(job, lease)
                continue
            self._lease_failed(job, lease, f"the fetcher {job['fetcher']} went quiet: nothing was heard from it "
                                           f"for {LEASE_TTL_S:g} seconds", True)

    def _leased(self, job_id: int, lease_id: str, owner: str | None = None) -> tuple[dict, Lease] | None:
        """The job and its lease, for a call that names both. None when there is no such job any more (it was
        taken out of the queue, or it is finished); StaleLease when the job is no longer this lease's, or the
        lease was given to another token than the one that calls."""
        row = self.store.one("SELECT * FROM video_jobs WHERE id=?", (job_id,))
        if row is None:
            return None
        job = dict(row)
        lease = self.leases.get(job["asset_id"])
        current = job["lease_id"] if job["state"] == "downloading" else None
        if (not current or lease is None or lease.ended or lease.lease_id != current
                or not hmac.compare_digest(current.encode(), lease_id.encode())
                or (owner is not None and lease.owner != owner)):
            raise StaleLease()
        lease.confirmed = True
        return job, lease

    def finished(self, lease_id: str) -> int | None:
        """The size of the file a lease ended with, when its upload was taken and finished a short while ago; None
        otherwise. A fetcher that lost the answer to its last piece asks again, and hears that it is done instead
        of that its lease is stale."""
        for old in [key for key, (when, _) in self._uploaded.items() if time.monotonic() - when > UPLOADED_KEPT_S]:
            del self._uploaded[old]
        return next((size for key, (_, size) in self._uploaded.items()
                     if hmac.compare_digest(key.encode(), lease_id.encode())), None)

    def _renew(self, job: dict, lease: Lease, **fields: Any) -> None:
        """The fetcher is there: its lease runs LEASE_TTL_S seconds from now."""
        lease.beat = lease.renewed = time.monotonic()
        self._heard(lease.owner or lease.fetcher)
        self._set(job["id"], lease_expires_at=self._lease_until(), **fields)

    async def lease(self, owner: str, *, name: str | None = None, platform: str = "", version: str = "",
                    slots_free: int = 1, free_bytes: int | None = None) -> dict | None:
        """Gives the next due job to a fetcher, or None when there is none for it. `owner` is the label of the
        token it called with (who it is); `name` is what statuses call it. The job is claimed first, with the
        same update and in the same order as tick() claims one for this server. Then the server describes the
        video itself (no cookies), so the size the upload must have and the largest video allowed are the
        server's own knowledge, and makes room for it. A job the content server refuses to describe has failed a
        try, as it would have here, and the next one is looked at."""
        if slots_free <= 0 or self._stopped:
            return None  # only asking whether its token works, or the server is on its way out
        name = name or owner
        self._heard(owner, name=name, platform=platform, version=version)
        self._free[owner] = free_bytes
        self._expire()
        if self._resting(owner) or sum(1 for held in self.leases.values() if held.owner == owner) >= FETCHER_SLOTS:
            return None
        cap = int(self.settings.video_max_gb * 2**30)
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        too_big = self._too_big.setdefault(owner, {})
        passed: set[str] = set()
        started = time.monotonic()
        # A job that can't be given away must not hide the ones behind it, but the fetcher is waiting for an
        # answer: a few tries, and not for long.
        while len(passed) < 5 and time.monotonic() - started < LEASE_BUDGET_S:
            due = self.store.query("SELECT * FROM video_jobs WHERE state='queued' AND (not_before IS NULL OR "
                                   "not_before<=?) ORDER BY queued_at, id LIMIT 50", (_now(),))
            job = next((dict(row) for row in due if row["asset_id"] not in passed
                        and row["asset_id"] not in self.active and row["asset_id"] not in self.leases
                        and row["asset_id"] not in self._server_only
                        and commons.SAFE_ID.fullmatch(row["asset_id"])
                        and not (free_bytes is not None and too_big.get(row["asset_id"], 0) > free_bytes)), None)
            if job is None:
                return None
            asset_id = job["asset_id"]
            passed.add(asset_id)
            lecture = self._lecture(job)
            host = lecture["video"].get("host", commons.HOST)
            lease = Lease(job_id=job["id"], asset_id=asset_id, fetcher=name, owner=owner,
                          lease_id=secrets.token_urlsafe(24), lecture=lecture)
            if not self._claim(job["id"], _now(), fetcher=name, lease_id=lease.lease_id,
                               lease_expires_at=self._lease_until()):
                continue  # this server took it in the meantime
            self.leases[asset_id] = lease
            try:
                async with commons.attempt(self._transport, stall_s=min(STALL_S, LEASE_DESCRIBE_S)) as content:
                    info = await content.describe(host, asset_id, max_bytes=cap)
                if lease.ended:
                    continue  # taken out of the queue while Commons was asked
                original = commons.original_size(info)
                expected = self._expected(lecture, original)
                if free_bytes is not None and expected > free_bytes:
                    # Not for this machine: back into its place for the server or another fetcher, and no failed
                    # try. Remembered, so this fetcher isn't handed it again at every call.
                    too_big[asset_id] = expected
                    self._give_back(job, lease)
                    continue
                self._reserved[asset_id] = expected
                self._make_room(expected, keep=asset_id)
            except asyncio.CancelledError:  # the request was dropped: nobody has the job, and nothing failed
                if not lease.ended:
                    self._give_back(job, lease)
                raise
            except Exception as exc:
                if not isinstance(exc, (VideoError, httpx.HTTPError, OSError)):
                    log.warning("video %s: %s", asset_id, type(exc).__name__)
                if not lease.ended:  # as a try of the server's own would have failed, with what that one keeps
                    self._lease_failed(job, lease, *commons.failure(exc), keep_part=True)
                continue
            # The job is the fetcher's now. What a try of the server's own left on disk is not what the upload
            # continues: the upload starts a file of its own.
            self._part(asset_id).unlink(missing_ok=True)
            lease.info = {k: info.get(k) for k in LEASE_INFO}
            lease.exact = lease.total = original or None
            self._renew(job, lease, total_bytes=original or None, downloaded_bytes=0, part_version=None,
                        validator=None, boot=None, last_progress_at=_now(),
                        lease_info=json.dumps({**lease.info, "owner": owner}))
            log.info("video %s: leased to the fetcher %s", asset_id, name)
            return {"job_id": job["id"], "lease_id": lease.lease_id, "asset_id": asset_id, "host": host,
                    "title": lecture.get("title"), "expected_bytes": original or None, "max_bytes": cap,
                    "lease_ttl_s": int(LEASE_TTL_S)}
        return None

    def give_back(self, job_id: int, lease_id: str, owner: str | None = None) -> None:
        """A lease whose answer could not be delivered (the fetcher had hung up): the job is back in the queue at
        once, with no failed try."""
        try:
            found = self._leased(job_id, lease_id, owner)
        except StaleLease:
            return
        if found:
            self._give_back(*found)

    def heartbeat(self, job_id: int, lease_id: str, *, phase: str, done: int, total: int | None = None,
                  speed: float | None = None, owner: str | None = None) -> bool:
        """A fetcher says where its job stands, which renews its lease. False when the job is gone (it was taken
        out of the queue): the fetcher stops and deletes its file. Raises StaleLease."""
        found = self._leased(job_id, lease_id, owner)
        if found is None:
            return False
        job, lease = found
        if phase == "uploading":  # the download is complete there; `done` counts what it has sent
            downloaded = lease.exact or total or lease.done
        else:
            downloaded = done
        if (phase, downloaded) != (lease.phase, lease.done) or phase == "uploading":
            lease.progress_at = _now()
        lease.phase, lease.done, lease.speed = phase, downloaded, speed
        lease.total = lease.exact or total or lease.total
        self._renew(job, lease, downloaded_bytes=downloaded, last_progress_at=lease.progress_at)
        return True

    def received(self, job_id: int, lease_id: str, owner: str | None = None) -> int:
        """How many bytes of a fetcher's upload are here, so it can go on from there. Raises StaleLease."""
        found = self._leased(job_id, lease_id, owner)
        if found is None:
            raise StaleLease()
        job, lease = found
        self._renew(job, lease)
        return self._on_disk(job["asset_id"])

    def fail_lease(self, job_id: int, lease_id: str, message: str, transient: bool, owner: str | None = None) -> dict:
        """A fetcher reports that its try failed. The message is the fetcher's (cleaned here), the pauses and the
        number of tries are this queue's. Raises StaleLease."""
        found = self._leased(job_id, lease_id, owner)
        if found is None:
            raise StaleLease()
        job, lease = found
        self._heard(lease.owner or lease.fetcher)
        self._strike(lease.owner)
        self._lease_failed(job, lease, f"{lease.fetcher}: {_safe_message(message)}", bool(transient))
        after = self._job(job["asset_id"]) or {}
        return {"state": after.get("state"), "attempts": after.get("attempts"),
                "retry_at": _shown(after.get("not_before"))}

    async def receive(self, job_id: int, lease_id: str, *, start: int, length: int, total: int,
                      sha256: str | None, body: AsyncIterator[bytes], owner: str | None = None) -> dict:
        """Takes a piece of a fetcher's upload: `length` bytes that start at byte `start` of a file of `total`.
        The piece must start where the file here ends (OutOfStep says where that is), so an upload cut off by
        the network goes on from what arrived. With the last piece the file is checked against what this server
        knows of it: the size Commons gave when the job was leased, the SHA-256 the fetcher sends against the
        bytes here, and the first bytes of an MP4. Then it is finished like a download of the server's own.
        A failed check fails the try (Refused). Raises StaleLease."""
        found = self._leased(job_id, lease_id, owner)
        if found is None:
            raise StaleLease()
        async with found[1].lock:
            found = self._leased(job_id, lease_id, owner)  # it may have ended while this waited for its turn
            if found is None:
                raise StaleLease()
            job, lease = found
            asset_id = job["asset_id"]
            self._renew(job, lease)
            cap = int(self.settings.video_max_gb * 2**30)
            if lease.exact is not None and total != lease.exact:
                self._refuse(job, lease, f"the upload is {total} bytes, but Commons described a file of "
                                         f"{lease.exact}", True)
            if total > cap:
                self._refuse(job, lease, f"the video is {total / 2**30:.1f} GB; only videos up to "
                                         f"{cap / 2**30:g} GB are downloaded (ICAMPUS_VIDEO_MAX_GB)", False)
            have = self._on_disk(asset_id)
            if start != have:
                raise OutOfStep(have)
            if total > self._reserved.get(asset_id, 0):  # Commons gave no size when it was leased: now it is known
                self._reserved[asset_id] = total
                try:
                    self._make_room(total, keep=asset_id, on_disk=have)
                except VideoError as exc:
                    self._refuse(job, lease, str(exc), exc.transient)
            part = self._part(asset_id)
            wrote, too_long = 0, False
            pieces = body.__aiter__()
            try:
                self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                with os.fdopen(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab") as f:
                    while True:
                        try:  # a request that brings nothing must not hold the file open for ever
                            async with asyncio.timeout(UPLOAD_IDLE_S):
                                chunk = await anext(pieces)
                        except StopAsyncIteration:
                            break
                        except TimeoutError:
                            break  # what arrived stays; the fetcher, if it is still there, goes on from it
                        if lease.ended:
                            break  # taken back or out of the queue while the piece was arriving
                        if wrote + len(chunk) > length:
                            too_long = True
                            break
                        f.write(chunk)
                        f.flush()
                        wrote += len(chunk)
                        if lease.hashed == have + wrote - len(chunk):
                            lease.digest.update(chunk)
                            lease.hashed += len(chunk)
                        if time.monotonic() - lease.renewed >= PERSIST_S:
                            self._renew(job, lease)
                    if too_long:  # more than the piece was said to hold: none of it is kept
                        f.truncate(have)
            except OSError as exc:  # the disk, not the fetcher: the try failed here, and it is told so
                if lease.ended:
                    raise StaleLease() from None
                self._refuse(job, lease, commons.failure(exc)[0], True, strike=False)
            if lease.ended:
                raise StaleLease()
            if too_long:
                lease.hashed = -1
                raise ValueError("the request carried more bytes than its Content-Range names")
            here = have + wrote
            if here < total:
                return {"received_bytes": here, "complete": False}
            # all of it is here: is it the file?
            try:
                if lease.hashed != here:  # some of it arrived before a restart: read it back
                    own = await asyncio.to_thread(_sha256, part)
                    if lease.ended:
                        raise StaleLease()
                else:
                    own = lease.digest.hexdigest()
                mp4 = commons.is_mp4(part)
            except OSError as exc:
                self._refuse(job, lease, commons.failure(exc)[0], True, strike=False)
            if not sha256 or not hmac.compare_digest(own, sha256.strip().lower()):
                self._refuse(job, lease, "the uploaded file is not the one the fetcher downloaded (SHA-256 "
                                         "mismatch)" if sha256 else "the upload came without its SHA-256", True)
            if not mp4:
                # What the content server has for this video is no MP4 (or the fetcher sent something else). The
                # server's own download keeps such a file and labels it, so the job stays with the server from now
                # on instead of going round the fetchers.
                self._server_only[asset_id] = "not an MP4"
                self._refuse(job, lease, "the uploaded file is not an MP4; this video is left to the server's own "
                                         "download from now on", True)
            try:
                self._finish(lease.lecture or self._lecture(job), lease.info, part, here, True)
            except OSError as exc:
                self._refuse(job, lease, commons.failure(exc)[0], True, strike=False)
            self.store.execute("DELETE FROM video_jobs WHERE id=?", (job["id"],))  # done: the cache has it now
            self._end_lease(lease)
            self._uploaded[lease.lease_id] = (time.monotonic(), here)
            self._streak.pop(lease.owner, None)  # a fetcher that delivers is a fetcher that works
            self._rest.pop(lease.owner, None)
            self._release(asset_id, None, False)
            self._wake.set()
            log.info("video %s uploaded by the fetcher %s (%.0f MB)", asset_id, lease.fetcher, here / MB)
            return {"received_bytes": here, "complete": True}

    def _refuse(self, job: dict, lease: Lease, message: str, transient: bool, *, strike: bool = True) -> None:
        """An upload failed a check, or could not be kept: the try has failed, and the fetcher is told why.
        `strike` is off when the fault is the server's (its disk)."""
        if strike:
            self._strike(lease.owner)
        self._lease_failed(job, lease, f"{lease.fetcher}: {message}", transient)
        raise Refused(message)

    def bench_targets(self, count: int = 4) -> list[dict]:
        """A few hosted videos to measure download speed with (`bench`): asset id and host, the biggest first, so
        a measurement doesn't run out of file. Read from the synced lecture list: nothing is asked of anyone."""
        seen: dict[str, dict] = {}
        for row in self.store.records("lectures"):
            video = row.get("video") or {}
            asset_id, host = str(video.get("asset_id") or ""), video.get("host", commons.HOST)
            if commons.SAFE_ID.fullmatch(asset_id) and commons.host_ok(f"https://{host}/"):
                seen.setdefault(asset_id, {"asset_id": asset_id, "host": host, "size": video.get("listed_size") or 0})
        biggest = sorted(seen.values(), key=lambda t: t["size"], reverse=True)[:count]
        return [{"asset_id": t["asset_id"], "host": t["host"]} for t in biggest]
