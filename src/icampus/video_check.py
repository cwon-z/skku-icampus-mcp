"""One-off feasibility check: can lecture videos be fetched without opening their viewer?

Reads the same LearningX `modules?include_detail=true` list the sync reads, and reports the shape of the
video items' `item_content_data` (key names, types, URL patterns with IDs masked), never their values.

With --head N it also asks SKKU's content server (Commons, lcms.skku.edu) about N videos the way other SKKU tools
do: a GET of the content's description (`/CommonsCore2/v2/contents/<id>`), then a GET of only the first byte of
each download address it names (HEAD answers 500 there). All of it goes without cookies, so the content server
can't tie it to your account. No item page, viewer or player is opened, so no watch progress is reported.
It prints status codes, types, sizes and masked URL patterns only. --captions reads every hosted video's description
for its caption and script flags. videos.py relies on what this found; rerun it if exports start failing.
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
_LONG_ID = re.compile(r"(?i)[0-9a-f-]{12,}|\d{6,}")  # in error text: keeps status codes, hides IDs
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


def _named(meta: Any, field: str) -> str | None:
    result = meta.get("result") if isinstance(meta, dict) else None
    url = result.get(field) if isinstance(result, dict) else None
    return url if isinstance(url, str) and url.startswith("https://") else None


def error_hint(body: bytes) -> str:
    """A short, masked line from an error page: its <title>, else its first text."""
    text = body.decode("utf-8", "replace")
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    words = " ".join((m.group(1) if m and m.group(1).strip() else re.sub(r"(?s)<[^>]+>", " ", text)).split())
    return _LONG_ID.sub("<id>", parse.scrub(re.sub(r"https?://\S+", lambda u: mask(u.group()) or "", words)))[:160]


async def _first_byte(client: httpx.AsyncClient, url: str, headers: dict | None = None) -> str:
    """A GET for byte 0 only (HEAD answers 500 here), following redirects. Only an error page's first 4 KB
    are read, for its message; a file's body is never read."""
    try:
        async with client.stream("GET", url, headers={"Range": "bytes=0-0", **(headers or {})},
                                 follow_redirects=True) as r:
            total = (r.headers.get("content-range") or "").rpartition("/")[2] or r.headers.get("content-length") or "0"
            hops = f" via {len(r.history)} redirect(s) to {mask(str(r.url))}" if r.history else ""
            size = f"{int(total) / 2**20:.0f} MB" if total.isdigit() else total
            hint = ""
            if r.status_code >= 400:
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) >= 4096:
                        break
                hint = f' "{error_hint(body[:4096])}"'
            return f"{r.status_code} {r.headers.get('content-type', '')} {size}{hops}{hint}"
    except httpx.HTTPError as exc:
        return type(exc).__name__


async def _describe(client: httpx.AsyncClient, host: str, aid: str) -> tuple[str, Any]:
    """(status line, the description) of a Commons asset: a plain GET, no cookies. Nothing is downloaded."""
    try:
        r = await client.get(f"https://{host}/CommonsCore2/v2/contents/{aid}")
        return f"{r.status_code} {r.headers.get('content-type', '')}", r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        return type(exc).__name__, None


def _result(meta: Any) -> dict:
    return meta.get("result") or {} if isinstance(meta, dict) else {}


async def _captions(assets: list[tuple[dict, tuple[str, str]]], lines: list[str]) -> None:
    """What every hosted video's Commons description says about captions and scripts (values are flags only)."""
    hosted = [(v, a) for v, a in assets if v.get("content_type") in HOSTED]
    flags: Counter = Counter()
    tracks: Counter = Counter()
    track_urls: Counter = Counter()
    shapes: dict[str, Any] = {}
    failed = 0
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for v, (host, aid) in hosted:
            status, meta = await _describe(client, host, aid)
            res = _result(meta)
            if not res:
                failed += 1
                continue
            flags[(res.get("exists_caption"), res.get("exists_script"))] += 1
            track_list = res.get("caption_track_list")
            tracks[type(track_list).__name__] += 1
            if track_list:
                shapes.setdefault(v.get("content_type"), shape(track_list))
                track_urls.update(mask(u) for u in urls_in(track_list))
    lines.append(f"\nCaptions, from {len(hosted)} Commons descriptions (no cookies; {failed} failed):")
    lines.append(f"  (exists_caption, exists_script): {dict(flags)}")
    lines.append(f"  caption_track_list types: {dict(tracks)}")
    for kind, sh in shapes.items():
        lines.append(f"  caption_track_list shape ({kind}): {sh}")
    lines.append(f"  URL patterns in caption tracks: {dict(track_urls)}")


async def check(settings: Settings, course: str | None, head: int, manual_login: bool = False,
                captions: bool = False) -> str:
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
                status, meta = await _describe(client, host, aid)
                res = _result(meta)
                lines.append(f"    GET contents/<id>: {status}")
                # enumerations and counts only (the description also names the uploader: never printed)
                lines.append("    " + ", ".join(f"{k}={res.get(k)!r}" for k in (
                    "type", "subtype", "extension", "convert_state", "status", "privacy",
                    "is_original_file_deleted", "read_cnt")))
                lines.append(f"    sizes: original {(res.get('original_file_length') or 0) / 2**20:.0f} MB, "
                             f"total {(res.get('total_file_size') or 0) / 2**20:.0f} MB, "
                             f"mobile {(res.get('mobile_file_size') or 0) / 2**20:.0f} MB")
                if url := _named(meta, "download_url"):  # the export needs it to answer with a Referer
                    lines.append(f"    first byte of download_url ({mask(url)}): {await _first_byte(client, url)}")
                    lines.append(f"      with Referer https://canvas.skku.edu/: "
                                 f"{await _first_byte(client, url, {'Referer': 'https://canvas.skku.edu/'})}")
                if meta is not None:
                    _, after = await _describe(client, host, aid)
                    lines.append(f"    read_cnt before/after: {res.get('read_cnt')!r} -> "
                                 f"{_result(after).get('read_cnt')!r}")
    if captions:
        await _captions(assets, lines)
    return "\n".join(lines)


# --- lecture materials (slide PDFs): are they on Commons too? ------------------------------------

MATERIAL_FIELDS = ("type", "subtype", "extension", "convert_state", "status", "privacy", "is_original_file_deleted",
                   "read_cnt")


def material_report(items: list[dict]) -> tuple[list[str], list[tuple[dict, tuple[str, str]]]]:
    """(report lines, the items that name a Commons asset) for lecture material items' item_content_data.
    Shapes, counts and masked URL patterns only: no titles, no ids, no values."""
    lines = [f"material items: {len(items)}", f"types: {dict(Counter(v.get('content_type') for v in items))}"]
    keys = Counter(k for v in items for k in v)
    lines.append("item_content_data keys (how many materials have each): "
                 + ", ".join(f"{k}={n}" for k, n in sorted(keys.items())))
    for field in ("thumbnail_url", "view_url"):
        lines.append(f"{field} patterns: {dict(Counter(mask(v.get(field)) for v in items))}")
    assets = [(v, a) for v in items if (a := asset(v))]
    lines.append(f"materials with a Commons asset id in the thumbnail: {len(assets)}/{len(items)}")
    seen = set()
    for v in items:
        if "content" in v and v.get("content_type") not in seen:
            seen.add(v.get("content_type"))
            lines.append(f"`content` shape for a {v.get('content_type')} item: {shape(v['content'])}")
    lines.append(f"URL patterns inside `content`: {dict(Counter(mask(u) for v in items for u in urls_in(v.get('content'))))}")
    return lines, assets


async def describe_materials(client: httpx.AsyncClient, picks: list[tuple[dict, tuple[str, str]]]) -> list[str]:
    """What Commons says about these material assets: the description only. No download address is requested,
    because fetching a material may count as opening it; the addresses the description names are shown masked."""
    lines = [f"\nCommons descriptions, no cookies sent, nothing downloaded ({len(picks)} materials):"]
    for n, (v, (host, aid)) in enumerate(picks, 1):
        status, meta = await _describe(client, host, aid)
        res = _result(meta)
        lines.append(f"  material {n} ({v.get('content_type')}): GET contents/<id>: {status}")
        if not res:
            continue
        lines.append("    " + ", ".join(f"{k}={res.get(k)!r}" for k in MATERIAL_FIELDS))
        lines.append(f"    sizes: original {(res.get('original_file_length') or 0) / 2**20:.1f} MB, "
                     f"total {(res.get('total_file_size') or 0) / 2**20:.1f} MB")
        named = sorted(k for k, value in res.items() if isinstance(value, str) and value.startswith("https://"))
        lines.append(f"    addresses it names (not requested): "
                     f"{ {k: mask(res[k]) for k in named} or 'none'}")
        _, after = await _describe(client, host, aid)
        lines.append(f"    read_cnt before/after a second description: {res.get('read_cnt')!r} -> "
                     f"{_result(after).get('read_cnt')!r}")
    return lines


async def check_materials(settings: Settings, course: str | None, head: int, manual_login: bool = False) -> str:
    """Reads the same LearningX modules list the sync reads and reports the shape of the lecture material items.
    With --head N it also asks Commons to describe N of them (no cookies). It never opens an item and never
    requests a file: whether materials may be fetched at all is a decision for after this report."""
    store = Store(settings.db_path)
    async with exclusive(settings.data_dir, 45) as held:
        if not held:
            return "a sync or download holds the session; try again in a minute"
        async with open_session(settings, store, headless=False if manual_login else None) as s:
            if manual_login and not await s.logged_in():
                from .probe import wait_manual_login
                await wait_manual_login(s)
            try:
                await s.ensure_login(allow_credentials=False)
            except NotLoggedIn:
                return ("the saved iCampus login has expired: run this again with --login manual to sign in "
                        "in a browser window, or `uv run icampus sync` if your .env has your login")
            _, _, raw = await collect.courses(s, settings.term)
            ids = [str(c["id"]) for c in raw if not course or str(c["id"]) == course]
            items = []
            for cid in ids:
                for module in await s.learningx(f"/courses/{cid}/modules", {"include_detail": "true"}):
                    for it in module.get("module_items") or []:
                        icd = (it.get("content_data") or {}).get("item_content_data") or {}
                        if parse.kind_of(icd.get("content_type"), it.get("content_type")) == "material":
                            items.append(icd)
    lines, assets = material_report(items)
    lines.insert(0, f"courses checked: {len(ids)}")
    if head and assets:
        picks: list[tuple[dict, tuple[str, str]]] = []
        for kind in sorted({v.get("content_type") for v, _ in assets}, key=str):  # one of each type first
            picks += [x for x in assets if x[0].get("content_type") == kind][:1]
        picks += [x for x in assets if x not in picks]
        async with httpx.AsyncClient(timeout=20, follow_redirects=False,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            lines += await describe_materials(client, picks[:head])
    elif head:
        lines.append("\nno material names a Commons asset, so there is nothing to describe")
    lines.append("\nNo item was opened and no file was requested.")
    return "\n".join(lines)
