"""The cache manager: one index over videos, audio, attachments and transcripts; caps per kind and in total;
least-recently-used eviction that leaves pinned and in-use entries alone; adopting files from before the index."""

import json
import time
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from icampus import files
from icampus.api import create_app
from icampus.cache import Cache, CacheFull
from icampus.config import KST, Settings
from icampus.runtime import Runtime
from icampus.store import Record, Store
from icampus.videos import VideoError, Videos

MB = 2**20
TOKEN, ADMIN = "t" * 20, "a" * 20


def make(tmp_path, **settings) -> tuple[Cache, Runtime]:
    env = Settings(data_dir=tmp_path, _env_file=None, **settings)
    runtime = Runtime(env, Store(env.db_path))
    return Cache(runtime, runtime._store), runtime


def add(cache: Cache, kind: str, key: str, size: int, **fields):
    folder = cache.root / kind
    folder.mkdir(exist_ok=True)
    path = folder / f"{key}.bin"
    path.write_bytes(b"x" * size)
    time.sleep(0.002)  # distinct last_used stamps
    return cache.put(kind, key, [path], **fields)


def keys(cache: Cache, kind: str | None = None) -> set[str]:
    return {e.key for e in cache.entries(kind)}


def test_put_list_touch_and_totals(tmp_path):
    cache, _ = make(tmp_path)
    add(cache, "video", "v1", 3000, course_id="1001", item_id="7002", title="Week 1")
    add(cache, "attachment", "f1", 500, course_id="1002", title="notes.pdf")
    add(cache, "transcript", "t1", 100, course_id="1001")
    assert [e.key for e in cache.entries()] == ["t1", "f1", "v1"]  # most recently used first
    assert keys(cache, "video") == {"v1"} and {e.key for e in cache.entries(course_ids={"1001"})} == {"v1", "t1"}
    cache.touch("video", "v1")
    assert cache.entries()[0].key == "v1"
    totals = cache.totals()
    assert totals["video"]["bytes"] == 3000 and totals["all"]["bytes"] == 3600 and totals["all"]["count"] == 3
    assert totals["video"]["cap_bytes"] == 30 * 2**30 and totals["transcript"]["cap_bytes"] is None
    with pytest.raises(ValueError):
        cache.put("video", "outside", [tmp_path.parent / "elsewhere.bin"])  # only files under the data directory
    with pytest.raises(ValueError):
        cache.put("mystery", "k", [])


def test_eviction_is_least_recently_used_and_skips_pinned_and_in_use(tmp_path):
    cache, _ = make(tmp_path, video_cache_gb=3.5 * MB / 2**30)
    for key in ("old", "pinned", "needed", "newer"):
        add(cache, "video", key, MB // 2)
    cache.pin("video", "pinned")
    cache.protect(lambda: {("video", "needed")})  # a queued transcription still needs this one
    cache.make_room("video", 2 * MB)  # 2 MB used + 2 MB needed > 3.5 MB: the oldest unprotected one goes
    assert keys(cache) == {"pinned", "needed", "newer"}
    cache.make_room("video", 5 * MB // 2)
    assert keys(cache) == {"pinned", "needed"}  # "newer" went too; the protected two stay whatever it takes
    with pytest.raises(CacheFull, match="pinned or waiting for a job"):
        cache.make_room("video", 3 * MB)
    assert keys(cache) == {"pinned", "needed"}
    assert cache.state(cache.get("video", "pinned")) == "pinned"
    assert cache.state(cache.get("video", "needed")) == "in use"
    totals = cache.totals()["video"]
    assert totals["pinned_bytes"] == MB // 2 and totals["in_use_bytes"] == MB // 2
    with pytest.raises(CacheFull, match="more than the whole video cache"):
        cache.make_room("video", 4 * MB)
    cache.pin("video", "pinned", False)
    cache.make_room("video", 3 * MB)
    assert keys(cache) == {"needed"}


def test_the_total_cap_spans_kinds_but_never_takes_transcripts(tmp_path):
    cache, runtime = make(tmp_path, cache_total_gb=0.5)
    runtime._overrides["cache_total_gb"] = 2.2 * MB / 2**30  # below the UI's minimum: only to keep the test small
    add(cache, "transcript", "t1", MB)
    add(cache, "audio", "a1", MB // 2)
    add(cache, "video", "v1", MB // 2)
    add(cache, "attachment", "f1", MB // 2)
    cache.enforce()
    assert keys(cache) == {"t1", "v1", "f1"}  # the oldest evictable entry went, the older transcript did not
    assert cache.state(cache.get("transcript", "t1")) == "kept"
    with pytest.raises(CacheFull, match="cache_total_gb"):
        cache.make_room("video", 2 * MB)  # even with everything evictable gone, the transcript leaves 1.2 MB
    assert keys(cache) == {"t1"}


def test_lowering_a_cap_applies_at_once_and_delete_removes_the_files(tmp_path):
    cache, runtime = make(tmp_path)
    runtime.on_change(lambda key: cache.enforce())
    first, second = add(cache, "audio", "a1", MB), add(cache, "audio", "a2", MB)
    runtime._overrides["audio_cache_gb"] = 1.5 * MB / 2**30
    cache.enforce()
    assert keys(cache) == {"a2"} and not cache.path(first).exists() and cache.path(second).exists()
    assert cache.delete("audio", "a2") and not cache.path(second).exists() and not cache.delete("audio", "a2")


def test_rows_follow_the_disk(tmp_path):
    cache, _ = make(tmp_path)
    entry = add(cache, "audio", "a1", 100)
    cache.path(entry).write_bytes(b"y" * 250)
    gone = add(cache, "audio", "a2", 100)
    cache.path(gone).unlink()
    cache.reconcile()
    assert keys(cache) == {"a1"} and cache.get("audio", "a1").size == 250
    assert cache.get("audio", "a2") is None


class Commons:
    def __init__(self, body: bytes):
        self.body, self.downloads = body, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/CommonsCore2/v2/contents/"):
            return httpx.Response(200, json={"result": {"original_file_length": len(self.body), "extension": "mp4"}})
        self.downloads += 1
        return httpx.Response(200, content=self.body)


def lecture(asset: str, item: str) -> dict:
    return {"key": f"canvas.skku.edu:1001:modules/items:{item}", "course_id": "1001", "item_id": item,
            "title": f"Lecture {item}", "kind": "video",
            "video": {"asset_id": asset, "host": "lcms.skku.edu", "updated_at": "20260301120000"}}


async def test_a_video_a_job_still_needs_is_not_evicted_for_a_new_download(tmp_path):
    body = b"\0\0\0\x18ftypmp42" + b"x" * 4000
    env = Settings(data_dir=tmp_path, video_cache_gb=1.6 * len(body) / 2**30, _env_file=None)
    cache = Cache(env, Store(env.db_path))
    videos = Videos(env, cache, transport=httpx.MockTransport(Commons(body)))
    await videos.ensure(lecture("aaa111", "1"))
    cache.protect(lambda: {("video", "aaa111")})  # its transcription is still queued
    with pytest.raises(VideoError, match="pinned or waiting for a job") as failure:
        await videos.ensure(lecture("bbb222", "2"))
    assert failure.value.transient and videos.path("aaa111") and videos.path("bbb222") is None
    cache.protect(set)  # the job finished
    assert (await videos.ensure(lecture("bbb222", "2"))).read_bytes() == body
    assert videos.path("aaa111") is None


def test_files_from_before_the_index_are_adopted_without_downloading(tmp_path):
    """An upgrade finds videos and attachments with their old sidecars, and no rows."""
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "a1b2c3.mp4").write_bytes(b"\0\0\0\x18ftypmp42" + b"x" * 100)
    (tmp_path / "videos" / "a1b2c3.json").write_text(json.dumps({
        "asset_id": "a1b2c3", "file": "a1b2c3.mp4", "size": 112, "item_id": "7002", "course_id": "1001",
        "title": "Week 1", "name": "Week 1.mp4", "format": "mp4", "content_type": "video/mp4",
        "synced_updated_at": "20260301120000", "downloaded_at": "2026-11-01T10:00:00+09:00",
        "last_used": "2026-11-02T10:00:00+09:00"}))
    (tmp_path / "videos" / "orphan.json").write_text(json.dumps({"asset_id": "orphan", "file": "orphan.mp4"}))
    (tmp_path / "videos" / "broken.json").write_text("{not json")
    (tmp_path / "files").mkdir()
    for suffix, content in ((".bin", b"hello"), (".txt", b"hello"), (".json", json.dumps({
            "id": "701", "name": "notes.txt", "content_type": "text/plain", "size": 5, "extract": "text",
            "fetched_at": "2026-11-01T09:00:00+09:00", "synced_updated_at": None}).encode())):
        (tmp_path / "files" / f"701{suffix}").write_bytes(content)

    store = Store(tmp_path / "icampus.db")
    announcement = {"key": "k", "course_id": "1002", "id": "8001", "title": "Exam notice",
                    "attachments": [{"id": "701", "name": "notes.txt", "via": "attached"}]}
    store.replace_scope("announcements", "term:x", [Record("k", "1002", None, announcement)], datetime.now(KST))
    lec = {**lecture("a1b2c3", "7002"), "key": "canvas.skku.edu:1001:modules/items:7002"}
    store.replace_scope("lectures", "course:1001", [Record(lec["key"], "1001", None, lec)], datetime.now(KST))
    app = create_app(Settings(api_tokens=f"test:{TOKEN}", data_dir=tmp_path, _env_file=None), store=store,
                     background=False)
    refuse = httpx.MockTransport(lambda request: pytest.fail("the upgrade must not download anything"))
    app.state.videos._transport = refuse
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        listing = c.get("/api/v1/cache").json()
        by_kind = {i["kind"]: i for i in listing["items"]}
        assert listing["count"] == 2 and set(by_kind) == {"video", "attachment"}
        assert by_kind["video"]["item_id"] == "7002" and by_kind["video"]["last_used"].startswith("2026-11-02")
        assert by_kind["attachment"]["course_id"] == "1002" and by_kind["attachment"]["title"] == "notes.txt"
        assert c.get("/api/v1/videos/7002").json()["state"] == "ready"  # reused, not downloaded again
        assert c.get("/api/v1/videos/7002/content").content.startswith(b"\0\0\0\x18ftyp")
    assert (tmp_path / "videos" / "a1b2c3.json").exists()  # the sidecar stays: an older version still reads it


@pytest.fixture
def client(tmp_path, monkeypatch):
    def no_icampus(*args, **kwargs):
        pytest.fail("looking at the cache must never open an iCampus session")
    monkeypatch.setattr(files, "open_session", no_icampus)
    store = Store(tmp_path / "t.db")
    courses = [{"id": "1001", "name": "Data Structures", "code": "CSE1001"},
               {"id": "1002", "name": "Linear Algebra", "code": "MTH2002"}]
    store.replace_scope("courses", "term:x", [Record(c["id"], c["id"], None, c) for c in courses], datetime.now(KST))
    app = create_app(Settings(api_tokens=f"test:{TOKEN},admin:{ADMIN}", data_dir=tmp_path, _env_file=None),
                     store=store, background=False)
    app.state.videos._transport = httpx.MockTransport(
        lambda request: pytest.fail("looking at the cache must never ask the content server"))
    cache = app.state.cache
    add(cache, "video", "v1", 3 * MB, course_id="1001", item_id="7002", title="Week 1")
    add(cache, "audio", "v1.opus24", MB, course_id="1001", item_id="7002", title="Week 1")
    add(cache, "attachment", "701", 2 * MB, course_id="1002", title="notes.pdf")
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


def test_cache_routes(client):
    body = client.get("/api/v1/cache").json()
    assert body["count"] == 3 and [i["kind"] for i in body["items"]] == ["attachment", "audio", "video"]
    assert body["totals"]["video"] == {"count": 1, "used_gb": 0.0, "cap_gb": 30.0, "pinned_gb": 0.0, "in_use_gb": 0.0}
    assert body["totals"]["all"]["count"] == 3 and body["totals"]["transcript"]["cap_gb"] is None
    assert body["items"][0]["course"] == "Linear Algebra" and body["items"][0]["size_mb"] == 2.0
    assert [i["key"] for i in client.get("/api/v1/cache?kind=video").json()["items"]] == ["v1"]
    assert client.get("/api/v1/cache?course=cse1001").json()["count"] == 2
    assert [i["kind"] for i in client.get("/api/v1/cache?sort=size&order=desc").json()["items"]][0] == "video"
    assert client.get("/api/v1/cache?kind=cookies").status_code == 422
    assert client.get("/api/v1/cache", headers={"Authorization": ""}).status_code == 401

    assert client.post("/api/v1/cache/video/v1/pin").json() == {"kind": "video", "key": "v1", "pinned": True}
    pinned = client.get("/api/v1/cache?pinned=true").json()
    assert [(i["key"], i["state"]) for i in pinned["items"]] == [("v1", "pinned")]
    assert client.delete("/api/v1/cache/video/v1/pin").json()["pinned"] is False
    assert client.post("/api/v1/cache/video/nope/pin").status_code == 404
    assert client.post("/api/v1/cache/video/..%2Fx/pin").status_code in (404, 422)

    assert client.delete("/api/v1/cache/attachment/701").status_code == 403  # the mcp token can't delete
    admin = {"Authorization": f"Bearer {ADMIN}"}
    assert client.delete("/api/v1/cache/attachment/701", headers=admin).json()["deleted"] is True
    assert client.delete("/api/v1/cache/attachment/701", headers=admin).status_code == 404
    assert client.get("/api/v1/cache").json()["count"] == 2


def test_changing_a_cap_through_the_api_takes_effect_without_a_restart(client):
    admin = {"Authorization": f"Bearer {ADMIN}"}
    assert client.get("/api/v1/settings").status_code == 403
    assert client.put("/api/v1/settings", json={"video_cache_gb": 0.1}).status_code == 403
    client.post("/api/v1/cache/audio/v1.opus24/pin")
    r = client.put("/api/v1/settings", headers=admin, json={"audio_cache_gb": 0.05, "video_cache_gb": 0.1})
    assert r.status_code == 200 and {i["key"]: i["source"] for i in r.json()["items"]} == {
        "audio_cache_gb": "ui", "video_cache_gb": "ui"}
    assert client.get("/api/v1/cache").json()["totals"]["video"]["cap_gb"] == 0.1
    listed = {i["key"]: i for i in client.get("/api/v1/settings", headers=admin).json()["items"]}
    assert listed["video_cache_gb"]["value"] == 0.1 and listed["video_cache_gb"]["default"] == 30
    assert listed["attachment_max_mb"]["source"] == "default"
    bad = client.put("/api/v1/settings", headers=admin, json={"video_cache_gb": -1, "api_tokens": "x:y"})
    assert bad.status_code == 422 and set(bad.json()["details"]) == {"video_cache_gb", "api_tokens"}
    assert client.put("/api/v1/settings", headers=admin, json={"video_cache_gb": None}).status_code == 200
    assert client.get("/api/v1/cache").json()["totals"]["video"]["cap_gb"] == 30.0
