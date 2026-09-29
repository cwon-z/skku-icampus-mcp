"""Task status: which source decides, how a lagging source shows, and what the default list keeps."""

from datetime import datetime, timedelta

from icampus.config import KST
from icampus.views import build_tasks, filter_tasks, lecture_status

NOW = datetime(2026, 9, 29, 9, 0, tzinfo=KST)
FRESH, OLD = (NOW - timedelta(hours=1)).isoformat(), (NOW - timedelta(hours=72)).isoformat()
CUTOFF = (NOW - timedelta(hours=12)).isoformat()


def at(days: float) -> str:
    return (NOW + timedelta(days=days)).isoformat()


def short(ts: str) -> str:
    return ts[:16].replace("T", " ")


def todo(n: int, **kw) -> dict:
    """A LearningX to-do row for Canvas assignment n, last seen three days ago (LearningX failing since)."""
    return {"key": f"canvas.skku.edu:1001:assignments:{n}", "course_id": "1001", "title": f"task {n}",
            "kind": "assignment", "url": "u", "unlock_at": at(-10), "due_at": at(2), "completed": False,
            "active": True, "first_seen": OLD, "last_seen": OLD, **kw}


def assignment(n: int, state: str = "unsubmitted", types: tuple = ("online_upload",), sub: dict | None = None,
               **kw) -> dict:
    return {"key": f"canvas.skku.edu:1001:assignments:{n}", "course_id": "1001", "id": str(n), "name": f"task {n}",
            "url": "u", "due_at": at(2), "unlock_at": None, "lock_at": None, "submission_types": list(types),
            "submission": {"state": state, "submitted_at": None, "late": False, "missing": False, **(sub or {})},
            "first_seen": FRESH, "last_seen": FRESH, **kw}


def lecture(n: int, **kw) -> dict:
    return {"key": f"canvas.skku.edu:1001:modules/items:{n}", "course_id": "1001", "title": f"video {n}",
            "kind": "video", "week": "5주차", "url": "u", "available_from": at(-3), "due_at": at(2), "late_until": None,
            "completed": False, "attendance": "none", "first_seen": OLD, "last_seen": OLD, **kw}


def build(todos=(), assignments=(), lectures=()) -> dict[str, dict]:
    built = build_tasks(list(todos), list(assignments), list(lectures),
                        [{"id": "1001", "name": "Data Structures", "code": "CSE1001"}], NOW, 2026,
                        outdated_before=CUTOFF)
    return {t["title"]: t for t in built}


def test_canvas_submission_wins_over_a_lagging_todo_list():
    submitted = at(-1.5)
    t = build([todo(1)], [assignment(1, "submitted", sub={"submitted_at": submitted, "late": True})])["task 1"]
    assert (t["status"], t["status_reason"]) == ("done", f"submitted late {short(submitted)} (Canvas)")
    assert t["done"] and t["status_checked_at"] == FRESH and t["assignment_id"] == "1"
    assert t["in_remaining_list"] and t["completed"] is False  # the raw LearningX fields keep what it said


def test_canvas_states():
    got = build(assignments=[
        assignment(1, "pending_review", sub={"submitted_at": at(-1)}),
        assignment(2, "graded", sub={"missing": True}, due_at=at(-3)),
        assignment(3, "graded", sub={"score": 30}),
        assignment(4, "unsubmitted", sub={"excused": True}),
    ])
    assert got["task 1"]["status_reason"].endswith(", waiting to be graded (Canvas)")
    assert (got["task 2"]["status"], got["task 2"]["status_reason"]) == ("missed", "graded as missing (Canvas)")
    assert got["task 3"]["status_reason"] == "graded without an online submission (Canvas)"
    assert got["task 4"]["status"] == "done"


def test_offline_and_external_hand_ins_are_unknown():
    got = build([todo(2)], [  # on-paper work is a task only when the to-do list has it (else a grade column)
        assignment(1, types=("external_tool",), due_at=at(-15)),
        assignment(2, types=("on_paper",)),
        assignment(3, types=("external_tool",), unlock_at=at(5), due_at=at(9)),
    ])
    assert got["task 1"]["status"] == "unknown" and "through an external tool" in got["task 1"]["status_reason"]
    assert got["task 2"]["status_reason"].startswith("handed in on paper")
    assert got["task 3"]["status"] == "upcoming"


def test_late_period_keeps_an_item_open():
    got = build(assignments=[
        assignment(1, sub={"missing": True}, due_at=at(-1), lock_at=at(6)),
        assignment(2, sub={"missing": True}, due_at=at(-8), lock_at=at(-1)),
    ], lectures=[lecture(3, due_at=at(-1), late_until=at(6)), lecture(4, due_at=at(-7), attendance="absent")])
    assert got["task 1"]["status"] == "todo"
    assert got["task 1"]["status_reason"] == \
        f"not submitted (Canvas); past due, still accepted late until {short(at(6))}"
    assert (got["task 2"]["status"], got["task 2"]["status_reason"]) == ("missed", "Canvas marks it missing")
    assert got["video 3"]["status"] == "todo" and "late until" in got["video 3"]["status_reason"]
    assert got["video 4"]["status"] == "missed"
    assert got["video 4"]["status_reason"].startswith("not completed (iCampus) by the deadline; attendance: absent")


def test_learningx_completion_and_staleness():
    got = build([todo(9, kind="video", key="canvas.skku.edu:1001:modules/items:1", title="video 1", completed=True)],
                lectures=[lecture(1, completed=False, last_seen=FRESH), lecture(2), lecture(3, attendance="attendance"),
                          lecture(4, completed=None, last_seen=FRESH)])
    assert got["video 1"]["status"] == "done"  # completion doesn't revert: a True from either source wins
    old = got["video 2"]
    assert old["status"] == "todo" and old["status_checked_at"] == OLD
    assert old["status_reason"] == f"not completed (iCampus) (outdated: iCampus last confirmed this {short(OLD)})"
    assert got["video 3"]["status_reason"] == "attendance recorded as present (iCampus)"
    assert got["video 4"]["status"] == "unknown" and "outdated" not in got["video 4"]["status_reason"]


def test_default_list_and_status_filter():
    tasks = build_tasks(
        [todo(1, unlock_at=None, due_at=None, title="open, no date"), todo(2, unlock_at=at(5), due_at=None,
                                                                         title="locked, no date")],
        [assignment(3, types=("external_tool",), due_at=at(-3)), assignment(4, sub={"missing": True}, due_at=at(-3)),
         assignment(5, "submitted", sub={"submitted_at": at(-1)})],
        [lecture(6, due_at=at(-2), late_until=at(3))],
        [{"id": "1001", "name": "Data Structures", "code": "CSE1001"}], NOW, 2026)

    def titles(**kw):
        return [t["title"] for t in filter_tasks(tasks, now=NOW, **kw)]
    assert titles() == ["video 6", "open, no date"]  # the late-period video stays; the locked undated item waits
    assert titles(status="missed", past_days=7) == ["task 4"]
    assert titles(past_days=7) == ["task 3", "task 4", "video 6", "open, no date"]
    assert titles(status="done") == ["task 5"] and "task 5" in titles(include_done=True)


def test_lecture_status():
    rows = lecture_status([lecture(1, last_seen=FRESH, available_from=at(2), due_at=at(9))], NOW, CUTOFF)
    assert rows[0]["status"] == "upcoming" and rows[0]["status_reason"] == f"opens {short(at(2))}"
