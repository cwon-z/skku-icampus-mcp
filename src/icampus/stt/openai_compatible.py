"""Any server that speaks OpenAI's transcription route: POST {base_url}/audio/transcriptions.

That covers OpenAI, Groq and self-hosted Whisper servers (speaches, LocalAI, vLLM, whisper.cpp's server). They
differ in what they return, so the provider's capabilities say what to ask for:

- timestamps "word": `response_format=verbose_json` with `timestamp_granularities[]` word and segment (whisper-1,
  Groq, faster-whisper servers);
- "segment": verbose_json with segments only (vLLM);
- "none": `response_format=json`, text only (OpenAI's gpt-transcribe and the gpt-4o models).

The answer is read for what it actually carries, whatever was asked for: segments, words (top-level, or nested in
the segments as some faster-whisper servers send them), or just text. Whether text without timestamps is good
enough is the pipeline's decision (setting stt_min_timestamps), not the adapter's.
"""

from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import (Capabilities, HttpAdapter, Segment, SttError, Transcript, Word, form_fields, http_failure,
               network_failure, norm_language, seconds, segments_from_words, upload_name)

# OpenAI and Groq's free tier take 25 MB per request. A self-hosted server's config lifts this with
# {"max_upload_mb": null}.
LIMITS = Capabilities(timestamps="word", max_upload_mb=25, keyterms=False)
PROMPT_CHARS = 800  # Whisper reads about 224 tokens of prompt


@dataclass
class OpenAICompatible(HttpAdapter):
    capabilities: Capabilities = field(default_factory=lambda: LIMITS)

    async def transcribe(self, audio: Path, *, language: str | None, options: dict) -> Transcript:
        name, mime = upload_name(audio, options.get("format"))
        level = self.capabilities.timestamps
        defaults: dict = {"model": self.model, "language": language,
                          "response_format": "json" if level == "none" else "verbose_json"}
        if level != "none":
            defaults["timestamp_granularities[]"] = ["word", "segment"] if level == "word" else ["segment"]
        terms = [t for t in options.get("keyterms") or [] if isinstance(t, str) and t.strip()]
        if terms and self.capabilities.keyterms:  # Whisper has no term list; a prompt naming the terms biases it
            defaults["prompt"] = ", ".join(terms)[:PROMPT_CHARS]
        url = f"{self.base_url.rstrip('/')}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            with open(audio, "rb") as f:
                async with self.client() as client:
                    response = await client.post(url, headers=headers, data=form_fields(defaults, self.params),
                                                 files={"file": (name, f, mime)})
        except httpx.HTTPError as exc:
            raise network_failure(self.id, url, exc, self.timeout_s) from None
        if response.status_code != 200:
            raise http_failure(self.id, response, self.api_key)
        try:
            data = response.json()
        except ValueError:  # response_format=text, srt or vtt set through params: keep the text
            data = {"text": response.text}
        if isinstance(data, str):
            data = {"text": data}
        if not isinstance(data, dict):
            raise SttError(f"{self.id} answered with an unexpected shape")
        return self._normalize(data, language, options)

    def _normalize(self, data: dict, language: str | None, options: dict) -> Transcript:
        segments: list[Segment] = []
        nested: list[dict] = []
        for raw in data.get("segments") or []:
            if not isinstance(raw, dict):
                continue
            start, end, text = seconds(raw.get("start")), seconds(raw.get("end")), str(raw.get("text") or "").strip()
            if start is not None and text:
                segments.append(Segment(start, max(start, end if end is not None else start), text))
            nested += [w for w in raw.get("words") or [] if isinstance(w, dict)]
        words: list[Word] = []
        for raw in data.get("words") or nested:
            if not isinstance(raw, dict):
                continue
            start, end = seconds(raw.get("start")), seconds(raw.get("end"))
            text = str(raw.get("word") or raw.get("text") or "").strip()
            if start is not None and text:
                probability = raw.get("probability")
                words.append(Word(start, max(start, end if end is not None else start), text,
                                  round(float(probability), 3) if isinstance(probability, (int, float))
                                  and not isinstance(probability, bool) and 0 <= probability <= 1 else None))
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        duration = seconds(data.get("duration")) or seconds(usage.get("seconds")) or seconds(options.get("duration")) \
            or (segments[-1].end if segments else words[-1].end if words else None)
        level = "word" if words else "segment" if segments else "none"
        if not segments and words:
            segments = segments_from_words(words)
        if not segments and (text := str(data.get("text") or "").strip()):
            segments = [Segment(0.0, duration or 0.0, text)]  # no timestamps came back: one piece, start to end
        detected = data.get("language")
        if not detected and isinstance(data.get("languages"), list) and data["languages"]:
            first = data["languages"][0]  # gpt-transcribe: [{"code": "en"}]
            detected = first.get("code") if isinstance(first, dict) else first
        return Transcript(language=norm_language(detected) or language, duration=duration, segments=segments,
                          words=words or None, provider=self.id, model=self.model, timestamps=level)
