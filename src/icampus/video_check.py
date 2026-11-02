"""One-off feasibility check: can lecture videos be fetched without opening their viewer?

Reads the same LearningX `modules?include_detail=true` list the sync reads, and reports the shape of the
video items' `item_content_data` (key names, types, URL patterns with IDs masked), never their values.

With --head N it also asks SKKU's content server (Commons, lcms.skku.edu) about N videos the way other SKKU tools
do: a GET of the content's description (`/CommonsCore2/v2/contents/<id>`), then a HEAD of any download address it
names. All of it goes without cookies, so the content server can't tie it to your account, and a HEAD downloads
nothing. No item page, viewer or player is opened, so no watch progress is reported. It prints status codes,
types, sizes and masked URL patterns only.
"""

import re
from collections import Counter
from typing import Any, Iterator
from urllib.parse import urlsplit

import httpx

from . import collect, parse
from .browser import NotLoggedIn, exclusive, open_session
from .config import Settings
from .probe import shape
from .store import Store

_ID = re.compile(r"(?i)[0-9a-f]{12,}|[0-9a-f-]{32,}|\d{3,}")
_THUMB = re.compile(r"^/CommonsCore2/v2/contents/([^/.]+)\.[A-Za-z0-9]+$")
HOSTED = {"movie", "everlec", "screenlecture"}  # youtube and zoom live elsewhere


def mask(url: str | None) -> str | None:
    """host + path with long IDs replaced, so the pattern shows but no content or user ID does."""
    if not url:
        return None
    p = urlsplit(url)
    return f"{p.scheme}://{p.hostname}{_ID.sub('<id>', p.path)}" + (" (+query)" if p.query else "")


def asset(icd: dict) -> tuple[str, str] | None:
    """(host, asset id) from a thumbnail like https://lcms.skku.edu/CommonsCore2/v2/contents/<id>.jpg."""
    parts = urlsplit(icd.get("thumbnail_url") or "")
    m = _THUMB.match(parts.path)
    if not m or parts.scheme != "https" or not (parts.hostname or "").endswith(".skku.edu"):
        return None
    return parts.hostname, m.group(1)


def urls_in(value: Any) -> Iterator[str]:
    """Every http(s) URL in a JSON value, at any depth."""
    if isinstance(value, dict):
        for v in value.values():
            yield from urls_in(v)
    elif isinstance(value, list):
        for v in value:
            yield from urls_in(v)
    elif isinstance(value, str) and value.startswith(("https://", "http://")):
        yield value


def _named_download(meta: Any) -> str | None:
    result = meta.get("result") if isinstance(meta, dict) else None
    url = result.get("download_url") if isinstance(result, dict) else None
    return url if isinstance(url, str) and url.startswith("https://") else None


async def _head(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.head(url)
    except httpx.HTTPError as exc:
        return type(exc).__name__
    size = int(r.headers.get("content-length") or 0)
    where = f" -> {mask(r.headers['location'])}" if "location" in r.headers else ""
    return f"{r.status_code} {r.headers.get('content-type', '')} {size / 2**20:.0f} MB{where}"


async def check(settings: Settings, course: str | None, head: int, manual_login: bool = False) -> str:
    store = Store(settings.db_path)
    async with exclusive(settings.data_dir, 45) as held:
        if not held:
            return "a sync or download holds the session; try again in a minute"
        async with open_session(settings, store, headless=False if manual_login else None) as s:
            if manual_login and not await s.logged_in():
                from .probe import wait_manual_login
                await wait_manual_login(s)  # you sign in yourself in the window; the session is saved for later
            try:
                await s.ensure_login(allow_credentials=False)
            except NotLoggedIn:
                return ("the saved iCampus login has expired: run this again with --login manual to sign in "
                        "in a browser window, or `uv run icampus sync` if your .env has your login")
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

    assets = [(v, a) for v in videos if (a := asset(v))]
    lines.append(f"videos with a Commons asset id in the thumbnail: {len(assets)}/{len(videos)}")
    lines.append(f"  asset id == content_id: {sum(a[1] == str(v.get('content_id')) for v, a in assets)}, "
                 f"== _id: {sum(a[1] == str(v.get('_id')) for v, a in assets)}")
    seen_types = set()
    for v in videos:
        if "content" in v and v.get("content_type") not in seen_types:
            seen_types.add(v.get("content_type"))
            lines.append(f"`content` shape for a {v.get('content_type')} video: {shape(v['content'])}")
    content_urls = Counter(mask(u) for v in videos for u in urls_in(v.get("content")))
    lines.append(f"URL patterns inside `content`: {dict(content_urls)}")

    picks: list[tuple[dict, tuple[str, str]]] = []
    for kind in ("movie", "everlec", "screenlecture"):  # one of each hosted kind first, then more movies
        picks += [x for x in assets if x[0].get("content_type") == kind][:1]
    picks += [x for x in assets if x[0].get("content_type") in HOSTED and x not in picks]
    if head and picks:
        lines.append(f"\nCommons checks, no cookies sent ({min(head, len(picks))} videos):")
        async with httpx.AsyncClient(timeout=20, follow_redirects=False,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            for n, (v, (host, aid)) in enumerate(picks[:head], 1):
                lines.append(f"  video {n} ({v.get('content_type')}, "
                             f"{(v.get('total_file_size') or 0) / 2**20:.0f} MB listed):")
                meta = None
                try:
                    r = await client.get(f"https://{host}/CommonsCore2/v2/contents/{aid}")
                    lines.append(f"    GET contents/<id>: {r.status_code} {r.headers.get('content-type', '')}")
                    if r.status_code == 200:
                        meta = r.json()
                        lines.append(f"    its shape: {shape(meta)}")
                        lines.append(f"    URL patterns in it: {dict(Counter(mask(u) for u in urls_in(meta)))}")
                except (httpx.HTTPError, ValueError) as exc:
                    lines.append(f"    GET contents/<id>: {type(exc).__name__}")
                if named := _named_download(meta):
                    lines.append(f"    HEAD its download_url ({mask(named)}): {await _head(client, named)}")
                fallback = f"https://{host}/CommonsCore2/v2/services/download/{aid}"
                lines.append(f"    HEAD services/download/<id>: {await _head(client, fallback)}")
    return "\n".join(lines)
