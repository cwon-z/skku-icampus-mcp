"""Headless Chromium session: login, guarded page loads, GET-only data access.

Opening an item page in iCampus can mark it complete (a lecture PDF did), so the
browser runs behind an allowlist: it may only load login pages, the Canvas home and
the LearningX list panels. Everything else is aborted and logged.
"""

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, AsyncIterator
from urllib.parse import urlencode, urljoin, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Page, Request, Route, async_playwright

from .config import CANVAS, KST, Settings
from .store import Store

log = logging.getLogger(__name__)

# (host, path regex) pairs a frame may navigate to. Method-agnostic: logins and LTI launches POST.
ALLOWED_PAGES = [
    ("icampus.skku.edu", r"/(xn-sso/.*|login(/.*)?)?"),
    ("canvas.skku.edu", r"/"),
    ("canvas.skku.edu", r"/login(/.*)?"),
    ("canvas.skku.edu", r"/learningx/login(/.*)?"),
    # LearningX list panels (the sync reads their JSON directly; these are for re-issuing its token
    # and for the probe). Item viewers such as /learningx/lti/lecture_attendance/items/view/<id> stay blocked.
    ("canvas.skku.edu", r"/(courses|accounts)/\d+/external_tools/\d+"),
    ("canvas.skku.edu", r"/learningx/lti/(dashboard_v2|modulebuilder|lecture_attendance/course_menu)"),
    ("canvas.skku.edu", r"/profile/settings"),
]
# Non-GET requests a page may send without navigating.
ALLOWED_POSTS = [("icampus.skku.edu", r"/xn-sso/customs/pages/logon-url\.php")]
BLOCKED_TYPES = {"image", "media", "font"}


def _match(rules: list[tuple[str, str]], url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and any(
        parts.hostname == host and re.fullmatch(path, parts.path or "/") for host, path in rules)


def navigation_allowed(url: str, method: str, is_navigation: bool, resource_type: str = "") -> bool:
    """The guard's whole policy, as a pure function."""
    if url.startswith(("about:", "data:", "blob:")):
        return True
    if is_navigation:
        return _match(ALLOWED_PAGES, url)
    if resource_type in BLOCKED_TYPES:
        return False
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    return _match(ALLOWED_POSTS, url)


def safe_path(url: str) -> str:
    """host + path only; query strings can carry SSO tokens."""
    p = urlsplit(url)
    return f"{p.hostname}{p.path}"


_URL_QUERY = re.compile(r"(https?://[^\s?#'\"]+)[?#][^\s'\"]*")


def clean_error(exc: BaseException) -> str:
    """Exception type + first line, minus URL query strings. Use this for anything stored or logged:
    Playwright appends a 'Call log' with every request header (session cookies, bearer token)
    and page.fill() errors can echo the value typed."""
    lines = str(exc).split("Call log:")[0].strip().splitlines()
    first = _URL_QUERY.sub(r"\1", lines[0])[:300] if lines else ""
    return f"{type(exc).__name__}: {first}" if first else type(exc).__name__


class LoginRejected(Exception):
    """Credentials refused or account locked. Automatic logins stop until a manual retry."""


class NeedsAttention(Exception):
    """Something a human has to look at (password-expiry notice, account picker, captcha)."""


class NotLoggedIn(Exception):
    pass


class FetchError(Exception):
    """Network-level failure (timeout, reset, DNS). The message is already sanitised."""


class CanvasError(Exception):
    def __init__(self, status: int, path: str):
        super().__init__(f"HTTP {status} for {path}")
        self.status = status


class Session:
    def __init__(self, settings: Settings, store: Store, page: Page):
        self.settings = settings
        self.store = store
        self.page = page
        self.context = page.context
        self.blocked: list[str] = []
        self._learningx_refreshed = False

    # --- guard -----------------------------------------------------------

    async def _route(self, route: Route, request: Request) -> None:
        nav = request.is_navigation_request()
        if navigation_allowed(request.url, request.method, nav, request.resource_type):
            await route.continue_()
            return
        entry = f"{request.method} {safe_path(request.url)}"
        if nav:  # a page load we did not expect: worth a warning and a 'partial' run
            self.blocked.append(entry)
            log.warning("guard blocked page %s", entry)
        elif request.method not in ("GET", "HEAD", "OPTIONS"):  # e.g. analytics beacons, preference saves
            log.info("guard blocked %s", entry)
        await route.abort("blockedbyclient")

    # --- Canvas REST (GET only) -------------------------------------------

    async def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> tuple[Any, dict]:
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params, doseq=True)
        try:
            resp = await self.context.request.get(
                url, max_redirects=0,
                headers={"Accept": "application/json+canvas-string-ids, application/json", **(headers or {})})
        except PlaywrightError as exc:
            raise FetchError(f"{clean_error(exc)} ({safe_path(url)})") from None
        if resp.status in (301, 302, 401):
            raise NotLoggedIn(safe_path(url))
        if resp.status >= 400:
            raise CanvasError(resp.status, safe_path(url))
        text = await resp.text()
        return json.loads(text.removeprefix("while(1);")), resp.headers

    async def canvas(self, path: str, params: dict | None = None) -> Any:
        """GET a Canvas API path, following Link rel=next pagination for lists."""
        data, headers = await self.get_json(urljoin(CANVAS, path), {"per_page": 100, **(params or {})})
        if not isinstance(data, list):
            return data
        items = list(data)
        while nxt := _next_link(headers.get("link", "")):
            page, headers = await self.get_json(nxt)
            items.extend(page)
        return items

    # --- login -----------------------------------------------------------

    async def logged_in(self) -> bool:
        try:
            # users/self answers 404 (not 401) when signed out here, so ask for one course instead
            await self.get_json(f"{CANVAS}/api/v1/courses", {"per_page": 1})
            return True
        except NotLoggedIn:
            return False

    async def ensure_login(self, *, allow_credentials: bool) -> str:
        """Returns how the session was obtained: 'saved', 'sso' or 'credentials'."""
        if await self.logged_in():
            return "saved"
        if await self._sso_hop():
            await self.save_state()
            return "sso"
        if not allow_credentials:
            raise NotLoggedIn("session expired and credential login is not allowed")
        if not (self.settings.username and self.settings.password.get_secret_value()):
            raise NotLoggedIn("session expired and ICAMPUS_USERNAME / ICAMPUS_PASSWORD are not set")
        self._count_credential_attempt()
        await self._submit_credentials()
        if not await self._wait_for_canvas() and not await self._sso_hop():
            raise NeedsAttention(f"login did not reach Canvas (stuck at {safe_path(self.page.url)})")
        if not await self.logged_in():
            raise NeedsAttention("login finished but Canvas has no session")
        await self.save_state()
        return "credentials"

    async def _sso_hop(self) -> bool:
        """Canvas /login -> SSO gateway -> back to Canvas. Works without a password while the SSO cookie lives.
        Also recovers from the callback error (ERR_HTTP_RESPONSE_CODE_FAILURE) seen once after SSO login."""
        try:
            await self.page.goto(f"{CANVAS}/login", wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:  # e.g. ERR_HTTP_RESPONSE_CODE_FAILURE on the callback
            log.info("sso hop navigation error: %s", type(exc).__name__)
        return await self._wait_for_canvas(timeout_s=20) and await self.logged_in()

    async def _wait_for_canvas(self, timeout_s: int = 45) -> bool:
        """True once the page sits on Canvas (not a login page); False if the SSO form shows."""
        for _ in range(timeout_s * 2):
            url = self.page.url
            if url.startswith(CANVAS) and "/login" not in urlsplit(url).path:
                await self.page.wait_for_load_state("domcontentloaded")
                return True
            if "/xn-sso/login.php" in url and await self.page.locator("#userid").is_visible():
                return False
            await asyncio.sleep(0.5)
        return False

    def _count_credential_attempt(self) -> None:
        now = datetime.now(KST)
        recent = [t for t in self.store.get_state("login_attempts", [])
                  if datetime.fromisoformat(t) > now - timedelta(hours=24)]
        if len(recent) >= self.settings.max_logins_per_day:
            raise NeedsAttention(f"{len(recent)} credential logins in 24h; not trying again automatically")
        self.store.set_state("login_attempts", [*recent, now.isoformat()])

    async def _submit_credentials(self) -> None:
        page = self.page
        if "/xn-sso/login.php" not in page.url:
            await page.goto(f"{CANVAS}/login", wait_until="domcontentloaded", timeout=30_000)
        answer: dict[str, Any] = {}
        answered = asyncio.Event()

        async def capture(route: Route, request: Request) -> None:
            # Read the SSO answer ourselves: on success the page navigates on at once, and the
            # XHR body is gone before a normal response listener could read it.
            resp = await route.fetch()
            try:
                answer.update(json.loads((await resp.text()).lstrip("\ufeff").strip()))
            except ValueError:
                pass
            await route.fulfill(response=resp)
            answered.set()  # only after the page has its response, or the unroute below races it

        await page.route("**/xn-sso/customs/pages/logon-url.php", capture)
        secrets = [self.settings.password.get_secret_value(), self.settings.username]
        try:
            await page.locator("#userid").wait_for(timeout=20_000)
            await page.fill("#userid", self.settings.username)
            await page.fill("#password", self.settings.password.get_secret_value())
            await page.click("#btnLoginBtn")
            await asyncio.wait_for(answered.wait(), 30)
        except (PlaywrightError, TimeoutError) as exc:
            reason = clean_error(exc)
            for secret in filter(None, secrets):  # errors can echo what was typed
                reason = reason.replace(secret, "***")
            raise NeedsAttention(f"the SSO login form did not behave as expected ({reason})") from None
        finally:
            await page.unroute("**/xn-sso/customs/pages/logon-url.php", capture)
        result = answer
        if not result:
            raise NeedsAttention("the SSO login answered with something that is not JSON")
        # Shapes from the login page's own script (xn-sso/login.php).
        if result.get("error"):
            raise LoginRejected(f"SSO refused the login ({result['error']})")
        if result.get("needNotice"):
            code = result.get("noticeCode")
            if code == 2:
                raise NeedsAttention("SSO shows a password-expiry notice; sign in by hand once")
            if code in (-4441, -4432):
                raise LoginRejected("SSO says the account is locked")
        if len(result.get("accounts") or []) != 1:
            raise NeedsAttention("SSO asks to pick an account; sign in by hand once")
        # The page now posts its callback form by itself. Wait for it to leave the login form, or the
        # next step sees the form still on screen and navigates away mid-login.
        try:
            await page.wait_for_url(lambda url: "/xn-sso/login.php" not in url, timeout=30_000)
        except PlaywrightError:
            raise NeedsAttention("the SSO page accepted the login but did not move on") from None

    # --- LearningX JSON API (GET only) -------------------------------------

    async def learningx(self, path: str, params: dict | None = None) -> Any:
        """LearningX answers with Authorization: Bearer <xn_api_token cookie>, set at SSO login.
        On 401 the token is re-issued by opening the My Page panel, at most once per session."""
        while True:
            token = next((c["value"] for c in await self.context.cookies(CANVAS) if c["name"] == "xn_api_token"), "")
            try:
                data, _ = await self.get_json(urljoin(CANVAS, "/learningx/api/v1" + path), params,
                                              {"Authorization": f"Bearer {token}"})
                return data
            except NotLoggedIn:
                if self._learningx_refreshed:
                    raise
                self._learningx_refreshed = True
                await self.launch(self.settings.mypage_path, "/learningx/lti/dashboard_v2")

    # --- LearningX panels --------------------------------------------------

    async def launch(self, path: str, frame_marker: str, timeout_s: int = 40) -> Frame:
        """Open a Canvas tool page and return the loaded LearningX frame.
        The iframe's src attribute reads about:blank, so match on the frame's real URL."""
        await self.page.goto(urljoin(CANVAS, path), wait_until="domcontentloaded", timeout=timeout_s * 1000)
        for _ in range(timeout_s * 2):
            for frame in self.page.frames:
                if frame_marker in frame.url:
                    await frame.wait_for_load_state("domcontentloaded")
                    return frame
            if "/login" in urlsplit(self.page.url).path:
                raise NotLoggedIn(safe_path(self.page.url))
            await asyncio.sleep(0.5)
        raise TimeoutError(f"{frame_marker} frame did not load from {path}")

    async def save_state(self) -> None:
        path = self.settings.session_path
        path.parent.mkdir(parents=True, exist_ok=True)
        state = await self.context.storage_state()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)


def _next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return part[part.find("<") + 1:part.find(">")]
    return None


@asynccontextmanager
async def open_session(settings: Settings, store: Store, *, headless: bool | None = None) -> AsyncIterator[Session]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=settings.headless if headless is None else headless,
            args=["--disable-dev-shm-usage"])
        try:
            state = settings.session_path if settings.session_path.exists() else None
            context = await browser.new_context(
                storage_state=state, service_workers="block", accept_downloads=False,
                locale="ko-KR", timezone_id="Asia/Seoul")
            page = await context.new_page()
            session = Session(settings, store, page)
            await context.route("**/*", session._route)
            yield session
        finally:
            await browser.close()
