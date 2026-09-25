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


async def test_skipped_run_is_recorded_finished(tmp_path):
    import fcntl
    settings = Settings(data_dir=tmp_path, _env_file=None)
    store = Store(tmp_path / "t.db")
    held = open(tmp_path / "sync.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    run_id = await Syncer(settings, store).run("manual")
    assert store.recent_runs(1)[0]["id"] == run_id and store.recent_runs(1)[0]["status"] == "skipped"


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
