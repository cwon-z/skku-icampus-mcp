"""The admin web UI at /admin: settings, speech-to-text providers, the job queue and everything kept on disk.

It is served by the API service, because that is where the data and the settings live, and it is reachable
wherever the API is: keep that a private (tailnet) address.

Signing in: paste an admin-labelled API token once. The server answers with a session cookie that is signed
(HMAC, key in var/admin.key), expires, and is HttpOnly, SameSite=Strict, Secure (unless turned off for plain HTTP
on a tailnet address) and scoped to /admin. Sessions are also recorded on the server, so signing out ends one for
good. Every form carries a CSRF token tied to the session, and every request that changes something must come
from this origin.

The pages read the database, the cache index and the disk. Nothing here sends a request to iCampus; the only
requests to SKKU at all are the ones a button names (download a video, transcribe), which go to the content
server without cookies, as exports always have. Pages are rendered with autoescape and a Content-Security-Policy
without inline script or style, since titles, file names and transcripts are text written by other people.

How it looks is one stylesheet (static/theme.css, tokens first), three bundled fonts and the icons in icons.py:
no page asks another host for anything.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import audio, files, videos
from ..cache import CAPS, KINDS
from ..pipeline import ACTIVE
from ..runtime import SPECS, coerce
from ..stt.registry import Unavailable
from ..transcripts import stamp
from .icons import NOTICE, icon

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
COOKIE = "icampus_admin"
LOGIN_FORM_MINUTES = 30
LOGIN_TRIES, LOGIN_WINDOW_S = 5, 600
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
       "media-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
HEADERS = {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
           "Referrer-Policy": "same-origin", "Cache-Control": "no-store"}
# The sidebar: (page, address, label, icon). Settings sits apart, at its foot.
NAV = [("overview", "/admin/", "Overview", "home"), ("jobs", "/admin/jobs", "Jobs", "jobs"),
       ("cache", "/admin/cache", "Cache", "cache"), ("stt", "/admin/stt", "STT models", "wave")]
STATE_TONE = {"queued": "neutral", "downloading": "info", "extracting": "info", "transcribing": "info",
              "done": "success", "ready": "success", "ok": "success", "failed": "danger", "failing": "warning",
              "skipped": "danger", "paused_budget": "warning", "outdated": "warning", "canceled": "neutral",
              "pinned": "accent", "in use": "info", "kept": "neutral", "unknown": "neutral", "partial": "warning",
              "running": "info", "interrupted": "warning"}
STATE_LABEL = {"ok": "OK", "paused_budget": "paused for budget"}  # the rest read well as they are


class _Login(Exception):
    """Not signed in (or the session ended): go to the sign-in page."""


class _Refused(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


class Sessions:
    """Signed cookies over server-side session records (state key admin_sessions)."""

    def __init__(self, store, data_dir: Path, hours: int):
        self.store, self.hours = store, hours
        self.key = self._key(Path(data_dir) / "admin.key")

    @staticmethod
    def _key(path: Path) -> bytes:
        try:
            key = path.read_bytes()
            if len(key) >= 32:
                return key
        except OSError:
            pass
        key = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key

    def _sign(self, purpose: str, value: str) -> str:
        return hmac.new(self.key, f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()

    def _all(self) -> dict[str, dict]:
        now = time.time()
        return {sid: s for sid, s in (self.store.get_state("admin_sessions") or {}).items() if s["exp"] > now}

    def fingerprint(self, token: str) -> str:
        """What a session remembers of the token that opened it: enough to see later that the token still
        exists, never the token."""
        return self._sign("token", token)

    def create(self, label: str, token: str) -> str:
        """A new session; returns the cookie value."""
        sessions = self._all()
        sid = secrets.token_urlsafe(24)
        sessions[sid] = {"label": label, "exp": time.time() + self.hours * 3600, "flash": [],
                         "token": self.fingerprint(token)}
        self.store.set_state("admin_sessions", sessions)
        return f"{sid}.{self._sign('session', sid)}"

    def get(self, cookie: str | None) -> dict | None:
        sid, _, signature = (cookie or "").partition(".")
        if not sid or not same(signature, self._sign("session", sid)):
            return None
        record = self._all().get(sid)
        return {**record, "sid": sid, "csrf": self._sign("csrf", sid)} if record else None

    def end(self, sid: str) -> None:
        sessions = self._all()
        sessions.pop(sid, None)
        self.store.set_state("admin_sessions", sessions)

    def flash(self, sid: str, tone: str, text: str) -> None:
        """A one-time message for the next page this session loads."""
        sessions = self._all()
        if sid in sessions:
            sessions[sid]["flash"] = [*sessions[sid].get("flash", []), [tone, text]][-5:]
            self.store.set_state("admin_sessions", sessions)

    def take_flash(self, sid: str) -> list[list[str]]:
        sessions = self._all()
        messages = (sessions.get(sid) or {}).get("flash") or []
        if messages:
            sessions[sid]["flash"] = []
            self.store.set_state("admin_sessions", sessions)
        return messages

    def login_token(self) -> str:
        """The sign-in form's CSRF token: a signed time, good for LOGIN_FORM_MINUTES."""
        stamp_ = str(int(time.time()))
        return f"{stamp_}.{self._sign('login', stamp_)}"

    def login_token_ok(self, token: str | None) -> bool:
        stamp_, _, signature = (token or "").partition(".")
        if not stamp_.isdigit() or not same(signature, self._sign("login", stamp_)):
            return False
        return 0 <= time.time() - int(stamp_) <= LOGIN_FORM_MINUTES * 60


def same(given: Any, expected: str) -> bool:
    """Constant-time comparison of something a browser sent with a value of ours. Bytes, because
    hmac.compare_digest refuses strings with non-ASCII characters, and a browser can send anything."""
    return hmac.compare_digest(str(given).encode("utf-8", "replace"), expected.encode())


def same_origin(request: Request) -> bool:
    """False when the browser says the request comes from another site. Sec-Fetch-Site is the browser's own
    statement and decides when it is there; older browsers are judged by Origin or Referer. A request with none
    of them (curl, a script) passes here and still needs the CSRF token."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in ("same-origin", "none")
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source == "null":  # "null": the page's referrer policy hid it
        return True
    return urlsplit(source).netloc == request.headers.get("host", "")


def _when(value: Any) -> str:
    """'2026-11-23T08:45:10+09:00' -> '11-23 08:45'."""
    return f"{value[5:10]} {value[11:16]}" if isinstance(value, str) and len(value) >= 16 else "–"


def _size(n: Any) -> str:
    n = float(n or 0)
    return f"{n / 2**30:.2f} GB" if n >= 2**30 else f"{n / 2**20:.1f} MB" if n >= 2**20 else f"{n / 1024:.0f} kB"


def _pips(used: Any, cap: Any, n: int = 24) -> list[bool]:
    """A meter of n squares: as many lit as the share used (one lit for anything above zero)."""
    used, cap = float(used or 0), float(cap or 0)
    if cap <= 0:
        return [False] * n
    lit = min(n, max(1 if used > 0 else 0, round(n * used / cap)))
    return [i < lit for i in range(n)]


def register(app: FastAPI, ctx: SimpleNamespace) -> None:
    env = ctx.settings.env
    if not env.admin_ui:
        return
    sessions = Sessions(ctx.store, env.data_dir, env.admin_session_hours)
    failures: dict[str, deque] = {}
    templates = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(default=True),
                            trim_blocks=True, lstrip_blocks=True)
    templates.filters.update(when=_when, size=_size, stamp=stamp,
                             cap=lambda text: str(text)[:1].upper() + str(text)[1:],
                             tojson_pretty=lambda v: json.dumps(v, ensure_ascii=False, indent=1))
    templates.globals.update(nav=NAV, tone=lambda state: STATE_TONE.get(state, "neutral"), pips=_pips, icon=icon,
                             notice_icon=NOTICE,
                             state_label=lambda state: STATE_LABEL.get(state) or str(state).replace("_", " "))
    app.mount("/admin/static", StaticFiles(directory=HERE / "static"), name="admin-static")
    app.state.admin_sessions = sessions
    settings, store, cache, registry, pipeline, transcripts = (ctx.settings, ctx.store, ctx.cache, ctx.registry,
                                                              ctx.pipeline, ctx.transcripts)

    # --- plumbing ----------------------------------------------------------

    def render(name: str, request: Request, session: dict | None = None, status: int = 200, **data: Any) -> Response:
        flash = sessions.take_flash(session["sid"]) if session else []
        if session and "page" in data:  # a whole page, with the sidebar: what in the queue wants a look
            queue = data.get("queue") or pipeline.queue_summary()
            data.setdefault("nav_counts", {"jobs": queue["failed"] + queue["paused_budget"]})
            data.setdefault("nav_notes", {"jobs": "failed or paused for budget"})
        html = templates.get_template(name).render(request=request, session=session, flash=flash,
                                                   csrf=session["csrf"] if session else "", **data)
        return HTMLResponse(html, status_code=status, headers=HEADERS)

    def back(path: str, session: dict | None = None, tone: str = "success", message: str | None = None) -> Response:
        if session and message:
            sessions.flash(session["sid"], tone, message)
        return RedirectResponse(path, status_code=303, headers=HEADERS)

    async def signed_in(request: Request) -> dict:
        # async on purpose: FastAPI runs a plain function dependency in a thread pool, and this one reads the
        # database. Every other reader runs on the event loop; a second thread on the same SQLite connection
        # fails as soon as a page loads a few files at once.
        session = sessions.get(request.cookies.get(COOKIE))
        if session is None:
            raise _Login()
        # The session is only as good as the token that opened it: a token that was rotated out, or whose label
        # is no longer an admin one, ends its sessions at the next request.
        still_admin = session["label"] in ctx.admins and any(
            label == session["label"] and same(session.get("token", ""), sessions.fingerprint(token))
            for token, label in ctx.tokens.items())
        if not still_admin:
            sessions.end(session["sid"])
            raise _Login()
        return session

    async def changing(request: Request) -> dict:
        """For every request that changes something: a session, this origin, and the session's CSRF token."""
        session = await signed_in(request)
        if not same_origin(request):
            raise _Refused(403, "This request came from another site and was refused.")
        form = await request.form()
        token = request.headers.get("x-csrf-token") or form.get("csrf") or ""
        if not same(token, session["csrf"]):
            raise _Refused(403, "The form's security token is missing or old. Reload the page and try again.")
        return session

    @app.exception_handler(_Login)
    async def to_login(request: Request, exc: _Login) -> Response:
        if request.headers.get("hx-request"):  # an htmx poll after the session ended: send the whole page
            return Response(status_code=401, headers={**HEADERS, "HX-Redirect": "/admin/login"})
        return RedirectResponse("/admin/login", status_code=303, headers=HEADERS)

    @app.exception_handler(_Refused)
    async def refused(request: Request, exc: _Refused) -> Response:
        return render("message.html", request, None, exc.status, title="Not allowed", message=exc.message)

    def courses() -> list[dict]:
        return sorted(store.records("courses"), key=lambda c: c.get("name") or "")

    def course_names() -> dict[str, str]:
        return {c["id"]: c.get("name") or c["id"] for c in store.records("courses")}

    def text_field(form, name: str) -> str:
        value = form.get(name)
        return value.strip() if isinstance(value, str) else ""

    # --- signing in and out ------------------------------------------------

    def plain_http(request: Request) -> bool:
        host = (request.headers.get("host") or "").split(":")[0]
        return env.admin_cookie_secure and request.url.scheme == "http" and host not in ("localhost", "127.0.0.1")

    @app.get("/admin", include_in_schema=False)
    async def admin_root() -> Response:
        return RedirectResponse("/admin/", status_code=307)

    @app.get("/admin/login", include_in_schema=False)
    async def login_page(request: Request) -> Response:
        try:
            await signed_in(request)
            return RedirectResponse("/admin/", status_code=303)
        except _Login:
            pass
        return render("login.html", request, None, login_token=sessions.login_token(), error=None,
                      plain_http=plain_http(request), configured=bool(ctx.tokens and ctx.admins))

    @app.post("/admin/login", include_in_schema=False)
    async def login(request: Request) -> Response:
        client = request.client.host if request.client else "?"
        recent = failures.setdefault(client, deque(maxlen=LOGIN_TRIES))
        form = await request.form()

        def again(status: int, error: str) -> Response:
            return render("login.html", request, None, status, login_token=sessions.login_token(), error=error,
                          plain_http=plain_http(request), configured=bool(ctx.tokens and ctx.admins))
        if len(recent) == LOGIN_TRIES and time.time() - recent[0] < LOGIN_WINDOW_S:
            return again(429, "Too many failed sign-ins from this address. Wait ten minutes and try again.")
        if not same_origin(request) or not sessions.login_token_ok(form.get("login_token")):
            return again(403, "The sign-in form expired. Try again.")
        given = text_field(form, "token").encode("utf-8", "replace")
        label = next((label for token, label in ctx.tokens.items()
                      if given and hmac.compare_digest(given, token.encode())), None)
        if label is None or label not in ctx.admins:
            recent.append(time.time())
            log.warning("admin UI: failed sign-in from %s", client)
            return again(401, "That isn't an admin token. Use a token whose label is listed in ICAMPUS_ADMIN_LABELS."
                         if label else "That token isn't one of this server's API tokens.")
        recent.clear()
        log.info("admin UI: %s signed in from %s", label, client)
        response = RedirectResponse("/admin/", status_code=303, headers=HEADERS)
        response.set_cookie(COOKIE, sessions.create(label, given.decode()), max_age=env.admin_session_hours * 3600,
                            path="/admin",
                            httponly=True, samesite="strict", secure=env.admin_cookie_secure)
        return response

    @app.post("/admin/logout", include_in_schema=False)
    async def logout(session: dict = Depends(changing)) -> Response:
        sessions.end(session["sid"])
        response = RedirectResponse("/admin/login", status_code=303, headers=HEADERS)
        response.delete_cookie(COOKIE, path="/admin")
        return response

    # --- overview ------------------------------------------------------------

    @app.get("/admin/", include_in_schema=False)
    async def overview(request: Request, session: dict = Depends(signed_in)) -> Response:
        up, summary = ctx.syncer.health()
        expected = store.get_state("expected_scopes", {})
        from ..sync import DATASETS
        sync = {"healthy": up, "summary": summary, "term": store.get_state("term"), "running": ctx.syncer.running,
                "next_run": ctx.syncer.next_run.isoformat() if ctx.syncer.next_run else None,
                "blocked": store.get_state("login_block"),
                "datasets": {d: store.synced_at(d, expected.get(d)) for d in DATASETS},
                "runs": store.recent_runs(5)}
        names = course_names()
        recent = pipeline.jobs(limit=3)  # the ones that run next, then the latest finished
        for job in recent:
            job["course"] = names.get(job["course_id"], job["course_id"])
        return render("overview.html", request, session, page="overview", sync=sync, totals=cache.totals(),
                      queue=pipeline.queue_summary(), recent=recent, downloads=ctx.videos.summary(),
                      providers=[registry.describe(c) for c in registry.all()],
                      search="trigram index" if transcripts.fts else "substring scan (no FTS5 trigram)",
                      kinds=KINDS)

    # --- settings ------------------------------------------------------------

    def settings_page(request: Request, session: dict, status: int = 200, errors: dict | None = None,
                      typed: dict | None = None) -> Response:
        groups: dict[str, list[dict]] = {}
        for item in settings.describe():
            shown = (typed or {}).get(item["key"])
            if shown is None:
                value = item["value"]
                shown = (", ".join(value) if item["type"] == "list"
                         else json.dumps(value, ensure_ascii=False, indent=1) if item["type"] in ("map", "rules")
                         else value)
            groups.setdefault(item["group"], []).append({**item, "shown": shown,
                                                         "error": (errors or {}).get(item["key"])})
        return render("settings.html", request, session, status, page="settings", groups=groups,
                      env_only=settings.describe_env_only())

    @app.get("/admin/settings", include_in_schema=False)
    async def settings_get(request: Request, session: dict = Depends(signed_in)) -> Response:
        return settings_page(request, session)

    @app.post("/admin/settings", include_in_schema=False)
    async def settings_save(request: Request, session: dict = Depends(changing)) -> Response:
        """Saves one group's form. Only values that differ from what is in force become overrides, so saving a
        page doesn't turn every environment value into a stored one."""
        form = await request.form()
        keys = [k for k in text_field(form, "_keys").split(",") if k in SPECS]
        changes, errors, typed = {}, {}, {}
        for key in keys:
            spec = SPECS[key]
            if spec.kind == "enum" and key not in form:
                continue  # a choice the post doesn't carry stays as it is (a browser always sends a select)
            raw: Any = (key in form) if spec.kind == "bool" else text_field(form, key)
            typed[key] = raw
            try:
                value = coerce(spec, raw)
            except ValueError as exc:
                errors[key] = str(exc)
                continue
            if value != getattr(settings, key):
                changes[key] = value
        if errors:
            return settings_page(request, session, 422, errors, typed)
        for key, value in changes.items():
            settings.set(key, value)
        log.info("admin UI: %s changed settings %s", session["label"], sorted(changes))
        return back("/admin/settings", session, "success",
                    f"Saved: {', '.join(sorted(changes))}." if changes else "Nothing changed.")

    @app.post("/admin/settings/reset", include_in_schema=False)
    async def settings_reset(request: Request, session: dict = Depends(changing)) -> Response:
        key = text_field(await request.form(), "key")
        if key not in SPECS:
            raise _Refused(404, "No such setting.")
        settings.reset(key)
        return back("/admin/settings", session, "success", f"{key} is back to its environment or default value.")

    # --- speech-to-text providers ------------------------------------------

    def stt_page(request: Request, session: dict, status: int = 200, **extra: Any) -> Response:
        rules = [*settings.stt_routing_rules, {}, {}]
        return render("stt.html", request, session, status, page="stt",
                      providers=[registry.describe(c, admin=True) for c in registry.all()],
                      default=registry.default_id(), fallback=", ".join(settings.stt_fallback_order), rules=rules,
                      courses=courses(), min_timestamps=settings.stt_min_timestamps, **extra)

    @app.get("/admin/stt", include_in_schema=False)
    async def stt_get(request: Request, session: dict = Depends(signed_in)) -> Response:
        return stt_page(request, session)

    def provider_form(request: Request, session: dict, config: dict, *, new: bool, status: int = 200,
                      errors: dict | None = None) -> Response:
        cfg = registry.get(config.get("id", "")) if not new else None
        return render("stt_form.html", request, session, status, page="stt", new=new, c=config, errors=errors or {},
                      key_status=registry.key_status(cfg) if cfg else None, formats=list(audio.FORMATS))

    @app.get("/admin/stt/new", include_in_schema=False)
    async def stt_new(request: Request, session: dict = Depends(signed_in)) -> Response:
        blank = {"id": "", "type": "openai_compatible", "enabled": True, "priority": 100, "concurrency": 1,
                 "timeout_s": 1800, "audio_format": "opus", "cost_per_hour": 0, "included_hours": 0,
                 "capabilities": {}, "params": {}, "languages": []}
        return provider_form(request, session, blank, new=True)

    @app.get("/admin/stt/{provider_id}/edit", include_in_schema=False)
    async def stt_edit(provider_id: str, request: Request, session: dict = Depends(signed_in)) -> Response:
        cfg = registry.get(provider_id)
        if cfg is None:
            raise _Refused(404, "No such provider.")
        return provider_form(request, session, cfg.model_dump(), new=False)

    def config_from(form, provider_id: str | None) -> tuple[dict, dict]:
        """(config, errors) from the provider form. Blank numbers mean "not set"; the two JSON boxes are parsed."""
        config: dict[str, Any] = {"id": provider_id or text_field(form, "id"), "type": text_field(form, "type"),
                                  "enabled": "enabled" in form, "keyterms": "keyterms" in form}
        errors: dict[str, str] = {}
        for name in ("name", "description", "model", "base_url", "api_key_env", "audio_format"):
            config[name] = text_field(form, name)
        config["languages"] = [x.strip() for x in text_field(form, "languages").split(",") if x.strip()]
        for name, kind, optional in (("cost_per_hour", float, False), ("included_hours", float, False),
                                     ("monthly_hours_cap", float, True), ("monthly_usd_cap", float, True),
                                     ("reset_day", int, True), ("concurrency", int, False), ("timeout_s", int, False),
                                     ("priority", int, False), ("audio_bitrate_kbps", int, True)):
            raw = text_field(form, name)
            if not raw:
                if optional:
                    config[name] = None
                continue
            try:
                config[name] = kind(raw)
            except ValueError:
                errors[name] = "Use a number" if kind is float else "Use a whole number"
        for name in ("capabilities", "params"):
            raw = text_field(form, name)
            try:
                config[name] = json.loads(raw) if raw else {}
                if not isinstance(config[name], dict):
                    raise ValueError
            except ValueError:
                errors[name] = 'Use a JSON object, e.g. {"max_upload_mb": null}'
                config[name] = {}
        return config, errors

    async def provider_save(request: Request, session: dict, provider_id: str | None) -> Response:
        form = await request.form()
        new = provider_id is None
        config, errors = config_from(form, provider_id)
        shown = {**config, "capabilities_text": text_field(form, "capabilities"), "params_text": text_field(form, "params")}
        before = registry.get(provider_id) if provider_id else None
        had_stored = bool(before and registry.key_status(before)["source"] == "stored")
        if not errors:
            try:
                cfg = registry.save(config, create=new)
            except ValueError as exc:
                errors = getattr(exc, "fields", {"config": str(exc)})
        if errors:
            return provider_form(request, session, shown, new=new, status=422, errors=errors)
        if key := text_field(form, "key"):  # write-only: stored, never shown again
            registry.set_key(cfg.id, key)
        log.info("admin UI: %s %s STT provider %s", session["label"], "added" if new else "changed", cfg.id)
        if had_stored and not key and registry.key_status(cfg)["source"] != "stored":
            return back("/admin/stt", session, "warning",
                        f"{cfg.id} saved. Its stored key was removed, because a key is only ever sent to the "
                        f"address it was entered for: enter it again for the new one.")
        return back("/admin/stt", session, "success", f"{cfg.id} saved.")

    @app.post("/admin/stt", include_in_schema=False)
    async def stt_create(request: Request, session: dict = Depends(changing)) -> Response:
        return await provider_save(request, session, None)

    @app.post("/admin/stt-routing", include_in_schema=False)  # not under /admin/stt/: "routing" is a valid id
    async def stt_routing(request: Request, session: dict = Depends(changing)) -> Response:
        form = await request.form()
        rules = []
        for n in range(50):
            if f"rule_provider_{n}" not in form:
                break
            rule = {k: text_field(form, f"rule_{k}_{n}") for k in ("course", "language", "provider")}
            if rule["provider"] and (rule["course"] or rule["language"]):
                rules.append({k: v for k, v in rule.items() if v})
        try:
            changes = {"stt_default_provider": coerce(SPECS["stt_default_provider"], text_field(form, "default")),
                       "stt_fallback_order": coerce(SPECS["stt_fallback_order"], text_field(form, "fallback")),
                       "stt_routing_rules": coerce(SPECS["stt_routing_rules"], rules),
                       "stt_min_timestamps": coerce(SPECS["stt_min_timestamps"], text_field(form, "min_timestamps"))}
        except ValueError as exc:
            return back("/admin/stt", session, "danger", f"Not saved: {exc}.")
        known = {c.id for c in registry.all()}
        unknown = sorted({*changes["stt_fallback_order"], *(r["provider"] for r in rules),
                          *([changes["stt_default_provider"]] if changes["stt_default_provider"] else [])} - known)
        if unknown:
            return back("/admin/stt", session, "danger", f"Not saved: no provider is called {', '.join(unknown)}.")
        for key, value in changes.items():
            if value != getattr(settings, key):
                settings.set(key, value)
        return back("/admin/stt", session, "success", "Routing saved.")

    @app.post("/admin/stt/{provider_id}", include_in_schema=False)
    async def stt_update(provider_id: str, request: Request, session: dict = Depends(changing)) -> Response:
        if registry.get(provider_id) is None:
            raise _Refused(404, "No such provider.")
        return await provider_save(request, session, provider_id)

    @app.post("/admin/stt/{provider_id}/{action}", include_in_schema=False)
    async def stt_action(provider_id: str, action: str, request: Request,
                         session: dict = Depends(changing)) -> Response:
        cfg = registry.get(provider_id)
        if cfg is None:
            raise _Refused(404, "No such provider.")
        if action == "test":  # the one paid call the UI can make by itself: five seconds of synthesized speech
            result = await registry.test(provider_id)
            row = registry.describe(cfg, admin=True)
            if request.headers.get("hx-request"):
                return render("_stt_health.html", request, session, p=row, result=result)
            return back("/admin/stt", session, "success" if result["ok"] else "danger",
                        f"{provider_id}: " + (f"heard “{result['text']}” in {result['seconds']} s." if result["ok"]
                                              else result["error"]))
        if action in ("enable", "disable"):
            registry.save({**cfg.model_dump(), "enabled": action == "enable"})
            return back("/admin/stt", session, "success", f"{provider_id} {action}d.")
        if action == "forget-key":
            registry.set_key(provider_id, None)
            return back("/admin/stt", session, "success", f"The stored key of {provider_id} is gone.")
        if action == "delete":
            registry.delete(provider_id)
            log.info("admin UI: %s deleted STT provider %s", session["label"], provider_id)
            return back("/admin/stt", session, "success", f"{provider_id} deleted. Its transcripts stay.")
        raise _Refused(404, "No such action.")

    # --- jobs ----------------------------------------------------------------

    def fetchers_data() -> dict:
        """The Fetchers panel: who downloads for this server, and what is being downloaded now. Shown once a
        fetcher token is configured (or a fetcher is still on record); without fetchers the page is as it was."""
        downloads = ctx.videos.summary()
        return {"downloads": downloads, "fetchers_shown": bool(ctx.fetcher_tokens or downloads["fetchers"])}

    def jobs_data(request: Request) -> dict:
        state = request.query_params.get("state") or ""
        states = ACTIVE if state == "active" else (state,) if state else None
        names = course_names()
        jobs = pipeline.jobs(states=states, limit=300)
        for job in jobs:
            job["course"] = names.get(job["course_id"], job["course_id"])
        return {"jobs": jobs, "queue": pipeline.queue_summary(), "state": state, **fetchers_data()}

    @app.get("/admin/jobs", include_in_schema=False)
    async def jobs_get(request: Request, session: dict = Depends(signed_in)) -> Response:
        return render("jobs.html", request, session, page="jobs", courses=courses(), plan=None, asked={},
                      providers=[c.id for c in registry.all() if c.enabled], **jobs_data(request))

    @app.get("/admin/jobs/table", include_in_schema=False)
    async def jobs_table(request: Request, session: dict = Depends(signed_in)) -> Response:
        return render("_jobs_table.html", request, session, **jobs_data(request))

    @app.get("/admin/jobs/fetchers", include_in_schema=False)
    async def jobs_fetchers(request: Request, session: dict = Depends(signed_in)) -> Response:
        """The Fetchers panel by itself, for its refresh. Reads the queue and what fetchers last reported."""
        return render("_fetchers.html", request, session, **fetchers_data())

    @app.post("/admin/jobs/queue", include_in_schema=False)
    async def jobs_queue(request: Request, session: dict = Depends(changing)) -> Response:
        """Queue a course: the first submit shows the plan, the second (confirm) queues it."""
        form = await request.form()
        asked = {"course": text_field(form, "course"), "week": text_field(form, "week"),
                 "model": text_field(form, "model"), "only_missing": "only_missing" in form}
        week = int(asked["week"]) if asked["week"].isdigit() else None
        lectures = [x for x in store.records("lectures", course_ids={asked["course"]} if asked["course"] else None)
                    if week is None or x.get("week_no") == week]
        model = asked["model"] or None
        if "confirm" not in form:
            plan = pipeline.plan(lectures, model=model, only_missing=asked["only_missing"])
            names = course_names()
            for item in plan["items"]:
                item["course"] = names.get(item["course_id"], item["course_id"])
            return render("jobs.html", request, session, page="jobs", courses=courses(), plan=plan, asked=asked,
                          providers=[c.id for c in registry.all() if c.enabled], **jobs_data(request))
        try:
            result = pipeline.queue(lectures, model=model, only_missing=asked["only_missing"], trigger="ui")
        except Unavailable as exc:
            return back("/admin/jobs", session, "danger", f"Nothing queued: {exc}")
        log.info("admin UI: %s queued %d transcription(s)", session["label"], result["queued"])
        return back("/admin/jobs", session, "success",
                    f"Queued {result['queued']} lecture(s), {result['hours']} h of audio.")

    @app.post("/admin/jobs/{job_id}/{action}", include_in_schema=False)
    async def job_action(job_id: int, action: str, session: dict = Depends(changing)) -> Response:
        try:
            job = pipeline.retry(job_id) if action == "retry" else pipeline.cancel(job_id) if action == "cancel" else None
        except ValueError as exc:
            return back("/admin/jobs", session, "danger", str(exc))
        if job is None:
            raise _Refused(404, "No such job.")
        return back("/admin/jobs", session, "success", f"Job {job_id}: {job['state']}.")

    # --- cache ---------------------------------------------------------------

    def cache_query(request: Request) -> dict:
        q = request.query_params
        return {"kind": q.get("kind") if q.get("kind") in KINDS else "", "course": q.get("course") or "",
                "pinned": q.get("pinned") == "1", "order": "asc" if q.get("order") == "asc" else "desc",
                "sort": q.get("sort") if q.get("sort") in ("last_used", "size", "kind", "title", "course") else "last_used"}

    @app.get("/admin/cache", include_in_schema=False)
    async def cache_get(request: Request, session: dict = Depends(signed_in)) -> Response:
        """One table over everything on disk. Read from the index: nothing is fetched to show it."""
        query = cache_query(request)
        names, held = course_names(), cache.protected()
        rows = [{"kind": e.kind, "key": e.key, "course": names.get(e.course_id, e.course_id or ""),
                 "item_id": e.item_id, "title": e.title or e.key, "size": e.size, "last_used": e.last_used,
                 "state": cache.state(e, held), "pinned": e.pinned, "detail": e.meta.get("provider")
                 or e.meta.get("format") or e.meta.get("extract") or ""}
                for e in cache.entries(query["kind"] or None, pinned=True if query["pinned"] else None,
                                       course_ids={query["course"]} if query["course"] else None)]
        rows.sort(key=lambda r: (r[query["sort"]] is None, r[query["sort"]] or ""), reverse=query["order"] == "desc")
        return render("cache.html", request, session, page="cache", rows=rows, totals=cache.totals(), q=query,
                      courses=courses(), kinds=KINDS, capped=CAPS, here="?" + urlencode(
                          {k: (1 if v is True else v) for k, v in query.items() if v}))

    @app.post("/admin/cache/{kind}/{key}/{action}", include_in_schema=False)
    async def cache_action(kind: str, key: str, action: str, request: Request,
                           session: dict = Depends(changing)) -> Response:
        where = text_field(await request.form(), "back")
        target = "/admin/cache" + (where if where.startswith("?") and "//" not in where else "")
        entry = cache.get(kind, key) if kind in KINDS else None
        if entry is None:
            return back(target, session, "danger", "That entry is no longer on disk.")
        if action in ("pin", "unpin"):
            cache.pin(kind, key, action == "pin")
            if action == "unpin":
                cache.enforce()
            return back(target, session, "success", f"{entry.title or key}: {action}ned.")
        if action == "delete":
            cache.delete(kind, key)
            log.info("admin UI: %s deleted %s %s from the cache", session["label"], kind, key)
            return back(target, session, "success", f"{entry.title or key} deleted.")
        lecture = videos.find(store, entry.item_id) if entry.item_id else None
        if action == "redownload" and kind == "video" and lecture:  # an explicit request to the content server
            ctx.videos.start(lecture, refresh=True)  # the copy here (and its pin) stays until the new one arrived
            return back(target, session, "success", f"Downloading {entry.title or key} again.")
        if action == "transcribe" and lecture:
            try:
                status = pipeline.request(lecture, trigger="ui")
            except Unavailable as exc:
                return back(target, session, "danger", str(exc))
            return back(target, session, "success", f"{entry.title or key}: transcript {status['state']}.")
        raise _Refused(404, "That action doesn't apply to this entry.")

    # --- one lecture: the player beside its transcript -----------------------

    @app.get("/admin/lecture/{item_id}", include_in_schema=False)
    async def lecture_page(item_id: str, request: Request, session: dict = Depends(signed_in)) -> Response:
        if not item_id.isdigit():
            raise _Refused(404, "No such lecture.")
        lecture = videos.find(store, item_id)
        rows = transcripts.rows(item_id)
        if lecture is None and not rows:
            raise _Refused(404, "No synced lecture video has this item_id.")
        wanted = request.query_params.get("transcript") or ""
        row = next((r for r in rows if str(r["id"]) == wanted), None) or transcripts.preferred(item_id)
        video = cache.get("video", lecture["video"]["asset_id"]) if lecture else None
        download = ctx.videos.status(lecture) if lecture else {"state": "gone"}
        return render("lecture.html", request, session, page="cache", item_id=item_id,
                      title=(lecture or rows[0]).get("title") or item_id,
                      course=course_names().get((lecture or rows[0]).get("course_id"), ""),
                      video=video, video_state=download["state"], download=download,
                      progress=pipeline.status(lecture) if lecture else None, rows=rows, row=row,
                      hours=bool(row and (row["duration_s"] or 0) >= 3600),
                      segments=transcripts.segments(row) if row else [], exportable=lecture is not None,
                      slides=pipeline.slides(lecture) if lecture else ("none", []),
                      slide_error=pipeline.slide_error(lecture) if lecture else None,
                      providers=[c.id for c in registry.all() if c.enabled])

    @app.post("/admin/lecture/{item_id}/{action}", include_in_schema=False)
    async def lecture_action(item_id: str, action: str, request: Request,
                             session: dict = Depends(changing)) -> Response:
        lecture = videos.find(store, item_id) if item_id.isdigit() else None
        form = await request.form()
        here = f"/admin/lecture/{item_id}"
        if action == "prefer" and text_field(form, "transcript").isdigit():
            transcripts.prefer(int(text_field(form, "transcript")))
            return back(here, session, "success", "That transcript is now the one read for this lecture.")
        if lecture is None:
            raise _Refused(404, "No synced lecture video has this item_id.")
        if action == "download":  # an explicit request to the content server; a failed download is tried again
            state = ctx.videos.start(lecture, retry=True)["state"]
            return back(here, session, "success", "The video is here." if state == "ready"
                        else "The video is in the download queue.")
        if action == "slides":  # explicit too: downloads the video first when it isn't here
            try:
                state = pipeline.request_slides(lecture)
            except audio.AudioError as exc:
                return back(here, session, "danger", str(exc))
            return back(here, session, "success", f"Slides: {state}.")
        if action == "transcribe":
            try:
                status = pipeline.request(lecture, model=text_field(form, "model") or None, trigger="ui",
                                          force="force" in form)
            except Unavailable as exc:
                return back(here, session, "danger", str(exc))
            return back(here, session, "success", f"Transcript: {status['state']}.")
        raise _Refused(404, "No such action.")

    # --- files, served to the signed-in browser ------------------------------

    @app.get("/admin/media/video/{item_id}", include_in_schema=False)
    async def media_video(item_id: str, session: dict = Depends(signed_in)) -> Response:
        """The cached video (Range requests work, so the player can seek). 404 when it isn't on disk."""
        lecture = videos.find(store, item_id) if item_id.isdigit() else None
        entry = cache.get("video", lecture["video"]["asset_id"]) if lecture else None
        if entry is None:
            raise _Refused(404, "The video isn't on this server.")
        cache.touch("video", entry.key)
        return FileResponse(cache.path(entry), media_type=entry.meta.get("content_type") or "video/mp4",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})

    @app.get("/admin/media/slide/{item_id}/{n}.jpg", include_in_schema=False)
    async def media_slide(item_id: str, n: int, session: dict = Depends(signed_in)) -> Response:
        lecture = videos.find(store, item_id) if item_id.isdigit() else None
        path = pipeline.slide_file(lecture, n) if lecture else None
        if path is None:
            raise _Refused(404, "That slide isn't on this server.")
        return FileResponse(path, media_type="image/jpeg",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})

    @app.get("/admin/media/audio/{key}", include_in_schema=False)
    async def media_audio(key: str, session: dict = Depends(signed_in)) -> Response:
        entry = cache.get("audio", key)
        if entry is None:
            raise _Refused(404, "That audio isn't on this server.")
        fmt = audio.FORMATS.get(entry.meta.get("format") or "")
        return FileResponse(cache.path(entry), media_type=fmt.mime if fmt else "application/octet-stream",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})

    @app.get("/admin/media/transcript/{transcript_id}.{fmt}", include_in_schema=False)
    async def media_transcript(transcript_id: int, fmt: str, session: dict = Depends(signed_in)) -> Response:
        row = transcripts.get(transcript_id)
        if row is None or fmt not in ("vtt", "srt", "txt", "json"):
            raise _Refused(404, "No such transcript.")
        body = (transcripts.vtt(row) if fmt == "vtt" else transcripts.srt(row) if fmt == "srt"
                else transcripts.text(row) if fmt == "txt"
                else json.dumps(transcripts.load(row), ensure_ascii=False, indent=1))
        kind = {"vtt": "text/vtt", "srt": "application/x-subrip", "txt": "text/plain", "json": "application/json"}[fmt]
        headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"}
        if fmt != "vtt":  # the player loads the VTT as its subtitle track; the others are downloads
            headers["Content-Disposition"] = f'attachment; filename="{row["item_id"]}-{row["provider_id"]}.{fmt}"'
        return PlainTextResponse(body, media_type=kind, headers=headers)

    @app.get("/admin/attachment/{file_id}", include_in_schema=False)
    async def attachment_page(file_id: str, request: Request, session: dict = Depends(signed_in)) -> Response:
        """An attachment's details and extracted text, from the copy on disk. Never downloads."""
        got = files.peek(settings, file_id) if file_id.isdigit() else None
        if got is None:
            raise _Refused(404, "That attachment isn't on this server. Reading it through the API or the "
                                "assistant downloads it, which iCampus records as you opening it.")
        entry = cache.get("attachment", file_id)
        return render("attachment.html", request, session, page="cache", file_id=file_id, meta=got.meta,
                      text=got.text[:200_000], more=len(got.text) > 200_000, entry=entry)

    @app.get("/admin/media/attachment/{file_id}", include_in_schema=False)
    async def media_attachment(file_id: str, session: dict = Depends(signed_in)) -> Response:
        """The file itself, always as a download: it was written by someone else and is never rendered here."""
        got = files.peek(settings, file_id) if file_id.isdigit() else None
        if got is None:
            raise _Refused(404, "That attachment isn't on this server.")
        cache.touch("attachment", file_id)
        return FileResponse(got.path, media_type="application/octet-stream",
                            filename=got.meta.get("name") or file_id,
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
                                     "Content-Security-Policy": "default-src 'none'; sandbox"})
