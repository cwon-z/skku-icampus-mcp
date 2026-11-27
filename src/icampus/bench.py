"""`bench`: how fast does the content server send lecture videos to this machine, and does that grow with the
number of connections?

It asks the icampus server for a few hosted videos (`GET /api/v1/fetchers/bench-targets`: asset id and host),
then downloads them for a while at each number of streams, each stream on a different video, and prints megabytes
per second for each stream and in total. The downloads go through commons.py like any other (no cookies, the
iCampus Referer, only *.skku.edu), and every byte is thrown away as it arrives: nothing is written to disk.

Run from the server (`icampus bench`) and from a laptop (`icampus-fetcher bench`), the numbers say whether a
fetcher on that laptop would help, and whether more connections from one place do (docs/fetchers.md).
"""

import asyncio
import time
from typing import Callable

import httpx

from . import commons

MB = 2**20
TARGETS = "/api/v1/fetchers/bench-targets"


def parse_streams(text: str) -> list[int]:
    """'1,2,3' -> [1, 2, 3]. ValueError says what is wrong."""
    try:
        counts = [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise ValueError("--streams takes numbers separated by commas, e.g. 1,2,3") from None
    if not counts or not all(1 <= n <= 8 for n in counts):
        raise ValueError("--streams takes numbers from 1 to 8, e.g. 1,2,3")
    return counts


async def _stream(target: dict, seconds: float, transport: httpx.AsyncBaseTransport | None) -> tuple[float | None, str]:
    """One connection for `seconds`: (bytes per second from its first byte on, "") or (None, why not). A video
    that ends before the time is up is asked for again, so the stream stays busy for the whole step."""
    got, first = 0, None

    def count(n: int) -> None:
        nonlocal got, first
        if first is None:
            first = time.monotonic()
        got += n

    try:
        async with asyncio.timeout(seconds):
            while True:
                async with commons.attempt(transport) as content:
                    await content.describe(target["host"], target["asset_id"], max_bytes=2**62)
                    await content.discard(count)
                if not got:
                    return None, "the content server sent an empty file"
    except TimeoutError:
        pass  # the step is over
    except Exception as exc:  # said the way a download's failure is: never an address
        if not got:
            return None, commons.failure(exc)[0]
    elapsed = time.monotonic() - first if first is not None else 0
    return (got / elapsed, "") if elapsed >= 0.2 else (None, "no data arrived")


async def run(api: httpx.AsyncClient, *, seconds: float = 45, streams: list[int] | None = None,
              out: Callable[[str], None] = print, transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Runs the measurement and prints it. Returns a process exit code. `api` is a client for the icampus server,
    with its token; `transport` stands in for the content server in tests."""
    streams = streams or [1, 2, 3]
    try:
        r = await api.get(TARGETS, params={"count": min(8, max(streams))})
    except httpx.HTTPError as exc:
        out(f"The icampus server can't be reached ({type(exc).__name__}). Is this machine on the tailnet?")
        return 1
    if r.status_code != 200:
        out(f"The icampus server refused the request (HTTP {r.status_code}). Check the token.")
        return 1
    body = r.json()
    targets = [t for t in body.get("items", []) if isinstance(t, dict)
               and commons.SAFE_ID.fullmatch(str(t.get("asset_id") or ""))
               and commons.host_ok(f"https://{t.get('host')}/")]
    if not targets:
        out("The server knows no hosted lecture video to measure with. Sync it first.")
        return 1
    hosts = ", ".join(sorted({t["host"] for t in targets}))
    out(f"Download speed from this machine to {hosts}: {seconds:g} s per step, nothing is kept.")
    if body.get("downloading"):
        out(f"Note: the server's queue is downloading {body['downloading']} video(s) right now. Measured from the "
            f"server, or from the same network, these numbers share the line with them.")
    if len(targets) < max(streams):
        out(f"Note: only {len(targets)} video(s) to try, so some streams share one.")
    out(f"{'streams':>7}  {'MB/s per stream':<28}  {'total MB/s':>10}")
    failed = False
    for n in streams:
        results = await asyncio.gather(*(_stream(targets[i % len(targets)], seconds, transport) for i in range(n)))
        speeds = [speed for speed, _ in results if speed is not None]
        shown = "  ".join(f"{speed / MB:.2f}" if speed is not None else "failed" for speed, _ in results)
        out(f"{n:>7}  {shown:<28}  {sum(speeds) / MB:>10.2f}")
        for why in sorted({why for speed, why in results if speed is None}):
            failed = True
            out(f"         a stream failed: {why}")
    out("If the total grows with the streams, the content server limits each connection; if it stays the same, it "
        "limits each address. docs/fetchers.md says what to do with either.")
    return 1 if failed else 0


async def main(server: str, token: str, *, seconds: float = 45, streams: list[int] | None = None,
               out: Callable[[str], None] = print) -> int:
    """`bench` for a command line: a client for `server` with `token`, then run()."""
    async with httpx.AsyncClient(base_url=server.rstrip("/"), timeout=httpx.Timeout(30, connect=10),
                                 headers={"Authorization": f"Bearer {token}"}) as api:
        return await run(api, seconds=seconds, streams=streams, out=out)
