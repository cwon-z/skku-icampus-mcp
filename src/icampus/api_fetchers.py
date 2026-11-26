"""The routes a fetcher calls: another machine of the owner's that downloads lecture videos for the queue and
uploads them here (fetcher.py; docs/fetchers.md).

Only a token from ICAMPUS_FETCHER_TOKENS opens them, and such a token opens nothing else: a fetcher learns an asset
id, a host and a title, and never reads a course, a file or a transcript. It gets no SKKU login and no session
from here, because the content server needs none. The one route an ordinary API token may also call is
`bench-targets`, for `icampus bench` on the server itself.

The queue's rules stay in videos.py: who gets which job, how long a lease lasts, what an upload must pass before
it becomes a cached video, and what a failed try costs. A call that names a lease which is no longer the job's is
answered 409, and the fetcher drops that job.
"""

import re
from types import SimpleNamespace
from typing import Annotated, AsyncIterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.requests import ClientDisconnect

from .videos import UPLOAD_PIECE_MAX, OutOfStep, Refused, StaleLease

NAME = r"^\w[\w .\-]{0,39}$"  # shown in statuses, stored in errors: a short plain name
PLAIN = r"^[^\x00-\x1f\x7f]*$"
LeaseId = Annotated[str, Field(min_length=8, max_length=200)]
LeaseHeader = Annotated[str, Header(min_length=8, max_length=200)]  # X-Lease-Id, where the body is the file
_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


class LeaseRequest(BaseModel):
    name: str = Field(pattern=NAME, description="what this fetcher is called in statuses")
    version: str = Field("", max_length=40, pattern=PLAIN)
    platform: str = Field("", max_length=60, pattern=PLAIN)
    slots_free: int = Field(1, ge=0, le=16, description="0 only checks the token: nothing is leased")
    free_bytes: int | None = Field(None, ge=0, description="the largest video this fetcher has room for; a "
                                                           "bigger one is left for the server or another fetcher")


class Progress(BaseModel):
    lease_id: LeaseId
    phase: Literal["downloading", "uploading"]
    done_bytes: int = Field(ge=0)
    total_bytes: int | None = Field(None, ge=0)
    speed_bps: float | None = Field(None, ge=0, le=1e12, description="bytes per second of the phase it is in")


class Failure(BaseModel):
    lease_id: LeaseId
    message: str = Field(max_length=500, description="short and safe to show: no address, no token")
    transient: bool = False


async def _drain(body: AsyncIterator[bytes]) -> None:
    """Reads what is left of a request that is being refused, so the answer reaches a fetcher still sending it."""
    try:
        async for _ in body:
            pass
    except (ClientDisconnect, RuntimeError):
        pass


def register(app: FastAPI, ctx: SimpleNamespace) -> None:
    fetcher = [Depends(ctx.fetcher_auth)]
    videos = ctx.videos
    stale = HTTPException(409, "this lease is no longer the job's (it ran out, or the job was taken out of the "
                               "queue): drop the job")

    @app.post("/api/v1/fetchers/lease", dependencies=fetcher, tags=["fetchers"],
              responses={204: {"description": "nothing is due for this fetcher"}})
    async def lease(ask: LeaseRequest):
        """Take the next due download: 200 with `{job_id, lease_id, asset_id, host, title, expected_bytes,
        max_bytes, lease_ttl_s}`, or 204 when nothing is due. The job is this fetcher's for `lease_ttl_s` seconds
        from its last call about it. `expected_bytes` is the size Commons gave this server for the file (null
        when it gave none); the upload must have exactly that size."""
        job = await videos.lease(ask.name, platform=ask.platform, version=ask.version, slots_free=ask.slots_free,
                                 free_bytes=ask.free_bytes)
        return job if job else Response(status_code=204)

    @app.post("/api/v1/fetchers/jobs/{job_id}/progress", dependencies=fetcher, tags=["fetchers"])
    async def progress(job_id: int, report: Progress) -> dict:
        """Say where the job stands, every 5 to 10 seconds: that is the heartbeat that keeps the lease. In phase
        `uploading`, `done_bytes` counts what was sent. `continue: false` means the job is gone: stop, and delete
        the file (`complete: true` beside it: because the upload was taken and the video is here)."""
        if videos.finished(report.lease_id) is not None:
            return {"continue": False, "complete": True}
        try:
            go_on = videos.heartbeat(job_id, report.lease_id, phase=report.phase, done=report.done_bytes,
                                     total=report.total_bytes, speed=report.speed_bps)
        except StaleLease:
            raise stale from None
        return {"continue": go_on}

    @app.put("/api/v1/fetchers/jobs/{job_id}/file", dependencies=fetcher, tags=["fetchers"])
    async def upload(job_id: int, request: Request, content_range: Annotated[str, Header()],
                     x_lease_id: LeaseHeader,
                     x_file_sha256: Annotated[str | None, Header(pattern=r"^[0-9a-fA-F]{64}$")] = None):
        """Send a piece of the downloaded file: the body is the bytes `Content-Range: bytes <first>-<last>/<size>`
        names, and `X-Lease-Id` the lease. A piece must start where the file on the server ends; 416 answers
        with `received_bytes` when it doesn't, and GET on this path says the same at any time, so an upload cut
        off by the network goes on from what arrived. The last piece carries `X-File-SHA256`, the SHA-256 of the
        whole file as the fetcher downloaded it. The server then checks the size against the one Commons gave
        it, the hash against the bytes it has, and that the file is an MP4: `complete: true`, or 422 when a check
        fails (that try has then failed, and the fetcher drops the job)."""
        body = request.stream()
        m = _RANGE.fullmatch(content_range.strip())
        first, last, size = map(int, m.groups()) if m else (0, -1, 0)
        try:
            if not m or last < first or last >= size:
                raise ValueError("Content-Range must be `bytes <first>-<last>/<size>`")
            if last - first + 1 > UPLOAD_PIECE_MAX:
                raise HTTPException(413, f"a piece can be at most {UPLOAD_PIECE_MAX // 2**20} MB")
            return await videos.receive(job_id, x_lease_id, start=first, length=last - first + 1, total=size,
                                        sha256=x_file_sha256, body=body)
        except StaleLease:
            await _drain(body)
            if (had := videos.finished(x_lease_id)) is not None:  # sent again after its answer was lost
                return {"received_bytes": had, "complete": True}
            raise stale from None
        except OutOfStep as exc:
            await _drain(body)
            return JSONResponse({"error": str(exc), "received_bytes": exc.received}, status_code=416)
        except Refused as exc:
            raise HTTPException(422, {"error": str(exc), "refused": True}) from None
        except ValueError as exc:
            await _drain(body)
            raise HTTPException(400, str(exc)) from None
        except HTTPException:
            await _drain(body)
            raise
        except ClientDisconnect:  # the fetcher is gone mid-piece; what arrived stays, and it asks where to go on
            return Response(status_code=400)

    @app.get("/api/v1/fetchers/jobs/{job_id}/file", dependencies=fetcher, tags=["fetchers"])
    async def uploaded(job_id: int, x_lease_id: LeaseHeader) -> dict:
        """How many bytes of this job's upload the server has: where the next piece must start. `complete: true`
        when the whole file was taken and finished (the answer to the last piece may have been lost)."""
        if (had := videos.finished(x_lease_id)) is not None:
            return {"received_bytes": had, "complete": True}
        try:
            return {"received_bytes": videos.received(job_id, x_lease_id), "complete": False}
        except StaleLease:
            raise stale from None

    @app.post("/api/v1/fetchers/jobs/{job_id}/fail", dependencies=fetcher, tags=["fetchers"])
    async def fail(job_id: int, report: Failure) -> dict:
        """Give the job back after a failed try. `transient` says whether trying again could work; what happens
        next (a pause and another try by whoever is free, or giving up) is decided by the queue, exactly as for a
        download of the server's own."""
        try:
            return videos.fail_lease(job_id, report.lease_id, report.message, report.transient)
        except StaleLease:
            raise stale from None

    @app.get("/api/v1/fetchers/bench-targets", dependencies=[Depends(ctx.any_auth)], tags=["fetchers"])
    async def bench_targets(count: int = Query(4, ge=1, le=8)) -> dict:
        """A few hosted lecture videos to measure download speed with (`icampus bench`, `icampus-fetcher bench`):
        asset id and host, nothing else. `downloading` is how many downloads the queue is running right now,
        which share the line with a measurement made from the same place. Reads the database only."""
        items = videos.bench_targets(count)
        return {"count": len(items), "items": items, "downloading": videos.summary()["downloading"]}
