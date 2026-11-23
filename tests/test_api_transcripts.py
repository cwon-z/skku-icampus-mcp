"""The REST routes for providers, transcripts, jobs, search and statistics, against fake provider servers. One test
runs the real background loop; the others drive the queue by hand. Throughout: no response and no log record may
carry an API key."""

import json
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from helpers import ADMIN, OTHER_SECRET, SECRET, TOKEN, WHISPER, Commons, Stt, fake_audio, lecture, seed, settings
from icampus.api import create_app
from icampus.store import Store

LECTURES = [lecture("7001", week=1), lecture("7002", week=2), lecture("7003", week=2, minutes=50),
            lecture("8001", course="1002", minutes=75, week=1), lecture("9001", hosted=False, minutes=30)]
MCP = {"Authorization": f"Bearer {TOKEN}"}
ADM = {"Authorization": f"Bearer {ADMIN}"}


def make_app(tmp_path, monkeypatch, background: bool = False):
    monkeypatch.setenv("TEST_EL_KEY", SECRET)
    store = Store(tmp_path / "icampus.db")
    seed(store, LECTURES)
    store.set_state("collect_version", 99)  # the rows are current: no start-up sync
    app = create_app(settings(tmp_path, sync_times=""), store=store, background=background)
    app.state.commons, app.state.stt = Commons(), Stt()
    app.state.videos._transport = httpx.MockTransport(app.state.commons)
    app.state.registry.transport = httpx.MockTransport(app.state.stt)
    app.state.registry.save(WHISPER, create=True)
    app.state.audio = fake_audio(monkeypatch, tmp_path)
    return app


@pytest.fixture
def app(tmp_path, monkeypatch):
    return make_app(tmp_path, monkeypatch)


@pytest.fixture
async def api(app):
    """A client in the test's own event loop, so the queue can be drained between requests. Every answer is
    kept in `seen` for the no-secrets check."""
    seen: list[str] = []

    async def record(response: httpx.Response) -> None:
        await response.aread()
        seen.append(response.text)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api", headers=MCP,
                                 event_hooks={"response": [record]}) as client:
        client.seen = seen
        client.app = app
        yield client


async def test_models_for_choosing(api):
    assert (await api.get("/api/v1/stt/models", headers={"Authorization": ""})).status_code == 401
    body = (await api.get("/api/v1/stt/models")).json()
    assert body["count"] == 2 and body["default"] == "el" and body["min_timestamps"] == "segment"
    el, whisper = body["items"]
    assert el["id"] == "el" and el["description"] == "hosted, word timestamps" and el["cost_per_hour_usd"] == 0.2
    assert el["word_timestamps"] and el["languages"] == "any" and el["health"]["state"] == "unknown"
    assert el["month"]["left_hours"] is None and el["ready"] and el["default"] and el["key"] == "set"
    assert whisper["key"] == "not needed" and whisper["cost_per_hour_usd"] == 0
    assert "config" not in el and "key_status" not in el


async def test_providers_are_managed_with_an_admin_token_and_keys_are_write_only(api):
    new = {"id": "groq", "type": "openai_compatible", "model": "whisper-large-v3",
           "base_url": "https://api.groq.example/openai/v1", "cost_per_hour": 0.111, "monthly_usd_cap": 5}
    for method, path, body in (("GET", "/api/v1/stt/providers", None), ("POST", "/api/v1/stt/providers", new),
                               ("PUT", "/api/v1/stt/providers/el", new), ("DELETE", "/api/v1/stt/providers/el", None),
                               ("PUT", "/api/v1/stt/providers/el/key", {"key": OTHER_SECRET}),
                               ("DELETE", "/api/v1/stt/providers/el/key", None),
                               ("POST", "/api/v1/stt/providers/el/test", None)):
        assert (await api.request(method, path, json=body)).status_code == 403, path  # the mcp token may not

    created = await api.post("/api/v1/stt/providers", headers=ADM, json=new)
    assert created.status_code == 201 and created.json()["config"]["monthly_usd_cap"] == 5
    assert created.json()["key_status"] == {"set": False, "source": None, "env": None, "hint": None, "needed": False}
    assert (await api.post("/api/v1/stt/providers", headers=ADM, json=new)).status_code == 422  # exists
    bad = await api.post("/api/v1/stt/providers", headers=ADM, json={
        **new, "id": "other", "api_key": SECRET, "api_key_env": "ICAMPUS_PASSWORD", "base_url": "https://lcms.skku.edu"})
    assert bad.status_code == 422 and set(bad.json()["details"]) == {"api_key", "api_key_env", "base_url"}
    assert SECRET not in bad.text

    keyed = await api.put("/api/v1/stt/providers/groq/key", headers=ADM, json={"key": OTHER_SECRET})
    assert keyed.json()["key_status"] == {"set": True, "source": "stored", "env": None, "hint": "…5678",
                                          "needed": False}
    assert (await api.put("/api/v1/stt/providers/groq/key", headers=ADM, json={"key": "short"})).status_code == 422
    listed = (await api.get("/api/v1/stt/providers", headers=ADM)).json()["items"]
    assert [p["id"] for p in listed] == ["el", "whisper", "groq"] and listed[2]["key"] == "set"

    updated = await api.put("/api/v1/stt/providers/groq", headers=ADM, json={**new, "id": "ignored", "enabled": False})
    assert updated.json()["id"] == "groq" and updated.json()["enabled"] is False
    assert (await api.put("/api/v1/stt/providers/nobody", headers=ADM, json=new)).status_code == 404

    tested = (await api.post("/api/v1/stt/providers/el/test", headers=ADM)).json()
    assert tested["ok"] and tested["health"] == "ok" and "eigenvalue" in tested["text"]
    api.app.state.stt.broken.add("api.elevenlabs.io")
    failed = (await api.post("/api/v1/stt/providers/el/test", headers=ADM)).json()
    assert not failed["ok"] and "refused the API key" in failed["error"] and failed["health"] == "failing"

    forgot = await api.delete("/api/v1/stt/providers/groq/key", headers=ADM)
    assert forgot.json()["key_status"]["set"] is False
    assert (await api.delete("/api/v1/stt/providers/groq", headers=ADM)).json() == {"id": "groq", "deleted": True}
    assert (await api.delete("/api/v1/stt/providers/groq", headers=ADM)).status_code == 404
    assert (await api.post("/api/v1/stt/providers/BAD ID/test", headers=ADM)).status_code == 422


async def test_transcribe_then_read_in_every_format(api):
    pipeline = api.app.state.pipeline
    estimate = (await api.post("/api/v1/transcripts/7001?estimate_only=true")).json()
    assert estimate["would_run"] and estimate["model"] == "el" and estimate["cost_usd"] == pytest.approx(0.033, abs=1e-3)
    assert (await api.get("/api/v1/jobs")).json()["count"] == 0  # an estimate queues nothing

    missing = await api.get("/api/v1/transcripts/7001")
    assert missing.status_code == 404 and missing.json()["state"] == "none"
    asked = await api.post("/api/v1/transcripts/7001")
    assert asked.status_code == 202 and asked.json()["state"] == "queued"
    waiting = await api.get("/api/v1/transcripts/7001")
    assert waiting.status_code == 404 and waiting.json()["state"] == "queued" and waiting.json()["job"]["id"] == 1
    await pipeline.drain()
    again = await api.post("/api/v1/transcripts/7001")
    assert again.status_code == 200 and again.json()["state"] == "ready" and again.json()["model"] == "el"
    assert (await api.get("/api/v1/transcripts/status?item_id=7001")).json()["state"] == "ready"

    text = (await api.get("/api/v1/transcripts/7001")).json()
    assert text["text"] == "[00:01] 행렬 분해는 eigenvalue 문제입니다. Next topic."
    assert (text["model"], text["language"], text["course"], text["week"], text["next_offset"]) == (
        "el", "ko", "Data Structures", "1주차", None)
    assert text["status"] == "ready" and text["chosen"] == "default provider" and text["total_chars"] == len(text["text"])
    as_json = (await api.get("/api/v1/transcripts/7001?format=json")).json()
    assert as_json["segments"] == [{"start": 1.0, "end": 4.6, "text": "행렬 분해는 eigenvalue 문제입니다."},
                                   {"start": 5.0, "end": 6.6, "text": "Next topic."}]
    assert [t["model"] for t in as_json["transcripts"]] == ["el"]
    vtt = await api.get("/api/v1/transcripts/7001?format=vtt")
    assert vtt.headers["content-type"].startswith("text/vtt") and vtt.text.startswith("WEBVTT\n\n00:00:01.000 --> ")
    srt = await api.get("/api/v1/transcripts/7001?format=srt&start_s=5")
    assert srt.text == "1\n00:00:05.000 --> 00:00:06.600\nNext topic.\n".replace(".000", ",000").replace(".600", ",600")
    assert (await api.get("/api/v1/transcripts/7001?start_s=5")).json()["text"] == "[00:05] Next topic."

    second = await api.post("/api/v1/transcripts/7001?model=whisper&language=ko")
    assert second.status_code == 202 and second.json()["job"]["model"] == "whisper"
    await pipeline.drain()
    assert (await api.get("/api/v1/transcripts/7001")).json()["model"] == "whisper"  # the newest is preferred
    assert (await api.get("/api/v1/transcripts/7001?model=el")).json()["model"] == "el"
    pinned = await api.put("/api/v1/transcripts/7001/preferred?model=el")
    assert pinned.json()["preferred"]["model"] == "el" and pinned.json()["preferred"]["preferred"] is True
    assert (await api.get("/api/v1/transcripts/7001")).json()["model"] == "el"
    assert (await api.put("/api/v1/transcripts/7002/preferred")).status_code == 404

    forced = await api.post("/api/v1/transcripts/7001?force=true")
    assert forced.status_code == 202  # a new transcript although one exists
    assert (await api.post("/api/v1/transcripts/9001")).status_code == 404  # a YouTube lecture
    assert (await api.post("/api/v1/transcripts/7x")).status_code == 422
    assert (await api.post("/api/v1/transcripts/7002?model=Bad Id")).status_code == 422


async def test_an_explicit_model_that_cannot_run_is_refused_with_the_ids_that_can(api):
    api.app.state.registry.save({**WHISPER, "enabled": False})
    refused = await api.post("/api/v1/transcripts/7001?model=whisper")
    assert refused.status_code == 409 and refused.json() == {
        "error": "whisper can't take this now: disabled", "working_models": ["el"]}
    unknown = await api.post("/api/v1/transcripts/7001?model=nobody")
    assert unknown.status_code == 409 and unknown.json()["working_models"] == ["el"]
    assert (await api.get("/api/v1/jobs")).json()["count"] == 0
    batch = await api.post("/api/v1/transcripts/batch?model=nobody&confirm=true")
    assert batch.status_code == 409 and (await api.get("/api/v1/jobs")).json()["count"] == 0


async def test_a_batch_is_a_dry_run_until_confirmed(api):
    api.app.state.registry.save({**api.app.state.registry.get("el").model_dump(), "monthly_hours_cap": 1.5})
    plan = await api.post("/api/v1/transcripts/batch?course=cse1001")
    body = plan.json()
    assert plan.status_code == 200 and body["dry_run"] and body["count"] == 3 and body["hours"] == 1.2
    assert body["skipped"] == {"not hosted on SKKU's content server (YouTube, Zoom)": 1}
    assert body["automatic_would_use"] == "el" and body["items"][0]["course"] == "Data Structures"
    el = body["models"][0]
    assert (el["id"], el["cap_left_hours"], el["hours_that_would_wait"]) == ("el", 1.5, 0)
    assert el["cost_usd"] == pytest.approx(0.2333, abs=1e-3) and body["download_gb"] == 0.0
    assert (await api.get("/api/v1/jobs")).json()["count"] == 0 and api.app.state.commons.downloads == []

    week2 = (await api.post("/api/v1/transcripts/batch?course=cse1001&week=2")).json()
    assert week2["count"] == 2 and [i["item_id"] for i in week2["items"]] == ["7002", "7003"]
    everything = (await api.post("/api/v1/transcripts/batch?model=el")).json()
    assert everything["count"] == 4 and everything["hours"] == 2.4 and everything["automatic_would_use"] is None
    assert everything["models"][0]["hours_that_would_wait"] == 0.9  # 2.4 h against a 1.5 h cap

    queued = await api.post("/api/v1/transcripts/batch?course=cse1001&confirm=true")
    assert queued.status_code == 202 and queued.json()["queued"] == 3 and queued.json()["states"] == {"queued": 3}
    again = (await api.post("/api/v1/transcripts/batch?course=cse1001")).json()
    assert again["count"] == 0 and again["skipped"]["already queued"] == 3
    await api.app.state.pipeline.drain()
    done = (await api.post("/api/v1/transcripts/batch?course=cse1001")).json()
    assert done["count"] == 0 and done["skipped"]["already transcribed"] == 3
    redo = (await api.post("/api/v1/transcripts/batch?course=cse1001&only_missing=false")).json()
    assert redo["count"] == 3  # on request, everything again


async def test_jobs_search_stats_and_lecture_rows(api):
    pipeline = api.app.state.pipeline
    await api.post("/api/v1/transcripts/batch?confirm=true")
    listed = (await api.get("/api/v1/jobs")).json()
    assert listed["count"] == 4 and listed["queue"]["queued"] == 4 and listed["queue"]["waiting_hours"] == 2.4
    assert listed["items"][0]["course"] == "Data Structures" and listed["items"][0]["trigger"] == "batch"
    canceled = await api.delete("/api/v1/jobs/4")
    assert canceled.json()["state"] == "canceled" and (await api.delete("/api/v1/jobs/99")).status_code == 404
    assert (await api.post("/api/v1/jobs/1/retry")).status_code == 409  # still queued
    assert (await api.post("/api/v1/jobs/99/retry")).status_code == 404
    await pipeline.drain()
    assert (await api.get("/api/v1/jobs?state=done")).json()["count"] == 3
    assert (await api.get("/api/v1/jobs?state=active")).json()["count"] == 0
    assert [j["id"] for j in (await api.get("/api/v1/jobs?course=mth2002")).json()["items"]] == [4]
    assert (await api.get("/api/v1/jobs?item_id=7002")).json()["items"][0]["state"] == "done"
    assert (await api.post("/api/v1/jobs/4/retry")).json()["state"] == "queued"
    assert (await api.get("/api/v1/jobs?state=nonsense")).status_code == 422

    rows = {x["item_id"]: x for x in (await api.get("/api/v1/lectures")).json()["items"]}
    assert rows["7001"]["transcript"] == {"state": "ready", "model": "el"}
    assert rows["8001"]["transcript"] == {"state": "queued"} and "transcript" not in rows["9001"]

    found = (await api.get("/api/v1/search?q=행렬")).json()  # two characters
    assert found["count"] == 3 and found["search"] == "trigram index"
    hit = found["items"][0]
    assert (hit["course"], hit["course_code"], hit["at"], hit["start_s"], hit["model"]) == (
        "Data Structures", "CSE1001", "00:01", 1.0, "el")
    assert hit["week"] in ("1주차", "2주차") and hit["snippet"].startswith("**행렬 분해는 eigenvalue 문제입니다.**")
    assert (await api.get("/api/v1/search?q=eigenvalue&week=2")).json()["count"] == 2
    assert (await api.get("/api/v1/search?q=eigenvalue&course=mth2002")).json()["count"] == 0
    assert (await api.get("/api/v1/search?q=eigenvalue&limit=1")).json()["count"] == 1
    assert (await api.get("/api/v1/search?q=")).status_code == 422
    assert (await api.get("/api/v1/search?q=x&course=nonsense")).status_code == 400

    stats = (await api.get("/api/v1/lectures/stats")).json()
    assert stats["count"] == 2 and stats["total"] == {
        "videos": 5, "hours": 2.9, "exportable_videos": 4, "exportable_hours": 2.4, "exportable_gb": 0.0,
        "transcribed_videos": 3, "transcribed_hours": 1.2, "outdated": 0, "in_queue": 1, "failed": 0}
    assert stats["untranscribed_hours"] == 1.2
    assert [(e["model"], e["cost_usd"]) for e in stats["cost_to_transcribe_the_rest"]] == [("el", 0.24), ("whisper", 0)]
    one = (await api.get("/api/v1/lectures/stats?course=1002")).json()
    assert one["items"][0]["course"] == "선형대수" and one["total"]["videos"] == 1 and one["total"]["in_queue"] == 1

    status = (await api.get("/api/v1/status")).json()["transcription"]
    assert status["done"] == 3 and status["queued"] == 1 and status["search"] == "trigram index"
    overview = (await api.get("/api/v1/transcripts/status")).json()
    assert overview["queue"]["queued"] == 1 and overview["month"]["el"]["used_hours"] == 1.17  # 70 minutes
    assert [j["item_id"] for j in overview["jobs"]] == ["8001"]


async def test_stt_settings_through_the_api(api):
    ok = await api.put("/api/v1/settings", headers=ADM, json={
        "stt_default_provider": "whisper", "stt_fallback_order": ["el"], "stt_min_timestamps": "word",
        "stt_routing_rules": [{"course": "MTH2002", "provider": "el"}], "prefetch_limit": 3,
        "stt_course_languages": {"MTH2002": "ko"}})
    assert ok.status_code == 200
    models = (await api.get("/api/v1/stt/models")).json()
    assert models["default"] == "whisper" and models["routing_rules"] == [{"course": "MTH2002", "provider": "el"}]
    assert (await api.post("/api/v1/transcripts/8001?estimate_only=true")).json()["chosen"] == "routing rule (course MTH2002)"
    assert (await api.post("/api/v1/transcripts/7001?estimate_only=true")).json()["model"] == "whisper"
    bad = await api.put("/api/v1/settings", headers=ADM, json={
        "stt_routing_rules": [{"provider": "el"}], "stt_min_timestamps": "letter", "prefetch_limit": 0})
    assert bad.status_code == 422 and len(bad.json()["details"]) == 3


async def test_no_key_or_token_in_any_answer_or_log_record(api, caplog):
    caplog.set_level(logging.DEBUG)
    app = api.app
    await api.put("/api/v1/stt/providers/whisper/key", headers=ADM, json={"key": OTHER_SECRET})
    app.state.stt.fail["api.elevenlabs.io"] = [500]  # the fake echoes the key it was sent in its error
    await api.post("/api/v1/transcripts/7001")
    await api.post("/api/v1/transcripts/7002?model=el")
    app.state.stt.broken.add("api.elevenlabs.io")
    await app.state.pipeline.drain()
    await api.post("/api/v1/stt/providers/el/test", headers=ADM)
    await api.post("/api/v1/stt/providers/whisper/test", headers=ADM)
    await api.post("/api/v1/stt/providers", headers=ADM, json={"id": "x1", "type": "elevenlabs", "api_key": SECRET})
    for path in ("/api/v1/stt/models", "/api/v1/jobs", "/api/v1/transcripts/status", "/api/v1/status", "/api/v1/cache",
                 "/api/v1/transcripts/7001", "/api/v1/transcripts/7002", "/api/v1/transcripts/7001?format=json",
                 "/api/v1/lectures", "/api/v1/lectures/stats", "/api/v1/search?q=eigen", "/api/v1/export"):
        await api.get(path)
    for path in ("/api/v1/stt/providers", "/api/v1/settings"):
        await api.get(path, headers=ADM)
    assert len(api.seen) > 15 and any("refused the API key" in text for text in api.seen)  # the failures are there
    everything = "\n".join(api.seen) + caplog.text + json.dumps(
        [dict(r) for r in app.state.store.query("SELECT * FROM jobs")]
        + [dict(r) for r in app.state.store.query("SELECT * FROM stt_providers")]
        + [dict(r) for r in app.state.store.query("SELECT * FROM transcripts")], ensure_ascii=False)
    for secret in (SECRET, OTHER_SECRET, TOKEN, ADMIN):
        assert secret not in everything
    assert json.loads(app.state.registry.keys_path.read_text()) == {"whisper": OTHER_SECRET}  # only in the 0600 file
    assert app.state.registry.keys_path.stat().st_mode & 0o777 == 0o600


def test_the_background_loop_runs_the_queue(tmp_path, monkeypatch):
    """The real coordinator, started by the app's lifespan, picks up a request by itself."""
    with TestClient(make_app(tmp_path, monkeypatch, background=True)) as c:
        c.headers.update(MCP)
        assert c.post("/api/v1/transcripts/7001").status_code == 202
        for _ in range(200):
            state = c.get("/api/v1/transcripts/status?item_id=7001").json()["state"]
            if state == "ready":
                break
            time.sleep(0.02)
        assert state == "ready" and c.get("/api/v1/transcripts/7001").json()["text"].startswith("[00:01] 행렬")
        assert c.get("/api/v1/status").json()["runs"] == []  # starting up asked iCampus for nothing
