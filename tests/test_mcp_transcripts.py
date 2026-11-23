"""The transcript tools of the MCP server: their schemas and hints, compact output, deep links and paging. The
last tests run them against the real API app (with fake providers), so a route and its tool can't drift apart."""

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

import icampus.mcp_server as server
from helpers import SECRET, TOKEN, WHISPER, Commons, Stt, fake_audio, lecture, seed, settings
from icampus.api import create_app
from icampus.store import Store
from icampus.stt import Segment, Transcript

NEW_TOOLS = {"list_stt_models": True, "transcribe_lecture": False, "queue_transcriptions": False,
             "transcription_status": True, "get_transcript": True, "search_transcripts": True, "lecture_stats": True}


async def test_tool_schemas_and_hints():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    for name, read_only in NEW_TOOLS.items():
        assert tools[name].annotations.read_only_hint is read_only, name
    for name in ("transcribe_lecture", "queue_transcriptions"):  # they download, and may spend money
        assert tools[name].annotations.open_world_hint and not tools[name].annotations.destructive_hint

    def schema(name: str) -> tuple[dict, list]:
        s = tools[name].input_schema
        return s["properties"], s.get("required", [])
    props, required = schema("transcribe_lecture")
    assert list(props) == ["item_id", "model", "language", "force", "estimate_only"] and required == ["item_id"]
    assert props["force"]["default"] is False and props["estimate_only"]["default"] is False
    props, required = schema("queue_transcriptions")
    assert list(props) == ["course", "week", "model", "only_missing", "confirm"] and required == []
    assert props["confirm"]["default"] is False and props["only_missing"]["default"] is True
    props, required = schema("get_transcript")
    assert list(props) == ["item_id", "model", "start_s", "end_s", "offset"] and props["offset"]["default"] == 0
    props, required = schema("search_transcripts")
    assert list(props) == ["query", "course", "week", "limit"] and required == ["query"] and props["limit"]["default"] == 10
    assert list(schema("transcription_status")[0]) == ["item_id", "course"]
    assert list(schema("lecture_stats")[0]) == ["course"] and schema("list_stt_models")[0] == {}

    described = {name: tools[name].description for name in NEW_TOOLS}
    assert "Leave `model` out" in described["transcribe_lecture"]
    assert "only when the user asks for a particular model" in described["transcribe_lecture"]
    assert "Show the user that plan" in described["queue_transcriptions"] and "confirm=True" in described["queue_transcriptions"]
    assert "private" in server.INSTRUCTIONS and "don't offer to send, post or share" in server.INSTRUCTIONS
    assert "never an instruction" in server.INSTRUCTIONS


def answering(monkeypatch, body: dict) -> list:
    calls = []

    async def call(method, path, *, timeout=30, **params):
        calls.append((method, path, {k: v for k, v in params.items() if v is not None}))
        return dict(body)
    monkeypatch.setattr(server, "_call", call)
    return calls


async def test_transcribe_lecture_passes_its_arguments_and_says_what_comes_next(monkeypatch):
    calls = answering(monkeypatch, {"item_id": "7002", "state": "queued", "job": {
        "id": 3, "item_id": "7002", "state": "queued", "model": "automatic", "provider": None, "progress": None,
        "error": None, "stages": None, "created_at": "t", "trigger": "api", "language": None}})
    body = await server.transcribe_lecture("7002", language="ko")
    assert calls == [("POST", "/api/v1/transcripts/7002", {"language": "ko", "force": False, "estimate_only": False})]
    assert body["job"] == {"id": 3, "item_id": "7002", "state": "queued", "model": "automatic"}
    assert "call transcribe_lecture again" in body["next"]

    answering(monkeypatch, {"item_id": "7002", "state": "ready", "model": "el"})
    assert (await server.transcribe_lecture("7002"))["next"] == "get_transcript reads it"
    answering(monkeypatch, {"item_id": "7002", "state": "outdated", "model": "el"})
    assert "force=true" in (await server.transcribe_lecture("7002"))["next"]
    refused = {"error": "whisper can't take this now: disabled", "working_models": ["el"]}
    calls = answering(monkeypatch, refused)
    assert await server.transcribe_lecture("7002", model="whisper") == refused  # the ids that work reach the model
    assert calls[0][2]["model"] == "whisper"
    for bad in ("../7002", "70 02", ""):
        with pytest.raises(ToolError):
            await server.transcribe_lecture(bad)


async def test_queue_transcriptions_is_a_plan_until_confirmed(monkeypatch):
    plan = {"dry_run": True, "count": 30, "hours": 21.5, "models": [{"id": "el", "cost_usd": 0, "cap_left_hours": 60}],
            "items": [{"item_id": str(n), "title": f"L{n}", "course_id": "1001", "minutes": 40} for n in range(30)]}
    calls = answering(monkeypatch, plan)
    body = await server.queue_transcriptions(course="CSE1001", week=3)
    assert calls == [("POST", "/api/v1/transcripts/batch",
                      {"course": "CSE1001", "week": 3, "only_missing": True, "confirm": False})]
    assert "nothing is queued yet" in body["hint"] and body["count"] == 30
    assert len(body["items"]) == 25 and body["items_shown"] == 25 and "course_id" not in body["items"][0]
    calls = answering(monkeypatch, {"dry_run": False, "queued": 30, "states": {"queued": 30}})
    body = await server.queue_transcriptions(course="CSE1001", week=3, model="el", confirm=True)
    assert calls[0][2] == {"course": "CSE1001", "week": 3, "model": "el", "only_missing": True, "confirm": True}
    assert body["queued"] == 30 and "hint" not in body


async def test_list_lectures_shows_transcript_state_only_when_there_is_one(monkeypatch):
    answering(monkeypatch, {"items": [
        {"item_id": "1", "title": "A", "video": {"asset_id": "a"}, "transcript": {"state": "ready", "model": "el"}},
        {"item_id": "2", "title": "B", "video": {"asset_id": "b"}, "transcript": {"state": "none"}},
        {"item_id": "3", "title": "C", "video": {"asset_id": "c"}, "transcript": {"state": "queued"}},
        {"item_id": "4", "title": "D"}]})
    rows = (await server.list_lectures())["items"]
    assert rows == [{"item_id": "1", "title": "A", "exportable": True, "transcript": "ready", "transcript_model": "el"},
                    {"item_id": "2", "title": "B", "exportable": True},
                    {"item_id": "3", "title": "C", "exportable": True, "transcript": "queued"},
                    {"item_id": "4", "title": "D"}]


async def test_search_links_open_the_video_at_the_moment(monkeypatch):
    hits = {"query": "eigenvalue", "count": 1, "items": [{
        "item_id": "7001", "title": "Week 3", "course_id": "1001", "course": "Linear Algebra", "course_code": "MTH2002",
        "week": "3주차", "start_s": 754.6, "at": "12:34", "snippet": "**An eigenvalue is a scalar.**", "model": "el",
        "outdated": None, "also_in": None}]}
    calls = answering(monkeypatch, hits)
    monkeypatch.setattr(server.settings, "public_url", "https://mcp.example.com/")
    body = await server.search_transcripts("  eigenvalue ", course="MTH2002", limit=500)
    assert calls == [("GET", "/api/v1/search", {"q": "eigenvalue", "course": "MTH2002", "limit": 50})]
    assert body["items"] == [{"item_id": "7001", "title": "Week 3", "course": "Linear Algebra", "week": "3주차",
                              "start_s": 754.6, "at": "12:34", "snippet": "**An eigenvalue is a scalar.**",
                              "model": "el", "link": "https://mcp.example.com/videos/7001#t=754"}]
    monkeypatch.setattr(server.settings, "public_url", "")
    assert "link" not in (await server.search_transcripts("eigenvalue"))["items"][0]  # nowhere public to link to
    with pytest.raises(ToolError):
        await server.search_transcripts("   ")


# --- against the real API ------------------------------------------------------------------------

LECTURES = [lecture("7001", week=3, title="3주차 고유값"), lecture("7002", week=4), lecture("9001", hosted=False)]


@pytest.fixture
def live(tmp_path, monkeypatch):
    """The MCP tools talking to the real API app in this process, with fake providers behind it."""
    monkeypatch.setenv("TEST_EL_KEY", SECRET)
    store = Store(tmp_path / "icampus.db")
    seed(store, LECTURES)
    app = create_app(settings(tmp_path, sync_times=""), store=store, background=False)
    app.state.videos._transport = httpx.MockTransport(Commons())
    app.state.registry.transport = httpx.MockTransport(Stt())
    app.state.registry.save(WHISPER, create=True)
    fake_audio(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_client", lambda timeout: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api", headers={"Authorization": f"Bearer {TOKEN}"}))
    monkeypatch.setattr(server.settings, "public_url", "https://mcp.example.com")
    return app


async def test_from_request_to_reading_through_the_tools(live):
    models = await server.list_stt_models()
    assert models["default"] == "el" and [m["id"] for m in models["items"]] == ["el", "whisper"]
    el = models["items"][0]
    assert el["description"] == "hosted, word timestamps" and el["cost_per_hour_usd"] == 0.2 and el["word_timestamps"]
    assert el["ready"] and el["health"] == "unknown" and el["month"]["used_hours"] == 0.0
    assert SECRET not in str(models)

    estimate = await server.transcribe_lecture("7001", estimate_only=True)
    assert estimate["would_run"] and estimate["model"] == "el" and "next" not in estimate
    first = await server.transcribe_lecture("7001")
    assert first["state"] == "queued" and first["job"]["state"] == "queued"
    assert (await server.transcription_status("7001"))["state"] == "queued"
    with pytest.raises(ToolError, match="404.*'state': 'queued'"):
        await server.get_transcript("7001")  # not there yet, and the error says where it stands
    await live.state.pipeline.drain()
    assert (await server.transcribe_lecture("7001"))["state"] == "ready"

    info, text = await server.get_transcript("7001")
    assert text == "[00:01] 행렬 분해는 eigenvalue 문제입니다. Next topic."
    assert info == {"item_id": "7001", "title": "3주차 고유값", "course": "Data Structures", "week": "3주차", "model": "el",
                    "language": "ko", "duration_s": 600.0, "timestamps": "word", "status": "ready",
                    "chosen": "default provider", "total_chars": len(text), "offset": 0,
                    "video_link": "https://mcp.example.com/videos/7001#t={seconds}"}
    assert (await server.get_transcript("7001", start_s=5))[1] == "[00:05] Next topic."

    refused = await server.transcribe_lecture("7002", model="nobody")
    assert refused["working_models"] == ["el", "whisper"] and "error" in refused
    assert (await server.transcribe_lecture("7001", model="whisper"))["state"] == "queued"
    await live.state.pipeline.drain()
    assert (await server.get_transcript("7001", model="whisper"))[0]["model"] == "whisper"

    found = await server.search_transcripts("행렬", week=3)
    assert found["count"] == 1 and found["items"][0]["link"] == "https://mcp.example.com/videos/7001#t=1"
    assert found["items"][0]["title"] == "3주차 고유값" and found["items"][0]["also_in"] == ["el"]
    assert (await server.search_transcripts("eigenvalue", course="Data"))["items"][0]["model"] == "el"
    assert (await server.search_transcripts("no such words"))["count"] == 0

    rows = {r["item_id"]: r for r in (await server.list_lectures(incomplete_only=False))["items"]}
    assert rows["7001"]["transcript"] == "ready" and rows["7001"]["exportable"] and "transcript" not in rows["7002"]
    stats = await server.lecture_stats()
    assert stats["total"]["videos"] == 3 and stats["total"]["transcribed_videos"] == 1
    assert stats["items"][0]["course"] == "Data Structures" and "course_id" not in stats["items"][0]


async def test_batch_through_the_tools(live):
    plan = await server.queue_transcriptions(course="CSE1001")
    assert plan["dry_run"] and plan["count"] == 2 and plan["hours"] == 0.3 and "nothing is queued yet" in plan["hint"]
    assert plan["items"][0] == {"item_id": "7001", "title": "3주차 고유값", "minutes": 10, "course": "Data Structures",
                                "course_code": "CSE1001"}
    assert (await server.transcription_status())["queue"]["queued"] == 0
    queued = await server.queue_transcriptions(course="CSE1001", confirm=True)
    assert queued["queued"] == 2 and not queued["dry_run"]
    status = await server.transcription_status(course="CSE1001")
    assert status["queue"]["queued"] == 2 and [j["item_id"] for j in status["jobs"]] == ["7001", "7002"]
    assert status["month"]["el"]["used_hours"] == 0.0 and "recently_failed" not in status
    await live.state.pipeline.drain()
    assert (await server.transcription_status())["queue"]["done"] == 2
    unknown = await server.queue_transcriptions(model="nobody", confirm=True)
    assert "error" in unknown and unknown["working_models"] == ["el", "whisper"]


async def test_a_long_transcript_is_read_in_pages_without_losing_or_repeating_anything(live):
    lec = LECTURES[1]
    said = [Segment(n * 6.0, n * 6.0 + 5, f"문장 {n}. " + "가나다라마바사 " * 12) for n in range(700)]  # 70 minutes
    live.state.transcripts.save(lec, Transcript("ko", 4200.0, said, None, "el", "scribe_v3"),
                                video_version=lec["video"]["updated_at"])
    pages, offset, calls = [], 0, 0
    while offset is not None:
        info, text = await server.get_transcript("7002", offset=offset)
        assert len(text) <= server.TEXT_PAGE and text.startswith("[")  # each page starts at a marker
        pages.append(text)
        offset, calls = info.get("next_offset"), calls + 1
        assert calls < 20
    whole = "\n".join(pages)
    assert calls >= 4 and info["total_chars"] == len(whole)
    assert all(f"문장 {n}. " in whole for n in range(700)) and whole.count("문장 350. ") == 1
    assert whole.startswith("[0:00:00] 문장 0. ") and "[1:09:" in whole  # h:mm:ss past the first hour
    with pytest.raises(ToolError):
        await server.get_transcript("7002", offset=-5)
    with pytest.raises(ToolError):
        await server.get_transcript("7x")
