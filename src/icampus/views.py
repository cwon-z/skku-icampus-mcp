"""Read side: course lookup, the merged task list and the grades view. Pure functions."""

from datetime import datetime, timedelta
from typing import Any

from .dates import from_canvas, review_flags

DONE_STATES = {"submitted", "graded", "pending_review"}
# Canvas' own to-do list skips assignments you can't hand in (grade columns, paper exams).
NOT_SUBMITTABLE = {"none", "on_paper", "not_graded", "wiki_page"}
KINDS = ("video", "material", "embedded_resource", "quiz", "assignment", "exam", "unknown")


def resolve_course(courses: list[dict], query: str | None) -> set[str] | None:
    """ID, code (CSE1001), exact name or part of the name -> course IDs. None query = all courses."""
    if not query:
        return None
    q = query.strip().lower()
    for field in ("id", "code"):
        hit = {c["id"] for c in courses if str(c.get(field) or "").lower() == q}
        if hit:
            return hit
    squash = q.replace(" ", "")

    def norm(c: dict) -> str:
        return (c.get("name") or "").lower().replace(" ", "")

    exact = [c for c in courses if norm(c) == squash]
    if len(exact) == 1:
        return {exact[0]["id"]}
    hit = [c for c in courses if squash in norm(c)]
    if len(hit) == 1:
        return {hit[0]["id"]}
    if not hit:
        raise LookupError(f"no course matches {query!r}", [c["name"] for c in courses])
    raise LookupError(f"{query!r} matches several courses", [c["name"] for c in hit])


def _is_done(task: dict) -> bool:
    sub = task.get("submission") or {}
    return sub.get("state") in DONE_STATES or bool(sub.get("excused")) or task.get("completed") is True


def build_tasks(todos: list[dict], assignments: list[dict], lectures: list[dict],
                courses: list[dict], now: datetime, year: int | None) -> list[dict]:
    """One list of things to do, merged by source key. Status fields stay None unless a source said so."""
    by_id = {c["id"]: c for c in courses}
    nowiso = now.isoformat()
    tasks: dict[str, dict[str, Any]] = {}

    def new_task(src: dict, title: str, kind: str, start: str | None) -> dict[str, Any]:
        return {"key": src["key"], "course_id": src["course_id"], "title": title, "kind": kind,
                "url": src.get("url"), "start_at": start, "due_at": src.get("due_at"),
                "in_remaining_list": False, "sources": [], "submission": None, "completed": None,
                "attendance": None, "related": None, "dropped": False,
                "first_seen": src["first_seen"], "last_seen": src["last_seen"]}

    for row in todos:
        t = tasks[row["key"]] = new_task(row, row["title"], row["kind"], row.get("unlock_at"))
        t["sources"].append("todos")
        t["completed"] = row.get("completed")
        # My Page's remaining list = not completed and already unlocked (checked against the UI)
        unlocked = not row.get("unlock_at") or row["unlock_at"] <= nowiso
        t["in_remaining_list"] = bool(row["active"] and row.get("completed") is False and unlocked)
        t["dropped"] = not row["active"]

    for a in assignments:
        t = tasks.get(a["key"])
        if t is None:
            if set(a.get("submission_types") or ["none"]) <= NOT_SUBMITTABLE:
                continue  # nothing to hand in (e.g. a paper midterm's grade column)
            t = tasks[a["key"]] = new_task(a, a["name"], "quiz" if a.get("quiz_id") else "assignment",
                                           a.get("unlock_at"))
        t["sources"].append("canvas")
        t["submission"] = a.get("submission")
        t["points_possible"] = a.get("points_possible")
        if a.get("due_at"):
            t["due_at"] = a["due_at"]  # Canvas' own deadline wins
        if a.get("quiz_id"):
            t["related"] = {"quiz_id": a["quiz_id"]}

    for lec in lectures:
        if lec["kind"] in ("quiz", "assignment", "exam"):
            continue  # Canvas assignments already cover these (different ID namespace)
        t = tasks.get(lec["key"])
        if t is None:
            if not lec.get("due_at"):
                continue  # undated lecture items belong to /lectures, not the to-do list
            t = tasks[lec["key"]] = new_task(lec, lec["title"], lec["kind"], lec.get("available_from"))
        t["sources"].append("lectures")
        t["week"] = lec.get("week")
        t["attendance"] = lec.get("attendance")
        if lec.get("completed") is not None:
            t["completed"] = lec["completed"]
        if not t.get("due_at") and lec.get("due_at"):
            t["due_at"] = lec["due_at"]

    out = []
    for t in tasks.values():
        course = by_id.get(t["course_id"]) or {}
        t["course"], t["course_code"] = course.get("name"), course.get("code")
        t["review_flags"] = review_flags(from_canvas(t["due_at"]), year)  # from the final, merged date
        t["done"] = _is_done(t)
        out.append(t)
    out.sort(key=lambda t: (t["due_at"] is None, t["due_at"] or "", t["course"] or "", t["title"] or ""))
    return out


def filter_tasks(tasks: list[dict], *, now: datetime, course_ids: set[str] | None = None, kind: str | None = None,
                 due_within_days: int | None = 14, past_days: int = 0, include_done: bool = False,
                 include_inactive: bool = False) -> list[dict]:
    """Due in [now - past_days, now + due_within_days]; undated items are always kept."""
    until = (now + timedelta(days=due_within_days)).isoformat() if due_within_days is not None else None
    since = (now - timedelta(days=past_days)).isoformat()
    out = []
    for t in tasks:
        if course_ids is not None and t["course_id"] not in course_ids:
            continue
        if kind and t["kind"] != kind:
            continue
        if t["done"] and not include_done:
            continue
        if t["dropped"] and not include_inactive:
            continue  # no longer returned by iCampus (not the same as done)
        due = t["due_at"]
        if due and (due < since or (until and due > until)):
            continue
        out.append(t)
    return out


def grades(courses: list[dict], assignments: list[dict]) -> list[dict]:
    by_course: dict[str, list[dict]] = {}
    for a in assignments:
        by_course.setdefault(a["course_id"], []).append(a)
    out = []
    for c in courses:
        items = by_course.get(c["id"], [])
        out.append({
            "course_id": c["id"], "course": c.get("name"), "course_code": c.get("code"), "grade": c.get("grade"),
            "grades_hidden": c.get("grades_hidden"),
            "groups": [{"name": n, "weight": w} for n, w in
                       sorted({(a.get("group"), a.get("group_weight")) for a in items}, key=lambda g: g[0] or "")],
            "assignments": [{
                "name": a["name"], "group": a.get("group"), "points_possible": a.get("points_possible"),
                **{k: (a.get("submission") or {}).get(k) for k in ("score", "grade", "state", "late", "missing")},
                "due_at": a.get("due_at"),
            } for a in items],
        })
    return out
