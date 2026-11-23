from datetime import datetime

from icampus.config import KST, Settings
from icampus.store import Store
from icampus.sync import Syncer


def test_next_slot_never_repeats(tmp_path):
    syncer = Syncer(Settings(sync_times="07:30,22:30", _env_file=None), Store(tmp_path / "t.db"))
    first = syncer._next_slot(datetime(2026, 9, 26, 7, 25, tzinfo=KST))
    assert first == datetime(2026, 9, 26, 7, 30, tzinfo=KST)
    # after running the 07:30 slot early (negative jitter) the next pick must be 22:30, not 07:30 again
    assert syncer._next_slot(first) == datetime(2026, 9, 26, 22, 30, tzinfo=KST)
    assert syncer._next_slot(datetime(2026, 9, 26, 23, 0, tzinfo=KST)) == datetime(2026, 9, 27, 7, 30, tzinfo=KST)
    assert Syncer(Settings(sync_times="", _env_file=None), Store(tmp_path / "u.db"))._next_slot(first) is None


async def test_skipped_run_is_recorded_finished(tmp_path, monkeypatch):
    import fcntl

    import icampus.sync as sync_mod
    monkeypatch.setattr(sync_mod, "LOCK_WAIT_S", 0)
    settings = Settings(data_dir=tmp_path, _env_file=None)
    store = Store(tmp_path / "t.db")
    held = open(tmp_path / "sync.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    syncer = Syncer(settings, store)
    run_id = await syncer.run("manual")
    assert store.recent_runs(1)[0]["id"] == run_id and store.recent_runs(1)[0]["status"] == "skipped"
    assert not syncer.running


async def test_lock_waits_for_the_holder(tmp_path):
    import asyncio

    from icampus.browser import exclusive

    async def hold(seconds):
        async with exclusive(tmp_path) as got:
            assert got
            await asyncio.sleep(seconds)

    holder = asyncio.create_task(hold(0.3))
    await asyncio.sleep(0.05)
    async with exclusive(tmp_path) as busy:
        assert not busy
    async with exclusive(tmp_path, wait_s=5) as waited:
        assert waited and holder.done()


def test_health_names_datasets_left_behind(tmp_path):
    from datetime import timedelta

    from icampus.sync import DATASETS
    store = Store(tmp_path / "t.db")
    syncer = Syncer(Settings(stale_hours=12, _env_file=None), store)
    now = datetime.now(KST)
    for d in DATASETS:  # LearningX failing for days while Canvas keeps syncing
        store.replace_scope(d, "s", [], now - timedelta(hours=72 if d in ("todos", "lectures") else 1))
    store.finish_run(store.start_run("schedule", now), "partial", {}, now)
    assert syncer.health() == (False, "last run partial; not updated for 12h+: todos, lectures")
    for d in ("todos", "lectures"):
        store.replace_scope(d, "s", [], now)
    assert syncer.health() == (True, "last run partial")


def test_health_for_a_term_without_courses(tmp_path):
    from icampus.sync import PER_COURSE, PER_TERM
    store = Store(tmp_path / "t.db")
    now = datetime.now(KST)
    for d in PER_TERM:
        store.replace_scope(d, "term:x", [], now)
    store.set_state("expected_scopes", {**{d: ["term:x"] for d in PER_TERM}, **{d: [] for d in PER_COURSE}})
    store.finish_run(store.start_run("schedule", now), "ok", {}, now)
    assert Syncer(Settings(_env_file=None), store).health() == (True, "last run ok")


async def test_scheduler_skips_slot_already_run_before_restart(tmp_path, monkeypatch):
    """A restart shortly before a slot must not repeat a run the old process already did early."""
    import asyncio
    from datetime import timedelta

    import icampus.sync as sync_mod
    store = Store(tmp_path / "t.db")
    syncer = Syncer(Settings(sync_times="07:30", sync_jitter_min=10, data_dir=tmp_path, _env_file=None), store)
    now = datetime(2026, 9, 26, 7, 25, tzinfo=KST)
    store.finish_run(store.start_run("schedule", now - timedelta(minutes=3)), "ok", {}, now)  # ran at 07:22

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(sync_mod, "datetime", Clock)
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        raise asyncio.CancelledError
    monkeypatch.setattr(sync_mod.asyncio, "sleep", fake_sleep)
    try:
        await syncer.scheduler()
    except asyncio.CancelledError:
        pass
    assert syncer.next_run.date() == datetime(2026, 9, 27).date()  # tomorrow's 07:30, not today's again


async def test_one_sync_after_an_upgrade_respects_the_cooldown_and_the_login_guard(tmp_path, monkeypatch):
    import asyncio
    from datetime import timedelta

    import icampus.sync as sync_mod
    store = Store(tmp_path / "t.db")
    syncer = Syncer(Settings(data_dir=tmp_path, manual_cooldown_min=10, _env_file=None), store)
    assert not syncer.needs_upgrade_sync()  # a new database: the first sync writes the current shape anyway
    store.replace_scope("lectures", "course:1001", [], datetime.now(KST))  # rows from an older version
    assert syncer.needs_upgrade_sync()

    runs = []

    async def fake_run(trigger, *, retry_login=False):
        runs.append(trigger)
        store.set_state("collect_version", sync_mod.COLLECT_VERSION)
        return 1
    monkeypatch.setattr(syncer, "run", fake_run)

    store.set_state("login_block", {"reason": "password rejected", "at": "x"})
    await syncer.upgrade_sync()
    assert runs == [] and syncer.needs_upgrade_sync()  # blocked logins stay blocked

    store.set_state("login_block", None)
    store.set_state("last_manual_at", (datetime.now(KST) - timedelta(minutes=9)).isoformat())  # synced by hand 9 min ago
    waits = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        waits.append(seconds)
        store.set_state("last_manual_at", None)  # the cooldown passes
        await real_sleep(0)
    monkeypatch.setattr(sync_mod.asyncio, "sleep", fake_sleep)
    await syncer.upgrade_sync()
    assert runs == ["upgrade"] and 0 < waits[0] <= 61 and not syncer.needs_upgrade_sync()
    await syncer.upgrade_sync()
    assert runs == ["upgrade"]  # once is enough


def test_changing_the_sync_times_never_cancels_a_sync_that_is_running(tmp_path):
    """The schedule loop is started over for new times, but not in the middle of a run: cancelling the loop then
    would cancel the sync it is awaiting. It is done when the run has ended."""
    import time

    from fastapi.testclient import TestClient

    from icampus.api import create_app
    token = "a" * 20
    app = create_app(Settings(api_tokens=f"admin:{token}", data_dir=tmp_path, sync_times="", _env_file=None),
                     store=Store(tmp_path / "t.db"), background=True)
    syncer = app.state.syncer
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        assert syncer.next_run is None  # no schedule yet
        syncer.running = True  # a sync is in the middle of its run
        assert c.put("/api/v1/settings", json={"sync_times": "06:00"}).status_code == 200
        time.sleep(0.05)
        assert syncer.next_run is None  # the loop was left alone
        syncer.running = False  # the run ends, and says so the way Syncer.run does
        c.portal.call(lambda: [callback() for callback in syncer.when_idle])
        for _ in range(50):
            if syncer.next_run is not None:
                break
            time.sleep(0.02)
        assert syncer.next_run is not None and abs(syncer.next_run.hour * 60 + syncer.next_run.minute - 360) <= 10
        first = syncer.next_run
        assert c.put("/api/v1/settings", json={"sync_times": "18:00"}).status_code == 200  # idle: applies at once
        for _ in range(50):
            if syncer.next_run != first:
                break
            time.sleep(0.02)
        assert abs(syncer.next_run.hour * 60 + syncer.next_run.minute - 18 * 60) <= 10


async def test_a_run_tells_its_listeners_when_it_is_over_whatever_happened(tmp_path, monkeypatch):
    import fcntl

    import icampus.sync as sync_mod
    monkeypatch.setattr(sync_mod, "LOCK_WAIT_S", 0)
    syncer = Syncer(Settings(data_dir=tmp_path, _env_file=None), Store(tmp_path / "t.db"))
    seen = []
    syncer.when_idle.append(lambda: seen.append(syncer.running))
    syncer.when_idle.append(lambda: 1 / 0)  # a listener that fails must not break the run
    held = open(tmp_path / "sync.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    await syncer.run("manual")  # skipped: another sync holds the lock
    assert seen == [False]
