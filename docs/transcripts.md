# Lecture transcripts, pluggable speech-to-text and the admin UI: design

What was there, what is being built on it, how the database changes, and where the brief turned out wrong or
unclear. Provider setup has its own page: [stt-providers.md](stt-providers.md). How iCampus itself behaves is in
[icampus-notes.md](icampus-notes.md).

## What was there

- **Two services, one image.** `icampus` (FastAPI, :9013) logs in, syncs and owns the data directory; `icampus-mcp`
  (:8724) holds an API token and nothing else. Both stay that way: every new MCP tool is a call to a new API route.
- **`store.py`**: one SQLite file with `records` (synced rows as JSON, grouped in scopes), `scopes`, `runs` and a
  key/value `state`. The schema script ended in `PRAGMA user_version = 1` and ran at every start, so there was no
  way to add a version: the next start would have set it back to 1.
- **`videos.py`**: exports a Commons-hosted lecture video to `var/videos/<asset>.<ext>` with a JSON sidecar. One
  download at a time behind an `asyncio.Lock`; jobs lived in a dict (lost on restart); `.part` files are removed at
  start; `_make_room` evicted by the sidecar's `last_used` and knew nothing about anyone still needing a video.
  Commons ignores `Range`, so a failed download starts over.
- **`files.py`**: attachments in `var/files/<id>.bin/.txt/.json`, reused until a sync sees a new version (12 hours
  when iCampus gives none), 30 MB limit, both as constants. No cap, no eviction, no listing.
- **`sync.py`**: one run under a file lock shared with attachment downloads; the schedule; a manual trigger with a
  cooldown and a login guard. Lecture rows get their `video` field (the Commons asset) only from a sync by code
  that knows it, which is why exports answered 404 after the export feature was deployed until a sync had run.
- **`api.py` / `mcp_server.py`**: bearer tokens with labels, `admin` labels for the one admin action
  (`retry_login`); list answers in an envelope; MCP rows slimmed with `_row`/`_slim`.
- **Image**: `python:3.12-slim-bookworm` (SQLite 3.40, which has FTS5 and the trigram tokenizer), Chromium's
  headless shell, no ffmpeg; 1 GB memory limit for the collector.

## Design

```
                 ┌───────────────────────── icampus (API, :9013) ─────────────────────────┐
 sync ──► records│  runtime.py   settings registry: UI value > ICAMPUS_* > default        │
                 │  cache.py     index + caps + LRU over video/audio/attachment/transcript │
 POST /transcripts ─► pipeline.py  jobs in SQLite: queued → downloading → extracting →    │
                 │       │           transcribing → done | failed | paused_budget | canceled│
                 │       ├─ videos.py   the existing downloader and its one-at-a-time lock │
                 │       ├─ audio.py    ffmpeg: extract, find silences, cut chunks         │
                 │       ├─ stt/        registry, routing, budget, health; two adapters ───┼──► ElevenLabs / any
                 │       └─ transcripts.py  JSON under var/transcripts, rows, FTS5 search  │    OpenAI-compatible server
                 │  admin/       server-rendered UI at /admin (cookie session, CSRF)       │
                 └──────────────────────────────────────────────────────────────────────────┘
 icampus-mcp (:8724): new tools that call the new routes; no volume, no STT keys.
```

### Part 1: one cache manager (`cache.py`) and runtime settings (`runtime.py`)

- **Index in SQLite, files where they were.** `cache_entries` has one row per entry: kind (`video`, `audio`,
  `attachment`, `transcript`, `frames`), key, course, item, title, size, the files it consists of, last use and a
  pin. Videos and attachments keep writing the sidecar they always had, so the index can be rebuilt from the disk
  and an older version of the program still finds its files.
- **Upgrade without re-downloading.** At start `reconcile()` adopts every video and attachment that has a sidecar
  but no row (taking `last_used` from the old sidecar), drops rows whose files are gone and corrects sizes. It
  reads the disk and the database only.
- **Caps.** One per kind (`video_cache_gb` 30, `audio_cache_gb` 5, `attachment_cache_gb` 2, `frames_cache_gb` 5)
  and one for everything (`cache_total_gb` 40). Eviction is least recently used. It never removes a pinned entry,
  an entry a queued or running job still needs, or a transcript. Before a video download `make_room` raises
  instead of evicting such an entry; the old `_make_room` would have deleted a video whose transcription had not
  run yet.
- **Who needs what.** The job queue tells the cache which entries are in use: the cached video of every job that
  has no audio yet, and the audio of every job that is not finished. That is derived from the jobs table, so it
  survives restarts and cannot leak.
- **Settings.** `runtime.py` holds one typed registry (type, range, help, unit, group, whether a change applies now
  or needs a restart). `Runtime` wraps the environment `Settings`: a value stored from the UI wins over
  `ICAMPUS_<NAME>`, which wins over the default, and the page shows which of the three is in force. Code asks for
  a setting each time it needs it, so a new cap applies at once (lowering one trims the cache immediately;
  changing the sync times restarts the schedule loop). Tokens, the SKKU login, login limits and the listen address
  are not in the registry and can't be changed at runtime.
- **REST.** `GET /api/v1/cache` (kind, course, pinned, sort; totals against caps), `POST|DELETE
  /api/v1/cache/{kind}/{key}/pin`, `DELETE /api/v1/cache/{kind}/{key}` (admin), `GET|PUT /api/v1/settings` (admin).

### Part 2: speech-to-text

- **Interface.** `SttProvider` is the protocol from the brief: `id`, `capabilities`, and `async transcribe(audio,
  language, options) -> Transcript`. The call returns when the transcript exists, however the provider gets there,
  so a submit-then-poll or webhook provider is an adapter that polls (or waits on a future its webhook route
  resolves) inside `transcribe`; callers don't change. `Transcript` is normalized: language (ISO 639-1 where
  known), duration, segments (start, end, text), optional words (start, end, text, confidence), provider id, model.
- **Adapters.** `elevenlabs` (`POST https://api.elevenlabs.io/v1/speech-to-text`, header `xi-api-key`) and
  `openai_compatible` (`POST {base_url}/audio/transcriptions`, bearer key optional). ElevenLabs returns words only,
  so segments are built from them (sentence end, a pause, or a length limit). A model that returns no timestamps
  is marked `timestamps: none`; the setting `stt_min_timestamps` (`word`, `segment` or `none`) decides whether such
  output is accepted.
- **Registry.** Provider instances are rows in `stt_providers` (config as JSON, validated by a pydantic model):
  id, type, name, description, model, base URL, key source, languages, capability overrides, cost per hour, hours
  included in a plan, monthly hour and USD caps, reset day, concurrency, timeout, priority, enabled, audio format
  and bitrate, extra request params. Keys come from the environment variable the config names, else from
  `var/stt_keys.json` (0600, written from the UI, never read back out). The environment wins. A config may not
  name an `ICAMPUS_*` variable as its key, so a typo can't send the SKKU password to a provider.
- **First boot.** With an empty table the registry is seeded from `ICAMPUS_STT_SEED_FILE` (a JSON file) or
  `ICAMPUS_STT_SEED` (inline JSON); without either it creates one ElevenLabs provider that reads
  `ELEVENLABS_API_KEY`, with a 100 h monthly cap.
- **Health.** *Test* transcribes a bundled 5-second clip of synthesized speech (made with ffmpeg's flite voice;
  no lecture audio) and records the result. After `stt_fail_threshold` failures in a row a provider is skipped
  for `stt_skip_minutes`.
- **Choosing.** With `model=<id>`: that provider or an error that says why not (disabled, no key, unhealthy, over
  its cap, language or timestamps it can't do) and lists the ids that would work. Without: the first routing rule
  that matches (course and/or language), then the default provider, then the fallback order, then the rest by
  priority; ineligible ones are skipped. On a retryable failure (429, 5xx, timeout, connection) automatic routing
  moves to the next eligible provider; an explicit model retries the same one later. Every transcript and job
  records the provider and the reason it was chosen.
- **Budget.** `stt_usage` gets a row per successful provider call (audio seconds, cost). A provider's period
  starts on its reset day; hours and USD in the period are checked against its caps, and all providers together
  against `stt_monthly_usd_cap`. Cost counts only hours beyond `included_hours`. A job whose provider is over its
  cap waits in `paused_budget` and resumes when the period rolls over or the cap is raised.
- **Pipeline.** Jobs are rows in `jobs`, so the queue survives restarts (anything caught mid-stage goes back to
  `queued`, and finished stages are found on disk and skipped). Per job: (1) the video, through
  `Videos.ensure()`, which shares the export lock and job record; (2) audio with ffmpeg, mono 16 kHz Opus at
  24 kbps by default (format and bitrate per provider), one ffmpeg at a time with `-threads 1`; (3) chunks only if
  the provider's upload or duration limit requires them, cut in the middle of the silence nearest the target
  length, timestamps shifted back and joined; (4) transcribe, store, index.
- **Prefetch.** At most `prefetch_limit` (2) jobs are past `queued` at a time, so downloads never run more than
  that far ahead of transcription, and jobs whose video is already cached go first. The budget is checked before
  a job downloads anything.
- **Idempotent.** A unique index allows one active job per (asset, video version, requested model). Asking again
  returns that job, or the transcript if one exists for this version (from any provider when no model is named).
- **Outdated.** A transcript stores the video's synced `updated_at`. After a sync, transcripts whose video has a
  newer one are marked `outdated`; they are re-run on request, or automatically with `stt_auto_retranscribe`.
- **Storage and search.** `var/transcripts/t<id>.json` holds the normalized transcript; `transcripts` and
  `segments` hold the rows; `segments_fts` is an FTS5 table with the trigram tokenizer. Terms of 3+ characters go
  through FTS (all terms must match), shorter ones (most two-syllable Korean words) through `LIKE`. Search covers
  every transcript of a lecture and folds hits at the same moment into one, preferring the preferred transcript.
  At start the SQLite version and a trial `CREATE VIRTUAL TABLE` decide whether FTS is used at all; without it
  search falls back to `LIKE` and `/api/v1/status` says so.

### Part 3: MCP tools

`list_stt_models`, `transcribe_lecture`, `queue_transcriptions`, `transcription_status`, `get_transcript`,
`search_transcripts`, `lecture_stats`, plus transcript fields in `list_lectures`. Same conventions as the existing
tools: slim rows, numeric ids checked before the call, `ToolError` for anything the API refuses.

### Part 4: admin UI

Served by the API at `/admin`. Sign-in takes an admin-labelled API token once and sets a signed, expiring,
`HttpOnly`, `SameSite=Strict` cookie scoped to `/admin`; sessions are recorded server-side so signing out ends
them. Every form carries a CSRF token and every state-changing request is checked for it and for a same-origin
`Origin`. Jinja2 templates with autoescape, HTMX vendored, one `theme.css` built from the Nocturne tokens, a
strict Content-Security-Policy (no inline script or style, no external hosts). The pages read the index, the
database and the disk; only buttons that say so (re-download, transcribe, queue) reach lcms, and nothing in the UI
reaches canvas.

## Schema changes

`PRAGMA user_version` now means something: `MIGRATIONS` is a list of scripts, one per version, applied in order
at start. Every statement is `IF NOT EXISTS` and nothing is dropped or rewritten.

| Version | Adds |
|---|---|
| 1 | (existing) `records`, `scopes`, `runs`, `state` |
| 2 | `settings` (runtime overrides), `cache_entries` (the cache index) |
| 3 | `stt_providers`, `stt_usage`, `jobs`, `transcripts`, `segments`, and the FTS table `segments_fts` when SQLite supports it |

The old program set `user_version` back to 1 at each start. After a downgrade and a second upgrade the scripts
therefore run again, which is why they must be repeatable. Synced rows have a version of their own
(`COLLECT_VERSION` in `sync.py`, recorded as state `collect_version` after a sync that refreshed every lecture
scope): when the stored one is older, the server asks for one sync at start through the same path as a manual
request, so it waits out the cooldown and does nothing while logins are blocked.

## Where the brief is wrong or unclear

1. **`DELETE /api/v1/videos/{item_id}` now needs an admin token.** The brief says deleting from the cache is an
   admin action; that route did it with any token. No MCP tool used it.
2. **"Pinned" means two things in the brief**: my pin (cache page) and "a video is pinned only until its audio has
   been extracted". They are separate here: `pinned` is yours and lasts until you remove it; `in use` comes from
   the job queue and ends by itself.
3. **Idempotent per (video version, provider)** can't hold for automatic routing, where the provider isn't known
   when the job is made. Without `model`, the key is (video version, automatic), and an existing transcript of
   that version from any provider answers the request.
4. **"Fail with a clear error" versus `paused_budget`.** A single request naming a provider that is over its cap
   is refused with the list of ids that work. A job that was accepted earlier (a batch) and meets the cap when
   its turn comes waits in `paused_budget`. With automatic routing a job pauses only when a cap is the reason no
   provider is left.
5. **The prefetch limit** is implemented as "at most N jobs past `queued`". Paused jobs don't hold a slot, because
   the budget is checked before the download; otherwise two paused ElevenLabs jobs would block a job for another
   provider.
6. **The lecture `transcript` field is worked out when a row is read**, not stored by the sync, so no synced row
   gains a field from this work. The start-up sync still exists (see above) and runs once for a database that
   never recorded a version, because such a database may predate `video`.
7. **`scribe_v3` exists** (released 9 November 2026) and is the default. Two details differ from the brief's
   hints: `tag_audio_events` defaults to **true** at ElevenLabs, so the adapter sends `false` explicitly; and
   `keyterms` are sent as repeated form fields, not JSON.
8. **ElevenLabs cost.** The Creator plan's "110 hours included" is the whole plan allowance expressed in Scribe
   hours: other ElevenLabs use eats into it. There is no post-paid overage on current plans; beyond the allowance
   it is prepaid top-ups at the same $0.20 per hour, and the service pauses at zero. Keyterm prompting costs
   about $0.05 per hour more (their pricing page and API reference disagree: $0.05 versus "20%"). The provider
   config has `included_hours` for this, so estimates show $0 inside the allowance.
9. **The backlog nearly fills the cap.** 98.8 h of Commons video against a 100 h cap leaves 1.2 h for retries,
   `force` re-runs and provider tests in the same billing period.
10. **Silence is billed** by ElevenLabs, and a file over 8 minutes counts as up to 4 concurrent requests there, so
    the seeded provider runs one job at a time.
11. **OpenAI changed since the brief.** `gpt-transcribe` is the current model and returns text without
    timestamps; `whisper-1`, the only OpenAI model with word and segment timestamps, is deprecated and shuts down
    on 26 February 2027. The adapter handles both shapes; the docs page says which to pick.
12. **Chunking and `[mm:ss]`.** Chunks are cut by stream copy, so a cut lands within one audio frame (20 ms) of
    the plan; timestamps are shifted by the planned offset. Markers switch to `[h:mm:ss]` past the first hour.
13. **Links into the video** (`<public_url>/videos/<item_id>#t=<s>`) work only while the video is in the cache.
    With "keep the video after processing" off, or after eviction, `export_video` brings it back.
14. **Attachment eviction has a cost the others don't**: an evicted attachment is downloaded again on its next
    read, which iCampus records as another open. The default cap (2 GB) is far above what a term's attachments
    need, so in practice only a deliberately low cap triggers it.
15. **The 5-second test clip can't be "generated" at run time on every machine**, since that needs ffmpeg built
    with a speech synthesizer. It is generated once (command in `stt/__init__.py`) and bundled as a 13 kB Ogg file.
16. **"Looking at the cache must never send a request to iCampus"** conflicts with `GET /api/v1/files/{id}`, which
    downloads on first use by design. The cache routes and every admin page read through `files.peek()`, which
    only looks at the disk; the files route is unchanged and remains an explicit action.
17. **ffmpeg in the image** comes from Debian (`apt-get install --no-install-recommends ffmpeg`). It adds roughly
    300–400 MB to the image. No Python dependency is heavy: Jinja2 is the only new package (python-multipart was
    already installed for the MCP server).
18. **What could not be checked here.** The acceptance checks that need the real server, real lectures and a paid
    key (a ~10-minute and a ~130-minute lecture through ElevenLabs and a second provider) were not run: this was
    built without access to the deployment. Everything else is covered offline, with real ffmpeg where audio is
    involved and fake provider servers.
