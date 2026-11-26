"""The server side of remote fetchers: leasing a queued download to another machine, its heartbeats, the upload
(resumed after a blip, and refused when it isn't the file), failures and leases that run out, who may call what,
and the queue's promises with fetchers in it: one job to one taker, in the order asked, room kept for it, and a
waiting transcription job let go when the upload is in. Everything is made up; the content server is a fake."""

import asyncio
import hashlib
import json
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from helpers import ADMIN, MP4, TOKEN, Commons, lecture, seed, settings
from icampus import videos as vm
from icampus.api import create_app
from icampus.cache import Cache
from icampus.config import KST, Settings
from icampus.store import MIGRATIONS, SCHEMA, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, Store
from icampus.videos import OutOfStep, Refused, StaleLease, VideoError, Videos

MBP, THINKPAD = "f" * 24, "g" * 24  # fetcher tokens
SHA = hashlib.sha256(MP4).hexdigest()
LECTURES = [lecture(str(7001 + n)) for n in range(5)]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def make(tmp_path, commons=None, **env):
    store = Store(tmp_path / "icampus.db")
    seed(store, LECTURES)
    store.set_state("collect_version", 99)
    env.setdefault("local_downloads", "off")  # unless a test is about the server downloading too
    app = create_app(settings(tmp_path, sync_times="", fetcher_tokens=f"mbp:{MBP},thinkpad:{THINKPAD}", **env),
                     store=store, background=False)
    app.state.commons = commons or Commons()
    app.state.videos._transport = httpx.MockTransport(app.state.commons)
    return app


@pytest.fixture
def app(tmp_path):
    return make(tmp_path)


@pytest.fixture
def c(app):
    with TestClient(app) as client:
        client.headers.update(bearer(TOKEN))
        yield client


def lease(c, token=MBP, name="mbp", **more):
    return c.post("/api/v1/fetchers/lease", headers=bearer(token),
                  json={"name": name, "version": "0.1.0", "platform": "macOS 15 arm64", **more})


def leased(c, item="7001", **how) -> dict:
    """Exports a lecture and leases it to a fetcher."""
    assert c.post(f"/api/v1/videos/{item}").status_code == 202
    r = lease(c, **how)
    assert r.status_code == 200, r.text
    return r.json()


def beat(c, job, token=MBP, **report):
    body = {"lease_id": job["lease_id"], "phase": "downloading", "done_bytes": 0, **report}
    return c.post(f"/api/v1/fetchers/jobs/{job['job_id']}/progress", headers=bearer(token), json=body)


def put(c, job, data: bytes, start: int = 0, total: int = len(MP4), sha: str | None = None, token=MBP):
    headers = {**bearer(token), "X-Lease-Id": job["lease_id"],
               "Content-Range": f"bytes {start}-{start + len(data) - 1}/{total}"}
    if sha:
        headers["X-File-SHA256"] = sha
    return c.put(f"/api/v1/fetchers/jobs/{job['job_id']}/file", headers=headers, content=data)


def have(c, job, token=MBP):
    return c.get(f"/api/v1/fetchers/jobs/{job['job_id']}/file", headers={**bearer(token), "X-Lease-Id": job["lease_id"]})


def fail(c, job, message="lcms.skku.edu: ReadTimeout", transient=True, token=MBP):
    return c.post(f"/api/v1/fetchers/jobs/{job['job_id']}/fail", headers=bearer(token),
                  json={"lease_id": job["lease_id"], "message": message, "transient": transient})


def tick(c) -> None:
    """One round of the queue's worker, inside the app's event loop."""
    c.portal.call(c.app.state.videos.tick)


def expire(c) -> None:
    """Every lease ran out a second ago, and the worker comes round."""
    past = (datetime.now(KST) - timedelta(seconds=1)).isoformat(timespec="microseconds")
    c.app.state.store.execute("UPDATE video_jobs SET lease_expires_at=? WHERE lease_id IS NOT NULL", (past,))
    tick(c)


def due_now(store) -> None:
    past = (datetime.now(KST) - timedelta(seconds=1)).isoformat(timespec="microseconds")
    store.execute("UPDATE video_jobs SET not_before=? WHERE not_before IS NOT NULL", (past,))


def rows(store) -> list[dict]:
    return [dict(r) for r in store.query("SELECT * FROM video_jobs ORDER BY queued_at, id")]


# --- who may call what ------------------------------------------------------------------------------------------

def test_a_fetcher_token_opens_the_fetcher_routes_and_nothing_else(c):
    for method, path in (("GET", "/api/v1/courses"), ("GET", "/api/v1/status"), ("GET", "/api/v1/videos"),
                         ("POST", "/api/v1/videos/7001"), ("GET", "/api/v1/videos/7001/content"),
                         ("GET", "/api/v1/transcripts/7001"), ("GET", "/api/v1/cache"), ("GET", "/api/v1/settings"),
                         ("DELETE", "/api/v1/videos/7001"), ("GET", "/api/v1/export"), ("POST", "/api/v1/sync")):
        r = c.request(method, path, headers=bearer(MBP))
        assert r.status_code == 403 and "fetcher token" in r.json()["error"], path
    assert rows(c.app.state.store) == []  # and the refused export queued nothing
    job = leased(c)
    for call in (lambda t: lease(c, token=t), lambda t: beat(c, job, token=t), lambda t: have(c, job, token=t),
                 lambda t: put(c, job, MP4, sha=SHA, token=t), lambda t: fail(c, job, token=t)):
        assert call(TOKEN).status_code == 403 and call(ADMIN).status_code == 403  # no other token, admin included
        assert call("x" * 24).status_code == 401 and call("").status_code == 401
    assert beat(c, job).json() == {"continue": True}  # none of that cost the fetcher its job
    for token in (MBP, TOKEN):  # the one route both kinds may read
        assert c.get("/api/v1/fetchers/bench-targets", headers=bearer(token)).status_code == 200
    assert c.get("/api/v1/fetchers/bench-targets", headers=bearer("x" * 24)).status_code == 401


def test_a_fetcher_token_is_not_an_admin_login(c):
    from bs4 import BeautifulSoup
    with TestClient(c.app, base_url="https://admin.test", follow_redirects=False) as browser:
        form = BeautifulSoup(browser.get("/admin/login").text, "html.parser").find("input", {"name": "login_token"})
        r = browser.post("/admin/login", data={"token": MBP, "login_token": form["value"]})
        said = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        assert r.status_code == 401 and "isn't one of this server's API tokens" in said
        assert "set-cookie" not in r.headers and browser.get("/admin/").status_code == 303  # still signed out


def test_without_fetcher_tokens_nothing_changes_and_the_routes_refuse(tmp_path):
    store = Store(tmp_path / "icampus.db")
    seed(store, LECTURES)
    app = create_app(settings(tmp_path, sync_times=""), store=store, background=False)
    commons = Commons()
    app.state.videos._transport = httpx.MockTransport(commons)
    with TestClient(app) as client:
        client.headers.update(bearer(TOKEN))
        assert lease(client, token=TOKEN).status_code == 503 and lease(client, token=MBP).status_code == 503
        assert client.post("/api/v1/videos/7001").status_code == 202  # the server downloads, as it always has
        for _ in range(200):
            if (exports := client.get("/api/v1/status").json()["exports"])["ready"] == 1:
                break
        assert exports["ready"] == 1 and exports["fetchers"] == [] and commons.downloads == ["asset7001"]
        assert exports["worker"]["local_downloads"] == "on"
        assert client.get("/api/v1/settings", headers=bearer(ADMIN)).status_code == 200


def test_one_token_cannot_be_both_kinds(tmp_path):
    with pytest.raises(ValueError, match="both an API token and a fetcher token"):
        create_app(settings(tmp_path, fetcher_tokens=f"mbp:{TOKEN}"), store=Store(tmp_path / "x.db"), background=False)
    with pytest.raises(ValueError, match="20\\+ random characters"):  # the same checks as for API tokens
        create_app(settings(tmp_path, fetcher_tokens="mbp:short"), store=Store(tmp_path / "y.db"), background=False)


def test_tokens_are_shown_by_name_only(c):
    shown = c.get("/api/v1/settings", headers=bearer(ADMIN)).json()
    row = next(e for e in shown["env_only"] if e["key"] == "fetcher_tokens")
    assert row["value"] == "mbp, thinkpad" and MBP not in json.dumps(shown) and THINKPAD not in json.dumps(shown)


# --- lease, heartbeat, upload -------------------------------------------------------------------------------------

def test_lease_heartbeat_and_a_resumed_upload_end_like_a_download_of_the_servers_own(c, tmp_path):
    videos, commons = c.app.state.videos, c.app.state.commons
    assert lease(c).status_code == 204  # nothing is due
    job = leased(c)
    assert job.keys() == {"job_id", "lease_id", "asset_id", "host", "title", "expected_bytes", "max_bytes",
                          "lease_ttl_s"}
    assert (job["asset_id"], job["host"], job["title"]) == ("asset7001", "lcms.skku.edu", "Lecture 7001")
    assert (job["expected_bytes"], job["max_bytes"], job["lease_ttl_s"]) == (len(MP4), 4 * 2**30, 90)
    assert commons.downloads == []  # the server asked Commons about the file (no cookies) and fetched nothing
    assert lease(c).status_code == 204  # the job is taken

    status = c.get("/api/v1/videos/7001").json()
    assert (status["state"], status["by"], status["phase"], status["attempts"]) == ("downloading", "mbp", "downloading", 1)
    assert beat(c, job, done_bytes=2 * 2**20, total_bytes=len(MP4), speed_bps=3 * 2**20).json() == {"continue": True}
    status = c.get("/api/v1/videos/7001").json()
    assert (status["downloaded_mb"], status["speed_mbps"], status["by"]) == (2, 3.0, "mbp")
    exports = c.get("/api/v1/status").json()["exports"]
    assert exports["downloading"] == 1 and exports["active"][0]["by"] == "mbp"
    assert exports["active"][0]["phase"] == "downloading" and exports["active"][0]["speed_mbps"] == 3.0
    assert exports["fetchers"] == [{"name": "mbp", "platform": "macOS 15 arm64", "version": "0.1.0",
                                    "last_seen": exports["fetchers"][0]["last_seen"], "online": True,
                                    "active_jobs": 1, "speed_mbps": 3.0}]

    # the upload: a piece, a blip, "how much do you have?", the rest
    assert have(c, job).json() == {"received_bytes": 0, "complete": False}
    assert put(c, job, MP4[:1500]).json() == {"received_bytes": 1500, "complete": False}
    assert beat(c, job, phase="uploading", done_bytes=1500, total_bytes=len(MP4), speed_bps=2**20).json()["continue"]
    status = c.get("/api/v1/videos/7001").json()
    assert (status["state"], status["phase"], status["by"], status["speed_mbps"]) == ("downloading", "uploading", "mbp", 1.0)
    again = put(c, job, MP4[:1500])  # sent once more, as after a lost answer: the server says where it is
    assert again.status_code == 416 and again.json()["received_bytes"] == 1500
    assert have(c, job).json() == {"received_bytes": 1500, "complete": False}
    assert put(c, job, MP4[1500:2000], start=1500).json() == {"received_bytes": 2000, "complete": False}
    short = put(c, job, MP4[2000:2500], start=2000)  # a piece cut off on its way counts for what arrived
    assert short.json() == {"received_bytes": 2500, "complete": False}
    done = put(c, job, MP4[2500:], start=2500, sha=SHA)
    assert done.status_code == 200 and done.json() == {"received_bytes": len(MP4), "complete": True}

    status = c.get("/api/v1/videos/7001").json()
    assert status["state"] == "ready" and status["format"] == "mp4" and "by" not in status
    assert c.get("/api/v1/videos/7001/content").content == MP4
    assert rows(c.app.state.store) == [] and videos._reserved == {} and videos.leases == {}
    assert sorted(p.name for p in (tmp_path / "videos").iterdir()) == ["asset7001.json", "asset7001.mp4"]
    assert oct((tmp_path / "videos" / "asset7001.mp4").stat().st_mode & 0o777) == "0o600"
    # the job is done. A fetcher that missed that answer hears it again, however it asks
    assert beat(c, job).json() == {"continue": False, "complete": True}
    assert have(c, job).json() == {"received_bytes": len(MP4), "complete": True}
    assert put(c, job, MP4[2500:], start=2500, sha=SHA).json() == {"received_bytes": len(MP4), "complete": True}
    assert fail(c, job).status_code == 409 and c.get("/api/v1/videos/7001").json()["state"] == "ready"

    # the same file, sidecar and cache entry as when the server downloads one itself
    c.app.state.settings.set("local_downloads", "on")
    assert c.post("/api/v1/videos/7002").status_code == 202
    for _ in range(200):
        if c.get("/api/v1/videos/7002").json()["state"] == "ready":
            break
    by_fetcher, by_server = videos.meta("asset7001"), videos.meta("asset7002")
    assert by_fetcher.keys() == by_server.keys() and commons.downloads == ["asset7002"]
    same = ("size", "format", "content_type", "course_id", "synced_updated_at", "pinned")
    assert {k: by_fetcher[k] for k in same} == {k: by_server[k] for k in same}
    assert (by_fetcher["file"], by_fetcher["name"], by_fetcher["item_id"]) == ("asset7001.mp4", "Lecture 7001.mp4", "7001")
    entry = c.app.state.cache.get("video", "asset7001")
    assert entry.size == c.app.state.cache.get("video", "asset7002").size and entry.item_id == "7001"
    listed = c.get("/api/v1/videos").json()
    assert sorted(x["item_id"] for x in listed["items"]) == ["7001", "7002"] and listed["queue"] == []


@pytest.mark.parametrize("case, message", [
    ("wrong size", "but Commons described a file of 4012"),
    ("wrong hash", "SHA-256 mismatch"),
    ("no hash", "without its SHA-256"),
    ("not mp4", "not an MP4"),
])
def test_an_upload_that_is_not_the_file_fails_the_try(tmp_path, case, message):
    body = MP4 if case != "not mp4" else b"PK\x03\x04" + b"z" * (len(MP4) - 4)  # as long as described, and no video
    app = make(tmp_path, Commons(body))
    with TestClient(app) as c:
        c.headers.update(bearer(TOKEN))
        videos = app.state.videos
        job = leased(c)
        assert videos._reserved == {"asset7001": len(body)}
        if case == "wrong size":
            r = put(c, job, body[:-10], total=len(body) - 10, sha=hashlib.sha256(body[:-10]).hexdigest())
        elif case == "wrong hash":
            assert put(c, job, body[:2000]).status_code == 200
            r = put(c, job, body[2000:-1] + b"!", start=2000, sha=hashlib.sha256(body).hexdigest())
        else:
            r = put(c, job, body, sha=None if case == "no hash" else hashlib.sha256(body).hexdigest())
        assert r.status_code == 422 and message in r.json()["error"] and r.json()["refused"] is True
        status = c.get("/api/v1/videos/7001").json()  # a failed try like any other: it waits for its next one
        assert (status["state"], status["attempts"]) == ("queued", 1) and status["retry_at"]
        assert status["last_error"].startswith("mbp: ") and message in status["last_error"]
        assert not list((tmp_path / "videos").glob("*")) and videos._reserved == {} and videos.leases == {}
        assert put(c, job, body, sha=hashlib.sha256(body).hexdigest()).status_code == 409  # that lease is over
        assert app.state.cache.get("video", "asset7001") is None


def test_malformed_uploads_are_refused_without_costing_the_job(c):
    job = leased(c)
    url, headers = f"/api/v1/fetchers/jobs/{job['job_id']}/file", {**bearer(MBP), "X-Lease-Id": job["lease_id"]}
    for bad in ("bytes 0-10", "bytes 10-5/4012", "bytes 0-4012/4012", "items 0-1/2", ""):
        assert c.put(url, headers={**headers, "Content-Range": bad}, content=b"x").status_code == 400, bad
    assert c.put(url, headers=headers, content=b"x").status_code == 422  # no Content-Range at all
    assert c.put(url, headers={**bearer(MBP), "Content-Range": "bytes 0-0/4012"}, content=b"x").status_code == 422
    big = c.put(url, headers={**headers, "Content-Range": f"bytes 0-{65 * 2**20}/{70 * 2**20}"}, content=b"x")
    assert big.status_code == 413
    longer = c.put(url, headers={**headers, "Content-Range": "bytes 0-9/4012"}, content=MP4[:500])
    assert longer.status_code == 400 and have(c, job).json()["received_bytes"] == 0  # none of it was kept
    assert put(c, job, MP4, sha="z" * 64).status_code == 422 and beat(c, job).json() == {"continue": True}
    assert put(c, job, MP4, sha=SHA.upper()).json()["complete"] is True  # and the job is still this fetcher's


def test_a_stale_lease_is_answered_409_everywhere(c):
    job = leased(c)
    other = {**job, "lease_id": "not-the-lease-of-this-job"}
    for call in (beat, have, fail, lambda c, j: put(c, j, MP4, sha=SHA)):
        assert call(c, other).status_code == 409
    assert beat(c, job).json() == {"continue": True}
    nobody = {**job, "job_id": 9999}
    assert beat(c, nobody).json() == {"continue": False}  # no such job (any more): stop
    assert have(c, nobody).status_code == 409 and fail(c, nobody).status_code == 409
    assert put(c, nobody, MP4, sha=SHA).status_code == 409


def test_a_job_taken_out_of_the_queue_tells_its_fetcher_to_stop(c, tmp_path):
    job = leased(c)
    assert put(c, job, MP4[:1000]).status_code == 200
    gone = c.delete("/api/v1/videos/7001", headers=bearer(ADMIN)).json()
    assert gone == {"deleted": False, "canceled": True}
    assert beat(c, job).json() == {"continue": False} and put(c, job, MP4[1000:], start=1000, sha=SHA).status_code == 409
    videos = c.app.state.videos
    assert videos._reserved == {} and videos.leases == {} and not list((tmp_path / "videos").glob("*.part"))
    assert c.get("/api/v1/videos/7001").json()["state"] == "not_downloaded"


# --- failures and leases that run out -----------------------------------------------------------------------------

def test_a_reported_failure_goes_through_the_queues_own_retry_policy(c, tmp_path):
    videos = c.app.state.videos
    job = leased(c)
    put(c, job, MP4[:1000])
    told = fail(c, job, "lcms.skku.edu: ReadTimeout", True)
    assert told.status_code == 200 and told.json()["state"] == "queued" and told.json()["attempts"] == 1
    status = c.get("/api/v1/videos/7001").json()
    assert status["last_error"] == "mbp: lcms.skku.edu: ReadTimeout" and status["state"] == "queued"
    wait = datetime.fromisoformat(status["retry_at"]) - datetime.now(KST)
    assert timedelta(seconds=20) < wait <= timedelta(seconds=30)  # the same first pause as a local try
    assert videos._reserved == {} and not list((tmp_path / "videos").glob("*.part"))
    assert fail(c, job).status_code == 409 and lease(c).status_code == 204  # over, and not due yet

    due_now(c.app.state.store)
    job = lease(c, token=THINKPAD, name="thinkpad").json()  # whoever is free takes the next try
    assert job["asset_id"] == "asset7001" and c.get("/api/v1/videos/7001").json()["by"] == "thinkpad"
    told = fail(c, job, "Commons no longer has this video's original file", False, token=THINKPAD)
    assert told.json()["state"] == "failed"
    status = c.get("/api/v1/videos/7001").json()
    assert status["state"] == "failed" and status["retryable"] is False and status["attempts"] == 2
    assert status["error"] == "thinkpad: Commons no longer has this video's original file"


def test_a_fetchers_words_are_cleaned_before_they_are_stored(c):
    job = leased(c)
    fail(c, job, "ReadError for https://lcms.skku.edu/index.php?module=x&token=SECRET\n\nand\tmore " + "x" * 400)
    stored = json.dumps(rows(c.app.state.store)) + json.dumps(c.get("/api/v1/status").json())
    assert "SECRET" not in stored and "index.php" not in stored and "(address removed)" in stored
    assert len(c.get("/api/v1/videos/7001").json()["last_error"]) < 220
    due_now(c.app.state.store)
    job = lease(c).json()
    fail(c, job, "see index.php?token=SECRET2")
    assert "SECRET2" not in json.dumps(rows(c.app.state.store))


def test_retries_run_out_for_a_fetchers_tries_too(tmp_path):
    app = make(tmp_path, export_retries=1)
    with TestClient(app) as c:
        c.headers.update(bearer(TOKEN))
        fail(c, leased(c), "fetcher stopped", True)
        due_now(app.state.store)
        fail(c, lease(c).json(), "fetcher stopped", True)
        status = c.get("/api/v1/videos/7001").json()
        assert status["state"] == "failed" and status["error"] == "mbp: fetcher stopped (gave up after 2 tries)"
        assert status["retryable"] is True and lease(c).status_code == 204
        assert c.post("/api/v1/videos/7001?retry=true").status_code == 202 and lease(c).status_code == 200


def test_a_fetcher_that_goes_quiet_loses_its_job_and_another_takes_it(c, tmp_path):
    videos, store = c.app.state.videos, c.app.state.store
    job = leased(c)
    put(c, job, MP4[:1000])
    before = datetime.fromisoformat(rows(store)[0]["lease_expires_at"])
    assert timedelta(seconds=80) < before - datetime.now(KST) <= timedelta(seconds=90)
    tick(c)  # the worker comes round: the lease still holds
    assert rows(store)[0]["state"] == "downloading" and videos._reserved == {"asset7001": len(MP4)}
    assert vm.LEASE_TTL_S + vm.TICK_S + vm.RETRY_AFTER_S[0] <= 130  # a closed lid: back and due in about two minutes

    expire(c)
    row = rows(store)[0]
    assert (row["state"], row["attempts"], row["fetcher"], row["lease_id"]) == ("queued", 1, None, None)
    assert row["error"] == "the fetcher mbp went quiet: nothing was heard from it for 90 seconds"
    assert videos._reserved == {} and videos.leases == {} and not list((tmp_path / "videos").glob("*.part"))
    # the laptop wakes up: whatever it says about the old job, the answer is to drop it
    assert beat(c, job).status_code == 409 and have(c, job).status_code == 409
    assert put(c, job, MP4[1000:], start=1000, sha=SHA).status_code == 409 and fail(c, job).status_code == 409
    assert c.get("/api/v1/videos/7001").json()["state"] == "queued"

    due_now(store)
    second = lease(c, token=THINKPAD, name="thinkpad").json()
    assert second["asset_id"] == "asset7001" and second["lease_id"] != job["lease_id"]
    assert put(c, second, MP4, sha=SHA, token=THINKPAD).json()["complete"] is True
    assert c.get("/api/v1/videos/7001").json()["state"] == "ready"


def test_heartbeats_and_upload_pieces_keep_the_lease(c):
    store = c.app.state.store
    job = leased(c)
    soon = (datetime.now(KST) + timedelta(seconds=2)).isoformat(timespec="microseconds")
    for call in (lambda: beat(c, job, done_bytes=10), lambda: have(c, job), lambda: put(c, job, MP4[:100])):
        store.execute("UPDATE video_jobs SET lease_expires_at=?", (soon,))
        assert call().status_code == 200
        left = datetime.fromisoformat(rows(store)[0]["lease_expires_at"]) - datetime.now(KST)
        assert timedelta(seconds=80) < left <= timedelta(seconds=90)


def test_the_server_or_a_fetcher_takes_over_after_a_lease_ran_out(tmp_path):
    commons = Commons()
    app = make(tmp_path, commons, local_downloads="on", download_concurrency=1)
    with TestClient(app) as c:
        c.headers.update(bearer(TOKEN))
        commons.hold = asyncio.Event()  # the server's one slot stays busy with the first video
        assert c.post("/api/v1/videos", json={"item_ids": ["7001", "7002"]}).status_code == 202
        job = lease(c).json()
        assert job["asset_id"] == "asset7002"
        expire(c)
        due_now(app.state.store)
        c.portal.call(commons.hold.set)
        for _ in range(300):
            tick(c)
            if c.get("/api/v1/status").json()["exports"]["ready"] == 2:
                break
        assert commons.downloads == ["asset7001", "asset7002"]  # the server did the second one itself after all
        assert put(c, job, MP4, sha=SHA).status_code == 409


# --- the queue's promises, with fetchers in it (the queue itself, no HTTP) ----------------------------------------

def queue(tmp_path, commons, **env) -> Videos:
    """Calling it again over the same directory is a restart of the server."""
    config = Settings(data_dir=tmp_path, _env_file=None, **{"local_downloads": "off", **env})
    return Videos(config, Cache(config, Store(config.db_path)), transport=httpx.MockTransport(commons))


async def pieces(*parts: bytes):
    for part in parts:
        yield part


async def upload(v: Videos, job: dict, body: bytes = MP4) -> dict:
    return await v.receive(job["job_id"], job["lease_id"], start=0, length=len(body), total=len(body),
                           sha256=hashlib.sha256(body).hexdigest(), body=pieces(body))


async def test_two_fetchers_never_get_the_same_job_and_the_order_is_kept(tmp_path):
    commons = Commons()
    v = queue(tmp_path, commons)
    for lec in LECTURES:
        v.start(lec)
    taken = await asyncio.gather(*(v.lease(name) for name in ("mbp", "thinkpad", "mbp", "thinkpad", "mbp", "mbp")))
    assets = [job["asset_id"] for job in taken if job]
    assert sorted(assets) == [f"asset{7001 + n}" for n in range(5)] and taken.count(None) == 1  # each exactly once
    assert len({job["lease_id"] for job in taken if job}) == 5
    by_start = [r["asset_id"] for r in sorted(rows(v.store), key=lambda r: (r["started_at"], r["id"]))]
    assert by_start == [f"asset{7001 + n}" for n in range(5)]  # claimed in the order they were asked for
    assert [(r["state"], r["fetcher"] in ("mbp", "thinkpad")) for r in rows(v.store)] == [("downloading", True)] * 5
    assert await v.lease("mbp") is None and commons.downloads == []
    mine = sum(1 for lease_ in v.leases.values() if lease_.fetcher == "mbp")
    assert 1 <= mine <= vm.FETCHER_SLOTS and v.summary()["downloading"] == 5


async def test_one_fetcher_holds_only_so_many_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(vm, "FETCHER_SLOTS", 2)
    v = queue(tmp_path, Commons())
    for lec in LECTURES:
        v.start(lec)
    assert [bool(await v.lease("mbp")) for _ in range(4)] == [True, True, False, False]
    assert await v.lease("mbp", slots_free=0) is None and (await v.lease("thinkpad"))["asset_id"] == "asset7003"


async def test_local_downloads_on_the_server_takes_the_first_and_fetchers_what_waits_behind(tmp_path):
    commons = Commons()
    commons.hold = asyncio.Event()
    v = queue(tmp_path, commons, local_downloads="on", download_concurrency=1)
    for lec in LECTURES[:3]:
        v.start(lec)
    assert [(r["state"], r["fetcher"]) for r in rows(v.store)] == [("downloading", None), ("queued", None), ("queued", None)]
    first, second = await v.lease("mbp"), await v.lease("thinkpad")
    assert (first["asset_id"], second["asset_id"]) == ("asset7002", "asset7003")  # in the order asked, whoever takes
    assert await v.lease("mbp") is None
    statuses = [v.status(lec) for lec in LECTURES[:3]]
    assert [(s["state"], s["by"]) for s in statuses] == [("downloading", "server"), ("downloading", "mbp"),
                                                         ("downloading", "thinkpad")]
    v.tick()  # a job a fetcher has is never also started here
    assert list(v.active) == ["asset7001"]
    await upload(v, first)
    commons.hold.set()
    await v.drain()
    await upload(v, second)
    assert commons.downloads == ["asset7001"] and [v.status(lec)["state"] for lec in LECTURES[:3]] == ["ready"] * 3


async def test_local_downloads_off_the_server_never_downloads(tmp_path):
    commons = Commons()
    v = queue(tmp_path, commons, local_downloads="off")
    v.start(LECTURES[0])
    await v.drain()
    v.tick()
    assert v.status(LECTURES[0])["state"] == "queued" and commons.downloads == [] and v.active == {}
    assert v.summary()["worker"]["local_downloads"] == "off"
    job = await v.lease("mbp")
    assert (await upload(v, job))["complete"] and v.status(LECTURES[0])["state"] == "ready"


async def test_local_downloads_fallback_the_server_downloads_only_while_no_fetcher_is_around(tmp_path, monkeypatch):
    commons = Commons()
    v = queue(tmp_path, commons, local_downloads="fallback")
    v.start(LECTURES[0])  # no fetcher was ever heard from: the server does it
    await v.drain()
    assert v.status(LECTURES[0])["state"] == "ready" and commons.downloads == ["asset7001"]

    assert await v.lease("mbp") is None  # a fetcher looks for work: from now on it is around
    v.start(LECTURES[1])
    await v.drain()
    assert v.status(LECTURES[1])["state"] == "queued" and v.active == {}  # left for the fetcher
    assert queue(tmp_path, commons, local_downloads="fallback")._local() is False  # a restart still knows of it
    assert v.fetcher_list()[0]["online"] is True

    monkeypatch.setattr(vm, "FETCHER_SEEN_S", 0.0)  # two minutes without a word from any fetcher
    assert v.fetcher_list()[0]["online"] is False
    v.tick()
    await v.drain()
    assert v.status(LECTURES[1])["state"] == "ready" and commons.downloads == ["asset7001", "asset7002"]


async def test_a_transcription_job_waiting_for_the_video_goes_on_when_the_upload_is_in(tmp_path):
    v = queue(tmp_path, Commons())
    waiting = asyncio.create_task(v.ensure(LECTURES[0]))  # what pipeline.py does for a job's download
    await asyncio.sleep(0)
    assert v.status(LECTURES[0])["state"] == "queued" and not waiting.done()
    job = await v.lease("mbp")
    await v.receive(job["job_id"], job["lease_id"], start=0, length=2000, total=len(MP4), sha256=None,
                    body=pieces(MP4[:2000]))
    await asyncio.sleep(0)
    assert not waiting.done()
    await v.receive(job["job_id"], job["lease_id"], start=2000, length=len(MP4) - 2000, total=len(MP4), sha256=SHA,
                    body=pieces(MP4[2000:3000], MP4[3000:]))
    path = await asyncio.wait_for(waiting, 1)
    assert path.read_bytes() == MP4 and v._waiters == {}

    # and when the fetcher's try fails instead, the job hears that, and that it may pass
    for end in ("fail", "quiet", "refused"):
        lec = LECTURES[1 + ("fail", "quiet", "refused").index(end)]
        waiting = asyncio.create_task(v.ensure(lec))
        await asyncio.sleep(0)
        job = await v.lease("mbp")
        if end == "fail":
            v.fail_lease(job["job_id"], job["lease_id"], "lcms.skku.edu: ConnectError", True)
        elif end == "quiet":
            v.store.execute("UPDATE video_jobs SET lease_expires_at='2000-01-01' WHERE id=?", (job["job_id"],))
            v.tick()
        else:
            with pytest.raises(Refused):
                await v.receive(job["job_id"], job["lease_id"], start=0, length=len(MP4), total=len(MP4),
                                sha256="0" * 64, body=pieces(MP4))
        with pytest.raises(VideoError) as failure:
            await asyncio.wait_for(waiting, 1)
        assert failure.value.transient and "mbp" in str(failure.value), end
        assert v.status(lec)["state"] == "failed"  # a job's download isn't retried here: its own queue does that


async def test_through_the_pipeline_a_fetchers_upload_becomes_a_transcript(tmp_path, monkeypatch):
    from helpers import SECRET, Stt, fake_audio
    from icampus.pipeline import Pipeline
    from icampus.runtime import Runtime
    from icampus.stt.registry import Registry
    from icampus.transcripts import Transcripts

    monkeypatch.setenv("TEST_EL_KEY", SECRET)
    config = settings(tmp_path, local_downloads="off")
    store = Store(config.db_path)
    seed(store, LECTURES)
    runtime = Runtime(config, store)
    cache = Cache(runtime, store)
    v = Videos(runtime, cache, transport=httpx.MockTransport(Commons()))
    transcripts = Transcripts(runtime, store, cache)
    pipeline = Pipeline(runtime, store, cache, v, Registry(runtime, store, transport=httpx.MockTransport(Stt())),
                        transcripts)
    fake_audio(monkeypatch, tmp_path)
    pipeline.request(vm.find(store, "7001"))
    pipeline.tick()
    for _ in range(100):
        if v.status(LECTURES[0])["state"] == "queued":
            break
        await asyncio.sleep(0.01)
    assert pipeline.jobs(item_id="7001")[0]["state"] == "downloading"  # it waits in ensure()
    job = await v.lease("mbp")
    assert pipeline.jobs(item_id="7001")[0]["progress"]["by"] == "mbp"  # and says who is fetching its video
    await upload(v, job)
    await pipeline.drain()
    assert pipeline.jobs(item_id="7001")[0]["state"] == "done" and transcripts.preferred("7001") is not None


async def test_room_is_kept_from_the_lease_on_and_given_back(tmp_path):
    size = len(MP4)
    v = queue(tmp_path, Commons(), video_cache_gb=2.5 * size / 2**30)
    for lec in LECTURES[:4]:
        v.start(lec)
    a, b = await v.lease("mbp"), await v.lease("mbp")
    assert v._reserved == {"asset7001": size, "asset7002": size}
    assert await v.lease("mbp") is None  # a third doesn't fit beside the two that are on their way
    third = rows(v.store)[2]
    assert (third["state"], third["attempts"]) == ("queued", 1) and "no room for this video" in third["error"]

    v.fail_lease(a["job_id"], a["lease_id"], "fetcher stopped", True)  # a failure gives its room back
    assert v._reserved == {"asset7002": size}
    v.store.execute("UPDATE video_jobs SET lease_expires_at='2000-01-01' WHERE id=?", (b["job_id"],))
    v.tick()  # and so does a lease that ran out
    assert v._reserved == {} and v.leases == {}

    due_now(v.store)
    again = await v.lease("mbp")
    assert again["asset_id"] == "asset7001" and v._reserved == {"asset7001": size}
    assert (await upload(v, again))["complete"] and v._reserved == {}  # and a finished upload: it is in the cache now
    assert v.used_bytes() >= size


async def test_a_lease_counts_against_the_room_a_local_download_may_take(tmp_path):
    size = len(MP4)
    commons = Commons()
    v = queue(tmp_path, commons, local_downloads="on", video_cache_gb=1.5 * size / 2**30)
    commons.hold = asyncio.Event()
    v.start(LECTURES[0])  # the server's own
    v.start(LECTURES[1])
    for _ in range(100):
        if v._reserved:
            break
        await asyncio.sleep(0.01)
    assert await v.lease("mbp") is None and "beside the downloads" in rows(v.store)[1]["error"]
    commons.hold.set()
    await v.drain()


async def test_a_job_commons_will_not_describe_fails_a_try_and_the_next_one_is_leased(tmp_path):
    class Picky(Commons):
        def __call__(self, request):
            if request.url.path.endswith("/contents/asset7001"):
                return httpx.Response(503, text="busy")
            if request.url.path.endswith("/contents/asset7002"):
                return httpx.Response(200, json={"result": {"is_original_file_deleted": True}})
            if request.url.path.endswith("/contents/asset7003"):
                return httpx.Response(200, json={"result": {"original_file_length": 5 * 2**30, "extension": "mp4"}})
            return super().__call__(request)

    v = queue(tmp_path, Picky())
    for lec in LECTURES[:4]:
        v.start(lec)
    job = await v.lease("mbp")
    assert job["asset_id"] == "asset7004"  # the three before it could not be given away
    told = [(r["state"], r["attempts"], r["error"]) for r in rows(v.store)[:3]]
    assert told[0][:2] == ("queued", 1) and "HTTP 503" in told[0][2]  # may pass: it waits for another try
    assert told[1][0] == "failed" and "no longer has" in told[1][2]
    assert told[2][0] == "failed" and "ICAMPUS_VIDEO_MAX_GB" in told[2][2]  # the largest video allowed holds here too
    assert v._reserved == {"asset7004": len(MP4)} and list(v.leases) == ["asset7004"]


async def test_a_video_too_big_for_a_fetcher_is_left_in_its_place_for_someone_else(tmp_path):
    commons = Commons()
    v = queue(tmp_path, commons)
    v.start(LECTURES[0])
    v.start(LECTURES[1])
    assert await v.lease("mbp", free_bytes=1000) is None  # neither fits on that machine
    assert [(r["state"], r["attempts"], r["error"], r["fetcher"]) for r in rows(v.store)] == [("queued", 0, None, None)] * 2
    described = len(commons_seen := v._too_big["mbp"])
    assert described == 2 and await v.lease("mbp", free_bytes=1000) is None and len(commons_seen) == 2
    job = await v.lease("thinkpad", free_bytes=10**9)
    assert job["asset_id"] == "asset7001"  # still first in line for a fetcher that has the room
    assert (await v.lease("mbp", free_bytes=10**9))["asset_id"] == "asset7002"  # and for this one, once it has


async def test_a_restart_of_the_server_does_not_cost_a_fetcher_its_job(tmp_path):
    commons = Commons()
    first = queue(tmp_path, commons)
    first.start(LECTURES[0])
    first.start(LECTURES[1])
    job, other = await first.lease("mbp"), await first.lease("thinkpad")
    await first.receive(job["job_id"], job["lease_id"], start=0, length=1500, total=len(MP4), sha256=None,
                        body=pieces(MP4[:1500]))
    first.heartbeat(job["job_id"], job["lease_id"], phase="uploading", done=1500, total=len(MP4), speed=1000.0)
    # the other job was claimed but the server stopped before it had described the video
    first.store.execute("UPDATE video_jobs SET lease_info=NULL, lease_expires_at='2000-01-01' WHERE id=?",
                        (other["job_id"],))
    first.store.execute("UPDATE video_jobs SET lease_expires_at='2000-01-01' WHERE id=?", (job["job_id"],))
    await first.stop()

    second = queue(tmp_path, commons)  # the server comes back, later than the leases were good for
    second.tick()
    kept, back = rows(second.store)
    assert (kept["state"], kept["fetcher"], kept["attempts"]) == ("downloading", "mbp", 0)
    assert datetime.fromisoformat(kept["lease_expires_at"]) > datetime.now(KST) + timedelta(seconds=80)
    assert (back["state"], back["fetcher"], back["attempts"]) == ("queued", None, 0)
    assert second._reserved == {"asset7001": len(MP4)} and (tmp_path / "videos" / "asset7001.part").stat().st_size == 1500
    assert second.status(LECTURES[0])["by"] == "mbp" and second.fetcher_list()[0]["name"] == "mbp"
    assert second.received(job["job_id"], job["lease_id"]) == 1500
    with pytest.raises(StaleLease):
        second.received(other["job_id"], other["lease_id"])
    with pytest.raises(OutOfStep):
        await second.receive(job["job_id"], job["lease_id"], start=0, length=10, total=len(MP4), sha256=None,
                             body=pieces(MP4[:10]))
    done = await second.receive(job["job_id"], job["lease_id"], start=1500, length=len(MP4) - 1500, total=len(MP4),
                                sha256=SHA, body=pieces(MP4[1500:]))  # checked against the bytes read back from disk
    assert done["complete"] and second.path("asset7001").read_bytes() == MP4 and commons.downloads == []


async def test_a_wrong_hash_is_caught_after_a_restart_too(tmp_path):
    first = queue(tmp_path, Commons())
    first.start(LECTURES[0])
    job = await first.lease("mbp")
    await first.receive(job["job_id"], job["lease_id"], start=0, length=1500, total=len(MP4), sha256=None,
                        body=pieces(b"\0" * 1500))
    second = queue(tmp_path, Commons())
    with pytest.raises(Refused, match="SHA-256 mismatch"):
        await second.receive(job["job_id"], job["lease_id"], start=1500, length=len(MP4) - 1500, total=len(MP4),
                             sha256=SHA, body=pieces(MP4[1500:]))
    assert second.status(LECTURES[0])["state"] == "queued" and second.path("asset7001") is None


async def test_a_size_commons_did_not_give_is_taken_from_the_upload_within_the_cap(tmp_path):
    class Unsized(Commons):
        def __call__(self, request):
            answer = super().__call__(request)
            return httpx.Response(200, json={"result": {"extension": "mp4"}}) if "/contents/" in request.url.path else answer

    v = queue(tmp_path, Unsized(), video_max_gb=5000 / 2**30)
    v.start(LECTURES[0])
    job = await v.lease("mbp")
    assert job["expected_bytes"] is None and job["max_bytes"] == 5000
    assert (await upload(v, job))["complete"] and v.path("asset7001").read_bytes() == MP4
    v.start(LECTURES[1])
    job = await v.lease("mbp")
    with pytest.raises(Refused, match="ICAMPUS_VIDEO_MAX_GB"):
        await upload(v, job, MP4 * 2)  # bigger than the largest video allowed: refused before a byte is kept
    assert v.status(LECTURES[1])["state"] == "failed" and not list((tmp_path / "videos").glob("*.part"))


async def test_a_local_part_is_not_what_a_fetchers_upload_continues(tmp_path):
    commons = Commons(MP4 * 50)
    commons.hold = asyncio.Event()
    first = queue(tmp_path, commons, local_downloads="on")
    first.start(LECTURES[0])
    for _ in range(200):
        if first.active and (tmp_path / "videos" / "asset7001.part").exists():
            break
        await asyncio.sleep(0.01)
    await first.stop()  # the server's own try was interrupted
    (tmp_path / "videos" / "asset7001.part").write_bytes(b"left by the server's own try")
    second = queue(tmp_path, commons, local_downloads="off")
    assert await second.lease("mbp", free_bytes=10) is None  # too big for that machine: left as it was,
    assert (tmp_path / "videos" / "asset7001.part").exists()  # with what the server's try had
    job = await second.lease("mbp")
    assert second.received(job["job_id"], job["lease_id"]) == 0  # the upload starts a file of its own
    assert (await upload(second, job, MP4 * 50))["complete"] and second.path("asset7001").read_bytes() == MP4 * 50


# --- bench targets and the schema ---------------------------------------------------------------------------------

def test_bench_targets_are_an_asset_id_and_a_host_the_biggest_first(tmp_path):
    store = Store(tmp_path / "icampus.db")
    lectures = [lecture("7001"), lecture("7002"), lecture("7003"), lecture("9001", hosted=False)]
    lectures[1]["video"]["listed_size"] = 10 * len(MP4)
    lectures[2]["video"]["asset_id"] = "not/safe"
    seed(store, lectures)
    app = create_app(settings(tmp_path, sync_times="", fetcher_tokens=f"mbp:{MBP}"), store=store, background=False)
    app.state.videos._transport = httpx.MockTransport(lambda request: pytest.fail("a GET asked the content server"))
    with TestClient(app) as c:
        body = c.get("/api/v1/fetchers/bench-targets", headers=bearer(MBP)).json()
        assert body == {"count": 2, "downloading": 0, "items": [{"asset_id": "asset7002", "host": "lcms.skku.edu"},
                                                               {"asset_id": "asset7001", "host": "lcms.skku.edu"}]}
        assert c.get("/api/v1/fetchers/bench-targets?count=1", headers=bearer(TOKEN)).json()["count"] == 1
        assert c.get("/api/v1/fetchers/bench-targets?count=99", headers=bearer(TOKEN)).status_code == 422


def test_a_version_4_database_gets_the_lease_columns_and_keeps_its_queue(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(SCHEMA + SCHEMA_V2 + SCHEMA_V3 + SCHEMA_V4 + "PRAGMA user_version = 4;")
    old.execute("INSERT INTO video_jobs (asset_id, item_id, lecture, state, queued_at, updated_at) VALUES "
                "('asset7001', '7001', ?, 'queued', '2026-11-01T10:00:00.000000+09:00', 't')",
                (json.dumps(LECTURES[0]),))
    old.commit()
    old.close()

    store = Store(path)
    assert store.upgraded_from == 4 and store.schema_version == len(MIGRATIONS) == 5
    columns = {r["name"] for r in store.query("PRAGMA table_info(video_jobs)")}
    assert {"fetcher", "lease_id", "lease_expires_at", "lease_info"} <= columns
    row = dict(store.one("SELECT * FROM video_jobs"))
    assert (row["asset_id"], row["state"], row["fetcher"], row["lease_id"]) == ("asset7001", "queued", None, None)
    store.execute("UPDATE video_jobs SET fetcher='mbp'")
    store.execute("PRAGMA user_version = 1")  # what version 1 of the program did at every start
    store.close()
    again = Store(path)  # every step runs again and must find its work done
    assert again.upgraded_from == 1 and again.one("SELECT fetcher FROM video_jobs")["fetcher"] == "mbp"
    again.close()
    assert Store(path).upgraded_from is None
