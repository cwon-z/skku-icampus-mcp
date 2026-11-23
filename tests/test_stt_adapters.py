"""The two provider adapters against fake servers: what they send, how they normalize the answers, and how
failures are reported (retryable or not, and never with the API key)."""

import logging

import httpx
import pytest

from helpers import SECRET, fields
from icampus.stt import (TEST_CLIP, Capabilities, SttError, Word, norm_language, redact, segments_from_words)
from icampus.stt.elevenlabs import ElevenLabs, clean_terms
from icampus.stt.openai_compatible import LIMITS, OpenAICompatible


def eleven(handler, **kw) -> ElevenLabs:
    return ElevenLabs(id="el", model="scribe_v3", base_url="", api_key=SECRET, transport=httpx.MockTransport(handler),
                      **kw)


def whisper(handler, **kw) -> OpenAICompatible:
    kw.setdefault("api_key", SECRET)
    return OpenAICompatible(id="whisper", model="large-v3", base_url="http://gpu.example:8000/v1/",
                            transport=httpx.MockTransport(handler), **kw)


def el_words(*words: tuple[str, float, float]) -> list[dict]:
    out = []
    for text, start, end in words:
        out.append({"text": text, "type": "word", "start": start, "end": end, "logprob": -0.1})
        out.append({"text": " ", "type": "spacing", "start": end, "end": end + 0.1, "logprob": 0})
    return out


async def test_elevenlabs_request_and_normalization():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), key=request.headers.get("xi-api-key"), fields=fields(request),
                    body=request.content, cookie=request.headers.get("cookie"))
        return httpx.Response(200, json={
            "language_code": "kor", "language_probability": 0.97, "text": "안녕하세요 여러분. 오늘은 행렬입니다",
            "audio_duration_secs": 12.5,
            "words": [*el_words(("안녕하세요", 0.5, 1.1), ("여러분.", 1.2, 1.8)),
                      {"text": "(기침)", "type": "audio_event", "start": 2.0, "end": 2.4},
                      *el_words(("오늘은", 4.0, 4.5), ("행렬입니다", 4.6, 5.4))]})

    provider = eleven(handler, capabilities=Capabilities(keyterms=True), params={"no_verbatim": True})
    t = await provider.transcribe(TEST_CLIP, language="ko", options={
        "format": "opus", "keyterms": ["고유값", "Krylov subspace", "bad<term>", "x" * 60]})
    assert seen["url"] == "https://api.elevenlabs.io/v1/speech-to-text" and seen["key"] == SECRET
    assert seen["cookie"] is None
    assert seen["fields"] == {
        "model_id": ["scribe_v3"], "language_code": ["ko"], "tag_audio_events": ["false"], "diarize": ["false"],
        "timestamps_granularity": ["word"], "keyterms": ["고유값", "Krylov subspace"], "no_verbatim": ["true"]}
    assert b'name="file"; filename="audio.ogg"' in seen["body"] and b"audio/ogg" in seen["body"]
    assert (t.language, t.duration, t.timestamps, t.provider, t.model) == ("ko", 12.5, "word", "el", "scribe_v3")
    assert [(s.start, s.end, s.text) for s in t.segments] == [
        (0.5, 1.8, "안녕하세요 여러분."), (4.0, 5.4, "오늘은 행렬입니다")]  # sentence end, and the audio event left out
    assert [w.text for w in t.words] == ["안녕하세요", "여러분.", "오늘은", "행렬입니다"]
    assert t.words[0].confidence == pytest.approx(0.905, abs=0.001)


async def test_elevenlabs_sends_no_keyterms_unless_turned_on_and_no_language_unless_given():
    seen = {}

    def handler(request):
        seen.update(fields(request))
        return httpx.Response(200, json={"language_code": "eng", "text": "", "words": []})
    t = await eleven(handler, capabilities=Capabilities(keyterms=False)).transcribe(
        TEST_CLIP, language=None, options={"keyterms": ["eigenvalue"], "duration": 5.0})
    assert "keyterms" not in seen and "language_code" not in seen
    assert t.segments == [] and t.words is None and t.language == "en" and t.duration == 5.0


def test_keyterms_are_cleaned():
    assert clean_terms(["  Krylov   subspace ", "Krylov subspace", "a{b}", "one two three four five six", ""]) == [
        "Krylov subspace"]
    assert len(clean_terms([f"term{n}" for n in range(300)])) == 100


@pytest.mark.parametrize("status, body, retryable, expect", [
    (401, {"detail": {"status": "invalid_api_key", "message": f"Invalid API key {SECRET}"}}, False, "refused the API key"),
    (402, {"detail": {"code": "insufficient_credits", "message": "top up"}}, False, "out of credit"),
    (429, {"detail": {"code": "concurrent_limit_exceeded", "message": "too many"}}, True, "rate limiting"),
    (500, "<html>oops</html>", True, "HTTP 500"),
    (422, {"detail": [{"loc": ["body", "model_id"], "msg": "field required", "type": "missing"}]}, False,
     "body.model_id: field required"),
    (400, {"detail": {"code": "invalid_audio", "message": "not audio"}}, False, "not audio"),
])
async def test_http_errors_say_what_happened_without_the_key(status, body, retryable, expect, caplog):
    def handler(request):
        return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)
    caplog.set_level(logging.DEBUG)
    for provider in (eleven(handler), whisper(handler)):
        with pytest.raises(SttError) as failure:
            await provider.transcribe(TEST_CLIP, language=None, options={})
        assert failure.value.retryable is retryable and failure.value.status == status
        assert expect in str(failure.value) and SECRET not in str(failure.value)
    assert SECRET not in caplog.text


async def test_network_failures_are_retryable_and_name_only_the_host(caplog):
    caplog.set_level(logging.DEBUG)

    def timeout(request):
        raise httpx.ReadTimeout(f"timed out reading {request.url}?token={SECRET}")

    def refused(request):
        raise httpx.ConnectError(f"connection refused for {request.headers}")
    with pytest.raises(SttError, match="did not answer within") as failure:
        await eleven(timeout, timeout_s=30).transcribe(TEST_CLIP, language=None, options={})
    assert failure.value.retryable and SECRET not in str(failure.value)
    with pytest.raises(SttError, match="could not be reached at gpu.example") as failure:
        await whisper(refused).transcribe(TEST_CLIP, language=None, options={})
    assert failure.value.retryable and SECRET not in str(failure.value) and "/v1" not in str(failure.value)
    assert SECRET not in caplog.text


async def test_openai_compatible_asks_for_what_the_model_can_give():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("authorization"), fields(request), request.content))
        return httpx.Response(200, json={"text": "hi"})
    await whisper(handler).transcribe(TEST_CLIP, language="ko", options={"format": "opus"})
    await whisper(handler, capabilities=LIMITS.merged({"timestamps": "segment"}), api_key=None).transcribe(
        TEST_CLIP, language=None, options={"format": "mp3"})
    await whisper(handler, capabilities=LIMITS.merged({"timestamps": "none", "keyterms": True}),
                  params={"temperature": 0, "without_timestamps": False, "response_format": None}).transcribe(
        TEST_CLIP, language=None, options={"keyterms": ["eigenvalue", "행렬"]})
    word, segment, none = seen
    assert word[0] == "http://gpu.example:8000/v1/audio/transcriptions" and word[1] == f"Bearer {SECRET}"
    assert word[2] == {"model": ["large-v3"], "language": ["ko"], "response_format": ["verbose_json"],
                       "timestamp_granularities[]": ["word", "segment"]}
    assert b'filename="audio.ogg"' in word[3]
    assert segment[1] is None and segment[2]["timestamp_granularities[]"] == ["segment"]
    assert "language" not in segment[2] and b'filename="audio.mp3"' in segment[3]
    assert none[2] == {"model": ["large-v3"], "prompt": ["eigenvalue, 행렬"], "temperature": ["0"],
                       "without_timestamps": ["false"]}  # a null param removes the adapter's default


async def test_openai_compatible_reads_whatever_shape_comes_back():
    answers = {
        "verbose": {"task": "transcribe", "language": "korean", "duration": 30.5, "text": "첫 문장. 둘째 문장.",
                    "segments": [{"id": 0, "start": 0.0, "end": 4.2, "text": " 첫 문장."},
                                 {"id": 1, "start": 4.2, "end": 9.0, "text": " 둘째 문장."}],
                    "words": [{"word": " 첫", "start": 0.1, "end": 0.5, "probability": 0.91},
                              {"word": "문장.", "start": 0.6, "end": 1.4}]},
        "nested": {"language": "en", "duration": 8.0, "text": "one two",  # some faster-whisper servers
                   "segments": [{"start": 1.0, "end": 3.0, "text": "one two", "words": [
                       {"word": "one", "start": 1.0, "end": 1.5, "probability": 0.8},
                       {"word": "two", "start": 2.0, "end": 3.0, "probability": 0.7}]}]},
        "words_only": {"language": "english", "text": "First one. Second one.", "words": [
            {"word": "First", "start": 0.0, "end": 0.4}, {"word": "one.", "start": 0.5, "end": 0.9},
            {"word": "Second", "start": 3.0, "end": 3.4}, {"word": "one.", "start": 3.5, "end": 3.9}]},
        "text": {"text": "no timestamps at all", "languages": [{"code": "en"}],
                 "usage": {"type": "duration", "seconds": 61}},
    }
    current = {}

    def handler(request):
        return httpx.Response(200, json=answers[current["shape"]])
    provider = whisper(handler)
    current["shape"] = "verbose"
    t = await provider.transcribe(TEST_CLIP, language=None, options={})
    assert (t.language, t.duration, t.timestamps) == ("ko", 30.5, "word")
    assert [(s.start, s.end, s.text) for s in t.segments] == [(0.0, 4.2, "첫 문장."), (4.2, 9.0, "둘째 문장.")]
    assert (t.words[0].text, t.words[0].confidence, t.words[1].confidence) == ("첫", 0.91, None)
    current["shape"] = "nested"
    t = await provider.transcribe(TEST_CLIP, language=None, options={})
    assert t.timestamps == "word" and [w.text for w in t.words] == ["one", "two"]
    current["shape"] = "words_only"
    t = await provider.transcribe(TEST_CLIP, language=None, options={})
    assert [(s.start, s.end, s.text) for s in t.segments] == [(0.0, 0.9, "First one."), (3.0, 3.9, "Second one.")]
    assert t.language == "en"
    current["shape"] = "text"
    t = await provider.transcribe(TEST_CLIP, language=None, options={})
    assert (t.timestamps, t.language, t.duration, t.words) == ("none", "en", 61.0, None)
    assert [(s.start, s.end, s.text) for s in t.segments] == [(0.0, 61.0, "no timestamps at all")]

    def plain(request):  # response_format=text set through params
        return httpx.Response(200, text="just text\n")
    t = await whisper(plain).transcribe(TEST_CLIP, language="en", options={"duration": 5})
    assert t.timestamps == "none" and t.segments[0].text == "just text" and t.language == "en"


def test_segments_from_words_breaks_on_sentences_pauses_and_length():
    words = [Word(n * 0.5, n * 0.5 + 0.4, f"w{n}") for n in range(100)]  # 50 s without punctuation
    segments = segments_from_words(words)
    assert len(segments) == 2 and segments[0].end - segments[0].start <= 28 and segments[1].end == 49.9
    paused = segments_from_words([Word(0, 1, "before"), Word(5, 6, "after")])
    assert [s.text for s in paused] == ["before", "after"]
    cjk = segments_from_words([Word(0, 1, "今日"), Word(1, 2, "は"), Word(2, 3, "線形代数。"), Word(3, 4, "次")])
    assert [s.text for s in cjk] == ["今日は線形代数。", "次"]  # no spaces put between Japanese words
    assert segments_from_words([]) == []


def test_small_helpers():
    assert [norm_language(x) for x in ("eng", "Korean", "ko-KR", "zh_CN", "xx", "", None)] == [
        "en", "ko", "ko", "zh", "xx", None, None]
    assert redact(f"bad key {SECRET}\nsecond line " + "y" * 400, SECRET, None).startswith("bad key *** second line")
    assert len(redact("y" * 400)) == 300
    caps = Capabilities().merged({"max_upload_mb": None, "timestamps": "segment", "languages": ["ko"]})
    assert caps.max_upload_mb is None and caps.timestamps == "segment" and caps.languages == ("ko",)
    for bad in ({"timestamps": "letter"}, {"max_upload_mb": -1}, {"nonsense": 1}, {"languages": "ko"},
                {"max_duration_s": True}):
        with pytest.raises(ValueError):
            Capabilities().merged(bad)


def test_the_bundled_clip_is_small_and_is_not_a_lecture():
    data = TEST_CLIP.read_bytes()
    assert data.startswith(b"OggS") and b"OpusHead" in data[:100] and 5_000 < len(data) < 40_000
