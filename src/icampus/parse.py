"""Pure parsers for Canvas / LearningX JSON (no network), so they can be tested against fixtures."""

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .dates import from_canvas, iso, review_flags

# LearningX content types (My Page icons, lecture items) -> a small set of kinds
KIND = {
    "movie": "video", "youtube": "video", "everlec": "video", "screenlecture": "video", "zoom": "video",
    "dashboard_movie": "video",
    "pdf": "material", "file": "material", "dashboard_resource": "material",
    "embed": "embedded_resource",
    "dashboard_quiz": "quiz", "quiz": "quiz",
    "dashboard_assignment": "assignment", "assignment": "assignment",
    "offline_exam": "exam",
}
_SOURCE = re.compile(r"^/courses/(\d+)/(modules/items|assignments|quizzes|discussion_topics)/(\d+)/?$")
_WEEK = re.compile(r"(\d+)")
_COURSE = re.compile(r"^(?P<name>.+)_(?P<code>[A-Z]{3,4}\d{3,4})_(?P<section>[A-Z0-9]+)\((?P<teacher>[^)]*)\)$")


def kind_of(*types: str | None) -> str:
    return next((KIND[t] for t in types if t in KIND), "unknown")


def source_key(url: str | None) -> tuple[str, str] | None:
    """Canvas item URL -> (key, course_id); keys look like
    canvas.skku.edu:<course>:<modules/items|assignments|...>:<id>."""
    parts = urlsplit(url or "")
    m = _SOURCE.match(parts.path)
    if not m or parts.hostname not in (None, "canvas.skku.edu"):
        return None
    course, kind, sid = m.groups()
    return f"canvas.skku.edu:{course}:{kind}:{sid}", course


def split_course_name(raw: str) -> dict:
    """'Data Structures_CSE1001_42(홍길동)' -> name/code/section/teacher (as SKKU names courses)."""
    m = _COURSE.match(raw or "")
    if not m:
        return {"name": raw, "code": None, "section": None, "teacher": None}
    return m.groupdict()


_BLOCKS = ["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"]


def html_to_text(html: str | None) -> str:
    """Announcement HTML -> readable plain text (block tags and <br> become line breaks)."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(_BLOCKS):
        block.append("\n")
    lines = [" ".join(line.split()) for line in soup.get_text().splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def todo(item: dict, course_id: str, year: int | None) -> dict | None:
    """One row of LearningX learner/todos (what My Page's remaining list is built from)."""
    src = source_key(item.get("url"))
    if src is None:
        return None
    key, _ = src
    due = from_canvas(item.get("due_at"))
    return {
        "key": key, "course_id": course_id, "title": item.get("todo_title"),
        "kind": kind_of(item.get("component_type"), item.get("dashboard_content_type")),
        "type_raw": item.get("component_type") or item.get("dashboard_content_type"),
        "url": f"https://canvas.skku.edu{item['url']}",
        "unlock_at": iso(from_canvas(item.get("unlock_at"))), "due_at": iso(due),
        "lock_at": iso(from_canvas(item.get("lock_at"))),
        "completed": item.get("completed"), "ungraded": item.get("is_ungraded"),
        "review_flags": review_flags(due, year),
    }


def lectures(modules: list[dict], attendance: dict, course_id: str, year: int | None) -> list[dict]:
    """LearningX modules?include_detail=true + attendance_items/summary -> one row per lecture item."""
    status = {str(k): v.get("attendance_status") for k, v in (attendance.get("attendance_summaries") or {}).items()}
    rows = []
    for module in modules:
        m = _WEEK.search(module.get("title") or "")
        week_no = int(m.group(1)) if m else module.get("position")
        for it in module.get("module_items") or []:
            data = it.get("content_data") or {}
            content = data.get("item_content_data") or {}
            due = from_canvas(data.get("due_at"))
            item_id = str(data.get("item_id")) if data.get("item_id") else None
            duration = content.get("duration")
            rows.append({
                "key": f"canvas.skku.edu:{course_id}:modules/items:{it['module_item_id']}",
                "course_id": course_id, "week": module.get("title"), "week_no": week_no,
                "title": it.get("title"), "kind": kind_of(content.get("content_type"), it.get("content_type")),
                "type_raw": content.get("content_type") or it.get("content_type"),
                "url": f"https://canvas.skku.edu/courses/{course_id}/modules/items/{it['module_item_id']}",
                "available_from": iso(from_canvas(data.get("unlock_at"))), "due_at": iso(due),
                "late_until": iso(from_canvas(data.get("late_at"))),
                "available_until": iso(from_canvas(data.get("lock_at"))),
                "duration_min": round(duration / 60, 1) if duration else None,
                "completed": it.get("completed") if it.get("completion_determinable") else None,
                "counts_for_attendance": data.get("use_attendance"),
                # LearningX status: attendance (present) / late / absent / none (not decided yet); null = not tracked
                "attendance": status.get(item_id) if item_id else None,
                "period": data.get("lecture_period_status"),
                "review_flags": review_flags(due, year) if due else [],
            })
    return rows


def lesson_attendance(lessons: list[dict]) -> dict:
    """lessons/attendances -> per-course summary (official status per week/lesson)."""
    counts: dict[str, int] = {}
    for lesson in lessons:
        s = lesson.get("attendance_status") or "none"
        counts[s] = counts.get(s, 0) + 1
    return {"counts": counts, "lessons": [
        {"week": x.get("week_position"), "lesson": x.get("lesson_position"), "status": x.get("attendance_status") or "none"}
        for x in lessons]}
