"""Collectors: one logged-in session -> Records for the store. GET requests only."""

import re
from datetime import date, datetime, timedelta

from . import parse
from .browser import Session
from .config import CANVAS, KST
from .dates import from_canvas, iso
from .store import Record

_TERM = re.compile(r"(\d{4})\s*년\s*(\d)\s*학기")


def pick_term(courses: list[dict], override: str = "") -> str | None:
    """Current term: the override, else a term whose dates contain today, else the latest 'YYYY년 N학기'.
    (SKKU terms have no end date in Canvas, so the fallback is the usual path.)"""
    if override:
        return override
    now = datetime.now(KST)
    terms = [c.get("term") or {} for c in courses]
    live = [t["name"] for t in terms if t.get("name") and _TERM.search(t["name"]) and t.get("start_at")
            and t.get("end_at") and from_canvas(t["start_at"]) <= now <= from_canvas(t["end_at"])]
    if live:
        return max(set(live), key=live.count)
    regular = [(int(m.group(1)), int(m.group(2)), t["name"]) for t in terms
               if t.get("name") and (m := _TERM.search(t["name"]))]
    return max(regular)[2] if regular else None


def term_start(courses: list[dict]) -> date:
    starts = [s for c in courses if (s := from_canvas((c.get("term") or {}).get("start_at")))]
    return min(starts).date() if starts else (datetime.now(KST) - timedelta(days=120)).date()


async def courses(s: Session, term_override: str = "") -> tuple[str | None, list[Record], list[dict]]:
    raw = await s.canvas("/api/v1/courses", {
        "enrollment_state": "active", "include[]": ["term", "total_scores", "teachers"]})
    term = pick_term(raw, term_override)
    current = [c for c in raw if (c.get("term") or {}).get("name") == term]
    records = []
    for c in current:
        cid = str(c["id"])
        names = parse.split_course_name(c.get("name") or "")
        enrollment = next((e for e in c.get("enrollments") or [] if e.get("type") == "student"), {})
        grade = {k: enrollment.get(f"computed_{k}") for k in
                 ("current_score", "current_grade", "final_score", "final_grade")}
        records.append(Record(key=cid, course_id=cid, due_at=None, data={
            "id": cid, "name": names["name"], "code": names["code"], "section": names["section"],
            "full_name": c.get("name"), "term": term, "url": f"{CANVAS}/courses/{cid}",
            "teachers": [t.get("display_name") for t in c.get("teachers") or []] or [names["teacher"]],
            "grade": grade, "grades_hidden": bool(c.get("hide_final_grades")),
        }))
    return term, records, current


async def assignments(s: Session, course_id: str) -> list[Record]:
    groups = await s.canvas(f"/api/v1/courses/{course_id}/assignment_groups",
                            {"include[]": ["assignments", "submission"]})
    records = []
    for g in groups:
        for a in g.get("assignments") or []:
            sub = a.get("submission") or {}
            due = from_canvas(a.get("due_at"))
            key = f"canvas.skku.edu:{course_id}:assignments:{a['id']}"
            records.append(Record(key=key, course_id=course_id, due_at=due, data={
                "key": key, "course_id": course_id, "id": str(a["id"]), "name": a.get("name"),
                "url": a.get("html_url"), "group": g.get("name"), "group_weight": g.get("group_weight"),
                "due_at": iso(due), "unlock_at": iso(from_canvas(a.get("unlock_at"))),
                "lock_at": iso(from_canvas(a.get("lock_at"))), "points_possible": a.get("points_possible"),
                "submission_types": a.get("submission_types"),
                "quiz_id": str(a["quiz_id"]) if a.get("quiz_id") else None,
                "submission": {
                    "state": sub.get("workflow_state"), "submitted_at": iso(from_canvas(sub.get("submitted_at"))),
                    "late": sub.get("late"), "missing": sub.get("missing"), "excused": sub.get("excused"),
                    "score": sub.get("score"), "grade": sub.get("grade"),
                } if sub else None,
            }))
    return records


async def announcements(s: Session, course_ids: list[str], since: date) -> list[Record]:
    if not course_ids:
        return []
    raw = await s.canvas("/api/v1/announcements", {
        "context_codes[]": [f"course_{c}" for c in course_ids],
        "start_date": since.isoformat(), "end_date": (datetime.now(KST).date() + timedelta(days=1)).isoformat()})
    records = []
    for a in raw:
        course_id = (a.get("context_code") or "").removeprefix("course_")
        key = f"canvas.skku.edu:{course_id}:discussion_topics:{a['id']}"
        records.append(Record(key=key, course_id=course_id, due_at=None, data={
            "key": key, "course_id": course_id, "id": str(a["id"]), "title": a.get("title"),
            "posted_at": iso(from_canvas(a.get("posted_at"))), "author": (a.get("author") or {}).get("display_name"),
            "read_state": a.get("read_state"), "url": a.get("html_url"), "text": parse.html_to_text(a.get("message")),
            "attachments": [f.get("display_name") for f in a.get("attachments") or []],
        }))
    return records


async def todos(s: Session, course_ids: list[str], year: int | None) -> list[Record]:
    """LearningX learner/todos: the data behind My Page's remaining to-do list."""
    raw = await s.learningx("/learner/todos", {"course_ids[]": course_ids})
    records = []
    for course in raw:
        cid = str(course.get("course_id"))
        if cid not in course_ids:
            continue
        for item in course.get("todo_list") or []:
            row = parse.todo(item, cid, year)
            if row:
                records.append(Record(row["key"], cid, from_canvas(item.get("due_at")), row))
    return records


async def lectures(s: Session, course_id: str, year: int | None) -> list[Record]:
    modules = await s.learningx(f"/courses/{course_id}/modules", {"include_detail": "true"})
    attendance = await s.learningx(f"/courses/{course_id}/attendance_items/summary", {"only_use_attendance": "true"})
    return [Record(r["key"], course_id, from_canvas(r["due_at"]), r)
            for r in parse.lectures(modules, attendance, course_id, year)]


async def attendance(s: Session, course_id: str) -> list[Record]:
    """Official attendance per week/lesson (what 출결현황 shows), one summary record per course."""
    lessons = await s.learningx(f"/courses/{course_id}/lessons/attendances")
    return [Record(course_id, course_id, None, {"course_id": course_id, **parse.lesson_attendance(lessons)})]
