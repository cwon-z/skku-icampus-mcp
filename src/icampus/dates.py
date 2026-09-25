"""Date handling. Everything is shown in Asia/Seoul; nothing is shifted or invented."""

import re
from datetime import datetime

from .config import KST

_TERM_YEAR = re.compile(r"(\d{4})\s*년")


def from_canvas(value: str | None) -> datetime | None:
    """Canvas / LearningX ISO 8601 timestamps (UTC) -> Asia/Seoul."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(KST)


def term_year(term: str | None) -> int | None:
    m = _TERM_YEAR.search(term or "")
    return int(m.group(1)) if m else None


def review_flags(due: datetime | None, year: int | None) -> list[str]:
    """Kept as-is but flagged: some courses reuse items whose deadlines lie in an earlier year."""
    if due is None:
        return ["no_due_date_displayed"]
    if year and due.year < year:
        return ["date_precedes_selected_term_year"]
    return []


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
