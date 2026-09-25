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
  It answers 401 without it. It is callable directly, with no panel launch. The collector uses:
  - `learner/todos?course_ids[]=…`: the data behind My Page's to-do list. My Page shows the rows with
    `completed=false` and `unlock_at` in the past.
  - `courses/{id}/modules?include_detail=true`: lecture items, with completion and deadlines.
  - `courses/{id}/attendance_items/summary?only_use_attendance=true`: attendance per lecture item.
  - `courses/{id}/lessons/attendances`: the official attendance per week and lesson.
- Course names look like `Name_CODE_SECTION(Instructor)`. Terms have a start date but no end date.

## Side effects: why the browser works from an allowlist

- **Opening a lecture item marks it complete.** Items open at `/courses/{id}/modules/items/{id}`, or in the
  LearningX viewer at `/learningx/lti/lecture_attendance/items/view/{id}`. The collector never loads item,
  assignment, quiz or discussion pages.
- **Loading My Page saves UI preferences.** It sends a POST to `/learningx/api/v1/dashboard-preference`,
  which the guard blocks.
- **Announcements are read from the list API only,** so they stay unread.
- **Everything here counts as account activity.** Canvas and LearningX record page views and API use.
