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
