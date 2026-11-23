"""Shared fakes for the transcription tests. Everything here is made up: no real course, lecture or provider data,
and nothing that leaves the machine."""

import re
from datetime import datetime
from pathlib import Path

import httpx

from icampus import audio
from icampus.config import KST, Settings
from icampus.store import Record, Store

TOKEN, ADMIN = "t" * 20, "a" * 20
MP4 = b"\0\0\0\x18ftypmp42" + b"x" * 4000
SECRET = "sk-test-SECRETKEY-do-not-leak-1234"
OTHER_SECRET = "gsk-test-OTHERKEY-do-not-leak-5678"
COURSES = [{"id": "1001", "name": "Data Structures", "code": "CSE1001"},
           {"id": "1002", "name": "선형대수", "code": "MTH2002"}]
EL = {"id": "el", "type": "elevenlabs", "name": "Hosted", "description": "hosted, word timestamps",
      "api_key_env": "TEST_EL_KEY", "cost_per_hour": 0.2, "priority": 10}
WHISPER = {"id": "whisper", "type": "openai_compatible", "name": "Homelab", "model": "large-v3",
           "base_url": "http://gpu.tailnet.example:8000/v1", "priority": 20,
           "capabilities": {"max_upload_mb": None}}


def lecture(item: str, *, course: str = "1001", minutes: float = 10, asset: str | None = None,
            updated: str = "20260301120000", week: int = 1, hosted: bool = True, title: str | None = None) -> dict:
    row = {"key": f"canvas.skku.edu:{course}:modules/items:{item}", "course_id": course, "item_id": item,
           "title": title or f"Lecture {item}", "kind": "video", "week": f"{week}주차", "week_no": week,
           "duration_min": minutes}
    if hosted:
        row["video"] = {"asset_id": asset or f"asset{item}", "host": "lcms.skku.edu", "listed_size": len(MP4),
                        "updated_at": updated}
    return row


def seed(store: Store, lectures: list[dict], courses: list[dict] = COURSES) -> None:
    now = datetime.now(KST)
    store.replace_scope("courses", "term:x", [Record(c["id"], c["id"], None, c) for c in courses], now)
    for cid in {lec["course_id"] for lec in lectures}:
        store.replace_scope("lectures", f"course:{cid}",
                            [Record(lec["key"], cid, None, lec) for lec in lectures if lec["course_id"] == cid], now)


class Commons:
    """lcms.skku.edu as videos.py meets it. `fail` holds statuses to answer the next downloads with."""

    def __init__(self, body: bytes = MP4):
        self.body, self.downloads, self.fail = body, [], []
        self.active = self.peak = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert "cookie" not in request.headers and "authorization" not in request.headers
        path = request.url.path
        if path.startswith("/CommonsCore2/v2/contents/"):
            return httpx.Response(200, json={"result": {"original_file_length": len(self.body), "extension": "mp4"}})
        if self.fail:
            return httpx.Response(self.fail.pop(0), text="busy")
        self.downloads.append(path.rsplit("/", 1)[1])
        return httpx.Response(200, content=self.body)


def fields(request: httpx.Request) -> dict[str, list[str]]:
    """The text fields of a multipart body (the file part is left out)."""
    out: dict[str, list[str]] = {}
    for name, value in re.findall(rb'name="([^"]+)"\r\n\r\n(.*?)\r\n--', request.content, re.S):
        out.setdefault(name.decode(), []).append(value.decode())
    return out


class Stt:
    """One fake for both provider styles: ElevenLabs on api.elevenlabs.io, OpenAI-compatible anywhere else.
    Each call answers with the words of `say[host]` one second apart, starting at 1.0 within the piece sent.
    `fail[host]` holds statuses to answer the next calls with; `broken` hosts always answer 401."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, list[int]] = {}
        self.broken: set[str] = set()
        self.say = {"api.elevenlabs.io": "행렬 분해는 eigenvalue 문제입니다. Next topic.",
                    "gpu.tailnet.example": "행렬 분해는 eigen value 문제입니다. Next topic."}
        self.shape = "verbose"  # what OpenAI-compatible hosts answer: verbose, segments or text

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append((host, fields(request)))
        key = request.headers.get("xi-api-key") or request.headers.get("authorization", "")
        if host in self.broken:
            return httpx.Response(401, json={"detail": {"status": "invalid_api_key",
                                                        "message": f"Invalid API key: {key}"}})
        if self.fail.get(host):
            status = self.fail[host].pop(0)
            return httpx.Response(status, json={"error": {"message": f"try later (key {key})"}})
        words = self.say.get(host, "hello world.").split()
        if host == "api.elevenlabs.io":
            tokens = []
            for n, word in enumerate(words):
                tokens.append({"text": word, "type": "word", "start": 1.0 + n, "end": 1.6 + n, "logprob": -0.05})
                tokens.append({"text": " ", "type": "spacing", "start": 1.6 + n, "end": 2.0 + n, "logprob": 0})
            return httpx.Response(200, json={"language_code": "kor", "language_probability": 0.98,
                                             "text": " ".join(words), "words": tokens})
        text = " ".join(words)
        if self.shape == "text":
            return httpx.Response(200, json={"text": text})
        body = {"task": "transcribe", "language": "korean", "duration": 2.0 + len(words), "text": text,
                "segments": [{"id": 0, "start": 1.0, "end": 1.0 + len(words), "text": " " + text}]}
        if self.shape == "verbose":
            body["words"] = [{"word": w, "start": 1.0 + n, "end": 1.6 + n} for n, w in enumerate(words)]
        return httpx.Response(200, json=body)

    def hosts(self) -> list[str]:
        return [host for host, _ in self.calls]


def fake_audio(monkeypatch, tmp_path: Path, *, quiet_every: float = 60.0) -> dict:
    """Stands in for ffmpeg: "extracting" writes a file whose size follows the bitrate, and remembers how long the
    lecture was said to be. Returns the record of what was asked."""
    seen: dict = {"extract": [], "cut": [], "silences": 0, "durations": {}}

    async def extract(src, dst, *, fmt="opus", kbps=24, threads=1, duration=None, on_progress=None):
        seconds = float(duration or 600)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"a" * max(1, int(seconds * kbps * 125 / 1000)))  # a thousandth of the real size
        seen["extract"].append((Path(src).name, dst.name, fmt, kbps))
        seen["durations"][dst.name] = seconds
        if on_progress:
            on_progress(1.0)
        return seconds

    async def silences(path, *, threads=1):
        seen["silences"] += 1
        total = seen["durations"].get(Path(path).name, 600)
        return [(t - 0.5, t + 0.5) for t in [quiet_every * n for n in range(1, int(total // quiet_every) + 1)]]

    async def cut(src, dst, start, end):
        dst.write_bytes(b"c" * 10)
        seen["cut"].append((round(start, 1), round(end, 1)))

    monkeypatch.setattr(audio, "available", lambda: True)
    monkeypatch.setattr(audio, "extract", extract)
    monkeypatch.setattr(audio, "silences", silences)
    monkeypatch.setattr(audio, "cut", cut)
    return seen


def make_lecture_mp4(path: Path) -> Path:
    """A 60-second 'lecture' made with ffmpeg: a tone with silence at 9-11 s, 29-31 s and 48-50 s, and a tiny
    video track. Callers skip when ffmpeg is missing."""
    import shutil
    import subprocess
    tone = "sin(2*PI*440*t)*(lt(t,9)+between(t,11,29)+between(t,31,48)+gte(t,50))"
    subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"aevalsrc='{tone}':s=44100:d=60", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=5:d=60",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(api_tokens=f"mcp:{TOKEN},admin:{ADMIN}", data_dir=tmp_path, _env_file=None,
                    stt_seed='[{"id": "el", "type": "elevenlabs", "api_key_env": "TEST_EL_KEY", '
                             '"cost_per_hour": 0.2, "priority": 10, "description": "hosted, word timestamps"}]',
                    **overrides)
