# skku-icampus

Keeps a copy of your own SKKU iCampus data and serves it to other programs over a REST API, and to AI
assistants over MCP. That covers tasks and deadlines, assignment submission status, announcements,
lectures and attendance, and grades.

```
icampus      (FastAPI :9013)  logs in by itself, syncs 4×/day, stores everything in SQLite
icampus-mcp  (:8724/mcp)      MCP tools; only talks to the API, never sees your password
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
- **Careful with logins.** At most 3 password logins a day. A rejected password, a locked account
  or a password-expiry notice stops automatic logins until you retry with an admin token
  (`POST /api/v1/sync?retry_login=true`).
- **Schedule.** Syncs run at 07:30, 12:30, 18:00 and 22:30 KST (±10 min), plus on request.
  They show up as normal account activity in Canvas.
- **Failures.** If part of a sync fails, the old data for that part is kept and its `synced_at`
  stays at the last good sync. `stale` turns true once that is older than 12 hours
  (`ICAMPUS_STALE_HOURS`), and `/api/v1/status` shows per-run errors. An item that drops off the
  iCampus to-do list is not marked "done".

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

## REST API

Send `Authorization: Bearer <token>` on every call. Tokens come from `ICAMPUS_API_TOKENS`
(`label:token,...`). List responses look like `{synced_at, stale, count, items}`; when a list is cut
short they add `total` and `truncated`. `course` accepts an ID, a course code or part of a name.

| Endpoint | Notes |
|---|---|
| `GET /health` | no auth |
| `GET /api/v1/status` | last runs, freshness per dataset, login state, next run |
| `POST /api/v1/sync` | Returns 202 started, 409 running, 429 cooldown or 423 blocked. `?retry_login=true` needs an `admin` token |
| `GET /api/v1/courses` | codes, instructors, current grade |
| `GET /api/v1/tasks` | merged to-do list: `course, kind, due_within_days=14, past_days=0, include_done, include_inactive, limit` |
| `GET /api/v1/assignments` | `course, unsubmitted_only` |
| `GET /api/v1/announcements` | `course, since_days=30, unread_only, limit`. `/{id}` gives the full text |
| `GET /api/v1/lectures` | `course, week, kind, incomplete_only, available_only, upcoming_only, include_attendance, limit` |
| `GET /api/v1/grades` | `course` |
| `GET /api/v1/export` | everything as one JSON document |

A few fields to read carefully:
- `in_remaining_list` is what My Page shows. It does not mean you haven't submitted.
- `completed` and `submission` are only filled in when iCampus reported them. `null` means unknown.
- `attendance` is one of `attendance` (present), `late`, `absent` or `none` (not decided yet).

## MCP

Tools: `sync_status`, `list_courses`, `list_tasks`, `list_announcements`, `read_announcement`,
`list_lectures`, `get_grades`, `refresh`. All are read-only except `refresh`, which asks for a sync.

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

## Deploy with Docker

```bash
cp .env.example .env && chmod 600 .env    # fill it in
docker compose up -d --build              # ports on 127.0.0.1 only
```

To expose the ports on a server, copy `compose.homelab.example.yaml` to `compose.homelab.yaml`
(gitignored), put in your addresses, and add `COMPOSE_FILE=compose.yaml:compose.homelab.yaml` to
`.env`. Keep the API off the public internet, and put only the MCP port behind Cloudflare Access.
The MCP container never gets your SSO password.

## License

MIT — see [LICENSE](LICENSE).

## Repo

| Path | What |
|---|---|
| `src/icampus/` | the program (`browser.py` login + allowlist, `collect.py`, `sync.py`, `api.py`, `mcp_server.py`) |
| `docs/icampus-notes.md` | how iCampus behaves: login flow, endpoints, side effects |
| `tests/` | offline tests; one runs a local Chromium against a fake login page |

`var/` (the database, saved session, probe output), `.env` and `private/` hold personal data and are
gitignored.
