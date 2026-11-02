"""Lecture video downloads from a fake Commons server: Referer and no cookies, redirects kept on SKKU hosts,
web pages refused, size caps, the cache (reuse, outdated copies, least recently used out first)."""

import asyncio

import httpx
import pytest

from icampus.config import Settings
from icampus.videos import Videos

MP4 = b"\0\0\0\x18ftypmp42" + b"x" * 4000
HOST = "lcms.skku.edu"


def lecture(asset="a1b2c3", item="7002", updated="20260301120000", size=len(MP4)):
    return {"key": f"canvas.skku.edu:1001:modules/items:{item}", "course_id": "1001", "item_id": item,
            "title": "1주차: Intro / overview?", "kind": "video",
            "video": {"asset_id": asset, "host": HOST, "listed_size": size, "updated_at": updated}}


class Commons:
    """Answers like lcms.skku.edu did in November 2026."""

    def __init__(self, body=MP4, content_type="application/octet-stream", redirect_to=None, deleted=False):
        self.body, self.content_type, self.deleted = body, content_type, deleted
        self.redirect_to = redirect_to or f"https://{HOST}/index.php?module=x&token=SECRET"
        self.downloads = 0
        self.seen: list[httpx.Request] = []

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
            return httpx.Response(302, headers={"location": self.redirect_to})
        if path == "/index.php":
            self.downloads += 1
            return httpx.Response(200, content=self.body, headers={"content-type": self.content_type})
        return httpx.Response(404)


def videos(tmp_path, commons, **settings) -> Videos:
    return Videos(Settings(data_dir=tmp_path, _env_file=None, **settings), transport=httpx.MockTransport(commons))


async def finish(v: Videos, lec: dict) -> dict:
    status = v.start(lec)
    job = v.jobs.get(lec["video"]["asset_id"])
    if job and job.task:
        await job.task
    return v.status(lec) if status["state"] != "ready" else status


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
    assert not list(tmp_path.glob("videos/*.part"))


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
    assert v.path("a1b2c3") is None and not list(tmp_path.glob("videos/*.part"))
    assert "SECRET" not in status["error"]


async def test_size_caps(tmp_path):
    v = videos(tmp_path, Commons(), video_max_gb=1000 / 2**30)
    status = await finish(v, lecture())
    assert status["state"] == "failed" and "ICAMPUS_VIDEO_MAX_GB" in status["error"]
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
    assert v.used_bytes() == 2 * len(MP4)


async def test_not_an_mp4_is_kept_but_labelled(tmp_path):
    v = videos(tmp_path, Commons(body=b"PK\x03\x04" + b"z" * 100))
    status = await finish(v, lecture(size=104))
    assert status["state"] == "ready" and status["format"].startswith("not mp4")
    assert v.path("a1b2c3").suffix == ".bin" and status["content_type"] == "application/octet-stream"


def test_api_endpoints(tmp_path):
    import time

    from fastapi.testclient import TestClient

    from icampus.api import create_app
    from icampus.store import Record, Store
    from icampus.config import KST
    from datetime import datetime

    store = Store(tmp_path / "t.db")
    lec = lecture()
    plain = {"key": "canvas.skku.edu:1001:modules/items:7003", "course_id": "1001", "item_id": "7003",
             "title": "YouTube talk", "kind": "video"}
    store.replace_scope("lectures", "course:1001", [Record(r["key"], "1001", None, r) for r in (lec, plain)],
                        datetime.now(KST))
    token = "t" * 20
    app = create_app(Settings(api_tokens=f"test:{token}", data_dir=tmp_path, _env_file=None), store=store,
                     background=False)
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
        assert listing["count"] == 1 and listing["items"][0]["item_id"] == "7002"
        assert c.delete("/api/v1/videos/7002").json() == {"deleted": True}
        assert c.get("/api/v1/videos/7002").json()["state"] == "not_downloaded"
        assert c.get("/api/v1/videos/7002", headers={"Authorization": ""}).status_code == 401
