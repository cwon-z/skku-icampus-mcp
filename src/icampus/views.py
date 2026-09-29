"""Read side: course lookup, the merged task list and the grades view. Pure functions."""

from datetime import datetime, timedelta
from typing import Any

from .dates import from_canvas, review_flags

DONE_STATES = {"submitted", "graded", "pending_review"}
# Canvas' own to-do list skips assignments you can't hand in (grade columns, paper exams).
NOT_SUBMITTABLE = {"none", "on_paper", "not_graded", "wiki_page"}
# Handed in outside Canvas: it records nothing until a score comes back.
OFFLINE = {"external_tool": "through an external tool", "on_paper": "on paper",
           "none": "offline", "not_graded": "offline"}
KINDS = ("video", "material", "embedded_resource", "quiz", "assignment", "exam", "unknown")
# todo: open, not done yet · done · missed: deadline (and any late period) passed · upcoming: not open yet
# unknown: iCampus can't tell (handed in offline, completion not tracked)
STATUSES = ("todo", "done", "missed", "upcoming", "unknown")


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


def _short(ts: str) -> str:
    return ts[:16].replace("T", " ")


def _deadline(item: dict) -> str | None:
    """The last moment it still counts: the due date, or the end of a late period after it."""
    return max(filter(None, (item.get("due_at"), item.get("late_until"))), default=None)


def _by_dates(item: dict, now: str, not_done: str) -> tuple[str, str]:
    """Something not done yet: upcoming, open (todo) or past its deadline (missed)."""
    start, due, late = item.get("start_at"), item.get("due_at"), item.get("late_until")
    if start and start > now:
        return "upcoming", f"opens {_short(start)}"
    if due and due < now:
        if late and late >= now:
            return "todo", f"{not_done}; past due, still accepted late until {_short(late)}"
        return "missed", f"{not_done} by the deadline"
    return "todo", not_done


def task_status(item: dict, now: str) -> tuple[str, str, str | None]:
    """(status, reason, checked_at) for one merged item. Canvas decides for what you hand in; LearningX (the to-do
    list and the lecture data) for what you watch or read. checked_at is when that source last confirmed it."""
    sub, seen = item.get("submission"), item["_seen"]
    if sub:
        state, at = sub.get("state"), seen.get("canvas")
        if sub.get("excused"):
            return "done", "excused (Canvas)", at
        if state in DONE_STATES and sub.get("submitted_at"):
            graded = {"graded": ", graded", "pending_review": ", waiting to be graded"}.get(state, "")
            late = " late" if sub.get("late") else ""
            return "done", f"submitted{late} {_short(sub['submitted_at'])}{graded} (Canvas)", at
        if state == "graded":
            if sub.get("missing"):
                return "missed", "graded as missing (Canvas)", at
            return "done", "graded without an online submission (Canvas)", at
        if state in DONE_STATES:
            return "done", f"{state.replace('_', ' ')} (Canvas)", at
        if item.get("completed") is True:
            return "done", "completed on the iCampus to-do list; Canvas shows no submission", seen.get("learningx")
        if sub.get("missing") and not (item.get("late_until") and item["late_until"] >= now):
            return "missed", "Canvas marks it missing", at
        types = set(item.get("submission_types") or [])
        if types and types <= OFFLINE.keys() and not (item.get("start_at") and item["start_at"] > now):
            how = OFFLINE[sorted(types)[0]]
            return "unknown", f"handed in {how}: Canvas shows nothing until it is graded", at
        status, reason = _by_dates(item, now, "not submitted (Canvas)")
        return status, reason, at
    if item.get("completed") is True:
        return "done", "completed (iCampus)", seen.get("learningx")
    if item.get("attendance") in ("attendance", "late"):
        word = "present" if item["attendance"] == "attendance" else "late"
        return "done", f"attendance recorded as {word} (iCampus)", seen.get("lectures")
    if item.get("completed") is None:
        latest = max(filter(None, (seen.get("todos"), seen.get("lectures"))), default=None)
        return "unknown", "iCampus doesn't report completion for this item", latest
    status, reason = _by_dates(item, now, "not completed (iCampus)")
    if status == "missed" and item.get("attendance") == "absent":
        reason += "; attendance: absent"
    return status, reason, seen.get("learningx")


def _set_status(item: dict, now: str, outdated_before: str | None) -> None:
    """Adds status, status_reason and status_checked_at. A status that isn't done and rests on data older than
    outdated_before (its part of iCampus hasn't synced since) says so in its reason."""
    status, reason, at = task_status(item, now)
    if outdated_before and status != "done" and at and at < outdated_before:
        reason += f" (outdated: iCampus last confirmed this {_short(at)})"
    item.update(status=status, status_reason=reason, status_checked_at=at)


def _merge_completion(t: dict, completed: bool | None, seen: str) -> None:
    """LearningX reports completion twice (to-do list, lecture data). Completion doesn't revert, so True wins;
    two reports of 'not completed' count from the later one."""
    if completed is None or t["completed"] is True:
        return
    if t["completed"] is None or completed is True:
        t["completed"], t["_seen"]["learningx"] = completed, seen
    else:
        t["_seen"]["learningx"] = max(seen, t["_seen"]["learningx"])


def build_tasks(todos: list[dict], assignments: list[dict], lectures: list[dict],
                courses: list[dict], now: datetime, year: int | None, *,
                outdated_before: str | None = None) -> list[dict]:
    """One list of things to do, merged by source key, each with a status. Raw source fields (submission,
    completed, attendance) stay None unless a source said so."""
    by_id = {c["id"]: c for c in courses}
    nowiso = now.isoformat()
    tasks: dict[str, dict[str, Any]] = {}

    def new_task(src: dict, title: str, kind: str, start: str | None) -> dict[str, Any]:
        return {"key": src["key"], "course_id": src["course_id"], "title": title, "kind": kind,
                "url": src.get("url"), "start_at": start, "due_at": src.get("due_at"), "late_until": None,
                "in_remaining_list": False, "sources": [], "submission": None, "completed": None,
                "attendance": None, "related": None, "dropped": False, "_seen": {},
                "first_seen": src["first_seen"], "last_seen": src["last_seen"]}

    for row in todos:
        t = tasks[row["key"]] = new_task(row, row["title"], row["kind"], row.get("unlock_at"))
        t["sources"].append("todos")
        t["_seen"]["todos"] = row["last_seen"]
        _merge_completion(t, row.get("completed"), row["last_seen"])
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
        t["_seen"]["canvas"] = a["last_seen"]
        t["assignment_id"] = a.get("id")
        t["submission"] = a.get("submission")
        t["submission_types"] = a.get("submission_types")
        t["points_possible"] = a.get("points_possible")
        t["attachments"] = len(a.get("attachments") or []) or None
        if a.get("due_at"):
            t["due_at"] = a["due_at"]  # Canvas' own deadline wins
            # after the due date and before lock_at Canvas still takes late submissions
            if a.get("lock_at") and a["lock_at"] > a["due_at"]:
                t["late_until"] = a["lock_at"]
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
        t["_seen"]["lectures"] = lec["last_seen"]
        t["week"] = lec.get("week")
        t["attendance"] = lec.get("attendance")
        _merge_completion(t, lec.get("completed"), lec["last_seen"])
        if not t.get("due_at") and lec.get("due_at"):
            t["due_at"] = lec["due_at"]
        if lec.get("late_until") and not t.get("late_until"):
            t["late_until"] = lec["late_until"]

    out = []
    for t in tasks.values():
        course = by_id.get(t["course_id"]) or {}
        t["course"], t["course_code"] = course.get("name"), course.get("code")
        t["review_flags"] = review_flags(from_canvas(t["due_at"]), year)  # from the final, merged date
        _set_status(t, nowiso, outdated_before)
        if t["dropped"]:
            t["status_reason"] += " (no longer on the iCampus to-do list)"
        t["done"] = t["status"] == "done"
        del t["_seen"]
        out.append(t)
    out.sort(key=lambda t: (t["due_at"] is None, t["due_at"] or "", t["course"] or "", t["title"] or ""))
    return out


def lecture_status(rows: list[dict], now: datetime, outdated_before: str | None = None) -> list[dict]:
    """Lecture items with the same status fields as tasks, from their LearningX completion and attendance."""
    out = []
    for row in rows:
        item = {"start_at": row.get("available_from"), "due_at": row.get("due_at"),
                "late_until": row.get("late_until"), "completed": row.get("completed"),
                "attendance": row.get("attendance"),
                "_seen": {"learningx": row["last_seen"], "lectures": row["last_seen"]}}
        _set_status(item, now.isoformat(), outdated_before)
        out.append({**row, **{k: item[k] for k in ("status", "status_reason", "status_checked_at")}})
    return out


def filter_tasks(tasks: list[dict], *, now: datetime, course_ids: set[str] | None = None, kind: str | None = None,
                 status: str | None = None, due_within_days: int | None = 14, past_days: int = 0,
                 include_done: bool = False, include_inactive: bool = False) -> list[dict]:
    """Due in [now - past_days, now + due_within_days], where a late period still counts as due. Undated items are
    kept once they are open. Done items only with include_done (or status='done')."""
    until = (now + timedelta(days=due_within_days)).isoformat() if due_within_days is not None else None
    since = (now - timedelta(days=past_days)).isoformat()
    out = []
    for t in tasks:
        if course_ids is not None and t["course_id"] not in course_ids:
            continue
        if kind and t["kind"] != kind:
            continue
        if status and t["status"] != status:
            continue
        if t["status"] == "done" and not (include_done or status == "done"):
            continue
        if t["dropped"] and not include_inactive:
            continue  # no longer returned by iCampus (not the same as done)
        # a late period keeps an item current, unless iCampus can't tell whether it was handed in at all
        deadline = t["due_at"] if t["status"] == "unknown" else _deadline(t)
        if deadline and deadline < since:
            continue
        if until and t["due_at"] and t["due_at"] > until:
            continue
        if not deadline and t["status"] == "upcoming":
            continue  # no due date and not open yet: nothing to do about it now
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
