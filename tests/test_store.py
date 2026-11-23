from datetime import datetime, timedelta

import pytest

from icampus.config import KST
from icampus.store import Record, Store

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=KST)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def rec(key, course="1"):
    return Record(key=key, course_id=course, due_at=None, data={"key": key, "course_id": course})


def test_replace_only_touches_its_scope(store):
    store.replace_scope("lectures", "course:1", [rec("a"), rec("b")], T0)
    store.replace_scope("lectures", "course:2", [rec("c", "2")], T0)
    upserted, gone = store.replace_scope("lectures", "course:1", [rec("a")], T0 + timedelta(hours=1))
    assert (upserted, gone) == (1, 1)
    assert {r["key"] for r in store.records("lectures")} == {"a", "c"}
    assert {r["key"]: r["active"] for r in store.records("lectures", include_inactive=True)} == {
        "a": True, "b": False, "c": True}


def test_reappearing_row_keeps_first_seen(store):
    store.replace_scope("mypage", "term", [rec("a")], T0)
    store.replace_scope("mypage", "term", [], T0 + timedelta(hours=1))
    store.replace_scope("mypage", "term", [rec("a")], T0 + timedelta(hours=2))
    row = store.record("mypage", "a")
    assert row["active"] and row["first_seen"] == T0.isoformat()
    assert row["last_seen"] == (T0 + timedelta(hours=2)).isoformat()


def test_failed_scope_keeps_old_data_and_time(store):
    store.replace_scope("lectures", "course:1", [rec("a")], T0)
    store.replace_scope("lectures", "course:2", [rec("c", "2")], T0)
    # next run: course 2 fails, so only course 1 is replaced
    store.replace_scope("lectures", "course:1", [rec("a")], T0 + timedelta(hours=3))
    assert store.record("lectures", "c")["active"]
    assert store.synced_at("lectures") == T0.isoformat()


def test_expected_scopes(store):
    store.replace_scope("lectures", "course:1", [rec("a")], T0)
    assert store.synced_at("lectures", ["course:1"]) == T0.isoformat()
    assert store.synced_at("lectures", ["course:1", "course:2"]) is None  # course 2 never synced


def test_retire_scopes(store):
    store.replace_scope("lectures", "course:1", [rec("a")], T0)
    store.replace_scope("lectures", "course:old", [rec("x", "old")], T0)
    store.retire_scopes("lectures", {"course:1"})
    assert [r["key"] for r in store.records("lectures")] == ["a"]
    assert [s["scope"] for s in store.scopes("lectures")] == ["course:1"]


def test_state_and_runs(store):
    store.set_state("login_block", {"reason": "rejected"})
    assert store.get_state("login_block") == {"reason": "rejected"}
    run = store.start_run("manual", T0)
    store.mark_interrupted()
    assert store.recent_runs()[0]["status"] == "interrupted"
    store.finish_run(run, "ok", {"n": 1}, T0)
    assert store.recent_runs()[0]["detail"] == {"n": 1}


def test_a_version_1_database_is_migrated_in_place(tmp_path):
    """What an existing install has: the first schema, user_version 1, rows and state."""
    import sqlite3

    from icampus.store import MIGRATIONS, SCHEMA
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(SCHEMA + "PRAGMA user_version = 1;")
    old.execute("INSERT INTO records VALUES ('lectures', 'a', 'course:1', '1', NULL, '{\"key\": \"a\"}', 't', 't', 1)")
    old.execute("INSERT INTO state VALUES ('term', '\"2026-2\"')")
    old.commit()
    old.close()

    store = Store(path)
    assert store.upgraded_from == 1 and store.schema_version == len(MIGRATIONS)
    assert [r["key"] for r in store.records("lectures")] == ["a"] and store.get_state("term") == "2026-2"
    tables = {r["name"] for r in store.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"settings", "cache_entries"} <= tables
    store.execute("INSERT INTO settings VALUES ('video_cache_gb', '20', 't')")
    store.close()

    again = Store(path)  # nothing to do the second time, and nothing lost
    assert again.upgraded_from is None and again.one("SELECT value FROM settings")["value"] == "20"
    # the first version of this program set user_version back to 1 at every start: after such a downgrade the
    # scripts run again and must leave the newer tables' rows alone
    again.execute("PRAGMA user_version = 1")
    again.close()
    third = Store(path)
    assert third.upgraded_from == 1 and third.one("SELECT value FROM settings")["value"] == "20"
    assert Store(tmp_path / "new.db").upgraded_from == 0


def test_transactions_are_all_or_nothing(store):
    with pytest.raises(RuntimeError):
        with store.transaction() as db:
            db.execute("INSERT INTO settings VALUES ('a', '1', 't')")
            raise RuntimeError("halfway")
    assert store.query("SELECT * FROM settings") == []
    with store.transaction() as db:
        db.execute("INSERT INTO settings VALUES ('a', '1', 't')")
    assert store.one("SELECT value FROM settings WHERE key='a'")["value"] == "1"
