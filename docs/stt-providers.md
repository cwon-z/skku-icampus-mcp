# Speech-to-text providers

How to register the providers that turn lecture audio into transcripts, with example configs for ElevenLabs,
OpenAI, Groq and a self-hosted Whisper server. The design behind this is in [transcripts.md](transcripts.md).

Provider details below were checked against each project's own documentation on 23 November 2026. Nothing here
was run against the real services, so treat a config as a starting point and press **Test** after saving it.
Where the documentation was unclear or two pages disagreed, the text says so.

## Registering a provider

A provider is one registered instance of an adapter: an id, a type, a model, and what it costs. Several can be
registered at once. There are three ways to add one, and they all store the same config:

- **Admin UI**: `/admin/stt` → *Add provider*. The key field is write-only.
- **REST**, with an admin token: `POST /api/v1/stt/providers` with the config as JSON, then
  `PUT /api/v1/stt/providers/<id>/key` with `{"key": "..."}` if the key isn't in the environment.
- **First boot**: while no provider is registered, the server reads a list of configs from the file named by
  `ICAMPUS_STT_SEED_FILE`, or inline JSON in `ICAMPUS_STT_SEED`. Without either it registers the ElevenLabs
  config below. The seed is not read again once a provider exists; edit providers in the UI after that.

**Keys never go in a config.** A config names the environment variable that holds its key (`api_key_env`), or you
store a key from the UI or the key route; it lands in `var/stt_keys.json` (mode 0600) and nothing returns it. The
environment wins when both exist. A config can't name an `ICAMPUS_*` variable, so a typo can't send this server's
own secrets to a provider. In Docker, pass the variable to the `icampus` service only (`compose.yaml` already
passes `ELEVENLABS_API_KEY`, `OPENAI_API_KEY` and `GROQ_API_KEY`); the MCP container gets none.

### Config fields

| Field | Meaning |
|---|---|
| `id` | What `model=` takes. Lowercase letters, digits, `.`, `-`, `_`. |
| `type` | `elevenlabs` or `openai_compatible`. |
| `name`, `description` | Shown in the UI and to the assistant (`list_stt_models`), so it can choose. Say what this one is good for. |
| `model` | The provider's model id. |
| `base_url` | For `openai_compatible`: the address up to and including `/v1`. Never an `skku.edu` host. |
| `api_key_env` | Environment variable with the key. Empty: the stored key, or none for a server without auth. |
| `languages` | ISO 639-1 codes this provider should be used for. Empty: any. |
| `capabilities` | Overrides of what the adapter assumes: `timestamps` (`word`, `segment`, `none`), `max_upload_mb`, `max_duration_s`, `audio_formats`. `null` lifts a limit. |
| `cost_per_hour` | USD per audio hour; 0 for self-hosted. |
| `included_hours` | Hours per budget period a plan already paid for. Estimates and usage count only hours beyond them. |
| `monthly_hours_cap`, `monthly_usd_cap` | Jobs that would pass a cap wait in `paused_budget` until the next period. |
| `reset_day` | Day of the month the provider's budget period starts. Empty: the `stt_budget_reset_day` setting. |
| `concurrency`, `timeout_s`, `priority` | Requests at once; seconds per request; order in automatic routing (lower first). |
| `enabled` | Disabled providers are never chosen. |
| `audio_format`, `audio_bitrate_kbps` | What is extracted and sent: `opus` (in Ogg, the default), `mp3`, `m4a`, `flac`, `wav`. |
| `keyterms` | Send the course's term list (setting `stt_course_terms`). |
| `params` | Extra request fields, sent as they are. A `null` value removes a field the adapter would send. |

### How one gets chosen

- `model=<id>` uses that provider. If it is disabled, has no key, keeps failing, is over a cap, or can't give the
  timestamps required, the request fails with the reason and the ids that would work. It never switches silently.
- Without `model`: the first routing rule that matches the lecture's course and language, then the default
  provider, then the fallback order, then the rest by priority. A provider that fails with a rate limit, a server
  error or a timeout is followed by the next one. Each transcript records who made it and why.
- Routing, the default and the fallback order are edited on `/admin/stt`; the settings behind them are
  `stt_routing_rules`, `stt_default_provider` and `stt_fallback_order`.

### Splitting long lectures

Audio goes whole unless the provider's `max_upload_mb` or `max_duration_s` says it can't. Then it is cut at
silences near `stt_chunk_minutes` (20), each piece is sent, and the times are shifted back and joined. At the
default 24 kbps Opus an hour is about 11 MB, so a 25 MB limit is reached at roughly 2 h 15 min.

## ElevenLabs Scribe

`POST https://api.elevenlabs.io/v1/speech-to-text`, key in the `xi-api-key` header. This is what a first boot
registers when no seed is given:

```json
{
  "id": "elevenlabs-scribe",
  "type": "elevenlabs",
  "name": "ElevenLabs Scribe v3",
  "model": "scribe_v3",
  "description": "Hosted, word-level timestamps, strong on Korean and English lectures. $0.20 per audio hour; a Creator plan covers about 110 hours a month.",
  "api_key_env": "ELEVENLABS_API_KEY",
  "cost_per_hour": 0.20,
  "included_hours": 110,
  "monthly_hours_cap": 100,
  "priority": 10
}
```

- **Models.** `scribe_v3` (released 9 November 2026) is the newest; `scribe_v2` still works with the same
  request. `scribe_v1` has been removed.
- **What is sent.** `model_id`, the audio, `timestamps_granularity=word`, and explicitly `diarize=false` and
  `tag_audio_events=false`: the API's own default for audio events is *true*, which would put "(laughter)" in the
  text. `language_code` only when a language is known.
- **What comes back.** Words with start, end and a log probability, no sentences. Segments are built from the
  words: at sentence punctuation, at a pause over 1.2 s, or after 28 s.
- **Limits.** 5 GB and 15 hours per file, so lectures are never split. Ogg/Opus is accepted.
- **Cost.** $0.20 per audio hour on every self-serve plan; silence is billed. The Creator plan's "110 hours
  included" is the plan's whole allowance expressed in Scribe hours, so other ElevenLabs use eats into it.
  Current plans have no post-paid overage: past the allowance you top up in advance at the same rate, and the
  service stops at zero. Set `reset_day` to your billing day so the budget period matches the invoice.
- **Key terms** cost extra: $0.05 per hour on the pricing page, "20%" in the API reference. Leave `keyterms` off
  unless a course needs it; with it on, up to 100 terms from `stt_course_terms` are sent (more than 100 would
  raise the price again).
- **Concurrency.** A file over 8 minutes counts as up to 4 concurrent requests against the plan's limit (20 on
  Creator). Leave `concurrency` at 1 or 2.
- **Errors.** 401 for a bad key and 402 for no credit are not retried; 429 and 5xx are.

## OpenAI

OpenAI's line-up changed in 2026. Pick by whether you need timestamps:

```json
{
  "id": "openai-whisper",
  "type": "openai_compatible",
  "name": "OpenAI whisper-1",
  "model": "whisper-1",
  "base_url": "https://api.openai.com/v1",
  "description": "Hosted Whisper with word and segment timestamps. $0.36 per audio hour. Deprecated: OpenAI shuts it down on 26 February 2027.",
  "api_key_env": "OPENAI_API_KEY",
  "cost_per_hour": 0.36,
  "monthly_usd_cap": 10,
  "priority": 40
}
```

```json
{
  "id": "openai-transcribe",
  "type": "openai_compatible",
  "name": "OpenAI gpt-transcribe",
  "model": "gpt-transcribe",
  "base_url": "https://api.openai.com/v1",
  "description": "OpenAI's current transcription model. Text only, no timestamps: good for a second reading of a lecture, not for finding moments. $0.27 per audio hour.",
  "api_key_env": "OPENAI_API_KEY",
  "cost_per_hour": 0.27,
  "monthly_usd_cap": 10,
  "priority": 50,
  "capabilities": {"timestamps": "none", "max_duration_s": 1400}
}
```

- **Timestamps exist only on `whisper-1`** (`verbose_json` with `timestamp_granularities[]`). `gpt-transcribe` and
  the `gpt-4o-*` models return text. A provider marked `"timestamps": "none"` is only eligible when the setting
  `stt_min_timestamps` is `none`; its transcript then has one segment per piece sent, so with 20-minute pieces a
  search hit is only that precise.
- **Limits.** 25 MB per upload (the adapter's default `max_upload_mb`). OpenAI documents no maximum duration; a
  1,500-second limit on the `gpt-4o` models is reported by users, which is why the example sets
  `max_duration_s` to 1400. That value is a precaution, not a documented number.
- **Language.** `gpt-transcribe` takes a `languages[]` list instead of `language`, and the guide says not to send
  both. If it rejects the request, add `"params": {"language": null, "languages[]": ["ko", "en"]}`.
- The answer's `language` is a name ("korean"); it is stored as a code (`ko`).

## Groq

```json
{
  "id": "groq-whisper",
  "type": "openai_compatible",
  "name": "Groq whisper-large-v3",
  "model": "whisper-large-v3",
  "base_url": "https://api.groq.com/openai/v1",
  "description": "Hosted Whisper large-v3, fast and cheap ($0.111 per audio hour), word timestamps. Long lectures go in pieces.",
  "api_key_env": "GROQ_API_KEY",
  "cost_per_hour": 0.111,
  "monthly_usd_cap": 5,
  "priority": 30
}
```

- **Models.** `whisper-large-v3` ($0.111/h) and `whisper-large-v3-turbo` ($0.04/h). Both return word and segment
  timestamps with `verbose_json`.
- **Limits.** 25 MB per upload on the free tier, 100 MB on the developer tier (then add
  `"capabilities": {"max_upload_mb": 100}`). Each request is billed for at least 10 seconds.
- **Rate limits.** The free tier allows 7,200 seconds of audio per hour and 28,800 per day: two hours of lecture
  per hour, eight per day. A batch larger than that gets 429s; jobs retry with growing pauses and then fail as
  retryable. For a backlog, use the developer tier or keep Groq as a fallback, not the default.

## A self-hosted Whisper server on a GPU box

Any server with OpenAI's transcription route works. This example is
[speaches](https://github.com/speaches-ai/speaches) (formerly faster-whisper-server) on a machine with an RTX 3090,
reached over the tailnet. No model runs inside the collector: it only sends audio over HTTP.

On the GPU box:

```bash
docker run --detach --name speaches --restart unless-stopped --gpus=all \
  --publish 8000:8000 \
  --volume hf-hub-cache:/home/ubuntu/.cache/huggingface/hub \
  --env WHISPER__INFERENCE_DEVICE=cuda --env WHISPER__COMPUTE_TYPE=float16 \
  ghcr.io/speaches-ai/speaches:latest-cuda
```

```bash
curl -X POST http://localhost:8000/v1/models/Systran/faster-whisper-large-v3
```

The second command installs the model; without it the server answers 404 "not installed locally". Publish the port
on the tailnet address only (`--publish 100.x.y.z:8000:8000`), or set `API_KEY` in the container and give this
server the same key (store it from the UI, or name a variable such as `ICAMPUS_STT_HOMELAB_KEY`).

On this server:

```json
{
  "id": "homelab-whisper",
  "type": "openai_compatible",
  "name": "Homelab Whisper (RTX 3090)",
  "model": "Systran/faster-whisper-large-v3",
  "base_url": "http://100.64.0.20:8000/v1",
  "description": "Self-hosted faster-whisper large-v3 on the GPU box. Free, word timestamps, no monthly cap. Only when the GPU box is on.",
  "cost_per_hour": 0,
  "priority": 20,
  "timeout_s": 3600,
  "capabilities": {"max_upload_mb": null}
}
```

- **The address.** The `icampus` container has to reach the GPU box. With Tailscale on the Docker host, a tailnet
  IP (100.x.y.z) usually works from inside the container; a MagicDNS name only resolves there if you add it with
  `extra_hosts` or point the container's DNS at 100.100.100.100.
- **Image tags.** speaches' documentation follows its `master` branch (0.9.0 release candidates), while the
  `latest-*` images were still 0.8.x when this was written. They differ:
  - 0.8.x returns `probability` with each word (stored as the word's confidence) and unloads an idle model after
    `WHISPER__TTL` seconds (300; `-1` keeps it loaded).
  - 0.9.0-rc returns words without a probability, names the idle time `STT_MODEL_TTL`, and has a non-standard
    request field `without_timestamps` that defaults to true. If **Test** reports `none` timestamps on such a
    build, add `"params": {"without_timestamps": false}`. This comes from reading their source, not from running it.
  Pin a version tag (`0.8.3-cuda`, `0.9.0-rc.3-cuda`) once it works, so an update doesn't change the answers.
- **Which checkpoint.** `Systran/faster-whisper-large-v3` is the accurate choice. `large-v3-turbo` (for example
  `deepdml/faster-whisper-large-v3-turbo-ct2`) is several times faster with, in OpenAI's words, minor quality
  degradation; its model card doesn't single out Korean either way. Both fit a 24 GB card with room to spare
  (faster-whisper's own benchmark has large-v2 at 4.5 GB in float16). Their benchmark runs 12 to 46 times faster
  than real time on a smaller card, so a 130-minute lecture should take minutes; `timeout_s` is set to an hour
  to be safe.
- **Fallback.** With `stt_default_provider` set to `homelab-whisper` and `elevenlabs-scribe` in
  `stt_fallback_order`, lectures go to the GPU box when it answers and to ElevenLabs when it doesn't. After three
  failures in a row the box is skipped for 15 minutes (`stt_fail_threshold`, `stt_skip_minutes`).

Other servers, with what to override:

| Server | Config notes |
|---|---|
| [LocalAI](https://localai.io/docs/models/capabilities/audio/audio-to-text/) | `/v1/audio/transcriptions` with whisper.cpp or faster-whisper backends. `verbose_json` returns segments; word timestamps depend on the backend. If words are missing, set `"capabilities": {"timestamps": "segment", "max_upload_mb": null}`. |
| [vLLM](https://docs.vllm.ai/en/latest/serving/online_serving/speech_to_text/) | Serves Whisper with `vllm[audio]`. Segment timestamps only, and by default it refuses more than 25 MB or 600 s per request: `"capabilities": {"timestamps": "segment", "max_duration_s": 600}`. Lectures then go in 10-minute pieces. |
| whisper.cpp `whisper-server` | Start it with `--inference-path /v1/audio/transcriptions`. Words carry a probability. `"capabilities": {"max_upload_mb": null}`. |

## Seeding several providers at first boot

`ICAMPUS_STT_SEED_FILE` points at a JSON file with a list (or `{"providers": [...]}`):

```json
[
  {"id": "elevenlabs-scribe", "type": "elevenlabs", "model": "scribe_v3", "api_key_env": "ELEVENLABS_API_KEY",
   "cost_per_hour": 0.20, "included_hours": 110, "monthly_hours_cap": 100, "priority": 10,
   "description": "Hosted, word timestamps, the default."},
  {"id": "homelab-whisper", "type": "openai_compatible", "model": "Systran/faster-whisper-large-v3",
   "base_url": "http://100.64.0.20:8000/v1", "priority": 20, "capabilities": {"max_upload_mb": null},
   "description": "Self-hosted, free, when the GPU box is on."}
]
```

A config that doesn't validate is skipped with a log line; the others are registered. Mount the file into the
`icampus` container (it holds no secrets) and set the variable in `compose.homelab.yaml`.

## Adding another kind of provider

An adapter is a class with `id`, `capabilities` and `async transcribe(audio, *, language, options) -> Transcript`
(`src/icampus/stt/__init__.py`). The pipeline awaits that one call per piece of audio and knows nothing else
about the provider, so a service that takes a submission and is polled later (Deepgram or AssemblyAI style) is an
adapter whose `transcribe` submits, then polls until the result is there; one that calls back on a webhook waits
on a future that its webhook route resolves. Neither needs a change in the pipeline, the registry's routing or
the job states. Register the new type in `stt/registry.py` (`ProviderConfig.type` and `Registry.provider`), raise
`SttError(..., retryable=...)` for failures, and pass every message through `redact` with the key.
