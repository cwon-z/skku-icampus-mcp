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


def test_reader_skips_the_memory_cap_only_on_macos(tmp_path, monkeypatch, capsys):
    import resource

    import pytest

    import icampus.extract as extract

    def refuse(what, limits):
        raise ValueError("current limit exceeds maximum limit")  # what macOS says
    monkeypatch.setattr(resource, "setrlimit", refuse)
    path = tmp_path / "a.txt"
    path.write_text("hi")
    monkeypatch.setattr(sys, "argv", ["extract", str(path), "a.txt", ""])
    monkeypatch.setattr(sys, "platform", "darwin")
    extract.main()
    assert json.loads(capsys.readouterr().out) == {"text": "hi", "how": "text"}
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ValueError):
        extract.main()


async def test_reuse_window_size_limit_and_cap_are_settings(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    import pytest

    from icampus.cache import Cache
    from icampus.config import KST
    fake = Fake(updated_at=None)  # iCampus gives no version: the reuse window decides
    settings, store = setup(tmp_path, monkeypatch, fake)
    settings.attachment_reuse_hours = 2
    cache = Cache(settings, store)
    ref = {"id": "7", "via": "linked", "course_id": "1001", "parent": {"type": "assignment", "id": "9", "title": "HW"}}
    await files.get(settings, store, ref, cache)
    entry = cache.get("attachment", "7")
    assert entry.course_id == "1001" and entry.title == "notes.txt" and entry.meta["parent"]["title"] == "HW"
    assert len(entry.files) == 3 and entry.size > 5  # the file, its text and its record
    await files.get(settings, store, ref, cache)
    assert fake.downloads == 1

    sidecar = tmp_path / "files" / "7.json"
    meta = json.loads(sidecar.read_text())
    meta["fetched_at"] = (datetime.now(KST) - timedelta(hours=3)).isoformat()
    sidecar.write_text(json.dumps(meta))
    assert files.peek(settings, "7").text == "hello"  # still on disk for the cache page, without asking iCampus
    await files.get(settings, store, ref, cache)
    assert fake.downloads == 2  # past the 2-hour window: fetched again

    settings.attachment_max_mb = 1

    async def huge(path, params=None):
        return {"id": 7, "display_name": "big.bin", "size": 3 * 2**20, "url": "https://canvas.skku.edu/files/7/download"}
    monkeypatch.setattr(fake, "canvas", huge)
    sidecar.unlink()
    with pytest.raises(files.Unavailable, match="only files up to 1 MB"):
        await files.get(settings, store, ref, cache)


async def test_attachments_past_their_cap_go_least_recently_used_first(tmp_path, monkeypatch):
    from icampus.cache import Cache

    class Sized(Fake):
        async def canvas(self, path, params=None):
            fid = path.rsplit("/", 1)[1]
            return {"id": int(fid), "display_name": f"{fid}.txt", "content-type": "text/plain", "size": 4000,
                    "updated_at": SYNCED, "url": f"https://canvas.skku.edu/files/{fid}/download?verifier=x"}

        async def download(self, url, max_bytes):
            self.downloads += 1
            return b"k" * 4000

    fake = Sized()
    settings, store = setup(tmp_path, monkeypatch, fake)
    settings.attachment_cache_gb = 20_000 / 2**30  # room for two files with their text and record, not three
    cache = Cache(settings, store)

    def ref(fid):
        return {"id": fid, "via": "linked", "course_id": "1001", "updated_at": SYNCED, "parent": {"type": "assignment"}}
    for fid in ("1", "2"):
        await files.get(settings, store, ref(fid), cache)
    await files.get(settings, store, ref("1"), cache)  # read again: now the most recent
    await files.get(settings, store, ref("3"), cache)
    assert {e.key for e in cache.entries("attachment")} == {"1", "3"}
    assert not (tmp_path / "files" / "2.bin").exists() and files.peek(settings, "2") is None
    assert fake.downloads == 3


async def test_the_reader_process_gets_none_of_the_servers_secrets(tmp_path, monkeypatch):
    """It parses files written by other people, so it must not hold a password, a token or a provider key."""
    monkeypatch.setenv("ICAMPUS_PASSWORD", "hunter2")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-not-for-the-reader")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-nor-this")
    monkeypatch.setattr(files, "READER", [sys.executable, "-c",
                                          "import json, os; print(json.dumps({'text': ' '.join(sorted(os.environ)), "
                                          "'how': 'env'}))"])
    path = tmp_path / "x.txt"
    path.write_text("hi")
    names, how = await files._extract(path, "x.txt", None)
    assert how == "env" and "PATH" in names.split()
    assert not any(n.startswith("ICAMPUS_") or n.endswith("_KEY") for n in names.split())
