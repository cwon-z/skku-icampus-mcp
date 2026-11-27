"""SKKU's content server (Commons, lcms.skku.edu): the one client for it.

The server's download worker (videos.py), a fetcher on another machine (fetcher.py) and `bench` all go through
this module, so there is one place that decides what is sent to the content server and what is believed of its
answers. It needs the standard library and httpx only: no database, no settings, no browser.

- No cookies and no login, ever: these requests must not carry the iCampus session. Commons describes a video to
  anyone (`/CommonsCore2/v2/contents/<asset id>`) and serves the original file from `services/download/<asset id>`
  to a request with an iCampus Referer (see docs/icampus-notes.md).
- The download redirects once, to an address with a query string. That address is never stored, logged, put in an
  error message or handed to a caller: errors carry the status or the exception's type, nothing else, and the
  HTTP library's own request log is kept quiet (`_hush`).
- Every request, and every redirect it follows, stays on `*.skku.edu` over https.
- A try ends when the content server sends nothing for `STALL_S` seconds.
- An interrupted download is continued with `Range` where the content server honours one (lcms.skku.edu was seen
  answering 200 with the whole file instead, which is then taken from its start).
"""

import asyncio
import errno
import logging
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlsplit

import httpx

REFERER = "https://canvas.skku.edu/"
HOST = "lcms.skku.edu"
MB = 2**20
MAX_DOWNLOAD_S = 6 * 3600  # one try (4 GB at 0.2 MB per second); a dead connection ends far sooner, see STALL_S
STALL_S = 60.0  # a try ends when the content server sends nothing for this long
SPEED_WINDOW_S = 20.0  # a speed is the average over this long
# An asset id that is safe as a file name on every system (Windows included) and as one URL path segment. The ids
# Commons gives are hex strings; a job with anything else stays with the server, which made its own rules for them.
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_EXTENSION = re.compile(r"[a-z0-9]{1,8}")


class VideoError(Exception):
    """A message safe to show: no URLs with queries, no tokens. `transient` marks a failure that may pass by
    itself (the network, a full cache), so the job tries again later."""

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def failure(exc: BaseException) -> tuple[str, bool]:
    """How a try that ended with `exc` is reported, wherever it ran: a message that is safe to store and show, and
    whether the failure may pass by itself. An httpx error's own message can hold the redirect address, so only
    its type is kept."""
    if isinstance(exc, VideoError):
        return str(exc), exc.transient
    if isinstance(exc, TimeoutError):
        return f"the download took longer than {MAX_DOWNLOAD_S // 3600} hours", True
    if isinstance(exc, httpx.HTTPError):
        return f"lcms.skku.edu: {type(exc).__name__}", True
    if isinstance(exc, OSError):
        return f"could not write the video to disk ({errno.errorcode.get(exc.errno, 'OSError')})", True
    return type(exc).__name__, False


def host_ok(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").endswith(".skku.edu")


def original_size(info: dict) -> int:
    """The size of the original file as Commons describes it, or 0 when it doesn't say."""
    try:
        return max(0, int(info.get("original_file_length") or 0))
    except (TypeError, ValueError):
        return 0


def extension(info: dict, mp4: bool) -> str:
    """The suffix for a downloaded file: .mp4 only when its first bytes say so, else what Commons calls it."""
    described = str(info.get("extension") or "").lower()
    if mp4:
        return ".mp4"
    return f".{described}" if _EXTENSION.fullmatch(described) and described != "mp4" else ".bin"  # no fake .mp4


def is_mp4(path: Path) -> bool:
    """Whether the file starts like an MP4 (`ftyp` at offset 4)."""
    with open(path, "rb") as f:
        return f.read(16)[4:8] == b"ftyp"


def _validator(headers: httpx.Headers) -> str | None:
    """What a later Range request can name to be sure the file is still the same: a strong ETag, else the date."""
    etag = headers.get("etag") or ""
    return etag if etag and not etag.startswith("W/") else headers.get("last-modified") or None


def _hush() -> None:
    """httpx logs every request's address at INFO, the download redirect's query included, and httpcore logs
    headers at DEBUG. Neither is ever written by a process that talks to the content server, whatever its own log
    level and however it was started."""
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if logger.getEffectiveLevel() < logging.WARNING:
            logger.setLevel(logging.WARNING)


def client(transport: httpx.AsyncBaseTransport | None = None, stall_s: float = STALL_S) -> httpx.AsyncClient:
    """The client every request to the content server is made with. No cookies, ever: these requests must not
    carry the iCampus session. The read timeout sits just behind the stall watchdog in `attempt`, which notices
    first and says what happened."""
    _hush()
    return httpx.AsyncClient(transport=transport, follow_redirects=True, max_redirects=3,
                             timeout=httpx.Timeout(stall_s + 15, connect=20),
                             headers={"User-Agent": "Mozilla/5.0", "Referer": REFERER})


async def describe(http: httpx.AsyncClient, host: str, asset_id: str) -> dict:
    """What Commons says about an asset: sizes, extension, flags. It also names the uploader: callers keep only the
    fields they need, never the whole answer."""
    url = f"https://{host}/CommonsCore2/v2/contents/{asset_id}"
    if not host_ok(url):
        raise VideoError("the video is not on an SKKU content server")
    r = await http.get(url)
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


@dataclass
class Meter:
    """The live numbers of one transfer: bytes so far, the total when it is known, and the speed."""
    done: int = 0
    total: int | None = None
    seen: float = field(default_factory=time.monotonic)  # when the other side last answered or sent bytes
    samples: deque = field(default_factory=deque, repr=False)  # (monotonic time, bytes) for the speed

    def restart(self, done: int, total: int | None) -> None:
        """The transfer (re)starts counting at `done` bytes."""
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
        return True

    def speed(self) -> float | None:
        """Bytes per second over the last seconds. It falls towards zero while nothing arrives."""
        if not self.samples:
            return None
        since, had = self.samples[0]
        elapsed = time.monotonic() - since
        return max(0.0, (self.done - had) / elapsed) if elapsed >= 1 else None


@dataclass(frozen=True)
class Resume:
    """What an interrupted try left on disk that the next one may continue."""
    offset: int  # the bytes of the .part
    total: int  # the size the whole file had
    validator: str | None  # what the first answer gave to recognise the file by (sent as If-Range)


@dataclass(frozen=True)
class Opened:
    """What the content server's answer says about the file. Given to the caller once the .part is open and before
    its first byte is written, so that what the caller records never describes other bytes than the file holds."""
    offset: int  # where this answer's bytes start: 0, or the size of the part they continue
    total: int | None  # the size the finished file should have, when anything says
    validator: str | None
    discarded: int  # bytes of an interrupted try that could not be continued: the whole file comes again
    range_ignored: bool  # a Range request was answered with the whole file


@dataclass(frozen=True)
class Fetched:
    size: int
    mp4: bool  # the first bytes say MP4


def _continues(r: httpx.Response, resume: Resume) -> bool:
    """Whether a 206 answer really is the rest of the file the .part began: from our offset, of the same total
    size, unencoded, and with the same validator when both sides have one."""
    m = _CONTENT_RANGE.fullmatch((r.headers.get("content-range") or "").strip())
    if not m or int(m.group(1)) != resume.offset or int(m.group(3)) != resume.total:
        return False
    if (r.headers.get("content-encoding") or "identity").lower() != "identity":
        return False
    validator = _validator(r.headers)
    return not (resume.validator and validator and validator != resume.validator)


class Attempt:
    """One try at one video, under the stall watchdog of `attempt()`: describe it, then fetch it."""

    def __init__(self, http: httpx.AsyncClient, watchdog: asyncio.Timeout, stall_s: float,
                 on_alive: Callable[[], None] | None):
        self.http = http
        self._watchdog, self._stall_s, self._on_alive = watchdog, stall_s, on_alive
        self.host = self.asset_id = ""
        self.info: dict[str, Any] = {}
        self.max_bytes = 0

    def alive(self) -> None:
        """The content server answered or sent bytes: the stall clock starts over."""
        self._watchdog.reschedule(asyncio.get_running_loop().time() + self._stall_s)
        if self._on_alive:
            self._on_alive()

    async def describe(self, host: str, asset_id: str, *, max_bytes: int) -> dict:
        """Asks Commons about the asset and refuses what can't be downloaded: an original that is gone, or one
        bigger than `max_bytes`."""
        info = await describe(self.http, host, asset_id)
        self.alive()
        if info.get("is_original_file_deleted"):
            raise VideoError("Commons no longer has this video's original file")
        original = original_size(info)
        if original > max_bytes:
            raise VideoError(f"the video is {original / 2**30:.1f} GB; only videos up to "
                             f"{max_bytes / 2**30:g} GB are downloaded (ICAMPUS_VIDEO_MAX_GB)")
        self.host, self.asset_id, self.info, self.max_bytes = host, asset_id, info, max_bytes
        return info

    @asynccontextmanager
    async def _answer(self, headers: dict[str, str]) -> AsyncIterator[httpx.Response]:
        """The content server's answer to a request for the file, once it is known to come from SKKU."""
        url = self.info.get("download_url")
        if not (isinstance(url, str) and host_ok(url)):
            url = f"https://{self.host}/CommonsCore2/v2/services/download/{self.asset_id}"
        async with self.http.stream("GET", url, headers=headers) as r:
            self.alive()
            if not host_ok(str(r.url)):
                raise VideoError("the download was redirected away from SKKU; stopped")
            yield r

    @staticmethod
    def _is_the_file(r: httpx.Response, status: int) -> None:
        if r.status_code != status:
            raise VideoError(f"Commons refused the download (HTTP {r.status_code})",
                             transient=r.status_code >= 500 or r.status_code == 429)
        if "text/html" in (r.headers.get("content-type") or ""):
            raise VideoError("Commons sent a web page instead of the video")

    async def fetch(self, part: Path, *, meter: Meter, expected: int = 0, resume: Resume | None = None,
                    opened: Callable[[Opened], None] | None = None,
                    progress: Callable[[], None] | None = None, digest: Any = None) -> Fetched:
        """Downloads the described video into `part` and checks that all of it came.

        `meter` counts the bytes; `progress` is called about once a second while they arrive. `expected` is the
        size to plan with when the answer names none. `resume` describes a .part to continue; `opened` hears what
        the answer says about the file before the first byte is written. `digest` (a hashlib object) is fed every
        byte of a download that starts at zero."""
        cap, original = self.max_bytes, original_size(self.info)
        kept = resume.offset if resume else 0
        exact: int | None = None  # the size the finished file must have, when the content server says
        for offset in ((kept, 0) if kept else (0,)):  # the rest of the file first, when some of it is here
            headers = {}
            if offset:
                headers = {"Range": f"bytes={offset}-", "Accept-Encoding": "identity"}
                if resume.validator:
                    headers["If-Range"] = resume.validator
            async with self._answer(headers) as r:
                resumed = bool(offset) and r.status_code == 206 and _continues(r, resume)
                if offset and not resumed and r.status_code in (206, 416):
                    continue  # not the rest of what is here: ask for the whole file
                self._is_the_file(r, 206 if resumed else 200)
                plain = (r.headers.get("content-encoding") or "identity").lower() == "identity"
                length = r.headers.get("content-length") or ""
                if resumed:
                    exact = resume.total
                elif plain and length.isdigit() and int(length):
                    exact = int(length)
                total = exact or expected or None
                if total and total > cap:
                    raise VideoError(f"the video is {total / 2**30:.1f} GB; only videos up to "
                                     f"{cap / 2**30:g} GB are downloaded")
                meter.restart(offset if resumed else 0, total)
                flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if resumed else os.O_TRUNC) | getattr(os, "O_BINARY", 0)
                with os.fdopen(os.open(part, flags, 0o600), "ab" if resumed else "wb") as f:
                    if opened:
                        opened(Opened(offset=meter.done, total=total, validator=_validator(r.headers) if plain else None,
                                      discarded=0 if resumed else kept, range_ignored=bool(offset) and not resumed))
                    async for chunk in r.aiter_bytes():
                        f.write(chunk)
                        f.flush()  # into the kernel's cache, where it outlives this process
                        self.alive()
                        if digest is not None:
                            digest.update(chunk)
                        if meter.arrived(len(chunk)) and progress:
                            progress()
                        if meter.done > cap:
                            raise VideoError(f"the video is over {cap / 2**30:g} GB; stopped")
            break
        # Complete? By the size the content server gave with the file; without one, at least not less than the
        # size Commons describes (an answer with no length can end early without the connection saying so).
        if meter.done != exact if exact is not None else meter.done < original:
            raise VideoError("the download was cut short; try again", transient=True)
        return Fetched(size=meter.done, mp4=is_mp4(part))

    async def discard(self, count: Callable[[int], None]) -> None:
        """Reads the described video once and throws every byte away (bench): nothing touches the disk."""
        async with self._answer({}) as r:
            self._is_the_file(r, 200)
            async for chunk in r.aiter_bytes():
                self.alive()
                count(len(chunk))


@asynccontextmanager
async def attempt(transport: httpx.AsyncBaseTransport | None = None, *, stall_s: float = STALL_S,
                  on_alive: Callable[[], None] | None = None) -> AsyncIterator[Attempt]:
    """A cookie-less client and a stall watchdog for one try. Whatever is awaited inside ends with a VideoError
    once the content server has sent nothing for `stall_s` seconds."""
    try:
        async with asyncio.timeout(stall_s) as watchdog, client(transport, stall_s) as http:
            yield Attempt(http, watchdog, stall_s, on_alive)
    except TimeoutError:
        raise VideoError(f"the content server sent nothing for {stall_s:g} seconds", transient=True) from None
