"""The transcription queue from request to stored transcript, with a fake content server, fake providers and a
stand-in for ffmpeg: idempotent requests, explicit and automatic routing with fallback, budget pauses and month
rollover, the prefetch limit, restarts, retries, chunking, and what the cache may not evict meanwhile."""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from helpers import MP4, SECRET, WHISPER, Commons, Stt, fake_audio, lecture, make_lecture_mp4, seed, settings
from icampus import audio
from icampus.cache import Cache
from icampus.config import KST
from icampus.pipeline import ACTIVE, Pipeline
from icampus.runtime import Runtime
from icampus.store import Store
from icampus.stt.registry import Registry, Unavailable
from icampus.transcripts import Transcripts
from icampus.videos import Videos

LECTURES = [lecture("7001"), lecture("7002"), lecture("7003"), lecture("7004"), lecture("7005"),
            lecture("8001", course="1002", minutes=25), lecture("9001", hosted=False)]


def build(tmp_path, commons, stt, **env) -> SimpleNamespace:
    """Every component over one data directory, the way create_app wires them. Calling it again is a restart."""
    config = settings(tmp_path, **env)
    store = Store(config.db_path)
    runtime = Runtime(config, store)
    cache = Cache(runtime, store)
    videos = Videos(runtime, cache, transport=httpx.MockTransport(commons))
    registry = Registry(runtime, store, transport=httpx.MockTransport(stt))
    if registry.get("whisper") is None:
        registry.save(WHISPER, create=True)
    transcripts = Transcripts(runtime, store, cache)
    pipeline = Pipeline(runtime, store, cache, videos, registry, transcripts)
    cache.reconcile()
    return SimpleNamespace(settings=runtime, store=store, cache=cache, videos=videos, registry=registry,
                           transcripts=transcripts, pipeline=pipeline, commons=commons, stt=stt)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_EL_KEY", SECRET)
    r = build(tmp_path, Commons(), Stt())
    seed(r.store, LECTURES)
    r.audio = fake_audio(monkeypatch, tmp_path)
    r.tmp_path = tmp_path
    return r


def find(rig, item: str) -> dict:
    from icampus import videos
    return videos.find(rig.store, item)


def job_of(rig, item: str) -> dict:
    return rig.pipeline.jobs(item_id=item)[0]


def due_now(rig) -> None:
    """Brings every waiting retry forward, as if its pause had passed."""
    rig.store.execute("UPDATE jobs SET not_before=? WHERE not_before IS NOT NULL",
                      ((datetime.now(KST) - timedelta(seconds=1)).isoformat(),))


async def test_one_lecture_from_request_to_transcript(rig):
    lec = find(rig, "7001")
    assert rig.pipeline.status(lec) == {"item_id": "7001", "title": "Lecture 7001", "state": "none"}
    first = rig.pipeline.request(lec)
    assert first["state"] == "queued" and first["job"]["model"] == "automatic"
    assert rig.pipeline.request(lec)["job"]["id"] == first["job"]["id"]  # asking again finds the same job
    await rig.pipeline.drain()

    status = rig.pipeline.status(lec)
    assert status["state"] == "ready" and status["model"] == "el" and "job" not in status
    t = status["transcript"]
    assert (t["language"], t["duration_s"], t["timestamps"], t["chosen"]) == ("ko", 600.0, "word", "default provider")
    assert t["cost_usd"] == pytest.approx(0.2 * 600 / 3600, abs=1e-4)
    job = job_of(rig, "7001")
    assert job["state"] == "done" and job["provider"] == "el" and job["transcript_id"] == t["id"]
    assert set(job["stages"]) == {"download", "extract", "transcribe"} and job["hours"] == pytest.approx(1 / 6, abs=1e-3)
    assert rig.commons.downloads == ["asset7001"] and rig.stt.hosts() == ["api.elevenlabs.io"]
    assert rig.audio["extract"] == [("asset7001.mp4", "asset7001.opus24.ogg", "opus", 24)]
    assert rig.registry.usage(rig.registry.get("el"))["hours"] == pytest.approx(1 / 6)
    assert {e.kind for e in rig.cache.entries()} == {"video", "audio", "transcript"} and rig.pipeline.in_use() == set()

    stored = json.loads((rig.tmp_path / "transcripts" / f"t{t['id']}.json").read_text())
    assert stored["provider"] == "el" and stored["segments"][0] == [1.0, 4.6, "행렬 분해는 eigenvalue 문제입니다."]
    assert stored["words"][0][:3] == [1.0, 1.6, "행렬"] and stored["item_id"] == "7001"
    assert (rig.tmp_path / "transcripts" / f"t{t['id']}.json").stat().st_mode & 0o777 == 0o600

    again = rig.pipeline.request(lec)  # a transcript of this video version exists: nothing new happens
    assert again["state"] == "ready" and len(rig.pipeline.jobs()) == 1
    await rig.pipeline.drain()
    assert len(rig.commons.downloads) == 1 and len(rig.stt.calls) == 1


async def test_a_second_provider_by_name_and_both_are_searchable(rig):
    lec = find(rig, "7001")
    rig.pipeline.request(lec)
    await rig.pipeline.drain()
    second = rig.pipeline.request(lec, model="whisper")
    assert second["state"] == "queued" and second["job"]["model"] == "whisper"  # per provider, not per lecture
    await rig.pipeline.drain()
    assert rig.stt.hosts() == ["api.elevenlabs.io", "gpu.tailnet.example"]
    assert len(rig.commons.downloads) == 1 and len(rig.audio["extract"]) == 1  # the video and audio were reused
    rows = rig.transcripts.rows("7001")
    assert [r["provider_id"] for r in rows] == ["whisper", "el"] and rows[0]["reason"] == "asked for by id (whisper)"
    assert rig.transcripts.preferred("7001")["provider_id"] == "whisper"  # the newest, until one is pinned
    assert rig.transcripts.preferred("7001", "el")["provider_id"] == "el"
    assert rig.pipeline.status(lec, "el")["model"] == "el"

    only_el = rig.transcripts.search("eigenvalue")
    assert [(h["item_id"], h["model"], h["at"]) for h in only_el] == [("7001", "el", "00:01")]
    both = rig.transcripts.search("eigen")  # said at the same moment in both: one hit, the preferred one's
    assert len(both) == 1 and both[0]["model"] == "whisper" and both[0]["also_in"] == ["el"]
    assert rig.transcripts.search("행렬")[0]["also_in"] == ["el"]  # a two-character Korean word
    rig.transcripts.prefer(rig.transcripts.preferred("7001", "el")["id"])
    assert rig.transcripts.preferred("7001")["provider_id"] == "el"
    assert rig.transcripts.search("eigen")[0]["model"] == "el"


async def test_automatic_routing_falls_back_but_an_explicit_model_never_switches(rig, caplog):
    caplog.set_level(logging.DEBUG)
    rig.stt.fail["api.elevenlabs.io"] = [429]
    rig.pipeline.request(find(rig, "7001"))
    await rig.pipeline.drain()
    row = rig.transcripts.preferred("7001")
    assert row["provider_id"] == "whisper" and rig.stt.hosts() == ["api.elevenlabs.io", "gpu.tailnet.example"]
    assert row["reason"].startswith("by priority; after el: ElevenLabs is rate limiting (HTTP 429)")
    assert rig.registry.health("el")["failures"] == 1 and rig.registry.health_state("whisper") == "ok"

    rig.stt.broken.add("api.elevenlabs.io")  # a wrong key: the default provider is broken for good
    rig.pipeline.request(find(rig, "7002"))
    await rig.pipeline.drain()
    assert rig.transcripts.preferred("7002")["provider_id"] == "whisper"

    rig.pipeline.request(find(rig, "7003"), model="el")
    await rig.pipeline.drain()
    job = job_of(rig, "7003")
    assert job["state"] == "failed" and job["retryable"] is False and "refused the API key" in job["error"]
    assert rig.transcripts.preferred("7003") is None and rig.stt.hosts()[-1] == "api.elevenlabs.io"  # not whisper

    rig.stt.broken.clear()
    rig.registry.record_ok("el")
    rig.stt.fail["api.elevenlabs.io"] = [503]
    rig.pipeline.request(find(rig, "7004"), model="el")
    await rig.pipeline.drain()
    job = job_of(rig, "7004")  # a failure that may pass: the same provider again later, nobody else meanwhile
    assert job["state"] == "queued" and job["attempts"] == 1 and "trying again at" in job["error"]
    assert job["not_before"] > datetime.now(KST).isoformat() and rig.stt.hosts()[-1] == "api.elevenlabs.io"
    due_now(rig)
    await rig.pipeline.drain()
    assert job_of(rig, "7004")["state"] == "done" and rig.transcripts.preferred("7004")["provider_id"] == "el"
    assert SECRET not in caplog.text
    assert SECRET not in json.dumps(rig.pipeline.jobs()) + json.dumps(rig.registry.health("el"))


async def test_an_explicit_model_that_is_unavailable_fails_up_front_with_the_ids_that_work(rig):
    rig.registry.save({**WHISPER, "enabled": False})
    with pytest.raises(Unavailable, match="whisper can't take this now: disabled") as failure:
        rig.pipeline.request(find(rig, "7001"), model="whisper")
    assert failure.value.working == ["el"] and rig.pipeline.jobs() == []
    with pytest.raises(Unavailable, match="no speech-to-text provider is called 'nobody'"):
        rig.pipeline.request(find(rig, "7001"), model="nobody")


async def test_a_cap_pauses_jobs_before_they_download_and_the_next_period_resumes_them(rig):
    rig.registry.save({**rig.registry.get("el").model_dump(), "monthly_hours_cap": 0.3})  # 18 minutes
    rig.registry.save({**WHISPER, "enabled": False})
    lectures = [find(rig, i) for i in ("7001", "7002", "7003")]
    plan = rig.pipeline.plan(lectures, model="el")
    assert plan["count"] == 3 and plan["hours"] == 0.5 and plan["models"][0]["hours_that_would_wait"] == 0.2
    result = rig.pipeline.queue(lectures, model="el")
    assert result["queued"] == 3
    await rig.pipeline.drain()
    states = {i: job_of(rig, i)["state"] for i in ("7001", "7002", "7003")}
    assert states == {"7001": "done", "7002": "paused_budget", "7003": "paused_budget"}
    assert "monthly cap of 0.3 h" in job_of(rig, "7002")["error"]
    assert rig.commons.downloads == ["asset7001"]  # nothing was downloaded for a job that can't be paid for
    assert rig.pipeline.status(find(rig, "7002"))["state"] == "paused_budget"
    await rig.pipeline.drain()
    assert job_of(rig, "7002")["state"] == "paused_budget"  # still paused: waiting costs nothing

    with pytest.raises(Unavailable, match="monthly cap") as failure:  # one request, by name: refused, not parked
        rig.pipeline.request(find(rig, "7004"), model="el")
    assert failure.value.budget and failure.value.working == []
    parked = rig.pipeline.request(find(rig, "7004"))  # automatic routing with every provider at its cap: it waits
    assert parked["state"] == "paused_budget"

    last_month = (datetime.now(KST) - timedelta(days=32)).isoformat()
    rig.store.execute("UPDATE stt_usage SET at=?", (last_month,))  # the billing period rolled over
    await rig.pipeline.drain()
    states = [job_of(rig, i)["state"] for i in ("7002", "7003", "7004")]
    assert states.count("done") == 1 and states.count("paused_budget") == 2  # one more fits in the new period
    assert len(rig.commons.downloads) == 2

    rig.registry.save({**rig.registry.get("el").model_dump(), "monthly_hours_cap": 5})  # raising the cap does it too
    await rig.pipeline.drain()
    assert [job_of(rig, i)["state"] for i in ("7002", "7003", "7004")] == ["done"] * 3


async def test_a_lecture_of_unknown_length_is_checked_against_the_cap_once_its_audio_exists(rig):
    unknown = lecture("7001", minutes=0)  # the sync saw no duration, so nothing can be estimated up front
    seed(rig.store, [unknown, *LECTURES[1:]])
    rig.registry.save({**rig.registry.get("el").model_dump(), "monthly_hours_cap": 0.1})  # 6 minutes
    rig.registry.save({**WHISPER, "enabled": False})
    rig.pipeline.request(find(rig, "7001"))
    await rig.pipeline.drain()
    job = job_of(rig, "7001")  # the extracted audio turned out to be 10 minutes long
    assert job["state"] == "paused_budget" and job["hours"] == pytest.approx(1 / 6, abs=1e-3)
    assert rig.stt.calls == [] and len(rig.commons.downloads) == 1
    for _ in range(3):
        await rig.pipeline.drain()  # and it stays paused instead of going round in circles
    assert job_of(rig, "7001")["state"] == "paused_budget" and len(rig.audio["extract"]) == 1


async def test_the_prefetch_limit_keeps_downloads_from_running_ahead(rig):
    gate = asyncio.Event()
    running = {"now": 0, "peak": 0}

    class SlowStt(Stt):
        async def __call__(self, request):
            await gate.wait()
            return super().__call__(request)
    rig = build(rig.tmp_path, rig.commons, SlowStt())
    rig.settings.set("prefetch_limit", 2)
    result = rig.pipeline.queue([find(rig, i) for i in ("7001", "7002", "7003", "7004", "7005")])
    assert result["queued"] == 5 and result["states"] == {"queued": 5}
    for _ in range(30):
        rig.pipeline.tick()
        await asyncio.sleep(0.01)
    # the providers are stuck, so nothing finishes: two jobs are past the queue and exactly their videos are here
    assert len(rig.pipeline._tasks) == 2 and sorted(rig.commons.downloads) == ["asset7001", "asset7002"]
    assert [j["state"] for j in rig.pipeline.jobs(states=ACTIVE)].count("queued") == 3
    gate.set()
    await rig.pipeline.drain()
    assert all(j["state"] == "done" for j in rig.pipeline.jobs()) and len(rig.commons.downloads) == 5


async def test_the_queue_survives_a_restart_and_reuses_finished_stages(rig):
    gate = asyncio.Event()
    started = asyncio.Event()

    class StuckStt(Stt):
        async def __call__(self, request):
            started.set()
            await gate.wait()
            return super().__call__(request)
    first = build(rig.tmp_path, rig.commons, StuckStt())
    first.pipeline.queue([find(first, "7001"), find(first, "7002")])
    first.pipeline.tick()
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(0.05)
    assert job_of(first, "7001")["state"] == "transcribing"
    assert job_of(first, "7002")["state"] == "transcribing"  # past the queue too, waiting for the provider's one slot
    await first.pipeline.stop()  # the container stops mid-transcription
    assert job_of(first, "7001")["state"] == "transcribing"  # as the database was left

    second = build(rig.tmp_path, rig.commons, Stt())  # a new process over the same data directory
    assert [j["state"] for j in second.pipeline.jobs()] == ["queued", "queued"]
    await second.pipeline.drain()
    assert [j["state"] for j in second.pipeline.jobs()] == ["done", "done"]
    assert sorted(rig.commons.downloads) == ["asset7001", "asset7002"]  # 7001 was not downloaded a second time
    assert [e[1] for e in rig.audio["extract"]].count("asset7001.opus24.ogg") == 1  # nor extracted again
    assert second.transcripts.preferred("7001")["provider_id"] == "el"


async def test_what_a_job_still_needs_cannot_be_evicted(rig):
    await rig.videos.ensure(find(rig, "7001"))  # exported earlier, so already on disk
    await rig.videos.ensure(find(rig, "7002"))
    assert rig.pipeline.in_use() == set()
    rig.pipeline.request(find(rig, "7001"))
    assert rig.pipeline.in_use() == {("video", "asset7001")}  # held until its audio exists
    rig.settings._overrides["video_cache_gb"] = 2.5 * (len(MP4) + 500) / 2**30  # room for two videos, not three
    await rig.videos.ensure(find(rig, "7003"))  # needs room: the unneeded 7002 goes, the queued job's video stays
    assert rig.videos.path("asset7001") and rig.videos.path("asset7002") is None

    await rig.pipeline.drain()
    assert rig.pipeline.in_use() == set()  # done: nothing is held any more
    rig.registry.save({**rig.registry.get("el").model_dump(), "monthly_hours_cap": 0})
    rig.registry.save({**WHISPER, "enabled": False})
    rig.pipeline.request(find(rig, "7003"))
    assert job_of(rig, "7003")["state"] == "paused_budget"
    assert rig.pipeline.in_use() == set()  # a job that may wait for weeks holds no video

    rig.registry.save(WHISPER)
    rig.pipeline.request(find(rig, "7001"), model="whisper")
    audio_key = rig.cache.entries("audio")[0].key
    assert rig.pipeline.in_use() == {("audio", audio_key)}  # with the audio made, the job needs only that


async def test_downloads_are_retried_with_backoff_then_given_up(rig):
    rig.settings.set("download_retries", 2)
    rig.commons.fail = [503, 503, 503]
    rig.pipeline.request(find(rig, "7001"))
    await rig.pipeline.drain()
    job = job_of(rig, "7001")
    assert job["state"] == "queued" and job["attempts"] == 1
    assert job["error"].startswith("download: Commons refused the download (HTTP 503); trying again at")
    await rig.pipeline.drain()
    assert job_of(rig, "7001")["attempts"] == 1  # not before its time
    for _ in range(2):
        due_now(rig)
        await rig.pipeline.drain()
    job = job_of(rig, "7001")
    assert job["state"] == "failed" and job["retryable"] is True and "gave up after 3 tries" in job["error"]

    retried = rig.pipeline.retry(job["id"])  # by hand, once the content server is back
    assert retried["state"] == "queued" and retried["attempts"] is None and retried["error"] is None
    await rig.pipeline.drain()
    assert job_of(rig, "7001")["state"] == "done"
    with pytest.raises(ValueError, match="only failed or canceled jobs"):
        rig.pipeline.retry(job["id"])
    assert rig.pipeline.retry(999) is None


async def test_a_failure_that_retrying_cannot_fix_fails_at_once(rig, monkeypatch):
    rig.settings.set("video_max_gb", 0.01)

    def describe(request):  # the content server describes a 1 GB original
        if request.url.path.startswith("/CommonsCore2/v2/contents/"):
            return httpx.Response(200, json={"result": {"original_file_length": 2**30}})
        return httpx.Response(500)
    rig.videos._transport = httpx.MockTransport(describe)
    rig.pipeline.request(find(rig, "7001"))
    await rig.pipeline.drain()
    job = job_of(rig, "7001")
    assert job["state"] == "failed" and job["retryable"] is False and "ICAMPUS_VIDEO_MAX_GB" in job["error"]

    monkeypatch.setattr(audio, "available", lambda: False)
    rig.videos._transport = httpx.MockTransport(rig.commons)
    rig.pipeline.request(find(rig, "7002"))
    await rig.pipeline.drain()
    job = job_of(rig, "7002")
    assert job["state"] == "failed" and "ffmpeg is not installed" in job["error"] and rig.commons.downloads == []


async def test_a_newer_video_makes_the_transcript_outdated(rig):
    rig.pipeline.request(find(rig, "7001"))
    await rig.pipeline.drain()
    assert rig.pipeline.after_sync() == {"outdated": 0, "queued": 0}
    newer = [lecture("7001", updated="20260401090000"), *LECTURES[1:]]
    seed(rig.store, newer)
    assert rig.pipeline.after_sync() == {"outdated": 1, "queued": 0}  # marked, not re-run: that costs money
    assert rig.pipeline.status(find(rig, "7001"))["state"] == "outdated"
    assert rig.pipeline.states()["7001"] == {"state": "outdated", "model": "el"}
    assert rig.pipeline.after_sync() == {"outdated": 0, "queued": 0}

    asked = rig.pipeline.request(find(rig, "7001"))  # on request it is made again, for the new version
    assert asked["state"] == "queued" and asked["transcript"]["status"] == "outdated"
    assert rig.pipeline.states()["7001"] == {"state": "queued", "has": "outdated"}
    await rig.pipeline.drain()
    rows = rig.transcripts.rows("7001")
    assert [(r["status"], r["video_version"]) for r in rows] == [("ready", "20260401090000"),
                                                                 ("outdated", "20260301120000")]
    assert rig.transcripts.preferred("7001")["status"] == "ready" and len(rig.commons.downloads) == 2
    assert [e[1] for e in rig.audio["extract"]].count("asset7001.opus24.ogg") == 2  # not the old version's audio

    rig.settings.set("stt_auto_retranscribe", True)
    seed(rig.store, [lecture("7001", updated="20260501090000"), *LECTURES[1:]])
    assert rig.pipeline.after_sync() == {"outdated": 1, "queued": 1}
    assert job_of(rig, "7001")["trigger"] == "auto"


async def test_auto_transcribe_queues_new_lectures_of_the_chosen_courses_once(rig):
    assert rig.pipeline.after_sync() == {"outdated": 0, "queued": 0}  # off by default
    rig.settings.set("stt_auto_transcribe", True)
    assert rig.pipeline.after_sync()["queued"] == 0  # on, but for no course
    rig.settings.set("stt_auto_courses", ["mth2002"])
    assert rig.pipeline.after_sync()["queued"] == 1
    job = job_of(rig, "8001")
    assert (job["trigger"], job["model"], job["state"]) == ("auto", "automatic", "queued")
    assert rig.pipeline.after_sync()["queued"] == 0
    rig.pipeline.cancel(job["id"])
    assert rig.pipeline.after_sync()["queued"] == 0  # tried once for this version: not queued again by itself


async def test_cancel(rig):
    queued = rig.pipeline.request(find(rig, "7001"))["job"]
    canceled = rig.pipeline.cancel(queued["id"])
    assert canceled["state"] == "canceled" and rig.pipeline.in_use() == set()
    await rig.pipeline.drain()
    assert rig.commons.downloads == [] and rig.pipeline.status(find(rig, "7001"))["state"] == "canceled"
    assert rig.pipeline.cancel(999) is None
    assert rig.pipeline.retry(queued["id"])["state"] == "queued"
    await rig.pipeline.drain()
    done = job_of(rig, "7001")
    assert done["state"] == "done" and rig.pipeline.cancel(done["id"])["state"] == "done"  # finished: left alone


async def test_videos_are_deleted_after_processing_when_asked_unless_pinned(rig):
    rig.settings.set("keep_video_after_processing", False)
    await rig.videos.ensure(find(rig, "7002"))
    rig.cache.pin("video", "asset7002")
    rig.pipeline.queue([find(rig, "7001"), find(rig, "7002")])
    await rig.pipeline.drain()
    assert rig.videos.path("asset7001") is None and rig.videos.path("asset7002") is not None
    assert {e.kind for e in rig.cache.entries()} == {"video", "audio", "transcript"}
    assert len(rig.transcripts.rows()) == 2


async def test_a_long_lecture_goes_in_pieces_when_the_provider_needs_it(rig):
    rig.registry.save({**WHISPER, "capabilities": {"max_upload_mb": None, "max_duration_s": 600}})
    rig.pipeline.request(find(rig, "8001"), model="whisper")  # 25 minutes against a 10-minute limit
    rig.stt.fail["gpu.tailnet.example"] = []
    await rig.pipeline.drain()
    job = job_of(rig, "8001")
    assert job["state"] == "done" and job["stages"]["transcribe"]["pieces"] == 3
    assert rig.audio["cut"] == [(0.0, 540.0), (540.0, 1080.0), (1080.0, 1500.0)]  # at silences, none over 600 s
    segments = rig.transcripts.segments(rig.transcripts.preferred("8001"))
    assert [s[0] for s in segments] == [1.0, 541.0, 1081.0]  # shifted by each piece's start
    stored = rig.transcripts.load(rig.transcripts.preferred("8001"))
    assert stored["duration"] == 1500.0 and [w[0] for w in stored["words"]][7:9] == [541.0, 542.0]
    assert rig.registry.usage(rig.registry.get("whisper"))["hours"] == pytest.approx(1500 / 3600)
    assert not list((rig.tmp_path / "audio").glob(".chunks-*"))

    rig.registry.save(WHISPER)  # without the limit the next lecture goes whole
    rig.pipeline.request(find(rig, "7001"), model="whisper")
    await rig.pipeline.drain()
    assert job_of(rig, "7001")["stages"]["transcribe"]["pieces"] == 1 and len(rig.audio["cut"]) == 3


async def test_pieces_already_transcribed_are_not_paid_for_again_after_a_failure(rig):
    rig.registry.save({**WHISPER, "capabilities": {"max_upload_mb": None, "max_duration_s": 600}})

    class SecondPieceFailsOnce(Stt):
        def __call__(self, request):
            if len(self.calls) == 1:
                self.calls.append((request.url.host, {}))
                return httpx.Response(502, text="bad gateway")
            return super().__call__(request)
    rig = build(rig.tmp_path, rig.commons, SecondPieceFailsOnce())
    rig.pipeline.request(find(rig, "8001"), model="whisper")
    await rig.pipeline.drain()
    assert job_of(rig, "8001")["state"] == "queued" and "HTTP 502" in job_of(rig, "8001")["error"]
    assert rig.registry.usage(rig.registry.get("whisper"))["hours"] == pytest.approx(540 / 3600)
    due_now(rig)
    await rig.pipeline.drain()
    assert job_of(rig, "8001")["state"] == "done"
    assert len(rig.stt.calls) == 4  # piece 1, piece 2 (failed), then pieces 2 and 3: piece 1 was not sent again
    assert rig.registry.usage(rig.registry.get("whisper"))["hours"] == pytest.approx(1500 / 3600)


async def test_text_without_timestamps_is_refused_or_accepted_by_setting(rig):
    rig.stt.shape = "text"
    rig.pipeline.request(find(rig, "7001"), model="whisper")
    await rig.pipeline.drain()
    job = job_of(rig, "7001")
    assert job["state"] == "failed" and "returned none timestamps; at least segment are required" in job["error"]
    assert rig.transcripts.rows() == []
    rig.settings.set("stt_min_timestamps", "none")
    rig.pipeline.retry(job["id"])
    await rig.pipeline.drain()
    row = rig.transcripts.preferred("7001")
    assert row["timestamps"] == "none" and rig.transcripts.segments(row)[0][:2] == (0.0, 600.0)


async def test_languages_and_terms_per_course(rig):
    rig.settings.set("stt_course_languages", {"CSE1001": "en"})
    rig.settings.set("stt_default_language", "ko")
    rig.settings.set("stt_course_terms", {"1002": "고유값, Gram-Schmidt"})
    rig.registry.save({**rig.registry.get("el").model_dump(), "keyterms": True})
    rig.pipeline.request(find(rig, "7001"))
    rig.pipeline.request(find(rig, "8001"))
    rig.pipeline.request(find(rig, "7002"), language="ja")
    await rig.pipeline.drain()
    sent = {call["language_code"][0]: call.get("keyterms") for _, call in rig.stt.calls}
    assert sent == {"en": None, "ko": ["고유값", "Gram-Schmidt"], "ja": None}


async def test_a_plan_says_what_would_happen_without_doing_it(rig):
    await rig.videos.ensure(find(rig, "7001"))
    rig.pipeline.request(find(rig, "7002"))
    plan = rig.pipeline.plan([find(rig, i) or lec for i, lec in zip(
        ("7001", "7002", "7003", "8001", "9001"), (None, None, None, None, LECTURES[-1]))])
    assert plan["count"] == 3 and plan["hours"] == pytest.approx(0.8, abs=0.05)
    assert plan["skipped"] == {"already queued": 1, "not hosted on SKKU's content server (YouTube, Zoom)": 1}
    assert plan["automatic_would_use"] == "el" and [m["id"] for m in plan["models"]] == ["el", "whisper"]
    assert plan["models"][0]["cost_usd"] == pytest.approx(0.15, abs=0.02) and plan["models"][1]["cost_usd"] == 0
    assert [i["item_id"] for i in plan["items"]] == ["7001", "7003", "8001"]
    assert len(rig.pipeline.jobs()) == 1 and rig.commons.downloads == ["asset7001"]  # the plan queued nothing

    estimate = rig.pipeline.estimate(find(rig, "7003"))
    assert estimate["would_run"] and estimate["model"] == "el" and estimate["fallbacks"] == ["whisper"]
    assert estimate["cost_usd"] == pytest.approx(0.033, abs=0.001) and estimate["video_cached"] is False
    refused = rig.pipeline.estimate(find(rig, "7003"), model="nobody")
    assert refused["would_run"] is False and refused["working_models"] == ["el", "whisper"]


@pytest.mark.skipif(not audio.available(), reason="ffmpeg is not installed")
async def test_end_to_end_with_real_ffmpeg(tmp_path, monkeypatch):
    """Nothing faked but the two servers: a real MP4 is downloaded, its audio extracted, split at its real
    silences for a provider that takes 22 seconds at most, and the pieces' times land where the cuts were."""
    monkeypatch.setenv("TEST_EL_KEY", SECRET)
    video = make_lecture_mp4(tmp_path / "source.mp4").read_bytes()  # silences at 9-11 s, 29-31 s and 48-50 s
    rig = build(tmp_path / "data", Commons(video), Stt())
    seed(rig.store, [lecture("7001", minutes=1)])
    rig.registry.save({**WHISPER, "capabilities": {"max_upload_mb": None, "max_duration_s": 22}})

    rig.pipeline.request(find(rig, "7001"))  # automatic: ElevenLabs takes the minute whole
    rig.pipeline.request(find(rig, "7001"), model="whisper")
    await rig.pipeline.drain(timeout=120)
    assert [j["state"] for j in rig.pipeline.jobs()] == ["done", "done"], rig.pipeline.jobs()
    [sound] = rig.cache.entries("audio")
    assert sound.meta["duration_s"] == pytest.approx(60, abs=0.2) and 100_000 < sound.size < 400_000
    assert rig.cache.path(sound).read_bytes().startswith(b"OggS") and len(rig.commons.downloads) == 1

    whole = rig.transcripts.preferred("7001", "el")
    assert whole["duration_s"] == pytest.approx(60, abs=0.2) and rig.transcripts.segments(whole)[0][0] == 1.0
    pieces = rig.transcripts.preferred("7001", "whisper")
    starts = [s[0] for s in rig.transcripts.segments(pieces)]
    # the fake provider's words start one second into whatever it is sent, so each start is a cut plus one:
    assert starts == pytest.approx([1.0, 11.0, 31.0, 50.0], abs=0.1)  # cut in the middle of each real silence
    assert job_of(rig, "7001")["stages"]["extract"]["audio_s"] == 60
    assert rig.registry.usage(rig.registry.get("whisper"))["hours"] == pytest.approx(60 / 3600, abs=1e-4)
    assert not list((tmp_path / "data" / "audio").glob(".chunks-*"))
