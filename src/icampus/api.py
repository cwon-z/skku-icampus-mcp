"""REST API over the cached iCampus data. Every /api/v1 route needs a bearer token."""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from types import SimpleNamespace

from . import admin as admin_ui
from . import api_transcripts, files, videos, views
from .browser import NotLoggedIn, clean_error
from .cache import CAPS, GB, Cache
from .config import KST, Settings, parse_tokens
from .dates import term_year
from .pipeline import Pipeline
from .runtime import SPECS, Runtime
from .store import Store
from .stt.registry import Registry
from .sync import DATASETS, Syncer
from .transcripts import Transcripts

log = logging.getLogger(__name__)
VERSION = "0.1.0"
Kind = Literal["video", "material", "embedded_resource", "quiz", "assignment", "exam", "unknown"]
Status = Literal["todo", "done", "missed", "upcoming", "unknown"]
FileId = Annotated[str, Path(pattern=r"^\d+$")]
ItemId = Annotated[str, Path(pattern=r"^\d+$", description="the lecture's item_id")]
CacheKind = Literal["video", "audio", "attachment", "transcript", "frames"]
CacheKey = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")]


def create_app(settings: Settings | None = None, *, store: Store | None = None, background: bool = True) -> FastAPI:
    env = settings or Settings()
    store = store or Store(env.db_path)
    settings = Runtime(env, store)  # the environment with the values changed in the admin UI on top
    syncer = Syncer(settings, store)
    tokens = parse_tokens(env.api_tokens)
    admins = {x.strip() for x in env.admin_labels.split(",") if x.strip()}
    cache = Cache(settings, store)
    video_cache = videos.Videos(settings, cache)
    cache.adopter(lambda: files.adopt(cache, settings, store))
    registry = Registry(settings, store)
    transcripts = Transcripts(settings, store, cache)
    pipeline = Pipeline(settings, store, cache, video_cache, registry, transcripts)
    cache.reconcile()  # adopts downloads made before the index existed; reads the disk only
    syncer.after_run.append(lambda status: pipeline.after_sync())
    loops: dict[str, asyncio.Task] = {}  # the background loops, by name

    def setting_changed(key: str) -> None:
        if key in CAPS.values() or key == "cache_total_gb":
            cache.enforce()  # a lowered cap applies now
        if key in ("sync_times", "sync_jitter_min") and "scheduler" in loops:
            loops.pop("scheduler").cancel()  # the running schedule sleeps towards a slot from the old times
            loops["scheduler"] = asyncio.get_running_loop().create_task(syncer.scheduler())
    settings.on_change(setting_changed)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if background:
            loops["scheduler"] = asyncio.create_task(syncer.scheduler())
            loops["upgrade"] = asyncio.create_task(syncer.upgrade_sync())  # at most one sync, after an upgrade
            loops["pipeline"] = asyncio.create_task(pipeline.run())  # the transcription queue
            if env.kuma_push_url:
                loops["kuma"] = asyncio.create_task(syncer.kuma_pusher())
            try:
                pipeline.after_sync()  # a sync by the CLI or an older version may have seen newer videos
            except Exception as exc:
                log.error("could not check transcripts against the synced videos: %s", clean_error(exc))
        yield
        for t in loops.values():
            t.cancel()
        loops.clear()
        await pipeline.stop()

    app = FastAPI(title="SKKU iCampus", version=VERSION, lifespan=lifespan,
                  description="Your iCampus tasks, assignments, announcements, lectures and grades, "
                              "as last synced. Times are Asia/Seoul.")
    app.state.store, app.state.syncer, app.state.videos = store, syncer, video_cache
    app.state.cache, app.state.settings = cache, settings
    app.state.registry, app.state.transcripts, app.state.pipeline = registry, transcripts, pipeline

    def auth(request: Request) -> str:
        if not tokens:
            raise HTTPException(503, "no API tokens configured (ICAMPUS_API_TOKENS)")
        header = request.headers.get("authorization", "")
        given = header[7:].strip().encode() if header.startswith("Bearer ") else b""
        for token, label in tokens.items():
            if given and hmac.compare_digest(given, token.encode()):
                return label
        raise HTTPException(401, "missing or wrong bearer token", headers={"WWW-Authenticate": "Bearer"})

    def admin(label: str = Depends(auth)) -> str:
        if label not in admins:
            raise HTTPException(403, f"this needs a token labelled one of {sorted(admins)} (ICAMPUS_ADMIN_LABELS)")
        return label

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

    def outdated_before(now: datetime) -> str:
        return (now - timedelta(hours=settings.stale_hours)).isoformat()

    def all_tasks(now: datetime, include_inactive: bool = False) -> list[dict]:
        return views.build_tasks(store.records("todos", include_inactive=include_inactive),
                                 store.records("assignments"), store.records("lectures"), courses(),
                                 now, term_year(store.get_state("term")), outdated_before=outdated_before(now))

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
            "transcription": {**pipeline.queue_summary(),
                              "search": "trigram index" if transcripts.fts else "substring scan (no FTS5 trigram)"},
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
    async def tasks(course: str | None = None, kind: Kind | None = None, status: Status | None = None,
                    due_within_days: int = Query(14, ge=0), past_days: int = Query(0, ge=0),
                    include_done: bool = False, include_inactive: bool = False,
                    limit: int = Query(200, ge=1, le=1000)) -> dict:
        """Each task has a status (todo, done, missed, upcoming, unknown) with status_reason and
        status_checked_at. Missed items lie in the past: combine status=missed with past_days."""
        ids = course_ids(course)
        now = datetime.now(KST)
        items = views.filter_tasks(all_tasks(now, include_inactive), now=now, course_ids=ids, kind=kind,
                                   status=status, due_within_days=due_within_days, past_days=past_days,
                                   include_done=include_done, include_inactive=include_inactive)
        return envelope(("todos", "assignments", "lectures"), items, limit)

    @app.get("/api/v1/assignments", dependencies=api, tags=["data"])
    async def assignments(course: str | None = None, unsubmitted_only: bool = False) -> dict:
        """The description is left out here: /assignments/{id} has it."""
        items = [{k: v for k, v in a.items() if k != "description"}
                 for a in store.records("assignments", course_ids=course_ids(course))]
        if unsubmitted_only:
            items = [a for a in items if (a.get("submission") or {}).get("state") not in views.DONE_STATES]
        return envelope(("assignments",), with_course(items))

    @app.get("/api/v1/assignments/{assignment_id}", dependencies=api, tags=["data"])
    async def assignment(assignment_id: str) -> dict:
        """One assignment or quiz: description, attached files, submission and its task status."""
        for a in store.records("assignments", include_inactive=True):
            if a["id"] == assignment_id:
                task = next((t for t in all_tasks(datetime.now(KST)) if t["key"] == a["key"]), {})
                return {**with_course([a])[0],
                        **{k: task[k] for k in ("status", "status_reason", "status_checked_at") if k in task}}
        raise HTTPException(404, "no such assignment")

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
        moment = datetime.now(KST)
        now = moment.isoformat()

        def deadline(x: dict) -> str | None:
            return max(filter(None, (x.get("due_at"), x.get("late_until"))), default=None)

        items = [x for x in store.records("lectures", course_ids=ids)
                 if (week is None or x.get("week_no") == week) and (kind is None or x.get("kind") == kind)
                 and (not incomplete_only or x.get("completed") is False)
                 and (not available_only or not x.get("available_from") or x["available_from"] <= now)
                 and (not upcoming_only or not deadline(x) or deadline(x) >= now)]
        items.sort(key=lambda x: (x.get("due_at") is None, x.get("due_at") or "", x["course_id"], x.get("week_no") or 0))
        items = views.lecture_status(items, moment, outdated_before(moment))
        states = pipeline.states()
        for x in items:
            if x.get("video"):  # only videos on SKKU's content server can be transcribed
                state = states.get(x["item_id"]) or {"state": "none"}
                x["transcript"] = {k: v for k, v in state.items() if v is not None}
        body = envelope(("lectures",), with_course(items), limit)
        if include_attendance:
            body["attendance"] = with_course(store.records("attendance", course_ids=ids))
        return body

    def attachment(file_id: str) -> dict:
        ref = files.find(store, file_id)
        if ref is None:
            raise HTTPException(404, "no synced announcement or assignment has this file")
        return ref

    async def fetch(ref: dict) -> files.Fetched:
        try:
            return await files.get(settings, store, ref, cache)
        except files.Busy as exc:
            raise HTTPException(409, str(exc)) from None
        except files.Unavailable as exc:
            raise HTTPException(422, str(exc)) from None
        except NotLoggedIn:
            raise HTTPException(503, "the saved iCampus login has expired; the next sync signs in again") from None
        except Exception as exc:  # Playwright errors carry request headers: only the cleaned message leaves here
            log.warning("file %s: %s", ref["id"], clean_error(exc))
            raise HTTPException(502, f"iCampus: {clean_error(exc)}") from None

    @app.get("/api/v1/files/{file_id}", dependencies=api, tags=["data"])
    async def file_text(file_id: FileId, offset: int = Query(0, ge=0),
                        max_chars: int = Query(20000, ge=1, le=100000)) -> dict:
        """A file attached to or linked from an announcement or assignment: its details and text, max_chars at a
        time from offset. The first request downloads it from iCampus, which records that as you opening it."""
        ref = attachment(file_id)
        got = await fetch(ref)
        text = got.text[offset:offset + max_chars]
        end = offset + len(text)
        return {**got.meta, "parent": ref["parent"], "total_chars": len(got.text), "offset": offset,
                "next_offset": end if end < len(got.text) else None, "text": text}

    @app.get("/api/v1/files/{file_id}/content", dependencies=api, tags=["data"])
    async def file_content(file_id: FileId) -> FileResponse:
        """The file itself (downloaded from iCampus on first use, like /files/{file_id})."""
        got = await fetch(attachment(file_id))
        return FileResponse(got.path, media_type=got.meta.get("content_type") or "application/octet-stream",
                            filename=got.meta.get("name") or file_id)

    def lecture_video(item_id: str) -> dict:
        lecture = videos.find(store, item_id)
        if lecture is None:
            raise HTTPException(404, "no synced lecture video with this item_id is hosted on SKKU's content server "
                                     "(YouTube and Zoom videos aren't; a sync after updating adds new ones)")
        return lecture

    @app.get("/api/v1/videos", dependencies=api, tags=["videos"])
    async def video_cache_list() -> dict:
        """Videos kept on this server, most recently used first, and how full the cache is."""
        items = [{k: m.get(k) for k in ("item_id", "course_id", "title", "format", "downloaded_at", "last_used")}
                 | {"size_mb": round((m.get("size") or 0) / 2**20)} for m in video_cache.cached()]
        return {"count": len(items), "used_gb": round(video_cache.used_bytes() / 2**30, 2),
                "cap_gb": settings.video_cache_gb, "items": with_course(items)}

    @app.get("/api/v1/videos/{item_id}", dependencies=api, tags=["videos"])
    async def video_status(item_id: ItemId) -> dict:
        """state: not_downloaded, queued, downloading, ready, outdated (a sync saw a newer version) or failed."""
        return video_cache.status(lecture_video(item_id))

    @app.post("/api/v1/videos/{item_id}", dependencies=api, tags=["videos"])
    async def video_export(item_id: ItemId) -> JSONResponse:
        """Download the lecture video to this server, unless it is cached and current. Returns at once (202 while
        it downloads, 200 when ready). The download goes to SKKU's content server without your login, so it
        records no viewing or attendance."""
        body = video_cache.start(lecture_video(item_id))
        return JSONResponse(body, status_code=200 if body["state"] == "ready" else 202)

    @app.get("/api/v1/videos/{item_id}/content", dependencies=api, tags=["videos"])
    async def video_content(item_id: ItemId) -> FileResponse:
        """The cached video file (supports Range, so players can seek). POST /videos/{item_id} first."""
        lecture = lecture_video(item_id)
        asset_id = lecture["video"]["asset_id"]
        path, meta = video_cache.path(asset_id), video_cache.meta(asset_id)
        if path is None or meta is None:
            state = video_cache.status(lecture)["state"]
            raise HTTPException(409 if state in ("queued", "downloading") else 404,
                                f"the video isn't on this server yet (state: {state})")
        video_cache.touch(asset_id)
        return FileResponse(path, media_type=meta["content_type"], filename=meta["name"])

    @app.delete("/api/v1/videos/{item_id}", dependencies=[Depends(admin)], tags=["videos"])
    async def video_delete(item_id: ItemId) -> dict:
        """Remove the cached copy (a later export downloads it again). Needs an admin token."""
        return {"deleted": video_cache.delete(lecture_video(item_id)["video"]["asset_id"])}

    # --- everything kept on disk -------------------------------------------

    def gb(n: int | None) -> float | None:
        return None if n is None else round(n / GB, 2)

    def cache_totals() -> dict:
        return {kind: {"count": t["count"], "used_gb": gb(t["bytes"]), "cap_gb": gb(t["cap_bytes"]),
                       "pinned_gb": gb(t["pinned_bytes"]), "in_use_gb": gb(t["in_use_bytes"])}
                for kind, t in cache.totals().items()}

    @app.get("/api/v1/cache", dependencies=api, tags=["cache"])
    async def cache_list(kind: CacheKind | None = None, course: str | None = None, pinned: bool | None = None,
                         sort: Literal["last_used", "size", "kind", "title", "created_at"] = "last_used",
                         order: Literal["asc", "desc"] = "desc", limit: int = Query(200, ge=1, le=2000)) -> dict:
        """What the server has on disk: videos, extracted audio, attachments, transcripts and frames, with the
        totals against each cap. Read from the index only; nothing is fetched from iCampus. `state` says why an
        entry is safe from eviction: pinned, in use (a queued or running job needs it) or kept (transcripts)."""
        held = cache.protected()
        items = [{"kind": e.kind, "key": e.key, "course_id": e.course_id, "item_id": e.item_id, "title": e.title,
                  "size_mb": round(e.size / 2**20, 1), "created_at": e.created_at, "last_used": e.last_used,
                  "pinned": e.pinned, "state": cache.state(e, held)}
                 for e in cache.entries(kind, course_ids=course_ids(course), pinned=pinned)]
        key = {"size": "size_mb"}.get(sort, sort)
        items.sort(key=lambda x: (x[key] is None, x[key] if x[key] is not None else ""), reverse=order == "desc")
        body = {"count": len(items), "totals": cache_totals(), "items": with_course(items)}
        if len(items) > limit:
            body.update(count=limit, total=len(items), truncated=True, items=body["items"][:limit])
        return body

    @app.post("/api/v1/cache/{kind}/{key}/pin", dependencies=api, tags=["cache"])
    async def cache_pin(kind: CacheKind, key: CacheKey) -> dict:
        """Keep this entry: eviction skips it until it is unpinned."""
        if not cache.pin(kind, key, True):
            raise HTTPException(404, "nothing cached under that kind and key")
        return {"kind": kind, "key": key, "pinned": True}

    @app.delete("/api/v1/cache/{kind}/{key}/pin", dependencies=api, tags=["cache"])
    async def cache_unpin(kind: CacheKind, key: CacheKey) -> dict:
        if not cache.pin(kind, key, False):
            raise HTTPException(404, "nothing cached under that kind and key")
        cache.enforce()  # it may be over a cap that only the pin was holding off
        return {"kind": kind, "key": key, "pinned": False}

    @app.delete("/api/v1/cache/{kind}/{key}", dependencies=[Depends(admin)], tags=["cache"])
    async def cache_delete(kind: CacheKind, key: CacheKey) -> dict:
        """Remove an entry and its files. Needs an admin token."""
        if not cache.delete(kind, key):
            raise HTTPException(404, "nothing cached under that kind and key")
        return {"kind": kind, "key": key, "deleted": True}

    # --- runtime settings ----------------------------------------------------

    @app.get("/api/v1/settings", dependencies=[Depends(admin)], tags=["settings"])
    async def settings_list() -> dict:
        """Every runtime setting: its value, where that comes from (ui, env or default) and its limits. Settings
        that stay in the environment are listed under `env_only`, secrets only as set or not set."""
        return {"items": settings.describe(), "env_only": settings.describe_env_only()}

    @app.put("/api/v1/settings", dependencies=[Depends(admin)], tags=["settings"])
    async def settings_update(changes: dict[str, Any]) -> dict:
        """Set runtime settings: {"video_cache_gb": 20}. A null value drops the override (back to the environment
        or the default). All of them are checked before any is stored."""
        from .runtime import coerce
        errors = {}
        for key, value in changes.items():
            if key not in SPECS:
                errors[key] = "not a runtime setting (settings that decide who gets in stay in the environment)"
            elif value is not None:
                try:
                    coerce(SPECS[key], value)
                except ValueError as exc:
                    errors[key] = str(exc)
        if errors:
            raise HTTPException(422, {"error": "invalid settings", "details": errors})
        for key, value in changes.items():
            settings.reset(key) if value is None else settings.set(key, value)
        return {"changed": sorted(changes), "items": [s for s in settings.describe() if s["key"] in changes]}

    ctx = SimpleNamespace(settings=settings, store=store, cache=cache, videos=video_cache, registry=registry,
                          transcripts=transcripts, pipeline=pipeline, syncer=syncer, api=api, auth=auth, admin=admin,
                          admins=admins, tokens=tokens, course_ids=course_ids, with_course=with_course,
                          synced=synced, courses=courses)
    app.state.ctx = ctx
    api_transcripts.register(app, ctx)
    admin_ui.register(app, ctx)  # the web UI at /admin, unless ICAMPUS_ADMIN_UI=false

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
