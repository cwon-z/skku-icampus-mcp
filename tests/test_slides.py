"""Slide frames: grouping by perceptual hash, extraction with real ffmpeg, the pipeline cutting them before a
video leaves, the routes and the MCP tool that pair each slide with what was said, and the read-only check of
whether lecture materials sit on the content server."""

import json
import shutil
import subprocess

import httpx
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError

import icampus.mcp_server as server
from helpers import ADMIN, SECRET, TOKEN, WHISPER, Commons, Stt, fake_audio, lecture, seed, settings
from icampus import audio, frames
from icampus.api import create_app
from icampus.store import Store
from icampus.stt import Segment, Transcript
from icampus.video_check import _describe, _result, describe_materials, material_report

AID = "0123456789abcdef0123456789abcdef"  # made up
MCP = {"Authorization": f"Bearer {TOKEN}"}


# --- grouping ------------------------------------------------------------------------------------

def picture(fn) -> bytes:
    """A 16x16 grey fingerprint from brightness(row, col)."""
    return bytes(max(0, min(255, int(fn(r, c)))) for r in range(16) for c in range(16))


WHITE = picture(lambda r, c: 245)
TITLE = picture(lambda r, c: 60 if r == 2 and 2 <= c <= 11 else 245)  # a title on a white slide
OTHER_TITLE = picture(lambda r, c: 60 if r == 2 and 4 <= c <= 13 else 245)
BULLET = picture(lambda r, c: 60 if (r == 2 and 2 <= c <= 11) else 160 if (r == 6 and 3 <= c <= 8) else 245)  # + one thin line
CURSOR = bytes(b - 40 if i == 200 else b for i, b in enumerate(TITLE))
DIAGRAM = picture(lambda r, c: 245 if (r + c) % 5 else 30)


def test_fingerprints_separate_slides_and_ignore_small_changes():
    assert frames.difference(TITLE, TITLE) == 0
    assert frames.difference(TITLE, CURSOR) < 0.3 < frames.SAME_SLIDE  # a cursor is the same slide
    assert frames.difference(TITLE, BULLET) < frames.SAME_SLIDE  # so is one more line on it
    assert frames.difference(TITLE, DIAGRAM) > frames.SAME_SLIDE  # another slide is not
    assert frames.difference(WHITE, picture(lambda r, c: 10)) > 200  # nor is black for white
    top, bottom = picture(lambda r, c: 200 if r < 5 else 40), picture(lambda r, c: 200 if r > 10 else 40)
    assert frames.difference(top, bottom) > 50  # bands a horizontal difference hash could not tell apart
    assert frames.difference(TITLE, OTHER_TITLE) > frames.difference(TITLE, CURSOR) * 10


def test_frames_that_look_alike_are_one_slide_and_the_last_picture_is_kept():
    slides = frames.group([0.0, 30.0, 45.0, 60.0, 200.0, 300.0], [TITLE, CURSOR, BULLET, DIAGRAM, WHITE, TITLE], 400.0)
    assert slides == [{"start": 0.0, "frame": 2, "end": 60.0},  # title, cursor, one bullet: one slide, the fullest kept
                      {"start": 60.0, "frame": 3, "end": 200.0}, {"start": 200.0, "frame": 4, "end": 300.0},
                      {"start": 300.0, "frame": 5, "end": 400.0}]  # the first slide shown again is a new stretch
    assert frames.group([], [], 10.0) == []
    assert frames.group([0.0], [TITLE], 0.0) == [{"start": 0.0, "frame": 0, "end": 0.0}]


def test_a_camera_recording_does_not_become_a_thousand_slides():
    noise = [picture(lambda r, c, n=n: (r * 37 + c * 91 + n * 53) % 256) for n in range(1000)]  # never the same
    slides = frames.group([float(n) for n in range(1000)], noise, 1000.0, most=50)
    assert 1 <= len(slides) <= 50 and slides[0]["start"] == 0.0 and slides[-1]["end"] == 1000.0
    assert all(a["end"] == b["start"] for a, b in zip(slides, slides[1:]))


@pytest.fixture(scope="module")
def slide_video(tmp_path_factory):
    """30 seconds: one pattern for 8 s, a second for 12 s (a small white box appears on it halfway), a third
    for 10 s."""
    if not audio.available():
        pytest.skip("ffmpeg is not installed")
    path = tmp_path_factory.mktemp("media") / "slides.mp4"
    box = "drawbox=x=40:y=40:w=30:h=20:color=white:t=fill"
    subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "smptebars=s=640x360:r=10:d=8",
                    "-f", "lavfi", "-i", "rgbtestsrc=s=640x360:r=10:d=6",
                    "-f", "lavfi", "-i", f"rgbtestsrc=s=640x360:r=10:d=6,{box}",
                    "-f", "lavfi", "-i", "yuvtestsrc=s=640x360:r=10:d=10", "-f", "lavfi", "-i", "sine=f=300:d=30",
                    "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]", "-map", "[v]", "-map", "4:a",
                    "-c:v", "libx264", "-g", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True)
    return path


@pytest.mark.parametrize("threshold", [0.1, 0.001])
async def test_extract_with_real_ffmpeg(slide_video, tmp_path, threshold):
    out = tmp_path / "frames" / "7001"
    out.mkdir(parents=True)
    (out / "stale.jpg").write_bytes(b"from an older version")
    found = await frames.extract(slide_video, out, duration=30.0, threshold=threshold, width=320,
                                 about={"item_id": "7001", "asset_id": "asset7001"})
    assert [(s["n"], s["start"], s["end"], s["file"]) for s in found] == [
        (1, 0.0, 8.0, "001.jpg"), (2, 8.0, 20.0, "002.jpg"), (3, 20.0, 30.0, "003.jpg")]
    # at the low threshold the frame with the box is a candidate too; it looks like slide 2 and is folded into it
    assert sorted(p.name for p in out.iterdir()) == ["001.jpg", "002.jpg", "003.jpg", "index.json"]
    assert all((out / s["file"]).read_bytes().startswith(b"\xff\xd8") for s in found)  # JPEGs
    assert all((out / s["file"]).stat().st_mode & 0o777 == 0o600 for s in found)
    assert all(1_000 < (out / s["file"]).stat().st_size < 60_000 for s in found)  # small: 320 px wide
    assert frames.index(out)["asset_id"] == "asset7001" and frames.read(out) == found
    assert not list(tmp_path.glob("frames/*.part"))


async def test_extract_reports_what_ffmpeg_cannot_do(tmp_path):
    if not audio.available():
        pytest.skip("ffmpeg is not installed")
    broken = tmp_path / "not-a-video.mp4"
    broken.write_bytes(b"\0\0\0\x18ftypmp42" + b"x" * 500)
    with pytest.raises(audio.AudioError, match="ffmpeg could not read the frames"):
        await frames.extract(broken, tmp_path / "frames" / "1", duration=10.0)
    assert not (tmp_path / "frames" / "1").exists() and not list(tmp_path.glob("frames/*.part"))
    assert frames.read(tmp_path / "nowhere") == [] and frames.index(tmp_path / "nowhere") == {}
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "index.json").write_text(json.dumps({"slides": [{"n": 1, "file": "../../etc/passwd"}]}))
    assert frames.read(tmp_path / "bad") == []  # only plain NNN.jpg names are ever served


# --- in the pipeline and over the API ------------------------------------------------------------

LECTURES = [lecture("7001", minutes=5, title="1주차 Slides"), lecture("7002", minutes=5), lecture("9001", hosted=False)]


def make(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("TEST_EL_KEY", SECRET)
    store = Store(tmp_path / "icampus.db")
    seed(store, LECTURES)
    store.set_state("collect_version", 99)
    app = create_app(settings(tmp_path, sync_times="", **env), store=store, background=False)
    app.state.commons, app.state.stt = Commons(), Stt()
    app.state.videos._transport = httpx.MockTransport(app.state.commons)
    app.state.registry.transport = httpx.MockTransport(app.state.stt)
    app.state.registry.save(WHISPER, create=True)
    app.state.seen = fake_audio(monkeypatch, tmp_path)
    return app


@pytest.fixture
def app(tmp_path, monkeypatch):
    return make(tmp_path, monkeypatch)


@pytest.fixture
async def api(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api", headers=MCP) as c:
        c.app = app
        yield c


async def test_slides_are_cut_during_a_transcription_and_paired_with_what_was_said(api):
    pipeline = api.app.state.pipeline
    assert (await api.get("/api/v1/slides/7001")).json() == {
        "item_id": "7001", "title": "1주차 Slides", "state": "none", "count": 0, "slides": []}
    missing = await api.get("/api/v1/lectures/7001/segments")
    assert missing.status_code == 404 and missing.json()["state"] == "none"

    await api.post("/api/v1/transcripts/7001")
    await pipeline.drain()
    job = pipeline.jobs()[0]
    assert job["state"] == "done" and job["stages"]["slides"]["count"] == 2
    assert api.app.state.seen["slides"] == [("asset7001.mp4", "7001", 0.1, 960)]
    status = (await api.get("/api/v1/slides/7001")).json()
    assert status["state"] == "ready" and status["slides"] == [
        {"n": 1, "start_s": 0.0, "end_s": 60.0, "at": "00:00"}, {"n": 2, "start_s": 60.0, "end_s": 300.0, "at": "01:00"}]
    listed = (await api.get("/api/v1/cache?kind=frames")).json()
    assert listed["count"] == 1 and listed["items"][0]["item_id"] == "7001" and listed["items"][0]["key"] == "asset7001"

    image = await api.get("/api/v1/slides/7001/2.jpg")
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    assert image.content.startswith(b"\xff\xd8") and b"slide 2" in image.content
    assert (await api.get("/api/v1/slides/7001/3.jpg")).status_code == 404
    assert (await api.get("/api/v1/slides/7001/x.jpg")).status_code == 422
    assert (await api.get("/api/v1/slides/9001")).status_code == 404  # a YouTube lecture
    assert (await api.get("/api/v1/slides/7001/1.jpg", headers={"Authorization": ""})).status_code == 401

    # a transcript whose sentences fall on both slides
    api.app.state.transcripts.save(LECTURES[0], Transcript("ko", 300.0, [
        Segment(2.0, 8.0, "첫 슬라이드 설명."), Segment(30.0, 40.0, "계속 첫 슬라이드."), Segment(61.0, 70.0, "둘째 슬라이드."),
        Segment(200.0, 210.0, "마무리.")], None, "el", "scribe_v3"), video_version="20260301120000")
    body = (await api.get("/api/v1/lectures/7001/segments")).json()
    assert (body["total"], body["offset"], body["next_offset"], body["slides"], body["model"]) == (2, 0, None, "ready", "el")
    assert body["course"] == "Data Structures" and body["transcript"] == "ready"
    assert [(i["n"], i["at"], i["image"], i["text"]) for i in body["items"]] == [
        (1, "00:00", "/api/v1/slides/7001/1.jpg", "첫 슬라이드 설명. 계속 첫 슬라이드."),
        (2, "01:00", "/api/v1/slides/7001/2.jpg", "둘째 슬라이드. 마무리.")]
    one = (await api.get("/api/v1/lectures/7001/segments?limit=1")).json()
    assert len(one["items"]) == 1 and one["next_offset"] == 1
    rest = (await api.get("/api/v1/lectures/7001/segments?offset=1&limit=1")).json()
    assert rest["items"][0]["n"] == 2 and rest["next_offset"] is None
    tight = (await api.get("/api/v1/lectures/7001/segments?max_chars=200")).json()
    assert len(tight["items"]) == 2  # the first slide's text is never cut, and the second still fits
    assert (await api.get("/api/v1/lectures/7001/segments?offset=5")).json()["items"] == []


async def test_slides_can_be_turned_off_and_a_failure_does_not_fail_the_transcript(api, monkeypatch):
    app = api.app
    app.state.settings.set("slides_enabled", False)
    await api.post("/api/v1/transcripts/7001")
    await app.state.pipeline.drain()
    assert app.state.seen["slides"] == [] and (await api.get("/api/v1/slides/7001")).json()["state"] == "none"
    assert "slides" not in app.state.pipeline.jobs()[0]["stages"]

    app.state.settings.set("slides_enabled", True)

    async def broken(*args, **kwargs):
        raise audio.AudioError("ffmpeg could not read the frames (no keyframes)")
    monkeypatch.setattr(frames, "extract", broken)
    await api.post("/api/v1/transcripts/7002")
    await app.state.pipeline.drain()
    job = app.state.pipeline.jobs(item_id="7002")[0]
    assert job["state"] == "done" and job["stages"]["slides"] == {"error": "ffmpeg could not read the frames (no keyframes)"}
    assert (await api.get("/api/v1/transcripts/7002")).status_code == 200


async def test_slides_on_request_download_the_video_only_when_asked(api):
    app, commons = api.app, api.app.state.commons
    assert (await api.get("/api/v1/slides/7001")).json()["state"] == "none" and commons.downloads == []  # looking: nothing
    asked = await api.post("/api/v1/slides/7001")
    assert asked.status_code == 202 and asked.json()["state"] == "extracting"
    assert ("video", "asset7001") in app.state.pipeline.in_use()  # the video is held while its slides are cut
    await app.state.pipeline._slide_jobs["asset7001"]["task"]
    assert commons.downloads == ["asset7001"] and (await api.get("/api/v1/slides/7001")).json()["state"] == "ready"
    assert app.state.pipeline.in_use() == set()
    again = await api.post("/api/v1/slides/7001")
    assert again.status_code == 200 and commons.downloads == ["asset7001"]

    commons.fail = [503]
    await api.post("/api/v1/slides/7002")
    await app.state.pipeline._slide_jobs["asset7002"]["task"]
    failed = (await api.get("/api/v1/slides/7002")).json()
    assert failed["state"] == "failed" and "HTTP 503" in failed["error"]
    assert app.state.pipeline.in_use() == set()  # a failed extraction holds nothing
    retried = await api.post("/api/v1/slides/7002")  # and asking again tries again
    assert retried.status_code == 202
    await app.state.pipeline._slide_jobs["asset7002"]["task"]
    assert (await api.get("/api/v1/slides/7002")).json()["state"] == "ready"

    newer = [lecture("7001", minutes=5, updated="20260401090000"), *LECTURES[1:]]
    seed(app.state.store, newer)
    assert (await api.get("/api/v1/slides/7001")).json()["state"] == "outdated"  # cut from the older video
    assert (await api.get("/api/v1/lectures/7001/segments")).json()["slides"] == "outdated"


async def test_a_slide_directory_without_a_row_is_indexed_again_and_a_half_written_one_removed(tmp_path, monkeypatch):
    first = make(tmp_path, monkeypatch)
    first.state.pipeline.request_slides(LECTURES[0])
    await first.state.pipeline._slide_jobs["asset7001"]["task"]
    first.state.store.execute("DELETE FROM cache_entries WHERE kind='frames'")  # the index was lost
    (tmp_path / "frames" / "7002.part").mkdir()  # an extraction the last run didn't finish
    (tmp_path / "frames" / "5555").mkdir()  # and a directory that says nothing about itself
    (tmp_path / "frames" / "5555" / "001.jpg").write_bytes(b"x")
    second = create_app(settings(tmp_path, sync_times=""), store=Store(tmp_path / "icampus.db"), background=False)
    entry = second.state.cache.get("frames", "asset7001")
    assert entry.item_id == "7001" and entry.meta["count"] == 2 and entry.meta["version"] == "20260301120000"
    assert second.state.pipeline.slides(LECTURES[0])[0] == "ready"
    assert sorted(p.name for p in (tmp_path / "frames").iterdir()) == ["7001"]


# --- the MCP tool ----------------------------------------------------------------------------------

async def test_get_lecture_segments_returns_each_slide_as_a_picture_with_its_text(app, monkeypatch):
    monkeypatch.setattr(server, "_client", lambda timeout: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api", headers=MCP))
    tools = {t.name: t for t in await server.mcp.list_tools()}
    tool = tools["get_lecture_segments"]
    assert tool.annotations.read_only_hint and list(tool.input_schema["properties"]) == ["item_id", "offset"]
    assert tool.input_schema["properties"]["offset"]["default"] == 0 and "Look at each picture" in tool.description

    with pytest.raises(ToolError, match="no slide frames yet"):
        await server.get_lecture_segments("7001")
    app.state.pipeline.request_slides(LECTURES[0])
    await app.state.pipeline._slide_jobs["asset7001"]["task"]
    app.state.transcripts.save(LECTURES[0], Transcript("ko", 300.0, [
        Segment(2.0, 8.0, "첫 슬라이드 설명."), Segment(61.0, 70.0, "둘째 슬라이드.")], None, "el", "scribe_v3"),
        video_version="20260301120000")

    info, *rest = await server.get_lecture_segments("7001")
    assert info == {"item_id": "7001", "title": "1주차 Slides", "course": "Data Structures", "week": "1주차",
                    "slides": "ready", "transcript": "ready", "model": "el", "total": 2, "offset": 0}
    head1, image1, text1, head2, image2, text2 = rest
    assert head1 == "Slide 1 of 2, on screen 00:00 to 01:00" and head2 == "Slide 2 of 2, on screen 01:00 to 05:00"
    assert isinstance(image1, Image) and image1.to_image_content().mime_type == "image/jpeg"
    assert (text1, text2) == ("첫 슬라이드 설명.", "둘째 슬라이드.")
    result = await server.mcp.call_tool("get_lecture_segments", {"item_id": "7001"})
    assert [c.type for c in result.content] == ["text", "text", "image", "text", "text", "image", "text"]

    monkeypatch.setattr(server, "SLIDES_PER_CALL", 1)
    info, _, _, text = await server.get_lecture_segments("7001")
    assert info["next_offset"] == 1 and text == "첫 슬라이드 설명."
    info, head, _, text = await server.get_lecture_segments("7001", offset=1)
    assert "next_offset" not in info and head.startswith("Slide 2 of 2") and text == "둘째 슬라이드."
    for bad in ("7x", "../7001"):
        with pytest.raises(ToolError):
            await server.get_lecture_segments(bad)
    with pytest.raises(ToolError):
        await server.get_lecture_segments("7001", offset=-1)


# --- the admin page --------------------------------------------------------------------------------

async def test_the_lecture_page_shows_the_slides_and_can_ask_for_them(app):
    app.state.pipeline.request_slides(LECTURES[0])
    await app.state.pipeline._slide_jobs["asset7001"]["task"]
    with TestClient(app, base_url="https://admin.test", follow_redirects=False) as c:
        assert c.get("/admin/media/slide/7001/1.jpg").status_code == 303  # a session, like every admin page
        token = BeautifulSoup(c.get("/admin/login").text, "html.parser").find("input", {"name": "login_token"})["value"]
        c.post("/admin/login", data={"token": ADMIN, "login_token": token})
        page = BeautifulSoup(c.get("/admin/lecture/7001").text, "html.parser")
        thumbs = page.select("button.slide")
        assert [(b["data-seek"], b.select_one("img")["src"]) for b in thumbs] == [
            ("0.0", "/admin/media/slide/7001/1.jpg"), ("60.0", "/admin/media/slide/7001/2.jpg")]
        assert c.get("/admin/media/slide/7001/2.jpg").content.startswith(b"\xff\xd8")
        assert c.get("/admin/media/slide/7001/9.jpg").status_code == 404
        other = BeautifulSoup(c.get("/admin/lecture/7002").text, "html.parser")
        assert "No slide frames yet." in other.get_text() and not other.select("button.slide")
        csrf = other.find("input", {"name": "csrf"})["value"]
        before = list(app.state.commons.downloads)
        assert c.post("/admin/lecture/7002/slides", data={"csrf": csrf}).status_code == 303
        assert c.post("/admin/lecture/7002/slides", data={}).status_code == 403
    await app.state.pipeline._slide_jobs.get("asset7002", {"task": __import__("asyncio").sleep(0)})["task"]
    assert before == ["asset7001"] and app.state.pipeline.slides(LECTURES[1])[0] in ("ready", "extracting")


async def test_all_slide_pictures_load_when_asked_for_at_once(app):
    """The strip asks for every thumbnail in parallel (two of three once failed in a real browser)."""
    import asyncio
    app.state.pipeline.request_slides(LECTURES[0])
    await app.state.pipeline._slide_jobs["asset7001"]["task"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin.test") as c:
        token = BeautifulSoup((await c.get("/admin/login")).text, "html.parser").find(
            "input", {"name": "login_token"})["value"]
        await c.post("/admin/login", data={"token": ADMIN, "login_token": token})
        answers = await asyncio.gather(*(c.get(f"/admin/media/slide/7001/{n}.jpg") for n in (1, 2) * 20))
        assert [r.status_code for r in answers] == [200] * 40
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api", headers=MCP) as c:
        answers = await asyncio.gather(*(c.get(f"/api/v1/slides/7001/{n}.jpg") for n in (1, 2) * 20))
        assert [r.status_code for r in answers] == [200] * 40


# --- are lecture materials on the content server? --------------------------------------------------

def material(kind="pdf", **extra) -> dict:
    return {"content_type": kind, "content_id": "c-314159", "title": "Week 3 slides (private title)",
            "thumbnail_url": f"https://lcms.skku.edu/CommonsCore2/v2/contents/{AID}.png",
            "view_url": f"https://lcms.skku.edu/em/{AID}?user=2026123456", **extra}


def test_the_material_report_shows_shapes_not_contents():
    items = [material(), material("file", thumbnail_url=None, content={"files": [
        {"url": "https://canvas.skku.edu/files/987654/download?verifier=SECRETVERIFIER"}]}), material("pdf")]
    lines, assets = material_report(items)
    text = "\n".join(lines)
    assert "material items: 3" in text and "'pdf': 2" in text and "'file': 1" in text
    assert "materials with a Commons asset id in the thumbnail: 2/3" in text and len(assets) == 2
    assert "https://lcms.skku.edu/CommonsCore2/v2/contents/<id>.png" in text
    for private in (AID, "private title", "2026123456", "SECRETVERIFIER", "987654", "c-314159"):
        assert private not in text


async def test_describing_materials_asks_for_the_description_only():
    seen: list[httpx.Request] = []

    def commons(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == f"/CommonsCore2/v2/contents/{AID}":
            return httpx.Response(200, json={"result": {
                "type": "Document", "extension": "pdf", "privacy": "public", "is_original_file_deleted": False,
                "read_cnt": 7, "original_file_length": 3 * 2**20, "author": "Some Professor",
                "download_url": f"https://lcms.skku.edu/CommonsCore2/v2/services/download/{AID}?token=SECRET"}})
        return pytest.fail(f"something other than a description was requested: {request.url.path}")
    async with httpx.AsyncClient(transport=httpx.MockTransport(commons)) as client:
        lines = await describe_materials(client, [(material(), ("lcms.skku.edu", AID))])
        status, meta = await _describe(client, "lcms.skku.edu", AID)  # the helper video-check uses too
    text = "\n".join(lines)
    assert status.startswith("200") and _result(meta)["extension"] == "pdf" and _result(None) == {}
    assert "type='Document'" in text and "extension='pdf'" in text and "original 3.0 MB" in text
    assert "read_cnt before/after a second description: 7 -> 7" in text
    assert "'download_url': 'https://lcms.skku.edu/CommonsCore2/v2/services/download/<id> (+query)'" in text
    assert "(not requested)" in text
    for private in (AID, "SECRET", "Some Professor"):
        assert private not in text
    assert {r.url.path for r in seen} == {f"/CommonsCore2/v2/contents/{AID}"}  # never the download address
    assert all("cookie" not in r.headers and "authorization" not in r.headers for r in seen)
