"""The attachment cache and the reader process: one download per file version even with concurrent reads,
and a reader that hangs, crashes or balloons can't take the service with it."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager

from icampus import files
from icampus.config import Settings
from icampus.store import Store

SYNCED = "2026-09-20T10:00:00+09:00"


class Fake:
    """The browser session a download opens, answering for one made-up file."""

    def __init__(self, updated_at: str = SYNCED):
        self.updated_at, self.downloads = updated_at, 0

    async def ensure_login(self, *, allow_credentials):
        return "saved"

    async def canvas(self, path, params=None):
        info = {"id": 7, "display_name": "notes.txt", "content-type": "text/plain", "size": 5,
                "updated_at": self.updated_at, "url": "https://canvas.skku.edu/files/7/download?verifier=x"}
        return [{"id": 1, "attachments": [info]}] if path == "/api/v1/announcements" else info

    async def download(self, url, max_bytes):
        self.downloads += 1
        await asyncio.sleep(0.2)
        return b"hello"


def setup(tmp_path, monkeypatch, fake: Fake) -> tuple[Settings, Store]:
    @asynccontextmanager
    async def fake_open(settings, store, **kw):
        yield fake
    monkeypatch.setattr(files, "open_session", fake_open)
    return Settings(data_dir=tmp_path, _env_file=None), Store(tmp_path / "t.db")


async def test_concurrent_reads_download_once(tmp_path, monkeypatch):
    fake = Fake()
    settings, store = setup(tmp_path, monkeypatch, fake)
    ref = {"id": "7", "via": "linked", "course_id": "1001", "parent": {"type": "assignment"}}
    first, second = await asyncio.gather(files.get(settings, store, ref), files.get(settings, store, ref))
    assert fake.downloads == 1 and first.text == second.text == "hello"
    assert not list((tmp_path / "files").glob("*.tmp"))


async def test_a_file_replaced_after_the_sync_is_downloaded_once(tmp_path, monkeypatch):
    fake = Fake(updated_at="2026-09-28T09:00:00+09:00")  # newer than the version the sync saw
    settings, store = setup(tmp_path, monkeypatch, fake)
    ref = {"id": "7", "via": "attached", "course_id": "1001", "updated_at": SYNCED,
           "parent": {"type": "announcement", "posted_at": SYNCED}}
    for _ in range(3):
        assert (await files.get(settings, store, ref)).meta["updated_at"] == "2026-09-28T09:00:00+09:00"
    assert fake.downloads == 1
    ref["updated_at"] = "2026-09-28T09:00:00+09:00"  # the next sync sees the new version
    await files.get(settings, store, ref)
    assert fake.downloads == 2


async def test_reader_time_limit_and_crash(tmp_path, monkeypatch):
    path = tmp_path / "x.txt"
    path.write_text("hi")
    assert await files._extract(path, "x.txt", None) == ("hi", "text")
    monkeypatch.setattr(files, "EXTRACT_TIMEOUT_S", 0.5)
    monkeypatch.setattr(files, "READER", [sys.executable, "-c", "import time; time.sleep(30)"])
    assert await files._extract(path, "x.txt", None) == ("", "unreadable: gave up after 0.5s")
    monkeypatch.setattr(files, "READER", [sys.executable, "-c", "print('not json')"])
    assert (await files._extract(path, "x.txt", None))[1].startswith("unreadable: the reader stopped")


def test_reader_caps_its_memory_first(tmp_path, monkeypatch, capsys):
    import resource

    import icampus.extract as extract
    calls = []
    monkeypatch.setattr(resource, "setrlimit", lambda what, limits: calls.append((what, limits)))
    path = tmp_path / "a.txt"
    path.write_text("hi")
    monkeypatch.setattr(sys, "argv", ["extract", str(path), "a.txt", ""])
    extract.main()
    assert calls == [(resource.RLIMIT_AS, (extract.MEMORY_LIMIT, extract.MEMORY_LIMIT))]
    assert json.loads(capsys.readouterr().out) == {"text": "hi", "how": "text"}
