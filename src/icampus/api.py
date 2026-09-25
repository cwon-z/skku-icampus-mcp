"""REST API over the cached iCampus data. Every /api/v1 route needs a bearer token."""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import views
from .config import KST, Settings, parse_tokens
from .dates import term_year
from .store import Store
from .sync import DATASETS, Syncer

log = logging.getLogger(__name__)
VERSION = "0.1.0"
Kind = Literal["video", "material", "embedded_resource", "quiz", "assignment", "exam", "unknown"]


def create_app(settings: Settings | None = None, *, store: Store | None = None, background: bool = True) -> FastAPI:
    settings = settings or Settings()
    store = store or Store(settings.db_path)
    syncer = Syncer(settings, store)
    tokens = parse_tokens(settings.api_tokens)
    admins = {x.strip() for x in settings.admin_labels.split(",") if x.strip()}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if background:
            tasks.append(asyncio.create_task(syncer.scheduler()))
            if settings.kuma_push_url:
                tasks.append(asyncio.create_task(syncer.kuma_pusher()))
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="SKKU iCampus", version=VERSION, lifespan=lifespan,
                  description="Your iCampus tasks, assignments, announcements, lectures and grades, "
                              "as last synced. Times are Asia/Seoul.")
    app.state.store, app.state.syncer = store, syncer

    def auth(request: Request) -> str:
        if not tokens:
            raise HTTPException(503, "no API tokens configured (ICAMPUS_API_TOKENS)")
        header = request.headers.get("authorization", "")
        given = header[7:].strip().encode() if header.startswith("Bearer ") else b""
        for token, label in tokens.items():
            if given and hmac.compare_digest(given, token.encode()):
                return label
        raise HTTPException(401, "missing or wrong bearer token", headers={"WWW-Authenticate": "Bearer"})

    def synced(datasets: tuple[str, ...]) -> tuple[str | None, bool]:
        expected = store.get_state("expected_scopes", {})
        times = [store.synced_at(d, expected.get(d)) for d in datasets]
        at = None if None in times else min(times)
        stale = at is None or datetime.fromisoformat(at) < datetime.now(KST) - timedelta(hours=settings.stale_hours)
        return at, stale

    def envelope(datasets: tuple[str, ...], items: list[Any], limit: int | None = None) -> dict[str, Any]:
        at, stale = synced(datasets)
        body = {"synced_at": at, "stale": stale, "count": len(items), "items": items}
        if limit is not None and len(items) > limit:
            body.update(count=limit, total=len(items), truncated=True, items=items[:limit])
        return body

    def courses() -> list[dict]:
        return store.records("courses")

    def with_course(rows: list[dict]) -> list[dict]:
        by_id = {c["id"]: c for c in courses()}
        return [{**r, "course": (by_id.get(r["course_id"]) or {}).get("name"),
                 "course_code": (by_id.get(r["course_id"]) or {}).get("code")} for r in rows]

    def course_ids(course: str | None) -> set[str] | None:
        try:
            return views.resolve_course(courses(), course)
        except LookupError as exc:
            message, candidates = exc.args
            raise HTTPException(400, {"error": message, "candidates": candidates})

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        body = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError):
        errors = [f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors()]
        return JSONResponse({"error": "invalid parameters", "details": errors}, status_code=422)

    @app.get("/health", tags=["meta"])
    async def health() -> dict:
        return {"status": "ok", "version": VERSION}

    api = [Depends(auth)]

    @app.get("/api/v1/status", dependencies=api, tags=["meta"])
    async def status() -> dict:
        up, msg = syncer.health()
        attempts = [t for t in store.get_state("login_attempts", [])
                    if datetime.fromisoformat(t) > datetime.now(KST) - timedelta(hours=24)]
        expected = store.get_state("expected_scopes", {})
        return {
            "healthy": up, "summary": msg, "term": store.get_state("term"),
            "running": syncer.running, "next_run": syncer.next_run.isoformat() if syncer.next_run else None,
            "login": {"blocked": store.get_state("login_block"), "credential_logins_24h": len(attempts)},
            "datasets": {d: store.synced_at(d, expected.get(d)) for d in DATASETS},
            "runs": store.recent_runs(5),
        }

    @app.post("/api/v1/sync", tags=["meta"], status_code=202)
    async def sync(retry_login: bool = False, label: str = Depends(auth)):
        if retry_login and label not in admins:
            raise HTTPException(403, f"retry_login needs a token labelled one of {sorted(admins)}")
        result, retry_after = syncer.request(retry_login=retry_login)
        codes = {"started": 202, "running": 409, "cooldown": 429, "login_blocked": 423}
        body: dict[str, Any] = {"result": result}
        if retry_after:
            body["retry_after"] = retry_after
        if result == "login_blocked":
            body["hint"] = "fix the cause, then POST /api/v1/sync?retry_login=true with an admin token"
        return JSONResponse(body, status_code=codes[result])

    @app.get("/api/v1/courses", dependencies=api, tags=["data"])
    async def list_courses() -> dict:
        return envelope(("courses",), courses())

    @app.get("/api/v1/tasks", dependencies=api, tags=["data"])
    async def tasks(course: str | None = None, kind: Kind | None = None,
                    due_within_days: int = Query(14, ge=0), past_days: int = Query(0, ge=0),
                    include_done: bool = False, include_inactive: bool = False,
                    limit: int = Query(200, ge=1, le=1000)) -> dict:
        ids = course_ids(course)
        now = datetime.now(KST)
        merged = views.build_tasks(store.records("todos", include_inactive=include_inactive),
                                   store.records("assignments"), store.records("lectures"), courses(),
                                   now, term_year(store.get_state("term")))
        items = views.filter_tasks(merged, now=now, course_ids=ids, kind=kind, due_within_days=due_within_days,
                                   past_days=past_days, include_done=include_done, include_inactive=include_inactive)
        return envelope(("todos", "assignments", "lectures"), items, limit)

    @app.get("/api/v1/assignments", dependencies=api, tags=["data"])
    async def assignments(course: str | None = None, unsubmitted_only: bool = False) -> dict:
        items = store.records("assignments", course_ids=course_ids(course))
        if unsubmitted_only:
            items = [a for a in items if (a.get("submission") or {}).get("state") not in views.DONE_STATES]
        return envelope(("assignments",), with_course(items))

    @app.get("/api/v1/announcements", dependencies=api, tags=["data"])
    async def announcements(course: str | None = None, since_days: int = Query(30, ge=0),
                            unread_only: bool = False, limit: int = Query(50, ge=1, le=500)) -> dict:
        since = (datetime.now(KST) - timedelta(days=since_days)).isoformat()
        items = [{**{k: v for k, v in a.items() if k != "text"}, "preview": (a.get("text") or "")[:300]}
                 for a in store.records("announcements", course_ids=course_ids(course))
                 if (a.get("posted_at") or "") >= since and (not unread_only or a.get("read_state") == "unread")]
        items.sort(key=lambda a: a.get("posted_at") or "", reverse=True)
        return envelope(("announcements",), with_course(items), limit)

    @app.get("/api/v1/announcements/{announcement_id}", dependencies=api, tags=["data"])
    async def announcement(announcement_id: str) -> dict:
        for a in store.records("announcements", include_inactive=True):
            if a["id"] == announcement_id:
                return with_course([a])[0]
        raise HTTPException(404, "no such announcement")

    @app.get("/api/v1/lectures", dependencies=api, tags=["data"])
    async def lectures(course: str | None = None, week: int | None = Query(None, ge=0), kind: Kind | None = None,
                       incomplete_only: bool = False, available_only: bool = False, upcoming_only: bool = False,
                       include_attendance: bool = False, limit: int = Query(300, ge=1, le=2000)) -> dict:
        """incomplete_only keeps items iCampus reports as not completed (unknown completion is left out);
        available_only drops items that have not opened yet; upcoming_only drops items whose deadline
        (late period included) has passed. Soonest deadline first, undated last."""
        ids = course_ids(course)
        now = datetime.now(KST).isoformat()

        def deadline(x: dict) -> str | None:
            return max(filter(None, (x.get("due_at"), x.get("late_until"))), default=None)

        items = [x for x in store.records("lectures", course_ids=ids)
                 if (week is None or x.get("week_no") == week) and (kind is None or x.get("kind") == kind)
                 and (not incomplete_only or x.get("completed") is False)
                 and (not available_only or not x.get("available_from") or x["available_from"] <= now)
                 and (not upcoming_only or not deadline(x) or deadline(x) >= now)]
        items.sort(key=lambda x: (x.get("due_at") is None, x.get("due_at") or "", x["course_id"], x.get("week_no") or 0))
        body = envelope(("lectures",), with_course(items), limit)
        if include_attendance:
            body["attendance"] = with_course(store.records("attendance", course_ids=ids))
        return body

    @app.get("/api/v1/grades", dependencies=api, tags=["data"])
    async def grades(course: str | None = None) -> dict:
        ids = course_ids(course)
        chosen = [c for c in courses() if ids is None or c["id"] in ids]
        return envelope(("courses", "assignments"), views.grades(chosen, store.records("assignments", course_ids=ids)))

    @app.get("/api/v1/export", dependencies=api, tags=["data"])
    async def export() -> dict:
        return {
            "version": 1, "exported_at": datetime.now(KST).isoformat(), "term": store.get_state("term"),
            **{d: store.records(d, include_inactive=True) for d in DATASETS},
            "scopes": store.scopes(), "runs": store.recent_runs(20),
        }

    return app
