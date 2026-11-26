"""icampus-fetcher: the loop against an in-process server with a fake content server (lease, download, upload,
delete), what happens when it is stopped, when the server takes a job back, when the network blips during an
upload; its work directory; the parts that differ per operating system, with the system calls replaced by
recorders (no caffeinate and no Windows call ever runs here); `check` and `bench`. All data is made up."""

import asyncio
import io
import json
import logging
import time
from pathlib import Path

import httpx
import pytest

from helpers import MP4, TOKEN, lecture, seed, settings
from icampus import bench, machine
from icampus import fetcher as fm
from icampus.api import create_app
from icampus.store import Store

MBP = "f" * 24
SECRET = "token=SECRET-QUERY"
LECTURES = [lecture("7001", title="3주차: 행렬 분해 / overview?"), lecture("7002"), lecture("7003")]


class Lcms:
    """lcms.skku.edu for both sides: a description, then one redirect to an address with a query, then the file.
    `hold` keeps every download open after its first bytes; `fail` holds statuses for the next downloads."""

    def __init__(self, body: bytes = MP4):
        self.body, self.seen, self.files, self.fail = body, [], [], []
        self.hold: asyncio.Event | None = None
        self.open = 0

    async def _send(self):
        self.open += 1
        try:
            yield self.body[:1000]
            if self.hold is not None:
                await self.hold.wait()
            yield self.body[1000:]
        finally:
            self.open -= 1

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert "cookie" not in request.headers and "authorization" not in request.headers  # never a token either
        assert request.url.host == "lcms.skku.edu"
        self.seen.append(request)
        path = request.url.path
        if path.startswith("/CommonsCore2/v2/contents/"):
            asset = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"result": {
                "original_file_length": len(self.body), "extension": "mp4", "author": "Some Professor",
                "download_url": f"https://lcms.skku.edu/CommonsCore2/v2/services/download/{asset}"}})
        if path.startswith("/CommonsCore2/v2/services/download/"):
            if request.headers.get("referer") != "https://canvas.skku.edu/":
                return httpx.Response(500)
            self.files.append(path.rsplit("/", 1)[1])
            return httpx.Response(302, headers={"location": f"https://lcms.skku.edu/index.php?module=x&{SECRET}"})
        if path == "/index.php":
            if self.fail:
                return httpx.Response(self.fail.pop(0), text="busy")
            return httpx.Response(200, content=self._send(), headers={"content-length": str(len(self.body))})
        return httpx.Response(404)


class Flaky(httpx.AsyncBaseTransport):
    """The network between the fetcher and the server. `blips` says, per upload piece (its Content-Range), what
    happens to the next sendings of it: "drop" (it never arrives) or "lose" (it arrives, and the answer is lost).
    `down`: nothing gets through at all. `calls` is every request the fetcher made."""

    def __init__(self, app):
        self.inner = httpx.ASGITransport(app=app)
        self.blips: dict[str, list[str]] = {}
        self.down = False
        self.calls: list[tuple[str, str]] = []
        self.when: list[float] = []  # when each of those calls was made
        self.ranges: list[str] = []
        self.headers: list[httpx.Headers] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        self.when.append(time.monotonic())
        self.headers.append(request.headers)
        if self.down:
            raise httpx.ConnectError("no route to host")
        if request.method != "PUT":
            return await self.inner.handle_async_request(request)
        piece = request.headers["content-range"]
        self.ranges.append(piece)
        blip = self.blips[piece].pop(0) if self.blips.get(piece) else None
        if blip == "drop":
            raise httpx.ConnectError("no route to host")
        response = await self.inner.handle_async_request(request)
        if blip == "lose":
            raise httpx.ReadError("connection reset")
        return response


class Awake(machine.KeepAwake):
    """A macOS-style KeepAwake whose caffeinate is a recorder."""

    def __init__(self):
        self.log: list[str] = []
        outer = self

        class Child:
            def terminate(self):
                outer.log.append("stopped")

            def wait(self, timeout=None):
                return 0
        super().__init__(True, kind="macos", caffeinate=lambda pid: outer.log.append("started") or Child())


class Rig:
    def __init__(self, tmp_path, lcms: Lcms, **env):
        self.tmp_path, self.lcms = tmp_path, lcms
        store = Store(tmp_path / "server" / "icampus.db")
        seed(store, LECTURES)
        store.set_state("collect_version", 99)
        self.app = create_app(settings(tmp_path / "server", sync_times="", fetcher_tokens=f"mbp:{MBP}",
                                       **{"local_downloads": "off", **env}), store=store, background=False)
        self.videos, self.store = self.app.state.videos, store
        self.videos._transport = httpx.MockTransport(lcms)
        self.net = Flaky(self.app)
        self.out = io.StringIO()
        self.awake = Awake()
        self.work = tmp_path / "laptop" / "work"

    def conf(self, **more) -> fm.FetcherSettings:
        return fm.FetcherSettings(_env_file=None, **{"server": "http://icampus.test", "token": MBP, "name": "mbp",
                                                     "dir": self.work, **more})

    def fetcher(self, lcms: Lcms | None = None, **conf) -> fm.Fetcher:
        return fm.Fetcher(self.conf(**conf), api_transport=self.net,
                          commons_transport=httpx.MockTransport(lcms or self.lcms), awake=self.awake,
                          console=fm.Console(self.out))

    def export(self, item: str) -> dict:
        from icampus import videos
        return self.videos.start(videos.find(self.store, item))

    def state(self, item: str) -> dict:
        from icampus import videos
        return self.videos.status(videos.find(self.store, item))

    def row(self, item: str) -> dict:
        return dict(self.store.one("SELECT * FROM video_jobs WHERE item_id=?", (item,)))

    def parts(self) -> list[str]:
        return sorted(p.name for p in self.work.glob("*.part")) if self.work.exists() else []


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(fm, "HEARTBEAT_S", 0.02)
    monkeypatch.setattr(fm, "IDLE_MIN_S", 0.01)
    monkeypatch.setattr(fm, "IDLE_MAX_S", 0.03)
    monkeypatch.setattr(fm, "PIECE", 1000)
    monkeypatch.setattr(fm, "LOCK_BEAT_S", 0.05)
    monkeypatch.setattr(fm, "AFTER_FAILURE_S", (0.01, 0.02))
    monkeypatch.setattr(fm, "UPLOAD_RETRY_S", (0.02, 0.05))
    return Rig(tmp_path, Lcms())


async def until(condition, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "it did not happen"
        await asyncio.sleep(0.005)


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# --- the loop ------------------------------------------------------------------------------------------------------

async def test_the_fetcher_takes_a_queued_export_downloads_it_uploads_it_and_keeps_nothing(rig, caplog):
    caplog.set_level(logging.DEBUG)
    rig.export("7001")
    rig.export("7002")
    seen = set()

    def watch() -> bool:
        seen.update((a["by"], a["phase"]) for a in rig.videos.summary()["active"])
        return rig.videos.summary()["ready"] == 2
    running = asyncio.create_task(rig.fetcher().run())
    await until(watch)
    assert seen and seen <= {("mbp", "downloading"), ("mbp", "uploading")}  # the status says who, and which phase
    # the server has confirmed the second upload: as soon as that answer is back, the file goes and sleep is free
    await until(lambda: rig.parts() == [] and rig.awake.held == 0, timeout=2)
    await stop(running)

    # the server ends up with what a download of its own leaves: the file, its sidecar, the cache entry
    for item, asset in (("7001", "asset7001"), ("7002", "asset7002")):
        assert rig.state(item)["state"] == "ready" and rig.videos.path(asset).read_bytes() == MP4
        meta = json.loads((rig.tmp_path / "server" / "videos" / f"{asset}.json").read_text())
        assert (meta["file"], meta["size"], meta["format"], meta["item_id"]) == (f"{asset}.mp4", len(MP4), "mp4", item)
        assert rig.app.state.cache.get("video", asset).size > len(MP4)
    assert rig.videos.meta("asset7001")["name"] == "3주차 행렬 분해 overview.mp4"  # the title names it on the server
    assert rig.store.query("SELECT * FROM video_jobs") == [] and rig.videos._reserved == {}
    assert rig.lcms.files == ["asset7001", "asset7002"]  # one at a time, in the order asked; the server fetched none
    assert rig.awake.log == ["started", "stopped", "started", "stopped"]  # awake only while a job was active
    assert [p.name for p in rig.work.iterdir()] == [".icampus-fetcher"]  # only the note that the directory is its own
    uploads = [r for r in rig.net.ranges]
    assert uploads[:2] == [f"bytes 0-999/{len(MP4)}", f"bytes 1000-1999/{len(MP4)}"] and len(uploads) == 10

    # what left this machine, and what was written down
    fetcher_row = rig.videos.fetcher_list()[0]
    assert fetcher_row["name"] == "mbp" and fetcher_row["active_jobs"] == 0 and fetcher_row["platform"]
    said = rig.out.getvalue()
    assert "downloading 3주차: 행렬 분해" in said and "uploaded 3주차: 행렬 분해" in said
    everything = said + caplog.text + json.dumps([dict(h) for h in rig.net.headers]) + str(rig.net.calls)
    assert "SECRET" not in everything and "index.php" not in everything  # the redirect went nowhere
    assert MBP not in said + caplog.text and "Some Professor" not in everything
    for headers in rig.net.headers:  # the server is told an id and numbers, with the fetcher's token and no other
        assert headers["authorization"] == f"Bearer {MBP}" and "cookie" not in headers


async def test_slots_download_side_by_side(rig):
    rig.lcms.hold = asyncio.Event()
    for item in ("7001", "7002", "7003"):
        rig.export(item)
    running = asyncio.create_task(rig.fetcher(slots=2).run())
    await until(lambda: rig.lcms.open == 2)
    await asyncio.sleep(0.05)
    assert rig.lcms.open == 2 and rig.parts() == ["asset7001.part", "asset7002.part"]  # named by asset id only
    assert [rig.state(i)["state"] for i in ("7001", "7002", "7003")] == ["downloading", "downloading", "queued"]
    assert rig.videos.fetcher_list()[0]["active_jobs"] == 2 and rig.awake.log == ["started"]
    rig.lcms.hold.set()
    await until(lambda: rig.videos.summary()["ready"] == 3)
    await stop(running)
    assert rig.parts() == [] and rig.awake.log == ["started", "stopped", "started", "stopped"]


async def test_stopping_the_fetcher_gives_its_job_back_at_once(rig):
    rig.lcms.hold = asyncio.Event()
    rig.export("7001")
    running = asyncio.create_task(rig.fetcher().run())
    await until(lambda: rig.lcms.open == 1 and rig.parts() == ["asset7001.part"])
    assert rig.state("7001")["by"] == "mbp" and rig.awake.held == 1
    await stop(running)  # Ctrl+C
    row = rig.row("7001")
    assert (row["state"], row["attempts"], row["error"], row["fetcher"]) == ("queued", 1, "mbp: fetcher stopped", None)
    assert rig.parts() == [] and rig.awake.held == 0 and rig.videos._reserved == {}
    assert ("POST", f"/api/v1/fetchers/jobs/{row['id']}/fail") in rig.net.calls
    assert not (rig.work / "fetcher.lock").exists()


async def test_a_job_taken_out_of_the_queue_is_dropped_and_its_file_deleted(rig):
    rig.lcms.hold = asyncio.Event()
    rig.export("7001")
    running = asyncio.create_task(rig.fetcher().run())
    await until(lambda: rig.parts() == ["asset7001.part"])
    assert rig.videos.cancel("asset7001")
    await until(lambda: rig.parts() == [] and rig.awake.held == 0)
    assert "dropped 3주차" in rig.out.getvalue() and not [c for c in rig.net.calls if c[1].endswith("/fail")]
    rig.lcms.hold.set()
    rig.export("7002")  # and it goes on with the next job
    await until(lambda: rig.state("7002")["state"] == "ready")
    await stop(running)
    assert rig.state("7001")["state"] == "not_downloaded"


async def test_after_a_sleep_the_fetcher_finds_its_lease_gone_and_starts_over(rig):
    """A closed lid: no heartbeats, the server takes the job back. On waking the fetcher is told its lease is
    stale, deletes the partial file and takes a job again."""
    rig.lcms.hold = asyncio.Event()
    rig.export("7001")
    running = asyncio.create_task(rig.fetcher().run())
    await until(lambda: rig.parts() == ["asset7001.part"] and rig.videos.leases["asset7001"].confirmed)
    first = rig.row("7001")["lease_id"]
    rig.store.execute("UPDATE video_jobs SET lease_expires_at='2000-01-01'")  # asleep for longer than the lease
    rig.videos.tick()
    row = rig.row("7001")
    assert row["state"] == "queued" and "the fetcher mbp went quiet" in row["error"]
    await until(lambda: rig.parts() == [])  # awake again: its next heartbeat is answered 409
    rig.lcms.hold.set()
    rig.store.execute("UPDATE video_jobs SET not_before=NULL")
    await until(lambda: rig.state("7001")["state"] == "ready")
    await stop(running)
    assert rig.lcms.files == ["asset7001", "asset7001"]  # from zero: the content server continues nothing
    assert rig.videos.path("asset7001").read_bytes() == MP4 and first not in str(rig.net.headers[-3:])


async def test_an_upload_goes_on_from_what_the_server_has_after_a_blip(rig):
    first, third, last = (f"bytes {a}-{b}/{len(MP4)}" for a, b in ((0, 999), (2000, 2999), (4000, len(MP4) - 1)))
    rig.net.blips = {first: ["lose"], third: ["drop", "drop"], last: ["lose"]}
    rig.export("7001")
    running = asyncio.create_task(rig.fetcher().run())
    await until(lambda: rig.state("7001")["state"] == "ready", timeout=30)
    await until(lambda: rig.parts() == [])
    await stop(running)
    assert rig.videos.path("asset7001").read_bytes() == MP4 and rig.lcms.files == ["asset7001"]  # one download
    asked = [c for c in rig.net.calls if c[0] == "GET" and c[1].endswith("/file")]
    assert len(asked) >= 3  # "how much do you have?" after each blip
    assert rig.net.ranges.count(first) == 1  # it had arrived, only its answer was lost: not sent again
    assert rig.net.ranges.count(third) == 3  # dropped twice on its way, then it arrived
    # the last piece arrived too and the server finished the video: asking again, the fetcher hears it is done
    assert rig.net.ranges.count(last) == 1 and "uploaded 3주차" in rig.out.getvalue()
    assert "dropped" not in rig.out.getvalue()
    assert rig.store.query("SELECT * FROM video_jobs") == [] and rig.awake.held == 0
    assert not [c for c in rig.net.calls if c[1].endswith("/fail")]


async def test_the_server_being_away_for_a_while_does_not_cost_the_job(rig, monkeypatch):
    monkeypatch.setattr(fm, "LOST_AFTER_S", 30.0)
    rig.lcms.hold = asyncio.Event()
    rig.export("7001")
    running = asyncio.create_task(rig.fetcher().run())
    await until(lambda: rig.parts() == ["asset7001.part"])
    rig.net.down = True  # the server restarts
    rig.lcms.hold.set()
    await asyncio.sleep(0.2)  # the download finishes meanwhile; heartbeats and the upload find nobody home
    assert rig.parts() == ["asset7001.part"] and rig.state("7001")["state"] == "downloading"
    rig.net.down = False
    await until(lambda: rig.state("7001")["state"] == "ready", timeout=20)
    await stop(running)
    assert rig.lcms.files == ["asset7001"] and rig.parts() == []


async def test_a_job_is_given_up_when_the_server_has_not_answered_for_too_long(rig, monkeypatch):
    monkeypatch.setattr(fm, "LOST_AFTER_S", 0.1)
    rig.lcms.hold = asyncio.Event()
    rig.export("7001")
    running = asyncio.create_task(rig.fetcher().run())
    await until(lambda: rig.parts() == ["asset7001.part"])
    rig.net.down = True
    await until(lambda: rig.parts() == [] and rig.awake.held == 0)  # nothing is kept for a server that is gone
    await until(lambda: "can't be reached (ConnectError)" in running.get_coro().cr_frame.f_locals["self"].idle)
    assert rig.lcms.files == ["asset7001"]  # and it waits for the server before it takes anything again
    await stop(running)


async def test_a_failed_download_is_reported_in_the_servers_own_words(rig):
    rig.lcms.fail = [503]
    rig.export("7001")
    running = asyncio.create_task(rig.fetcher().run())
    await until(lambda: rig.row("7001")["attempts"] == 1)
    row = rig.row("7001")
    assert (row["state"], row["transient"]) == ("queued", 1)
    assert row["error"] == "mbp: Commons refused the download (HTTP 503)"  # what videos.py says of its own tries
    assert rig.parts() == [] and "failed 3주차" in rig.out.getvalue()
    rig.store.execute("UPDATE video_jobs SET not_before=NULL")
    await until(lambda: rig.state("7001")["state"] == "ready")
    await stop(running)


async def test_after_a_failed_job_the_fetcher_waits_before_it_takes_another(rig, monkeypatch):
    """A machine that can't download would otherwise take one job after the other and cost each of them a try."""
    monkeypatch.setattr(fm, "AFTER_FAILURE_S", (0.4, 0.4))
    rig.lcms.fail = [503]
    rig.export("7001")
    rig.export("7002")
    running = asyncio.create_task(rig.fetcher().run())
    await until(lambda: rig.state("7002")["state"] == "ready")
    await stop(running)
    failed = next(i for i, call in enumerate(rig.net.calls) if call[1].endswith("/fail"))
    next_lease = next(i for i, call in enumerate(rig.net.calls) if i > failed and call[1].endswith("/lease"))
    assert rig.net.when[next_lease] - rig.net.when[failed] >= 0.35
    assert rig.row("7001")["attempts"] == 1 and rig.lcms.files == ["asset7001", "asset7002"]


def scripted(tmp_path, answers: dict[str, list]):
    """A fetcher whose server answers from a script: per method a list of (status, json), the last one for ever."""
    asked: list[tuple[str, str | None]] = []

    def server(request: httpx.Request) -> httpx.Response:
        asked.append((request.method, request.headers.get("content-range")))
        mine = answers[request.method]
        status, body = mine[min(sum(1 for method, _ in asked if method == request.method), len(mine)) - 1]
        return httpx.Response(status, json=body)
    conf = fm.FetcherSettings(_env_file=None, server="http://icampus.test", token=MBP, name="mbp", dir=tmp_path / "w")
    f = fm.Fetcher(conf, api_transport=httpx.MockTransport(server), awake=machine.KeepAwake(False),
                   console=fm.Console(io.StringIO()))
    part = tmp_path / "abc.part"
    part.write_bytes(MP4)
    job = fm.Job(job_id=1, lease_id="lease-of-a-made-up-job", asset_id="abc", host="lcms.skku.edu", title="",
                 expected=len(MP4), max_bytes=2**40)
    return f, job, part, asked


async def test_an_upload_waits_while_the_server_is_still_checking_the_file(tmp_path, monkeypatch):
    """After a restart the server reads a big upload back from disk before it answers the last piece, which can
    outlast the request. It then has every byte and no verdict yet: that is not "out of step"."""
    monkeypatch.setattr(fm, "PIECE", len(MP4))
    monkeypatch.setattr(fm, "UPLOAD_RETRY_S", (0.02, 0.05))
    size = len(MP4)
    f, job, part, asked = scripted(tmp_path, {
        "PUT": [(504, {})],                                           # the last piece: the answer never came
        "GET": [(200, {"received_bytes": size, "complete": False}),   # all of it is there, still being checked
                (200, {"received_bytes": size, "complete": False}),
                (200, {"received_bytes": size, "complete": True})]})  # and now it is the video
    async with f.client() as f.api:
        await asyncio.wait_for(f._upload(job, part, size, "0" * 64), 20)
    assert [method for method, _ in asked] == ["PUT", "GET", "GET", "GET"] and job.finished and job.up.done == size


async def test_an_upload_that_gets_nowhere_is_given_up(tmp_path, monkeypatch):
    monkeypatch.setattr(fm, "UPLOAD_STUCK_S", 0.3)
    monkeypatch.setattr(fm, "UPLOAD_RETRY_S", (0.02, 0.05))
    for answers in ({"PUT": [(500, {})], "GET": [(500, {})]},                                 # the server is in trouble
                    {"PUT": [(504, {})], "GET": [(200, {"received_bytes": len(MP4), "complete": False})]}):  # no verdict
        f, job, part, asked = scripted(tmp_path, answers)
        async with f.client() as f.api:
            with pytest.raises(fm.commons.VideoError, match="got nowhere") as failure:
                await asyncio.wait_for(f._upload(job, part, len(MP4), "0" * 64), 20)
        assert failure.value.transient and len(asked) >= 2  # handed back, for the queue to decide
    f, job, part, asked = scripted(tmp_path, {"PUT": [(409, {})]})
    async with f.client() as f.api:
        with pytest.raises(fm.Gone):
            await f._upload(job, part, len(MP4), "0" * 64)
    f, job, part, asked = scripted(tmp_path, {"PUT": [(422, {"error": "not an MP4", "refused": True})]})
    async with f.client() as f.api:
        with pytest.raises(fm.Refused, match="the server refused the file \\(not an MP4\\)"):
            await f._upload(job, part, len(MP4), "0" * 64)


async def test_a_file_the_server_refuses_is_dropped_without_a_second_report(rig):
    theirs = Lcms(b"PK\x03\x04" + b"z" * (len(MP4) - 4))  # this machine is served something else of the same size
    rig.export("7001")
    running = asyncio.create_task(rig.fetcher(lcms=theirs).run())
    await until(lambda: rig.row("7001")["attempts"] == 1)
    await until(lambda: rig.parts() == [] and rig.awake.held == 0)
    row = rig.row("7001")
    assert row["state"] == "queued" and row["error"].startswith("mbp: the uploaded file is not an MP4")
    assert "the server refused the file (the uploaded file is not an MP4" in rig.out.getvalue()
    assert not [c for c in rig.net.calls if c[1].endswith("/fail")] and rig.videos.path("asset7001") is None
    await stop(running)


async def test_a_refused_token_stops_the_fetcher_and_says_so(rig):
    wrong = fm.Fetcher(rig.conf(token="x" * 24), api_transport=rig.net, awake=rig.awake, console=fm.Console(rig.out))
    with pytest.raises(fm.Fatal, match="refused this fetcher's token \\(HTTP 401\\)") as failure:
        await asyncio.wait_for(wrong.run(), 5)
    assert "x" * 24 not in str(failure.value)
    api_token = fm.Fetcher(rig.conf(token=TOKEN, dir=rig.work / "b"), api_transport=rig.net, awake=rig.awake,
                           console=fm.Console(rig.out))
    with pytest.raises(fm.Fatal, match="HTTP 403"):
        await asyncio.wait_for(api_token.run(), 5)
    odd = fm.Fetcher(rig.conf(name="mbp/../x", dir=rig.work / "c"), api_transport=rig.net, awake=rig.awake,
                     console=fm.Console(rig.out))
    with pytest.raises(fm.Fatal, match="refused this fetcher's name"):
        await asyncio.wait_for(odd.run(), 5)
    assert not list(rig.work.rglob("fetcher.lock"))


async def test_a_job_with_an_id_or_host_the_fetcher_does_not_accept_is_given_back_untouched(rig):
    def server(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/lease"):
            return httpx.Response(200, json={"job_id": 1, "lease_id": "lease-of-a-made-up-job", "title": "x",
                                             "asset_id": server.asset, "host": server.host, "expected_bytes": 10,
                                             "max_bytes": 100, "lease_ttl_s": 90})
        server.told.append(json.loads(request.content))
        return httpx.Response(200, json={"state": "queued"})
    server.told = []
    nobody = httpx.MockTransport(lambda request: pytest.fail(f"a request went to {request.url.host}"))
    for server.asset, server.host in (("../../outside", "lcms.skku.edu"), ("abc", "evil.example"),
                                      ("abc", "lcms.skku.edu@evil.example"), ("con:x", "lcms.skku.edu")):
        f = fm.Fetcher(rig.conf(), api_transport=httpx.MockTransport(server), commons_transport=nobody,
                       awake=rig.awake, console=fm.Console(rig.out))
        f.dir.mkdir(parents=True, exist_ok=True)
        async with f.client() as f.api:
            assert await f._lease() is None
    assert len(server.told) == 4 and all(t["transient"] for t in server.told)
    assert list(rig.tmp_path.rglob("outside*")) == [] and rig.parts() == []


# --- the work directory ---------------------------------------------------------------------------------------------

def test_leftovers_are_deleted_at_start_and_nothing_else_is_touched(rig):
    rig.work.mkdir(parents=True)
    (rig.work / "asset7001.part").write_bytes(b"half a lecture")
    (rig.work / "notes.txt").write_text("mine")
    f = rig.fetcher()
    with pytest.raises(fm.Fatal, match="holds other files"):  # it deletes downloads there: not among your files
        f.prepare()
    assert sorted(p.name for p in rig.work.iterdir()) == ["asset7001.part", "notes.txt"]  # and it touched nothing

    own = rig.fetcher(dir=rig.tmp_path / "laptop" / "new")
    own.prepare()  # a directory it makes is its own, and private
    made = rig.tmp_path / "laptop" / "new"
    assert oct(made.stat().st_mode & 0o777) == "0o700" and (made / ".icampus-fetcher").is_file()
    own._lock.release()
    for name in ("asset7001.part", "5f3a-0B_9.part", "ubuntu-26.04.iso.part", "notes.txt"):
        (made / name).write_bytes(b"x")
    again = rig.fetcher(dir=made)
    again.prepare()  # what an earlier run left is deleted; a file it would never have named that way is not
    assert sorted(p.name for p in made.iterdir()) == [".icampus-fetcher", "fetcher.lock", "notes.txt",
                                                    "ubuntu-26.04.iso.part"]


async def test_a_work_file_the_system_would_not_let_go_of_is_deleted_before_the_next_job(rig, monkeypatch):
    """What Windows does while a scanner has the file open: the delete fails. The video must not stay."""
    real, refused = Path.unlink, []

    def unlink(self, missing_ok=False):
        if self.parent == rig.work and self.name == "asset7001.part" and not refused:  # the laptop's copy
            refused.append(self)
            raise PermissionError(13, "in use")
        return real(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", unlink)
    rig.export("7001")
    running = asyncio.create_task(rig.fetcher().run())
    await until(lambda: rig.state("7001")["state"] == "ready")
    await until(lambda: refused and rig.parts() == [])  # gone at the next look for work, a moment later
    await stop(running)


async def test_an_upload_request_the_server_calls_malformed_hands_the_job_back(rig):
    class Picky(Flaky):
        async def handle_async_request(self, request):
            if request.method == "PUT":
                self.calls.append((request.method, request.url.path))
                return httpx.Response(422, json={"error": "invalid parameters"})  # not a refusal of the file
            return await super().handle_async_request(request)
    rig.net = Picky(rig.app)
    rig.export("7001")
    running = asyncio.create_task(rig.fetcher().run())
    await until(lambda: rig.row("7001")["attempts"] == 1)
    row = rig.row("7001")
    assert (row["state"], row["error"]) == ("queued", "mbp: the server did not take the upload (HTTP 422)")
    await until(lambda: rig.parts() == [])
    await stop(running)


def test_one_fetcher_per_work_directory(rig, monkeypatch):
    first, second = rig.fetcher(), rig.fetcher()
    first.prepare()
    with pytest.raises(fm.Fatal, match="another icampus-fetcher is using"):
        second.prepare()  # it would have deleted the first one's downloads
    assert first._lock.beat() is True
    monkeypatch.setattr(fm, "LOCK_STALE_S", 0.0)  # the first one died without cleaning up, a while ago
    second.prepare()
    assert first._lock.beat() is False and second._lock.beat() is True  # the first, if it wakes, must stop
    first._lock.release()
    assert (rig.work / "fetcher.lock").exists()  # not its lock any more: left alone
    second._lock.release()
    assert not (rig.work / "fetcher.lock").exists()


def test_a_synced_folder_is_refused_as_work_directory(rig, monkeypatch):
    monkeypatch.setenv("OneDrive", str(rig.tmp_path / "OneDrive"))
    f = rig.fetcher(dir=rig.tmp_path / "OneDrive" / "Documents" / "fetcher")
    with pytest.raises(fm.Fatal, match="OneDrive copies to the cloud"):
        f.prepare()
    assert not (rig.tmp_path / "OneDrive").exists()  # nothing was made there


def test_room_is_the_free_disk_less_what_is_on_its_way_and_within_the_cap(rig, monkeypatch):
    monkeypatch.setattr(fm.shutil, "disk_usage", lambda path: type("U", (), {"free": 10 * 2**30})())
    f = rig.fetcher()
    assert f.room() == 9 * 2**30  # a margin stays free
    job = fm.Job(job_id=1, lease_id="x", asset_id="a", host="lcms.skku.edu", title="", expected=4 * 2**30, max_bytes=2**40)
    job.down.done = 2**30
    f.jobs[1] = job
    assert f.room() == 6 * 2**30  # the running download will write three more
    capped = rig.fetcher(max_disk_gb=5)
    capped.jobs[1] = job
    assert capped.room() == 2**30 and rig.fetcher(max_disk_gb=5).room() == 5 * 2**30


def test_settings_come_from_the_environment_a_dotenv_or_flags(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("SERVER", "TOKEN", "NAME", "SLOTS", "DIR", "MAX_DISK_GB", "KEEP_AWAKE"):
        monkeypatch.delenv(f"ICAMPUS_FETCHER_{name}", raising=False)
    plain = fm.FetcherSettings()
    assert (plain.server, plain.slots, plain.keep_awake, plain.max_disk_gb) == ("", 1, True, 0)
    assert plain.work_dir == machine.cache_dir() and plain.fetcher_name == machine.hostname()
    assert len(fm.problems(plain)) == 2
    (tmp_path / ".env").write_text(f"ICAMPUS_FETCHER_SERVER=http://100.64.0.10:9013/\nICAMPUS_FETCHER_TOKEN={MBP}\n"
                                   "ICAMPUS_FETCHER_SLOTS=2\nICAMPUS_FETCHER_TOKENS=other:" + "z" * 24 + "\n")
    from_file = fm.FetcherSettings()
    assert (from_file.api_url, from_file.slots) == ("http://100.64.0.10:9013", 2) and fm.problems(from_file) == []
    assert from_file.token.get_secret_value() == MBP and MBP not in repr(from_file)
    monkeypatch.setenv("ICAMPUS_FETCHER_SLOTS", "3")
    monkeypatch.setenv("ICAMPUS_FETCHER_KEEP_AWAKE", "false")
    assert (fm.FetcherSettings().slots, fm.FetcherSettings().keep_awake) == (3, False)
    assert fm.FetcherSettings(slots=4, name=" lab pc ").slots == 4  # a flag wins
    assert fm.FetcherSettings(name=" lab pc ").fetcher_name == "lab pc"
    for bad in (0, 5):
        with pytest.raises(ValueError):
            fm.FetcherSettings(slots=bad)
    # a variable that is there but empty is "not set": above all, never the current directory as work directory
    for name in ("DIR", "NAME", "SLOTS", "MAX_DISK_GB", "KEEP_AWAKE"):
        monkeypatch.setenv(f"ICAMPUS_FETCHER_{name}", "")
    blank = fm.FetcherSettings()
    assert blank.work_dir == machine.cache_dir() and (blank.slots, blank.max_disk_gb, blank.keep_awake) == (2, 0, True)
    assert fm.FetcherSettings(dir="~/somewhere/else").work_dir == Path.home() / "somewhere" / "else"
    assert fm.FetcherSettings(dir="work").work_dir == tmp_path / "work"
    monkeypatch.delenv("ICAMPUS_FETCHER_SLOTS")
    assert fm.problems(fm.FetcherSettings(server="ftp://x")) == [
        "ICAMPUS_FETCHER_SERVER must be an http(s) address, e.g. http://<tailnet address>:9013"]
    assert fm.public_plain_http("http://8.8.8.8:9013") and not fm.public_plain_http("https://8.8.8.8")
    for private in ("http://100.64.0.10:9013", "http://192.168.1.10:9013", "http://127.0.0.1:9013", "http://homelab:9013"):
        assert not fm.public_plain_http(private)


def test_command_line_flags_work_before_and_after_the_command(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for name in ("SERVER", "TOKEN", "NAME", "SLOTS", "DIR"):
        monkeypatch.delenv(f"ICAMPUS_FETCHER_{name}", raising=False)
    seen = {}

    async def fake_check(conf, **kwargs):
        seen["conf"] = conf
        return 0
    monkeypatch.setattr(fm, "check", fake_check)
    with pytest.raises(SystemExit) as done:
        fm.main(["--server", "http://100.64.0.10:9013", "--slots", "2", "check", "--name", "lab", "--no-keep-awake"])
    conf = seen["conf"]
    assert done.value.code == 0 and (conf.api_url, conf.slots, conf.name, conf.keep_awake) == (
        "http://100.64.0.10:9013", 2, "lab", False)
    with pytest.raises(SystemExit, match="slots: Input should be less than or equal to 4"):
        fm.main(["run", "--slots", "9"])
    with pytest.raises(SystemExit, match="no server.*no token"):
        fm.main(["run"])
    with pytest.raises(SystemExit, match="--streams takes numbers"):
        fm.main(["--server", "http://x", "--token", MBP, "bench", "--streams", "a,b"])


# --- what differs per operating system (machine.py) -----------------------------------------------------------------

def test_the_work_directory_defaults_to_the_systems_per_user_cache():
    home = Path("/Users/minsu")
    assert machine.cache_dir("macos", {}, home) == home / "Library" / "Caches" / "skku-icampus-fetcher"
    local = "C:\\Users\\minsu\\AppData\\Local"
    assert machine.cache_dir("windows", {"LOCALAPPDATA": local}, home) == Path(local) / "skku-icampus-fetcher"
    assert machine.cache_dir("windows", {}, home) == home / "AppData" / "Local" / "skku-icampus-fetcher"
    assert machine.cache_dir("other", {}, home) == home / ".cache" / "skku-icampus-fetcher"
    assert machine.cache_dir("other", {"XDG_CACHE_HOME": "/var/cache/me"}, home) == Path("/var/cache/me/skku-icampus-fetcher")
    assert (machine.system("darwin"), machine.system("win32"), machine.system("linux")) == ("macos", "windows", "other")
    assert machine.describe() and len(machine.describe()) <= 60


def test_synced_folders_are_recognised(tmp_path):
    home = tmp_path / "home"
    (home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Documents").mkdir(parents=True)
    (home / ".dropbox").mkdir()
    (home / ".dropbox" / "info.json").write_text(json.dumps({"personal": {"path": str(tmp_path / "elsewhere" / "Box")}}))
    env = {"OneDrive": str(tmp_path / "od")}

    def by(path, kind="macos"):
        return machine.synced_by(Path(path), kind, env, home)
    assert by(home / "Library" / "Caches" / "skku-icampus-fetcher") is None  # the default is fine
    assert by(home / "AppData" / "Local" / "skku-icampus-fetcher", "windows") is None
    assert by(home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "x") == "iCloud Drive"
    assert by(home / "Library" / "CloudStorage" / "OneDrive-Personal" / "x") == "a cloud storage app"
    assert by(tmp_path / "od" / "Documents" / "x", "windows") == "OneDrive"  # where Windows says OneDrive is
    assert by(tmp_path / "OD" / "x", "windows") == "OneDrive"  # whatever the letters' case
    assert by(tmp_path / "elsewhere" / "Box" / "x") == "Dropbox"  # where Dropbox says its folder is
    assert by(tmp_path / "D" / "OneDrive - SKKU" / "x", "windows") == "OneDrive"  # by its name, wherever it is
    assert by(home / "Dropbox (Personal)" / "x") == "Dropbox" and by(home / "Google Drive" / "x") == "Google Drive"
    assert by(home / "iCloud Drive (Archive)" / "x") == "iCloud Drive"
    assert by(home / "Documents" / "x") == "iCloud Drive (Desktop & Documents)"  # that folder is in iCloud here
    assert by(home / "Desktop" / "x") is None and by(home / "Documents" / "x", "windows") is None
    assert by(home / "Videos" / "fetcher", "windows") is None and by(tmp_path / "odd") is None


def test_keep_awake_holds_only_while_a_job_is_active_and_never_the_display():
    calls: list = []

    class Child:
        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout=None):
            calls.append("wait")
    mac = machine.KeepAwake(True, kind="macos", caffeinate=lambda pid: calls.append(("caffeinate", pid)) or Child())
    mac.hold()
    mac.hold()  # a second job: still one caffeinate
    assert calls == [("caffeinate", __import__("os").getpid())]
    mac.release()
    assert len(calls) == 1
    mac.release()
    mac.release()  # one too many does nothing
    assert calls[1:] == ["terminate", "wait"] and mac.held == 0

    states: list[int] = []
    windows = machine.KeepAwake(True, kind="windows", execution_state=states.append)
    windows.hold()
    windows.release()
    assert states == [0x80000001, 0x80000000]  # system required while a job runs; then only "continuous"
    assert not any(state & 0x00000002 for state in states)  # ES_DISPLAY_REQUIRED: the screen may always sleep

    def boom(*args):
        raise AssertionError("a system call was made")
    for quiet in (machine.KeepAwake(True, kind="other", caffeinate=boom, execution_state=boom),
                  machine.KeepAwake(False, kind="macos", caffeinate=boom),
                  machine.KeepAwake(False, kind="windows", execution_state=boom)):
        quiet.hold()
        quiet.release()
    broken = machine.KeepAwake(True, kind="macos", caffeinate=lambda pid: (_ for _ in ()).throw(FileNotFoundError()))
    broken.hold()  # no caffeinate on this Mac: the job runs anyway
    broken.release()
    here = machine.KeepAwake(True)  # on the machine the tests run on, nothing is called at all
    assert here.enabled is (machine.system() != "other")


def test_a_machine_name_becomes_a_name_the_server_accepts(monkeypatch):
    import re

    from icampus.api_fetchers import NAME
    for raw, name in (("Minsus-MacBook-Pro.local", "Minsus-MacBook-Pro"), ("DESKTOP-AB12CD", "DESKTOP-AB12CD"),
                      ("민수의 노트북", "민수의 노트북"), ("a/b\\c:d", "a-b-c-d"), ("", "fetcher"), ("...", "fetcher"),
                      ("x" * 80, "x" * 40)):
        monkeypatch.setattr(machine.socket, "gethostname", lambda raw=raw: raw)
        assert machine.hostname() == name and re.fullmatch(NAME, machine.hostname())


def test_a_status_line_fits_the_terminal_with_korean_titles():
    assert fm.fit("Lecture 7001", 20) == "Lecture 7001"
    cut = fm.fit("3주차: 행렬 분해와 고유값 문제", 16)
    assert cut.endswith("…") and fm._width(cut) <= 16 and cut.startswith("3주차")
    out = io.StringIO()
    console = fm.Console(out)
    console.status("never shown: not a terminal")
    console.event("downloading x")
    assert out.getvalue().endswith(" downloading x\n") and "never shown" not in out.getvalue()
    job = fm.Job(job_id=1, lease_id="x", asset_id="abc", host="lcms.skku.edu", title="1주차 소개", expected=200 * 2**20,
                 max_bytes=2**40)
    job.down.done = 50 * 2**20
    assert job.line() == "1주차 소개  50/200 MB  – MB/s  downloading"


# --- check and bench ------------------------------------------------------------------------------------------------

async def test_check_says_what_works_and_what_does_not(rig, monkeypatch):
    lines: list[str] = []
    lcms = httpx.MockTransport(rig.lcms)

    async def run(**conf) -> tuple[int, str]:
        lines.clear()
        code = await fm.check(rig.conf(**conf), out=lines.append, api_transport=rig.net, commons_transport=lcms)
        return code, "\n".join(lines)
    code, said = await run()
    assert code == 0 and [line.split()[1] for line in lines if not line.startswith("work")] == ["ok"] * 4
    assert "accepted as a fetcher token" in said and "lcms.skku.edu described a video" in said and "GB free" in said
    assert "not in a synced folder" in said and MBP not in said
    assert rig.videos.fetcher_list() == [] and rig.store.query("SELECT * FROM video_jobs") == []  # it took nothing
    assert rig.lcms.files == []  # one description, no download

    code, said = await run(token=TOKEN)
    assert code == 1 and "this is an API token, not a fetcher token" in said
    code, said = await run(token="x" * 24)
    assert code == 1 and "refused (HTTP 401)" in said and "x" * 24 not in said
    code, said = await run(server="")
    assert code == 1 and "no server" in said
    monkeypatch.setenv("OneDrive", str(rig.tmp_path / "OneDrive"))
    code, said = await run(dir=rig.tmp_path / "OneDrive" / "fetcher")
    assert code == 0 and "WARNING" in said and "OneDrive copies to the cloud" in said  # a warning; `run` refuses
    rig.net.down = True
    code, said = await run()
    assert code == 1 and "can't be reached (ConnectError)" in said and "Tailscale" in said


async def test_bench_measures_each_number_of_streams_and_keeps_nothing(rig, monkeypatch):
    monkeypatch.chdir(rig.tmp_path)
    before = sorted(p.name for p in rig.tmp_path.rglob("*") if p.is_file())
    body = MP4 * 1500  # more than a step can read, so each stream stays on its video

    class Slow(Lcms):
        async def _send(self):
            for start in range(0, len(body), 50_000):
                await asyncio.sleep(0.01)
                yield body[start:start + 50_000]
    lcms = Slow(body)
    lines: list[str] = []
    async with httpx.AsyncClient(transport=rig.net, base_url="http://icampus.test",
                                 headers={"Authorization": f"Bearer {MBP}"}) as api:
        code = await bench.run(api, seconds=0.5, streams=[1, 2, 3], out=lines.append, transport=httpx.MockTransport(lcms))
    assert code == 0
    table = [line.split() for line in lines if line.split() and line.split()[0] in ("1", "2", "3")]
    assert [len(row) for row in table] == [3, 4, 5]  # per stream, and the total
    for row in table:
        speeds = [float(x) for x in row[1:-1]]
        assert all(speed > 0 for speed in speeds) and float(row[-1]) == pytest.approx(sum(speeds), abs=0.02)
    assert float(table[2][-1]) > 1.5 * float(table[0][-1])  # this fake limits each connection: the total grows
    by_step = [lcms.files[0:1], lcms.files[1:3], lcms.files[3:6]]
    assert [len(set(step)) for step in by_step] == [1, 2, 3]  # each stream on a different video
    assert "nothing is kept" in lines[0] and "limits each connection" in lines[-1]
    assert sorted(p.name for p in rig.tmp_path.rglob("*") if p.is_file()) == before  # not a byte on disk
    assert rig.store.query("SELECT * FROM video_jobs") == [] and "SECRET" not in "\n".join(lines)

    lcms.fail = [503] * 9
    lines.clear()
    async with httpx.AsyncClient(transport=rig.net, base_url="http://icampus.test",
                                 headers={"Authorization": f"Bearer {TOKEN}"}) as api:  # an API token may ask too
        code = await bench.run(api, seconds=0.2, streams=[2], out=lines.append, transport=httpx.MockTransport(lcms))
    assert code == 1 and "failed  failed" in "\n".join(lines) and "Commons refused the download (HTTP 503)" in "\n".join(lines)
    async with httpx.AsyncClient(transport=rig.net, base_url="http://icampus.test",
                                 headers={"Authorization": "Bearer nope"}) as api:
        assert await bench.run(api, out=lines.append) == 1 and "refused the request (HTTP 401)" in lines[-1]


def test_bench_says_when_the_queue_is_downloading_and_reads_its_streams():
    assert bench.parse_streams("1,2,3") == [1, 2, 3] and bench.parse_streams(" 4 ") == [4]
    for bad in ("", "0", "9", "a", "1,,x"):
        with pytest.raises(ValueError, match="--streams"):
            bench.parse_streams(bad)

    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0, "items": [{"asset_id": "../x", "host": "lcms.skku.edu"},
                                                               {"asset_id": "abc", "host": "evil.example"}],
                                         "downloading": 2})
    lines: list[str] = []

    async def go() -> int:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server), base_url="http://icampus.test") as api:
            return await bench.run(api, out=lines.append)
    assert asyncio.run(go()) == 1 and "knows no hosted lecture video" in lines[-1]  # neither target is one to ask


def test_icampus_bench_is_a_command_of_the_server_cli(monkeypatch, tmp_path):
    from icampus import cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ICAMPUS_API_TOKENS", f"mcp:{TOKEN}")
    monkeypatch.setenv("ICAMPUS_PORT", "9999")
    asked = {}

    async def fake(server, token, **how):
        asked.update(server=server, token=token, **how)
        return 0
    monkeypatch.setattr(bench, "main", fake)
    monkeypatch.setattr("sys.argv", ["icampus", "bench", "--seconds", "5", "--streams", "1,2"])
    with pytest.raises(SystemExit) as done:
        cli.main()
    assert done.value.code == 0
    assert asked == {"server": "http://127.0.0.1:9999", "token": TOKEN, "seconds": 5.0, "streams": [1, 2]}
