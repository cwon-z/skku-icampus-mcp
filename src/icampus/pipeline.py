"""The transcription queue: lecture video -> audio -> pieces if a provider needs them -> transcript.

Jobs are rows in SQLite, so the queue survives a restart: a job caught mid-stage goes back to `queued`, and what
its earlier stages left on disk (the video, the audio) is found and reused. States: queued, downloading,
extracting, transcribing, done, failed (`retryable` says whether trying again unchanged could work),
paused_budget and canceled.

What keeps a backlog far larger than the video cache from filling the disk:
- at most `prefetch_limit` jobs are past `queued` at a time, so downloads never run far ahead of transcription;
- the budget is checked before a job downloads anything, and a job that doesn't fit waits in `paused_budget`;
- a video is only held for a job until its audio exists (`in_use`, which the cache manager asks before evicting).

Nothing here talks to canvas. The only request to SKKU is the video download in videos.py, which sends no cookies.
"""

import asyncio
import json
import logging
import shutil
import sqlite3
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import audio, frames, videos
from .cache import Cache, CacheFull, Entry
from .config import KST
from .store import Store
from .stt import TIMESTAMPS, SttError, Transcript, join
from .stt.registry import ProviderConfig, Registry, Unavailable
from .transcripts import Transcripts

log = logging.getLogger(__name__)
ACTIVE = ("queued", "downloading", "extracting", "transcribing", "paused_budget")
RUNNING = ("downloading", "extracting", "transcribing")
TICK_S = 5.0
PAUSED_CHECK_S = 30.0  # how often paused jobs are checked against their budget again
BACKOFF_MIN = (2, 10, 30, 60, 120)  # pause before the nth retry
PROVIDER_WAITS = 6  # how often a job waits for a provider to come back before it fails
BATCH_PRIORITY = 10  # single requests (0) go before batches
FORCE_AGAIN_MIN = 15  # `force` makes a new transcript once: asking again within this time gets that one


class _Retry(Exception):
    """This attempt failed in a way that may pass: try the job again later."""

    def __init__(self, message: str, *, limit: int, minutes: float | None = None):
        super().__init__(message)
        self.limit, self.minutes = limit, minutes


class _Fail(Exception):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class _Pause(Exception):
    """Over budget: wait for the next period or a higher cap."""


def _now() -> str:
    return datetime.now(KST).isoformat()


class Pipeline:
    def __init__(self, settings, store: Store, cache: Cache, video_cache: videos.Videos, registry: Registry,
                 transcripts: Transcripts):
        self.settings = settings
        self.store = store
        self.cache = cache
        self.videos = video_cache
        self.registry = registry
        self.transcripts = transcripts
        self.dir = Path(settings.data_dir) / "audio"
        self._tasks: dict[int, asyncio.Task] = {}
        self._progress: dict[int, dict] = {}
        self._partial: dict[int, dict[tuple, Transcript]] = {}  # pieces already paid for, kept across retries
        self._slots: dict[str, tuple[int, asyncio.Semaphore]] = {}
        self._ffmpeg = asyncio.Lock()  # one ffmpeg at a time
        self._wake = asyncio.Event()
        self._paused_checked = 0.0
        self._slide_jobs: dict[str, dict] = {}  # asset id -> {state, error, task} of an extraction asked for by hand
        cache.protect(self.in_use)
        # A restart: whatever was mid-stage starts that stage over; finished stages are found on disk.
        self.store.execute(f"UPDATE jobs SET state='queued', progress='{{}}', updated_at=? "
                           f"WHERE state IN ({','.join('?' * len(RUNNING))})", (_now(), *RUNNING))
        for leftover in self.dir.glob(".chunks-*"):
            shutil.rmtree(leftover, ignore_errors=True)
        cache.adopter(self._sweep)

    def _sweep(self) -> None:
        """Start-up: audio files the index doesn't know (a stopped extraction, a lost index) can't be reused,
        because what they were cut from isn't recorded anywhere else. They are removed. A slide directory says in
        its index.json what it belongs to, so one without a row is indexed again; a half-written one is removed."""
        known = {self.cache.path(e).name for e in self.cache.entries("audio")}
        for path in self.dir.glob("*"):
            if path.is_file() and path.name not in known:
                path.unlink(missing_ok=True)
        indexed = {self.cache.path(e).name for e in self.cache.entries("frames")}
        for path in (Path(self.settings.data_dir) / "frames").glob("*"):
            if not path.is_dir() or path.name in indexed:
                continue
            about = frames.index(path)
            if path.name.endswith(".part") or not (about.get("asset_id") and frames.read(path)):
                shutil.rmtree(path, ignore_errors=True)
            else:
                self.cache.put("frames", about["asset_id"], [path], course_id=about.get("course_id"),
                               item_id=about.get("item_id"), title=about.get("title"),
                               meta={"asset_id": about["asset_id"], "version": about.get("version") or "",
                                     "count": len(frames.read(path)), "duration_s": about.get("duration")})

    # --- slides ------------------------------------------------------------

    def slides(self, lecture: dict) -> tuple[str, list[dict]]:
        """(state, slides) for a lecture: none, extracting, failed, outdated (cut from an older version of the
        video) or ready, and each slide as {n, start, end, file}."""
        asset_id = lecture["video"]["asset_id"]
        job = self._slide_jobs.get(asset_id)
        if job and job["state"] == "extracting":
            return "extracting", []
        entry = self.cache.get("frames", asset_id)
        if entry is None:
            return ("failed" if job and job["state"] == "failed" else "none"), []
        found = frames.read(self.cache.path(entry))
        return ("ready" if entry.meta.get("version") == self.version(lecture) else "outdated"), found

    def slide_error(self, lecture: dict) -> str | None:
        return (self._slide_jobs.get(lecture["video"]["asset_id"]) or {}).get("error")

    def slide_file(self, lecture: dict, n: int) -> Path | None:
        entry = self.cache.get("frames", lecture["video"]["asset_id"])
        if entry is None:
            return None
        slide = next((s for s in frames.read(self.cache.path(entry)) if s.get("n") == n), None)
        path = self.cache.path(entry) / slide["file"] if slide else None
        if path is None or not path.is_file():
            return None
        self.cache.touch("frames", entry.key)
        return path

    async def _extract_slides(self, lecture: dict, video: Path, duration: float) -> list[dict]:
        """Cuts the slide frames out of a video that is on disk and indexes them. Raises AudioError."""
        asset_id = lecture["video"]["asset_id"]
        out_dir = Path(self.settings.data_dir) / "frames" / lecture["item_id"]
        about = {"item_id": lecture["item_id"], "asset_id": asset_id, "course_id": lecture.get("course_id"),
                 "title": lecture.get("title"), "version": self.version(lecture)}
        async with self._ffmpeg:
            found = await frames.extract(video, out_dir, duration=duration, about=about,
                                         threshold=self.settings.slides_scene_threshold,
                                         width=self.settings.slides_width, threads=self.settings.ffmpeg_threads)
        self.cache.put("frames", asset_id, [out_dir], course_id=lecture.get("course_id"), item_id=lecture["item_id"],
                       title=lecture.get("title"), meta={"asset_id": asset_id, "version": self.version(lecture),
                                                         "count": len(found), "duration_s": duration})
        try:
            self.cache.make_room("frames", 0, keep={("frames", asset_id)})
        except CacheFull:
            pass
        return found

    def request_slides(self, lecture: dict) -> str:
        """Starts cutting a lecture's slide frames in the background, downloading the video first if it isn't
        here (an explicit request to the content server, like an export). Returns the state."""
        state, _ = self.slides(lecture)
        if state in ("ready", "extracting"):
            return state
        if not audio.available():
            raise audio.AudioError("ffmpeg is not installed on this server, so slides can't be extracted")
        asset_id = lecture["video"]["asset_id"]
        job = self._slide_jobs[asset_id] = {"state": "extracting", "error": None}

        async def run() -> None:
            try:
                video = await self.videos.ensure(lecture)
                duration = (lecture.get("duration_min") or 0) * 60 or await audio.probe(video)
                await self._extract_slides(lecture, video, duration)
                self._slide_jobs.pop(asset_id, None)
            except (videos.VideoError, audio.AudioError) as exc:
                job.update(state="failed", error=str(exc))
            except Exception as exc:  # the message could hold anything: only its type is kept
                log.error("slides for item %s: %s", lecture["item_id"], type(exc).__name__)
                job.update(state="failed", error=f"internal error ({type(exc).__name__})")
        job["task"] = asyncio.get_running_loop().create_task(run())
        return "extracting"

    # --- lookups -----------------------------------------------------------

    def _job(self, job_id: int) -> dict | None:
        row = self.store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        return dict(row) if row else None

    def _set(self, job_id: int, **fields: Any) -> None:
        fields["updated_at"] = _now()
        for key in ("progress", "stages"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        self.store.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                           (*fields.values(), job_id))

    def _course(self, course_id: str | None) -> dict:
        return (self.store.record("courses", course_id) if course_id else None) or {"id": course_id}

    @staticmethod
    def hours(lecture: dict) -> float:
        """Estimated audio hours, from the duration the sync saw (0 when it saw none)."""
        return round((lecture.get("duration_min") or 0) / 60, 4)

    @staticmethod
    def version(lecture: dict) -> str:
        return lecture["video"].get("updated_at") or ""

    def language_for(self, course: dict) -> str | None:
        """The course's language (by ID or code), else the default, else None for the provider to detect."""
        by_course = {k.lower(): v for k, v in self.settings.stt_course_languages.items()}
        for key in ("id", "code"):
            if course.get(key) and str(course[key]).lower() in by_course:
                return by_course[str(course[key]).lower()]
        return self.settings.stt_default_language.strip() or None

    def terms_for(self, course: dict) -> list[str]:
        by_course = {k.lower(): v for k, v in self.settings.stt_course_terms.items()}
        for key in ("id", "code"):
            if course.get(key) and str(course[key]).lower() in by_course:
                return [t.strip() for t in by_course[str(course[key]).lower()].split(",") if t.strip()]
        return []

    def in_use(self, but: int | None = None) -> set[tuple[str, str]]:
        """What queued and running jobs still need: a job's audio once it exists, its cached video until then
        (and while its slide frames are being cut). A job paused for budget holds no video (it may wait for
        weeks), only audio that was already made. `but` leaves one job out: what the others need."""
        audio_by_asset: dict[str, list[str]] = {}
        for entry in self.cache.entries("audio"):
            audio_by_asset.setdefault(entry.meta.get("asset_id"), []).append(entry.key)
        held: set[tuple[str, str]] = set()
        for row in self.store.query(f"SELECT id, asset_id, state FROM jobs WHERE state IN "
                                    f"({','.join('?' * len(ACTIVE))})", ACTIVE):
            if row["id"] == but:
                continue
            if row["asset_id"] in audio_by_asset:
                held |= {("audio", key) for key in audio_by_asset[row["asset_id"]]}
            elif row["state"] != "paused_budget":
                held.add(("video", row["asset_id"]))
            if row["state"] == "extracting":  # the slide frames are cut from the video after the audio
                held.add(("video", row["asset_id"]))
        # and a slide extraction asked for by hand holds its video while it runs (not after it failed)
        held |= {("video", asset_id) for asset_id, job in self._slide_jobs.items() if job["state"] == "extracting"}
        return held

    # --- views -------------------------------------------------------------

    def job_view(self, job: dict) -> dict:
        """A job for an API answer, with the live progress of the stage it is in."""
        progress = dict(self._progress.get(job["id"]) or json.loads(job["progress"] or "{}"))
        if job["state"] == "downloading" and (download := self.videos.jobs.get(job["asset_id"])):
            progress.update(downloaded_mb=round(download.done / 2**20),
                            total_mb=round(download.total / 2**20) if download.total else None)
        out = {"id": job["id"], "item_id": job["item_id"], "title": job["title"], "course_id": job["course_id"],
               "state": job["state"], "model": job["model"] or "automatic", "provider": job["provider_id"],
               "chosen": job["reason"], "language": job["language"], "hours": job["hours"],
               "progress": progress or None, "error": job["error"], "attempts": job["attempts"] or None,
               "not_before": job["not_before"], "trigger": job["trigger"], "transcript_id": job["transcript_id"],
               "stages": json.loads(job["stages"] or "{}") or None, "created_at": job["created_at"],
               "updated_at": job["updated_at"], "started_at": job["started_at"], "finished_at": job["finished_at"]}
        if job["state"] == "failed":
            out["retryable"] = bool(job["retryable"])
        return out

    def jobs(self, *, states: tuple[str, ...] | None = None, item_id: str | None = None,
             course_ids: set[str] | None = None, limit: int = 200) -> list[dict]:
        """Jobs, the ones still to do first (in the order they will run), then finished ones, newest first."""
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if states:
            sql, args = sql + f" AND state IN ({','.join('?' * len(states))})", [*states]
        if item_id:
            sql, args = sql + " AND item_id=?", [*args, item_id]
        rows = [dict(r) for r in self.store.query(sql, args) if course_ids is None or r["course_id"] in course_ids]
        order = {s: n for n, s in enumerate(("transcribing", "extracting", "downloading", "queued", "paused_budget"))}
        rows.sort(key=lambda j: (order.get(j["state"], 9), j["priority"], j["id"]) if j["state"] in order
                  else (9, 0, -j["id"]))
        return [self.job_view(j) for j in rows[:limit]]

    def queue_summary(self) -> dict:
        counts = Counter(r["state"] for r in self.store.query("SELECT state FROM jobs"))
        hours = self.store.one(f"SELECT COALESCE(SUM(hours), 0) AS h FROM jobs WHERE state IN "
                               f"({','.join('?' * len(ACTIVE))})", ACTIVE)["h"]
        return {**{s: counts.get(s, 0) for s in (*ACTIVE, "done", "failed", "canceled")},
                "waiting_hours": round(hours, 1), "ffmpeg": audio.available()}

    def states(self) -> dict[str, dict]:
        """item_id -> {state, model} for lecture rows: an active job's state, else the preferred transcript's
        (ready or outdated), else the last job's failure."""
        out: dict[str, dict] = {}
        for item, row in self.transcripts.preferred_by_item().items():
            out[item] = {"state": row["status"], "model": row["provider_id"]}
        for row in self.store.query("SELECT item_id, state, model, provider_id FROM jobs ORDER BY id"):
            if row["state"] in ACTIVE or (row["state"] in ("failed", "canceled") and row["item_id"] not in out):
                was = out.get(row["item_id"])
                out[row["item_id"]] = {"state": row["state"]}
                if model := row["provider_id"] or row["model"]:
                    out[row["item_id"]]["model"] = model
                if was and row["state"] in ACTIVE:
                    out[row["item_id"]]["has"] = was["state"]  # an older transcript is readable meanwhile
        return out

    def status(self, lecture: dict, model: str | None = None) -> dict:
        """Where one lecture's transcript stands (for this provider, when one is named)."""
        item_id = lecture["item_id"]
        out: dict[str, Any] = {"item_id": item_id, "title": lecture.get("title"), "state": "none"}
        transcript = self.transcripts.preferred(item_id, model)
        if transcript:
            out.update(state=transcript["status"], model=transcript["provider_id"],
                       transcript=self.transcripts.summary(transcript))
        rows = [dict(r) for r in self.store.query("SELECT * FROM jobs WHERE item_id=? ORDER BY id DESC", (item_id,))
                if not model or r["model"] == model or r["provider_id"] == model]
        job = next((j for j in rows if j["state"] in ACTIVE), rows[0] if rows else None)
        if job is None or job["state"] == "done":
            return out
        if job["state"] in ACTIVE or not transcript:
            out["state"] = job["state"]  # in progress, or the last try failed and there is nothing to read
        if job["state"] in ACTIVE or not transcript or job["id"] > (transcript["job_id"] or 0):
            out["job"] = self.job_view(job)  # with a transcript: the newer try that failed, for its error
        return out

    # --- asking for a transcript -------------------------------------------

    def estimate(self, lecture: dict, *, model: str | None = None, language: str | None = None) -> dict:
        """What transcribing this lecture would take, without queueing anything."""
        course = self._course(lecture.get("course_id"))
        language = language or self.language_for(course)
        hours = self.hours(lecture)
        out: dict[str, Any] = {"item_id": lecture["item_id"], "title": lecture.get("title"), "estimate_only": True,
                               "hours": round(hours, 2) or None, "language": language or "detected by the provider",
                               "video_cached": self.videos.status(lecture)["state"] == "ready",
                               "download_mb": round((lecture["video"].get("listed_size") or 0) / 2**20) or None}
        try:
            choices = self.registry.choose(model=model, course=course, language=language, hours=hours)
        except Unavailable as exc:
            out.update(would_run=False, reason=str(exc), working_models=exc.working)
            return out
        cfg = choices[0].config
        used = self.registry.usage(cfg)["hours"]
        out.update(would_run=True, model=cfg.id, chosen=choices[0].reason,
                   fallbacks=[c.config.id for c in choices[1:]] or None,
                   cost_usd=self.registry.cost(cfg, hours, used), month=self.registry.budget(cfg))
        return out

    def request(self, lecture: dict, *, model: str | None = None, language: str | None = None, force: bool = False,
                trigger: str = "api", priority: int = 0, strict: bool = True) -> dict:
        """Queues the transcription of one lecture, or finds the job or transcript that already answers it.
        With strict (a single request), a provider that can't take it now raises Unavailable instead of leaving a
        job that waits; batches queue anyway and the job pauses or retries when its turn comes."""
        asset_id, version = lecture["video"]["asset_id"], self.version(lecture)
        if self.transcripts.for_version(asset_id, version, model):
            if not force:
                return self.status(lecture, model)
            # A forced request is followed by asking again with the same arguments. Once its job is done that
            # second request must find the new transcript, not start (and pay for) another one.
            newest = self.store.one(
                "SELECT MAX(created_at) AS at FROM transcripts WHERE asset_id=? AND video_version=?"
                + (" AND provider_id=?" if model else ""), (asset_id, version, *([model] if model else [])))["at"]
            if newest and datetime.fromisoformat(newest) > datetime.now(KST) - timedelta(minutes=FORCE_AGAIN_MIN):
                return {**self.status(lecture, model),
                        "note": f"made less than {FORCE_AGAIN_MIN} minutes ago; force starts a new one after that"}
        active = self.store.one(
            f"SELECT id FROM jobs WHERE asset_id=? AND video_version=? AND model=? AND state IN "
            f"({','.join('?' * len(ACTIVE))})", (asset_id, version, model or "", *ACTIVE))
        if active is None:
            course = self._course(lecture.get("course_id"))
            hours = self.hours(lecture)
            state, error = "queued", None
            try:
                self.registry.choose(model=model, course=course, language=language or self.language_for(course),
                                     hours=hours)
            except Unavailable as exc:
                if strict and not (exc.budget and not model):
                    raise
                if exc.budget:
                    state, error = "paused_budget", str(exc)
            now = _now()
            try:
                self.store.execute(
                    """INSERT INTO jobs (item_id, asset_id, course_id, title, video_version, model, language, state,
                           error, hours, priority, trigger, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (lecture["item_id"], asset_id, lecture.get("course_id"), lecture.get("title"), version,
                     model or "", language, state, error, hours or None, priority, trigger, now, now))
            except sqlite3.IntegrityError:
                pass  # asked twice at the same moment: the other request's job is the one
        self._wake.set()
        return self.status(lecture, model)

    def retry(self, job_id: int) -> dict | None:
        """Puts a failed or canceled job back in the queue. None when there is no such job; ValueError when it
        can't be retried (still active, or another job for the same lecture and model is)."""
        job = self._job(job_id)
        if job is None:
            return None
        if job["state"] not in ("failed", "canceled"):
            raise ValueError(f"job {job_id} is {job['state']}; only failed or canceled jobs can be retried")
        try:
            self._set(job_id, state="queued", error=None, attempts=0, not_before=None, retryable=0,
                      finished_at=None, progress={})
        except sqlite3.IntegrityError:
            raise ValueError("another job for this lecture and model is already queued") from None
        self._wake.set()
        return self.job_view(self._job(job_id))

    def cancel(self, job_id: int) -> dict | None:
        """Stops a job that hasn't finished. A download it started is left to finish into the cache."""
        job = self._job(job_id)
        if job is None:
            return None
        if job["state"] in ACTIVE:
            self._set(job_id, state="canceled", finished_at=_now(), error=None, progress={})
            if task := self._tasks.get(job_id):
                task.cancel()
            self._partial.pop(job_id, None)
            self._wake.set()
        return self.job_view(self._job(job_id))

    # --- batches -----------------------------------------------------------

    def plan(self, lectures: list[dict], *, model: str | None = None, only_missing: bool = True) -> dict:
        """What queueing these lectures would mean: how many, how many hours, what each model would cost and how
        much of its cap is left. Queues nothing."""
        return self._plan(lectures, model, only_missing)[0]

    def _plan(self, lectures: list[dict], model: str | None, only_missing: bool) -> tuple[dict, list[dict]]:
        states = self.states()
        todo: list[dict] = []
        skipped: Counter = Counter()
        for lecture in lectures:
            if lecture.get("kind") != "video":
                continue
            if not lecture.get("video"):
                skipped["not hosted on SKKU's content server (YouTube, Zoom)"] += 1
                continue
            state = (states.get(lecture["item_id"]) or {}).get("state", "none")
            if state in ACTIVE:
                skipped["already queued"] += 1
            elif state == "ready" and only_missing:
                skipped["already transcribed"] += 1
            else:
                todo.append(lecture)
        hours = sum(self.hours(lec) for lec in todo)
        cached = {e.key for e in self.cache.entries("video")} | {e.meta.get("asset_id") for e in self.cache.entries("audio")}
        download = sum(lec["video"].get("listed_size") or 0 for lec in todo if lec["video"]["asset_id"] not in cached)
        models = []
        for cfg in self.registry.all():
            if model and cfg.id != model:
                continue
            blocked = self.registry.not_eligible(cfg)
            budget = self.registry.budget(cfg)
            left = budget["left_hours"]
            models.append({"id": cfg.id, "ready": blocked is None, "not_ready": blocked[0] if blocked else None,
                           "cost_usd": self.registry.cost(cfg, hours, self.registry.usage(cfg)["hours"]),
                           "cap_left_hours": left, "resets": budget["resets"],
                           "hours_that_would_wait": round(max(0.0, hours - left), 1) if left is not None else 0})
        try:
            automatic = self.registry.choose()[0].config.id
        except Unavailable:
            automatic = None
        return {"count": len(todo), "hours": round(hours, 1),
                "unknown_duration": sum(1 for lec in todo if not lec.get("duration_min")) or None,
                "download_gb": round(download / 2**30, 1), "skipped": dict(skipped) or None,
                "model": model or "automatic", "automatic_would_use": None if model else automatic, "models": models,
                "items": [{"item_id": lec["item_id"], "title": lec.get("title"), "course_id": lec.get("course_id"),
                           "minutes": lec.get("duration_min")} for lec in todo[:40]]}, todo

    def queue(self, lectures: list[dict], *, model: str | None = None, only_missing: bool = True,
              trigger: str = "batch") -> dict:
        """Queues what `plan` listed. Raises Unavailable when the named model doesn't exist or nothing is usable."""
        try:
            self.registry.choose(model=model)
        except Unavailable as exc:
            if not exc.budget:  # a full budget only makes the jobs wait; anything else would fail every one
                raise
        plan, todo = self._plan(lectures, model, only_missing)
        states: Counter = Counter()
        for lecture in todo:
            done = self.transcripts.for_version(lecture["video"]["asset_id"], self.version(lecture), model)
            status = self.request(lecture, model=model, force=bool(done), trigger=trigger, priority=BATCH_PRIORITY,
                                  strict=False)
            states[status["state"]] += 1
        return {**plan, "queued": sum(states.values()), "states": dict(states)}

    # --- after a sync --------------------------------------------------------

    def after_sync(self) -> dict:
        """Marks transcripts of videos that changed as outdated, and queues what the settings say to queue by
        itself: re-runs of outdated transcripts, and new lectures of the auto-transcribe courses."""
        lectures = [lec for lec in self.store.records("lectures") if lec.get("video") and lec.get("kind") == "video"]
        by_asset = {lec["video"]["asset_id"]: lec for lec in lectures}
        turned = self.transcripts.mark_outdated(lectures)
        queued = 0
        if self.settings.stt_auto_retranscribe:
            for row in turned:
                queued += self._auto(by_asset[row["asset_id"]])
        wanted = {c.lower() for c in self.settings.stt_auto_courses}
        if self.settings.stt_auto_transcribe and wanted:
            course_ids = {c["id"] for c in self.store.records("courses")
                          if {str(c.get("id")).lower(), str(c.get("code")).lower()} & wanted}
            for lecture in lectures:
                if lecture["course_id"] in course_ids:
                    queued += self._auto(lecture)
        if turned or queued:
            log.info("after sync: %d transcript(s) outdated, %d job(s) queued", len(turned), queued)
        return {"outdated": len(turned), "queued": queued}

    def _auto(self, lecture: dict) -> int:
        """Queues a lecture once per video version: one that was tried before (even if it failed) is left alone."""
        asset_id, version = lecture["video"]["asset_id"], self.version(lecture)
        if self.transcripts.for_version(asset_id, version) or self.store.one(
                "SELECT 1 FROM jobs WHERE asset_id=? AND video_version=?", (asset_id, version)):
            return 0
        try:
            self.request(lecture, trigger="auto", priority=BATCH_PRIORITY, strict=False)
        except Unavailable:
            return 0
        return 1

    # --- the worker --------------------------------------------------------

    async def run(self) -> None:
        """The coordinator loop: wakes when something is queued or finishes, and every few seconds."""
        while True:
            try:
                self.tick(recheck_paused=False)
            except Exception as exc:  # the queue must outlive a bad row
                log.error("transcription queue: %s", type(exc).__name__)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), TICK_S)
            except TimeoutError:
                pass

    def tick(self, recheck_paused: bool = True) -> None:
        """Lets paused jobs back in when their budget has room again, and starts queued jobs while fewer than
        prefetch_limit are running. Jobs whose video or audio is already on disk go first within a priority."""
        now = _now()
        if recheck_paused or time.monotonic() - self._paused_checked >= PAUSED_CHECK_S:
            self._paused_checked = time.monotonic()
            lectures: dict[str, dict] | None = None
            for job in self.store.query("SELECT * FROM jobs WHERE state='paused_budget' ORDER BY priority, id"):
                course = self._course(job["course_id"])
                hours = job["hours"]
                if not hours:  # the same estimate the job itself will make, or it would be let in and pause again
                    if lectures is None:
                        lectures = {x["item_id"]: x for x in self.store.records("lectures", include_inactive=True)
                                    if x.get("item_id")}
                    hours = self.hours(lectures.get(job["item_id"]) or {})
                try:
                    self.registry.choose(model=job["model"] or None, course=course, hours=hours or 0.0,
                                         language=job["language"] or self.language_for(course))
                    self._set(job["id"], state="queued", error=None)
                except Unavailable as exc:
                    if not exc.budget:  # no longer a budget matter: let the job say what is
                        self._set(job["id"], state="queued", error=None)
        room = self.settings.prefetch_limit - len(self._tasks)
        if room <= 0:
            return
        waiting = [dict(r) for r in self.store.query(
            "SELECT * FROM jobs WHERE state='queued' AND (not_before IS NULL OR not_before<=?)", (now,))
            if r["id"] not in self._tasks]
        cached = {e.key for e in self.cache.entries("video")} | {e.meta.get("asset_id") for e in self.cache.entries("audio")}
        waiting.sort(key=lambda j: (j["priority"], j["asset_id"] not in cached, j["id"]))
        for job in waiting[:room]:
            task = self._tasks[job["id"]] = asyncio.get_running_loop().create_task(self._process(job))
            task.add_done_callback(lambda _, job_id=job["id"]: self._finished(job_id))

    def _finished(self, job_id: int) -> None:
        self._tasks.pop(job_id, None)
        self._wake.set()

    async def stop(self) -> None:
        """Server shutdown: running jobs stop where they are; the next start puts them back in the queue."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def drain(self, timeout: float = 30) -> None:
        """Runs the queue until nothing is running or ready to start. For tests and the CLI."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.tick()
            if not self._tasks:
                return
            await asyncio.wait(list(self._tasks.values()), timeout=0.2)
            await asyncio.sleep(0)
        raise TimeoutError("the queue did not settle")

    async def _process(self, job: dict) -> None:
        job_id = job["id"]
        try:
            await self._work(job)
        except _Pause as exc:
            self._set(job_id, state="paused_budget", error=str(exc), progress={})
        except _Retry as exc:
            attempts = (self._job(job_id) or job)["attempts"] + 1
            if attempts > exc.limit:
                self._set(job_id, state="failed", retryable=1, attempts=attempts, finished_at=_now(), progress={},
                          error=f"{exc} (gave up after {attempts} tries)")
            else:
                minutes = exc.minutes or BACKOFF_MIN[min(attempts, len(BACKOFF_MIN)) - 1]
                when = datetime.now(KST) + timedelta(minutes=minutes)
                self._set(job_id, state="queued", attempts=attempts, not_before=when.isoformat(), progress={},
                          error=f"{exc}; trying again at {when:%H:%M} (try {attempts} of {exc.limit})")
        except _Fail as exc:
            self._partial.pop(job_id, None)
            self._set(job_id, state="failed", retryable=int(exc.retryable), error=str(exc), finished_at=_now(),
                      progress={})
        except asyncio.CancelledError:
            raise  # canceled by you (the row already says so) or the server is stopping (reset at the next start)
        except Exception as exc:  # a bug here must not stop the queue; the message could hold anything, so only
            frames = traceback.extract_tb(exc.__traceback__)  # its type and place are kept
            where = f"{frames[-1].filename.rsplit('/', 1)[-1]}:{frames[-1].lineno}" if frames else "?"
            log.error("transcription job %s crashed: %s at %s", job_id, type(exc).__name__, where)
            self._set(job_id, state="failed", retryable=1, finished_at=_now(), progress={},
                      error=f"internal error ({type(exc).__name__} at {where})")
        finally:
            self.registry.release(job_id)
            self._progress.pop(job_id, None)

    async def _work(self, job: dict) -> None:
        job_id = job["id"]
        lecture = videos.find(self.store, job["item_id"])
        if lecture is None or lecture["video"]["asset_id"] != job["asset_id"]:
            raise _Fail("the lecture is no longer in the synced data, or its video moved")
        version = self.version(lecture)
        if version != job["video_version"]:  # a sync saw a newer video while this waited: transcribe that one
            try:
                self._set(job_id, video_version=version)
            except sqlite3.IntegrityError:
                raise _Fail("replaced by a job for the newer version of this video") from None
        course = self._course(lecture.get("course_id"))
        language = job["language"] or self.language_for(course)
        hours = job["hours"] or self.hours(lecture)
        stages = json.loads(job["stages"] or "{}")
        self._set(job_id, started_at=job["started_at"] or _now(), not_before=None)
        try:  # the budget and the provider's state are checked before anything is downloaded
            choices = self.registry.choose(model=job["model"] or None, course=course, language=language,
                                           hours=hours, but=job_id)
        except Unavailable as exc:
            if exc.budget:
                raise _Pause(str(exc)) from None
            raise _Retry(str(exc), limit=PROVIDER_WAITS, minutes=self.settings.stt_skip_minutes) from None

        errors: list[str] = []
        over: list[str] = []
        retryable = False
        for choice in choices:
            cfg = choice.config
            self.registry.reserve(job_id, cfg.id, hours)
            entry = await self._audio(job, lecture, cfg, version, stages)
            hours = entry.meta["duration_s"] / 3600  # the real length, now that it is known
            self._set(job_id, hours=round(hours, 4))
            self.registry.reserve(job_id, cfg.id, hours)
            if reason := self.registry.over_budget(cfg, hours, but=job_id):
                over.append(f"{cfg.id}: {reason}")
                continue
            self._set(job_id, state="transcribing", provider_id=cfg.id, hours=round(hours, 4), stages=stages)
            started = time.monotonic()
            try:
                transcript, cost, pieces = await self._transcribe(job_id, lecture, cfg, entry, language, course)
            except SttError as exc:
                message = str(exc)
                self.registry.record_failure(cfg.id, message)
                errors.append(f"{cfg.id}: {message}")
                retryable = retryable or exc.retryable
                if job["model"]:  # asked for by id: never another provider instead
                    if exc.retryable:
                        raise _Retry(message, limit=PROVIDER_WAITS) from None
                    raise _Fail(message) from None
                continue  # automatic routing: the next eligible provider
            self.registry.record_ok(cfg.id)
            reason = choice.reason + (f"; after {'; '.join(errors)}" if errors else "")
            stages["transcribe"] = {"s": round(time.monotonic() - started, 1), "provider": cfg.id, "pieces": pieces}
            tid = self.transcripts.save(lecture, transcript, video_version=version, reason=reason, job_id=job_id,
                                        cost_usd=cost)
            self._partial.pop(job_id, None)
            self._set(job_id, state="done", provider_id=cfg.id, reason=reason, transcript_id=tid, error=None,
                      finished_at=_now(), progress={}, stages=stages)
            log.info("transcript %s for item %s by %s (%.0f min of audio)", tid, job["item_id"], cfg.id, hours * 60)
            return
        if over and not errors:
            raise _Pause("; ".join(over))
        if retryable:
            raise _Retry("; ".join(errors), limit=PROVIDER_WAITS)
        raise _Fail("; ".join(errors + over) or "no provider could take this lecture")

    async def _audio(self, job: dict, lecture: dict, cfg: ProviderConfig, version: str, stages: dict) -> Entry:
        """The lecture's audio in the format this provider takes: from the cache, or extracted now from the video
        (downloaded first if needed) or from audio in another format when the video is no longer here."""
        job_id, asset_id = job["id"], job["asset_id"]
        fmt, kbps = cfg.audio_format, self.registry.bitrate(cfg)
        key = f"{asset_id}.{fmt}{kbps if audio.FORMATS[fmt].bitrate else ''}"
        entry = self.cache.get("audio", key)
        if entry and entry.meta.get("version") == version:
            self.cache.touch("audio", key)
            return entry
        if entry:
            self.cache.delete("audio", key)  # from an older version of the video
        if not audio.available():
            raise _Fail("ffmpeg is not installed on this server, so audio can't be extracted")
        source = self.videos.path(asset_id) if self.videos.status(lecture)["state"] == "ready" else None
        if source is None:
            other = next((e for e in self.cache.entries("audio") if e.meta.get("asset_id") == asset_id
                          and e.meta.get("version") == version), None)
            source = self.cache.path(other) if other else None
        if source is None:
            self._set(job_id, state="downloading")
            self._progress[job_id] = {"stage": "downloading"}
            started = time.monotonic()
            try:
                source = await self.videos.ensure(lecture)
            except videos.VideoError as exc:
                if exc.transient:
                    raise _Retry(f"download: {exc}", limit=self.settings.download_retries) from None
                raise _Fail(f"download: {exc}") from None
            stages["download"] = {"s": round(time.monotonic() - started, 1),
                                  "mb": round(source.stat().st_size / 2**20)}
        self._set(job_id, state="extracting", stages=stages)
        self._progress[job_id] = {"stage": "extracting", "percent": 0}
        dst = self.dir / f"{key}.{audio.FORMATS[fmt].ext}"
        seconds = (lecture.get("duration_min") or 0) * 60 or None

        def progress(done: float) -> None:
            self._progress[job_id] = {"stage": "extracting", "percent": round(done * 100)}
        started = time.monotonic()
        try:
            async with self._ffmpeg:
                duration = await audio.extract(source, dst, fmt=fmt, kbps=kbps, duration=seconds,
                                               threads=self.settings.ffmpeg_threads, on_progress=progress)
        except audio.AudioError as exc:
            if exc.retryable:
                raise _Retry(f"audio: {exc}", limit=2) from None
            raise _Fail(f"audio: {exc}") from None
        entry = self.cache.put("audio", key, [dst], course_id=lecture.get("course_id"), item_id=lecture["item_id"],
                               title=lecture.get("title"),
                               meta={"asset_id": asset_id, "version": version, "duration_s": round(duration, 3),
                                     "format": fmt, "kbps": kbps})
        stages["extract"] = {"s": round(time.monotonic() - started, 1), "audio_s": round(duration),
                             "mb": round(entry.size / 2**20, 1)}
        if self.settings.slides_enabled and source.parent == self.videos.dir and self.slides(lecture)[0] != "ready":
            self._progress[job_id] = {"stage": "extracting", "note": "slide frames"}
            started = time.monotonic()
            try:  # the video is here now: cut its slides before it may leave. A failure costs the job nothing.
                found = await self._extract_slides(lecture, source, duration)
                stages["slides"] = {"s": round(time.monotonic() - started, 1), "count": len(found)}
            except audio.AudioError as exc:
                stages["slides"] = {"error": str(exc)}
        try:
            self.cache.make_room("audio", 0, keep={("audio", key)})
        except CacheFull:
            pass  # over its cap only with entries that may not go: the cache page shows it
        if not self.settings.keep_video_after_processing:
            video = self.cache.get("video", asset_id)
            if video and not video.pinned and ("video", asset_id) not in self.in_use(but=job_id):
                self.cache.delete("video", asset_id)
        return entry

    def _slot(self, cfg: ProviderConfig) -> asyncio.Semaphore:
        size, slot = self._slots.get(cfg.id, (0, None))
        if slot is None or size != cfg.concurrency:
            slot = asyncio.Semaphore(cfg.concurrency)
            self._slots[cfg.id] = (cfg.concurrency, slot)
        return slot

    async def _transcribe(self, job_id: int, lecture: dict, cfg: ProviderConfig, entry: Entry,
                          language: str | None, course: dict) -> tuple[Transcript, float, int]:
        """(transcript, what it cost, how many pieces it took). The audio goes whole when the provider's limits
        allow; otherwise in pieces cut at silences, whose times are shifted back and joined."""
        provider = self.registry.provider(cfg)
        caps = provider.capabilities
        path, duration = self.cache.path(entry), entry.meta["duration_s"]
        options = {"format": cfg.audio_format, "keyterms": self.terms_for(course) if cfg.keyterms else None}
        limit = audio.chunk_limit_s(path.stat().st_size, duration, caps.max_upload_mb, caps.max_duration_s)
        done = self._partial.setdefault(job_id, {})
        cost = 0.0
        async with self._slot(cfg):
            if limit is None:
                plan = [(0.0, duration)]
            else:
                self._progress[job_id] = {"stage": "transcribing", "provider": cfg.id, "note": "looking for silences"}
                try:
                    async with self._ffmpeg:
                        quiet = await audio.silences(path, threads=self.settings.ffmpeg_threads)
                except audio.AudioError as exc:
                    raise SttError(f"audio: {exc}", retryable=exc.retryable) from None
                plan = audio.plan_chunks(duration, quiet, max_s=limit,
                                         target_s=min(self.settings.stt_chunk_minutes * 60, limit * 0.9))
            pieces: list[tuple[float, Transcript]] = []
            scratch = self.dir / f".chunks-{job_id}"
            try:
                for n, (start, end) in enumerate(plan, 1):
                    self._progress[job_id] = {"stage": "transcribing", "provider": cfg.id, "piece": n,
                                              "pieces": len(plan)}
                    piece_key = (cfg.id, start, end)
                    if piece_key not in done:
                        piece = path
                        if len(plan) > 1:
                            scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
                            piece = scratch / f"{n:03d}{path.suffix}"
                            try:
                                await audio.cut(path, piece, start, end)
                            except audio.AudioError as exc:
                                raise SttError(f"audio: {exc}", retryable=exc.retryable) from None
                        detected = next((t.language for _, t in pieces if t.language), None)
                        done[piece_key] = await provider.transcribe(
                            piece, language=language or detected, options={**options, "duration": end - start})
                        cost += self.registry.record_usage(cfg, end - start, job_id=job_id, item_id=lecture["item_id"])
                        if piece != path:
                            piece.unlink(missing_ok=True)
                    pieces.append((start, done[piece_key]))
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
        transcript = join(pieces, duration=duration, provider=cfg.id,
                          model=cfg.model or getattr(provider, "model", ""))
        need = self.settings.stt_min_timestamps
        if transcript.segments and TIMESTAMPS.index(transcript.timestamps) < TIMESTAMPS.index(need):
            raise SttError(f"{cfg.id} returned {transcript.timestamps} timestamps; at least {need} are required "
                           f"(stt_min_timestamps)")
        return transcript, round(cost, 4), len(plan)
