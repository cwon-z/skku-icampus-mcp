# skku-icampus

Keeps a copy of your own SKKU iCampus data and serves it to other programs over a REST API, and to AI
assistants over MCP. That covers tasks and deadlines, assignment submission status, announcements,
lectures and attendance, grades, the files attached to announcements and assignments, lecture videos, and
timestamped, searchable transcripts of those videos.

```
icampus          (FastAPI :9013)  logs in by itself, syncs 4×/day, stores everything in SQLite;
                                  transcribes lectures in the background; admin UI at /admin
icampus-mcp      (:8724/mcp)      MCP tools; only talks to the API, never sees your password or any key
icampus-fetcher  (optional)       on a laptop: downloads lecture videos for the server and uploads them
```

Unofficial, not affiliated with Sungkyunkwan University. It only reads your own account; follow your
university's rules when you use it.

## How it works

- **Login.** A headless Chromium signs in through the normal SSO page, but only when the saved
  session has expired. Everything after that is plain GET requests: Canvas `/api/v1` and the
  LearningX JSON API that My Page, 강의콘텐츠 and 출결현황 use themselves
  ([notes](docs/icampus-notes.md)).
- **Read-only.** The browser works from an allowlist of pages and never loads item pages, because
  opening a lecture item in iCampus marks it complete. It never submits anything, starts quizzes or
  marks anything read.
- **Attachments, on request only.** A sync records which files an announcement or assignment has (names,
  sizes, IDs), never the files. A file is downloaded only when someone asks for it, with the saved login,
  and only if a synced announcement or assignment points at it; lecture items are never opened. iCampus
  records a download as you opening the file, and if a linked file also sits in a course module, as viewing
  it there. Copies are kept in `var/files` and reused until a sync sees a new version (for 12 hours when
  iCampus gives none). A child process with a memory cap and a time limit reads the text out of them.
- **Lecture videos, on request only.** A video hosted on SKKU's content server (`lcms.skku.edu`) is
  downloaded when someone exports it, without your login: the content server serves it to any request that
  comes from iCampus, so nothing ties the download to your account, and no viewer, progress or attendance is
  involved. Videos stay in `var/videos` (up to `ICAMPUS_VIDEO_CACHE_GB`, 30 by default; the least recently
  used go first) and are reused until a sync sees a new version. YouTube and Zoom lectures aren't covered.
  Lecture videos are copyrighted: keep them for your own study.
- **A download queue that runs by itself.** Exporting a video adds it to a queue in SQLite and returns. A worker
  that starts with the server downloads the queue in the order the videos were asked for, one at a time
  (`download_concurrency`), whether or not anyone asks again. A restart keeps the queue and what had arrived of
  the video it interrupted; a try that stalls (no data for a minute) or fails in a way that may pass is made
  again after 30 seconds, then with longer pauses (`export_retries`, 6), and after that the export is `failed`
  with the reason. A failed export is never started over unasked: asking again reports the failure until you
  ask with `retry`. `/api/v1/status` shows the queue and the worker's heartbeat.
- **Other machines can do the downloading.** See [Remote fetchers](#remote-fetchers).
- **Transcripts, on request or by the batch.** A job downloads the video (as above), cuts out the sound with
  ffmpeg and sends it to a speech-to-text provider: ElevenLabs Scribe, or any server with OpenAI's
  `/audio/transcriptions` route (OpenAI, Groq, a self-hosted Whisper). Several providers can be registered; a
  request names one or lets the routing rules pick, with fallback when one fails. Each has a monthly cap, and a
  job that would pass it waits for the next period. The queue lives in SQLite and survives restarts.
  Transcripts are kept as JSON under `var/transcripts` and indexed for search (Korean included). They are
  lecture content like the videos: private, for your own study. See [docs/transcripts.md](docs/transcripts.md)
  and [docs/stt-providers.md](docs/stt-providers.md).
- **Slides.** While a lecture's video is on the server for transcription, ffmpeg also saves a picture of each
  slide (the key frames where the picture changes, near-identical ones folded together) with the time it was on
  screen. Each slide can then be read together with what was said during it.
- **One cache, with caps.** Videos, extracted audio, attachments and transcripts are indexed together. Each kind
  has a cap and so does the total; past a cap the least recently used go first, but never something you pinned,
  something a queued job still needs, or a transcript.
- **Careful with logins.** At most 3 password logins a day. A rejected password, a locked account
  or a password-expiry notice stops automatic logins until you retry with an admin token
  (`POST /api/v1/sync?retry_login=true`).
- **Schedule.** Syncs run at 07:30, 12:30, 18:00 and 22:30 KST (±10 min), plus on request.
  They show up as normal account activity in Canvas.
- **Failures.** If part of a sync fails, the old data for that part is kept and its `synced_at`
  stays at the last good sync. `stale` turns true once that is older than 12 hours
  (`ICAMPUS_STALE_HOURS`); from then on `/api/v1/status` (and the Uptime Kuma push) reports unhealthy
  and names the datasets left behind, and a task status resting on that data says it is outdated.
  `/api/v1/status` shows per-run errors. An item that drops off the iCampus to-do list is not marked "done".

## Run it locally

```bash
uv sync
uv run playwright install chromium
cp .env.example .env && chmod 600 .env     # your 킹고ID + tokens (openssl rand -hex 24)
uv run icampus sync --headed               # one sync, watching the browser
uv run icampus serve                       # API on 127.0.0.1:9013 (+ the schedule)
uv run pytest
```

Swagger UI: http://127.0.0.1:9013/docs. `icampus probe` is a one-off read-only check of what the
iCampus pages load. It writes its report to `var/probe/`, which is gitignored and holds personal data.
`icampus bench` measures how fast the content server sends videos to this machine, at 1, 2 and 3 connections,
and keeps nothing ([docs/fetchers.md](docs/fetchers.md) says how to read it; it needs the server running).
`icampus video-check` and `icampus material-check` are one-off read-only checks too: they print the shape of
the lecture video and lecture material items (no titles, no IDs) and, with `--head N`, what SKKU's content server
says about N of them. `material-check` never requests a file: whether lecture materials may be fetched at all is
an open question that its report is meant to answer.

## REST API

Send `Authorization: Bearer <token>` on every call. Tokens come from `ICAMPUS_API_TOKENS`
(`label:token,...`). List responses look like `{synced_at, stale, count, items}`; when a list is cut
short they add `total` and `truncated`. `course` accepts an ID, a course code or part of a name.
The routes under `/api/v1/fetchers` take the tokens of `ICAMPUS_FETCHER_TOKENS` instead; those tokens get 403
everywhere else.

| Endpoint | Notes |
|---|---|
| `GET /health` | no auth |
| `GET /api/v1/status` | last runs, freshness per dataset, login state, next run; `exports`: the video download queue (queued, downloading, failed, ready, what is active and who has it, what failed last), its worker's heartbeat, and `fetchers` (name, `token`, platform, version, `last_seen`, `online`, `active_jobs`, `speed_mbps`, `resting_until`) |
| `POST /api/v1/sync` | Returns 202 started, 409 running, 429 cooldown or 423 blocked. `?retry_login=true` needs an `admin` token |
| `GET /api/v1/courses` | codes, instructors, current grade |
| `GET /api/v1/tasks` | merged to-do list: `course, kind, status, due_within_days=14, past_days=0, include_done, include_inactive, limit` |
| `GET /api/v1/assignments` | `course, unsubmitted_only`. `/{id}` adds the description and task status |
| `GET /api/v1/announcements` | `course, since_days=30, unread_only, limit`. `/{id}` gives the full text |
| `GET /api/v1/files/{id}` | an announcement or assignment attachment and its text: `offset, max_chars=20000`. Downloads it on first use |
| `GET /api/v1/files/{id}/content` | the file itself |
| `GET /api/v1/lectures` | `course, week, kind, incomplete_only, available_only, upcoming_only, include_attendance, limit`. Hosted videos carry `transcript` (`state`: none, queued, …, ready, outdated; `model`) |
| `GET /api/v1/videos` | lecture videos kept on the server, and how full the cache is; `queue`: every download that hasn't finished, in the order they run |
| `POST /api/v1/videos/{item_id}` | put a lecture video in the download queue unless it's cached (202 while it waits or downloads, 200 otherwise). Asking again only reads the status; `?retry=true` queues a failed one again |
| `POST /api/v1/videos` | the same for several, `{"item_ids": [...], "retry": false}`: they join the queue in the order given |
| `GET /api/v1/videos/{item_id}` | `state`: not_downloaded, queued (`queue_position`; after a failed try `attempts`, `last_error`, `retry_at`), downloading (`by`: `server` or a fetcher's name, `phase`: downloading or uploading, `downloaded_mb`, `total_mb`, `uploaded_mb`, `speed_mbps` in MB per second and `eta_s` for that phase, `last_progress_at`, `stalled`), ready (`size_mb`), outdated or failed (`error`, `attempts`, `retryable`) |
| `GET /api/v1/videos/{item_id}/content` | the video file; supports Range, so players can seek. `DELETE /api/v1/videos/{item_id}` drops the copy and takes its download out of the queue (admin token) |
| `GET /api/v1/lectures/stats` | per course and in total: videos, hours, exportable hours and GB, transcribed hours, cost of the rest per provider |
| `GET /api/v1/stt/models` | the speech-to-text providers: description, cost per hour, word timestamps, languages, health, cap left |
| `POST /api/v1/transcripts/{item_id}` | transcribe a lecture: `model, language, force, estimate_only`. 202 with the job, 200 when a transcript exists, 409 with `working_models` when the named model can't run |
| `GET /api/v1/transcripts/{item_id}` | `model, format=text\|json\|vtt\|srt, offset, max_chars=15000, start_s, end_s`. `PUT .../preferred?model=` pins which transcript is read |
| `POST /api/v1/transcripts/batch` | `course, week, kind, model, only_missing`. A dry run (the plan) unless `confirm=true` |
| `GET /api/v1/transcripts/status` | `item_id` for one lecture; otherwise the queue, use per provider and open jobs (`course`) |
| `GET /api/v1/slides/{item_id}` | the slide frames of a lecture: `state` and each slide's time. `POST` cuts them now (downloads the video if needed); `/{n}.jpg` is one frame |
| `GET /api/v1/lectures/{item_id}/segments` | slide by slide: each frame with what was said while it was on screen. `model, offset, limit=4, max_chars` |
| `GET /api/v1/jobs` | `state, course, item_id, limit`. `POST /api/v1/jobs/{id}/retry`, `DELETE /api/v1/jobs/{id}` (cancel) |
| `GET /api/v1/search` | `q, course, week, limit`: where something was said, with the moment and the sentences around it |
| `GET /api/v1/cache` | everything on disk: `kind, course, pinned, sort, order, limit`, with totals against caps. `POST\|DELETE /api/v1/cache/{kind}/{key}/pin`; `DELETE /api/v1/cache/{kind}/{key}` needs an admin token |
| `GET\|PUT /api/v1/settings` | runtime settings (admin token): values, where each comes from, limits |
| `/api/v1/stt/providers` | manage providers (admin token): `GET`, `POST`, `PUT /{id}`, `DELETE /{id}`, `PUT\|DELETE /{id}/key` (write-only), `POST /{id}/test` |
| `GET /api/v1/grades` | `course` |
| `GET /api/v1/export` | everything as one JSON document |
| `POST /api/v1/fetchers/lease` | fetcher token. `{name, version, platform, slots_free, free_bytes}`: the next due download as `{job_id, lease_id, asset_id, host, title, expected_bytes, max_bytes, lease_ttl_s}`, or 204 |
| `POST /api/v1/fetchers/jobs/{job_id}/progress` | fetcher token. `{lease_id, phase, done_bytes, total_bytes, speed_bps}` renews the lease; answers `{continue}` |
| `PUT /api/v1/fetchers/jobs/{job_id}/file` | fetcher token. A piece of the upload (`Content-Range`, `X-Lease-Id`, `X-File-SHA256` on the last); `GET` says how many bytes the server has |
| `POST /api/v1/fetchers/jobs/{job_id}/fail` | fetcher token. `{lease_id, message, transient}`: the queue's retry policy takes it from there |
| `GET /api/v1/fetchers/bench-targets` | fetcher or API token. A few `{asset_id, host}` for `bench` |

Any token may read, queue transcriptions and cancel jobs: queueing can cost money, but only inside each
provider's cap. Changing settings or providers, testing a provider and deleting from the cache (including
`DELETE /api/v1/videos/{item_id}`) need a token whose label is in `ICAMPUS_ADMIN_LABELS`. No route returns an
API key.

A few fields to read carefully:
- `status` sums up a task or lecture item: `todo`, `done`, `missed` (deadline and any late period passed),
  `upcoming` (not open yet) or `unknown` (iCampus can't tell, e.g. handed in on paper or through an external
  tool). Canvas decides for what you hand in, LearningX for what you watch or read. `status_reason` says
  which, and `status_checked_at` when that source last confirmed it.
- `in_remaining_list` is what My Page shows. It does not mean you haven't submitted.
- `completed` and `submission` are only filled in when iCampus reported them. `null` means unknown.
- `attendance` is one of `attendance` (present), `late`, `absent` or `none` (not decided yet).
- Attachment text comes out of PDF, Word, PowerPoint, Excel, HWPX, notebooks and text files. `extract` says
  when there is none: a picture, a scanned PDF, or the binary HWP format.

## MCP

Tools: `sync_status`, `list_courses`, `list_tasks`, `list_announcements`, `read_announcement`,
`read_assignment`, `read_attachment`, `list_lectures`, `export_video`, `export_videos`, `get_grades`, `refresh`. All
are read-only except `refresh`, which asks for a sync; `read_attachment`, whose first read of a file downloads it
from iCampus (so clients may ask before running it) and which returns text, or the picture itself for images; and
`export_video`, which puts a lecture video (`exportable` in `list_lectures`) in the server's download queue. The
server works through that queue by itself; calling the tool again only reads the progress (place in the queue,
megabytes, speed, ETA, who is downloading it, the last error) and never restarts anything: a failed export says why and stays failed until
`retry=True`. `export_videos` does the same for a list, which joins the queue in the order given, and `sync_status`
shows the whole queue under `exports`. When a video is ready the tool returns a link,
`<ICAMPUS_MCP_PUBLIC_URL>/videos/<item_id>`. The MCP server streams the file from the API behind the same auth as
`/mcp`, so over Cloudflare Access the link opens in a signed-in browser. Without `ICAMPUS_MCP_PUBLIC_URL` the link is
the API address (bearer token needed). The size `list_lectures` shows (`video_mb_estimate`) is what iCampus lists;
the file itself can be several times smaller, and an export reports the real size as soon as it knows it.

Transcript tools:

| Tool | What it does |
|---|---|
| `list_stt_models` | the registered models with description, cost per hour, word timestamps, languages, health and cap left |
| `transcribe_lecture(item_id, model, language, force, estimate_only)` | queue one lecture; call again to follow it. Not read-only: it downloads and may cost money. Leave `model` out for automatic routing |
| `queue_transcriptions(course, week, model, only_missing, confirm)` | many lectures. Returns the plan (count, hours, cost per model, cap left) unless `confirm=True` |
| `transcription_status(item_id, course)` | one lecture's state, or the queue |
| `get_transcript(item_id, model, start_s, end_s, offset)` | the text with `[mm:ss]` markers, about 15,000 characters per call, and a link pattern into the video (`…/videos/<item_id>#t=<seconds>`) |
| `get_lecture_segments(item_id, offset)` | a lecture slide by slide: each slide's picture (as an image) with what was said while it was on screen, a few slides per call |
| `search_transcripts(query, course, week, limit)` | where something was said: title, course, time, the sentence, a link to that moment |
| `lecture_stats(course)` | totals per course, so "how many hours of lectures are there" doesn't need the whole list |

The links into a video work while the video is on the server; `export_video` brings it back. The server's
instructions tell the assistant that transcripts are machine-made, private and for your own study.

```bash
# over HTTP (icampus-mcp --http), with a token from ICAMPUS_MCP_TOKENS
claude mcp add --transport http icampus http://<server>:8724/mcp --header "Authorization: Bearer <token>"

# or locally over stdio against the API
claude mcp add icampus --env ICAMPUS_MCP_API_URL=http://<server>:9013 \
  --env ICAMPUS_MCP_API_TOKEN=<api token> -- uv run --directory "$PWD" icampus-mcp
```

Over HTTP, a request gets in with either a bearer token from `ICAMPUS_MCP_TOKENS` or a Cloudflare
Access JWT. Without either one configured, the server won't start.

The Access route is how claude.ai and the mobile apps reach it:
1. Put a public hostname behind an Access app with Managed OAuth, and proxy it to `:8724`.
2. Set these variables:
   - `ICAMPUS_MCP_ACCESS_TEAM`, for example `myteam.cloudflareaccess.com`;
   - `ICAMPUS_MCP_ACCESS_AUD`, the app's AUD tag or tags, comma-separated;
   - `ICAMPUS_MCP_ACCESS_EMAILS`, the emails allowed in;
   - `ICAMPUS_MCP_ALLOWED_HOSTS`, which must include the public hostname.

## Remote fetchers

Downloads from SKKU's content server are slow from some networks (about a megabyte a second from the author's
homelab). A fetcher is a small program on another machine of yours, a MacBook or a Windows laptop, that takes
jobs from the server's download queue, downloads each video where it is, and uploads the file. The server stays
the only place where videos are kept, transcribed and served, and with no fetcher configured nothing changes.

```bash
# on the server: one token per machine, in .env, then docker compose up -d
ICAMPUS_FETCHER_TOKENS=macbook:<token>
```

```bash
# on the laptop, with Tailscale connected
uv tool install git+https://github.com/cwon-z/skku-icampus-mcp
```

```bash
icampus-fetcher check    # server, token, content server, disk, work directory
```

```bash
icampus-fetcher bench    # is this machine faster than the server? (run `icampus bench` there to compare)
```

```bash
icampus-fetcher run      # takes jobs until Ctrl+C
```

The fetcher reads `ICAMPUS_FETCHER_SERVER`, `ICAMPUS_FETCHER_TOKEN`, `ICAMPUS_FETCHER_NAME`,
`ICAMPUS_FETCHER_SLOTS` (1 to 4), `ICAMPUS_FETCHER_DIR`, `ICAMPUS_FETCHER_MAX_DISK_GB` and
`ICAMPUS_FETCHER_KEEP_AWAKE` from the environment, a `.env` beside where it runs, or flags.

- A fetcher token opens the fetcher routes and nothing else, and a fetcher is who its token says it is. It gets
  no SKKU login, no session, no speech-to-text key and no access to the data volume: an asset id and a host are
  all it needs.
- A video lies on the laptop only while it is downloaded and uploaded, in the per-user cache directory, named by
  its asset id, never in a folder that iCloud, OneDrive or Dropbox copies.
- A job is a lease that heartbeats keep. Closing the laptop puts the job back in the queue within about two
  minutes; Ctrl+C does at once. The server checks an upload's size, SHA-256 and that it is an MP4 before it
  becomes a cached video. A fetcher whose tries keep failing is given no work for a while, so it can't use up
  the queue's retries.
- `local_downloads` (Settings, Storage) says whether the server also downloads: `on`, `fallback` (only while no
  fetcher is around) or `off`.
- The API stays on its private address: a fetcher reaches it over your tailnet, and nothing public was added.

[docs/fetchers.md](docs/fetchers.md) has the whole setup, how to read `bench`, starting it at login on macOS
and Windows, and what hasn't been checked on a real Mac or Windows machine yet.

## Admin UI

`http://<api address>:9013/admin` shows what the server has and lets you change how it runs: an overview (sync
status, storage against caps, the queue, speech-to-text use this month), every runtime setting, the STT models
(add, edit, test, routing), the job queue (retry, cancel, queue a course after a dry run; with fetchers, who they
are and what is being downloaded by whom), and one table of
everything on disk (open, pin, delete, re-download, transcribe). A lecture's page plays the video beside its
transcript; clicking a line seeks to it.

- **Sign in** by pasting an admin-labelled API token once. You get a session cookie that is signed, expires after
  `ICAMPUS_ADMIN_SESSION_HOURS` (12), and is HttpOnly, SameSite=Strict and Secure. Every form carries a CSRF
  token. Signing out ends the session on the server.
- **HTTPS.** The cookie is only sent over HTTPS. On a tailnet, `tailscale serve --bg --https=443 9013` on the
  host gives the API (and the UI) an HTTPS address with a real certificate, reachable from your tailnet only.
  To use plain HTTP on a tailnet address instead, set `ICAMPUS_ADMIN_COOKIE_SECURE=false`.
- **It is reachable wherever the API is**, so keep the API on a private address, as before.
  `ICAMPUS_ADMIN_UI=false` turns the UI off.
- **From outside the tailnet** it is not exposed, on purpose. If you want that later, the route would be the one
  videos already take: the MCP host, which sits behind Cloudflare Access, proxying `/admin` to the API the way it
  proxies `/videos`. That would need the proxy to pass cookies and form posts through and the UI to accept the
  public origin; it is described in [docs/transcripts.md](docs/transcripts.md) and not built.
- Looking at any page reads the database and the disk only. Nothing in the UI sends a request to iCampus; the
  buttons that download a video or transcribe a lecture reach SKKU's content server without your login, like
  an export.
- The look: paper-coloured pages, white cards, one black card per page for the numbers that matter, a single
  blue for marks, red only for what needs you, and pixel numerals for counts. Light and dark follow the system;
  the control at the foot of the sidebar picks one. Every colour, shadow, radius and font is a CSS custom
  property in the first section of `src/icampus/admin/static/theme.css`, each colour written once as
  `light-dark(light, dark)`: change the values there and the whole UI follows. The fonts (Geist, Geist Mono and
  Galmuri11, all SIL OFL, licences beside them), the icons and htmx are served from the package, so the UI makes
  no outside requests. It needs a browser from 2024 or later (`light-dark()`).

## Settings

Everything under *Settings* in the admin UI can also be set as `ICAMPUS_<NAME>` in the environment (a value saved
in the UI wins, and the page shows which is in force) or through `PUT /api/v1/settings`. The groups:

| Group | Settings |
|---|---|
| Storage | `video_cache_gb` (30), `video_max_gb` (4), `download_concurrency` (1), `export_retries` (6), `local_downloads` (on), `attachment_cache_gb` (2), `attachment_reuse_hours` (12), `attachment_max_mb` (30), `audio_cache_gb` (5), `frames_cache_gb` (5), `cache_total_gb` (40) |
| Sync | `sync_times`, `sync_jitter_min` (10), `manual_cooldown_min` (10), `stale_hours` (12) |
| Transcription jobs | `prefetch_limit` (2), `keep_video_after_processing` (on), `download_retries` (3), `ffmpeg_threads` (1), `slides_enabled` (on), `slides_scene_threshold` (0.1), `slides_width` (960), `stt_auto_transcribe` (off), `stt_auto_courses`, `stt_auto_retranscribe` (off) |
| Speech-to-text | `stt_default_provider`, `stt_fallback_order`, `stt_routing_rules`, `stt_default_language`, `stt_course_languages`, `stt_course_terms`, `stt_min_timestamps` (segment), `stt_chunk_minutes` (20), `stt_audio_bitrate_kbps` (24), `stt_fail_threshold` (3), `stt_skip_minutes` (15) |
| Budget | `stt_monthly_usd_cap` (0 = none), `stt_budget_reset_day` (1); each provider has its own hour and USD caps |

Tokens (`ICAMPUS_API_TOKENS`, `ICAMPUS_FETCHER_TOKENS`), the SKKU login, login limits, the listen address and the
admin cookie settings stay in the environment and are only shown, never changed, from the UI.

## Deploy with Docker

```bash
cp .env.example .env && chmod 600 .env    # fill it in
docker compose up -d --build              # ports on 127.0.0.1 only
```

To expose the ports on a server, copy `compose.homelab.example.yaml` to `compose.homelab.yaml`
(gitignored), put in your addresses, and add `COMPOSE_FILE=compose.yaml:compose.homelab.yaml` to
`.env`. Keep the API off the public internet, and put only the MCP port behind Cloudflare Access.
The MCP container never gets your SSO password, a speech-to-text key, a fetcher token or the data volume.

Upgrading an existing install needs nothing by hand: the database migrates in place, videos and attachments
already on disk are indexed instead of downloaded again, and if the stored lecture rows come from an older
version one sync runs at start (through the usual cooldown and login guard). The image now includes ffmpeg.
Video downloads that were waiting when an older version stopped are not carried over (it kept them in memory):
export them once more, and from then on the queue survives restarts.
For transcripts, put `ELEVENLABS_API_KEY=...` in `.env` (or register another provider), then open `/admin/stt`
and press *Test*. Fetchers are off until `ICAMPUS_FETCHER_TOKENS` is set; until then the server downloads every
video itself, as before.

## License

MIT — see [LICENSE](LICENSE).

## Repo

| Path | What |
|---|---|
| `src/icampus/` | the program (`browser.py` login + allowlist, `collect.py`, `sync.py`, `views.py` task status, `files.py` + `extract.py` attachments, `commons.py` the one client for SKKU's content server, `videos.py` lecture videos and their download queue, `video_check.py` the check behind them, `api.py`, `mcp_server.py`) |
| `src/icampus/` (fetchers) | `fetcher.py` the `icampus-fetcher` command, `machine.py` what differs per operating system, `bench.py`, `api_fetchers.py` the routes a fetcher calls |
| `src/icampus/` (transcripts) | `cache.py` the cache manager, `runtime.py` the settings registry, `audio.py` ffmpeg, `frames.py` slide frames, `stt/` providers and their registry, `pipeline.py` the job queue, `transcripts.py` storage and search, `api_transcripts.py` |
| `src/icampus/admin/` | the admin UI: `__init__.py` (sessions, CSRF, pages), `templates/`, `static/` (theme, htmx, fonts) |
| `docs/icampus-notes.md` | how iCampus behaves: login flow, endpoints, side effects |
| `docs/fetchers.md` | remote fetchers: setup, `bench`, running at login, the protocol, what is still unmeasured |
| `docs/transcripts.md` | the design of the transcript pipeline, cache and admin UI, the schema versions, and what the brief got wrong |
| `docs/stt-providers.md` | provider configs: ElevenLabs, OpenAI, Groq, a self-hosted Whisper server |
| `tests/` | offline tests with made-up data and fake servers; one runs a local Chromium against a fake login page, a few run ffmpeg on generated audio (skipped without it) |

`var/` (the database, saved session, downloaded attachments and videos, audio, transcripts, stored provider keys,
probe output), `.env` and `private/` hold personal data and are gitignored.
