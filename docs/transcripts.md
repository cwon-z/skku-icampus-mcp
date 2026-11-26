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
                 │       ├─ videos.py   the downloader and its queue (part 6)              │
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
  name an `ICAMPUS_*` variable as its key, so a typo can't send the SKKU password to a provider. A stored key
  is dropped when its provider's host or type changes, and an ElevenLabs provider only accepts an
  `elevenlabs.io` address: otherwise whoever can edit a provider could read a "write-only" key by pointing it
  at a server of their own. What remains is that an admin chooses which environment variable goes to which
  OpenAI-compatible address; the admin token is as sensitive as the keys.
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
  `Videos.ensure()`, which puts it in the same download queue exports use (part 6) and waits for the end of its
  next try; (2) audio with ffmpeg, mono 16 kHz Opus at
  24 kbps by default (format and bitrate per provider), one ffmpeg at a time with `-threads 1`; (3) chunks only if
  the provider's upload or duration limit requires them, cut in the middle of the silence nearest the target
  length, timestamps shifted back and joined; (4) transcribe, store, index.
- **Prefetch.** At most `prefetch_limit` (2) jobs are past `queued` at a time, so downloads never run more than
  that far ahead of transcription, and jobs whose video is already cached go first. The budget is checked before
  a job downloads anything.
- **Idempotent.** A unique index allows one active job per (asset, video version, requested model). Asking again
  returns that job, or the transcript if one exists for this version (from any provider when no model is named).
  `force` makes a new transcript once: a forced request repeated within 15 minutes of that transcript gets it
  back, so following a forced job by asking again can't pay for it a second time.
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

### Part 4: admin UI (`admin/`)

Served by the API at `/admin`, because that service owns the data and the settings.

- **Sign-in.** An admin-labelled API token, pasted once. The answer is a cookie holding a random session id and
  its HMAC (key in `var/admin.key`, 0600): `HttpOnly`, `SameSite=Strict`, `Secure` unless
  `ICAMPUS_ADMIN_COOKIE_SECURE=false`, scoped to `/admin`, valid for `ICAMPUS_ADMIN_SESSION_HOURS`. Sessions are
  also recorded in the database, so signing out (or expiry) ends one for every copy of the cookie. A session
  remembers a fingerprint of the token that opened it and ends when that token is rotated out or its label
  stops being an admin one. Five failed sign-ins from an address block it for ten minutes. The cookie opens the UI only; `/api/v1` still wants a bearer
  token, and a bearer token is not a session.
- **CSRF.** Every form carries a token derived from the session (the sign-in form has a signed, 30-minute one of
  its own), htmx sends it as a header, and every route that changes something checks it. The same routes refuse
  requests the browser marks as coming from another site (`Sec-Fetch-Site`, else `Origin`/`Referer`). All
  changes are POSTs; there is no state-changing GET.
- **Rendering.** Jinja2 with autoescape; lecture titles, file names, transcripts and provider error messages are
  other people's text and are only ever inserted as text. The Content-Security-Policy allows no inline script or
  style and no other origin, so the pages have none: htmx (vendored, 2.0.11, eval off), one small `admin.js`, one
  `theme.css`, the icons as inline SVG (`admin/icons.py`) and three font files, all served from the package. Attachments are sent as downloads with
  `nosniff` and a sandbox policy, never rendered.
- **Pages.** Overview, Settings (generated from the registry in `runtime.py`), STT models (providers, keys,
  test, routing), Jobs (the queue, refreshed by an htmx poll; queue a course as plan → confirm), Cache (one table
  over every kind) and a page per lecture with the player beside the transcript. They read the database, the
  cache index and the disk. The buttons that say so (download, re-download, transcribe) start what an export or
  a transcription request starts; nothing in the UI reaches canvas or learningx.
- **Design.** A monochrome system with one accent: paper canvas, white cards, one ink (black) card per page
  for the figures that matter, cobalt for marks only, red only for what needs attention, Geist for text, Geist
  Mono for labels and Galmuri11 pixel numerals for counts. `theme.css` has three sections: the tokens (every
  colour, shadow, radius and font as a custom property; each colour once, as `light-dark(light, dark)`, so the
  theme follows the system or the `data-theme` the sidebar control sets), the components, and the layout. The
  last two use tokens only, which a test checks. Shares are rows of squares or `<progress>` bars and the sign-in
  picture is drawn on a canvas by `admin.js` in the theme's colours, so no element needs an inline style and no
  image is loaded. States are tags in words: solid for what is happening now, outlined for what wants a look,
  red for what needs you. Rows keep their common actions visible and put the rest in a "more" menu.
- **Reaching it from outside the tailnet (not built).** The API should stay private. The route that already
  exists for videos would carry the UI too: the MCP host is the one thing behind Cloudflare Access, and it could
  proxy `/admin/*` to the API the way `mcp_server.video_file` proxies `/videos/<id>`. Three things would have to
  be added for that: the proxy must pass methods, form bodies, `Cookie` and `Set-Cookie` through (today it
  passes `Range` only); the origin check must accept the public host (Access rewrites nothing, but the `Host`
  seen by the API would be the proxy's); and the MCP container would then relay admin sessions, which is more
  than the "holds one API token" role it has now. A second Access application on its own hostname pointing at a
  reverse proxy in front of the API is the alternative that leaves the MCP container alone. Either is a
  decision for later; `tailscale serve` already gives HTTPS inside the tailnet.

### Part 5: slides (`frames.py`)

- **Extraction.** One ffmpeg pass over the key frames only (`-skip_frame nokey`): fast, light on memory, and
  encoders place key frames where the picture changes. Frames whose scene score against the previous key frame
  passes `slides_scene_threshold` (0.1) are written as JPEGs scaled to `slides_width` (960). A second, tiny
  ffmpeg pass turns each into a 16x16 grey thumbnail, its fingerprint; consecutive frames whose fingerprints
  differ by no more than about one line of text are one slide, and the last picture of such a run is kept (the
  slide with all its bullets) with the first one's start time. A slide that returns later is a new slide.
  Output: `var/frames/<item_id>/NNN.jpg` and `index.json` (item, asset, video version, each slide's start and
  end), indexed as cache kind `frames` under `frames_cache_gb`.
- **When.** Inside a transcription job, right after the audio and before the video may leave: a job in
  `extracting` holds its video for exactly that. A failure there is recorded in the job's stages and does not
  fail the transcript. For lectures transcribed earlier, `POST /api/v1/slides/{item_id}` (or the button on the
  lecture's admin page) cuts them on request, downloading the video first if it isn't cached.
- **Reading.** `GET /api/v1/lectures/{item_id}/segments` pairs each slide with the transcript segments that
  began while it was on screen. The MCP tool `get_lecture_segments` returns a few slides per call, each as the
  image itself followed by that text, so the assistant reads the slide rather than an OCR of it.
- **Lecture materials (slide PDFs).** Nothing fetches them. `icampus material-check` reads the modules list the
  sync already reads and prints the shape of the material items; with `--head N` it asks the content server to
  describe N of them, without cookies, and never requests a download address (fetching a material may count
  as opening it). Its report decides what, if anything, is built next.

### Part 6: the video download queue (`videos.py`)

Added after a day on which 5 of 23 queued exports finished. The queue used to be a dict of asyncio tasks behind a
lock: a restart dropped every waiting job and deleted the partial file, a failed job stayed failed until the next
request for it, and that request (`export_video`, which follows a download by asking for it again) started it over
from zero without saying so. From outside that looked like a queue that stalls, resets and moves only when polled.

- **Jobs are rows** in `video_jobs`: one per video that is waiting, downloading or failed, with what the download
  needs from the synced lecture row. A finished job's row is deleted; the cache index is what says a video is here.
- **One worker loop**, started in the app's lifespan like the transcription queue's. It starts what is due when the
  server comes up, when something is asked for or ends, and every 5 seconds, and it records a heartbeat. Jobs run
  in the order they were asked for (`ORDER BY queued_at, id`), `download_concurrency` (1) at a time; a job is
  claimed with `UPDATE ... WHERE state='queued'`.
- **Restarts.** At start every `downloading` row goes back to `queued` in its old place. What had arrived stays in
  `<asset>.part`; the next try asks for the rest (`Range`, with `If-Range` when the first answer had an ETag or a
  date) and continues only if the answer is a 206 from that byte, of the same total size, for the same video
  version, and the part was written during this run of the machine (the kernel's boot id: after a power cut the
  last bytes of a file can't be trusted). Anything else, including the 200 lcms answers with today, takes the
  whole file again, and the status carries a note saying so. A host seen answering a Range request with the
  whole file is remembered while the server runs: after a failed try nothing is kept for it. What waiting jobs do
  keep on disk counts against `video_cache_gb` when the next download makes room.
- **Stalls and retries.** A try ends when the content server sends nothing for 60 seconds (describing the video,
  answering, or in the middle of the file), or after 6 hours. Failures that may pass (those, 5xx and 429, a
  download cut short, a full cache or disk) are tried again after 30 s, 2 min, 5 min, 15 min, 30 min, then every
  hour, `export_retries` (6) times; a job waiting out a pause does not hold up the ones behind it. Then, or at
  once for a failure retrying can't fix, the job is `failed` with the reason.
- **No silent restart.** `POST /videos/{item_id}` on a failed job answers with the failure. Only `retry=true` (the
  admin UI's download button, `export_video(retry=True)`) queues it again, at the back. Two things are not a
  retry and need none: a video whose refresh failed while its copy is still current (the copy is the answer, and
  `retry` only clears the old failure), and one a sync has seen a newer version of since it failed (a new
  download). A failure recorded while refreshing a copy goes when that copy leaves the cache.
- **Transcription jobs** go through the same queue but ask for one try only (`keep_trying` off): their retries
  stay with the transcription queue (`download_retries`), so a job that waits for a retry gives its prefetch slot
  back. If the same video is also exported, the export's retries apply. A job uses a current copy as it is, even
  while a fresh one is on its way, and one whose video changed while its download waited is tried again rather
  than failed.
- **What a status says.** `queue_position` (among the jobs that could start now: one waiting out a pause is not
  ahead of anyone), `attempts`, `last_error`, `retry_at`; while downloading
  `downloaded_mb`, `total_mb` (from the Commons description, then `Content-Length`), `speed_mbps` (megabytes per
  second over the last 20 seconds), `eta_s`, `last_progress_at`, and `stalled` after 15 seconds without data.
  `/api/v1/status` has the whole queue under `exports`, with the worker's heartbeat. The size from the iCampus
  listing is only shown while nothing better is known, as `listed_size_mb_estimate`.
- **Order across calls.** First in, first out is by arrival at the API. Calls sent side by side arrive in no
  particular order, so `POST /api/v1/videos` (MCP: `export_videos`) takes a list and queues it in that order.

## Schema changes

`PRAGMA user_version` now means something: `MIGRATIONS` is a list of steps, one per version, applied in order
at start. A step is a script whose every statement is `IF NOT EXISTS`, or (version 5, which adds columns) a
function that looks at what is there first. Nothing is dropped or rewritten.

| Version | Adds |
|---|---|
| 1 | (existing) `records`, `scopes`, `runs`, `state` |
| 2 | `settings` (runtime overrides), `cache_entries` (the cache index) |
| 3 | `stt_providers`, `stt_usage`, `jobs`, `transcripts`, `segments`, and the FTS table `segments_fts` when SQLite supports it |
| 4 | `video_jobs` (the video download queue) |
| 5 | `video_jobs.fetcher`, `lease_id`, `lease_expires_at`, `lease_info`: a download leased to a remote fetcher ([fetchers.md](fetchers.md)) |

Slides need no schema change: their index is a cache entry of kind `frames`, and each directory's `index.json`
says what it belongs to.

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
    with a speech synthesizer. It is generated once (command in `stt/__init__.py`) and bundled as a 14 kB Ogg
    file of synthesized speech.
16. **"Looking at the cache must never send a request to iCampus"** conflicts with `GET /api/v1/files/{id}`, which
    downloads on first use by design. The cache routes and every admin page read through `files.peek()`, which
    only looks at the disk; the files route is unchanged and remains an explicit action.
17. **ffmpeg in the image** comes from Debian (`apt-get install --no-install-recommends ffmpeg`), which adds a
    few hundred MB of codec libraries. No Python dependency is heavy: Jinja2 is the only new package
    (python-multipart was already installed for the MCP server and is now named directly).
18. **"Violet for primary actions" is not what Nocturne says.** The design system's own rule is "violet points,
    blue measures, white acts": the primary button is the inverse (white on dark) one, and the violet button is
    kept for brand moments. The UI follows the design system: violet marks where you are (navigation, the line
    being spoken, focus, switches), blue is the meters, and the one primary action per view is the inverse
    button. Swapping `nc-btn--primary` for `nc-btn--accent` in the templates makes primary actions violet.
19. **"Dark first"** is implemented the way Nocturne prescribes: dark is the default, and a browser that asks for
    a light scheme gets the light companion unless you pick one with the toggle.
20. **The batch route's `kind` filter** can only ever select videos, since nothing else can be transcribed; it
    is accepted and defaults to `video`.
21. **`Origin: null`.** With a strict referrer policy Chrome sends `Origin: null` on same-site form posts, which
    a naive origin check refuses (found by driving the UI in a real browser). The check trusts `Sec-Fetch-Site`
    when the browser sends it, and the pages use `Referrer-Policy: same-origin`.
22. **A chunked provider and retries.** Pieces already transcribed are remembered while the server runs, so a
    retry after a failure in piece 5 doesn't pay for pieces 1 to 4 again. After a restart they are sent again.
23. **An independent review of this branch found 15 defects, all fixed** before merge, each with a test. The
    serious one: a forced transcription that was followed by asking again (as the tool told the assistant to)
    started, and paid for, a new transcription every time the last one finished. The others: a course filter
    applied after the search's row limit; the all-providers cap not counting running jobs; a used-up cap still
    letting lectures of unknown length download; an error message shortened before the key was cut out of it;
    sessions outliving their token; child processes (ffmpeg, the attachment reader) inheriting provider keys
    from the environment; and smaller ones.
24. **"Perceptual-hash de-duplication" is done on thumbnails, not bit hashes.** A difference hash merged two
    different test pictures made of horizontal bands, gives sparse slides (a title on white) only a handful of
    bits to differ in, and can't tell a white page from a black one. Comparing 16x16 grey thumbnails by mean
    difference has none of these problems and needs no image library either. The thresholds (scene score 0.1,
    mean difference 2.5) are reasoned, not measured: no real lecture was available to tune them on.
25. **Slide times are as fine as the video's key frames**, typically a few seconds, because only key frames are
    decoded. Decoding every frame of a two-hour video would take many CPU-minutes for little gain.
26. **A camera recording has no slides.** Its picture never settles, so the grouping is loosened and then
    thinned to at most 400 frames; those are stills of the lecturer, not slides. `slides_enabled` turns the
    whole step off.
27. **The material check could not be run here**: it needs the saved iCampus login. The command is in this
    change; its report (which content types materials have, whether they name a Commons asset, what Commons
    says about one) is the thing to send back before anything fetches a material.
28. **What could not be checked here.** The acceptance checks that need the real server, real lectures and a paid
    key (a ~10-minute and a ~130-minute lecture through ElevenLabs and a second provider) were not run: this was
    built without access to the deployment or any provider key. The Docker image was not built either (no
    Docker daemon in the build environment), so the ffmpeg layer is untested as an image. Everything else is
    covered offline: fake provider servers, real ffmpeg on generated audio (extract, silences, cuts, an
    end-to-end job), and the admin UI driven in headless Chromium.
