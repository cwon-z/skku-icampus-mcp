"""One-off feasibility check: can lecture videos be fetched without opening their viewer?

Reads the same LearningX `modules?include_detail=true` list the sync reads, and reports the shape of the
video items' `item_content_data` (key names, URL patterns with IDs masked), never their values. With
--head N it also sends HEAD requests for the media files other SKKU tools fetch
(<lcms content path>/<content_id>/contents/media_files/*.mp4) for N videos, without cookies, and reports only
status, type and size. A HEAD downloads nothing and loads no viewer or player, so it reports no watch progress;
the content server still sees the request. Nothing here opens an item page.
"""

import re
from collections import Counter
from urllib.parse import urlsplit

import httpx

from . import collect, parse
from .browser import NotLoggedIn, exclusive, open_session
from .config import Settings
from .store import Store

# Media files under a Commons content folder, as seen in public SKKU browser extensions (not yet confirmed).
CANDIDATES = ["media_files/screen.mp4", "media_files/main.mp4", "media_files/sub.mp4",
              "media_files/mobile/ssmovie.mp4", "media_files/mobile/screen.mp4", "media_files/mobile/main.mp4"]
_ID = re.compile(r"(?i)[0-9a-f]{12,}|[0-9a-f-]{32,}|\d{3,}")


def mask(url: str | None) -> str | None:
    """host + path with long IDs replaced, so the pattern shows but no content or user ID does."""
    if not url:
        return None
    p = urlsplit(url)
    return f"{p.scheme}://{p.hostname}{_ID.sub('<id>', p.path)}" + (" (+query)" if p.query else "")


def content_base(icd: dict) -> str | None:
    """https://lcms.skku.edu/.../<content_id>/contents/ from the thumbnail URL, if it has that shape."""
    cid, thumb = str(icd.get("content_id") or ""), icd.get("thumbnail_url") or ""
    parts = urlsplit(thumb)
    if not cid or cid not in parts.path or parts.scheme != "https" or not (parts.hostname or "").endswith("skku.edu"):
        return None
    return f"https://{parts.hostname}{parts.path.split(cid)[0]}{cid}/contents/"


async def check(settings: Settings, course: str | None, head: int) -> str:
    store = Store(settings.db_path)
    async with exclusive(settings.data_dir, 45) as held:
        if not held:
            return "a sync or download holds the session; try again in a minute"
        async with open_session(settings, store) as s:
            try:
                await s.ensure_login(allow_credentials=False)
            except NotLoggedIn:
                return ("the saved iCampus login has expired: run `uv run icampus sync` once (it signs in again), "
                        "then run this check again")
            _, _, raw = await collect.courses(s, settings.term)
            ids = [str(c["id"]) for c in raw if not course or str(c["id"]) == course]
            videos = []
            for cid in ids:
                for module in await s.learningx(f"/courses/{cid}/modules", {"include_detail": "true"}):
                    for it in module.get("module_items") or []:
                        icd = (it.get("content_data") or {}).get("item_content_data") or {}
                        if parse.kind_of(icd.get("content_type"), it.get("content_type")) == "video":
                            videos.append(icd)

    lines = [f"courses checked: {len(ids)}", f"video items: {len(videos)}",
             f"types: {dict(Counter(v.get('content_type') for v in videos))}"]
    keys = Counter(k for v in videos for k in v)
    lines.append("item_content_data keys (how many videos have each): "
                 + ", ".join(f"{k}={n}" for k, n in sorted(keys.items())))
    for field in ("thumbnail_url", "view_url"):
        lines.append(f"{field} patterns: {dict(Counter(mask(v.get(field)) for v in videos))}")
    bases = [b for v in videos if (b := content_base(v))]
    lines.append(f"videos with a content folder derivable from the thumbnail: {len(bases)}/{len(videos)}")

    if head and bases:
        lines.append(f"\nHEAD checks, no cookies sent ({min(head, len(bases))} videos):")
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            for n, base in enumerate(bases[:head], 1):
                for name in CANDIDATES:
                    try:
                        r = await client.head(base + name)
                        size = int(r.headers.get("content-length") or 0)
                        where = f" -> {mask(r.headers['location'])}" if "location" in r.headers else ""
                        lines.append(f"  video {n} {name}: {r.status_code} {r.headers.get('content-type', '')} "
                                     f"{size / 2**20:.0f} MB{where}")
                    except httpx.HTTPError as exc:
                        lines.append(f"  video {n} {name}: {type(exc).__name__}")
    return "\n".join(lines)
