from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from icampus.api import create_app
from icampus.config import KST, Settings
from icampus.store import Record, Store

TOKEN = "t" * 20
NOW = datetime.now(KST)


def at(days: float) -> str:
    return (NOW + timedelta(days=days)).isoformat()


@pytest.fixture
def client(tmp_path):
    store = Store(tmp_path / "t.db")
    courses = [
        {"id": "1001", "name": "Data Structures", "code": "CSE1001", "course_code": "CSE1001_42"},
        {"id": "1002", "name": "Linear Algebra", "code": "MTH2002"},
        {"id": "1003", "name": "미술사입문", "code": "ART1003"},
    ]
    store.replace_scope("courses", "term:x", [Record(c["id"], c["id"], None, c) for c in courses], NOW)
    todos = [
        {"key": "canvas.skku.edu:1001:assignments:5001", "course_id": "1001", "title": "[Quiz] Week 3",
         "kind": "quiz", "url": "u", "unlock_at": at(-5), "due_at": at(2), "completed": False, "review_flags": []},
        {"key": "canvas.skku.edu:1001:modules/items:1", "course_id": "1001", "title": "Old video",
         "kind": "video", "url": "u", "unlock_at": None, "due_at": at(-400), "completed": False,
         "review_flags": ["date_precedes_selected_term_year"]},
        {"key": "canvas.skku.edu:1003:modules/items:2", "course_id": "1003", "title": "No date",
         "kind": "material", "url": "u", "unlock_at": None, "due_at": None, "completed": False,
         "review_flags": ["no_due_date_displayed"]},
        {"key": "canvas.skku.edu:1003:modules/items:3", "course_id": "1003", "title": "Watched",
         "kind": "video", "url": "u", "unlock_at": None, "due_at": at(3), "completed": True, "review_flags": []},
        {"key": "canvas.skku.edu:1003:modules/items:4", "course_id": "1003", "title": "Next week's video",
         "kind": "video", "url": "u", "unlock_at": at(4), "due_at": at(10), "completed": False, "review_flags": []},
    ]
    store.replace_scope("todos", "term:x", [Record(r["key"], r["course_id"], None, r) for r in todos], NOW)
    lectures = [{"key": "canvas.skku.edu:1003:modules/items:4", "course_id": "1003", "title": "Next week's video",
                 "kind": "video", "week": "5주차", "week_no": 5, "available_from": at(4), "due_at": at(10),
                 "completed": False, "attendance": "none"}]
    store.replace_scope("lectures", "course:1003", [Record(r["key"], "1003", None, r) for r in lectures], NOW)
    assignments = [
        {"key": "canvas.skku.edu:1001:assignments:5001", "course_id": "1001", "id": "5001",
         "name": "[Quiz] Week 3", "quiz_id": "6001", "due_at": at(2), "submission": {"state": "unsubmitted"}},
        {"key": "canvas.skku.edu:1002:assignments:9", "course_id": "1002", "id": "9", "name": "Proposal",
         "due_at": at(7), "submission_types": ["online_upload"], "submission": {"state": "submitted", "score": 9}},
        {"key": "canvas.skku.edu:1002:assignments:10", "course_id": "1002", "id": "10", "name": "Far away",
         "due_at": at(40), "submission_types": ["online_upload"], "submission": {"state": "unsubmitted"}},
        {"key": "canvas.skku.edu:1002:assignments:12", "course_id": "1002", "id": "12", "name": "Gradescope HW1",
         "due_at": at(3), "submission_types": ["external_tool"], "submission": {"state": "unsubmitted"}},
        {"key": "canvas.skku.edu:1002:assignments:11", "course_id": "1002", "id": "11", "name": "Midterm (paper)",
         "due_at": None, "submission_types": ["on_paper"], "submission": {"state": "unsubmitted"}},
    ]
    for a in assignments:
        store.replace_scope("assignments", f"course:{a['course_id']}",
                            [Record(x["key"], x["course_id"], None, x) for x in assignments
                             if x["course_id"] == a["course_id"]], NOW)
    ann = {"key": "k", "course_id": "1002", "id": "8001", "title": "[Updated] Project proposal deadline",
           "posted_at": at(-1), "read_state": "unread", "text": "Submit by Friday 23:59."}
    store.replace_scope("announcements", "term:x", [Record("k", "1002", None, ann)], NOW)
    app = create_app(Settings(api_tokens=f"test:{TOKEN}", _env_file=None), store=store, background=False)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


def test_auth(client, tmp_path):
    assert client.get("/health", headers={"Authorization": ""}).status_code == 200
    assert client.get("/api/v1/courses", headers={"Authorization": ""}).status_code == 401
    assert client.get("/api/v1/courses", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/v1/courses?token=" + TOKEN, headers={"Authorization": ""}).status_code == 401
    bare = create_app(Settings(api_tokens="", _env_file=None), store=Store(tmp_path / "b.db"), background=False)
    assert TestClient(bare).get("/api/v1/courses").status_code == 503


def test_course_lookup(client):
    assert client.get("/api/v1/assignments?course=mth2002").json()["count"] == 4
    assert client.get("/api/v1/assignments?course=1002").json()["count"] == 4
    assert client.get("/api/v1/assignments?course=linear algebra").json()["count"] == 4
    r = client.get("/api/v1/assignments?course=o")
    assert r.status_code == 400 and len(r.json()["candidates"]) >= 2


def test_tasks_merge_and_filters(client):
    items = client.get("/api/v1/tasks").json()["items"]
    titles = [t["title"] for t in items]
    # quiz row and its Canvas assignment are one task; submitted, completed, past and far-future ones are hidden
    assert titles == ["[Quiz] Week 3", "Gradescope HW1", "Next week's video", "No date"]
    quiz = items[0]
    assert quiz["sources"] == ["todos", "canvas"] and quiz["related"] == {"quiz_id": "6001"}
    assert quiz["in_remaining_list"] and quiz["submission"]["state"] == "unsubmitted"
    upcoming = items[2]  # not unlocked yet: shown as upcoming, but not on the remaining list
    assert not upcoming["in_remaining_list"] and upcoming["week"] == "5주차" and upcoming["attendance"] == "none"

    past = client.get("/api/v1/tasks?past_days=500").json()["items"]
    assert "Old video" in [t["title"] for t in past]
    assert "Old video" not in [t["title"] for t in client.get("/api/v1/tasks?past_days=7").json()["items"]]
    limited = client.get("/api/v1/tasks?limit=1").json()
    assert limited["count"] == 1 and limited["total"] == 4 and limited["truncated"]
    assert client.get("/api/v1/tasks?kind=nonsense").status_code == 422
    done = client.get("/api/v1/tasks?include_done=true&due_within_days=60").json()["items"]
    assert {"Proposal", "Far away", "Watched"} <= {t["title"] for t in done}
    assert "Midterm (paper)" not in {t["title"] for t in done}  # nothing to hand in
    assert [t["kind"] for t in client.get("/api/v1/tasks?kind=material").json()["items"]] == ["material"]


def test_lectures(client):
    body = client.get("/api/v1/lectures?course=미술사").json()
    assert body["count"] == 1 and "attendance" not in body and body["items"][0]["course"] == "미술사입문"
    assert client.get("/api/v1/lectures?include_attendance=true").json()["attendance"] == []
    assert client.get("/api/v1/lectures?week=5").json()["count"] == 1
    assert client.get("/api/v1/lectures?week=15").json()["count"] == 0  # exact week, not a substring
    assert client.get("/api/v1/lectures?available_only=true").json()["count"] == 0  # opens in 4 days
    assert client.get("/api/v1/lectures?upcoming_only=true").json()["count"] == 1  # due in 10 days


def test_retry_login_needs_admin(client):
    assert client.post("/api/v1/sync?retry_login=true").status_code == 403


def test_announcements(client):
    body = client.get("/api/v1/announcements?unread_only=true").json()
    assert body["count"] == 1 and "text" not in body["items"][0] and body["items"][0]["preview"]
    assert client.get("/api/v1/announcements/8001").json()["text"].startswith("Submit")
    assert client.get("/api/v1/announcements/1").status_code == 404


def test_status_and_export(client):
    status = client.get("/api/v1/status").json()
    assert status["login"]["blocked"] is None and status["datasets"]["courses"]
    assert client.get("/api/v1/export").json()["version"] == 1
