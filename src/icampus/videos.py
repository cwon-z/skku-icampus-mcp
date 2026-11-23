"""Lecture videos: downloaded from SKKU's content server (Commons, lcms.skku.edu) on request and kept in var/videos.

The lecture list the sync reads names each hosted video's Commons asset. Commons describes the asset without a
login (`/CommonsCore2/v2/contents/<id>`) and serves the original file from `services/download/<id>` when the
request has an iCampus Referer. No cookies are sent, so none of this is tied to your account: no viewer or player
opens, and no watch progress or attendance is recorded (see docs/icampus-notes.md). The download redirects to an
address with a query string, which is never stored or logged.

Downloads run one at a time in the background. A finished video is reused until a sync sees a new version of it.
The cache manager (cache.py) indexes the files and makes room: once the videos pass video_cache_gb, the ones used
least recently are deleted first, except those you pinned and those a transcription job still needs.
"""

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .cache import Cache, CacheFull
from .config import CANVAS, KST, Settings
from .store import Store

log = logging.getLogger(__name__)
REFERER = f"{CANVAS}/"
CHUNK = 1 << 20
MAX_DOWNLOAD_S = 3 * 3600
DISK_MARGIN = 1 << 30  # leave this much free on the disk
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


class VideoError(Exception):
    """A message safe to show: no URLs with queries, no tokens. `transient` marks a failure that may pass by
    itself (the network, a full cache), so a queued job tries again later."""

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


@dataclass
class Job:
    state: str  # queued, downloading, failed
    total: int | None = None
    done: int = 0
    error: str | None = None
    transient: bool = False  # the failure may pass by itself (network, full cache)
    task: asyncio.Task | None = field(default=None, repr=False)


class Videos:
    def __init__(self, settings: Settings, cache: Cache, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.cache = cache
        self.dir = settings.data_dir / "videos"
        for leftover in self.dir.glob("*.part"):  # a download cut short by a restart (the folder comes with the first)
            leftover.unlink(missing_ok=True)
        self.jobs: dict[str, Job] = {}
        self._one_at_a_time = asyncio.Lock()
        self._transport = transport
        cache.adopter(self.adopt)

    # --- the cache ---------------------------------------------------------

    def _meta_path(self, asset_id: str) -> Path:
        return self.dir / f"{asset_id}.json"

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

    def _make_room(self, needed: int, keep: str) -> None:
        cap = int(self.settings.video_cache_gb * 2**30)
        if needed > cap:
            raise VideoError(f"the video ({needed / 2**30:.1f} GB) is bigger than the whole video cache "
                             f"({self.settings.video_cache_gb:g} GB, ICAMPUS_VIDEO_CACHE_GB)")
        try:
            self.cache.make_room("video", needed, keep={("video", keep)})
        except CacheFull as exc:  # what is left is pinned or waiting for a transcription: room comes back later
            raise VideoError(str(exc), transient=True) from None
        if shutil.disk_usage(self.dir).free < needed + DISK_MARGIN:
            raise VideoError("not enough free disk space for this video", transient=True)

    # --- status ----------------------------------------------------------

    def status(self, lecture: dict) -> dict:
        video = lecture["video"]
        asset_id = video["asset_id"]
        out: dict[str, Any] = {"item_id": lecture.get("item_id"), "title": lecture.get("title"),
                               "listed_size_mb": round((video.get("listed_size") or 0) / 2**20) or None}
        meta = self.meta(asset_id)
        job = self.jobs.get(asset_id)
        if job and job.state in ("queued", "downloading"):
            out.update(state=job.state, downloaded_mb=round(job.done / 2**20),
                       total_mb=round(job.total / 2**20) if job.total else None)
        elif meta:
            outdated = bool(video.get("updated_at")) and meta.get("synced_updated_at") != video.get("updated_at")
            out.update(state="outdated" if outdated else "ready",
                       **{k: meta.get(k) for k in ("format", "content_type", "downloaded_at", "captions", "script")},
                       size_mb=round((meta.get("size") or 0) / 2**20))
            if job and job.state == "failed":  # refreshing an outdated copy failed; the old one is still here
                out["last_error"] = job.error
        elif job and job.state == "failed":
            out.update(state="failed", error=job.error)
        else:
            out["state"] = "not_downloaded"
        return out

    # --- downloading -------------------------------------------------------

    def start(self, lecture: dict) -> dict:
        """Starts a download unless the video is cached and current, or already on its way."""
        status = self.status(lecture)
        if status["state"] in ("ready", "queued", "downloading"):
            return status
        asset_id = lecture["video"]["asset_id"]
        job = self.jobs[asset_id] = Job("queued")
        job.task = asyncio.get_running_loop().create_task(self._run(lecture, job))
        return self.status(lecture)

    async def ensure(self, lecture: dict) -> Path:
        """The video's file, downloading it first unless a current copy is cached. Shares the one-at-a-time lock
        and the job record with exports, so /videos/{item_id} shows the progress. Raises VideoError."""
        asset_id = lecture["video"]["asset_id"]
        if self.start(lecture)["state"] != "ready":
            job = self.jobs.get(asset_id)
            if job and job.task:
                await asyncio.shield(job.task)
            status = self.status(lecture)
            if status["state"] != "ready":
                job = self.jobs.get(asset_id)
                raise VideoError(status.get("error") or status.get("last_error") or "the download did not finish",
                                 transient=bool(job and job.transient))
        self.touch(asset_id)
        path = self.path(asset_id)
        if path is None:
            raise VideoError("the video left the cache right after its download", transient=True)
        return path

    async def _run(self, lecture: dict, job: Job) -> None:
        asset_id = lecture["video"]["asset_id"]
        try:
            async with self._one_at_a_time:
                job.state = "downloading"
                async with asyncio.timeout(MAX_DOWNLOAD_S):
                    await self._download(lecture, job)
            del self.jobs[asset_id]
        except VideoError as exc:
            job.state, job.error, job.transient = "failed", str(exc), exc.transient
        except TimeoutError:
            job.state, job.error = "failed", f"the download took longer than {MAX_DOWNLOAD_S // 3600} hours"
            job.transient = True
        except httpx.HTTPError as exc:  # the message can hold the redirect URL: keep only the type
            job.state, job.error, job.transient = "failed", f"lcms.skku.edu: {type(exc).__name__}", True
        except Exception as exc:
            log.warning("video %s: %s", asset_id, type(exc).__name__)
            job.state, job.error = "failed", type(exc).__name__
        finally:
            (self.dir / f"{asset_id}.part").unlink(missing_ok=True)

    def _client(self) -> httpx.AsyncClient:
        # No cookies, ever: these requests must not carry the iCampus session.
        return httpx.AsyncClient(transport=self._transport, follow_redirects=True, max_redirects=3,
                                 timeout=httpx.Timeout(60, connect=20),
                                 headers={"User-Agent": "Mozilla/5.0", "Referer": REFERER})

    async def _describe(self, client: httpx.AsyncClient, host: str, asset_id: str) -> dict:
        r = await client.get(f"https://{host}/CommonsCore2/v2/contents/{asset_id}")
        if r.status_code != 200:
            raise VideoError(f"Commons did not describe this video (HTTP {r.status_code})")
        try:
            result = r.json().get("result")
        except (ValueError, AttributeError):
            result = None
        if not isinstance(result, dict):
            raise VideoError("Commons answered with something unexpected")
        return result

    async def _download(self, lecture: dict, job: Job) -> None:
        video = lecture["video"]
        asset_id, host = video["asset_id"], video.get("host", "lcms.skku.edu")
        cap = int(self.settings.video_max_gb * 2**30)
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        async with self._client() as client:
            info = await self._describe(client, host, asset_id)
            if info.get("is_original_file_deleted"):
                raise VideoError("Commons no longer has this video's original file")
            expected = info.get("original_file_length") or video.get("listed_size") or 0
            if expected > cap:
                raise VideoError(f"the video is {expected / 2**30:.1f} GB; only videos up to "
                                 f"{self.settings.video_max_gb:g} GB are downloaded (ICAMPUS_VIDEO_MAX_GB)")
            self._make_room(expected, keep=asset_id)
            url = info.get("download_url")
            if not (isinstance(url, str) and _host_ok(url)):
                url = f"https://{host}/CommonsCore2/v2/services/download/{asset_id}"
            part = self.dir / f"{asset_id}.part"
            async with client.stream("GET", url) as r:
                if not _host_ok(str(r.url)):
                    raise VideoError("the download was redirected away from SKKU; stopped")
                if r.status_code != 200:
                    raise VideoError(f"Commons refused the download (HTTP {r.status_code})",
                                     transient=r.status_code >= 500 or r.status_code == 429)
                if "text/html" in (r.headers.get("content-type") or ""):
                    raise VideoError("Commons sent a web page instead of the video")
                job.total = int(r.headers.get("content-length") or 0) or expected or None
                if job.total and job.total > cap:
                    raise VideoError(f"the video is {job.total / 2**30:.1f} GB; only videos up to "
                                     f"{self.settings.video_max_gb:g} GB are downloaded")
                fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                head = b""
                with os.fdopen(fd, "wb") as f:
                    async for chunk in r.aiter_bytes(CHUNK):
                        if len(head) < 16:
                            head += chunk[:16]
                        f.write(chunk)
                        job.done += len(chunk)
                        if job.done > cap:
                            raise VideoError(f"the video is over {self.settings.video_max_gb:g} GB; stopped")
                length = r.headers.get("content-length")
            if length and int(length) != job.done:
                raise VideoError("the download was cut short; try again", transient=True)
        mp4 = head[4:8] == b"ftyp"
        described = str(info.get("extension") or "").lower()
        ext = ".mp4" if mp4 else (f".{described}" if re.fullmatch(r"[a-z0-9]{1,8}", described)
                                  and described != "mp4" else ".bin")  # don't name a non-mp4 .mp4
        final = self.dir / f"{asset_id}{ext}"
        previous = self.meta(asset_id)  # an outdated copy being replaced
        os.replace(part, final)
        now = datetime.now(KST).isoformat()
        meta = {
            "asset_id": asset_id, "file": final.name, "size": job.done, "item_id": lecture.get("item_id"),
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
        log.info("video %s downloaded (%.0f MB)", asset_id, job.done / 2**20)
