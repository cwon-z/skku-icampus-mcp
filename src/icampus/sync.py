"""One sync run, the daily schedule, and the optional Uptime Kuma heartbeat."""

import asyncio
import fcntl
import logging
import random
import traceback
from datetime import datetime, time, timedelta
from typing import Any, Literal

import httpx

from . import collect
from .browser import LoginRejected, NeedsAttention, NotLoggedIn, clean_error, open_session
from .config import KST, Settings
from .dates import term_year
from .store import Store

log = logging.getLogger(__name__)
RequestResult = Literal["started", "running", "cooldown", "login_blocked"]
PER_TERM = ("courses", "todos", "announcements")
PER_COURSE = ("assignments", "lectures", "attendance")
DATASETS = PER_TERM + PER_COURSE


def _where(exc: BaseException) -> str:
    """file:line of the failure, without the (possibly secret-bearing) message."""
    frames = traceback.extract_tb(exc.__traceback__)
    return f"{frames[-1].filename.rsplit('/', 1)[-1]}:{frames[-1].lineno}" if frames else "?"


class Syncer:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.running = False
        self.next_run: datetime | None = None
        self._task: asyncio.Task | None = None

    # --- manual trigger (REST / MCP) --------------------------------------

    def request(self, *, retry_login: bool = False) -> tuple[RequestResult, int | None]:
        if self.running:
            return "running", None
        if self.store.get_state("login_block") and not retry_login:
            return "login_blocked", None
        last = self.store.get_state("last_manual_at")
        if last and not retry_login:
            wait = datetime.fromisoformat(last) + timedelta(minutes=self.settings.manual_cooldown_min) - datetime.now(KST)
            if wait.total_seconds() > 0:
                return "cooldown", int(wait.total_seconds()) + 1
        self.store.set_state("last_manual_at", datetime.now(KST).isoformat())
        self._task = asyncio.get_running_loop().create_task(self.run("manual", retry_login=retry_login))
        return "started", None

    # --- a run -------------------------------------------------------------

    async def run(self, trigger: str, *, retry_login: bool = False) -> int:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        lock = open(self.settings.data_dir / "sync.lock", "w")
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log.info("another sync is running; skipping")
                run_id = self.store.start_run(trigger, datetime.now(KST))
                self.store.finish_run(run_id, "skipped", {"reason": "another sync held the lock"}, datetime.now(KST))
                return run_id
            self.running = True
            run_id = self.store.start_run(trigger, datetime.now(KST))
            detail: dict[str, Any] = {"datasets": {}}
            status = "failed"
            try:
                if retry_login:
                    self.store.set_state("login_block", None)
                    self.store.set_state("login_attempts", [])
                async with asyncio.timeout(self.settings.sync_timeout_s):
                    status = await self._run(detail)
            except TimeoutError:
                detail["error"] = f"run exceeded {self.settings.sync_timeout_s}s"
            except Exception as exc:  # keep the service alive; the run record says what broke
                detail["error"] = clean_error(exc)
                log.error("sync failed at %s: %s", _where(exc), detail["error"])
            finally:
                try:
                    self.store.finish_run(run_id, status, detail, datetime.now(KST))
                except Exception as exc:
                    log.error("could not record run %s: %s", run_id, clean_error(exc))
            log.info("sync %s: %s", run_id, status)
            return run_id
        finally:
            self.running = False
            lock.close()

    async def _run(self, detail: dict[str, Any]) -> str:
        s_ = self.settings
        allow_credentials = not self.store.get_state("login_block")
        async with open_session(s_, self.store) as s:
            try:
                detail["login"] = await s.ensure_login(allow_credentials=allow_credentials)
            except (LoginRejected, NeedsAttention) as exc:
                self.store.set_state("login_block", {"reason": str(exc), "at": datetime.now(KST).isoformat()})
                detail["error"] = f"login: {exc}"
                return "failed"
            except NotLoggedIn as exc:
                detail["error"] = f"login: {exc}"
                return "failed"

            ok = True

            async def step(dataset: str, scope: str, coro) -> None:
                nonlocal ok
                try:
                    records = await coro
                    upserted, gone = self.store.replace_scope(dataset, scope, records, datetime.now(KST))
                    detail["datasets"].setdefault(dataset, {})[scope] = {"count": upserted, "dropped": gone}
                except Exception as exc:
                    ok = False
                    error = clean_error(exc)
                    log.warning("%s/%s failed at %s: %s", dataset, scope, _where(exc), error)
                    detail["datasets"].setdefault(dataset, {})[scope] = {"error": error}

            term, course_records, raw_courses = await collect.courses(s, s_.term)
            if not term:
                detail["error"] = "could not work out the current term; set ICAMPUS_TERM"
                return "failed"
            self.store.set_state("term", term)
            term_scope = f"term:{term}"
            self.store.replace_scope("courses", term_scope, course_records, datetime.now(KST))
            detail["datasets"]["courses"] = {term_scope: {"count": len(course_records)}}
            ids = [r.course_id for r in course_records]
            self.store.set_state("expected_scopes", {
                **{d: [term_scope] for d in PER_TERM}, **{d: [f"course:{c}" for c in ids] for d in PER_COURSE}})
            year = term_year(term)
            for dataset in PER_COURSE:
                self.store.retire_scopes(dataset, {f"course:{c}" for c in ids})
            for dataset in PER_TERM:
                self.store.retire_scopes(dataset, {term_scope})

            await step("todos", term_scope, collect.todos(s, ids, year))
            await step("announcements", term_scope, collect.announcements(s, ids, collect.term_start(raw_courses)))
            for cid in ids:
                await step("assignments", f"course:{cid}", collect.assignments(s, cid))
                await step("lectures", f"course:{cid}", collect.lectures(s, cid, year))
                await step("attendance", f"course:{cid}", collect.attendance(s, cid))

            detail["blocked"] = s.blocked
            await s.save_state()
            return "ok" if ok and not s.blocked else "partial"

    # --- background loops ---------------------------------------------------

    def _next_slot(self, after: datetime) -> datetime | None:
        """The first configured time strictly after `after` (jitter is added separately)."""
        times = [time.fromisoformat(t.strip()) for t in self.settings.sync_times.split(",") if t.strip()]
        if not times:
            return None
        candidates = [datetime.combine(after.date() + timedelta(days=d), t, KST) for d in (0, 1) for t in times]
        return min(c for c in candidates if c > after)

    async def scheduler(self) -> None:
        self.store.mark_interrupted()
        cursor = datetime.now(KST)
        # After a restart, don't repeat a slot the previous process already ran early (negative jitter).
        upcoming = self._next_slot(cursor)
        last = next((r for r in self.store.recent_runs(20) if r["trigger"] == "schedule"), None)
        if upcoming and last and datetime.fromisoformat(last["started_at"]) >= upcoming - timedelta(
                minutes=self.settings.sync_jitter_min):
            cursor = upcoming
        while True:
            slot = self._next_slot(cursor)
            if slot is None:
                return
            jitter = random.uniform(-self.settings.sync_jitter_min, self.settings.sync_jitter_min)
            self.next_run = slot + timedelta(minutes=jitter)
            await asyncio.sleep(max(0.0, (self.next_run - datetime.now(KST)).total_seconds()))
            # search after this slot (jitter never repeats it) and after now (a late wake-up after
            # sleep runs once, not once per missed slot)
            cursor = max(slot, datetime.now(KST))
            if not self.running:
                try:
                    await self.run("schedule")
                except Exception as exc:  # never let one bad run end the schedule
                    log.error("scheduled run crashed: %s", clean_error(exc))

    def health(self) -> tuple[bool, str]:
        block = self.store.get_state("login_block")
        if block:
            return False, f"login blocked: {block['reason']}"
        runs = [r for r in self.store.recent_runs(10) if r["status"] in ("ok", "partial", "failed")]
        if not runs:
            return True, "no runs yet"
        last = runs[0]
        age = datetime.now(KST) - datetime.fromisoformat(last["started_at"])
        if age > timedelta(hours=self.settings.stale_hours):
            return False, f"last run {age.total_seconds() / 3600:.0f}h ago"
        return last["status"] != "failed", f"last run {last['status']}"

    async def kuma_pusher(self) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                up, msg = self.health()
                try:
                    await client.get(self.settings.kuma_push_url,
                                     params={"status": "up" if up else "down", "msg": msg})
                except httpx.HTTPError as exc:
                    log.warning("kuma push failed: %s", type(exc).__name__)
                await asyncio.sleep(300)
