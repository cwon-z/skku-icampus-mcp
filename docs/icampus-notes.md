# How SKKU iCampus behaves (notes for maintainers)

This is what the code relies on. It was observed with a student account in September 2026. iCampus
changes over time, so re-check with `icampus probe` when something breaks.

## Login

- `https://canvas.skku.edu/login` redirects to the SSO form at
  `icampus.skku.edu/xn-sso/login.php`. The form has `#userid`, `#password` and `#btnLoginBtn`.
- The button sends a POST to `/xn-sso/customs/pages/logon-url.php`. The response is JSON, served as `text/plain`:
  - `error` means the login was refused;
  - `needNotice` with `noticeCode` 2 means the password has expired, and -4441 or -4432 mean the account is locked;
  - otherwise `accounts` lists one or more accounts.
- With one account, the page posts a callback form to `/xn-sso/gw-cb.php` at once. That leads through
  `canvas.skku.edu/learningx/login` to `POST /login/canvas`, which sets the Canvas session cookies and
  `xn_api_token`.
- The immediate redirect is why `browser.py` reads the JSON through a route handler, and waits for the page
  to leave the form before doing anything else.
- A second login does not end existing sessions.

## Data

- **Canvas REST** (`/api/v1/...`) works with the session cookie. JSON may start with `while(1);`.
  When signed out, `/api/v1/courses` answers 401, but `/api/v1/users/self` answers 404.
- **LearningX JSON API** (`/learningx/api/v1/...`) needs `Authorization: Bearer <xn_api_token cookie>`.
  It answers 401 without it. It is callable directly, with no panel launch. From 26 September 2026 every
  LearningX call answered **400** for days while the Canvas session stayed valid, so the to-do list, lecture
  completion and attendance froze. The collector now re-issues the token (by opening My Page) on 400, 401 or
  403 and when the cookie is missing, and records the error body's message in the run. Whether that cures the
  400 is not confirmed yet: check the next runs in `/api/v1/status`. The collector uses:
  - `learner/todos?course_ids[]=…`: the data behind My Page's to-do list. My Page shows the rows with
    `completed=false` and `unlock_at` in the past.
  - `courses/{id}/modules?include_detail=true`: lecture items, with completion and deadlines.
  - `courses/{id}/attendance_items/summary?only_use_attendance=true`: attendance per lecture item.
  - `courses/{id}/lessons/attendances`: the official attendance per week and lesson.
- Course names look like `Name_CODE_SECTION(Instructor)`. Terms have a start date but no end date.
- **Files.** The announcement list gives each announcement's `attachments` (seen: file names). The rest
  comes from the Canvas API docs and is not yet checked against SKKU: each attachment also has id, size,
  content type and a download `url`; assignment groups include each assignment's `description` HTML, whose
  file links look like `/courses/<id>/files/<id>` (sometimes with `?verifier=…`); `GET /api/v1/files/<id>`
  describes a file, `url` included. A `verifier` opens the file without a login, so the collector never
  stores or logs download URLs.

## Task status

- Canvas submission state decides for assignments and quizzes, LearningX completion and attendance for
  videos and materials. Either source saying it is done counts: while LearningX answered 400, the frozen
  to-do list kept showing work that Canvas already had as submitted.
- Canvas records nothing for an `external_tool` (Goorm, Gradescope) or `on_paper` assignment until a score
  comes back (it stays `unsubmitted` and is never marked missing), so its status is `unknown`, not missed.
- Canvas still takes late submissions between `due_at` and `lock_at`. LearningX's `late_at` is the end of
  the late-attendance period.

## Side effects: why the browser works from an allowlist

- **Opening a lecture item marks it complete.** Items open at `/courses/{id}/modules/items/{id}`, or in the
  LearningX viewer at `/learningx/lti/lecture_attendance/items/view/{id}`. The collector never loads item,
  assignment, quiz or discussion pages.
- **Loading My Page saves UI preferences.** It sends a POST to `/learningx/api/v1/dashboard-preference`,
  which the guard blocks.
- **Announcements are read from the list API only,** so they stay unread. Attachment downloads look the
  file up in that same list rather than open the announcement.
- **Downloading a file counts as opening it.** Canvas logs it for the course's access reports, and a file that
  is also a module item with a "view" requirement gets that requirement completed. So files are fetched only
  on request, only when a synced announcement or assignment points at them, and never from lecture items
  (LearningX materials): not yet observed live, but that is how Canvas treats file downloads.
- **Everything here counts as account activity.** Canvas and LearningX record page views and API use.
