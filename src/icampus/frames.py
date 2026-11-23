"""Slide frames: one picture for each stretch of a lecture video during which the screen showed the same thing.

ffmpeg decodes key frames only (fast, and encoders put a key frame where the picture changes), keeps those whose
scene score against the previous one passes a threshold, and writes them as small JPEGs. A 16x16 grey thumbnail
of each (also made by ffmpeg, so no image library is needed) is its perceptual fingerprint: runs of frames whose
fingerprints barely differ are one slide. That folds a moving cursor or one more bullet point into the slide it
belongs to. Of such a run the last picture is kept (the slide with everything on it) with the first one's
start time.

The fingerprints are compared by their mean difference, not as a bit hash: a difference hash turned out to merge
sparse slides (a title on white has few edges to hash) and can't tell a white page from a black one.

The result is var/frames/<item_id>/NNN.jpg plus index.json with each slide's start and end. Like the videos they
come from, the frames are lecture content: private, served only behind auth.
"""

import asyncio
import json
import math
import os
import re
import shutil
from pathlib import Path

from .audio import EXTRACT_TIMEOUT_S, AudioError, _env, _last_line, _run

SIDE = 16  # a fingerprint is a SIDE x SIDE grey thumbnail
SAME_SLIDE = 2.5  # mean brightness difference (of 255) up to which two frames show the same slide: about one
#                   new line of text on a slide; a cursor is 0.05, a different slide 5 and more
MAX_SLIDES = 400  # a lecture with more "slides" than this is a camera recording: the grouping is loosened
_PTS = re.compile(r"pts_time:([\d.]+)")


def difference(a: bytes, b: bytes) -> float:
    """Mean absolute difference of two fingerprints, 0 (the same picture) to 255."""
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, min(len(a), len(b)))


def group(times: list[float], prints: list[bytes], duration: float, *, same: float = SAME_SLIDE,
          most: int = MAX_SLIDES) -> list[dict]:
    """Candidate frames -> slides. Consecutive frames that look alike are one slide, shown from the first one's
    time until the next slide starts; `frame` is the index of the last frame of the run. Each frame is compared
    with the one before it, so a slide that builds up line by line stays one slide. A slide that returns later
    is a new slide: what was said then belongs to that moment. More than `most` slides means the picture never
    settles (a camera): the comparison is loosened, and what is still too many is thinned out evenly."""
    while True:
        slides: list[dict] = []
        for i, at in enumerate(times):
            if i and difference(prints[i], prints[i - 1]) <= same:
                slides[-1]["frame"] = i  # the same slide, a little further on: keep the later picture
            else:
                slides.append({"start": round(at, 2), "frame": i})
        if len(slides) <= most or same >= 64:
            break
        same *= 2
    if len(slides) > most:
        slides = slides[::math.ceil(len(slides) / most)]
    for n, slide in enumerate(slides):
        slide["end"] = round(slides[n + 1]["start"] if n + 1 < len(slides) else max(duration, slide["start"]), 2)
    return slides


async def extract(video: Path, out_dir: Path, *, duration: float, threshold: float = 0.1, width: int = 960,
                  threads: int = 1, about: dict | None = None) -> list[dict]:
    """Writes the slides of `video` into out_dir (replacing what was there) and returns them:
    [{n, start, end, file}]. `about` (item, asset, video version) is recorded in index.json, so the directory
    says what it belongs to. Raises AudioError (the ffmpeg error type) when ffmpeg fails."""
    work = out_dir.with_name(out_dir.name + ".part")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(mode=0o700, parents=True)
    try:
        select = f"select='eq(n,0)+gt(scene,{threshold:.3f})',showinfo,scale='min({int(width)},iw)':-2"
        code, _, err = await _run(["ffmpeg", "-nostdin", "-hide_banner", "-y", "-threads", str(threads),
                                   "-skip_frame", "nokey", "-i", str(video), "-an", "-sn", "-dn", "-vf", select,
                                   "-vsync", "vfr", "-q:v", "5", "-threads", str(threads),
                                   str(work / "c%05d.jpg")], EXTRACT_TIMEOUT_S)
        if code != 0:
            no_video = "matches no streams" in err or "Output file does not contain any stream" in err
            raise AudioError("the file has no video track" if no_video
                             else f"ffmpeg could not read the frames ({_last_line(err)})", retryable=not no_video)
        files = sorted(work.glob("c*.jpg"))
        times = [float(t) for t in _PTS.findall(err)][:len(files)]
        if not files or len(times) != len(files):
            raise AudioError("ffmpeg found no frames in the video")
        size = SIDE * SIDE
        code, raw, err = await _run_bytes(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-threads",
                                           str(threads), "-i", str(work / "c%05d.jpg"), "-vf",
                                           f"scale={SIDE}:{SIDE},format=gray", "-f", "rawvideo", "-"])
        if code != 0 or len(raw) < size * len(files):
            raise AudioError(f"ffmpeg could not compare the frames ({_last_line(err)})", retryable=True)
        prints = [raw[i * size:(i + 1) * size] for i in range(len(files))]
        slides = group(times, prints, duration)
        for n, slide in enumerate(slides, 1):
            name = f"{n:03d}.jpg"
            os.replace(files[slide.pop("frame")], work / name)
            os.chmod(work / name, 0o600)
            slide.update(n=n, file=name)
        for leftover in work.glob("c*.jpg"):
            leftover.unlink()
        (work / "index.json").write_text(json.dumps({**(about or {}), "duration": duration, "slides": slides}))
        os.chmod(work / "index.json", 0o600)
        shutil.rmtree(out_dir, ignore_errors=True)
        os.replace(work, out_dir)
        return slides
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _run_bytes(args: list[str]) -> tuple[int, bytes, str]:
    """Like audio._run, but the output is binary."""
    try:
        proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                    env=_env())
    except FileNotFoundError:
        raise AudioError("ffmpeg is not installed on this server") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), EXTRACT_TIMEOUT_S)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out, err.decode("utf-8", "replace")


def index(directory: Path) -> dict:
    """A frames directory's index.json ({} when it is missing or broken)."""
    try:
        data = json.loads((directory / "index.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read(directory: Path) -> list[dict]:
    """The slides recorded in a frames directory, or [] when it has none."""
    slides = index(directory).get("slides")
    return [s for s in slides if isinstance(s, dict) and re.fullmatch(r"\d{3,5}\.jpg", str(s.get("file")))] \
        if isinstance(slides, list) else []
