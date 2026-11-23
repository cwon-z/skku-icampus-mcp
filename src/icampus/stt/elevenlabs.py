"""ElevenLabs Speech-to-Text (Scribe), checked against the API reference in November 2026.

POST {base}/speech-to-text as multipart form data with the key in `xi-api-key`. The answer is a list of words (and
the spacing between them) with start and end times; there are no sentences, so segments are built from the words.
Sent explicitly because their defaults differ from what is wanted here: `tag_audio_events=false` (it defaults to
true and would put "(laughter)" in the text) and `diarize=false`. Key terms go as repeated `keyterms` fields and
are billed extra, so they are only sent when the provider's config turns them on.
"""

from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import (Capabilities, HttpAdapter, SttError, Transcript, Word, confidence, form_fields, http_failure,
               network_failure, norm_language, seconds, segments_from_words, upload_name)

BASE_URL = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL = "scribe_v3"
# Their limits are 5 GB and 15 hours per file; a little under, so a file at the edge is split rather than refused.
LIMITS = Capabilities(timestamps="word", max_upload_mb=4500, max_duration_s=14.5 * 3600, keyterms=True)
MAX_TERMS = 100  # more than 100 raises the price again and adds a 20 s minimum
_FORBIDDEN = set("<>{}[]\\")


def clean_terms(terms: list[str] | None) -> list[str]:
    """Key terms the API accepts: up to 50 characters and 5 words each, none of < > { } [ ] \\."""
    out: list[str] = []
    for term in terms or []:
        term = " ".join(str(term).split())
        if term and len(term) <= 50 and len(term.split()) <= 5 and not _FORBIDDEN & set(term) and term not in out:
            out.append(term)
    return out[:MAX_TERMS]


@dataclass
class ElevenLabs(HttpAdapter):
    capabilities: Capabilities = field(default_factory=lambda: LIMITS)

    async def transcribe(self, audio: Path, *, language: str | None, options: dict) -> Transcript:
        name, mime = upload_name(audio, options.get("format"))
        terms = clean_terms(options.get("keyterms")) if self.capabilities.keyterms else []
        fields = form_fields({"model_id": self.model or DEFAULT_MODEL, "language_code": language,
                              "tag_audio_events": False, "diarize": False, "timestamps_granularity": "word",
                              "keyterms": terms or None}, self.params)
        url = f"{(self.base_url or BASE_URL).rstrip('/')}/speech-to-text"
        try:
            with open(audio, "rb") as f:
                async with self.client() as client:
                    response = await client.post(url, headers={"xi-api-key": self.api_key or ""}, data=fields,
                                                 files={"file": (name, f, mime)})
        except httpx.HTTPError as exc:
            raise network_failure("ElevenLabs", url, exc, self.timeout_s) from None
        if response.status_code != 200:
            raise http_failure("ElevenLabs", response, self.api_key)
        try:
            data = response.json()
        except ValueError:
            raise SttError("ElevenLabs answered with something that is not JSON") from None
        if isinstance(data, dict) and isinstance(data.get("transcripts"), list) and data["transcripts"]:
            data = data["transcripts"][0]  # the multichannel shape; one channel is sent
        if not isinstance(data, dict):
            raise SttError("ElevenLabs answered with an unexpected shape")
        return self._normalize(data, language, options)

    def _normalize(self, data: dict, language: str | None, options: dict) -> Transcript:
        words: list[Word] = []
        gaps: list[str] = []
        for token in data.get("words") or []:
            if not isinstance(token, dict):
                continue
            kind, text = token.get("type"), str(token.get("text") or "")
            if kind == "spacing":
                if gaps:
                    gaps[-1] += text or " "
            elif kind == "word" and text.strip():
                last = words[-1].end if words else 0.0
                start = seconds(token.get("start"))
                start = last if start is None else start
                end = seconds(token.get("end"))
                words.append(Word(start, max(start, end if end is not None else start), text.strip(),
                                  confidence(token.get("logprob"))))
                gaps.append("")
            # audio events ("(music)") are left out
        text = str(data.get("text") or "").strip()
        duration = seconds(data.get("audio_duration_secs")) or seconds(options.get("duration")) \
            or (words[-1].end if words else None)
        if words:
            segments, level = segments_from_words(words, gaps), "word"
        else:  # timestamps turned off through the config's params, or nothing was said
            from . import Segment
            segments, level = ([Segment(0.0, duration or 0.0, text)] if text else []), "none"
        return Transcript(language=norm_language(data.get("language_code")) or language, duration=duration,
                          segments=segments, words=words or None, provider=self.id,
                          model=self.model or DEFAULT_MODEL, timestamps=level)
