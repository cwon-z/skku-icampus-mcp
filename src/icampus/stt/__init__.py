"""Speech-to-text: the normalized transcript, what a provider has to offer, and how it may fail.

A provider is one registered instance of an adapter (registry.py): "elevenlabs-scribe", "homelab-whisper". Callers
only know this protocol. `transcribe` returns when the transcript exists, however the provider gets there, so a
provider that takes a submission and is polled later, or that calls back on a webhook, is an adapter that polls
(or waits on a future its webhook route resolves) inside `transcribe`; nothing that calls it changes.

Lecture audio and transcripts are copyrighted course material: they stay under var/ and go to the provider you
configured, nowhere else. API keys never appear in an error: every message built here passes through `redact`.

test_clip.ogg is five seconds of synthesized speech for the registry's Test action, made once with
  ffmpeg -f lavfi -i "flite=text='This is a short test of the speech to text service. One, two, three.':voice=slt" \\
         -af apad -t 5 -ac 1 -ar 16000 -c:a libopus -b:a 24k -application voip test_clip.ogg
"""

import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

TEST_CLIP = Path(__file__).with_name("test_clip.ogg")
TEST_CLIP_SECONDS = 5.0
TIMESTAMPS = ("none", "segment", "word")  # weakest to finest


@dataclass
class Word:
    start: float
    end: float
    text: str
    confidence: float | None = None  # 0..1 when the provider gives one


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    language: str | None  # ISO 639-1 where the provider's answer could be mapped
    duration: float | None  # seconds of audio
    segments: list[Segment]
    words: list[Word] | None
    provider: str  # the registered instance that made it
    model: str
    timestamps: str = "segment"  # what the answer actually carried: word, segment or none

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments if s.text)


@dataclass(frozen=True)
class Capabilities:
    timestamps: str = "word"  # finest timestamps the model returns: word, segment or none
    languages: tuple[str, ...] = ()  # ISO 639-1 codes; empty = any, detected when not given
    max_upload_mb: float | None = None  # per request; None = no limit known
    max_duration_s: float | None = None
    audio_formats: tuple[str, ...] = ("opus", "mp3", "flac", "wav", "m4a")
    keyterms: bool = False  # takes a list of terms to listen for

    def merged(self, overrides: dict[str, Any]) -> "Capabilities":
        """With the overrides a provider's config gives (null lifts a limit)."""
        values = asdict(self)
        for key, value in overrides.items():
            if key not in values:
                raise ValueError(f"unknown capability {key!r} (known: {', '.join(values)})")
            if key in ("languages", "audio_formats"):
                if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) for x in value):
                    raise ValueError(f"{key} must be a list of names")
                value = tuple(value)
            elif key == "timestamps":
                if value not in TIMESTAMPS:
                    raise ValueError(f"timestamps must be one of {', '.join(TIMESTAMPS)}")
            elif key == "keyterms":
                if not isinstance(value, bool):
                    raise ValueError("keyterms must be true or false")
            elif value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
                raise ValueError(f"{key} must be a positive number, or null for no limit")
            values[key] = value
        return Capabilities(**values)


class SttError(Exception):
    """A provider call that failed. The message is safe to store and show. `retryable` marks what may pass by
    itself (rate limits, server errors, timeouts, the network)."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class SttProvider(Protocol):
    id: str  # a registered instance, e.g. "elevenlabs-scribe", "homelab-whisper"
    capabilities: Capabilities

    async def transcribe(self, audio: Path, *, language: str | None, options: dict) -> Transcript:
        """The whole file as one normalized transcript, timestamps from the start of this file. `options` may
        carry `keyterms` (list of str), `duration` (seconds, when known) and `format` (the audio format name).
        Raises SttError."""
        ...


# --- helpers the adapters share ----------------------------------------------------------------

_MIME = {"opus": ("audio.ogg", "audio/ogg"), "mp3": ("audio.mp3", "audio/mpeg"), "flac": ("audio.flac", "audio/flac"),
         "wav": ("audio.wav", "audio/wav"), "m4a": ("audio.m4a", "audio/mp4")}


def upload_name(audio: Path, fmt: str | None) -> tuple[str, str]:
    """(file name, content type) to send: a neutral name, never the lecture's title."""
    if fmt in _MIME:
        return _MIME[fmt]
    by_suffix = {"ogg": "opus", "opus": "opus", "mp3": "mp3", "flac": "flac", "wav": "wav", "m4a": "m4a"}
    return _MIME.get(by_suffix.get(audio.suffix.lstrip(".").lower(), ""), ("audio.bin", "application/octet-stream"))


def redact(text: str, *secrets: str | None) -> str:
    """One line, at most 300 characters, with every given secret cut out."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return " ".join(text.split())[:300]


def host_of(url: str) -> str:
    """The host of a URL, for messages: no path, query or credentials."""
    return urlsplit(url).hostname or "the provider"


def http_failure(name: str, response: httpx.Response, *secrets: str | None) -> SttError:
    """An SttError for an HTTP error answer, with the provider's own message when it gave a short one."""
    detail = ""
    try:
        body = response.json()
    except ValueError:
        body = response.text  # whole: redact cuts the key out first and shortens after, never the other way round
    if isinstance(body, dict):
        inner = body.get("detail", body.get("error", body.get("message")))
        if isinstance(inner, dict):
            inner = inner.get("message") or inner.get("code") or inner.get("status")
        elif isinstance(inner, list) and inner and isinstance(inner[0], dict):  # validation errors: [{loc, msg}]
            inner = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg')}" for e in inner[:3])
        detail = str(inner) if inner else ""
    elif isinstance(body, str):
        detail = body
    status = response.status_code
    retryable = status in (408, 409, 425, 429) or status >= 500
    what = {401: "refused the API key", 403: "refused the API key", 402: "says the account is out of credit",
            413: "says the file is too large", 429: "is rate limiting"}.get(status, "answered with an error")
    message = f"{name} {what} (HTTP {status})" + (f": {redact(detail, *secrets)}" if detail else "")
    return SttError(message, retryable=retryable, status=status)


def network_failure(name: str, url: str, exc: httpx.HTTPError, timeout_s: float) -> SttError:
    """An SttError for a request that got no answer. Only the exception's type is kept: its text can hold a URL."""
    if isinstance(exc, httpx.TimeoutException):
        return SttError(f"{name} did not answer within {timeout_s:g} s ({type(exc).__name__})", retryable=True)
    return SttError(f"{name} could not be reached at {host_of(url)} ({type(exc).__name__})", retryable=True)


_ISO3 = {"eng": "en", "kor": "ko", "jpn": "ja", "zho": "zh", "cmn": "zh", "yue": "zh", "spa": "es", "fra": "fr",
         "fre": "fr", "deu": "de", "ger": "de", "ita": "it", "por": "pt", "rus": "ru", "vie": "vi", "tha": "th",
         "ind": "id", "hin": "hi", "ara": "ar", "tur": "tr", "nld": "nl", "pol": "pl", "ukr": "uk", "swe": "sv"}
_NAMES = {"english": "en", "korean": "ko", "japanese": "ja", "chinese": "zh", "mandarin": "zh", "cantonese": "zh",
          "spanish": "es", "french": "fr", "german": "de", "italian": "it", "portuguese": "pt", "russian": "ru",
          "vietnamese": "vi", "thai": "th", "indonesian": "id", "hindi": "hi", "arabic": "ar", "turkish": "tr",
          "dutch": "nl", "polish": "pl", "ukrainian": "uk", "swedish": "sv"}


def norm_language(value: Any) -> str | None:
    """'eng', 'english', 'EN', 'ko-KR' -> 'en', 'en', 'en', 'ko'. Unknown values come back lower-cased."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip().lower()
    if v in _NAMES:
        return _NAMES[v]
    v = v.split("-")[0].split("_")[0]
    return _ISO3.get(v, v)


def confidence(logprob: Any) -> float | None:
    """A log probability as a 0..1 confidence."""
    if not isinstance(logprob, (int, float)) or isinstance(logprob, bool) or math.isnan(logprob):
        return None
    return round(max(0.0, min(1.0, math.exp(min(logprob, 0.0)))), 3)


def seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value) or value < 0:
        return None
    return round(float(value), 3)


_SENTENCE_END = re.compile(r"[.?!…。？！]+[\"'”’)\]]*$")
_NO_SPACE = re.compile(r"[぀-ヿ㐀-䶿一-鿿＀-￯]")  # kana, han, full-width forms
SEGMENT_MAX_S = 28.0
SEGMENT_MAX_CHARS = 320
SEGMENT_PAUSE_S = 1.2


def segments_from_words(words: list[Word], gaps: list[str] | None = None) -> list[Segment]:
    """Sentences out of a word list, for providers that return words only: a segment ends at sentence punctuation,
    at a pause, or when it gets long. `gaps[i]` is the text between word i and the next (ElevenLabs says; Whisper
    doesn't, and then a space is assumed except between Chinese or Japanese characters)."""
    segments: list[Segment] = []
    parts: list[str] = []
    start = 0.0
    for i, word in enumerate(words):
        if not parts:
            start = word.start
        parts.append(word.text)
        nxt = words[i + 1] if i + 1 < len(words) else None
        text = "".join(parts)
        ends = (nxt is None or _SENTENCE_END.search(word.text) is not None
                or nxt.start - word.end > SEGMENT_PAUSE_S
                or nxt.end - start > SEGMENT_MAX_S or len(text) > SEGMENT_MAX_CHARS)
        if ends:
            if text.strip():
                segments.append(Segment(start, max(word.end, start), " ".join(text.split())))
            parts = []
        elif gaps is not None:
            parts.append(gaps[i] if i < len(gaps) else " ")
        elif not (_NO_SPACE.match(word.text[-1:]) and _NO_SPACE.match(nxt.text[:1])):
            parts.append(" ")
    return segments


@dataclass
class HttpAdapter:
    """What the two adapters have in common: identity, limits, and one HTTP client per call."""

    id: str
    model: str
    base_url: str
    api_key: str | None = None
    timeout_s: float = 1800
    params: dict[str, Any] = field(default_factory=dict)
    capabilities: Capabilities = field(default_factory=Capabilities)
    transport: httpx.AsyncBaseTransport | None = None

    def client(self) -> httpx.AsyncClient:
        # no cookies, no redirects: the key goes to the configured host only
        return httpx.AsyncClient(transport=self.transport, follow_redirects=False,
                                 timeout=httpx.Timeout(self.timeout_s, connect=20))


def form_fields(defaults: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Multipart form fields: the adapter's defaults with the config's extra params on top. Booleans become
    true/false, lists repeat their field, null removes a default."""
    out: dict[str, Any] = {}
    for key, value in {**defaults, **extra}.items():
        if value is None or key == "file":
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            out[key] = [str(v) for v in value]
        else:
            out[key] = str(value)
    return out


def join(pieces: list[tuple[float, Transcript]], *, duration: float | None, provider: str, model: str) -> Transcript:
    """One transcript out of the transcripts of consecutive pieces: (start of the piece in the whole file, its
    transcript). Every time is shifted by its piece's start. The result is as fine as its coarsest piece."""
    segments: list[Segment] = []
    words: list[Word] = []
    languages: dict[str, float] = {}
    level = len(TIMESTAMPS) - 1
    for offset, piece in pieces:
        segments += [Segment(round(s.start + offset, 3), round(s.end + offset, 3), s.text) for s in piece.segments]
        words += [Word(round(w.start + offset, 3), round(w.end + offset, 3), w.text, w.confidence)
                  for w in piece.words or []]
        if piece.segments:
            level = min(level, TIMESTAMPS.index(piece.timestamps))
            if piece.language:
                languages[piece.language] = languages.get(piece.language, 0) + sum(len(s.text) for s in piece.segments)
    every_piece_has_words = all(p.words for _, p in pieces if p.segments)
    return Transcript(language=max(languages, key=languages.get) if languages else None, duration=duration,
                      segments=segments, words=words if words and every_piece_has_words else None,
                      provider=provider, model=model, timestamps=TIMESTAMPS[level] if segments else "none")
