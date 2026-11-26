"""The admin UI: who gets in and for how long, CSRF on everything that changes something, pages that never ask
iCampus for anything, no secret or unescaped foreign text in the HTML, and the forms doing what they say."""

import json
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

import icampus.admin as admin
from helpers import ADMIN, MP4, OTHER_SECRET, SECRET, TOKEN, WHISPER, Commons, Stt, fake_audio, lecture, seed, settings
from icampus import browser, files, sync
from icampus.api import create_app
from icampus.store import Store
from icampus.stt import Segment, Transcript

EVIL = '<img src=x onerror=alert(1)><script>alert("x")</script>'
LECTURES = [lecture("7001", week=1, title=f"1주차 {EVIL}"), lecture("7002", week=2), lecture("9001", hosted=False)]
PAGES = ["/admin/", "/admin/settings", "/admin/stt", "/admin/stt/new", "/admin/stt/el/edit", "/admin/jobs",
         "/admin/jobs?state=failed", "/admin/jobs/table", "/admin/cache", "/admin/cache?kind=video&sort=size&order=asc",
         "/admin/lecture/7001", "/admin/lecture/7002", "/admin/attachment/701"]


def make(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("TEST_EL_KEY", SECRET)
    store = Store(tmp_path / "icampus.db")
    seed(store, LECTURES)
    store.set_state("collect_version", 99)
    app = create_app(settings(tmp_path, sync_times="", password="hunter2-hunter2", **env), store=store,
                     background=False)
    app.state.commons, app.state.stt = Commons(), Stt()
    app.state.videos._transport = httpx.MockTransport(app.state.commons)
    app.state.registry.transport = httpx.MockTransport(app.state.stt)
    app.state.registry.save(WHISPER, create=True)
    fake_audio(monkeypatch, tmp_path)
    # what is on disk: a video, a transcript with awkward text, and an attachment
    (tmp_path / "videos").mkdir(exist_ok=True)
    (tmp_path / "videos" / "asset7001.mp4").write_bytes(MP4)
    (tmp_path / "videos" / "asset7001.json").write_text(json.dumps({
        "asset_id": "asset7001", "file": "asset7001.mp4", "item_id": "7001", "course_id": "1001", "title": f"1주차 {EVIL}",
        "name": "x.mp4", "format": "mp4", "content_type": "video/mp4", "synced_updated_at": "20260301120000"}))
    (tmp_path / "files").mkdir(exist_ok=True)
    for suffix, content in ((".bin", b"%PDF-1.4 not really"), (".txt", f"시험 범위 {EVIL}".encode()),
                            (".json", json.dumps({"id": "701", "name": f"notice {EVIL}.pdf", "size": 19,
                                                  "content_type": "text/html", "extract": "pdf",
                                                  "fetched_at": "2026-11-01T09:00:00+09:00"}).encode())):
        (tmp_path / "files" / f"701{suffix}").write_bytes(content)
    app.state.cache.reconcile()
    app.state.transcripts.save(LECTURES[0], Transcript("ko", 600.0, [
        Segment(1.0, 4.0, f"첫 문장 {EVIL}"), Segment(75.0, 80.0, "둘째 문장")], None, "el", "scribe_v3"),
        video_version="20260301120000", reason="test")
    return app


@pytest.fixture
def app(tmp_path, monkeypatch):
    return make(tmp_path, monkeypatch)


@pytest.fixture
def anon(app):
    with TestClient(app, base_url="https://admin.test", follow_redirects=False) as c:
        yield c


def sign_in(c: TestClient, token: str = ADMIN) -> httpx.Response:
    page = c.get("/admin/login")
    login_token = BeautifulSoup(page.text, "html.parser").find("input", {"name": "login_token"})["value"]
    return c.post("/admin/login", data={"token": token, "login_token": login_token})


@pytest.fixture
def ui(anon):
    """A signed-in browser. `ui.csrf` is the session's token, `ui.submit` posts a form with it."""
    assert sign_in(anon).status_code == 303
    anon.csrf = BeautifulSoup(anon.get("/admin/").text, "html.parser").find("input", {"name": "csrf"})["value"]

    def submit(path: str, **fields) -> httpx.Response:
        return anon.post(path, data={"csrf": anon.csrf, **fields})
    anon.submit = submit
    return anon


def visible(response: httpx.Response) -> str:
    """The page as a reader sees it: text only, entities decoded."""
    return BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)


def flash(c: TestClient, path: str = "/admin/") -> str:
    return " ".join(n.get_text(" ", strip=True) for n in BeautifulSoup(c.get(path).text, "html.parser").select(".notice"))


# --- signing in ----------------------------------------------------------------------------------

def test_every_page_needs_a_session(anon):
    for path in PAGES + ["/admin/media/video/7001", "/admin/media/attachment/701", "/admin/media/transcript/1.vtt"]:
        r = anon.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/admin/login", path
    assert anon.get("/admin").status_code == 307
    assert anon.get("/admin/jobs/table", headers={"HX-Request": "true"}).headers["hx-redirect"] == "/admin/login"
    assert anon.get("/admin/static/theme.css").status_code == 200  # the sign-in page needs its stylesheet
    assert anon.get("/admin/static/../__init__.py").status_code in (404, 400)
    assert anon.get("/admin/", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 303  # a cookie, not a bearer
    anon.cookies.set("icampus_admin", ADMIN, domain="admin.test", path="/admin")
    assert anon.get("/admin/").status_code == 303  # the token itself is not a session


def test_only_an_admin_token_signs_in(anon):
    page = anon.get("/admin/login")
    assert page.status_code == 200 and "Paste an admin API token" in page.text
    wrong = sign_in(anon, "not-a-token-at-all-123456")
    assert wrong.status_code == 401 and "isn't one of this server's API tokens" in visible(wrong)
    not_admin = sign_in(anon, TOKEN)  # the MCP container's token is valid for the API, but not an admin one
    assert not_admin.status_code == 401 and "isn't an admin token" in visible(not_admin)
    assert "icampus_admin" not in anon.cookies and anon.get("/admin/").status_code == 303
    ok = sign_in(anon)
    assert ok.status_code == 303 and ok.headers["location"] == "/admin/"
    cookie = ok.headers["set-cookie"]
    assert cookie.startswith("icampus_admin=") and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Secure" in cookie and "Path=/admin" in cookie and "Max-Age=43200" in cookie
    assert ADMIN not in cookie and anon.get("/admin/").status_code == 200
    assert anon.get("/admin/login").status_code == 303  # already signed in
    assert anon.get("/api/v1/courses").status_code == 401  # the session opens the UI, not the API


def test_the_sign_in_form_has_its_own_token_and_cross_site_posts_are_refused(anon):
    assert anon.post("/admin/login", data={"token": ADMIN}).status_code == 403
    assert anon.post("/admin/login", data={"token": ADMIN, "login_token": "123.abc"}).status_code == 403
    old = admin.time.time() - 3600
    stale = f"{int(old)}.{anon.app.state.admin_sessions._sign('login', str(int(old)))}"
    assert anon.post("/admin/login", data={"token": ADMIN, "login_token": stale}).status_code == 403  # an hour old
    token = BeautifulSoup(anon.get("/admin/login").text, "html.parser").find("input", {"name": "login_token"})["value"]
    for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                    {"Origin": "https://evil.example"}, {"Referer": "https://evil.example/page"}):
        r = anon.post("/admin/login", data={"token": ADMIN, "login_token": token}, headers=headers)
        assert r.status_code == 403 and "set-cookie" not in r.headers, headers
    for headers in ({"Sec-Fetch-Site": "same-origin", "Origin": "null"},  # Chrome under a strict referrer policy
                    {"Origin": "https://admin.test"}, {}):
        with TestClient(anon.app, base_url="https://admin.test", follow_redirects=False) as fresh:
            token = BeautifulSoup(fresh.get("/admin/login").text, "html.parser").find(
                "input", {"name": "login_token"})["value"]
            assert fresh.post("/admin/login", data={"token": ADMIN, "login_token": token},
                              headers=headers).status_code == 303, headers


def test_failed_sign_ins_are_limited(anon):
    for _ in range(5):
        assert sign_in(anon, "wrong-wrong-wrong-wrong-1").status_code == 401
    blocked = sign_in(anon)  # even the right token has to wait now
    assert blocked.status_code == 429 and "Too many failed sign-ins" in blocked.text


def test_a_session_ends_on_sign_out_on_expiry_and_cannot_be_forged(ui, monkeypatch):
    cookie = ui.cookies["icampus_admin"]
    sid, signature = cookie.split(".")
    assert ui.get("/admin/").status_code == 200

    def with_cookie(value: str) -> int:
        with TestClient(ui.app, base_url="https://admin.test", follow_redirects=False) as other:
            other.cookies.set("icampus_admin", value, domain="admin.test", path="/admin")
            return other.get("/admin/").status_code
    assert with_cookie(cookie) == 200
    assert with_cookie(f"{sid}.{'0' * 64}") == 303 and with_cookie(f"someone-else.{signature}") == 303
    assert with_cookie(sid) == 303

    assert ui.submit("/admin/logout").status_code == 303
    assert with_cookie(cookie) == 303  # the copied cookie died with the session, not just in this browser

    assert sign_in(ui).status_code == 303
    later = time.time() + 13 * 3600
    monkeypatch.setattr(admin.time, "time", lambda: later)
    assert ui.get("/admin/").status_code == 303  # twelve hours is the default


def test_plain_http_needs_the_setting_and_the_page_says_so(tmp_path, monkeypatch):
    app = make(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://100.64.0.10:9013", follow_redirects=False) as c:
        assert "plain HTTP" in c.get("/admin/login").text and "Secure" in sign_in(c).headers["set-cookie"]
    relaxed = make(tmp_path / "b", monkeypatch, admin_cookie_secure=False)
    with TestClient(relaxed, base_url="http://100.64.0.10:9013", follow_redirects=False) as c:
        assert "plain HTTP" not in c.get("/admin/login").text
        assert "Secure" not in sign_in(c).headers["set-cookie"] and c.get("/admin/").status_code == 200
    off = make(tmp_path / "c", monkeypatch, admin_ui=False)
    with TestClient(off) as c:
        assert c.get("/admin/").status_code == 404 and c.get("/admin/login").status_code == 404
        assert c.get("/health").status_code == 200


def test_a_session_is_only_as_good_as_the_token_that_opened_it(tmp_path, monkeypatch):
    """Sessions are kept in the database, so they survive a restart. A token that was rotated out, or whose
    label is no longer an admin one, must not."""
    app = make(tmp_path, monkeypatch)
    with TestClient(app, base_url="https://admin.test", follow_redirects=False) as c:
        sign_in(c)
        cookie = c.cookies["icampus_admin"]
        assert c.get("/admin/settings").status_code == 200

    def after_restart(**env) -> int:
        config = settings(tmp_path, sync_times="", **env)
        again = create_app(config, store=Store(tmp_path / "icampus.db"), background=False)
        with TestClient(again, base_url="https://admin.test", follow_redirects=False) as other:
            other.cookies.set("icampus_admin", cookie, domain="admin.test", path="/admin")
            return other.get("/admin/settings").status_code
    assert after_restart() == 200  # the same token and label: the session carries over
    assert after_restart(admin_labels="owner") == 303  # "admin" is no longer an admin label
    assert after_restart() == 303  # and that ended the session for good
    with TestClient(app, base_url="https://admin.test", follow_redirects=False) as c:
        sign_in(c)
        cookie = c.cookies["icampus_admin"]
    assert after_restart(api_tokens=f"mcp:{TOKEN},admin:{'z' * 24}") == 303  # the token was rotated


def test_odd_bytes_in_a_cookie_or_token_are_refused_not_a_server_error(ui):
    """A browser can send any bytes. They must fail the comparison, not crash it."""
    with TestClient(ui.app, base_url="https://admin.test", follow_redirects=False) as other:
        for value in ("sid.ünïcödé", "☃.☃", "a.b.c", "."):
            headers = {"Cookie": b"icampus_admin=" + value.encode("utf-8")}
            assert other.get("/admin/", headers=headers).status_code == 303, value
            assert other.get("/admin/login", headers=headers).status_code == 200, value
        assert sign_in(other, "töken-that-is-not-ascii-1234").status_code == 401
    assert ui.post("/admin/settings", data={"csrf": "snöwman"}).status_code == 403
    assert ui.post("/admin/settings", headers={"X-CSRF-Token": "café".encode("utf-8")}).status_code == 403
    assert ui.get("/admin/").status_code == 200  # and the real session is untouched


# --- CSRF ----------------------------------------------------------------------------------------

def test_every_request_that_changes_something_needs_the_sessions_csrf_token(ui):
    posts = sorted({r.path for r in ui.app.routes if getattr(r, "methods", None) and "POST" in r.methods
                    and r.path.startswith("/admin") and r.path != "/admin/login"})
    assert len(posts) >= 10 and "/admin/settings" in posts and "/admin/cache/{kind}/{key}/{action}" in posts
    assert not [r.path for r in ui.app.routes if r.path.startswith("/admin") and getattr(r, "methods", None)
                and r.methods & {"PUT", "DELETE", "PATCH"}]  # nothing changes state any other way
    with TestClient(ui.app, base_url="https://admin.test", follow_redirects=False) as other:
        sign_in(other)
        foreign = BeautifulSoup(other.get("/admin/").text, "html.parser").find("input", {"name": "csrf"})["value"]
    assert foreign != ui.csrf
    fill = {"provider_id": "el", "action": "disable", "job_id": "1", "kind": "video", "key": "asset7001",
            "item_id": "7001"}
    for path in posts:
        url = re.sub(r"\{(\w+)\}", lambda m: fill[m.group(1)], path)
        for data in ({}, {"csrf": ""}, {"csrf": "0" * 64}, {"csrf": foreign}):
            r = ui.post(url, data=data)
            assert r.status_code == 403 and "security token" in r.text, (url, data)
        assert ui.post(url, data={"csrf": ui.csrf}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403, url
    assert ui.app.state.registry.get("el").enabled and ui.app.state.cache.get("video", "asset7001")  # untouched
    assert ui.get("/admin/").status_code == 200  # and the session survived all of it
    by_header = ui.post("/admin/stt/el/disable", headers={"X-CSRF-Token": ui.csrf})  # how htmx sends it
    assert by_header.status_code == 303 and not ui.app.state.registry.get("el").enabled


def test_every_form_carries_a_token_and_no_page_needs_inline_code(ui):
    for path in PAGES + ["/admin/login"]:
        client = ui if path != "/admin/login" else TestClient(ui.app, base_url="https://admin.test")
        r = client.get(path)
        assert r.status_code == 200, path
        if path != "/admin/jobs/table":
            assert "default-src 'none'" in r.headers["content-security-policy"], path
            assert "unsafe-inline" not in r.headers["content-security-policy"]
            assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
        soup = BeautifulSoup(r.text, "html.parser")
        for form in soup.find_all("form"):
            if (form.get("method") or "get").lower() == "post":
                assert form.find("input", {"name": "csrf"}) or form.find("input", {"name": "login_token"}), (path, form)
        # the Content-Security-Policy allows no inline script or style, so the pages must not rely on any
        assert not [s for s in soup.find_all("script") if not s.get("src")], path
        assert not soup.find_all("style") and not soup.find_all(style=True), path
        assert not [t for t in soup.find_all(True) if any(a.startswith("on") for a in t.attrs)], path
        for tag in soup.find_all(["script", "link", "img", "video", "audio", "track", "source"]):
            assert (tag.get("src") or tag.get("href") or "/").startswith("/"), (path, tag)  # nothing from elsewhere


# --- what the pages show ---------------------------------------------------------------------------

def test_looking_never_asks_icampus_or_the_content_server_for_anything(ui, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a page view opened an iCampus session")
    for module in (files, browser, sync):
        monkeypatch.setattr(module, "open_session", forbidden)
    refuse = httpx.MockTransport(lambda request: pytest.fail(f"a page view sent a request to {request.url.host}"))
    ui.app.state.videos._transport = refuse
    ui.app.state.registry.transport = refuse
    for path in PAGES:
        assert ui.get(path).status_code == 200, path
    assert ui.get("/admin/media/video/7001", headers={"Range": "bytes=4-7"}).content == b"ftyp"
    assert ui.get("/admin/media/attachment/701").content == b"%PDF-1.4 not really"
    assert ui.get("/admin/media/transcript/1.vtt").text.startswith("WEBVTT")
    assert ui.get("/admin/attachment/999").status_code == 404  # not on disk: it says so instead of fetching it
    assert ui.get("/admin/media/video/7002").status_code == 404 and ui.get("/admin/lecture/123456").status_code == 404
    assert ui.get("/api/v1/cache", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert ui.app.state.store.recent_runs(5) == []


def test_foreign_text_is_escaped_and_secrets_never_reach_the_html(ui):
    ui.app.state.registry.set_key("whisper", OTHER_SECRET)
    ui.app.state.pipeline.request(LECTURES[1])
    seen = ""
    for path in PAGES:
        html = ui.get(path).text
        seen += html
        assert "<script>alert" not in html and "<img src=x" not in html, path
    assert "&lt;script&gt;alert" in seen and "onerror=alert(1)&gt;" in seen  # shown as text, where it appears
    for secret in (SECRET, OTHER_SECRET, TOKEN, ADMIN, "hunter2"):
        assert secret not in seen
    assert "set · …1234" in seen and "set · …5678" in seen  # the last four characters, and no more
    assert "from TEST_EL_KEY" in seen and "stored on the server" in seen
    settings_page = BeautifulSoup(ui.get("/admin/settings").text, "html.parser").get_text(" ", strip=True)
    assert "ICAMPUS_PASSWORD set" in settings_page and "ICAMPUS_API_TOKENS admin, mcp" in settings_page
    sent = ui.get("/admin/media/attachment/701")  # written by someone else: a download, never rendered
    assert sent.headers["content-type"] == "application/octet-stream" and "attachment" in sent.headers["content-disposition"]
    assert sent.headers["x-content-type-options"] == "nosniff" and "sandbox" in sent.headers["content-security-policy"]


def test_overview_cache_and_lecture_pages(ui):
    overview = BeautifulSoup(ui.get("/admin/").text, "html.parser")
    text = overview.get_text(" ", strip=True)
    for expected in ("Sync status", "Storage by kind", "Transcription queue", "Speech-to-text this month",
                     "never evicted", "trigram index"):
        assert expected in text, expected
    assert overview.select_one(".nav.on[aria-current=page]").get_text(strip=True) == "Overview"
    assert [a.get_text(strip=True) for a in overview.select(".side .nav")] == ["Overview", "Jobs", "Cache", "STT models",
                                                                             "Settings"]
    # shares are drawn as squares and <progress> bars: the policy allows no inline widths
    assert len(overview.select(".meter i")) == 20 and overview.select("progress.bar[max]")
    assert overview.select_one(".side form[action='/admin/logout'] button")["aria-label"] == "Sign out"
    assert [b["data-theme-set"] for b in overview.select(".side [data-theme-set]")] == ["system", "light", "dark"]

    rows = BeautifulSoup(ui.get("/admin/cache").text, "html.parser").select("tbody tr")
    kinds = sorted(r.select_one("td").get_text(strip=True) for r in rows)
    assert kinds == ["attachment", "transcript", "video"]
    only = BeautifulSoup(ui.get("/admin/cache?kind=video").text, "html.parser").select("tbody tr")
    assert len(only) == 1 and "4 kB" in only[0].get_text(" ", strip=True)
    actions = [b.get_text(strip=True) for b in only[0].select(".table-actions button, .table-actions a")]
    assert actions == ["Open", "Pin", "Transcribe", "Re-download", "Delete"]
    assert only[0].select_one("a")["href"] == "/admin/lecture/7001"
    assert "Nothing on disk matches" in ui.get("/admin/cache?kind=audio").text
    assert "Nothing on disk matches" in ui.get("/admin/cache?course=1002").text

    lecture_page = BeautifulSoup(ui.get("/admin/lecture/7001").text, "html.parser")
    assert lecture_page.select_one("video[data-player]")["src"] == "/admin/media/video/7001"
    assert lecture_page.select_one("track")["src"] == "/admin/media/transcript/1.vtt"
    lines = lecture_page.select("button.line")
    assert [(b["data-seek"], b.select_one(".line__time").get_text()) for b in lines] == [("1.0", "00:01"), ("75.0", "01:15")]
    without = BeautifulSoup(ui.get("/admin/lecture/7002").text, "html.parser")
    assert not without.select_one("video") and "isn't on this server" in without.get_text()
    assert "No transcript yet" in without.get_text()
    for fmt, start in (("vtt", "WEBVTT"), ("srt", "1\n00:00:01,000"), ("txt", "[00:01] 첫 문장"), ("json", "{")):
        assert ui.get(f"/admin/media/transcript/1.{fmt}").text.startswith(start)
    assert ui.get("/admin/media/transcript/1.exe").status_code == 404
    attachment = ui.get("/admin/attachment/701")
    assert "시험 범위" in attachment.text and "Nothing is fetched to show it" in attachment.text


async def test_a_page_that_loads_several_files_at_once_gets_all_of_them(app):
    """A lecture page asks for its video, subtitles and pictures in parallel. Each request checks the session
    in the database; found with a real browser, where two of three thumbnails failed."""
    import asyncio
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin.test") as c:
        page = await c.get("/admin/login")
        token = BeautifulSoup(page.text, "html.parser").find("input", {"name": "login_token"})["value"]
        assert (await c.post("/admin/login", data={"token": ADMIN, "login_token": token})).status_code == 303
        paths = ["/admin/media/video/7001", "/admin/media/transcript/1.vtt", "/admin/media/attachment/701",
                 "/admin/", "/admin/cache", "/admin/jobs/table"] * 12
        answers = await asyncio.gather(*(c.get(path) for path in paths))
        assert [r.status_code for r in answers] == [200] * len(paths)


# --- what the forms do -----------------------------------------------------------------------------

def test_settings_change_at_once_show_their_source_and_refuse_bad_values(ui):
    cache = ui.app.state.cache
    page = BeautifulSoup(ui.get("/admin/settings").text, "html.parser")
    keys = page.find("input", {"name": "video_cache_gb"}).find_parent("form").find("input", {"name": "_keys"})["value"]
    assert "video_cache_gb" in keys and "From default" in page.get_text(" ", strip=True)
    current = {i["name"]: i.get("value", "") for i in page.find("input", {"name": "video_cache_gb"}).find_parent(
        "form").find_all("input") if i.get("name") and i.get("type") != "hidden"}

    saved = ui.submit("/admin/settings", **{**current, "_keys": keys, "video_cache_gb": "12"})
    assert saved.status_code == 303 and "Saved: video_cache_gb." in flash(ui, "/admin/settings")
    assert cache.cap("video") == 12 * 2**30  # in force without a restart
    assert ui.app.state.settings.source("video_cache_gb") == "ui"
    assert ui.app.state.settings.source("video_max_gb") == "default"  # saving a page doesn't pin the rest
    again = BeautifulSoup(ui.get("/admin/settings").text, "html.parser")
    assert again.find("input", {"name": "video_cache_gb"})["value"] == "12.0" and "Set here" in again.get_text(" ")

    bad = ui.submit("/admin/settings", **{**current, "_keys": keys, "video_cache_gb": "-3", "attachment_max_mb": "9999"})
    assert bad.status_code == 422
    errors = [h.get_text(strip=True) for h in BeautifulSoup(bad.text, "html.parser").select(".field.bad .hint")]
    assert errors == ["Use a value from 0.1 to 4000", "Use a value from 1 to 200"]
    assert BeautifulSoup(bad.text, "html.parser").find("input", {"name": "video_cache_gb"})["value"] == "-3"  # kept to fix
    assert cache.cap("video") == 12 * 2**30

    assert ui.submit("/admin/settings/reset", key="video_cache_gb").status_code == 303
    assert cache.cap("video") == 30 * 2**30 and ui.app.state.settings.source("video_cache_gb") == "default"
    assert ui.submit("/admin/settings/reset", key="api_tokens").status_code == 404
    assert ui.submit("/admin/settings", _keys="api_tokens,password", api_tokens="x:y", password="z").status_code == 303
    assert "Nothing changed." in flash(ui, "/admin/settings")  # settings that stay in the environment are ignored

    jobs_form = again.find("input", {"name": "prefetch_limit"}).find_parent("form")
    fields = {i["name"]: i.get("value", "") for i in jobs_form.find_all("input")
              if i.get("name") and i.get("type") not in ("hidden", "checkbox")}
    ui.submit("/admin/settings", _keys=jobs_form.find("input", {"name": "_keys"})["value"],
            **{**fields, "prefetch_limit": "4", "stt_auto_transcribe": "1", "stt_auto_courses": "CSE1001, MTH2002"})
    live = ui.app.state.settings
    assert (live.prefetch_limit, live.stt_auto_transcribe, live.stt_auto_courses) == (4, True, ["CSE1001", "MTH2002"])
    assert live.keep_video_after_processing is False  # its switch was left off in this submission
    ui.submit("/admin/settings", _keys=jobs_form.find("input", {"name": "_keys"})["value"],
            **{**fields, "keep_video_after_processing": "1"})
    assert live.keep_video_after_processing is True and live.stt_auto_transcribe is False


def test_providers_are_added_edited_tested_and_removed_and_the_key_is_write_only(ui):
    registry = ui.app.state.registry
    form = {"id": "groq", "type": "openai_compatible", "name": "Groq", "model": "whisper-large-v3",
            "base_url": "https://api.groq.example/openai/v1", "description": "fast and cheap", "cost_per_hour": "0.111",
            "monthly_usd_cap": "5", "priority": "30", "concurrency": "2", "timeout_s": "600", "audio_format": "opus",
            "languages": "ko, en", "enabled": "1", "capabilities": '{"max_upload_mb": 100}', "params": "",
            "key": OTHER_SECRET, "api_key_env": ""}
    created = ui.submit("/admin/stt", **form)
    assert created.status_code == 303 and "groq saved." in flash(ui, "/admin/stt")
    cfg = registry.get("groq")
    assert (cfg.cost_per_hour, cfg.monthly_usd_cap, cfg.languages, cfg.capabilities, cfg.monthly_hours_cap) == (
        0.111, 5, ["ko", "en"], {"max_upload_mb": 100}, None)
    assert registry.key(cfg) == OTHER_SECRET
    edit = ui.get("/admin/stt/groq/edit").text
    assert OTHER_SECRET not in edit and "set · …5678 (stored)" in edit
    assert BeautifulSoup(edit, "html.parser").find("input", {"name": "key"}).get("value") == ""  # never prefilled

    bad = ui.submit("/admin/stt", **{**form, "id": "Bad Id", "base_url": "https://lcms.skku.edu/v1", "priority": "soon",
                                   "capabilities": "{not json", "api_key_env": "ICAMPUS_PASSWORD"})
    assert bad.status_code == 422 and OTHER_SECRET not in bad.text
    hints = " ".join(h.get_text() for h in BeautifulSoup(bad.text, "html.parser").select(".field.bad .hint"))
    assert "Use a whole number" in hints and "JSON object" in hints
    worse = ui.submit("/admin/stt", **{**form, "id": "Bad Id", "base_url": "https://lcms.skku.edu/v1",
                                     "api_key_env": "ICAMPUS_PASSWORD"})
    hints = " ".join(h.get_text() for h in BeautifulSoup(worse.text, "html.parser").select(".field.bad .hint"))
    assert "lowercase" in hints and "never sent to an SKKU host" in hints and "never sent to a provider" in hints
    assert registry.get("Bad Id") is None and len(registry.all()) == 3

    kept = ui.submit("/admin/stt/groq", **{**form, "description": "second opinion", "key": "", "enabled": "1"})
    assert kept.status_code == 303 and registry.get("groq").description == "second opinion"
    assert registry.key(registry.get("groq")) == OTHER_SECRET  # an empty key field keeps the stored key
    assert ui.submit("/admin/stt/nobody", **form).status_code == 404

    tested = ui.post("/admin/stt/whisper/test", headers={"X-CSRF-Token": ui.csrf, "HX-Request": "true"})
    assert tested.status_code == 200 and "Heard" in tested.text and "<html" not in tested.text  # only the cell
    ui.app.state.stt.broken.add("gpu.tailnet.example")
    assert ui.submit("/admin/stt/whisper/test").status_code == 303
    assert "whisper: whisper refused the API key" in flash(ui, "/admin/stt")

    assert ui.submit("/admin/stt/groq/disable").status_code == 303 and not registry.get("groq").enabled
    assert ui.submit("/admin/stt/groq/enable").status_code == 303 and registry.get("groq").enabled
    assert ui.submit("/admin/stt/groq/forget-key").status_code == 303 and registry.key(registry.get("groq")) is None
    assert ui.submit("/admin/stt/groq/delete").status_code == 303 and registry.get("groq") is None
    assert ui.submit("/admin/stt/groq/delete").status_code == 404 and ui.submit("/admin/stt/el/explode").status_code == 404


def test_a_provider_may_be_called_routing_or_new(ui):
    registry = ui.app.state.registry
    for name in ("routing", "new"):
        assert ui.submit("/admin/stt", **{"id": name, "type": "openai_compatible", "model": "m", "enabled": "1",
                                         "base_url": "http://gpu.tailnet.example:8000/v1", "audio_format": "opus",
                                         "capabilities": "", "params": ""}).status_code == 303
        assert ui.get(f"/admin/stt/{name}/edit").status_code == 200
        edited = ui.submit(f"/admin/stt/{name}", **{"type": "openai_compatible", "model": "m2", "enabled": "1",
                                                   "base_url": "http://gpu.tailnet.example:8000/v1",
                                                   "audio_format": "opus", "capabilities": "", "params": ""})
        assert edited.status_code == 303 and registry.get(name).model == "m2"
    assert ui.get("/admin/stt/new").status_code == 200  # the empty form, not the provider called "new"


def test_changing_a_providers_address_drops_its_stored_key_and_says_so(ui):
    registry = ui.app.state.registry
    registry.set_key("whisper", OTHER_SECRET)
    form = {"type": "openai_compatible", "model": "large-v3", "enabled": "1", "audio_format": "opus",
            "capabilities": '{"max_upload_mb": null}', "params": "", "key": ""}
    ui.submit("/admin/stt/whisper", **form, base_url="http://gpu.tailnet.example:8000/v1", description="reworded")
    assert registry.key(registry.get("whisper")) == OTHER_SECRET and "whisper saved." in flash(ui, "/admin/stt")
    ui.submit("/admin/stt/whisper", **form, base_url="https://elsewhere.example/v1")
    assert registry.key(registry.get("whisper")) is None
    assert "stored key was removed" in flash(ui, "/admin/stt")
    ui.submit("/admin/stt/whisper", **{**form, "key": OTHER_SECRET}, base_url="https://third.example/v1")
    assert registry.key(registry.get("whisper")) == OTHER_SECRET  # entered again with the new address: kept


def test_routing_is_edited_on_the_stt_page(ui):
    live = ui.app.state.settings
    saved = ui.submit("/admin/stt-routing", default="whisper", fallback="el", min_timestamps="word",
                    rule_course_0="MTH2002", rule_language_0="ko", rule_provider_0="el",
                    rule_course_1="", rule_language_1="en", rule_provider_1="whisper",
                    rule_course_2="CSE1001", rule_language_2="", rule_provider_2="")
    assert saved.status_code == 303 and "Routing saved." in flash(ui, "/admin/stt")
    assert (live.stt_default_provider, live.stt_fallback_order, live.stt_min_timestamps) == ("whisper", ["el"], "word")
    assert live.stt_routing_rules == [{"course": "MTH2002", "language": "ko", "provider": "el"},
                                      {"language": "en", "provider": "whisper"}]  # the row without a provider is dropped
    page = BeautifulSoup(ui.get("/admin/stt").text, "html.parser")
    assert page.find("select", {"name": "rule_provider_0"}).find("option", selected=True)["value"] == "el"
    assert page.find("select", {"name": "rule_course_0"}).find("option", selected=True)["value"] == "MTH2002"
    assert page.find("select", {"name": "rule_provider_3"}) is not None  # two empty rows after the two rules

    ui.submit("/admin/stt-routing", default="nobody", fallback="", min_timestamps="word", rule_provider_0="")
    assert "no provider is called nobody" in flash(ui, "/admin/stt") and live.stt_default_provider == "whisper"
    ui.submit("/admin/stt-routing", default="", fallback="", min_timestamps="letter", rule_provider_0="")
    assert "Not saved" in flash(ui, "/admin/stt") and live.stt_min_timestamps == "word"


async def test_queueing_a_course_shows_the_plan_first(ui):
    pipeline = ui.app.state.pipeline
    plan = ui.submit("/admin/jobs/queue", course="1001", week="", model="", only_missing="1")
    assert plan.status_code == 200
    text = BeautifulSoup(plan.text, "html.parser").get_text(" ", strip=True)
    assert "1 lecture(s), 0.2 h of audio" in text and "Automatic routing would start with el" in text
    assert "1 already transcribed" in text and "1 not hosted" in text and "Queue 1 lecture(s)" in text
    assert pipeline.jobs() == [] and ui.app.state.commons.downloads == []  # only a plan so far

    nothing = ui.submit("/admin/jobs/queue", course="1002", week="", model="", only_missing="1")
    assert "Nothing to queue with these choices." in nothing.text and "name=\"confirm\"" not in nothing.text
    queued = ui.submit("/admin/jobs/queue", course="1001", week="", model="", only_missing="1", confirm="1")
    assert queued.status_code == 303 and "Queued 1 lecture(s)" in flash(ui, "/admin/jobs")
    [job] = pipeline.jobs()
    assert (job["item_id"], job["state"], job["trigger"]) == ("7002", "queued", "ui")
    table = ui.get("/admin/jobs/table").text
    assert "hx-trigger=\"every 4s\"" in table and "Lecture 7002" in table and "</i>Queued</span>" not in table and ">Queued</span>" in table

    assert ui.submit(f"/admin/jobs/{job['id']}/cancel").status_code == 303
    assert pipeline.jobs()[0]["state"] == "canceled" and "Retry" in ui.get("/admin/jobs?state=canceled").text
    assert ui.submit(f"/admin/jobs/{job['id']}/retry").status_code == 303 and pipeline.jobs()[0]["state"] == "queued"
    ui.submit(f"/admin/jobs/{job['id']}/retry")
    assert "only failed or canceled jobs" in flash(ui, "/admin/jobs")
    assert ui.submit("/admin/jobs/999/retry").status_code == 404 and ui.submit("/admin/jobs/1/explode").status_code == 404
    unknown = ui.submit("/admin/jobs/queue", course="", week="", model="nobody", confirm="1")
    assert unknown.status_code == 303 and "Nothing queued" in flash(ui, "/admin/jobs")


def test_the_sidebar_counts_the_jobs_that_want_a_look(ui):
    def badge(path):
        found = BeautifulSoup(ui.get(path).text, "html.parser").select_one(".side .nav-badge")
        return found.get_text(" ", strip=True) if found else None
    assert badge("/admin/") is None  # nothing failed, nothing paused: no badge at all
    ui.submit("/admin/jobs/queue", course="1001", week="", model="", only_missing="1", confirm="1")
    assert badge("/admin/jobs") is None  # a queued job is on its way, not waiting for anyone
    ui.app.state.store.execute("UPDATE jobs SET state='failed', error='made up'")
    for path in ("/admin/", "/admin/cache", "/admin/settings", "/admin/lecture/7001"):
        assert badge(path) == "1 failed or paused for budget", path
    tabs = BeautifulSoup(ui.get("/admin/jobs?state=failed").text, "html.parser").select(".tabs a")
    assert [t.get_text(" ", strip=True) for t in tabs] == ["All 1", "Active 0", "Paused for budget 0", "Failed 1", "Done 0"]
    assert [t.get_text(strip=True) for t in tabs if "on" in t.get("class", [])] == ["Failed1"]


def test_every_icon_a_page_asks_for_exists():
    """A missing icon is a KeyError while rendering, so a page would answer 500: check the names, not the pages."""
    from icampus.admin.icons import NOTICE, PATHS, icon
    from icampus.cache import KINDS
    templates = Path(admin.__file__).parent / "templates"
    asked = {name for page in templates.glob("*.html") for name in re.findall(r'icon\("([a-z-]+)"', page.read_text())}
    asked |= {name for page in templates.glob("*.html") for name in re.findall(r'glyph="([a-z-]+)"', page.read_text())}
    assert len(asked) > 15 and asked <= set(PATHS), sorted(asked - set(PATHS))
    assert {glyph for *_, glyph in admin.NAV} | set(NOTICE.values()) | set(KINDS) <= set(PATHS)
    drawn = str(icon("home", 18, 1.8))
    assert drawn.startswith("<svg ") and 'width="18"' in drawn and "style=" not in drawn and "http" not in drawn


def test_cache_actions(ui):
    cache, pipeline = ui.app.state.cache, ui.app.state.pipeline
    assert ui.submit("/admin/cache/video/asset7001/pin", back="?kind=video").headers["location"] == "/admin/cache?kind=video"
    assert cache.get("video", "asset7001").pinned and "PINNED" in ui.get("/admin/cache").text.upper()
    ui.submit("/admin/cache/video/asset7001/unpin")
    assert not cache.get("video", "asset7001").pinned
    for elsewhere in ("https://evil.example/", "//evil.example", "?x=//evil.example", "/admin/../x"):
        assert ui.submit("/admin/cache/video/asset7001/pin", back=elsewhere).headers["location"] == "/admin/cache"

    assert ui.submit("/admin/cache/video/asset7001/transcribe").status_code == 303
    assert "transcript ready" in flash(ui, "/admin/cache") and pipeline.jobs() == []  # it already has one
    assert ui.app.state.commons.downloads == []
    cache.pin("video", "asset7001")
    assert ui.submit("/admin/cache/video/asset7001/redownload").status_code == 303  # asked for: the content server is used
    assert cache.get("video", "asset7001").pinned  # the copy and its pin stay until the new one has arrived
    for _ in range(200):  # the download runs in the server's queue: nothing has to ask for it again
        if ui.app.state.commons.downloads == ["asset7001"]:
            break
        time.sleep(0.02)
    assert ui.app.state.commons.downloads == ["asset7001"]
    assert ui.submit("/admin/cache/attachment/701/redownload").status_code == 404  # not something attachments offer
    assert ui.submit("/admin/cache/attachment/701/delete").status_code == 303
    assert cache.get("attachment", "701") is None and ui.get("/admin/attachment/701").status_code == 404
    assert ui.submit("/admin/cache/transcript/1/delete").status_code == 303
    assert ui.app.state.transcripts.rows() == [] and ui.app.state.transcripts.search("문장") == []
    ui.submit("/admin/cache/transcript/1/delete")
    assert "no longer on disk" in flash(ui, "/admin/cache")
    assert ui.submit("/admin/cache/secrets/x/delete").status_code == 303 and "no longer on disk" in flash(ui, "/admin/cache")


async def test_lecture_actions(ui):
    pipeline, transcripts = ui.app.state.pipeline, ui.app.state.transcripts
    assert ui.submit("/admin/lecture/7002/transcribe", model="whisper").status_code == 303
    assert pipeline.jobs()[0]["model"] == "whisper" and "Transcript: queued." in flash(ui, "/admin/lecture/7002")
    assert ui.submit("/admin/lecture/7001/transcribe", model="").status_code == 303 and len(pipeline.jobs()) == 1
    ui.submit("/admin/lecture/7001/transcribe", model="", force="1")
    assert len(pipeline.jobs()) == 1  # its transcript is minutes old: "again" right away doesn't pay twice
    ui.app.state.store.execute("UPDATE transcripts SET created_at='2026-01-01T00:00:00+09:00'")
    assert ui.submit("/admin/lecture/7001/transcribe", model="", force="1").status_code == 303 and len(pipeline.jobs()) == 2
    ui.submit("/admin/lecture/7002/transcribe", model="nobody")
    assert "no speech-to-text provider is called" in flash(ui, "/admin/lecture/7002")
    assert ui.submit("/admin/lecture/7001/prefer", transcript="1").status_code == 303 and transcripts.get(1)["preferred"]
    assert ui.submit("/admin/lecture/7002/download").status_code == 303  # asked for: the content server is used
    assert ui.submit("/admin/lecture/9001/transcribe").status_code == 404  # a YouTube lecture
    assert ui.submit("/admin/lecture/7002/explode").status_code == 404


# --- shipping --------------------------------------------------------------------------------------

def test_a_download_that_failed_is_shown_and_the_button_tries_again(ui):
    videos, commons = ui.app.state.videos, ui.app.state.commons

    def wait(done) -> None:
        for _ in range(200):
            if done():
                return
            time.sleep(0.02)
        pytest.fail("the download queue did not get there")
    commons.fail = [404]
    assert ui.submit("/admin/lecture/7002/download").status_code == 303
    assert "download queue" in flash(ui, "/admin/lecture/7002")
    wait(lambda: videos.summary()["failed"] == 1)
    page = visible(ui.get("/admin/lecture/7002"))
    assert "its download failed" in page and "The download failed: Commons refused the download (HTTP 404)" in page
    assert "Video downloads: 0 downloading · 0 queued · 1 failed (last: Commons refused" in visible(ui.get("/admin/"))
    assert ui.submit("/admin/lecture/7002/download").status_code == 303  # pressing the button is asking to retry
    wait(lambda: videos.summary()["ready"] == 2)
    assert commons.downloads == ["asset7002"] and "Video downloads" not in visible(ui.get("/admin/"))
    assert "isn't on this server" not in visible(ui.get("/admin/lecture/7002"))


def test_templates_and_static_files_are_part_of_the_package():
    here = Path(admin.__file__).parent
    for name in ("templates/base.html", "templates/login.html", "templates/_ui.html", "static/theme.css",
                 "static/htmx.min.js", "static/admin.js", "static/fonts/Geist-Variable.woff2",
                 "static/fonts/GeistMono-Variable.woff2", "static/fonts/Galmuri11-Bold.woff2",
                 "static/fonts/OFL-Geist.txt", "static/fonts/OFL-Galmuri.txt"):
        assert (here / name).is_file(), name
    css = (here / "static/theme.css").read_text()
    assert "--accent:" in css and "--canvas:" in css and '[data-theme="light"]' in css and '[data-theme="dark"]' in css
    tokens = css.split("/* ── 2.")[0]
    assert tokens.count("light-dark(") > 30 and "color-scheme: light dark" in tokens  # each colour once, both themes
    fonts = set(re.findall(r'url\("fonts/([^"]+)"\)', css))  # every font the stylesheet names is in the package
    assert fonts and all((here / "static/fonts" / name).is_file() for name in fonts)
    assert "http://" not in css and "https://" not in css and "@import" not in css  # no font or CDN requests
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css.split("/* ── 2.")[1])  # colours live in the token block only
    assert "https://" not in (here / "static/admin.js").read_text()


@pytest.mark.skipif(not shutil.which("uv"), reason="uv is not installed")
def test_the_wheel_ships_them(tmp_path):
    root = Path(__file__).resolve().parents[1]
    built = subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(root)], capture_output=True,
                           text=True, timeout=180)
    if built.returncode != 0:
        pytest.skip(f"the wheel could not be built here ({built.stderr.strip().splitlines()[-1][:120]})")
    names = zipfile.ZipFile(next(tmp_path.glob("*.whl"))).namelist()
    for name in ("icampus/admin/templates/base.html", "icampus/admin/templates/cache.html",
                 "icampus/admin/static/theme.css", "icampus/admin/static/htmx.min.js",
                 "icampus/admin/static/fonts/Geist-Variable.woff2", "icampus/admin/static/fonts/Galmuri11-Bold.woff2",
                 "icampus/admin/static/fonts/OFL-Galmuri.txt", "icampus/stt/test_clip.ogg"):
        assert name in names, name
