"""The admin UI and the MCP tools with fetchers in the picture: the Fetchers panel on the jobs page, the
local_downloads setting under Storage, who downloads what on the pages that show a download, and tool
descriptions that promise no speed. Made-up data only."""

import hashlib

import httpx
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from helpers import ADMIN, MP4, TOKEN, Commons, lecture, seed, settings
from icampus.api import create_app
from icampus.store import Store

MBP = "f" * 24
EVIL = '<img src=x onerror=alert(1)>'


def make(tmp_path, **env):
    store = Store(tmp_path / "icampus.db")
    seed(store, [lecture("7001", title=f"1주차 {EVIL}"), lecture("7002")])
    store.set_state("collect_version", 99)
    app = create_app(settings(tmp_path, sync_times="", **env), store=store, background=False)
    app.state.videos._transport = httpx.MockTransport(Commons())
    return app


def signed_in(app) -> TestClient:
    c = TestClient(app, base_url="https://admin.test", follow_redirects=False)
    c.__enter__()
    token = BeautifulSoup(c.get("/admin/login").text, "html.parser").find("input", {"name": "login_token"})["value"]
    assert c.post("/admin/login", data={"token": ADMIN, "login_token": token}).status_code == 303
    c.csrf = BeautifulSoup(c.get("/admin/").text, "html.parser").find("input", {"name": "csrf"})["value"]
    return c


@pytest.fixture
def ui(tmp_path):
    c = signed_in(make(tmp_path, fetcher_tokens=f"mbp:{MBP}", local_downloads="off"))
    yield c
    c.__exit__(None, None, None)


def fetcher(c, method: str, path: str, **kwargs) -> httpx.Response:
    return c.request(method, f"/api/v1/fetchers{path}", headers={"Authorization": f"Bearer {MBP}",
                                                                 **kwargs.pop("headers", {})}, **kwargs)


def text(response: httpx.Response, selector: str | None = None) -> str:
    soup = BeautifulSoup(response.text, "html.parser")
    return (soup.select_one(selector) if selector else soup).get_text(" ", strip=True)


def test_the_jobs_page_shows_who_fetches_and_what_is_being_downloaded(ui):
    page = ui.get("/admin/jobs")
    assert "No fetcher has called yet" in text(page, "#fetchers") and "local_downloads" in text(page, "#fetchers")
    panel = BeautifulSoup(page.text, "html.parser").select_one("#fetchers")
    assert panel["hx-get"] == "/admin/jobs/fetchers" and panel["hx-trigger"] == "every 4s"  # it refreshes by itself

    assert ui.post("/api/v1/videos/7001", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 202
    job = fetcher(ui, "POST", "/lease", json={"name": "mbp", "version": "0.1.0", "platform": f"macOS {EVIL}"}).json()
    fetcher(ui, "POST", f"/jobs/{job['job_id']}/progress", json={
        "lease_id": job["lease_id"], "phase": "downloading", "done_bytes": 300 * 2**20, "total_bytes": 900 * 2**20,
        "speed_bps": 4.5 * 2**20})
    panel = ui.get("/admin/jobs/fetchers")
    assert panel.status_code == 200 and "<img src=x" not in panel.text  # what a fetcher says of itself is text
    rows = [[td.get_text(" ", strip=True) for td in tr.select("td")]
            for tr in BeautifulSoup(panel.text, "html.parser").select("tbody tr")]
    who, what = rows
    assert who[0] == "mbp Online" and who[1] == f"macOS {EVIL}" and who[2] == "0.1.0" and who[4:] == ["1", "4.50"]
    assert what[0] == f"1주차 {EVIL}" and what[1:3] == ["mbp", "Downloading"] and what[4] == "4.50"
    assert "300 / 0 MB" not in panel.text and "300 MB" in what[3]  # the server knows the real size (a few kB here)
    for secret in (MBP, ADMIN, TOKEN, job["lease_id"]):
        assert secret not in panel.text and secret not in ui.get("/admin/jobs").text

    # the same on the pages that mention a download
    assert "by the fetcher mbp" in text(ui.get("/admin/lecture/7001"))
    assert "by mbp" in text(ui.get("/admin/"))

    fetcher(ui, "PUT", f"/jobs/{job['job_id']}/file", content=MP4[:2000],
            headers={"X-Lease-Id": job["lease_id"], "Content-Range": f"bytes 0-1999/{len(MP4)}"})
    fetcher(ui, "POST", f"/jobs/{job['job_id']}/progress", json={
        "lease_id": job["lease_id"], "phase": "uploading", "done_bytes": 2000, "total_bytes": len(MP4), "speed_bps": 2**20})
    assert "Uploading" in text(ui.get("/admin/jobs/fetchers")) and "being uploaded by the fetcher mbp" in text(
        ui.get("/admin/lecture/7001"))
    done = fetcher(ui, "PUT", f"/jobs/{job['job_id']}/file", content=MP4[2000:], headers={
        "X-Lease-Id": job["lease_id"], "Content-Range": f"bytes 2000-{len(MP4) - 1}/{len(MP4)}",
        "X-File-SHA256": hashlib.sha256(MP4).hexdigest()})
    assert done.json()["complete"] is True
    after = text(ui.get("/admin/jobs/fetchers"))
    assert "Downloading now" not in after and "mbp Online" in after
    assert BeautifulSoup(ui.get("/admin/lecture/7001").text, "html.parser").select_one("video[data-player]")


def test_the_panel_says_which_token_a_fetcher_is_and_when_it_rests(ui):
    from datetime import datetime, timedelta

    from icampus.config import KST
    videos = ui.app.state.videos
    fetcher(ui, "POST", "/lease", json={"name": "Minsus MacBook", "version": "0.1.0", "platform": "macOS"})
    row = text(ui.get("/admin/jobs/fetchers"), "tbody tr")
    assert "Minsus MacBook Online" in row and "token mbp" in row  # called one thing, known by its token
    videos._streak["mbp"], videos._rest["mbp"] = 3, datetime.now(KST) + timedelta(minutes=1)
    row = text(ui.get("/admin/jobs/fetchers"), "tbody tr")
    assert "Minsus MacBook Resting" in row and "3 failed tries in a row: no work for it until" in row


def test_the_panel_is_a_page_like_the_others(ui, tmp_path):
    with TestClient(ui.app, base_url="https://admin.test", follow_redirects=False) as anon:
        assert anon.get("/admin/jobs/fetchers").headers["location"] == "/admin/login"  # a session, like every page
        assert anon.get("/admin/jobs/fetchers", headers={"Authorization": f"Bearer {MBP}"}).status_code == 303
    ui.app.state.videos._transport = httpx.MockTransport(lambda request: pytest.fail("a page view asked the content server"))
    for path in ("/admin/jobs", "/admin/jobs/fetchers", "/admin/settings"):
        r = ui.get(path)
        soup = BeautifulSoup(r.text, "html.parser")
        assert r.status_code == 200 and "default-src 'none'" in r.headers["content-security-policy"], path
        assert not [s for s in soup.find_all("script") if not s.get("src")] and not soup.find_all(style=True), path
        assert not soup.find_all("style") and MBP not in r.text, path


def test_without_fetchers_the_jobs_page_is_as_it_was(tmp_path):
    c = signed_in(make(tmp_path))
    try:
        page = BeautifulSoup(c.get("/admin/jobs").text, "html.parser")
        assert page.select_one("#fetchers") is None and "Fetchers" not in page.get_text(" ", strip=True)
        assert page.select_one("#jobs-table") is not None
        assert "ICAMPUS_FETCHER_TOKENS not set" in text(c.get("/admin/settings"))
    finally:
        c.__exit__(None, None, None)


def test_local_downloads_is_a_storage_setting_and_tokens_show_as_names(ui):
    page = BeautifulSoup(ui.get("/admin/settings").text, "html.parser")
    select = page.find("select", {"name": "local_downloads"})
    assert [o["value"] for o in select.find_all("option")] == ["on", "fallback", "off"]
    assert select.find("option", selected=True)["value"] == "off"  # what this server was started with
    form = select.find_parent("form")
    assert form.find_parent("section").find("h2").get_text(strip=True) == "Storage"
    assert "ICAMPUS_FETCHER_TOKENS mbp" in page.get_text(" ", strip=True) and MBP not in str(page)

    keys = form.find("input", {"name": "_keys"})["value"]
    current = {i["name"]: i.get("value", "") for i in form.find_all("input") if i.get("name") and i.get("type") != "hidden"}
    saved = ui.post("/admin/settings", data={"csrf": ui.csrf, **current, "_keys": keys, "local_downloads": "fallback"})
    assert saved.status_code == 303 and ui.app.state.settings.local_downloads == "fallback"
    assert ui.app.state.videos.summary()["worker"]["local_downloads"] == "fallback"  # in force at once
    bad = ui.post("/admin/settings", data={"csrf": ui.csrf, **current, "_keys": keys, "local_downloads": "sometimes"})
    assert bad.status_code == 422 and "Use one of: on, fallback, off" in text(bad)
    assert ui.app.state.settings.local_downloads == "fallback"
    api = ui.put("/api/v1/settings", headers={"Authorization": f"Bearer {ADMIN}"}, json={"local_downloads": "on"})
    assert api.status_code == 200 and ui.app.state.settings.local_downloads == "on"
    assert ui.put("/api/v1/settings", headers={"Authorization": f"Bearer {ADMIN}"},
                  json={"fetcher_tokens": "x:" + "y" * 24}).status_code == 422  # who gets in is not a runtime setting


async def test_the_tool_descriptions_promise_no_speed_and_say_who_downloads():
    from icampus import mcp_server as server
    tools = {t.name: t.description for t in await server.mcp.list_tools()}
    everything = " ".join(tools.values())
    for promise in ("1 MB per second", "20 minutes", "a minute per 50 MB", "one video at a time"):
        assert promise not in everything and promise not in " ".join(everything.split()), promise
    for name in ("export_video", "export_videos", "sync_status"):
        assert "`by`" in tools[name] and "`phase`" in tools[name], name
    assert "speed_mbps" in tools["export_video"] and "don't promise the user a time" in " ".join(tools["export_video"].split())
    assert "`fetchers`" in tools["sync_status"] and "uploading" in tools["export_video"]
