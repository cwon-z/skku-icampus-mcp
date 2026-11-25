"""Lecture video downloads from a fake Commons server: Referer and no cookies, redirects kept on SKKU hosts,
web pages refused, size caps, the cache (reuse, outdated copies, least recently used out first), and the download
queue: it runs by itself in the order asked, survives a restart, tries again after a stall, and fails visibly."""

import asyncio
import time
from datetime import datetime, timedelta

import httpx
import pytest

from icampus import videos as vm
from icampus.cache import Cache
from icampus.config import KST, Settings
from icampus.store import Store
from icampus.videos import MB, VideoError, Videos

MP4 = b"\0\0\0\x18ftypmp42" + b"x" * 4000
HOST = "lcms.skku.edu"


def lecture(asset="a1b2c3", item="7002", updated="20260301120000", size=len(MP4)):
    return {"key": f"canvas.skku.edu:1001:modules/items:{item}", "course_id": "1001", "item_id": item,
            "title": "1주차: Intro / overview?", "kind": "video",
            "video": {"asset_id": asset, "host": HOST, "listed_size": size, "updated_at": updated}}


class Commons:
    """Answers like lcms.skku.edu did in November 2026: a description, then a redirect to the file, which comes
    whole whatever Range says. `ranges=True` makes it honour Range, which the real one didn't. `fail` holds
    statuses for the next downloads; `hold` (an Event) keeps every download open after its first `first` bytes
    until it is set; `hang` makes a download send those bytes and then nothing; `cut` makes the next one end
    after those bytes, as when the connection drops."""

    def __init__(self, body=MP4, content_type="application/octet-stream", redirect_to=None, deleted=False,
                 ranges=False):
        self.body, self.content_type, self.deleted, self.ranges = body, content_type, deleted, ranges
        self.redirect_to = redirect_to or f"https://{HOST}/index.php?module=x&token=SECRET"
        self.downloads = 0
        self.order: list[str] = []  # the assets, in the order their files were asked for
        self.asked: list[str | None] = []  # the Range header of each file request
        self.seen: list[httpx.Request] = []
        self.fail: list[int] = []
        self.first = 1000
        self.hold: asyncio.Event | None = None
        self.hang = False
        self.cut = False
        self.open = 0  # downloads being sent right now
        self.peak = 0
        self.started = asyncio.Event()  # set when a held download has sent its first bytes

    async def _send(self, data: bytes):
        self.open += 1
        self.peak = max(self.peak, self.open)
        try:
            yield data[:self.first]
            self.started.set()
            if self.cut:
                self.cut = False
                return
            if self.hang:
                await asyncio.sleep(3600)
            if self.hold is not None:
                await self.hold.wait()
            yield data[self.first:]
        finally:
            self.open -= 1

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        if path.startswith("/CommonsCore2/v2/contents/"):
            aid = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"result": {
                "asset_id": aid, "type": "Movie", "extension": "mp4", "original_file_length": len(self.body),
                "is_original_file_deleted": self.deleted, "exists_caption": "N", "exists_script": "N",
                "download_url": f"https://{HOST}/CommonsCore2/v2/services/download/{aid}",
                "author": "Some Professor", "email": "prof@example.com"}})
        if path.startswith("/CommonsCore2/v2/services/download/"):
            if request.headers.get("referer") != "https://canvas.skku.edu/":
                return httpx.Response(500, text="500")
            self.order.append(path.rsplit("/", 1)[1])
            return httpx.Response(302, headers={"location": self.redirect_to})
        if path == "/index.php":
            self.downloads += 1
            self.asked.append(request.headers.get("range"))
            if self.fail:
                return httpx.Response(self.fail.pop(0), text="busy")
            status, start = 200, 0
            headers = {"content-type": self.content_type, "etag": '"one"'}
            if self.ranges and request.headers.get("range"):
                status, start = 206, int(request.headers["range"].removeprefix("bytes=").rstrip("-"))
                headers["content-range"] = f"bytes {start}-{len(self.body) - 1}/{len(self.body)}"
            data = self.body[start:]
            headers["content-length"] = str(len(data))
            return httpx.Response(status, content=self._send(data), headers=headers)
        return httpx.Response(404)


def videos(tmp_path, commons, **settings) -> Videos:
    """Calling it again over the same directory is a restart of the server."""
    config = Settings(data_dir=tmp_path, _env_file=None, **settings)
    return Videos(config, Cache(config, Store(config.db_path)), transport=httpx.MockTransport(commons))


async def finish(v: Videos, lec: dict, **how) -> dict:
    v.start(lec, **how)
    await v.drain()
    return v.status(lec)


async def until(condition, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "it did not happen"
        await asyncio.sleep(0.01)


def due_now(v: Videos) -> None:
    """Brings every waiting retry forward, as if its pause had passed."""
    past = (datetime.now(KST) - timedelta(seconds=1)).isoformat(timespec="microseconds")
    v.store.execute("UPDATE video_jobs SET not_before=? WHERE not_before IS NOT NULL", (past,))


def rows(v: Videos) -> list[dict]:
    return [dict(r) for r in v.store.query("SELECT * FROM video_jobs ORDER BY queued_at, id")]


async def test_download_is_cached_and_reused(tmp_path):
    commons = Commons()
    v = videos(tmp_path, commons)
    lec = lecture()
    assert v.status(lec)["state"] == "not_downloaded"
    status = await finish(v, lec)
    assert status["state"] == "ready" and status["format"] == "mp4" and status["captions"] == "N"
    path = v.path("a1b2c3")
    assert path.read_bytes() == MP4 and path.suffix == ".mp4" and oct(path.stat().st_mode & 0o777) == "0o600"
    meta = v.meta("a1b2c3")
    assert meta["name"] == "1주차 Intro overview.mp4"  # safe as a file name
    assert "SECRET" not in str(meta) and "prof@example.com" not in str(meta) and "Some Professor" not in str(meta)
    for request in commons.seen:  # never the iCampus session
        assert "cookie" not in request.headers and "authorization" not in request.headers
    assert v.start(lec)["state"] == "ready" and commons.downloads == 1  # no second download
    assert not list(tmp_path.glob("videos/*.part")) and rows(v) == []  # a finished job leaves nothing behind


async def test_outdated_copy_is_replaced(tmp_path):
    commons = Commons()
    v = videos(tmp_path, commons)
    await finish(v, lecture())
    newer = lecture(updated="20260401120000")
    assert v.status(newer)["state"] == "outdated"
    assert (await finish(v, newer))["state"] == "ready" and commons.downloads == 2


@pytest.mark.parametrize("commons, message", [
    (Commons(content_type="text/html; charset=utf-8", body=b"<html>login</html>"), "web page instead"),
    (Commons(redirect_to="https://evil.example/file.mp4"), "redirected away from SKKU"),
    (Commons(deleted=True), "no longer has"),
])
async def test_refusals(tmp_path, commons, message):
    v = videos(tmp_path, commons)
    status = await finish(v, lecture())
    assert status["state"] == "failed" and message in status["error"]
    assert status["attempts"] == 1 and status["retryable"] is False  # trying again couldn't change it: not retried
    assert v.path("a1b2c3") is None and not list(tmp_path.glob("videos/*.part"))
    assert "SECRET" not in status["error"] and "SECRET" not in str(rows(v))


async def test_size_caps(tmp_path):
    v = videos(tmp_path, Commons(), video_max_gb=1000 / 2**30)
    status = await finish(v, lecture())
    assert status["state"] == "failed" and "ICAMPUS_VIDEO_MAX_GB" in status["error"]

    class Unsized(Commons):  # a description without the original's size: only the iCampus listing is left
        def __call__(self, request):
            answer = super().__call__(request)
            return httpx.Response(200, json={"result": {}}) if "/contents/" in request.url.path else answer
    v = videos(tmp_path / "a", Unsized(), video_max_gb=5000 / 2**30)
    status = await finish(v, lecture(size=25_000))  # which says five times the file: that alone refuses nothing
    assert status["state"] == "ready" and v.path("a1b2c3").read_bytes() == MP4
    v = videos(tmp_path / "b", Commons(), video_cache_gb=1000 / 2**30)
    status = await finish(v, lecture())
    assert status["state"] == "failed" and "bigger than the whole video cache" in status["error"]


async def test_least_recently_used_video_goes_first(tmp_path):
    v = videos(tmp_path, Commons(), video_cache_gb=2.5 * len(MP4) / 2**30)
    first, second, third = lecture("aaa111", "1"), lecture("bbb222", "2"), lecture("ccc333", "3")
    await finish(v, first)
    await asyncio.sleep(0.01)
    await finish(v, second)
    await asyncio.sleep(0.01)
    v.touch("aaa111")  # watched again: now the most recent
    await finish(v, third)
    assert v.path("aaa111") and v.path("ccc333") and v.path("bbb222") is None
    on_disk = sum(p.stat().st_size for p in (tmp_path / "videos").iterdir())  # the videos and their sidecars
    assert v.used_bytes() == on_disk and 2 * len(MP4) < on_disk < 2.5 * len(MP4)


async def test_not_an_mp4_is_kept_but_labelled(tmp_path):
    v = videos(tmp_path, Commons(body=b"PK\x03\x04" + b"z" * 100))
    status = await finish(v, lecture(size=104))
    assert status["state"] == "ready" and status["format"].startswith("not mp4")
    assert v.path("a1b2c3").suffix == ".bin" and status["content_type"] == "application/octet-stream"


def seeded(tmp_path, lectures):
    from icampus.store import Record
    store = Store(tmp_path / "t.db")
    store.replace_scope("lectures", "course:1001", [Record(r["key"], "1001", None, r) for r in lectures],
                        datetime.now(KST))
    store.set_state("collect_version", 99)  # the rows are current: no start-up sync
    return store


def test_api_endpoints(tmp_path):
    from fastapi.testclient import TestClient

    from icampus.api import create_app

    plain = {"key": "canvas.skku.edu:1001:modules/items:7003", "course_id": "1001", "item_id": "7003",
             "title": "YouTube talk", "kind": "video"}
    store = seeded(tmp_path, [lecture(), plain])
    token, admin = "t" * 20, "a" * 20
    app = create_app(Settings(api_tokens=f"test:{token},admin:{admin}", data_dir=tmp_path, _env_file=None),
                     store=store, background=False)
    app.state.videos._transport = httpx.MockTransport(Commons())
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        assert c.get("/api/v1/videos/7002").json()["state"] == "not_downloaded"
        assert c.get("/api/v1/videos/7003").status_code == 404  # not on Commons
        assert c.get("/api/v1/videos/7002/content").status_code == 404
        assert c.post("/api/v1/videos/7002").status_code == 202
        for _ in range(100):
            if (state := c.get("/api/v1/videos/7002").json()["state"]) not in ("queued", "downloading"):
                break
            time.sleep(0.02)
        assert state == "ready"
        assert c.post("/api/v1/videos/7002").status_code == 200
        full = c.get("/api/v1/videos/7002/content")
        assert full.content == MP4 and full.headers["content-type"] == "video/mp4"
        assert "attachment" in full.headers["content-disposition"]
        part = c.get("/api/v1/videos/7002/content", headers={"Range": "bytes=4-7"})
        assert part.status_code == 206 and part.content == b"ftyp"
        listing = c.get("/api/v1/videos").json()
        assert listing["count"] == 1 and listing["items"][0]["item_id"] == "7002" and listing["queue"] == []
        assert c.delete("/api/v1/videos/7002").status_code == 403  # deleting from the cache takes an admin token
        assert c.delete("/api/v1/videos/7002", headers={"Authorization": f"Bearer {admin}"}).json() == {
            "deleted": True, "canceled": False}
        assert c.get("/api/v1/videos/7002").json()["state"] == "not_downloaded"
        assert c.get("/api/v1/videos/7002", headers={"Authorization": ""}).status_code == 401


def test_a_failed_export_is_reported_until_it_is_asked_for_again(tmp_path):
    """The call that follows a download must not start a failed one over: that hid every error."""
    from fastapi.testclient import TestClient

    from icampus.api import create_app

    token, admin = "t" * 20, "a" * 20
    app = create_app(Settings(api_tokens=f"test:{token},admin:{admin}", data_dir=tmp_path, _env_file=None),
                     store=seeded(tmp_path, [lecture()]), background=False)
    commons = Commons(content_type="text/html")
    app.state.videos._transport = httpx.MockTransport(commons)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        assert c.post("/api/v1/videos/7002").status_code == 202
        for _ in range(100):
            if (body := c.post("/api/v1/videos/7002")).json()["state"] == "failed":  # asking again, as a client does
                break
            time.sleep(0.02)
        assert body.status_code == 200 and "web page instead" in body.json()["error"] and commons.downloads == 1
        queue = c.get("/api/v1/videos").json()["queue"]
        assert [(j["item_id"], j["state"]) for j in queue] == [("7002", "failed")]
        exports = c.get("/api/v1/status").json()["exports"]
        assert (exports["failed"], exports["queued"], exports["downloading"], exports["ready"]) == (1, 0, 0, 0)
        assert exports["recently_failed"][0]["item_id"] == "7002" and "web page" in exports["recently_failed"][0]["error"]
        commons.content_type = "application/octet-stream"
        assert c.post("/api/v1/videos/7002?retry=true").status_code == 202
        for _ in range(100):
            if (state := c.get("/api/v1/videos/7002").json()["state"]) == "ready":
                break
            time.sleep(0.02)
        assert state == "ready" and commons.downloads == 2
        assert c.get("/api/v1/status").json()["exports"]["ready"] == 1
        # the admin token takes a download out of the queue with the same call that drops a copy
        gone = c.delete("/api/v1/videos/7002", headers={"Authorization": f"Bearer {admin}"}).json()
        assert gone == {"deleted": True, "canceled": False}


def test_a_batch_joins_the_queue_in_the_order_given(tmp_path):
    from fastapi.testclient import TestClient

    from icampus.api import create_app

    token = "t" * 20
    batch = [lecture(f"asset{n}", str(7010 + n)) for n in range(5)]
    app = create_app(Settings(api_tokens=f"test:{token}", data_dir=tmp_path, _env_file=None),
                     store=seeded(tmp_path, batch), background=False)
    commons = Commons()
    app.state.videos._transport = httpx.MockTransport(commons)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        wanted = ["7013", "7010", "7014", "7999", "7011", "7010"]  # an order of the caller's own; one unknown, one twice
        r = c.post("/api/v1/videos", json={"item_ids": wanted})
        assert r.status_code == 202 and r.json()["count"] == 4 and r.json()["not_found"] == ["7999"]
        assert [x["item_id"] for x in r.json()["items"]] == ["7013", "7010", "7014", "7011"]
        assert c.post("/api/v1/videos", json={"item_ids": []}).status_code == 422
        assert c.post("/api/v1/videos", json={"item_ids": ["../x"]}).status_code == 422
        assert c.post("/api/v1/videos", json={"item_ids": ["7010"]}, headers={"Authorization": ""}).status_code == 401
        for _ in range(200):
            again = c.post("/api/v1/videos", json={"item_ids": wanted}).json()  # asking again only reads
            if all(x["state"] == "ready" for x in again["items"]):
                break
            time.sleep(0.02)
        assert c.post("/api/v1/videos", json={"item_ids": wanted}).status_code == 200
    assert commons.order == ["asset3", "asset0", "asset4", "asset1"] and commons.downloads == 4


def test_the_status_shows_the_export_queue_and_a_fresh_worker_heartbeat(tmp_path):
    """The worker the app starts runs a queued export with nobody asking again, and says it is alive."""
    from fastapi.testclient import TestClient

    from icampus.api import create_app

    token = "t" * 20
    app = create_app(Settings(api_tokens=f"test:{token}", data_dir=tmp_path, sync_times="", _env_file=None),
                     store=seeded(tmp_path, [lecture()]), background=True)
    app.state.videos._transport = httpx.MockTransport(Commons())
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        worker = c.get("/api/v1/status").json()["exports"]["worker"]
        assert worker["running"] is True and worker["concurrency"] == 1
        assert datetime.now(KST) - datetime.fromisoformat(worker["heartbeat_at"]) < timedelta(minutes=1)
        assert c.post("/api/v1/videos/7002").status_code == 202
        for _ in range(200):
            if (exports := c.get("/api/v1/status").json()["exports"])["ready"] == 1:
                break
            time.sleep(0.02)
        assert (exports["ready"], exports["queued"], exports["downloading"], exports["failed"]) == (1, 0, 0, 0)
    assert app.state.videos.summary()["worker"]["running"] is False  # stopped with the server


async def test_a_refresh_keeps_the_copy_and_its_pin_until_the_new_one_has_arrived(tmp_path):
    commons = Commons()
    v = videos(tmp_path, commons)
    lec = lecture()
    await finish(v, lec)
    v.cache.pin("video", "a1b2c3")
    assert v.start(lec)["state"] == "ready" and commons.downloads == 1  # current: nothing to do
    commons.content_type = "text/html; charset=utf-8"  # the content server is having a bad day
    status = v.start(lec, refresh=True)
    assert status["state"] in ("queued", "downloading")
    await v.drain()
    again = v.status(lec)
    assert again["state"] == "ready" and "web page instead" in again["last_error"]  # the old copy is still here
    assert v.path("a1b2c3").read_bytes() == MP4 and v.cache.get("video", "a1b2c3").pinned
    commons.content_type = "application/octet-stream"
    v.start(lec, refresh=True)
    await v.drain()
    assert commons.downloads == 3 and v.status(lec)["state"] == "ready" and v.cache.get("video", "a1b2c3").pinned
    assert "last_error" not in v.status(lec)


# --- the queue -------------------------------------------------------------------------------------------------

async def test_a_batch_finishes_in_the_order_asked_with_nobody_asking_again(tmp_path):
    commons = Commons()
    v = videos(tmp_path, commons)
    worker = asyncio.create_task(v.run())
    batch = [lecture(f"asset{n:02d}", str(8000 + n)) for n in range(20)]
    states = [v.start(lec)["state"] for lec in batch]
    assert states == ["downloading"] + ["queued"] * 19  # asking only adds to the queue
    assert [v.status(lec).get("queue_position") for lec in batch[:4]] == [None, 1, 2, 3]
    assert v.status(batch[19])["queue_position"] == 19
    await until(lambda: len(v.cache.entries("video")) == 20)  # from here on nothing is asked for or polled
    assert commons.order == [lec["video"]["asset_id"] for lec in batch] and commons.peak == 1  # first in, first out
    assert rows(v) == [] and v.summary()["ready"] == 20
    worker.cancel()


async def test_two_at_a_time_when_the_setting_says_so(tmp_path):
    commons = Commons()
    commons.hold = asyncio.Event()
    v = videos(tmp_path, commons, download_concurrency=2)
    batch = [lecture(f"asset{n}", str(8000 + n)) for n in range(5)]
    assert [v.start(lec)["state"] for lec in batch] == ["downloading", "downloading", "queued", "queued", "queued"]
    await until(lambda: commons.open == 2)
    assert v.summary()["worker"]["concurrency"] == 2 and len(v.summary()["active"]) == 2
    commons.hold.set()
    await v.drain()
    assert commons.peak == 2 and commons.order[:2] == ["asset0", "asset1"] and len(v.cached()) == 5


async def test_a_restart_goes_on_by_itself_and_continues_what_had_arrived(tmp_path):
    body = MP4 * 50
    commons = Commons(body=body, ranges=True)
    commons.hold, commons.first = asyncio.Event(), 70_000
    first = videos(tmp_path, commons)
    a, b, c = lecture("aaa111", "1"), lecture("bbb222", "2"), lecture("ccc333", "3")
    for lec in (a, b, c):
        first.start(lec)
    await until(lambda: first.active["aaa111"].done == 70_000)  # the first video is part of the way
    await first.stop()  # the server stops
    assert first.status(a)["state"] == "queued" and (tmp_path / "videos" / "aaa111.part").stat().st_size == 70_000
    # a process that is killed can't even do that: its row still says downloading, with an older byte count
    first.store.execute("UPDATE video_jobs SET state='downloading', downloaded_bytes=1 WHERE asset_id='aaa111'")

    commons.hold = None
    second = videos(tmp_path, commons)  # the server comes back
    assert [(r["asset_id"], r["state"], r["downloaded_bytes"]) for r in rows(second)] == [
        ("aaa111", "queued", 70_000), ("bbb222", "queued", 0), ("ccc333", "queued", 0)]
    assert second.status(a)["queue_position"] == 1 and second.status(c)["queue_position"] == 3
    worker = asyncio.create_task(second.run())  # only the worker: nobody asks for anything
    await until(lambda: len(second.cache.entries("video")) == 3)
    assert commons.asked == [None, "bytes=70000-", None, None]  # the first one went on where it was
    if_range = [r.headers.get("if-range") for r in commons.seen if r.url.path == "/index.php"]
    assert if_range == [None, '"one"', None, None]  # and only if the file is still the same one
    assert commons.order == ["aaa111", "aaa111", "bbb222", "ccc333"]  # in its old place, before the others
    assert all(second.path(x).read_bytes() == body for x in ("aaa111", "bbb222", "ccc333"))
    assert not list(tmp_path.glob("videos/*.part")) and rows(second) == []
    worker.cancel()


async def test_a_restart_starts_over_and_says_so_when_the_content_server_ignores_range(tmp_path):
    """What lcms.skku.edu did in November 2026: a Range request gets the whole file, with a 200."""
    body = MP4 * 50
    commons = Commons(body=body)
    commons.hold, commons.first = asyncio.Event(), 70_000
    first = videos(tmp_path, commons)
    lec = lecture()
    first.start(lec)
    await until(lambda: first.active["a1b2c3"].done == 70_000)
    await first.stop()

    commons.first = 20_000
    second = videos(tmp_path, commons)
    assert second.status(lec)["state"] == "queued"
    worker = asyncio.create_task(second.run())
    await until(lambda: "a1b2c3" in second.active and second.active["a1b2c3"].done == 20_000)
    status = second.status(lec)
    assert status["state"] == "downloading" and "started over from zero" in status["note"]  # not a silent reset
    assert commons.asked == [None, "bytes=70000-"]
    commons.hold.set()
    await until(lambda: second.status(lec)["state"] == "ready")
    assert second.path("a1b2c3").read_bytes() == body and commons.downloads == 2  # one request, not a third
    worker.cancel()


async def test_what_is_on_disk_is_not_continued_for_another_version_or_after_a_reboot(tmp_path, monkeypatch):
    body = MP4 * 50
    commons = Commons(body=body, ranges=True)
    commons.hold, commons.first = asyncio.Event(), 70_000
    first = videos(tmp_path, commons)
    first.start(lecture())
    await until(lambda: first.active["a1b2c3"].done == 70_000)
    await first.stop()
    commons.hold = None

    monkeypatch.setattr(vm, "_BOOT", "another-boot")  # the machine restarted: the last bytes may never have landed
    second = videos(tmp_path, commons)
    await second.drain()
    assert commons.asked == [None, None] and second.path("a1b2c3").read_bytes() == body

    commons.hold = asyncio.Event()
    newer = lecture(updated="20260401120000")
    second.start(newer)
    await until(lambda: "a1b2c3" in second.active and second.active["a1b2c3"].done == 70_000)
    await second.stop()
    commons.hold = None
    third = videos(tmp_path, commons)
    third.start(lecture(updated="20260501120000"))  # a sync saw yet another version while it waited
    await third.drain()
    assert commons.asked[2:] == [None, None]  # the part belonged to the version before: not continued


async def test_a_part_without_a_job_is_removed_at_start(tmp_path):
    commons = Commons()
    commons.hold = asyncio.Event()
    first = videos(tmp_path, commons)
    first.start(lecture())
    await until(lambda: commons.open == 1)
    await first.stop()
    (tmp_path / "videos" / "orphan.part").write_bytes(b"left by an older version")
    videos(tmp_path, commons)
    assert sorted(p.name for p in tmp_path.glob("videos/*.part")) == ["a1b2c3.part"]


async def test_a_stalled_download_is_tried_again_and_fails_where_it_can_be_seen(tmp_path, monkeypatch):
    assert vm.STALL_S + vm.RETRY_AFTER_S[0] <= 120  # no data for a minute: the next try is within two
    monkeypatch.setattr(vm, "STALL_S", 0.05)
    commons = Commons()
    commons.hang = True
    v = videos(tmp_path, commons, export_retries=2)
    lec, behind = lecture(), lecture("bbb222", "7005")
    v.start(lec)
    v.start(behind)
    await until(lambda: commons.open == 1)
    assert v.status(lec)["state"] == "downloading" and v.status(behind)["queue_position"] == 1
    commons.hang = False
    await v.drain()  # the first try ends after 0.05 s without data; the one behind it isn't held up
    status = v.status(lec)
    assert status["state"] == "queued" and status["attempts"] == 1 and "sent nothing for 0.05 seconds" in status["last_error"]
    wait = datetime.fromisoformat(status["retry_at"]) - datetime.now(KST)
    assert timedelta(seconds=20) < wait <= timedelta(seconds=30)
    assert v.status(behind)["state"] == "ready" and commons.downloads == 2

    commons.hang = True
    for tries in (2, 3):
        due_now(v)
        await v.drain()
        assert commons.downloads == tries + 1
    status = v.status(lec)
    assert status["state"] == "failed" and status["attempts"] == 3 and status["retryable"] is True
    assert "sent nothing" in status["error"] and "gave up after 3 tries" in status["error"]
    assert status["failed_at"] and not list(tmp_path.glob("videos/*.part"))
    assert v.summary()["failed"] == 1 and v.summary()["recently_failed"][0]["item_id"] == "7002"

    assert v.start(lec)["state"] == "failed" and v.start(lec)["error"] == status["error"]  # asking is not retrying
    await v.drain()
    assert commons.downloads == 4
    commons.hang = False
    assert v.start(lec, retry=True)["state"] == "downloading"
    await v.drain()
    assert v.status(lec)["state"] == "ready" and commons.downloads == 5


async def test_the_worker_makes_the_next_try_by_itself_when_the_pause_is_over(tmp_path, monkeypatch):
    monkeypatch.setattr(vm, "RETRY_AFTER_S", (0.05, 0.05))
    monkeypatch.setattr(vm, "TICK_S", 0.02)
    commons = Commons()
    commons.fail = [503, 429]
    v = videos(tmp_path, commons)
    worker = asyncio.create_task(v.run())
    lec = lecture()
    v.start(lec)
    await until(lambda: len(v.cache.entries("video")) == 1)  # nobody asked again
    assert commons.downloads == 3 and v.status(lec)["state"] == "ready"
    worker.cancel()


async def test_a_download_cut_short_keeps_what_arrived_for_the_next_try(tmp_path):
    body = MP4 * 50
    commons = Commons(body=body, ranges=True)
    commons.cut, commons.first = True, 70_000
    v = videos(tmp_path, commons)
    lec = lecture()
    status = await finish(v, lec)
    assert status["state"] == "queued" and "cut short" in status["last_error"] and status["attempts"] == 1
    assert (tmp_path / "videos" / "a1b2c3.part").stat().st_size == 70_000 and rows(v)[0]["downloaded_bytes"] == 70_000
    due_now(v)
    await v.drain()
    assert commons.asked == [None, "bytes=70000-"] and v.status(lec)["state"] == "ready"
    assert v.path("a1b2c3").read_bytes() == body


async def test_a_job_that_failed_goes_to_the_back_when_it_is_asked_for_again(tmp_path):
    commons = Commons(deleted=True)
    v = videos(tmp_path, commons)
    a, b, c = lecture("aaa111", "1"), lecture("bbb222", "2"), lecture("ccc333", "3")
    assert (await finish(v, a))["state"] == "failed"
    commons.deleted, commons.hold = False, asyncio.Event()
    v.start(b)
    v.start(c)
    assert v.start(a, retry=True)["queue_position"] == 2  # behind the one that waits, not ahead of it
    assert [j["item_id"] for j in v.queue()] == ["2", "3", "1"]
    commons.hold.set()
    await v.drain()
    assert commons.order == ["bbb222", "ccc333", "aaa111"]


async def test_cancel_takes_a_download_out_of_the_queue(tmp_path):
    commons = Commons()
    commons.hold = asyncio.Event()
    v = videos(tmp_path, commons)
    a, b, c = lecture("aaa111", "1"), lecture("bbb222", "2"), lecture("ccc333", "3")
    for lec in (a, b, c):
        v.start(lec)
    await until(lambda: commons.open == 1)
    assert v.cancel("bbb222") and v.status(b)["state"] == "not_downloaded"  # a waiting one
    assert v.cancel("aaa111") and not v.cancel("nope")  # the running one
    await until(lambda: commons.order == ["aaa111", "ccc333"])  # the next one starts by itself
    commons.hold.set()
    await v.drain()
    assert v.status(a)["state"] == "not_downloaded" and v.status(c)["state"] == "ready"
    assert not list(tmp_path.glob("videos/*.part")) and rows(v) == []


async def test_status_of_a_running_download(tmp_path):
    body = b"\0\0\0\x18ftypmp42" + b"x" * (3 * MB)
    commons = Commons(body=body)
    commons.hold, commons.first = asyncio.Event(), 2 * MB
    v = videos(tmp_path, commons)
    lec, behind = lecture(size=15 * MB), lecture("bbb222", "7005", size=15 * MB)
    assert v.status(lec) == {"item_id": "7002", "title": "1주차: Intro / overview?", "state": "not_downloaded",
                             "listed_size_mb_estimate": 15}  # what iCampus lists: several times the real file
    v.start(lec)
    v.start(behind)
    await until(lambda: v.active["a1b2c3"].done == 2 * MB)
    status = v.status(lec)
    assert (status["state"], status["downloaded_mb"], status["total_mb"], status["attempts"]) == ("downloading", 2, 3, 1)
    assert "listed_size_mb_estimate" not in status  # the real size is known now
    assert {"speed_mbps", "eta_s", "started_at", "queued_at"} <= status.keys() and "stalled" not in status
    assert datetime.now(KST) - datetime.fromisoformat(status["last_progress_at"]) < timedelta(seconds=5)
    waiting = v.status(behind)
    assert (waiting["state"], waiting["queue_position"], waiting["total_mb"]) == ("queued", 1, None)
    assert waiting["listed_size_mb_estimate"] == 15 and waiting["attempts"] == 0
    summary = v.summary()
    assert (summary["queued"], summary["downloading"], summary["next"]) == (1, 1, ["7005"])
    assert summary["active"][0]["item_id"] == "7002" and summary["active"][0]["total_mb"] == 3
    stored = str(rows(v))
    assert "SECRET" not in stored and "prof@example.com" not in stored and "Some Professor" not in stored
    commons.hold.set()
    await v.drain()
    done = v.status(lec)
    assert done["state"] == "ready" and done["size_mb"] == 3 and "listed_size_mb_estimate" not in done


def test_speed_and_eta_fall_while_nothing_arrives(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(vm.time, "monotonic", lambda: clock[0])
    live = vm.Running()
    live.restart(0, 10 * MB)
    assert live.speed() is None  # too early to say
    clock[0] += 2
    assert live.arrived(2 * MB) and live.speed() == MB  # a megabyte a second
    clock[0] += 2
    assert live.speed() == MB / 2  # nothing came for two seconds
    for _ in range(40):  # then a steady megabyte a second: only the last seconds count
        clock[0] += 1
        live.arrived(MB)
    assert live.speed() == pytest.approx(MB) and len(live.samples) <= vm.SPEED_WINDOW_S + 1


async def test_a_caller_that_retries_by_itself_gets_the_first_failure(tmp_path):
    """A transcription job's download: the transcription queue decides about trying again."""
    commons = Commons()
    commons.fail = [503, 503]
    v = videos(tmp_path, commons)
    lec = lecture()
    with pytest.raises(VideoError, match="HTTP 503") as failure:
        await v.ensure(lec)
    assert failure.value.transient and v.status(lec)["state"] == "failed" and commons.downloads == 1
    assert v.start(lec)["state"] == "failed"  # an export that only asks doesn't restart it either
    with pytest.raises(VideoError, match="HTTP 503"):
        await v.ensure(lec)  # the caller's retry: a new try at once
    assert (await v.ensure(lec)).read_bytes() == MP4 and commons.downloads == 3

    other = lecture("bbb222", "7005")
    commons.hold = asyncio.Event()
    blocker = lecture("ccc333", "7006")
    v.start(blocker)  # keeps the one slot busy, so the next two requests meet in the queue
    await until(lambda: commons.open == 1)
    commons.fail = [503]
    waiting = asyncio.create_task(v.ensure(other))
    await asyncio.sleep(0)
    assert v.start(other)["state"] == "queued" and rows(v)[1]["keep_trying"] == 1  # exported too: it keeps trying
    commons.hold.set()
    with pytest.raises(VideoError, match="HTTP 503") as failure:
        await waiting
    assert failure.value.transient and v.status(other)["state"] == "queued" and v.status(other)["attempts"] == 1
