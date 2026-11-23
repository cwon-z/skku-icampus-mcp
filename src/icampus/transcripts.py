"""Stored transcripts: the normalized JSON under var/transcripts, the rows that describe them, full-text search over
their segments, and the text, VTT and SRT renderings.

A lecture can have several transcripts (different providers, or an older video version). One is preferred: the one
you pinned, else the newest one that matches the current video. Transcripts are copyrighted lecture content, like
the videos they come from: they stay under var/ and are only served behind the API's auth.

Search uses an FTS5 table with the trigram tokenizer, which matches substrings and therefore Korean without a word
splitter. Trigrams need three characters, so shorter terms (most two-syllable Korean words) are matched with LIKE
instead. Without FTS5 or the tokenizer (SQLite older than 3.34) every term goes through LIKE.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .cache import Cache, Entry
from .config import KST
from .store import Store
from .stt import Transcript

log = logging.getLogger(__name__)
PARAGRAPH_CHARS = 420  # a text rendering starts a new [mm:ss] paragraph after about this much
PARAGRAPH_GAP_S = 2.5  # or after a pause this long
SAME_MOMENT_S = 12  # search hits of one lecture closer than this are the same moment in different transcripts


def stamp(seconds: float, hours: bool = False) -> str:
    """75 -> '01:15'; with hours (or past the first hour) 3675 -> '1:01:15'."""
    s = int(seconds)
    if hours or s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


def _clock(seconds: float, sep: str) -> str:
    ms = int(round(seconds * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d}{sep}{ms % 1000:03d}"


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Transcripts:
    def __init__(self, settings, store: Store, cache: Cache):
        self.settings = settings
        self.store = store
        self.cache = cache
        self.dir = Path(settings.data_dir) / "transcripts"
        self.fts = self._open_fts()
        cache.on_delete("transcript", self._forget)
        cache.adopter(self.adopt)

    def adopt(self) -> None:
        """Start-up: a transcript whose file is gone loses its rows; one that lost only its index entry gets it
        back."""
        for row in self.rows():
            path = Path(self.settings.data_dir) / row["path"]
            if not path.is_file():
                self._forget(Entry("transcript", str(row["id"]), []))
            elif self.cache.get("transcript", str(row["id"])) is None:
                self.cache.put("transcript", str(row["id"]), [path], course_id=row["course_id"],
                               item_id=row["item_id"], title=row["title"], created_at=row["created_at"],
                               meta={"provider": row["provider_id"], "model": row["model"],
                                     "language": row["language"], "duration_s": row["duration_s"]})

    def _open_fts(self) -> bool:
        """True when full-text search is available. The trigram tokenizer came with SQLite 3.34."""
        try:
            self.store.execute("CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(text, tokenize='trigram')")
            return True
        except sqlite3.OperationalError as exc:
            log.warning("SQLite %s has no FTS5 trigram tokenizer (%s): transcript search falls back to LIKE",
                        sqlite3.sqlite_version, exc)
            return False

    # --- storing -----------------------------------------------------------

    def save(self, lecture: dict, transcript: Transcript, *, video_version: str, reason: str = "",
             job_id: int | None = None, cost_usd: float | None = None) -> int:
        """Writes the JSON, the rows and the search index for one transcript; returns its id."""
        now = datetime.now(KST).isoformat()
        asset_id = lecture["video"]["asset_id"]
        with self.store.transaction() as db:
            tid = db.execute(
                """INSERT INTO transcripts (item_id, asset_id, course_id, title, video_version, provider_id, model,
                       language, duration_s, chars, segment_count, timestamps, reason, job_id, cost_usd, path,
                       created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)""",
                (lecture["item_id"], asset_id, lecture.get("course_id"), lecture.get("title"), video_version,
                 transcript.provider, transcript.model, transcript.language, transcript.duration,
                 sum(len(s.text) for s in transcript.segments), len(transcript.segments), transcript.timestamps,
                 reason, job_id, cost_usd, now)).lastrowid
            path = self.dir / f"t{tid}.json"
            db.execute("UPDATE transcripts SET path=? WHERE id=?", (f"transcripts/{path.name}", tid))
            for idx, s in enumerate(transcript.segments):
                sid = db.execute("INSERT INTO segments (transcript_id, idx, start_s, end_s, text) VALUES (?, ?, ?, ?, ?)",
                                 (tid, idx, s.start, s.end, s.text)).lastrowid
                if self.fts:
                    db.execute("INSERT INTO segments_fts (rowid, text) VALUES (?, ?)", (sid, s.text))
        body = {"version": 1, "id": tid, "item_id": lecture["item_id"], "asset_id": asset_id,
                "course_id": lecture.get("course_id"), "title": lecture.get("title"), "video_version": video_version,
                "provider": transcript.provider, "model": transcript.model, "language": transcript.language,
                "duration": transcript.duration, "timestamps": transcript.timestamps, "reason": reason,
                "created_at": now,
                "segments": [[s.start, s.end, s.text] for s in transcript.segments],
                "words": [[w.start, w.end, w.text, w.confidence] for w in transcript.words]
                if transcript.words else None}
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(body, f, ensure_ascii=False)
        os.replace(tmp, path)
        self.cache.put("transcript", str(tid), [path], course_id=lecture.get("course_id"), item_id=lecture["item_id"],
                       title=lecture.get("title"), created_at=now,
                       meta={"provider": transcript.provider, "model": transcript.model,
                             "language": transcript.language, "duration_s": transcript.duration})
        return tid

    def _forget(self, entry: Entry) -> None:
        """The cache is deleting a transcript's file: its rows and search index go with it."""
        try:
            tid = int(entry.key)
        except ValueError:
            return
        with self.store.transaction() as db:
            if self.fts:
                db.execute("DELETE FROM segments_fts WHERE rowid IN (SELECT id FROM segments WHERE transcript_id=?)",
                           (tid,))
            db.execute("DELETE FROM segments WHERE transcript_id=?", (tid,))
            db.execute("DELETE FROM transcripts WHERE id=?", (tid,))

    def delete(self, transcript_id: int) -> bool:
        return self.cache.delete("transcript", str(transcript_id))

    # --- finding -----------------------------------------------------------

    def rows(self, item_id: str | None = None) -> list[dict]:
        """Transcript rows, newest first."""
        sql, args = "SELECT * FROM transcripts", ()
        if item_id is not None:
            sql, args = sql + " WHERE item_id=?", (item_id,)
        return [dict(r) for r in self.store.query(sql + " ORDER BY created_at DESC, id DESC", args)]

    def get(self, transcript_id: int) -> dict | None:
        row = self.store.one("SELECT * FROM transcripts WHERE id=?", (transcript_id,))
        return dict(row) if row else None

    @staticmethod
    def _best(rows: list[dict]) -> dict | None:
        """Pinned first, then one that matches the current video, then the newest."""
        return min(rows, key=lambda r: (not r["preferred"], r["status"] != "ready", -r["id"])) if rows else None

    def preferred(self, item_id: str, model: str | None = None) -> dict | None:
        """The transcript to read for a lecture; with `model`, the best one that provider made."""
        return self._best([r for r in self.rows(item_id) if not model or r["provider_id"] == model])

    def preferred_by_item(self) -> dict[str, dict]:
        grouped: dict[str, list[dict]] = {}
        for row in self.rows():
            grouped.setdefault(row["item_id"], []).append(row)
        return {item: self._best(rows) for item, rows in grouped.items()}

    def for_version(self, asset_id: str, video_version: str, model: str | None = None) -> dict | None:
        """A transcript of exactly this video version (from this provider, when one is named)."""
        return self._best([dict(r) for r in self.store.query(
            "SELECT * FROM transcripts WHERE asset_id=? AND video_version=?", (asset_id, video_version))
            if not model or r["provider_id"] == model])

    def prefer(self, transcript_id: int) -> bool:
        """Pins this transcript as the one to read for its lecture."""
        row = self.get(transcript_id)
        if row is None:
            return False
        with self.store.transaction() as db:
            db.execute("UPDATE transcripts SET preferred=0 WHERE item_id=?", (row["item_id"],))
            db.execute("UPDATE transcripts SET preferred=1 WHERE id=?", (transcript_id,))
        return True

    def mark_outdated(self, lectures: list[dict]) -> list[dict]:
        """After a sync: transcripts whose video now has another version become outdated (and ones that match
        again become ready). Returns the rows that just turned outdated."""
        current = {lec["video"]["asset_id"]: lec["video"].get("updated_at") or "" for lec in lectures
                   if lec.get("video")}
        turned = []
        for row in self.rows():
            if row["asset_id"] not in current:
                continue  # the lecture is gone from the sync: leave its transcript as it is
            status = "ready" if row["video_version"] == current[row["asset_id"]] else "outdated"
            if status != row["status"]:
                self.store.execute("UPDATE transcripts SET status=? WHERE id=?", (status, row["id"]))
                if status == "outdated":
                    turned.append({**row, "status": status})
        return turned

    # --- reading -----------------------------------------------------------

    def load(self, row: dict) -> dict:
        """The stored JSON (segments as [start, end, text], words as [start, end, text, confidence])."""
        self.cache.touch("transcript", str(row["id"]))
        return json.loads((Path(self.settings.data_dir) / row["path"]).read_text())

    def segments(self, row: dict, start_s: float | None = None, end_s: float | None = None) -> list[tuple]:
        """(start, end, text) of the segments that overlap start_s..end_s."""
        sql, args = "SELECT start_s, end_s, text FROM segments WHERE transcript_id=?", [row["id"]]
        if start_s is not None:
            sql, args = sql + " AND end_s>=?", [*args, start_s]
        if end_s is not None:
            sql, args = sql + " AND start_s<=?", [*args, end_s]
        return [(r["start_s"], r["end_s"], r["text"]) for r in self.store.query(sql + " ORDER BY idx", args)]

    def by_slide(self, row: dict | None, slides: list[dict]) -> list[str]:
        """What was said while each slide was on screen: every segment goes to the slide showing when it began
        (anything before the first slide's start goes to the first)."""
        said: list[list[str]] = [[] for _ in slides]
        current = 0
        for start, _, text in self.segments(row) if row and slides else []:
            while current + 1 < len(slides) and start >= slides[current + 1]["start"]:
                current += 1
            said[current].append(text)
        return [" ".join(parts) for parts in said]

    def text(self, row: dict, start_s: float | None = None, end_s: float | None = None) -> str:
        """Plain text in paragraphs, each starting with a [mm:ss] marker ([h:mm:ss] for lectures over an hour)."""
        hours = (row.get("duration_s") or 0) >= 3600
        lines: list[str] = []
        current: list[str] = []
        last_end = 0.0
        for start, end, text in self.segments(row, start_s, end_s):
            size = sum(len(t) + 1 for t in current)
            if current and (size + len(text) > PARAGRAPH_CHARS or start - last_end > PARAGRAPH_GAP_S):
                lines.append(" ".join(current))
                current = []
            if not current:
                current.append(f"[{stamp(start, hours)}]")
            current.append(text)
            last_end = end
        if current:
            lines.append(" ".join(current))
        return "\n".join(lines)

    def page(self, row: dict, *, offset: int = 0, max_chars: int = 15000, start_s: float | None = None,
             end_s: float | None = None) -> dict:
        """One page of the text rendering, ending on a paragraph boundary: {text, total_chars, offset, next_offset}."""
        full = self.text(row, start_s, end_s)
        end = min(len(full), offset + max_chars)
        if end < len(full):
            cut = full.rfind("\n", offset, end)
            end = cut + 1 if cut > offset else end
        return {"total_chars": len(full), "offset": offset, "next_offset": end if end < len(full) else None,
                "text": full[offset:end].rstrip("\n")}

    def vtt(self, row: dict, start_s: float | None = None, end_s: float | None = None) -> str:
        cues = [f"{_clock(a, '.')} --> {_clock(max(b, a + 0.01), '.')}\n{text}" for a, b, text
                in self.segments(row, start_s, end_s)]
        return "WEBVTT\n\n" + "\n\n".join(cues) + "\n"

    def srt(self, row: dict, start_s: float | None = None, end_s: float | None = None) -> str:
        cues = [f"{n}\n{_clock(a, ',')} --> {_clock(max(b, a + 0.01), ',')}\n{text}" for n, (a, b, text)
                in enumerate(self.segments(row, start_s, end_s), 1)]
        return "\n\n".join(cues) + "\n"

    # --- search ------------------------------------------------------------

    def search(self, query: str, *, item_ids: set[str] | None = None, limit: int = 10) -> list[dict]:
        """Segments that contain every term of the query (case-insensitive substrings), best first. Each hit:
        transcript and segment details, a snippet with the neighbouring segments, and `also_in` when another
        transcript of the same lecture says it at the same moment."""
        terms = [t for t in query.split() if t]
        if not terms:
            return []
        long_terms = [t for t in terms if len(t) >= 3] if self.fts else []
        like_terms = [t for t in terms if t not in long_terms]
        sql = ("SELECT s.id AS segment_id, s.transcript_id, s.idx, s.start_s, s.end_s, s.text, t.item_id, "
               "t.provider_id, t.model, t.status, t.preferred, t.title, t.course_id FROM segments s "
               "JOIN transcripts t ON t.id = s.transcript_id")
        where, args = [], []
        if long_terms:
            sql += " JOIN segments_fts f ON f.rowid = s.id"
            where.append("segments_fts MATCH ?")
            args.append(" AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms))
        for term in like_terms:
            where.append("s.text LIKE ? ESCAPE '\\'")
            args.append(_like(term))
        if item_ids is not None:  # in the query, not after it: the row limit below must count matching rows only
            where.append("t.item_id IN (SELECT value FROM json_each(?))")
            args.append(json.dumps(sorted(item_ids)))
        order = "f.rank, " if long_terms else ""
        rows = self.store.query(f"{sql} WHERE {' AND '.join(where)} ORDER BY {order}t.created_at DESC, s.idx "
                                f"LIMIT ?", [*args, max(200, limit * 20)])
        best = {item: row["id"] for item, row in self.preferred_by_item().items()}
        hits: list[dict] = []
        for row in rows:
            same = next((h for h in hits if h["item_id"] == row["item_id"]
                         and abs(h["start_s"] - row["start_s"]) < SAME_MOMENT_S), None)
            if same is None:
                hits.append({**dict(row), "also_in": []})
            elif row["transcript_id"] == same["transcript_id"]:
                continue  # the neighbouring segment of the same transcript: one hit for the moment is enough
            elif best.get(row["item_id"]) == row["transcript_id"]:  # the preferred transcript speaks for the moment
                also = sorted({*same["also_in"], same["provider_id"]} - {row["provider_id"]})
                hits[hits.index(same)] = {**dict(row), "also_in": also}
            elif row["provider_id"] != same["provider_id"] and row["provider_id"] not in same["also_in"]:
                same["also_in"].append(row["provider_id"])
        out = []
        for hit in hits[:limit]:
            around = self.store.query("SELECT idx, text FROM segments WHERE transcript_id=? AND idx BETWEEN ? AND ? "
                                      "ORDER BY idx", (hit["transcript_id"], hit["idx"] - 1, hit["idx"] + 1))
            snippet = " ".join(("**" + r["text"] + "**") if r["idx"] == hit["idx"] and len(around) > 1 else r["text"]
                               for r in around)
            out.append({"item_id": hit["item_id"], "title": hit["title"], "course_id": hit["course_id"],
                        "start_s": round(hit["start_s"], 1), "at": stamp(hit["start_s"]), "snippet": snippet[:600],
                        "model": hit["provider_id"], "outdated": hit["status"] == "outdated" or None,
                        "also_in": hit["also_in"] or None})
        return out

    # --- for lecture rows and stats ----------------------------------------

    def summary(self, row: dict) -> dict[str, Any]:
        """A transcript row for an API answer."""
        return {"id": row["id"], "model": row["provider_id"], "provider_model": row["model"],
                "language": row["language"], "duration_s": row["duration_s"], "chars": row["chars"],
                "segments": row["segment_count"], "timestamps": row["timestamps"], "status": row["status"],
                "preferred": bool(row["preferred"]), "created_at": row["created_at"], "chosen": row["reason"],
                "cost_usd": row["cost_usd"]}
