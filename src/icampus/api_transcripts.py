"""REST routes for speech-to-text providers, transcripts, the job queue, search and lecture statistics.

Any API token may read, queue and cancel: queueing can spend money, but only inside the providers' caps. Changing
providers or their keys, and testing one (a paid call, however small), needs an admin-labelled token. No route here
returns an API key.
"""

from types import SimpleNamespace
from typing import Annotated, Any, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from . import videos
from .pipeline import ACTIVE
from .stt.registry import Unavailable

ItemId = Annotated[str, Path(pattern=r"^\d+$", description="the lecture's item_id")]
ProviderId = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9._-]{1,62}$")]
ModelParam = Annotated[str | None, Query(pattern=r"^[a-z0-9][a-z0-9._-]{1,62}$",
                                         description="a provider id from /api/v1/stt/models; omit for automatic routing")]
Language = Annotated[str | None, Query(pattern=r"^[A-Za-z]{2,3}$", description="ISO 639-1, e.g. ko")]
JobState = Literal["queued", "downloading", "extracting", "transcribing", "done", "failed", "paused_budget",
                   "canceled", "active"]
Kind = Literal["video", "material", "embedded_resource", "quiz", "assignment", "exam", "unknown"]


def lecture_stats(ctx: SimpleNamespace, course_ids: set[str] | None) -> dict:
    """Per course and in total: videos, hours, what can be exported, what is transcribed, and what the rest would
    cost with each provider."""
    states = ctx.pipeline.states()
    courses = {c["id"]: c for c in ctx.store.records("courses")}
    per: dict[str, dict] = {}
    for row in ctx.store.records("lectures", course_ids=course_ids):
        if row.get("kind") != "video":
            continue
        c = per.setdefault(row["course_id"], {
            "course_id": row["course_id"], "course": (courses.get(row["course_id"]) or {}).get("name"),
            "course_code": (courses.get(row["course_id"]) or {}).get("code"), "videos": 0, "hours": 0.0,
            "exportable_videos": 0, "exportable_hours": 0.0, "exportable_gb": 0.0, "transcribed_videos": 0,
            "transcribed_hours": 0.0, "outdated": 0, "in_queue": 0, "failed": 0})
        hours = (row.get("duration_min") or 0) / 60
        c["videos"] += 1
        c["hours"] += hours
        if not row.get("video"):
            continue
        c["exportable_videos"] += 1
        c["exportable_hours"] += hours
        c["exportable_gb"] += (row["video"].get("listed_size") or 0) / 2**30
        state = states.get(row["item_id"], {})
        if state.get("state") == "ready" or state.get("has") == "ready":
            c["transcribed_videos"] += 1
            c["transcribed_hours"] += hours
        if state.get("state") == "outdated":
            c["outdated"] += 1
        elif state.get("state") in ACTIVE:
            c["in_queue"] += 1
        elif state.get("state") == "failed":
            c["failed"] += 1
    items = sorted(per.values(), key=lambda c: c["course"] or "")
    total = {k: sum(c[k] for c in items) for k in ("videos", "hours", "exportable_videos", "exportable_hours",
                                                   "exportable_gb", "transcribed_videos", "transcribed_hours",
                                                   "outdated", "in_queue", "failed")}
    for c in [*items, total]:
        for k in ("hours", "exportable_hours", "exportable_gb", "transcribed_hours"):
            c[k] = round(c[k], 1)
    left = round(max(0.0, total["exportable_hours"] - total["transcribed_hours"]), 1)
    estimates = []
    for cfg in ctx.registry.all():
        if not cfg.enabled:
            continue
        budget = ctx.registry.budget(cfg)
        estimates.append({"model": cfg.id, "cost_usd": ctx.registry.cost(cfg, left, ctx.registry.usage(cfg)["hours"]),
                          "cap_left_hours": budget["left_hours"], "resets": budget["resets"]})
    return {"count": len(items), "total": total, "untranscribed_hours": left, "cost_to_transcribe_the_rest": estimates,
            "items": items}


def register(app: FastAPI, ctx: SimpleNamespace) -> None:
    api, admin = ctx.api, [Depends(ctx.admin)]
    registry, pipeline, transcripts, store = ctx.registry, ctx.pipeline, ctx.transcripts, ctx.store

    def lecture_video(item_id: str) -> dict:
        lecture = videos.find(store, item_id)
        if lecture is None:
            raise HTTPException(404, "no synced lecture video with this item_id is hosted on SKKU's content server "
                                     "(YouTube and Zoom videos can't be transcribed)")
        return lecture

    def unavailable(exc: Unavailable) -> HTTPException:
        return HTTPException(409, {"error": str(exc), "working_models": exc.working})

    # --- providers ---------------------------------------------------------

    @app.get("/api/v1/stt/models", dependencies=api, tags=["transcripts"])
    async def stt_models() -> dict:
        """The registered speech-to-text providers: what each is good for, what it costs per audio hour, whether
        it returns word timestamps, its languages, its health, and how much of this month's cap is left. Without
        `model`, a transcription uses the first routing rule that matches, else `default`, else the fallbacks."""
        items = [registry.describe(c) for c in registry.all()]
        return {"count": len(items), "default": registry.default_id(),
                "fallback_order": ctx.settings.stt_fallback_order, "routing_rules": ctx.settings.stt_routing_rules,
                "min_timestamps": ctx.settings.stt_min_timestamps, "items": items}

    @app.get("/api/v1/stt/providers", dependencies=admin, tags=["transcripts"])
    async def stt_providers() -> dict:
        """The providers with their whole config, for editing. A key is reported as set or not, with its source
        and last four characters; never the key."""
        return {"items": [registry.describe(c, admin=True) for c in registry.all()]}

    def save(config: dict, create: bool) -> dict:
        try:
            cfg = registry.save(config, create=create)
        except ValueError as exc:
            raise HTTPException(422, {"error": "invalid provider config",
                                      "details": getattr(exc, "fields", {"config": str(exc)})}) from None
        return registry.describe(cfg, admin=True)

    @app.post("/api/v1/stt/providers", dependencies=admin, tags=["transcripts"], status_code=201)
    async def stt_provider_add(config: dict[str, Any]) -> dict:
        """Register a provider (docs/stt-providers.md has examples). The key is not part of the config: name the
        environment variable that holds it (`api_key_env`), or store one with PUT .../key."""
        return save(config, True)

    @app.put("/api/v1/stt/providers/{provider_id}", dependencies=admin, tags=["transcripts"])
    async def stt_provider_update(provider_id: ProviderId, config: dict[str, Any]) -> dict:
        """Replace a provider's config. Fields left out go back to their defaults."""
        if registry.get(provider_id) is None:
            raise HTTPException(404, "no such provider")
        return save({**config, "id": provider_id}, False)

    @app.delete("/api/v1/stt/providers/{provider_id}", dependencies=admin, tags=["transcripts"])
    async def stt_provider_delete(provider_id: ProviderId) -> dict:
        """Remove a provider and its stored key. Transcripts it made stay."""
        if not registry.delete(provider_id):
            raise HTTPException(404, "no such provider")
        return {"id": provider_id, "deleted": True}

    @app.put("/api/v1/stt/providers/{provider_id}/key", dependencies=admin, tags=["transcripts"])
    async def stt_provider_key(provider_id: ProviderId, key: Annotated[str, Body(embed=True, min_length=8,
                                                                                 max_length=400)]) -> dict:
        """Store a provider's API key on the server (var/stt_keys.json, readable by the service user only). It is
        write-only: nothing returns it. An environment variable named in the config still wins over it."""
        cfg = registry.get(provider_id)
        if cfg is None:
            raise HTTPException(404, "no such provider")
        registry.set_key(provider_id, key)
        return {"id": provider_id, "key_status": registry.key_status(cfg)}

    @app.delete("/api/v1/stt/providers/{provider_id}/key", dependencies=admin, tags=["transcripts"])
    async def stt_provider_key_forget(provider_id: ProviderId) -> dict:
        cfg = registry.get(provider_id)
        if cfg is None:
            raise HTTPException(404, "no such provider")
        registry.set_key(provider_id, None)
        return {"id": provider_id, "key_status": registry.key_status(cfg)}

    @app.post("/api/v1/stt/providers/{provider_id}/test", dependencies=admin, tags=["transcripts"])
    async def stt_provider_test(provider_id: ProviderId) -> dict:
        """Send the bundled five-second clip of synthesized speech to the provider and record the outcome as its
        health. A real (tiny) paid call."""
        if registry.get(provider_id) is None:
            raise HTTPException(404, "no such provider")
        return {"id": provider_id, **(await registry.test(provider_id)),
                "health": registry.health_state(provider_id)}

    # --- transcripts -------------------------------------------------------

    def chosen_lectures(course: str | None, week: int | None, kind: str | None) -> list[dict]:
        return [x for x in store.records("lectures", course_ids=ctx.course_ids(course))
                if (week is None or x.get("week_no") == week) and (kind is None or x.get("kind") == kind)]

    @app.post("/api/v1/transcripts/batch", dependencies=api, tags=["transcripts"])
    async def transcripts_batch(course: str | None = None, week: int | None = Query(None, ge=0),
                                kind: Kind = "video", model: ModelParam = None, only_missing: bool = True,
                                confirm: bool = False) -> JSONResponse:
        """Queue every exportable lecture video that matches. Without `confirm=true` nothing is queued: the answer
        is the plan (how many lectures and hours, what each model would cost, how much of its cap is left, how
        much would be downloaded). With it, the jobs are queued and run in the background, a few at a time."""
        lectures = chosen_lectures(course, week, kind)
        if not confirm:
            plan = pipeline.plan(lectures, model=model, only_missing=only_missing)
            return JSONResponse({"dry_run": True, **plan, "items": ctx.with_course(plan["items"]),
                                 "hint": "repeat with confirm=true to queue these"})
        try:
            result = pipeline.queue(lectures, model=model, only_missing=only_missing)
        except Unavailable as exc:
            raise unavailable(exc) from None
        return JSONResponse({"dry_run": False, **result, "items": ctx.with_course(result["items"])}, status_code=202)

    @app.get("/api/v1/transcripts/status", dependencies=api, tags=["transcripts"])
    async def transcripts_status(item_id: Annotated[str | None, Query(pattern=r"^\d+$")] = None,
                                 course: str | None = None) -> dict:
        """With item_id: where that lecture's transcript stands. Otherwise the queue (counts per state, hours
        waiting), this month's use per provider, and the jobs that are running, waiting or recently failed,
        for one course or all."""
        if item_id:
            return pipeline.status(lecture_video(item_id))
        ids = ctx.course_ids(course)
        open_jobs = pipeline.jobs(states=ACTIVE, course_ids=ids, limit=60)
        failed = pipeline.jobs(states=("failed",), course_ids=ids, limit=10)
        return {"queue": pipeline.queue_summary(),
                "month": {c.id: registry.budget(c) for c in registry.all() if c.enabled},
                "jobs": ctx.with_course(open_jobs), "recently_failed": ctx.with_course(failed)}

    @app.post("/api/v1/transcripts/{item_id}", dependencies=api, tags=["transcripts"])
    async def transcript_request(item_id: ItemId, model: ModelParam = None, language: Language = None,
                                 force: bool = False, estimate_only: bool = False) -> JSONResponse:
        """Transcribe a lecture video: 200 when a transcript of the current video already exists, 202 with the
        job while it runs (ask again to follow it). Downloads the video unless it is cached, extracts the audio
        and sends it to the provider, which may cost money within that provider's cap. `model` names a provider;
        without it the routing rules and defaults choose. `force` makes a new transcript even though one exists;
        it does so once, and a forced request repeated within 15 minutes of that transcript answers with it
        instead of paying again. `estimate_only` only says what would happen and what it would cost."""
        lecture = lecture_video(item_id)
        if estimate_only:
            return JSONResponse(pipeline.estimate(lecture, model=model, language=language))
        try:
            body = pipeline.request(lecture, model=model, language=language.lower() if language else None,
                                    force=force)
        except Unavailable as exc:
            raise unavailable(exc) from None
        return JSONResponse(body, status_code=200 if body["state"] in ("ready", "outdated") else 202)

    @app.get("/api/v1/transcripts/{item_id}", dependencies=api, tags=["transcripts"])
    async def transcript_read(item_id: ItemId, model: ModelParam = None,
                              format: Literal["text", "json", "vtt", "srt"] = "text",
                              offset: int = Query(0, ge=0), max_chars: int = Query(15000, ge=200, le=200000),
                              start_s: float | None = Query(None, ge=0), end_s: float | None = Query(None, ge=0)):
        """A lecture's transcript: the preferred one, or the one a named provider made. `text` is paragraphs that
        start with a [mm:ss] marker, `max_chars` at a time from `offset`; `json` is the segments (and
        `transcripts`, every transcript the lecture has); `vtt` and `srt` are subtitle files. `start_s`/`end_s`
        cut out a stretch. Lecture content: private, for the account owner's own study."""
        lecture = lecture_video(item_id)
        row = transcripts.preferred(item_id, model)
        if row is None:
            status = pipeline.status(lecture, model)
            raise HTTPException(404, {"error": "there is no transcript of this lecture yet"
                                      + (f" by {model}" if model else ""), "state": status["state"],
                                      "job": status.get("job")})
        if format in ("vtt", "srt"):
            body = transcripts.vtt(row, start_s, end_s) if format == "vtt" else transcripts.srt(row, start_s, end_s)
            return PlainTextResponse(body, media_type="text/vtt" if format == "vtt" else "application/x-subrip",
                                     headers={"Content-Disposition": f'attachment; filename="{item_id}.{format}"'})
        head = {"item_id": item_id, "title": lecture.get("title"), **ctx.with_course([{
            "course_id": lecture.get("course_id")}])[0], "week": lecture.get("week"),
            **transcripts.summary(row)}
        if format == "json":
            return {**head, "segments": [{"start": a, "end": b, "text": t}
                                         for a, b, t in transcripts.segments(row, start_s, end_s)],
                    "transcripts": [transcripts.summary(r) for r in transcripts.rows(item_id)]}
        return {**head, **transcripts.page(row, offset=offset, max_chars=max_chars, start_s=start_s, end_s=end_s)}

    @app.put("/api/v1/transcripts/{item_id}/preferred", dependencies=api, tags=["transcripts"])
    async def transcript_prefer(item_id: ItemId, model: ModelParam = None) -> dict:
        """Make the named provider's transcript the one that is read for this lecture."""
        row = transcripts.preferred(item_id, model)
        if row is None or not transcripts.prefer(row["id"]):
            raise HTTPException(404, "this lecture has no transcript" + (f" by {model}" if model else ""))
        return {"item_id": item_id, "preferred": transcripts.summary(transcripts.get(row["id"]))}

    # --- jobs --------------------------------------------------------------

    @app.get("/api/v1/jobs", dependencies=api, tags=["transcripts"])
    async def jobs(state: JobState | None = None, course: str | None = None,
                   item_id: Annotated[str | None, Query(pattern=r"^\d+$")] = None,
                   limit: int = Query(100, ge=1, le=1000)) -> dict:
        """The transcription queue: jobs that are running or waiting first, in the order they will run, then
        finished ones, newest first. `state=active` is everything not finished."""
        states = ACTIVE if state == "active" else (state,) if state else None
        items = pipeline.jobs(states=states, item_id=item_id, course_ids=ctx.course_ids(course), limit=limit)
        return {"count": len(items), "queue": pipeline.queue_summary(), "items": ctx.with_course(items)}

    @app.post("/api/v1/jobs/{job_id}/retry", dependencies=api, tags=["transcripts"])
    async def job_retry(job_id: int) -> dict:
        """Put a failed or canceled job back in the queue."""
        try:
            job = pipeline.retry(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if job is None:
            raise HTTPException(404, "no such job")
        return job

    @app.delete("/api/v1/jobs/{job_id}", dependencies=api, tags=["transcripts"])
    async def job_cancel(job_id: int) -> dict:
        """Cancel a job that hasn't finished. Finished jobs are returned as they are."""
        job = pipeline.cancel(job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        return job

    # --- search and statistics ---------------------------------------------

    @app.get("/api/v1/search", dependencies=api, tags=["transcripts"])
    async def search(q: str = Query(min_length=1, max_length=200), course: str | None = None,
                     week: int | None = Query(None, ge=0), limit: int = Query(10, ge=1, le=100)) -> dict:
        """Search what was said in transcribed lectures. Every word of `q` must appear in the same sentence, as a
        substring and regardless of case; this works for Korean, including two-character words. Each hit has the
        lecture, the moment (`start_s`, `at`), the sentence with its neighbours, and the provider (`model`) whose
        transcript it is from."""
        lectures = {x["item_id"]: x for x in store.records("lectures", include_inactive=True) if x.get("item_id")}
        wanted = None
        if course or week is not None:
            ids = ctx.course_ids(course)
            wanted = {i for i, x in lectures.items() if (ids is None or x["course_id"] in ids)
                      and (week is None or x.get("week_no") == week)}
        hits = transcripts.search(q, item_ids=wanted, limit=limit)
        for hit in hits:
            hit["week"] = (lectures.get(hit["item_id"]) or {}).get("week")
        return {"query": q, "count": len(hits), "search": "trigram index" if transcripts.fts else "substring scan",
                "items": ctx.with_course(hits)}

    @app.get("/api/v1/lectures/stats", dependencies=api, tags=["data"])
    async def stats(course: str | None = None) -> dict:
        """Lecture videos per course and in total: how many, how many hours, how much of that is hosted on SKKU's
        content server (exportable, in hours and GB), how much is transcribed, and what transcribing the rest
        would cost with each provider."""
        at, stale = ctx.synced(("lectures",))
        return {"synced_at": at, "stale": stale, **lecture_stats(ctx, ctx.course_ids(course))}
