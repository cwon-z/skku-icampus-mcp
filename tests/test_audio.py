"""Chunk planning and timestamp joining (pure), and the ffmpeg wrappers against audio made on the spot."""

import shutil
import subprocess

import pytest

from helpers import make_lecture_mp4
from icampus import audio
from icampus.stt import Segment, Transcript, Word, join

needs_ffmpeg = pytest.mark.skipif(not audio.available(), reason="ffmpeg is not installed")


def test_cuts_land_in_the_silence_nearest_each_target():
    quiet = [(295.0, 297.0), (310.0, 311.0), (588.0, 592.0), (905.0, 907.0)]
    plan = audio.plan_chunks(1000.0, quiet, target_s=300, max_s=400)
    assert plan == [(0.0, 296.0), (296.0, 590.0), (590.0, 906.0), (906.0, 1000.0)]
    assert all(end - start <= 400 for start, end in plan)
    assert plan[0][0] == 0.0 and plan[-1][1] == 1000.0
    assert all(a[1] == b[0] for a, b in zip(plan, plan[1:]))  # no gap and no overlap


def test_without_a_silence_in_reach_the_cut_is_at_the_target():
    assert audio.plan_chunks(700.0, [], target_s=300, max_s=320) == [(0.0, 300.0), (300.0, 600.0), (600.0, 700.0)]
    far = [(10.0, 11.0), (319.0, 321.0)]  # one too early to be worth a piece, one just past the limit
    assert audio.plan_chunks(500.0, far, target_s=300, max_s=315)[0] == (0.0, 300.0)


def test_a_file_that_fits_is_one_piece_and_bad_lengths_are_refused():
    assert audio.plan_chunks(250.0, [(100.0, 101.0)], target_s=300, max_s=400) == [(0.0, 250.0)]
    assert audio.plan_chunks(399.9, [], target_s=600, max_s=400) == [(0.0, 399.9)]
    with pytest.raises(ValueError):
        audio.plan_chunks(100.0, [], target_s=0, max_s=10)


def test_the_longest_piece_a_provider_takes():
    hour = 3600.0
    eleven_mb = 11 * 2**20  # an hour of 24 kbps Opus
    assert audio.chunk_limit_s(eleven_mb, hour, None, None) is None
    assert audio.chunk_limit_s(eleven_mb, hour, 25, None) is None  # fits in 25 MB
    limit = audio.chunk_limit_s(3 * eleven_mb, 3 * hour, 25, None)  # three hours don't
    assert 7700 < limit < 7800 and limit * (eleven_mb / hour) < 25 * 2**20
    assert audio.chunk_limit_s(eleven_mb, hour, 25, 600) == 600  # a duration limit alone forces pieces
    assert audio.chunk_limit_s(3 * eleven_mb, 3 * hour, 25, 600) == 600


def piece(*segments: tuple[float, float, str], words: bool = True, language: str = "ko",
          level: str = "word") -> Transcript:
    return Transcript(language=language, duration=None, segments=[Segment(*s) for s in segments],
                      words=[Word(s[0], s[1], s[2], 0.9) for s in segments] if words else None,
                      provider="p", model="m", timestamps=level)


def test_joining_pieces_shifts_every_time_by_the_piece_start():
    whole = join([(0.0, piece((1.0, 2.0, "첫째"), (3.0, 4.5, "둘째"))),
                  (296.0, piece((0.5, 1.5, "셋째"))),
                  (590.0, piece((2.25, 3.0, "넷째"), language="en"))],
                 duration=700.0, provider="whisper", model="large-v3")
    assert [(s.start, s.end, s.text) for s in whole.segments] == [
        (1.0, 2.0, "첫째"), (3.0, 4.5, "둘째"), (296.5, 297.5, "셋째"), (592.25, 593.0, "넷째")]
    assert [w.start for w in whole.words] == [1.0, 3.0, 296.5, 592.25]
    assert (whole.language, whole.duration, whole.timestamps, whole.provider) == ("ko", 700.0, "word", "whisper")
    starts = [s.start for s in whole.segments]
    assert starts == sorted(starts)


def test_a_joined_transcript_is_as_fine_as_its_coarsest_piece():
    mixed = join([(0.0, piece((1.0, 2.0, "a"))), (100.0, piece((0.0, 90.0, "b"), words=False, level="none")),
                  (200.0, piece())], duration=300.0, provider="p", model="m")  # the last piece was silent
    assert mixed.timestamps == "none" and mixed.words is None and len(mixed.segments) == 2
    assert join([(0.0, piece())], duration=5.0, provider="p", model="m").segments == []


@pytest.fixture(scope="module")
def lecture_video(tmp_path_factory):
    if not audio.available():
        pytest.skip("ffmpeg is not installed")
    return make_lecture_mp4(tmp_path_factory.mktemp("media") / "lecture.mp4")


@needs_ffmpeg
async def test_extract_find_silences_and_cut(lecture_video, tmp_path):
    seen = []
    out = tmp_path / "audio" / "a.ogg"
    duration = await audio.extract(lecture_video, out, duration=60, on_progress=seen.append)
    assert duration == pytest.approx(60, abs=0.1) and seen and seen[-1] <= 1.0
    assert out.stat().st_mode & 0o777 == 0o600 and not list(out.parent.glob("*.part"))
    assert 100_000 < out.stat().st_size < 400_000  # about 24 kbps, not the 44.1 kHz original
    assert await audio.probe(out) == pytest.approx(60, abs=0.1)

    quiet = await audio.silences(out)
    assert [(round(a), round(b)) for a, b in quiet] == [(9, 11), (29, 31), (48, 50)]
    plan = audio.plan_chunks(duration, quiet, target_s=20, max_s=25)
    assert [round(end) for _, end in plan] == [10, 30, 49, 60]  # every cut in the middle of a silence
    total = 0.0
    for n, (start, end) in enumerate(plan):
        part = tmp_path / f"piece{n}.ogg"
        await audio.cut(out, part, start, end)
        length = await audio.probe(part)
        assert length == pytest.approx(end - start, abs=0.05)  # a copied piece starts within a frame of the plan
        total += length
    assert total == pytest.approx(duration, abs=0.2)


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["mp3", "flac", "wav", "m4a"])
async def test_other_formats(lecture_video, tmp_path, fmt):
    out = tmp_path / f"a.{audio.FORMATS[fmt].ext}"
    assert await audio.extract(lecture_video, out, fmt=fmt, kbps=32) == pytest.approx(60, abs=0.2)


@needs_ffmpeg
async def test_a_video_without_sound_is_a_clear_error_that_retrying_cannot_fix(tmp_path):
    silent = tmp_path / "silent.mp4"
    subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=64x64:r=5:d=2", "-c:v", "libx264", str(silent)], check=True)
    with pytest.raises(audio.AudioError, match="no audio track") as failure:
        await audio.extract(silent, tmp_path / "x.ogg")
    assert failure.value.retryable is False and not (tmp_path / "x.ogg").exists()
    with pytest.raises(audio.AudioError, match="unknown audio format"):
        await audio.extract(silent, tmp_path / "x.xyz", fmt="xyz")


async def test_a_missing_ffmpeg_is_reported_not_raised_as_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing installed here
    assert not audio.available()
    with pytest.raises(audio.AudioError, match="ffprobe is not installed"):
        await audio.probe(tmp_path / "a.ogg")


@needs_ffmpeg
async def test_ffmpeg_never_sees_the_servers_secrets(lecture_video, tmp_path, monkeypatch):
    seen = {}
    real = audio.asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        seen.update(kwargs["env"])
        return await real(*args, **kwargs)
    monkeypatch.setenv("ICAMPUS_PASSWORD", "hunter2")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-not-for-ffmpeg")
    monkeypatch.setenv("SOME_OTHER_PROVIDER_KEY", "also-not")
    monkeypatch.setattr(audio.asyncio, "create_subprocess_exec", spy)
    await audio.probe(lecture_video)
    assert "PATH" in seen and not any(k.startswith("ICAMPUS_") for k in seen)
    assert not any("KEY" in k for k in seen) and "sk-not-for-ffmpeg" not in seen.values()
