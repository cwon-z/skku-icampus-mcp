"""One-off live probe: which requests do the iCampus panels make, and can we call them directly?

Writes var/probe/<time>/report.md (shapes only, no values) and raw/ (mode 600, personal data,
gitignored; used to build parsers). Never opens items, never sends non-GET API calls.
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import Response

from .browser import CanvasError, NotLoggedIn, Session, clean_error, open_session, safe_path
from .config import CANVAS, KST, Settings
from .store import Store


def shape(value: Any, depth: int = 0) -> Any:
    """Keys and types only, so the report never carries personal values."""
    if depth > 3:
        return "…"
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [f"{len(value)} items", shape(value[0], depth + 1)] if value else []
    return type(value).__name__


class Recorder:
    def __init__(self, raw_dir: Path):
        self.raw_dir = raw_dir
        self.calls: list[dict[str, Any]] = []
        self.label = ""

    async def on_response(self, resp: Response) -> None:
        req = resp.request
        if req.resource_type not in ("xhr", "fetch", "document"):
            return
        url = urlsplit(req.url)
        headers = await req.all_headers()
        entry = {
            "panel": self.label, "method": req.method, "type": req.resource_type,
            "path": f"{url.hostname}{url.path}", "params": sorted({k for k, _ in parse_qsl(url.query)}),
            "status": resp.status, "ctype": (resp.headers.get("content-type") or "").split(";")[0],
            "auth_header": "authorization" in headers,
            "cookies": sorted({c.split("=")[0].strip() for c in headers.get("cookie", "").split(";") if c.strip()}),
            "frame": safe_path(req.frame.url) if req.frame else None,
            "url": req.url, "headers": headers,  # kept in memory only, for the direct-call test
        }
        if "json" in entry["ctype"] and req.method == "GET":
            try:
                body = await resp.json()
                entry["shape"] = shape(body)
                name = f"{len(self.calls):03d}-{self.label}-{url.path.strip('/').replace('/', '_')[:80]}.json"
                write_private(self.raw_dir / name, json.dumps(body, ensure_ascii=False, indent=1))
            except Exception:
                entry["shape"] = "unparseable"
        self.calls.append(entry)


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


async def wait_manual_login(s: Session, minutes: int = 10) -> None:
    await s.page.goto(f"{CANVAS}/login")
    print(">>> Sign in to iCampus in the browser window that just opened.", flush=True)
    for i in range(minutes * 20):
        if s.page.is_closed():
            raise RuntimeError("the browser window was closed before signing in")
        if await s.logged_in():
            await s.save_state()
            print(">>> Signed in. Reading list pages now; the window closes by itself.", flush=True)
            return
        # Signed in to the portal but not yet on Canvas: hop over (the SSO cookie does the rest).
        url = s.page.url
        if i % 5 == 4 and url.startswith("https://icampus.skku.edu") and "/xn-sso/login.php" not in url:
            await s._sso_hop()
        await asyncio.sleep(3)
    raise TimeoutError("no login within the time limit")


async def probe(settings: Settings, login: str, course: str | None) -> Path:
    from .collect import pick_term, term_start

    out = settings.data_dir / "probe" / datetime.now(KST).strftime("%Y%m%d-%H%M%S")
    raw = out / "raw"
    rec = Recorder(raw)
    report: list[str] = [f"# Probe {datetime.now(KST).isoformat(timespec='seconds')}", ""]
    store = Store(settings.data_dir / "probe.db")

    async with open_session(settings, store, headless=False) as s:
        s.page.on("response", lambda r: asyncio.ensure_future(rec.on_response(r)))

        # 1. login
        rec.label = "login"
        if login == "manual":
            how = "saved" if await s.logged_in() else (await wait_manual_login(s) or "manual")
        else:
            how = await s.ensure_login(allow_credentials=True)
        cookies = await s.context.cookies()
        report += ["## Login", f"- obtained via: {how}", "- cookies (name / domain / persistent / httpOnly):"]
        report += [f"  - {c['name']} / {c['domain']} / {c['expires'] > 0} / {c['httpOnly']}" for c in cookies]

        # 2. Canvas REST
        report += ["", "## Canvas API (GET)"]

        async def api(label: str, path: str, params: dict | None = None) -> Any:
            try:
                data = await s.canvas(path, params)
                report.append(f"- {label}: OK ({len(data) if isinstance(data, list) else 'object'})")
                write_private(raw / f"canvas-{label}.json", json.dumps(data, ensure_ascii=False, indent=1))
                report.append(f"  - shape: `{json.dumps(shape(data), ensure_ascii=False)[:600]}`")
                return data
            except (CanvasError, NotLoggedIn) as exc:
                report.append(f"- {label}: FAILED {clean_error(exc)}")
                return None

        courses = await api("courses", "/api/v1/courses", {
            "enrollment_state": "active", "include[]": ["term", "total_scores", "teachers"]}) or []
        term = pick_term(courses, settings.term)
        current = [c for c in courses if (c.get("term") or {}).get("name") == term]
        report.append(f"- detected term: {term} ({len(current)} of {len(courses)} courses)")
        report.append("- terms seen: " + ", ".join(sorted({(c.get('term') or {}).get('name') or '?' for c in courses})))
        cid = course or (current[0]["id"] if current else None)
        if cid:
            tabs = await api("tabs", f"/api/v1/courses/{cid}/tabs") or []
            report += [f"  - tab: {t.get('label')} -> {urlsplit(t.get('html_url', '')).path}" for t in tabs]
            await api("assignment_groups", f"/api/v1/courses/{cid}/assignment_groups",
                      {"include[]": ["assignments", "submission"]})
            await api("modules", f"/api/v1/courses/{cid}/modules", {"include[]": ["items"]})
        await api("announcements", "/api/v1/announcements", {
            "context_codes[]": [f"course_{c['id']}" for c in current], "start_date": term_start(current).isoformat(),
            "end_date": datetime.now(KST).date().isoformat()})
        views = await api("page_views", "/api/v1/users/self/page_views", {"per_page": 20})
        if views:
            report.append("  - recent logged paths: " + ", ".join(
                sorted({f"{v.get('http_method')} {urlsplit(v.get('url', '')).path}" for v in views[:20]})))

        # 3. can students create access tokens? (look, never click)
        rec.label = "settings"
        await s.page.goto(f"{CANVAS}/profile/settings", wait_until="domcontentloaded")
        has_token_ui = await s.page.locator(".add_access_token_link").count()
        report += ["", "## Access token", f"- 'new access token' control present: {bool(has_token_ui)}"]

        # 4. LearningX panels
        report += ["", "## LearningX panels"]
        panels = [("mypage", settings.mypage_path, "/learningx/lti/dashboard_v2")]
        if cid:
            tab_paths = {t.get("label", "").strip("• "): urlsplit(t.get("html_url", "")).path for t in tabs}
            for label, marker, key in (("강의콘텐츠", "/learningx/lti/modulebuilder", "lectures"),
                                       ("출결현황", "/learningx/lti/lecture_attendance", "attendance")):
                if tab_paths.get(label):
                    panels.append((key, tab_paths[label], marker))
                else:
                    report.append(f"- no '{label}' tab in course {cid}")
        for key, path, marker in panels:
            rec.label = key
            try:
                frame = await s.launch(path, marker)
                await asyncio.sleep(8)  # let the panel's own requests finish
                write_private(raw / f"{key}.html", await frame.content())
                report.append(f"- {key}: frame {safe_path(frame.url)} loaded; frames: "
                              + ", ".join(safe_path(f.url) for f in s.page.frames))
            except Exception as exc:
                report.append(f"- {key}: FAILED {clean_error(exc)}")

        # 5. can LearningX JSON be fetched directly with this session?
        report += ["", "## Requests made by pages (method path [params] status type auth cookies)"]
        tested: set[str] = set()
        for c in rec.calls:
            line = (f"- [{c['panel']}] {c['method']} {c['path']} {c['params']} {c['status']} {c['ctype']}"
                    f" auth_header={c['auth_header']} cookies={c['cookies']}")
            if c.get("shape") and c["path"] not in tested and "/learningx/" in c["path"]:
                tested.add(c["path"])
                hdr = {k: v for k, v in c["headers"].items() if k in ("authorization", "accept")}
                resp = await s.context.request.get(c["url"], headers=hdr, max_redirects=0)
                line += f" | direct GET: {resp.status}"
                line += f"\n  - shape: `{json.dumps(c['shape'], ensure_ascii=False)[:600]}`"
            report.append(line)

        report += ["", "## Guard", f"- blocked: {s.blocked or 'nothing'}"]
        await s.save_state()

    store.close()
    write_private(out / "report.md", "\n".join(report) + "\n")
    return out
