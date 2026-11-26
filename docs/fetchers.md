# Remote fetchers

A fetcher is a small program (`icampus-fetcher`) that runs on another machine of yours, such as a MacBook or a
Windows laptop. It takes download jobs from the server's queue, downloads each lecture video from SKKU's content
server where it is, and uploads the file to the server.

Why: from the homelab the content server sends about a megabyte a second, so a 1.9 GB lecture takes about an hour
(see [icampus-notes.md](icampus-notes.md)). From other machines and networks it may be faster. `bench` (below)
tells you whether it is before you set anything up.

The server stays the only place where videos are kept, transcribed and served. Exports, the cache, transcripts
and the MCP tools work as before; a status now also says who is downloading. With no fetcher token configured,
nothing changes at all.

## What a fetcher has, and what it doesn't

- One token, which opens the routes under `/api/v1/fetchers` and nothing else. It can't read a course, a file or
  a transcript, and no other token opens those routes.
- No SKKU login, no iCampus session, no speech-to-text key, no access to the server's data volume. The content
  server needs no login, so the fetcher is given none. From the server it learns an asset id, a host on
  `*.skku.edu`, a title for its status line, and sizes.
- Its requests to the content server are made by `commons.py`, the same code the server downloads with: no
  cookies, and only `*.skku.edu`. The download's redirect address (with its query string) is never stored,
  logged, or sent anywhere, the server included.
- A video is lecture content. On the fetcher it lies in a private per-user directory, named by its asset id
  only, for as long as the upload takes. It is deleted as soon as the server confirms the upload, and on every
  other way out. `run` refuses a work directory inside a folder that iCloud Drive, OneDrive, Dropbox or Google
  Drive copies.
- The API stays on its private (tailnet) address. Nothing public was added for fetchers: the machine must be on
  your tailnet.

## Set up

### 1. A token, on the server

Make one token per machine and add them to the server's `.env`:

```bash
openssl rand -hex 24
```

```
ICAMPUS_FETCHER_TOKENS=macbook:<token>,thinkpad:<token>
```

Then `docker compose up -d`. A fetcher is who its token says it is: the name before the colon is what the server
knows the machine by (how many jobs it holds, whose lease a call may speak for), so give each machine its own
token. A token can't be in `ICAMPUS_API_TOKENS` as well.

### 2. Install, on the laptop

Install [uv](https://docs.astral.sh/uv/), then:

```bash
uv tool install git+https://github.com/cwon-z/skku-icampus-mcp
```

That puts `icampus-fetcher` on your PATH (`uv tool dir --bin` says where; `uv tool update-shell` fixes a PATH
that lacks it). uv brings its own Python. No browser, no Docker and no ffmpeg are needed: the fetcher imports the
standard library, httpx and pydantic-settings. Upgrade later with `uv tool upgrade skku-icampus`.

### 3. Settings

`ICAMPUS_FETCHER_*` in the environment, in a `.env` file in the directory you start it from, or as flags
(`icampus-fetcher --slots 2 run` and `icampus-fetcher run --slots 2` both work). A `.env` keeps the token off
the command line:

```
ICAMPUS_FETCHER_SERVER=http://<tailnet address of the server>:9013
ICAMPUS_FETCHER_TOKEN=<this machine's token>
```

| Setting | Flag | Default | |
|---|---|---|---|
| `ICAMPUS_FETCHER_SERVER` | `--server` | | the icampus API, as you reach it over Tailscale |
| `ICAMPUS_FETCHER_TOKEN` | `--token` | | this machine's token from `ICAMPUS_FETCHER_TOKENS` |
| `ICAMPUS_FETCHER_NAME` | `--name` | the machine's name | what statuses call this fetcher: letters, digits, spaces, dots, hyphens; 40 at most; not `server` |
| `ICAMPUS_FETCHER_SLOTS` | `--slots` | 1 | videos downloaded at a time, 1 to 4 |
| `ICAMPUS_FETCHER_DIR` | `--dir` | the per-user cache directory | where a video lies while it is downloaded and uploaded. A directory of the fetcher's own: a new or empty one |
| `ICAMPUS_FETCHER_MAX_DISK_GB` | `--max-disk-gb` | 0 (no limit) | the most the work directory may hold; a video that doesn't fit is left for the server or another fetcher |
| `ICAMPUS_FETCHER_KEEP_AWAKE` | `--keep-awake`, `--no-keep-awake` | on | no idle sleep while a job is active |

The default work directory is `~/Library/Caches/skku-icampus-fetcher` on macOS,
`%LOCALAPPDATA%\skku-icampus-fetcher` on Windows and `~/.cache/skku-icampus-fetcher` elsewhere. Run one fetcher
per directory; `--slots` is how one fetcher downloads several videos at once. The fetcher deletes leftover
downloads in its work directory, so it refuses one that already holds other files.

### 4. `icampus-fetcher check`

Connect Tailscale first: the server's address is only reachable on your tailnet.

```
server       ok       http://<address>:9013 answered HTTP 200, version 0.1.0
token        ok       accepted as a fetcher token; this fetcher is called 'macbook'
commons      ok       lcms.skku.edu described a video of <size> MB
disk         ok       <free> GB free
work dir     ok       /Users/<you>/Library/Caches/skku-icampus-fetcher (private to this user, not in a synced folder)
```

It leases nothing and downloads nothing: one description of one video is all it asks of the content server. A
line that says `FAILED` says what to change. `WARNING` on `work dir` means the directory is in a synced folder,
which `run` refuses.

### 5. `bench`: is it worth it?

Run it in both places, at a time when the queue is idle:

```bash
docker compose exec icampus icampus bench    # on the server
```

```bash
icampus-fetcher bench                        # on the laptop
```

It asks the server for a few hosted videos, downloads them for 45 seconds (`--seconds`) at 1, 2 and 3
connections (`--streams 1,2,3`), each connection on a different video, and throws every byte away. Nothing is
written to disk.

```
Download speed from this machine to lcms.skku.edu: 45 s per step, nothing is kept.
streams  MB/s per stream               total MB/s
      1  <a>                                  <a>
      2  <b>  <c>                           <b+c>
      3  <d>  <e>  <f>                    <d+e+f>
```

How to read it:

- **One stream from the laptop is faster than one stream from the server.** A fetcher on that laptop helps.
  Start it. (The upload to the server costs time too: it runs at whatever your tailnet gives between the two
  machines. A status shows that phase and its speed.)
- **On the server, the total grows with the streams.** The content server limits each connection, not each
  address. Raise `download_concurrency` in the admin UI first (up to 4): that costs nothing and needs no fetcher.
- **The total stays about the same however many streams.** The limit is per address. More connections from one
  place don't help; another place does, which is what a fetcher is.
- **On the laptop the total grows too.** Give the fetcher more `--slots`.

### 6. `icampus-fetcher run`

It runs in the foreground until you stop it with Ctrl+C, and shows one line per active job: title, megabytes
done of the total, MB per second, and the phase (`downloading`, then `uploading`). Without a terminal (started
at login, see below) it writes one line per event and one per minute for a running job.

Whether the server also downloads is the `local_downloads` setting (admin UI, Settings, Storage):

| `local_downloads` | |
|---|---|
| `on` (default) | The server downloads as it always has, `download_concurrency` at a time, and fetchers take what waits behind. |
| `fallback` | The server downloads only while no fetcher has been heard from for two minutes. While one is around, the server still takes a video no fetcher around can: one too big for their disks, or one that isn't an MP4. |
| `off` | The server never downloads. The queue waits for a fetcher. |

Whoever takes a job, the queue runs in the order the videos were asked for, and a job a fetcher has is never
also started by the server.

## Run it at login

Both examples start the fetcher in a directory that holds its `.env`. Replace the placeholders; no address or
token belongs in a file you share.

### macOS: a LaunchAgent

Put the `.env` in `~/.config/icampus-fetcher/` (`chmod 600` it), and save this as
`~/Library/LaunchAgents/com.example.icampus-fetcher.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.icampus-fetcher</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOUR_USER/.local/bin/icampus-fetcher</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/YOUR_USER/.config/icampus-fetcher</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>/Users/YOUR_USER/Library/Logs/icampus-fetcher.log</string>
  <key>StandardErrorPath</key><string>/Users/YOUR_USER/Library/Logs/icampus-fetcher.log</string>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.icampus-fetcher.plist
```

```bash
launchctl bootout gui/$(id -u)/com.example.icampus-fetcher
```

The first starts it now and at every login; the second stops it (the fetcher hands its job back first). The
log holds lecture titles and numbers, never an address or the token.

### Windows: Task Scheduler

Put the `.env` in a folder that OneDrive doesn't copy, for example `%USERPROFILE%\icampus-fetcher\`, and beside
it a `run.cmd`:

```bat
@echo off
cd /d "%USERPROFILE%\icampus-fetcher"
"%USERPROFILE%\.local\bin\icampus-fetcher.exe" run >> "%USERPROFILE%\icampus-fetcher\fetcher.log" 2>&1
```

Then, in a terminal:

```bat
schtasks /Create /TN "icampus-fetcher" /SC ONLOGON /RL LIMITED /TR "\"%USERPROFILE%\icampus-fetcher\run.cmd\""
```

```bat
schtasks /Run /TN "icampus-fetcher"
```

```bat
schtasks /End /TN "icampus-fetcher"
```

By default a task made this way starts only on AC power and is stopped after three days: look at the task's
*Conditions* and *Settings* tabs in Task Scheduler. Ending a task kills the fetcher without a word; the
server then takes its job back when the lease runs out, about 90 seconds later.

## How it behaves

- **A job is a lease.** The fetcher asks for work (`POST /lease`); when nothing is due it asks again after 2 to
  30 seconds. A job is its for 90 seconds from its last word about it, and it reports progress every 5 seconds,
  during the download and during the upload. One token holds four jobs at most. A lease nobody ever spoke for
  (the fetcher had stopped waiting for the answer) goes back into the queue without a failed try.
- **The server knows the file before the fetcher has it.** When it gives a job away, the server asks the content
  server itself (no cookies) how big the file is, refuses one over `video_max_gb`, and keeps room for it in the
  video cache. The room is given back when the job fails, runs out, or is finished.
- **The upload** goes in 8 MB pieces, each starting where the file on the server ends. After a network blip the
  fetcher asks the server how much it has and goes on from there, within the same lease. With the last piece it
  sends the SHA-256 of what it downloaded. The server checks the size against what the content server told it,
  the hash against the bytes it received, and that the file is an MP4. Then the file becomes a cached video
  through the same code as a download of the server's own: the same file, sidecar and cache entry, and
  transcription jobs that were waiting for it go on. A failed check fails that try, and the upload is deleted.
  A request that brings nothing for 30 seconds is ended (the upload can go on from what arrived), and an upload
  that gets nowhere for ten minutes is given up.
- **A video that isn't an MP4** is refused as an upload, once. From then on that job is left to the server's own
  download, which keeps such a file and labels it. With `local_downloads` off it waits.
- **Failures** are reported in the same words, and with the same "may pass" flag, as the server's own tries
  (`commons.failure`). The queue's retry policy decides what follows: a pause and another try by whoever is
  free, `export_retries` times, then `failed`.
- **A fetcher whose tries keep failing rests.** A laptop that can't reach the content server would otherwise
  cost every job in the queue a try. After a failed job the fetcher itself waits before it looks for work again
  (5 seconds, growing to 5 minutes). After three failed tries in a row the server gives that token no work for a
  minute, then 5, 15 and 60; an upload that arrives clears the count. A resting fetcher doesn't count as "around"
  for `fallback`.
- **Ctrl+C, logout, shutdown.** The fetcher tells the server "fetcher stopped", so the job is back in the queue
  at once, and deletes its file. Killed without a chance to say so, its lease runs out instead.
- **Sleep, a closed lid, a lost network.** Heartbeats stop. After 90 seconds the server takes the job back: it
  counts as a failed try and is due again 30 seconds later (longer after repeated failures), for the server or
  another fetcher. When the laptop wakes, the fetcher is told its lease is stale, deletes the partial file and
  asks for work again. There is no continuing a download: the content server ignores `Range`.
- **Keep-awake.** While a job is active the machine doesn't go to sleep by itself: `caffeinate -i` on macOS,
  `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` on Windows, nothing elsewhere. It is cleared as
  soon as no job is active, and the display may always sleep. Closing the lid still sleeps the machine.
- **A restart of the server** costs nothing: leases and what was uploaded are kept, and every lease gets a full
  90 seconds from the start. The fetcher keeps downloading meanwhile and goes on when the server answers again.
  It gives a job up after ten minutes without an answer.
- **A start of the fetcher** deletes the `<asset id>.part` files an earlier run left in its work directory, and
  nothing else. A second fetcher on the same directory refuses to start.
- **Disk.** A fetcher says how much room it has when it asks for work (free space less 1 GB, and
  `MAX_DISK_GB`). A video that doesn't fit stays in its place in the queue for someone else, with no failed try.

## Where to see it

- `GET /api/v1/videos/{item_id}`, `export_video`: while a video downloads, `by` is `server` or the fetcher's
  name and `phase` is `downloading` or `uploading`, with `speed_mbps` and `eta_s` of that phase.
- `GET /api/v1/status`, `sync_status`: under `exports`, each active download with `by` and `phase`, and
  `fetchers`: name, `token` (the token's label), platform, version, `last_seen`, `online`, `active_jobs`,
  `speed_mbps`, and `failed_in_a_row` with `resting_until` while it is given no work.
- Admin UI, *Jobs*: the Fetchers panel shows the same, refreshed every few seconds.

## The routes

All under `/api/v1/fetchers`, with `Authorization: Bearer <fetcher token>`. A call that names a lease which is
no longer the job's gets 409, and the fetcher drops that job.

| Route | |
|---|---|
| `POST /lease` | `{name, version, platform, slots_free, free_bytes}` → `{job_id, lease_id, asset_id, host, title, expected_bytes, max_bytes, lease_ttl_s}`, or 204: nothing is due, the token holds four jobs, or it is resting |
| `POST /jobs/{job_id}/progress` | `{lease_id, phase, done_bytes, total_bytes, speed_bps}` → `{continue}`; renews the lease |
| `PUT /jobs/{job_id}/file` | a piece: `Content-Range: bytes <first>-<last>/<size>`, `X-Lease-Id`, and `X-File-SHA256` on the last → `{received_bytes, complete}`; 416 with `received_bytes` when the piece is out of step; 422 with `refused` when the file fails a check or can't be kept |
| `GET /jobs/{job_id}/file` | `X-Lease-Id` → `{received_bytes, complete}` |
| `POST /jobs/{job_id}/fail` | `{lease_id, message, transient}` → what the queue did with it |
| `GET /bench-targets` | a few `{asset_id, host}`; the one route an ordinary API token may call too |

## Measured

Nothing here has been measured yet. Fill it in from `bench` and from real exports, with the date and the place,
in the style of [icampus-notes.md](icampus-notes.md):

| When | From | 1 stream | 2 streams (total) | 3 streams (total) |
|---|---|---|---|---|
| | the homelab | | | |
| | MacBook, home network | | | |
| | MacBook, campus network | | | |
| | Windows laptop | | | |

- Per connection or per address: not known yet (the question `download_concurrency` and `bench` are there to
  answer).
- Upload speed from each laptop to the server over the tailnet: not measured.
- A whole lecture through a fetcher, download plus upload, against the server's own hour: not measured.

## Not checked on a real Mac or Windows machine yet

The code was written and tested on Linux. The parts that differ per system are in `machine.py` and are tested
with the system calls replaced by recorders. By hand, on each system:

**Both**
- `uv tool install git+https://…` succeeds and `icampus-fetcher --help` runs.
- `icampus-fetcher check` is all `ok` with Tailscale connected, and says so plainly without it.
- `run` picks up a queued export; the server ends with a `ready` video that plays; the work directory is empty
  afterwards.
- Ctrl+C in the middle of a download: the job is `queued` again at once with "fetcher stopped".
- A work directory that holds other files is refused; the default one is made and used without a word.
- The status line with a Korean title, and after resizing the window.

**macOS**
- The work directory is `~/Library/Caches/skku-icampus-fetcher`, mode 700.
- During a job `pmset -g assertions` lists a `caffeinate` holding `PreventUserIdleSystemSleep`; it is gone when
  the job ends, and the display still sleeps.
- Close the lid during a download: within about two minutes the server shows the job queued again with "the
  fetcher … went quiet". Open it: the fetcher drops the old job and takes a new one.
- A work directory under `~/Documents` with iCloud's "Desktop & Documents Folders" on is refused.
- The LaunchAgent starts at login, writes its log, and `launchctl bootout` hands the job back.

**Windows**
- The work directory is `%LOCALAPPDATA%\skku-icampus-fetcher`.
- During a job `powercfg /requests` (in an elevated terminal) lists a SYSTEM request by the fetcher's Python
  process and none under DISPLAY; it is gone when the job ends.
- Ctrl+C in Windows Terminal hands the job back. Closing the window doesn't get the chance: the lease runs out.
- A work directory under a OneDrive folder (including a Documents folder that OneDrive backs up) is refused.
- The status line in the console you use, with Korean titles.
- Sleep and resume, as for the lid on macOS.
- The Task Scheduler entry starts at logon; check its power conditions.
