"""Audio for transcription, made with ffmpeg: extract a lecture's sound track, and split it only when a provider's
limits ask for that.

The default is mono 16 kHz Opus at 24 kbps in an Ogg file, about 11 MB per hour, which every provider here takes.
ffmpeg runs as a child process with a thread cap and without the ICAMPUS_* environment; nothing is decoded inside
the collector, which keeps it under its memory limit.

Splitting: `plan_chunks` cuts in the middle of the silence nearest each target length, so no word is cut in two,
and `cut` copies the stream without re-encoding. A copied piece starts on an audio frame, so it begins within one
frame (20 ms for Opus) of the planned time; timestamps are shifted by the planned start when pieces are joined.
"""

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

EXTRACT_TIMEOUT_S = 3600
PROBE_TIMEOUT_S = 60
SILENCE_DB = -35  # quieter than this counts as silence
SILENCE_MIN_S = 0.35
_SILENCE_START = re.compile(r"silence_start: (-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end: (-?[\d.]+)")


class AudioError(Exception):
    """ffmpeg failed. `retryable` is False for what trying again can't fix (no ffmpeg, no audio track)."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class Format:
    ext: str
    mime: str
    codec: tuple[str, ...]
    bitrate: bool = True  # lossless formats take none


FORMATS = {
    "opus": Format("ogg", "audio/ogg", ("-c:a", "libopus", "-application", "voip")),
    "mp3": Format("mp3", "audio/mpeg", ("-c:a", "libmp3lame")),
    "m4a": Format("m4a", "audio/mp4", ("-c:a", "aac")),
    "flac": Format("flac", "audio/flac", ("-c:a", "flac"), bitrate=False),
    "wav": Format("wav", "audio/wav", ("-c:a", "pcm_s16le"), bitrate=False),
}


def available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("ICAMPUS_")}  # it needs no password or key


async def _run(args: list[str], timeout: float, on_line: Callable[[str], None] | None = None) -> tuple[int, str, str]:
    """Runs a command; (exit code, stdout, stderr). With on_line, stdout is fed to it line by line instead."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=_env())
    except FileNotFoundError:
        raise AudioError(f"{args[0]} is not installed on this server") from None

    async def pump() -> tuple[str, str]:
        if on_line is None:
            out, err = await proc.communicate()
            return out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        err_task = asyncio.ensure_future(proc.stderr.read())
        async for raw in proc.stdout:
            on_line(raw.decode("utf-8", "replace").strip())
        await proc.wait()
        return "", (await err_task).decode("utf-8", "replace")

    try:
        out, err = await asyncio.wait_for(pump(), timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        proc.kill()
        await proc.wait()
        if isinstance(exc, TimeoutError):
            raise AudioError(f"{args[0]} did not finish within {timeout:g} s", retryable=True) from None
        raise
    return proc.returncode or 0, out, err


def _last_line(err: str) -> str:
    """ffmpeg's last message, without the path it names (paths hold ids; nothing secret, but no use either)."""
    lines = [line.strip() for line in err.splitlines() if line.strip()]
    return re.sub(r"(/[^\s:]+)+", "<file>", lines[-1])[:200] if lines else "no message"


async def probe(path: Path) -> float:
    """Seconds of audio in a file. Raises AudioError when it has no audio track."""
    code, out, err = await _run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                                 "stream=codec_type:format=duration", "-of", "default=nw=1", str(path)],
                                PROBE_TIMEOUT_S)
    if code != 0:
        raise AudioError(f"ffprobe could not read the file ({_last_line(err)})")
    if "codec_type=audio" not in out:
        raise AudioError("the video has no audio track")
    m = re.search(r"duration=([\d.]+)", out)
    if not m:
        raise AudioError("ffprobe gave no duration for the file")
    return float(m.group(1))


async def extract(src: Path, dst: Path, *, fmt: str = "opus", kbps: int = 24, threads: int = 1,
                  duration: float | None = None, on_progress: Callable[[float], None] | None = None) -> float:
    """The first audio track of `src` as mono 16 kHz `fmt` at `kbps`, written beside `dst` and renamed over it.
    Returns the result's duration in seconds. on_progress gets 0..1."""
    spec = FORMATS.get(fmt)
    if spec is None:
        raise AudioError(f"unknown audio format {fmt!r} (known: {', '.join(FORMATS)})")
    dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    container = {"m4a": "ipod", "opus": "ogg"}.get(fmt, spec.ext)
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-threads", str(threads),
            "-i", str(src), "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000", *spec.codec,
            *(("-b:a", f"{kbps}k") if spec.bitrate else ()), "-threads", str(threads),
            "-progress", "pipe:1", "-nostats", "-f", container, str(part)]

    def line(text: str) -> None:
        if on_progress and duration and text.startswith("out_time_us="):
            try:
                on_progress(min(1.0, int(text.split("=", 1)[1]) / 1e6 / duration))
            except ValueError:
                pass

    try:
        code, _, err = await _run(args, EXTRACT_TIMEOUT_S, line)
        if code != 0:
            no_audio = "matches no streams" in err or "does not contain any stream" in err
            raise AudioError("the video has no audio track" if no_audio
                             else f"ffmpeg could not extract the audio ({_last_line(err)})", retryable=not no_audio)
        fd = os.open(part, os.O_RDONLY)
        os.fchmod(fd, 0o600)
        os.close(fd)
        os.replace(part, dst)
    finally:
        part.unlink(missing_ok=True)
    return await probe(dst)


async def silences(path: Path, *, threads: int = 1) -> list[tuple[float, float]]:
    """(start, end) of every stretch quieter than SILENCE_DB for at least SILENCE_MIN_S."""
    code, _, err = await _run(["ffmpeg", "-nostdin", "-hide_banner", "-threads", str(threads), "-i", str(path),
                               "-af", f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MIN_S}", "-f", "null", "-"],
                              EXTRACT_TIMEOUT_S)
    if code != 0:
        raise AudioError(f"ffmpeg could not look for silences ({_last_line(err)})", retryable=True)
    found: list[tuple[float, float]] = []
    start: float | None = None
    for text in err.splitlines():
        if m := _SILENCE_START.search(text):
            start = max(0.0, float(m.group(1)))
        elif (m := _SILENCE_END.search(text)) and start is not None:
            found.append((start, float(m.group(1))))
            start = None
    return found


def plan_chunks(duration: float, quiet: list[tuple[float, float]], *, target_s: float,
                max_s: float) -> list[tuple[float, float]]:
    """(start, end) pieces that cover 0..duration, each at most max_s long. Every cut lies in the middle of the
    silence nearest to target_s after the previous cut; where no silence is in reach, the cut is at the target."""
    if max_s <= 0 or target_s <= 0:
        raise ValueError("chunk lengths must be positive")
    target_s = min(target_s, max_s)
    middles = sorted((a + b) / 2 for a, b in quiet if b > a)
    pieces: list[tuple[float, float]] = []
    pos = 0.0
    while duration - pos > max_s:
        ideal = pos + target_s
        low, high = pos + target_s * 0.5, pos + max_s
        near = [m for m in middles if low <= m <= high]
        cut = min(near, key=lambda m: abs(m - ideal)) if near else ideal
        pieces.append((round(pos, 3), round(cut, 3)))
        pos = cut
    pieces.append((round(pos, 3), round(duration, 3)))
    return pieces


async def cut(src: Path, dst: Path, start: float, end: float) -> None:
    """Copies start..end of an audio file into dst without re-encoding."""
    part = dst.with_name(dst.name + ".part" + dst.suffix)
    try:
        code, _, err = await _run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                                   "-ss", f"{start:.3f}", "-t", f"{max(0.0, end - start):.3f}", "-c", "copy",
                                   str(part)], EXTRACT_TIMEOUT_S)
        if code != 0:
            raise AudioError(f"ffmpeg could not cut the audio ({_last_line(err)})", retryable=True)
        fd = os.open(part, os.O_RDONLY)
        os.fchmod(fd, 0o600)
        os.close(fd)
        os.replace(part, dst)
    finally:
        part.unlink(missing_ok=True)


def chunk_limit_s(size_bytes: int, duration: float, max_upload_mb: float | None,
                  max_duration_s: float | None) -> float | None:
    """The longest piece of this file a provider takes, in seconds, or None when the whole file fits."""
    limits = []
    if max_duration_s and duration > max_duration_s:
        limits.append(max_duration_s)
    if max_upload_mb and size_bytes > max_upload_mb * 2**20 and duration > 0:
        limits.append(max_upload_mb * 2**20 * 0.95 / (size_bytes / duration))  # 5% for container overhead
        if max_duration_s:
            limits.append(max_duration_s)
    return min(limits) if limits else None
