"""Stored transcripts: text with markers and paging, subtitles, search (Korean substrings, two-character words,
several terms, awkward characters, the fallback without FTS5), preference, and deletion."""

import sqlite3

import pytest

from helpers import lecture
from icampus.cache import Cache
from icampus.config import Settings
from icampus.store import Store
from icampus.stt import Segment, Transcript, Word
from icampus.transcripts import Transcripts, stamp

LINEAR = [(0.0, 4.0, "오늘은 행렬의 고유값 분해를 다룹니다."), (4.5, 9.0, "An eigenvalue is a scalar."),
          (9.2, 14.0, "식 Ax = λx 를 보세요."), (20.0, 26.0, "100% of the grade_weight comes from the \"final\" exam."),
          (3700.0, 3706.0, "마지막으로 QR 분해입니다.")]


def make(tmp_path, fts: bool = True) -> Transcripts:
    settings = Settings(data_dir=tmp_path, _env_file=None)
    store = Store(settings.db_path)
    transcripts = Transcripts(settings, store, Cache(settings, store))
    if not fts:
        transcripts.fts = False  # as on an SQLite without the trigram tokenizer
    return transcripts


def save(transcripts: Transcripts, item: str, segments, provider: str = "el", duration: float = 3710.0,
         version: str = "20260301120000", **lecture_fields) -> int:
    transcript = Transcript(language="ko", duration=duration, segments=[Segment(*s) for s in segments],
                            words=[Word(s[0], s[1], s[2].split()[0], 0.9) for s in segments], provider=provider,
                            model="m", timestamps="word")
    return transcripts.save(lecture(item, **lecture_fields), transcript, video_version=version, reason="test")


def found(transcripts: Transcripts, query: str, **kw) -> list[tuple[str, str]]:
    return [(h["item_id"], h["at"]) for h in transcripts.search(query, **kw)]


@pytest.mark.parametrize("fts", [True, False], ids=["trigram index", "LIKE fallback"])
def test_search(tmp_path, fts):
    t = make(tmp_path, fts)
    assert t.fts is fts
    save(t, "7001", LINEAR)
    save(t, "7002", [(1.0, 5.0, "정렬 알고리즘과 Eigenvalue 는 관계가 없습니다."), (6.0, 9.0, "퀵 정렬을 봅시다.")],
         duration=600.0)
    assert found(t, "고유값") == [("7001", "00:00")]
    assert found(t, "행렬") == [("7001", "00:00")]  # two characters: too short for a trigram
    assert found(t, "QR") == [("7001", "1:01:40")]  # past the first hour
    assert found(t, "정렬") == [("7002", "00:01")]  # two hits six seconds apart count as one moment
    assert sorted(found(t, "eigenvalue")) == [("7001", "00:04"), ("7002", "00:01")]  # any case
    assert found(t, "EIGEN") == found(t, "eigenvalue")  # a substring is enough
    assert found(t, "고유값 행렬") == [("7001", "00:00")]  # every term, in any order, long and short mixed
    assert found(t, "고유값 정렬") == []
    assert found(t, "eigenvalue 정렬") == [("7002", "00:01")]
    assert found(t, "λx") == [("7001", "00:09")]
    assert found(t, "100%") == [("7001", "00:20")] and found(t, "grade_weight") == [("7001", "00:20")]
    assert found(t, "100_") == [] and found(t, "%") == [("7001", "00:20")]  # % and _ are text, not wildcards
    assert found(t, '"final"') == [("7001", "00:20")] and found(t, "'; DROP TABLE segments;--") == []
    assert found(t, "   ") == [] and found(t, "없는말") == []
    assert found(t, "eigenvalue", item_ids={"7002"}) == [("7002", "00:01")]
    assert len(t.search("분해", limit=1)) == 1 and len(t.search("분해")) == 2

    hit = t.search("scalar")[0]
    assert hit["snippet"] == ("오늘은 행렬의 고유값 분해를 다룹니다. **An eigenvalue is a scalar.** 식 Ax = λx 를 보세요.")
    assert (hit["start_s"], hit["model"], hit["title"], hit["course_id"]) == (4.5, "el", "Lecture 7001", "1001")
    assert hit["also_in"] is None and hit["outdated"] is None


def test_the_same_moment_in_two_transcripts_is_one_hit(tmp_path):
    t = make(tmp_path)
    first = save(t, "7001", LINEAR, provider="el")
    second = save(t, "7001", [(0.3, 4.1, "오늘은 행렬의 고윳값 분해를 다룹니다."), (4.4, 9.1, "An eigen value is a scalar.")],
                  provider="whisper")
    assert t.preferred("7001")["id"] == second  # the newest
    [hit] = t.search("행렬의")
    assert hit["model"] == "whisper" and hit["also_in"] == ["el"]
    [hit] = t.search("고유값")  # only one of them heard it this way: still found
    assert hit["model"] == "el" and hit["also_in"] is None
    t.prefer(first)
    assert t.search("행렬의")[0]["model"] == "el" and t.search("행렬의")[0]["also_in"] == ["whisper"]
    assert [r["preferred"] for r in t.rows("7001")] == [0, 1] and not t.prefer(999)


def test_text_has_markers_and_pages_end_on_a_paragraph(tmp_path):
    t = make(tmp_path)
    segments = [(n * 10.0, n * 10.0 + 8, f"문장 {n} " + "가" * 150) for n in range(40)] + [(3700.0, 3705.0, "끝.")]
    row = t.get(save(t, "7001", segments))
    text = t.text(row)
    lines = text.split("\n")
    assert lines[0].startswith("[0:00:00] 문장 0 ") and lines[-1] == "[1:01:40] 끝."  # h:mm:ss for a long lecture
    assert all(line.startswith("[") for line in lines) and len(lines) == 21  # two sentences per paragraph
    page = t.page(row, max_chars=1000)
    assert page["offset"] == 0 and page["total_chars"] == len(text) and page["text"] == "\n".join(lines[:3])
    rest, offset = [page["text"]], page["next_offset"]
    while offset is not None:
        page = t.page(row, offset=offset, max_chars=1000)
        rest.append(page["text"])
        offset = page["next_offset"]
    assert "\n".join(rest) == text  # the pages put together are the whole text, nothing twice
    window = t.text(row, start_s=100, end_s=125)
    assert window.startswith("[0:01:40] 문장 10") and "문장 12" in window and "문장 13" not in window

    short = t.get(save(t, "7002", [(75.0, 80.0, "one"), (81.0, 85.0, "two"), (95.0, 99.0, "after a pause")],
                       duration=120.0))
    assert t.text(short) == "[01:15] one two\n[01:35] after a pause"
    assert stamp(59) == "00:59" and stamp(3600) == "1:00:00" and stamp(75, hours=True) == "0:01:15"


def test_subtitles(tmp_path):
    t = make(tmp_path)
    row = t.get(save(t, "7001", [(0.0, 4.0, "첫 줄"), (3661.5, 3665.25, "second line")]))
    assert t.vtt(row) == ("WEBVTT\n\n00:00:00.000 --> 00:00:04.000\n첫 줄\n\n"
                          "01:01:01.500 --> 01:01:05.250\nsecond line\n")
    assert t.srt(row) == ("1\n00:00:00,000 --> 00:00:04,000\n첫 줄\n\n"
                          "2\n01:01:01,500 --> 01:01:05,250\nsecond line\n")
    assert t.vtt(row, start_s=3000).count("-->") == 1


def test_outdated_and_preference_order(tmp_path):
    t = make(tmp_path)
    old = save(t, "7001", LINEAR, version="v1")
    current = lecture("7001", updated="v2")
    assert [r["id"] for r in t.mark_outdated([current])] == [old] and t.get(old)["status"] == "outdated"
    assert t.mark_outdated([current]) == []  # only what just turned
    assert t.preferred("7001")["id"] == old  # an outdated transcript is still better than none
    new = save(t, "7001", LINEAR, version="v2")
    assert t.preferred("7001")["id"] == new and t.for_version("asset7001", "v2")["id"] == new
    assert t.for_version("asset7001", "v1")["id"] == old and t.for_version("asset7001", "v3") is None
    assert t.for_version("asset7001", "v2", "whisper") is None
    assert t.search("고유값")[0]["outdated"] is None  # the hit comes from the current one
    t.prefer(old)
    assert t.preferred("7001")["id"] == old  # a pin beats newer
    back = t.mark_outdated([lecture("7001", updated="v1")])  # the video went back to its earlier version
    assert [r["id"] for r in back] == [new] and t.get(old)["status"] == "ready" and t.get(new)["status"] == "outdated"
    assert t.mark_outdated([lecture("9999")]) == []  # a lecture that left the sync keeps its transcripts as they are


def test_deleting_a_transcript_removes_its_file_rows_and_index(tmp_path):
    t = make(tmp_path)
    keep, gone = save(t, "7001", LINEAR), save(t, "7002", [(1.0, 2.0, "지울 내용입니다")])
    path = tmp_path / "transcripts" / f"t{gone}.json"
    assert path.exists() and found(t, "지울") == [("7002", "00:01")]
    assert t.cache.delete("transcript", str(gone))  # as the cache page does
    assert not path.exists() and t.get(gone) is None and found(t, "지울") == []
    assert t.store.one("SELECT COUNT(*) AS n FROM segments WHERE transcript_id=?", (gone,))["n"] == 0
    assert t.store.one("SELECT COUNT(*) AS n FROM segments_fts")["n"] == len(LINEAR)
    assert found(t, "고유값") == [("7001", "00:00")] and not t.delete(gone)

    (tmp_path / "transcripts" / f"t{keep}.json").unlink()  # removed by hand while the server was down
    t.cache.reconcile()
    assert t.get(keep) is None and found(t, "고유값") == []


def test_an_index_entry_lost_with_the_file_still_there_is_restored(tmp_path):
    t = make(tmp_path)
    tid = save(t, "7001", LINEAR)
    t.store.execute("DELETE FROM cache_entries")
    t.cache.reconcile()
    entry = t.cache.get("transcript", str(tid))
    assert entry.item_id == "7001" and entry.meta["provider"] == "el" and t.cache.state(entry) == "kept"
    assert t.load(t.get(tid))["segments"][1][2] == "An eigenvalue is a scalar."


def test_sqlite_here_has_the_trigram_tokenizer():
    """The image's SQLite (3.40) and any recent one do. The LIKE fallback above covers the rest."""
    assert sqlite3.sqlite_version_info >= (3, 34), sqlite3.sqlite_version
