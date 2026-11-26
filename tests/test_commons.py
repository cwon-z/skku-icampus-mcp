"""commons.py by itself: the one client for SKKU's content server, as the server's worker, a fetcher and bench use
it. (How the download queue uses it is in test_videos.py, which predates the module and passes unchanged.) The
content server here is made up: no real asset, host answer or lecture."""

import asyncio
import hashlib
import subprocess
import sys

import httpx
import pytest

from icampus import commons
from icampus.commons import VideoError

MP4 = b"\0\0\0\x18ftypmp42" + b"x" * 4000
HOST = "lcms.skku.edu"
SECRET = "token=SECRET-QUERY"


class Server:
    """Describes any asset and serves `body` behind one redirect to an address with a query string."""

    def __init__(self, body=MP4, **described):
        self.body, self.described, self.seen = body, described, []
        self.redirect_to = f"https://{HOST}/index.php?module=x&{SECRET}"
        self.status, self.content_type, self.hang = 200, "application/octet-stream", False

    async def _slowly(self):
        yield self.body[:100]
        await asyncio.sleep(3600)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        if path.startswith("/CommonsCore2/v2/contents/"):
            return httpx.Response(200, json={"result": {
                "original_file_length": len(self.body), "extension": "mp4", "author": "Some Professor",
                "download_url": f"https://{HOST}/CommonsCore2/v2/services/download/{path.rsplit('/', 1)[1]}",
                **self.described}})
        if path.startswith("/CommonsCore2/v2/services/download/"):
            if request.headers.get("referer") != "https://canvas.skku.edu/":
                return httpx.Response(500)
            return httpx.Response(302, headers={"location": self.redirect_to})
        if path == "/index.php":
            if self.hang:
                return httpx.Response(200, content=self._slowly(), headers={"content-length": str(len(self.body))})
            return httpx.Response(self.status, content=self.body, headers={"content-type": self.content_type})
        return httpx.Response(404)


async def fetch(server, part, **how):
    meter, digest = commons.Meter(), hashlib.sha256()
    async with commons.attempt(httpx.MockTransport(server), **how) as content:
        info = await content.describe(HOST, "abc123", max_bytes=10_000)
        got = await content.fetch(part, meter=meter, digest=digest)
    return info, got, meter, digest


async def test_a_download_goes_to_the_path_given_with_a_referer_and_never_a_cookie(tmp_path):
    server, ticks = Server(), []
    part = tmp_path / "abc123.part"
    info, got, meter, digest = await fetch(server, part, on_alive=lambda: ticks.append(1))
    assert part.read_bytes() == MP4 and got == commons.Fetched(size=len(MP4), mp4=True)
    assert (meter.done, meter.total) == (len(MP4), len(MP4))
    assert digest.hexdigest() == hashlib.sha256(MP4).hexdigest()  # what a fetcher sends along with its upload
    assert ticks and commons.original_size(info) == len(MP4) and commons.extension(info, got.mp4) == ".mp4"
    for request in server.seen:
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert request.headers["referer"] == "https://canvas.skku.edu/" and request.url.host == HOST


async def test_progress_is_reported_through_the_callback(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(commons.time, "monotonic", lambda: clock[0])
    body = MP4 * 40

    class Chunked(Server):
        async def _pieces(self):
            for start in range(0, len(self.body), 20_000):
                clock[0] += 1.5  # each piece a second and a half after the last
                yield self.body[start:start + 20_000]

        def __call__(self, request):
            if request.url.path == "/index.php":
                return httpx.Response(200, content=self._pieces(), headers={"content-length": str(len(self.body))})
            return super().__call__(request)

    meter, heard = commons.Meter(), []
    async with commons.attempt(httpx.MockTransport(Chunked(body))) as content:
        await content.describe(HOST, "abc123", max_bytes=10**9)
        await content.fetch(tmp_path / "abc123.part", meter=meter, progress=lambda: heard.append(meter.done))
    assert len(heard) >= 3 and heard == sorted(heard) and heard[-1] == len(body)
    assert meter.speed() == pytest.approx(20_000 / 1.5, rel=0.2)


@pytest.mark.parametrize("change, message, transient", [
    ({"described": {"is_original_file_deleted": True}}, "no longer has", False),
    ({"described": {"original_file_length": 20_000}}, "ICAMPUS_VIDEO_MAX_GB", False),
    ({"redirect_to": "https://evil.example/file.mp4?x=1"}, "redirected away from SKKU", False),
    ({"content_type": "text/html"}, "web page instead", False),
    ({"status": 503}, "HTTP 503", True),
    ({"status": 429}, "HTTP 429", True),
    ({"status": 403}, "HTTP 403", False),
])
async def test_what_is_refused_and_whether_it_may_pass(tmp_path, change, message, transient):
    server = Server(**change.pop("described", {}))
    for name, value in change.items():
        setattr(server, name, value)
    with pytest.raises(VideoError, match=message) as failure:
        await fetch(server, tmp_path / "abc123.part")
    assert failure.value.transient is transient and "SECRET" not in str(failure.value)
    assert commons.failure(failure.value) == (str(failure.value), transient)


async def test_a_host_outside_skku_is_never_asked(tmp_path):
    def nobody(request):
        pytest.fail(f"a request went to {request.url.host}")
    for host in ("evil.example", "lcms.skku.edu.evil.example", "lcms.skku.edu@evil.example", "evil.example/x.skku.edu"):
        with pytest.raises(VideoError, match="not on an SKKU content server"):
            async with commons.attempt(httpx.MockTransport(nobody)) as content:
                await content.describe(host, "abc123", max_bytes=10_000)
    assert commons.host_ok("https://lcms.skku.edu/x") and not commons.host_ok("http://lcms.skku.edu/x")


async def test_a_content_server_that_goes_silent_ends_the_try(tmp_path):
    server = Server()
    server.hang = True
    with pytest.raises(VideoError, match="sent nothing for 0.05 seconds") as failure:
        await fetch(server, tmp_path / "abc123.part", stall_s=0.05)
    assert failure.value.transient


async def test_an_answer_without_a_length_that_is_shorter_than_described_is_not_taken(tmp_path):
    class NoLength(Server):
        def __call__(self, request):
            answer = super().__call__(request)
            if request.url.path == "/index.php":
                del answer.headers["content-length"]
            return answer
    with pytest.raises(VideoError, match="cut short") as failure:
        await fetch(NoLength(original_file_length=len(MP4) + 500), tmp_path / "abc123.part")
    assert failure.value.transient


async def test_discard_reads_the_file_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    counted = []
    async with commons.attempt(httpx.MockTransport(Server())) as content:
        await content.describe(HOST, "abc123", max_bytes=10_000)
        await content.discard(counted.append)
    assert sum(counted) == len(MP4) and list(tmp_path.iterdir()) == []


def test_how_a_failed_try_is_reported_never_carries_an_address():
    request = httpx.Request("GET", f"https://{HOST}/index.php?{SECRET}")
    told = [commons.failure(exc) for exc in (
        httpx.ReadTimeout(f"timed out reading https://{HOST}/index.php?{SECRET}", request=request),
        httpx.ConnectError(f"https://{HOST}/index.php?{SECRET}"),
        TimeoutError(), OSError(28, f"No space left on device: /x?{SECRET}"), KeyError(SECRET))]
    assert told == [("lcms.skku.edu: ReadTimeout", True), ("lcms.skku.edu: ConnectError", True),
                    ("the download took longer than 6 hours", True),
                    ("could not write the video to disk (ENOSPC)", True), ("KeyError", False)]
    assert "SECRET" not in str(told)


def test_ids_and_extensions():
    assert commons.SAFE_ID.fullmatch("a1b2c3") and commons.SAFE_ID.fullmatch("5f3a-0B_9")
    for bad in ("", "../x", "a/b", "a\\b", "a:b", "a.mp4", "con ", "x" * 200):
        assert not commons.SAFE_ID.fullmatch(bad), bad
    assert commons.extension({"extension": "MP4"}, False) == ".bin"  # not an MP4 by its bytes: no fake .mp4
    assert commons.extension({"extension": "wmv"}, False) == ".wmv" and commons.extension({}, True) == ".mp4"
    assert commons.extension({"extension": "../x"}, False) == ".bin"
    assert commons.original_size({"original_file_length": "123"}) == 123 and commons.original_size({}) == 0
    assert commons.original_size({"original_file_length": "lots"}) == 0


def test_the_fetcher_and_its_helpers_import_no_server_code():
    """`uv tool install` on a laptop must give a fetcher that starts without a browser, a web framework, the
    database or ffmpeg: its modules import the standard library, httpx and pydantic-settings only."""
    code = ("import sys, icampus.fetcher, icampus.bench, icampus.commons, icampus.machine\n"
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in {'playwright', 'fastapi', 'starlette', "
            "'uvicorn', 'mcp', 'jinja2', 'bs4', 'pypdf', 'jwt', 'sqlite3', 'multipart'} or m in {'icampus.store', "
            "'icampus.videos', 'icampus.api', 'icampus.browser', 'icampus.audio', 'icampus.pipeline', "
            "'icampus.cache', 'icampus.config', 'icampus.runtime'})\n"
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "[]", out.stdout + out.stderr
