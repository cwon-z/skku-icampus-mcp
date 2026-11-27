"""icampus-fetcher: download lecture videos for the icampus server on another machine, and upload them to it.

The server's queue hands out one job at a time (an asset id and a host on SKKU's content server). The fetcher
downloads that video here, where the content server may answer faster than it does to the server, sends the file
to the server, and deletes its copy once the server has confirmed it. The server stays the only place where
videos are kept, transcribed and served (docs/fetchers.md).

  icampus-fetcher check    server, token, content server, disk space, work directory
  icampus-fetcher bench    download speed from here, at 1, 2 and 3 connections
  icampus-fetcher run      take jobs until stopped (Ctrl+C)

Settings: ICAMPUS_FETCHER_* in the environment or in a `.env` in the directory it is started from, or flags.

What a fetcher has and does:
- a fetcher token, which opens the fetcher routes of the server and nothing else. No SKKU login, no iCampus
  session, no speech-to-text key: the content server needs none, and the fetcher is given none;
- requests to the content server go through commons.py, the same code the server downloads with: no cookies, and
  the download's redirect address is never stored, logged, or sent anywhere, the server included;
- a video is lecture content: it lies in a private per-user directory, named by its asset id only, for as long as
  the upload takes, and never in a folder that iCloud, OneDrive or Dropbox would copy;
- a message it logs or reports names an exception's type or a status code, never an address, a header or a token.

This module imports the standard library, httpx and pydantic-settings only (and commons.py, bench.py and
machine.py, which do the same): no browser, no web framework, no database, no ffmpeg.
"""

import argparse
import asyncio
import hashlib
import ipaddress
import logging
import os
import random
import secrets
import shutil
import signal
import sys
import time
import unicodedata
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import bench, commons, machine

log = logging.getLogger(__name__)
MB = 2**20
API = "/api/v1/fetchers"
HEARTBEAT_S = 5.0  # how often a job's progress is reported, which is also what keeps its lease
IDLE_MIN_S, IDLE_MAX_S = 2.0, 30.0  # the wait between two looks for work grows from the one to the other
PIECE = 8 * MB  # an upload goes in pieces of this size, so a network blip costs one piece at most
LOST_AFTER_S = 600.0  # a job is given up when the server hasn't answered about it for this long
DISK_MARGIN = 1 << 30  # leave this much free on the disk
AFTER_FAILURE_S = (5.0, 300.0)  # after a failed job the next look for work waits, longer each time in a row
LEASE_ANSWER_S = 45.0  # how long a request for work may take: the server asks the content server before it answers
UPLOAD_STUCK_S = 600.0  # an upload that gets nowhere for this long is given up, and the job handed back
UPLOAD_RETRY_S = (1.0, 15.0)  # the wait before asking the server again after a piece went wrong: first, and at most
MARKER = ".icampus-fetcher"  # in a work directory: this directory is the fetcher's own
LOCK_BEAT_S, LOCK_STALE_S = 10.0, 30.0
NOTE_EVERY_S = 60.0  # without a terminal: how often a running job's progress is written


def version() -> str:
    try:
        from importlib.metadata import version as installed
        return installed("skku-icampus")
    except Exception:  # not installed as a package (run from a checkout)
        return "0"


class FetcherSettings(BaseSettings):
    """ICAMPUS_FETCHER_*: the environment, a `.env` beside where the fetcher runs, or flags."""

    # A variable that is there but empty counts as not set (`ICAMPUS_FETCHER_DIR=` is not "the current directory").
    model_config = SettingsConfigDict(env_prefix="ICAMPUS_FETCHER_", env_file=".env", extra="ignore",
                                      env_ignore_empty=True)

    server: str = ""  # the icampus API, e.g. http://<tailnet address>:9013
    token: SecretStr = SecretStr("")  # this fetcher's token (the server's ICAMPUS_FETCHER_TOKENS)
    name: str = ""  # what the server calls this fetcher; empty: this machine's name
    slots: int = Field(1, ge=1, le=4)  # videos downloaded at a time
    dir: Path | None = None  # work directory; empty: the system's per-user cache directory
    max_disk_gb: float = Field(0, ge=0)  # the most the work directory may hold; 0: whatever the disk has free
    keep_awake: bool = True  # no idle sleep while a job is active (never the display)

    @field_validator("*", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: Any, info: ValidationInfo) -> Any:
        """The same for an empty value given any other way."""
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].default
        return value

    @property
    def work_dir(self) -> Path:
        return self.dir.expanduser().absolute() if self.dir else machine.cache_dir()

    @property
    def fetcher_name(self) -> str:
        return self.name.strip() or machine.hostname()

    @property
    def api_url(self) -> str:
        return self.server.strip().rstrip("/")


class Gone(Exception):
    """The server says the job is no longer this fetcher's (taken out of the queue, or the lease ran out): drop it."""


class Refused(Gone):
    """The server took the upload and found it wanting. That try has failed there already; here the job is
    dropped, and the next look for work waits as after any failure."""


class Fatal(Exception):
    """Something no retry fixes, such as a refused token. The fetcher stops and says why."""


def problems(settings: FetcherSettings) -> list[str]:
    """What is missing or wrong in the settings, in words that say how to fix it."""
    found = []
    url = urlsplit(settings.api_url)
    if not settings.api_url:
        found.append("no server: set ICAMPUS_FETCHER_SERVER (or --server) to the icampus API, "
                     "e.g. http://<tailnet address>:9013")
    elif url.scheme not in ("http", "https") or not url.hostname:
        found.append("ICAMPUS_FETCHER_SERVER must be an http(s) address, e.g. http://<tailnet address>:9013")
    if not settings.token.get_secret_value().strip():
        found.append("no token: set ICAMPUS_FETCHER_TOKEN to this fetcher's token from the server's "
                     "ICAMPUS_FETCHER_TOKENS")
    return found


def public_plain_http(server: str) -> bool:
    """Whether the address is plain http to a public IP address, where the token would cross the internet
    unencrypted. A tailnet (100.64.0.0/10), a private network and this machine are fine."""
    url = urlsplit(server)
    try:
        address = ipaddress.ip_address(url.hostname or "")
    except ValueError:
        return False  # a name: it can't be told from here where it leads
    return url.scheme == "http" and address.is_global


# --- what the fetcher shows ------------------------------------------------------------------------------------

def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def fit(text: str, columns: int) -> str:
    """The text cut to `columns` columns of a terminal (Korean characters take two), with … where it was cut."""
    if _width(text) <= columns:
        return text
    out, used = "", 1
    for c in text:
        used += 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if used > columns:
            break
        out += c
    return out + "…"


class Console:
    """Lines for the person running the fetcher. On a terminal the last line is a status that is redrawn in place;
    into a file (a LaunchAgent, Task Scheduler) only events are written, with the time."""

    def __init__(self, out: Any = None):
        self.out = out or sys.stdout
        self.tty = bool(getattr(self.out, "isatty", lambda: False)())
        self._drawn = 0
        with suppress(Exception):  # a title may hold characters the console's code page lacks; a log file is UTF-8
            self.out.reconfigure(errors="replace", **({} if self.tty else {"encoding": "utf-8"}))

    def _write(self, text: str) -> None:
        with suppress(OSError, ValueError):
            self.out.write(text)
            self.out.flush()

    def event(self, text: str) -> None:
        clear = "\r" + " " * self._drawn + "\r" if self._drawn else ""
        self._drawn = 0
        self._write(f"{clear}{datetime.now():%H:%M:%S} {text}\n")

    def status(self, text: str) -> None:
        if not self.tty:
            return
        columns = shutil.get_terminal_size((80, 20)).columns - 1
        line = fit(text, columns)
        pad = max(0, self._drawn - _width(line))
        self._write("\r" + line + " " * pad + "\r" + line)
        self._drawn = _width(line)

    def done(self) -> None:
        if self._drawn:
            self._write("\n")
            self._drawn = 0


# --- one directory, one fetcher --------------------------------------------------------------------------------

class DirLock:
    """`fetcher.lock` in the work directory: a file with this run's id, touched every few seconds while the
    fetcher runs and removed when it ends. A second fetcher started on the same directory would delete the first
    one's downloads at its start, so it refuses while the file is fresh; after a crash it waits LOCK_STALE_S
    seconds at most. No system call that only one operating system has."""

    def __init__(self, directory: Path):
        self.path = directory / "fetcher.lock"
        self.id = secrets.token_hex(8)

    def take(self) -> bool:
        try:
            age = abs(time.time() - self.path.stat().st_mtime)
        except OSError:
            age = None
        if age is not None and age < LOCK_STALE_S:
            return False
        self.path.write_text(self.id, encoding="ascii")
        return True

    def beat(self) -> bool:
        """Marks the lock as alive. False when it is no longer this run's: another fetcher took the directory
        while this one was silent for too long (a sleep), and this one must stop."""
        try:
            if self.path.read_text(encoding="ascii") != self.id:
                return False
            os.utime(self.path)
        except FileNotFoundError:
            self.path.write_text(self.id, encoding="ascii")
        except OSError:
            pass
        return True

    def release(self) -> None:
        with suppress(OSError):
            if self.path.read_text(encoding="ascii") == self.id:
                self.path.unlink()


# --- the fetcher -----------------------------------------------------------------------------------------------

@dataclass
class Job:
    job_id: int
    lease_id: str
    asset_id: str
    host: str
    title: str
    expected: int | None  # the size Commons gave the server for the file
    max_bytes: int
    phase: str = "downloading"
    down: commons.Meter = field(default_factory=commons.Meter)
    up: commons.Meter = field(default_factory=commons.Meter)
    gone: bool = False  # the server said so: stop, delete the file, report nothing
    finished: bool = False  # the server has the whole file (learned from another answer than the last piece's)
    work: asyncio.Task | None = None
    heard: float = field(default_factory=time.monotonic)  # when the server last answered a heartbeat
    noted: float = field(default_factory=time.monotonic)

    @property
    def meter(self) -> commons.Meter:
        return self.up if self.phase == "uploading" else self.down

    def line(self) -> str:
        meter = self.meter
        total = meter.total or self.expected
        speed = meter.speed()
        size = f"{meter.done / MB:.0f}/{total / MB:.0f} MB" if total else f"{meter.done / MB:.0f} MB"
        rate = f"{speed / MB:.2f} MB/s" if speed is not None else "– MB/s"
        return f"{fit(self.title or self.asset_id, 36)}  {size}  {rate}  {self.phase}"


class Fetcher:
    def __init__(self, settings: FetcherSettings, *, api_transport: httpx.AsyncBaseTransport | None = None,
                 commons_transport: httpx.AsyncBaseTransport | None = None, awake: machine.KeepAwake | None = None,
                 console: Console | None = None):
        self.settings = settings
        self.name = settings.fetcher_name
        self.dir = settings.work_dir
        self.console = console or Console()
        self.awake = awake or machine.KeepAwake(settings.keep_awake)
        self.jobs: dict[int, Job] = {}
        self.idle = "starting"
        self._api_transport, self._commons_transport = api_transport, commons_transport
        self._lock: DirLock | None = None
        self.api: httpx.AsyncClient = None  # type: ignore[assignment]  # made by run()

    def client(self) -> httpx.AsyncClient:
        """The client for the icampus server. Redirects are not followed: the token goes to the address that was
        configured and nowhere else."""
        return httpx.AsyncClient(base_url=self.settings.api_url, transport=self._api_transport,
                                 timeout=httpx.Timeout(30, connect=10), follow_redirects=False,
                                 headers={"Authorization": f"Bearer {self.settings.token.get_secret_value().strip()}",
                                          "User-Agent": f"icampus-fetcher/{version()}"})

    # --- the work directory ------------------------------------------------------------------------------------

    def prepare(self) -> None:
        """The work directory: made for this user only, refused inside a folder a cloud service copies, the
        fetcher's own (it deletes leftover downloads there, so it won't take a directory that holds other
        people's files), held by one fetcher at a time, and emptied of what an earlier run left (there is no
        continuing a download: the content server starts every file from its beginning)."""
        if service := machine.synced_by(self.dir):
            raise Fatal(f"the work directory {self.dir} is inside a folder that {service} copies to the cloud. "
                        f"Lecture videos must not go there: choose another with ICAMPUS_FETCHER_DIR, or leave it "
                        f"unset for {machine.cache_dir()}")
        lock = DirLock(self.dir)
        try:
            self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            marker = self.dir / MARKER
            others = [p.name for p in self.dir.iterdir() if p.name not in (MARKER, lock.path.name)]
            if not marker.exists() and others:
                raise Fatal(f"the work directory {self.dir} holds other files. Give the fetcher a directory of its "
                            f"own (a new or empty one): it deletes leftover downloads in it")
            taken = lock.take()
            marker.write_text("The work directory of icampus-fetcher. Files here are deleted by it.\n",
                              encoding="ascii")
        except OSError as exc:
            raise Fatal(f"the work directory {self.dir} can't be used ({type(exc).__name__})") from None
        if not taken:
            raise Fatal(f"another icampus-fetcher is using {self.dir} (or one stopped without cleaning up less "
                        f"than {LOCK_STALE_S:g} seconds ago). Run one per directory; --slots downloads several "
                        f"videos at once")
        self._lock = lock
        self._sweep()

    def room(self) -> int:
        """The largest video there is room for now: the free disk space less a margin and less what the running
        downloads will still write, and under ICAMPUS_FETCHER_MAX_DISK_GB when that is set."""
        coming = sum(max(0, (job.expected or job.down.total or 0) - job.down.done) for job in self.jobs.values())
        room = shutil.disk_usage(self.dir).free - DISK_MARGIN - coming
        if self.settings.max_disk_gb:
            held = sum(max(job.expected or job.down.total or 0, job.down.done) for job in self.jobs.values())
            room = min(room, int(self.settings.max_disk_gb * 2**30) - held)
        return max(0, room)

    # --- running -----------------------------------------------------------------------------------------------

    async def run(self) -> None:
        """Takes jobs until cancelled (Ctrl+C, a shutdown). On the way out every job in hand is reported to the
        server as failed for now, so it goes back into the queue at once instead of when its lease runs out."""
        self.prepare()
        self.console.event(f"{self.name}: fetching for {self.settings.api_url}, {self.settings.slots} at a time, "
                           f"work files in {self.dir}")
        if public_plain_http(self.settings.api_url):
            self.console.event("WARNING: plain http to a public address: the token crosses the internet "
                               "unencrypted. Use the server's tailnet address")
        async with self.client() as self.api:
            slots = [asyncio.create_task(self._slot()) for _ in range(self.settings.slots)]
            extras = [asyncio.create_task(self._show()), asyncio.create_task(self._hold_dir(slots))]
            try:
                await asyncio.gather(*slots)
            finally:
                for task in (*slots, *extras):
                    if not task.cancelling():  # one that is already on its way out is left to hand its job back
                        task.cancel()
                await asyncio.gather(*slots, *extras, return_exceptions=True)
                self.console.done()
                if self._lock:
                    self._lock.release()

    async def _hold_dir(self, slots: list[asyncio.Task]) -> None:
        while True:
            await asyncio.sleep(LOCK_BEAT_S)
            if self._lock and not self._lock.beat():
                self._lock = None
                self.console.event("another icampus-fetcher has taken this work directory: stopping")
                for task in slots:
                    task.cancel()
                return

    async def _show(self) -> None:
        while True:
            await asyncio.sleep(1)
            jobs = list(self.jobs.values())
            self.console.status(" | ".join(job.line() for job in jobs) if jobs else self.idle)
            if not self.console.tty:
                for job in jobs:
                    if time.monotonic() - job.noted >= NOTE_EVERY_S:
                        job.noted = time.monotonic()
                        self.console.event(job.line())

    def _sweep(self) -> None:
        """Deletes work files that belong to no job in hand. A file is deleted when its job ends; this is for
        the one the system refused to let go of at that moment (Windows does while a scanner has it open)."""
        active = {f"{job.asset_id}.part" for job in self.jobs.values()}
        for leftover in self.dir.glob("*.part"):  # only what the fetcher itself names that way: <asset id>.part
            if leftover.name not in active and commons.SAFE_ID.fullmatch(leftover.stem):
                with suppress(OSError):
                    leftover.unlink()

    async def _slot(self) -> None:
        wait, after_failure = IDLE_MIN_S, AFTER_FAILURE_S[0]
        while True:
            self._sweep()
            try:
                job = await self._lease()
            except httpx.HTTPError as exc:
                job, self.idle = None, f"the server can't be reached ({type(exc).__name__}); trying again"
            if job is None:
                await asyncio.sleep(wait * random.uniform(0.8, 1.2))
                wait = min(IDLE_MAX_S, wait * 2)
                continue
            wait = IDLE_MIN_S
            if await self._do(job):
                after_failure = AFTER_FAILURE_S[0]
            else:
                # Whatever broke the job may break the next one too (this machine can't reach the content server,
                # say), and every try it takes and fails is one the job has fewer: not at once, and less often.
                self.idle = f"the last job failed; looking for work again in {after_failure:.0f} s"
                await asyncio.sleep(after_failure)
                after_failure = min(AFTER_FAILURE_S[1], after_failure * 3)

    async def _lease(self) -> Job | None:
        """Asks for the next job. None: nothing is due (or the server isn't ready). Fatal: the server refuses
        this fetcher."""
        r = await self.api.post(f"{API}/lease", timeout=httpx.Timeout(LEASE_ANSWER_S, connect=10), json={
            "name": self.name, "version": version(), "platform": machine.describe(),
            "slots_free": self.settings.slots - len(self.jobs), "free_bytes": self.room()})
        if r.status_code == 204:
            self.idle = "waiting for work"
            return None
        if r.status_code in (401, 403):
            raise Fatal(f"the server refused this fetcher's token (HTTP {r.status_code}): it must be one of the "
                        f"server's ICAMPUS_FETCHER_TOKENS")
        if r.status_code == 422:
            raise Fatal("the server refused this fetcher's name: use letters, digits, spaces, dots and hyphens, "
                        "40 characters at most (ICAMPUS_FETCHER_NAME)")
        if r.status_code != 200:
            self.idle = f"the server answered HTTP {r.status_code}; trying again"
            return None
        try:
            data = r.json()
            job = Job(job_id=int(data["job_id"]), lease_id=str(data["lease_id"]), asset_id=str(data["asset_id"]),
                      host=str(data["host"]), title=str(data.get("title") or ""),
                      expected=int(data["expected_bytes"]) if data.get("expected_bytes") else None,
                      max_bytes=int(data["max_bytes"]))
        except (ValueError, KeyError, TypeError):
            self.idle = "the server's answer wasn't understood (is it a newer version?); trying again"
            return None
        if not commons.SAFE_ID.fullmatch(job.asset_id) or not commons.host_ok(f"https://{job.host}/"):
            # never a file name or a host of the server's choosing that this module didn't check
            await self._fail(job, "the fetcher can't take this job (its asset id or host is not one it accepts)", True)
            return None
        return job

    async def _do(self, job: Job) -> bool:
        """One job from lease to the end, whatever the end is. The file is deleted on every way out. False when
        the job failed here (not when it was uploaded, or when the server took it back)."""
        failed = False
        part = self.dir / f"{job.asset_id}.part"
        self.jobs[job.job_id] = job
        self.awake.hold()
        self.console.event(f"downloading {fit(job.title or job.asset_id, 50)}"
                           + (f" ({job.expected / MB:.0f} MB)" if job.expected else ""))
        job.work = asyncio.create_task(self._transfer(job, part))
        beat = asyncio.create_task(self._heartbeat(job))
        try:
            await asyncio.wait({job.work})
            if job.finished or (not job.work.cancelled() and job.work.exception() is None):
                self.console.event(f"uploaded {fit(job.title or job.asset_id, 50)} ({(job.up.total or 0) / MB:.0f} MB)")
            elif job.work.cancelled() or job.gone:
                self.console.event(f"dropped {fit(job.title or job.asset_id, 50)}: the server took the job back")
            elif isinstance(exc := job.work.exception(), Gone):
                self.console.event(f"dropped {fit(job.title or job.asset_id, 50)}: {exc}")
                failed = isinstance(exc, Refused)
            else:
                if not isinstance(exc, (commons.VideoError, httpx.HTTPError, OSError)):
                    log.warning("job %s: %s", job.job_id, type(exc).__name__)
                message, transient = commons.failure(exc)
                self.console.event(f"failed {fit(job.title or job.asset_id, 50)}: {message}")
                await self._fail(job, message, transient)
                failed = True
            return not failed
        except asyncio.CancelledError:  # the fetcher is stopping: the job goes back into the queue now
            job.work.cancel()
            await asyncio.gather(job.work, return_exceptions=True)
            if not (job.gone or job.finished):
                await self._fail(job, "fetcher stopped", True)
            raise
        finally:
            beat.cancel()
            with suppress(OSError):
                part.unlink(missing_ok=True)
            self.jobs.pop(job.job_id, None)
            self.awake.release()

    async def _transfer(self, job: Job, part: Path) -> None:
        """Download with commons.py (the code the server downloads with), then upload."""
        digest = hashlib.sha256()
        async with asyncio.timeout(commons.MAX_DOWNLOAD_S):
            async with commons.attempt(self._commons_transport) as content:
                await content.describe(job.host, job.asset_id, max_bytes=job.max_bytes)
                got = await content.fetch(part, meter=job.down, expected=job.expected or 0, digest=digest)
        if not got.size:
            raise commons.VideoError("the content server sent an empty file", transient=True)
        job.up.restart(0, got.size)
        job.phase = "uploading"
        await self._upload(job, part, got.size, digest.hexdigest())

    async def _heartbeat(self, job: Job) -> None:
        """Reports the job's progress every few seconds, which keeps its lease. When the server says the job is
        gone, or hasn't answered for LOST_AFTER_S seconds, the work is stopped."""
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            meter = job.meter
            try:
                r = await self.api.post(f"{API}/jobs/{job.job_id}/progress", timeout=10, json={
                    "lease_id": job.lease_id, "phase": job.phase, "done_bytes": meter.done,
                    "total_bytes": meter.total, "speed_bps": meter.speed()})
                answered = r.status_code == 200 or 400 <= r.status_code < 500
                told = r.json() if r.status_code == 200 else {}
                gone = answered and not (r.status_code == 200 and told.get("continue", True))
                job.finished = job.finished or bool(told.get("complete"))
            except (httpx.HTTPError, ValueError, AttributeError):
                answered = gone = False
            if answered and not gone:
                job.heard = time.monotonic()
            elif gone or time.monotonic() - job.heard > LOST_AFTER_S:
                job.gone = True
                if job.work:
                    job.work.cancel()
                return

    async def _upload(self, job: Job, part: Path, size: int, sha256: str) -> None:
        """Sends the file in pieces. After anything but a plain yes the server is asked how much it has, and the
        upload goes on from there, for as long as the lease holds (the heartbeat decides when it no longer does)
        and something still moves: an upload that gets nowhere for UPLOAD_STUCK_S seconds is given up."""
        url = f"{API}/jobs/{job.job_id}/file"
        lease = {"X-Lease-Id": job.lease_id}
        offset, wait, moved = 0, UPLOAD_RETRY_S[0], time.monotonic()

        async def ask() -> int | None:
            """How much the server has, None when it didn't say. Returns `size` once it has finished the video."""
            with suppress(httpx.HTTPError, ValueError, KeyError, TypeError):
                r = await self.api.get(url, headers=lease, timeout=10)
                if r.status_code in (404, 409):
                    raise Gone("the server took the job back")
                if r.status_code == 200:
                    told = r.json()
                    job.finished = job.finished or bool(told.get("complete"))
                    return int(told["received_bytes"])
            return None

        with open(part, "rb") as f:
            while not job.finished:
                if time.monotonic() - moved > UPLOAD_STUCK_S:
                    raise commons.VideoError(f"the upload got nowhere for {UPLOAD_STUCK_S / 60:.0f} minutes",
                                             transient=True)
                if offset >= size:  # all of it is there: the server is still checking it. Ask until it has
                    await asyncio.sleep(min(wait, 3 * UPLOAD_RETRY_S[0]))
                    wait = min(UPLOAD_RETRY_S[1], wait * 2)
                    had = await ask()
                    offset = had if had is not None and had < size else offset
                    continue
                f.seek(offset)
                data = f.read(PIECE)
                last = offset + len(data) >= size
                headers = {**lease, "Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{size}",
                           "Content-Length": str(len(data)), "Content-Type": "application/octet-stream"}
                if last:
                    headers["X-File-SHA256"] = sha256
                try:  # the server checks the whole file before it answers the last piece: that one may take long
                    r = await self.api.put(url, content=self._pieces(job, data), headers=headers,
                                           timeout=httpx.Timeout(300 if last else 60, connect=10))
                    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    status = r.status_code
                except (httpx.HTTPError, ValueError):
                    status, body = 0, {}
                before = offset
                if status == 200 and body.get("complete"):
                    break
                if status in (200, 416) and isinstance(body.get("received_bytes"), int):
                    offset, wait = body["received_bytes"], UPLOAD_RETRY_S[0]
                elif status in (404, 409):
                    raise Gone("the server took the job back")
                elif status == 422 and body.get("refused"):  # it failed the server's checks: that try is over there
                    raise Refused(f"the server refused the file ({str(body.get('error') or 'no reason given')[:200]})")
                elif status in (401, 403):
                    raise Gone("the server refused this fetcher's token")
                elif status in (400, 413, 422):  # the request itself was wrong: hand the job back
                    raise commons.VideoError(f"the server did not take the upload (HTTP {status})", transient=True)
                else:  # the network, or the server in trouble: wait, then ask where to go on
                    await asyncio.sleep(wait)
                    wait = min(UPLOAD_RETRY_S[1], wait * 2)
                    had = await ask()
                    offset = had if had is not None else offset
                if not 0 <= offset <= size:
                    raise commons.VideoError("the upload got out of step with the server", transient=True)
                if offset > before:
                    moved = time.monotonic()
                if job.up.done != offset:
                    job.up.restart(offset, size)
        job.up.done = size

    @staticmethod
    async def _pieces(job: Job, data: bytes):
        """The piece in small blocks, counted as they are handed to the connection, for the speed that is shown."""
        for at in range(0, len(data), 64 * 1024):
            block = data[at:at + 64 * 1024]
            yield block
            job.up.arrived(len(block))

    async def _fail(self, job: Job, message: str, transient: bool) -> None:
        """Hands the job back with what went wrong. Best effort: if the server can't be told, the lease runs out."""
        with suppress(httpx.HTTPError, TimeoutError):
            async with asyncio.timeout(8):
                await self.api.post(f"{API}/jobs/{job.job_id}/fail", json={
                    "lease_id": job.lease_id, "message": message[:300], "transient": transient})


# --- check ------------------------------------------------------------------------------------------------------

async def check(settings: FetcherSettings, *, out: Callable[[str], None] = print,
                api_transport: httpx.AsyncBaseTransport | None = None,
                commons_transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Looks at everything `run` needs and says what it found, one line each. Returns a process exit code."""
    fetcher = Fetcher(settings, api_transport=api_transport, commons_transport=commons_transport,
                      console=Console(sys.stdout))
    bad = 0

    def say(what: str, state: str, detail: str) -> None:
        nonlocal bad
        bad += state == "FAILED"
        out(f"{what:<12} {state:<8} {detail}")

    for problem in problems(settings):
        say("settings", "FAILED", problem)
    if bad:
        return 1
    if public_plain_http(settings.api_url):
        say("server", "WARNING", "plain http to a public address: the token would cross the internet unencrypted. "
                                 "Use the server's tailnet address")
    target = None
    async with fetcher.client() as api:
        try:
            health = await api.get("/health")
            running = ""
            with suppress(ValueError, AttributeError):
                running = f", version {health.json().get('version')}"
            say("server", "ok" if health.status_code == 200 else "FAILED",
                f"{settings.api_url} answered HTTP {health.status_code}{running}")
        except httpx.HTTPError as exc:
            say("server", "FAILED", f"{settings.api_url} can't be reached ({type(exc).__name__}). Is Tailscale "
                                    f"connected on this machine?")
            return 1
        try:  # a lease that offers no free slot: the server checks the token and hands out nothing
            probe = await api.post(f"{API}/lease", json={"name": fetcher.name, "version": version(),
                                                         "platform": machine.describe(), "slots_free": 0})
            if probe.status_code == 204:
                say("token", "ok", f"accepted as a fetcher token; this fetcher is called {fetcher.name!r}")
            elif probe.status_code == 403:
                say("token", "FAILED", "this is an API token, not a fetcher token: use one from the server's "
                                       "ICAMPUS_FETCHER_TOKENS")
            elif probe.status_code == 503:
                say("token", "FAILED", "the server has no fetcher tokens: set ICAMPUS_FETCHER_TOKENS there")
            elif probe.status_code == 422:
                say("token", "FAILED", f"the server refused the name {fetcher.name!r}: use letters, digits, "
                                       f"spaces, dots and hyphens (ICAMPUS_FETCHER_NAME)")
            else:
                say("token", "FAILED", f"refused (HTTP {probe.status_code})")
            targets = await api.get(bench.TARGETS, params={"count": 1})
            target = (targets.json().get("items") or [None])[0] if targets.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as exc:
            say("token", "FAILED", f"the server stopped answering ({type(exc).__name__})")
    if not (isinstance(target, dict) and commons.SAFE_ID.fullmatch(str(target.get("asset_id") or ""))
            and commons.host_ok(f"https://{target.get('host')}/")):
        say("commons", "skipped", "the server named no lecture video to try (its token was refused, or nothing "
                                  "is synced yet)")
    else:
        try:
            async with commons.attempt(commons_transport, stall_s=20) as content:
                info = await content.describe(target["host"], target["asset_id"], max_bytes=2**62)
            size = commons.original_size(info)
            say("commons", "ok", f"{target['host']} described a video" + (f" of {size / MB:.0f} MB" if size else ""))
        except Exception as exc:
            say("commons", "FAILED", f"{target['host']}: {commons.failure(exc)[0]}")
    work = settings.work_dir
    try:
        work.mkdir(mode=0o700, parents=True, exist_ok=True)
        free = shutil.disk_usage(work).free
        cap = f", of which this fetcher uses at most {settings.max_disk_gb:g} GB" if settings.max_disk_gb else ""
        say("disk", "ok" if free > 2 * DISK_MARGIN else "WARNING", f"{free / 2**30:.1f} GB free{cap}")
    except OSError as exc:
        say("disk", "FAILED", f"{work} can't be used ({type(exc).__name__})")
    service = machine.synced_by(work)
    say("work dir", "WARNING" if service else "ok",
        f"{work} is inside a folder that {service} copies to the cloud: `run` refuses to start there. Leave "
        f"ICAMPUS_FETCHER_DIR unset to use {machine.cache_dir()}" if service
        else f"{work} (private to this user, not in a synced folder)")
    return 1 if bad else 0


# --- the command line -------------------------------------------------------------------------------------------

def _settings(args: argparse.Namespace) -> FetcherSettings:
    given = {"server": args.server, "token": args.token, "name": args.name, "slots": args.slots, "dir": args.dir,
             "max_disk_gb": args.max_disk_gb, "keep_awake": args.keep_awake}
    return FetcherSettings(**{k: v for k, v in given.items() if v is not None})


async def _until_stopped(work: Any) -> None:
    """Runs `work`, and lets a shutdown (SIGTERM, a closed terminal, Ctrl+Break) end it the way Ctrl+C does: by
    cancelling it, so it can hand its jobs back first."""
    task, loop = asyncio.current_task(), asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            loop.add_signal_handler(number, task.cancel)
        except (NotImplementedError, RuntimeError, ValueError):  # Windows' event loop has no signal handlers
            with suppress(ValueError, OSError):
                signal.signal(number, lambda *_: loop.call_soon_threadsafe(task.cancel))
    await work


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="icampus-fetcher",
        description="Download lecture videos for an icampus server on this machine and upload them to it. "
                    "Settings come from ICAMPUS_FETCHER_* in the environment, a .env in the current directory, "
                    "or these flags.")
    def settings_flags(to: argparse.ArgumentParser, default: Any) -> None:
        """The flags that stand for settings. They are accepted before and after the command; after it, one that
        isn't given must leave what was given before it alone, hence the default."""
        to.add_argument("--server", default=default,
                        help="the icampus API, e.g. http://<tailnet address>:9013 (ICAMPUS_FETCHER_SERVER)")
        to.add_argument("--token", default=default, help="this fetcher's token; better kept in "
                                                         "ICAMPUS_FETCHER_TOKEN or .env than on a command line")
        to.add_argument("--name", default=default,
                        help="what the server calls this fetcher (default: this machine's name)")
        to.add_argument("--slots", type=int, default=default, help="videos downloaded at a time, 1 to 4 (default 1)")
        to.add_argument("--dir", type=Path, default=default,
                        help="work directory (default: the per-user cache directory)")
        to.add_argument("--max-disk-gb", type=float, default=default,
                        help="the most the work directory may hold (default: no limit)")
        to.add_argument("--keep-awake", action=argparse.BooleanOptionalAction, default=default,
                        help="no idle sleep while a job is active (default on; the display may always sleep)")

    settings_flags(parser, None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    commands = [sub.add_parser("run", help="take download jobs from the server until stopped"),
                sub.add_parser("check", help="check the server, the token, the content server, disk space and "
                                             "the work directory"),
                sub.add_parser("bench", help="measure download speed from this machine (nothing is kept)")]
    commands[2].add_argument("--seconds", type=float, default=45, help="how long each step downloads (default 45)")
    commands[2].add_argument("--streams", default="1,2,3", help="connections per step (default 1,2,3)")
    for command in commands:
        settings_flags(command, argparse.SUPPRESS)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):  # their INFO and DEBUG lines print addresses with queries, and headers
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        settings = _settings(args)
    except ValueError as exc:  # say which setting and what is wrong with it, never the value that was given
        wrong = "; ".join(f"{e['loc'][0] if e['loc'] else 'settings'}: {e['msg']}"
                          for e in getattr(exc, "errors", lambda: [])()) or "a setting has a value it can't take"
        raise SystemExit(f"icampus-fetcher: {wrong}") from None
    try:
        if args.cmd == "check":
            raise SystemExit(asyncio.run(check(settings)))
        if found := problems(settings):
            raise SystemExit("icampus-fetcher: " + "; ".join(found))
        if args.cmd == "bench":
            try:
                streams = bench.parse_streams(args.streams)
            except ValueError as exc:
                raise SystemExit(f"icampus-fetcher: {exc}") from None
            raise SystemExit(asyncio.run(bench.main(settings.api_url, settings.token.get_secret_value().strip(),
                                                    seconds=max(1.0, args.seconds), streams=streams)))
        asyncio.run(_until_stopped(Fetcher(settings).run()))
    except Fatal as exc:
        raise SystemExit(f"icampus-fetcher: {exc}") from None
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
