"""Lecture videos: downloaded from SKKU's content server (Commons, lcms.skku.edu) on request and kept in var/videos.

The lecture list the sync reads names each hosted video's Commons asset. Commons describes the asset without a
login (`/CommonsCore2/v2/contents/<id>`) and serves the original file from `services/download/<id>` when the
request has an iCampus Referer. No cookies are sent, so none of this is tied to your account: no viewer or player
opens, and no watch progress or attendance is recorded (see docs/icampus-notes.md). The download redirects to an
address with a query string, which is never stored or logged.

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
"""

import asyncio
import errno
import json
import logging
import os
import re
import shutil
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .cache import Cache, CacheFull
from .config import CANVAS, KST, Settings
from .store import Store

log = logging.getLogger(__name__)
REFERER = f"{CANVAS}/"
MB = 2**20
MAX_DOWNLOAD_S = 6 * 3600  # one try (4 GB at 0.2 MB per second); a dead connection ends far sooner, see STALL_S
STALL_S = 60.0  # a try ends when the content server sends nothing for this long
STALL_SHOWN_S = 15.0  # from here on a status says `stalled`
RETRY_AFTER_S = (30, 120, 300, 900, 1800, 3600)  # the pause before the nth retry of an export
TICK_S = 5.0  # how often the worker looks at the queue when nothing wakes it
PERSIST_S = 5.0  # how often a running download's progress goes into the database
SPEED_WINDOW_S = 20.0  # the speed in a status is the average over this long
DISK_MARGIN = 1 << 30  # leave this much free on the disk
OPEN = ("queued", "downloading")
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


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


class VideoError(Exception):
    """A message safe to show: no URLs with queries, no tokens. `transient` marks a failure that may pass by
    itself (the network, a full cache), so the job tries again later."""

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def find(store: Store, item_id: str) -> dict | None:
    """The synced lecture item with this module item id, if it is a Commons-hosted video."""
    suffix = f":modules/items:{item_id}"
    for row in store.records("lectures", include_inactive=True):
        if row.get("key", "").endswith(suffix) and row.get("video"):
            return row
    return None


def _host_ok(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").endswith(".skku.edu")


def _safe_name(title: str | None, fallback: str) -> str:
    name = " ".join(_UNSAFE_NAME.sub(" ", title or "").split()).strip(". ")
    return name[:150] or fallback


def _validator(headers: httpx.Headers) -> str | None:
    """What a later Range request can name to be sure the file is still the same: a strong ETag, else the date."""
    etag = headers.get("etag") or ""
    return etag if etag and not etag.startswith("W/") else headers.get("last-modified") or None


def _snapshot(lecture: dict) -> str:
    """What a download needs from a synced lecture row, kept with its job so the queue outlives a restart."""
    video = lecture["video"]
    return json.dumps({**{k: lecture.get(k) for k in ("key", "course_id", "item_id", "title")},
                       "video": {k: video[k] for k in ("asset_id", "host", "listed_size", "updated_at")
                                 if video.get(k) is not None}}, ensure_ascii=False)


@dataclass
class Running:
    """A download this process is working on: the live numbers behind its status."""
    done: int = 0
    total: int | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    removed: bool = False  # taken out of the queue by hand while it ran
    note: str | None = None
    progress_at: str | None = None  # when bytes last arrived
    seen: float = field(default_factory=time.monotonic)  # when the content server last answered or sent bytes
    persisted: float = 0.0
    samples: deque = field(default_factory=deque, repr=False)  # (monotonic time, bytes) for the speed

    def restart(self, done: int, total: int | None) -> None:
        """The try (re)starts counting at `done` bytes."""
        self.done, self.total = done, total
        self.samples.clear()
        self.samples.append((time.monotonic(), done))

    def arrived(self, n: int) -> bool:
        """Counts n more bytes. True about once a second, when a sample for the speed was taken."""
        self.done += n
        self.seen = now = time.monotonic()
        if self.samples and now - self.samples[-1][0] < 1:
            return False
        self.samples.append((now, self.done))
        while now - self.samples[0][0] > SPEED_WINDOW_S:
            self.samples.popleft()
        self.progress_at = _now()
        return True

    def speed(self) -> float | None:
        """Bytes per second over the last seconds. It falls towards zero while nothing arrives."""
        if not self.samples:
            return None
        since, had = self.samples[0]
        elapsed = time.monotonic() - since
        return max(0.0, (self.done - had) / elapsed) if elapsed >= 1 else None


class Videos:
    def __init__(self, settings: Settings, cache: Cache, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.cache = cache
        self.store: Store = cache.store
        self.dir = settings.data_dir / "videos"
        self.active: dict[str, Running] = {}  # asset id -> the download running now
        self.heartbeat_at: str | None = None  # when the worker loop last looked at the queue
        self._working = False
        self._waiters: dict[str, list[asyncio.Future]] = {}  # asset id -> who waits for its next try to end
        self._reserved: dict[str, int] = {}  # asset id -> the bytes a running download will take
        self._wake = asyncio.Event()
        self._transport = transport
        self._recover()  # a restart: what was downloading goes on from the queue, nothing is dropped
        wanted = {f"{row['asset_id']}.part" for row in self.store.query(
            "SELECT asset_id FROM video_jobs WHERE state='queued'")}
        for leftover in self.dir.glob("*.part"):  # of a job that is gone: nothing would ever continue it
            if leftover.name not in wanted:
                leftover.unlink(missing_ok=True)
        cache.adopter(self.adopt)

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

    def _make_room(self, needed: int, keep: str, on_disk: int = 0) -> None:
        """Room for a download of `needed` bytes, `on_disk` of which are here already, beside what the other
        running downloads will still bring."""
        cap = int(self.settings.video_cache_gb * 2**30)
        if needed > cap:
            raise VideoError(f"the video ({needed / 2**30:.1f} GB) is bigger than the whole video cache "
                             f"({self.settings.video_cache_gb:g} GB, ICAMPUS_VIDEO_CACHE_GB)")
        others = {asset: size for asset, size in self._reserved.items() if asset != keep}
        if needed + sum(others.values()) > cap:
            raise VideoError("the video cache has no room for this video beside the downloads that are running",
                             transient=True)
        try:
            self.cache.make_room("video", needed + sum(others.values()), keep={("video", keep)})
        except CacheFull as exc:  # what is left is pinned or waiting for a transcription: room comes back later
            raise VideoError(str(exc), transient=True) from None
        coming = sum(max(0, size - self.active[asset].done) for asset, size in others.items() if asset in self.active)
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
        for row in self.store.query("SELECT id, asset_id FROM video_jobs WHERE state='downloading'"):
            if row["asset_id"] not in self.active:
                self._set(row["id"], state="queued", downloaded_bytes=self._on_disk(row["asset_id"]))

    def _slots(self) -> int:
        return max(1, int(self.settings.download_concurrency))

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
            outdated = bool(video.get("updated_at")) and meta.get("synced_updated_at") != video.get("updated_at")
            out.update(state="outdated" if outdated else "ready",
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
        done = live.done if live else job["downloaded_bytes"]
        total = (live.total if live else None) or job["total_bytes"]
        out: dict[str, Any] = {"state": job["state"], "downloaded_mb": round(done / MB),
                               "total_mb": round(total / MB) if total else None,
                               "queued_at": _shown(job["queued_at"]),
                               "attempts": job["attempts"] + int(job["state"] == "downloading")}
        if job["state"] == "queued":
            out["queue_position"] = 1 + self.store.one(
                "SELECT COUNT(*) AS n FROM video_jobs WHERE state='queued' AND (queued_at<? OR (queued_at=? AND id<?))",
                (job["queued_at"], job["queued_at"], job["id"]))["n"]
            if job["not_before"] and job["not_before"] > _now():
                out["retry_at"] = _shown(job["not_before"])
        else:
            out["started_at"] = _shown(job["started_at"])
            out["last_progress_at"] = _shown((live.progress_at if live else None) or job["last_progress_at"])
            if live:
                speed = live.speed()
                out["speed_mbps"] = round(speed / MB, 2) if speed is not None else None  # megabytes per second
                out["eta_s"] = round((total - done) / speed) if speed and total and total >= done else None
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
                if k in ("downloaded_mb", "total_mb", "queue_position", "speed_mbps", "eta_s", "stalled")}

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
        shown = ("downloaded_mb", "total_mb", "speed_mbps", "eta_s", "last_progress_at", "attempts", "stalled",
                 "last_error")
        active = [named(row) | {k: v for k, v in self._progress(row).items() if k in shown and v is not None}
                  for row in rows if row["state"] == "downloading"]
        failed = [named(row) | {"error": row["error"], "failed_at": _shown(row["finished_at"])}
                  for row in rows if row["state"] == "failed"]
        waiting = [row["item_id"] for row in rows if row["state"] == "queued"]
        return {"queued": counts["queued"], "downloading": counts["downloading"], "failed": counts["failed"],
                "ready": len(self.cache.entries("video")), "active": active, "next": waiting[:5],
                "recently_failed": failed[-5:],
                "worker": {"running": self._working, "heartbeat_at": _shown(self.heartbeat_at),
                           "concurrency": self._slots()}}

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
        if job and not (retry or refresh):
            return status  # failed: shown as that (or as the copy that is still here, with `last_error`)
        if status["state"] == "ready" and not refresh:
            return status
        now = _now()
        fresh = {"lecture": _snapshot(lecture), "item_id": lecture.get("item_id"), "state": "queued",
                 "keep_trying": int(keep_trying), "attempts": 0, "not_before": None, "error": None, "transient": 0,
                 "total_bytes": None, "downloaded_bytes": 0, "part_version": None, "validator": None, "boot": None,
                 "queued_at": now, "started_at": None, "last_progress_at": None, "finished_at": None}
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
        live = self.active.get(asset_id)
        if live and live.task and not live.task.done():
            live.removed = True
            live.task.cancel()  # it removes its .part on the way out, and the next job starts
        else:
            self._part(asset_id).unlink(missing_ok=True)
            self._release(asset_id)
        return True

    async def ensure(self, lecture: dict) -> Path:
        """The video's file, downloading it first unless a current copy is cached. The download goes through the
        same queue as exports, so /videos/{item_id} shows its progress, and this waits for the end of its next
        try. Raises VideoError; the caller decides about trying again."""
        asset_id = lecture["video"]["asset_id"]
        if self.start(lecture, retry=True, keep_trying=False)["state"] != "ready":
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.setdefault(asset_id, []).append(waiter)
            try:
                await waiter
            finally:
                if waiter in (waiting := self._waiters.get(asset_id, [])):
                    waiting.remove(waiter)
            status = self.status(lecture)
            if status["state"] != "ready":
                job = self._job(asset_id)
                raise VideoError(status.get("error") or status.get("last_error") or "the download did not finish",
                                 transient=bool(job and (job["transient"] or job["state"] in OPEN)))
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
        self._working = True
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
        self._recover()
        room = self._slots() - len(self.active)
        if room <= 0:
            return
        loop = asyncio.get_running_loop()
        due = self.store.query("SELECT * FROM video_jobs WHERE state='queued' AND (not_before IS NULL OR not_before<=?)"
                               " ORDER BY queued_at, id LIMIT ?", (_now(), room))
        for row in due:
            job = dict(row)
            now = _now()
            claimed = self.store.execute(
                "UPDATE video_jobs SET state='downloading', started_at=?, not_before=NULL, updated_at=? "
                "WHERE id=? AND state='queued'", (now, now, job["id"])).rowcount
            if not claimed or job["asset_id"] in self.active:
                continue
            live = self.active[job["asset_id"]] = Running(done=job["downloaded_bytes"], total=job["total_bytes"])
            live.task = loop.create_task(self._attempt(job, live))
            live.task.add_done_callback(lambda task, job=job, live=live: self._finished(job["asset_id"], live, task))

    def _kick(self) -> None:
        """Something changed in the queue: start what can start now, and let the worker loop know."""
        self._wake.set()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # asked from outside the event loop: the worker loop starts it
        try:
            self.tick()
        except Exception as exc:  # the job is in the queue either way; the worker loop comes round again
            log.error("video queue: %s", type(exc).__name__)

    def _release(self, asset_id: str) -> None:
        for waiter in self._waiters.pop(asset_id, []):
            if not waiter.done():
                waiter.set_result(None)

    def _finished(self, asset_id: str, live: Running, task: asyncio.Task) -> None:
        if self.active.get(asset_id) is live:
            del self.active[asset_id]
        self._reserved.pop(asset_id, None)
        self._release(asset_id)
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
        except VideoError as exc:
            self._failed(job, str(exc), exc.transient)
        except TimeoutError:
            self._failed(job, f"the download took longer than {MAX_DOWNLOAD_S // 3600} hours", True)
        except httpx.HTTPError as exc:  # the message can hold the redirect URL: keep only the type
            self._failed(job, f"lcms.skku.edu: {type(exc).__name__}", True)
        except OSError as exc:
            self._failed(job, f"could not write the video to disk ({errno.errorcode.get(exc.errno, 'OSError')})", True)
        except Exception as exc:
            log.warning("video %s: %s", asset_id, type(exc).__name__)
            self._failed(job, type(exc).__name__, False)

    def _failed(self, job: dict, message: str, transient: bool) -> None:
        """A try failed. One that may pass is made again after a pause, as often as export_retries allows (never
        for a download whose caller retries by itself); anything else ends the job, visibly."""
        asset_id = job["asset_id"]
        current = self._job(asset_id)  # an export may have asked for it while it ran
        if current is None or current["id"] != job["id"]:
            self._part(asset_id).unlink(missing_ok=True)
            return  # taken out of the queue meanwhile
        attempts = job["attempts"] + 1
        retries = int(self.settings.export_retries) if current["keep_trying"] else 0
        if transient and attempts <= retries:
            pause = RETRY_AFTER_S[min(attempts, len(RETRY_AFTER_S)) - 1]
            again = datetime.now(KST) + timedelta(seconds=pause)
            self._set(job["id"], state="queued", attempts=attempts, error=message, transient=1,
                      not_before=again.isoformat(timespec="microseconds"), downloaded_bytes=self._on_disk(asset_id))
            log.info("video %s: %s; trying again in %d s (retry %d of %d)", asset_id, message, pause, attempts, retries)
            return
        self._part(asset_id).unlink(missing_ok=True)
        if transient and retries:
            message = f"{message} (gave up after {attempts} tries)"
        self._set(job["id"], state="failed", attempts=attempts, error=message, transient=int(transient),
                  not_before=None, downloaded_bytes=0, finished_at=_now())
        log.warning("video %s failed: %s", asset_id, message)

    def _client(self) -> httpx.AsyncClient:
        # No cookies, ever: these requests must not carry the iCampus session. The read timeout sits just behind
        # the stall watchdog in _download, which notices first and says what happened.
        return httpx.AsyncClient(transport=self._transport, follow_redirects=True, max_redirects=3,
                                 timeout=httpx.Timeout(STALL_S + 15, connect=20),
                                 headers={"User-Agent": "Mozilla/5.0", "Referer": REFERER})

    async def _describe(self, client: httpx.AsyncClient, host: str, asset_id: str) -> dict:
        r = await client.get(f"https://{host}/CommonsCore2/v2/contents/{asset_id}")
        if r.status_code != 200:
            raise VideoError(f"Commons did not describe this video (HTTP {r.status_code})",
                             transient=r.status_code >= 500 or r.status_code == 429)
        try:
            result = r.json().get("result")
        except (ValueError, AttributeError):
            result = None
        if not isinstance(result, dict):
            raise VideoError("Commons answered with something unexpected")
        return result

    def _kept(self, job: dict, version: str) -> int:
        """How much of an interrupted try can be continued: the .part's bytes, when they belong to this version of
        the video, were written during this run of the machine and leave something to fetch. Otherwise 0."""
        size = self._on_disk(job["asset_id"])
        if job["part_version"] != version or job["boot"] != _BOOT or not 0 < size < (job["total_bytes"] or 0):
            return 0
        return size

    @staticmethod
    def _continues(r: httpx.Response, offset: int, job: dict) -> bool:
        """Whether a 206 answer really is the rest of the file the .part began: from our offset, of the same total
        size, unencoded, and with the same validator when both sides have one."""
        m = _CONTENT_RANGE.fullmatch((r.headers.get("content-range") or "").strip())
        if not m or int(m.group(1)) != offset or int(m.group(3)) != job["total_bytes"]:
            return False
        if (r.headers.get("content-encoding") or "identity").lower() != "identity":
            return False
        validator = _validator(r.headers)
        return not (job["validator"] and validator and validator != job["validator"])

    async def _download(self, lecture: dict, job: dict, live: Running) -> None:
        video = lecture["video"]
        asset_id, host = video["asset_id"], video.get("host", "lcms.skku.edu")
        version = video.get("updated_at") or ""
        cap = int(self.settings.video_max_gb * 2**30)
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        part = self._part(asset_id)
        loop = asyncio.get_running_loop()
        exact: int | None = None  # the size the finished file must have, when the content server says
        try:
            async with asyncio.timeout(STALL_S) as watchdog, self._client() as client:
                def alive() -> None:  # the content server answered or sent bytes: the stall clock starts over
                    watchdog.reschedule(loop.time() + STALL_S)
                    live.seen = time.monotonic()

                info = await self._describe(client, host, asset_id)
                alive()
                if info.get("is_original_file_deleted"):
                    raise VideoError("Commons no longer has this video's original file")
                original = info.get("original_file_length") or 0
                if original > cap:
                    raise VideoError(f"the video is {original / 2**30:.1f} GB; only videos up to "
                                     f"{self.settings.video_max_gb:g} GB are downloaded (ICAMPUS_VIDEO_MAX_GB)")
                # Without a size from Commons, room is made for what iCampus lists, capped at the largest video
                # allowed: the listing can be several times the file, so it never refuses a download by itself.
                expected = original or min(video.get("listed_size") or 0, cap)
                kept = self._kept(job, version)
                live.restart(kept, original or job["total_bytes"])
                self._reserved[asset_id] = expected
                self._make_room(expected, keep=asset_id, on_disk=kept)
                url = info.get("download_url")
                if not (isinstance(url, str) and _host_ok(url)):
                    url = f"https://{host}/CommonsCore2/v2/services/download/{asset_id}"
                for offset in ((kept, 0) if kept else (0,)):  # the rest of the file first, when some of it is here
                    headers = {}
                    if offset:
                        headers = {"Range": f"bytes={offset}-", "Accept-Encoding": "identity"}
                        if job["validator"]:
                            headers["If-Range"] = job["validator"]
                    async with client.stream("GET", url, headers=headers) as r:
                        alive()
                        if not _host_ok(str(r.url)):
                            raise VideoError("the download was redirected away from SKKU; stopped")
                        resumed = bool(offset) and r.status_code == 206 and self._continues(r, offset, job)
                        if offset and not resumed and r.status_code in (206, 416):
                            continue  # not the rest of what is here: ask for the whole file
                        if r.status_code != (206 if resumed else 200):
                            raise VideoError(f"Commons refused the download (HTTP {r.status_code})",
                                             transient=r.status_code >= 500 or r.status_code == 429)
                        if "text/html" in (r.headers.get("content-type") or ""):
                            raise VideoError("Commons sent a web page instead of the video")
                        plain = (r.headers.get("content-encoding") or "identity").lower() == "identity"
                        length = r.headers.get("content-length") or ""
                        if resumed:
                            exact = job["total_bytes"]
                        elif plain and length.isdigit() and int(length):
                            exact = int(length)
                        total = exact or expected or None
                        if total and total > cap:
                            raise VideoError(f"the video is {total / 2**30:.1f} GB; only videos up to "
                                             f"{self.settings.video_max_gb:g} GB are downloaded")
                        if kept and not resumed:  # the whole file again. Said, so that it isn't a silent reset.
                            live.note = (f"the content server did not continue the interrupted download: this try "
                                         f"started over from zero ({kept // MB} MB had arrived before)")
                            log.info("video %s: the content server did not honour Range; starting over", asset_id)
                        live.restart(offset if resumed else 0, total)
                        self._set(job["id"], total_bytes=total, downloaded_bytes=live.done, part_version=version,
                                  validator=_validator(r.headers) if plain else None, boot=_BOOT,
                                  last_progress_at=_now())
                        fd = os.open(part, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if resumed else os.O_TRUNC), 0o600)
                        with os.fdopen(fd, "ab" if resumed else "wb") as f:
                            async for chunk in r.aiter_bytes():
                                f.write(chunk)
                                f.flush()  # into the kernel's cache, where it outlives this process
                                alive()
                                if live.arrived(len(chunk)) and time.monotonic() - live.persisted >= PERSIST_S:
                                    live.persisted = time.monotonic()
                                    self._set(job["id"], downloaded_bytes=live.done, last_progress_at=live.progress_at)
                                if live.done > cap:
                                    raise VideoError(f"the video is over {self.settings.video_max_gb:g} GB; stopped")
                    break
        except TimeoutError:
            raise VideoError(f"the content server sent nothing for {STALL_S:g} seconds", transient=True) from None
        if exact is not None and live.done != exact:
            raise VideoError("the download was cut short; try again", transient=True)
        with open(part, "rb") as f:
            head = f.read(16)
        mp4 = head[4:8] == b"ftyp"
        described = str(info.get("extension") or "").lower()
        ext = ".mp4" if mp4 else (f".{described}" if re.fullmatch(r"[a-z0-9]{1,8}", described)
                                  and described != "mp4" else ".bin")  # don't name a non-mp4 .mp4
        final = self.dir / f"{asset_id}{ext}"
        previous = self.meta(asset_id)  # an outdated copy being replaced
        os.replace(part, final)
        now = datetime.now(KST).isoformat()
        meta = {
            "asset_id": asset_id, "file": final.name, "size": live.done, "item_id": lecture.get("item_id"),
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
        log.info("video %s downloaded (%.0f MB)", asset_id, live.done / MB)
