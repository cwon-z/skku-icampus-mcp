"""The registered speech-to-text providers: their configs, keys, health, budgets, and which one gets a lecture.

Configs live in the database and are edited from the admin UI. A key comes from the environment variable its
config names, or, when there is none, from var/stt_keys.json (0600), which the UI writes and nothing reads back
out: no response, log line or error carries a key, only whether one is set and its last four characters.

Choosing a provider:
- explicit (`model=<id>`): that one, or `Unavailable` with the reason and the ids that would work. Never another
  one silently.
- automatic: the first routing rule that matches the lecture's course and language, then the default provider,
  then the fallback order, then the rest by priority, leaving out any that is disabled, has no key, keeps failing,
  is over its budget or can't do the language or the timestamps asked for.
"""

import calendar
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..audio import FORMATS
from ..config import KST
from ..store import Store
from . import TEST_CLIP, TEST_CLIP_SECONDS, TIMESTAMPS, Capabilities, SttError, SttProvider, redact
from . import elevenlabs, openai_compatible

log = logging.getLogger(__name__)
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,80}$")
_LANG = re.compile(r"^[a-z]{2,3}$")
DEFAULT_ELEVENLABS = {
    "id": "elevenlabs-scribe", "type": "elevenlabs", "name": "ElevenLabs Scribe v3", "model": "scribe_v3",
    "description": "Hosted, word-level timestamps, strong on Korean and English lectures. $0.20 per audio hour; "
                   "a Creator plan covers about 110 hours a month.",
    "api_key_env": "ELEVENLABS_API_KEY", "cost_per_hour": 0.20, "included_hours": 110, "monthly_hours_cap": 100,
    "priority": 10}


class ProviderConfig(BaseModel):
    """One registered provider instance."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal["elevenlabs", "openai_compatible"]
    name: str = Field("", max_length=80)
    description: str = Field("", max_length=600)  # shown to the assistant, so it can choose
    model: str = Field("", max_length=120)
    base_url: str = Field("", max_length=300)
    api_key_env: str = ""  # the environment variable that holds the key; empty = the stored key, or none
    languages: list[str] = []  # ISO 639-1 codes it should be used for; empty = any
    capabilities: dict[str, Any] = {}  # overrides: timestamps, max_upload_mb, max_duration_s, audio_formats
    cost_per_hour: float = Field(0, ge=0, le=1000)  # USD per audio hour; 0 for self-hosted
    included_hours: float = Field(0, ge=0)  # hours per period already paid for by a plan: these cost nothing more
    monthly_hours_cap: float | None = Field(None, ge=0)
    monthly_usd_cap: float | None = Field(None, ge=0)
    reset_day: int | None = Field(None, ge=1, le=31)  # start of its budget period; None = stt_budget_reset_day
    concurrency: int = Field(1, ge=1, le=16)
    timeout_s: int = Field(1800, ge=10, le=7200)
    priority: int = Field(100, ge=0, le=10000)  # lower = earlier in automatic routing
    enabled: bool = True
    audio_format: str = "opus"
    audio_bitrate_kbps: int | None = Field(None, ge=8, le=256)  # None = stt_audio_bitrate_kbps
    keyterms: bool = False  # send the course's term list (ElevenLabs bills this extra; Whisper gets it as a prompt)
    params: dict[str, Any] = {}  # extra request fields, sent as they are

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError("use 2-63 lowercase letters, digits, dots, hyphens or underscores, e.g. homelab-whisper")
        return v

    @field_validator("api_key_env")
    @classmethod
    def _env(cls, v: str) -> str:
        v = v.strip()
        if not v:
            return v
        if not _ENV_NAME.match(v):
            raise ValueError("use the name of an environment variable, e.g. ELEVENLABS_API_KEY")
        if v.startswith("ICAMPUS_") and not v.startswith("ICAMPUS_STT_"):
            raise ValueError("an ICAMPUS_* variable holds this server's own secrets and is never sent to a provider; "
                             "use another name (ICAMPUS_STT_* is fine)")
        return v

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v:
            return v
        try:
            parts = urlsplit(v)
            host = parts.hostname or ""
        except ValueError:
            raise ValueError("use an address like https://api.example.com/v1") from None
        if parts.scheme not in ("http", "https") or not host or parts.query or parts.fragment:
            raise ValueError("use an address like https://api.example.com/v1 (no query)")
        if parts.username or parts.password:
            raise ValueError("leave credentials out of the address; the key has its own field")
        if host == "skku.edu" or host.endswith(".skku.edu"):
            raise ValueError("lecture audio is never sent to an SKKU host")
        return v

    @field_validator("languages")
    @classmethod
    def _languages(cls, v: list[str]) -> list[str]:
        out = [x.strip().lower() for x in v if x.strip()]
        if any(not _LANG.match(x) for x in out):
            raise ValueError("use ISO 639-1 codes such as ko, en")
        return out

    @field_validator("audio_format")
    @classmethod
    def _format(cls, v: str) -> str:
        if v not in FORMATS:
            raise ValueError(f"use one of: {', '.join(FORMATS)}")
        return v

    @field_validator("params")
    @classmethod
    def _params(cls, v: dict) -> dict:
        if "file" in v:
            raise ValueError('"file" is the audio itself and can\'t be set here')
        try:
            if len(json.dumps(v)) > 4000:
                raise ValueError("too large")
        except TypeError:
            raise ValueError("use plain JSON values") from None
        return v

    @model_validator(mode="after")
    def _whole(self) -> "ProviderConfig":
        if self.type == "openai_compatible" and not (self.base_url and self.model):
            raise ValueError("an OpenAI-compatible provider needs a base URL (ending in /v1 for most servers) "
                             "and a model")
        if self.type == "elevenlabs" and not self.model:
            self.model = elevenlabs.DEFAULT_MODEL  # spelled out, so the list shows what is actually used
        base = elevenlabs.LIMITS if self.type == "elevenlabs" else openai_compatible.LIMITS
        base.merged(self.capabilities)  # raises ValueError for an unknown or malformed override
        return self


class Unavailable(Exception):
    """No provider can take the request. `working` lists the ids that could; `budget` is True when a cap is the
    only thing in the way (the job can wait for the next period instead of failing)."""

    def __init__(self, message: str, working: list[str], *, budget: bool = False):
        super().__init__(message)
        self.working = working
        self.budget = budget


@dataclass
class Choice:
    config: ProviderConfig
    reason: str  # why this provider, in words: stored with the transcript


def period_start(day: int, now: datetime) -> datetime:
    """Midnight (Asia/Seoul) of the latest reset day. A day the month doesn't have means its last day."""
    def reset(year: int, month: int) -> datetime:
        return datetime(year, month, min(day, calendar.monthrange(year, month)[1]), tzinfo=KST)
    start = reset(now.year, now.month)
    if start > now:
        start = reset(now.year - (now.month == 1), 12 if now.month == 1 else now.month - 1)
    return start


def period_end(day: int, now: datetime) -> datetime:
    start = period_start(day, now)
    year, month = start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1
    return datetime(year, month, min(day, calendar.monthrange(year, month)[1]), tzinfo=KST)


def errors_of(exc: ValidationError) -> dict[str, str]:
    """field -> message, without the input values (a key pasted into the wrong field must not come back)."""
    out: dict[str, str] = {}
    for e in exc.errors():
        field = ".".join(str(p) for p in e["loc"]) or "config"
        out[field] = e["msg"].removeprefix("Value error, ")
    return out


class Registry:
    def __init__(self, settings, store: Store, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.store = store
        self.transport = transport  # tests put a fake server here
        self.keys_path = Path(settings.data_dir) / "stt_keys.json"
        self._reserved: dict[int, tuple[str, float]] = {}  # job id -> (provider id, hours) while a job runs
        if not self.store.one("SELECT 1 FROM stt_providers LIMIT 1"):
            self._seed()

    # --- configs -------------------------------------------------------------

    def _seed(self) -> None:
        """First boot: the seed file or inline seed, else one ElevenLabs provider reading ELEVENLABS_API_KEY."""
        env = getattr(self.settings, "env", self.settings)
        raw: Any = None
        try:
            if env.stt_seed_file:
                raw = json.loads(Path(env.stt_seed_file).read_text())
            elif env.stt_seed.strip():
                raw = json.loads(env.stt_seed)
        except (OSError, ValueError) as exc:
            log.error("the STT seed could not be read (%s); starting with the default provider", type(exc).__name__)
        configs = raw.get("providers") if isinstance(raw, dict) else raw
        for config in configs if isinstance(configs, list) and configs else [DEFAULT_ELEVENLABS]:
            try:
                self.save(config, create=True)
            except (ValueError, TypeError) as exc:
                log.error("STT seed: provider %r skipped (%s)", (config or {}).get("id") if isinstance(config, dict)
                          else "?", redact(str(exc)))

    def all(self) -> list[ProviderConfig]:
        """Every registered provider, by priority, then id."""
        out = []
        for row in self.store.query("SELECT id, config FROM stt_providers"):
            try:
                out.append(ProviderConfig(**json.loads(row["config"])))
            except (ValidationError, ValueError):
                log.warning("STT provider %s has a config this version can't read; ignored", row["id"])
        return sorted(out, key=lambda c: (c.priority, c.id))

    def get(self, provider_id: str) -> ProviderConfig | None:
        return next((c for c in self.all() if c.id == provider_id), None)

    def save(self, config: dict, *, create: bool = False) -> ProviderConfig:
        """Validates and stores a config. ValueError (with .fields for a form) when it is wrong."""
        try:
            cfg = ProviderConfig(**config)
        except ValidationError as exc:
            fields = errors_of(exc)
            error = ValueError("; ".join(f"{k}: {v}" for k, v in fields.items()))
            error.fields = fields  # type: ignore[attr-defined]
            raise error from None
        exists = self.store.one("SELECT 1 FROM stt_providers WHERE id=?", (cfg.id,)) is not None
        if create and exists:
            error = ValueError(f"id: a provider called {cfg.id} already exists")
            error.fields = {"id": "a provider with this id already exists"}  # type: ignore[attr-defined]
            raise error
        now = datetime.now(KST).isoformat()
        body = json.dumps(cfg.model_dump(), ensure_ascii=False)
        if exists:
            self.store.execute("UPDATE stt_providers SET config=?, updated_at=? WHERE id=?", (body, now, cfg.id))
        else:
            self.store.execute("INSERT INTO stt_providers (id, config, created_at, updated_at) VALUES (?, ?, ?, ?)",
                               (cfg.id, body, now, now))
        return cfg

    def delete(self, provider_id: str) -> bool:
        """Removes the provider and its stored key. Its transcripts and usage records stay."""
        cur = self.store.execute("DELETE FROM stt_providers WHERE id=?", (provider_id,))
        self.set_key(provider_id, None)
        return cur.rowcount > 0

    # --- keys ----------------------------------------------------------------

    def _stored_keys(self) -> dict[str, str]:
        try:
            data = json.loads(self.keys_path.read_text())
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}

    def set_key(self, provider_id: str, key: str | None) -> None:
        """Stores (or with None, forgets) a provider's key in var/stt_keys.json, readable by this user only."""
        keys = self._stored_keys()
        if key and key.strip():
            keys[provider_id] = key.strip()
        elif provider_id in keys:
            del keys[provider_id]
        else:
            return
        self.keys_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.keys_path.with_name(self.keys_path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(keys, f)
        os.replace(tmp, self.keys_path)

    def key(self, cfg: ProviderConfig) -> str | None:
        """The key in force: the environment wins over the stored one."""
        if cfg.api_key_env and (value := os.environ.get(cfg.api_key_env, "").strip()):
            return value
        return self._stored_keys().get(cfg.id)

    def key_status(self, cfg: ProviderConfig) -> dict:
        """Whether a key is set, where from, and its last four characters. Never the key."""
        from_env = bool(cfg.api_key_env and os.environ.get(cfg.api_key_env, "").strip())
        key = self.key(cfg)
        return {"set": bool(key), "source": "env" if from_env else "stored" if key else None,
                "env": cfg.api_key_env or None, "hint": f"…{key[-4:]}" if key and len(key) >= 12 else None,
                "needed": cfg.type == "elevenlabs" or bool(cfg.api_key_env)}

    # --- adapters ------------------------------------------------------------

    def capabilities(self, cfg: ProviderConfig) -> Capabilities:
        base = elevenlabs.LIMITS if cfg.type == "elevenlabs" else openai_compatible.LIMITS
        caps = base.merged({**cfg.capabilities, "keyterms": cfg.keyterms})
        return caps.merged({"languages": cfg.languages}) if cfg.languages else caps

    def provider(self, cfg: ProviderConfig) -> SttProvider:
        common = dict(id=cfg.id, api_key=self.key(cfg), timeout_s=cfg.timeout_s, params=cfg.params,
                      capabilities=self.capabilities(cfg), transport=self.transport)
        if cfg.type == "elevenlabs":
            return elevenlabs.ElevenLabs(model=cfg.model or elevenlabs.DEFAULT_MODEL,
                                         base_url=cfg.base_url or elevenlabs.BASE_URL, **common)
        return openai_compatible.OpenAICompatible(model=cfg.model, base_url=cfg.base_url, **common)

    def bitrate(self, cfg: ProviderConfig) -> int:
        return cfg.audio_bitrate_kbps or self.settings.stt_audio_bitrate_kbps

    # --- health --------------------------------------------------------------

    def health(self, provider_id: str) -> dict:
        row = self.store.one("SELECT health FROM stt_providers WHERE id=?", (provider_id,))
        return json.loads(row["health"]) if row else {}

    def _set_health(self, provider_id: str, health: dict) -> None:
        self.store.execute("UPDATE stt_providers SET health=? WHERE id=?",
                           (json.dumps(health, ensure_ascii=False), provider_id))

    def record_ok(self, provider_id: str) -> None:
        health = self.health(provider_id)
        health.update(last_ok_at=datetime.now(KST).isoformat(), failures=0, skip_until=None)
        self._set_health(provider_id, health)

    def record_failure(self, provider_id: str, message: str) -> None:
        """Counts a failed call; after stt_fail_threshold in a row the provider is skipped for stt_skip_minutes."""
        now = datetime.now(KST)
        health = self.health(provider_id)
        cfg = self.get(provider_id)  # the message is shown later: cut the key out even if a caller left it in
        health.update(last_error_at=now.isoformat(), last_error=redact(message, self.key(cfg) if cfg else None),
                      failures=int(health.get("failures") or 0) + 1)
        if health["failures"] >= self.settings.stt_fail_threshold:
            health["skip_until"] = (now + timedelta(minutes=self.settings.stt_skip_minutes)).isoformat()
            log.warning("STT provider %s failed %d times in a row; skipped for %d min", provider_id,
                        health["failures"], self.settings.stt_skip_minutes)
        self._set_health(provider_id, health)

    def skipped_until(self, provider_id: str, now: datetime | None = None) -> str | None:
        until = self.health(provider_id).get("skip_until")
        return until if until and until > (now or datetime.now(KST)).isoformat() else None

    def health_state(self, provider_id: str, now: datetime | None = None) -> str:
        """ok, failing (recent errors), skipped (failing too often: left out for now) or unknown (never called)."""
        health = self.health(provider_id)
        if self.skipped_until(provider_id, now):
            return "skipped"
        if health.get("failures"):
            return "failing"
        return "ok" if health.get("last_ok_at") else "unknown"

    async def test(self, provider_id: str) -> dict:
        """Transcribes the bundled five-second clip of synthesized speech and records how that went."""
        cfg = self.get(provider_id)
        if cfg is None:
            raise KeyError(provider_id)
        started = datetime.now(KST)
        result: dict[str, Any] = {"at": started.isoformat()}
        try:
            transcript = await self.provider(cfg).transcribe(
                TEST_CLIP, language="en", options={"format": "opus", "duration": TEST_CLIP_SECONDS, "test": True})
            result.update(ok=True, text=transcript.text[:120], timestamps=transcript.timestamps,
                          language=transcript.language)
            self.record_usage(cfg, TEST_CLIP_SECONDS, kind="test")
            self.record_ok(provider_id)
        except SttError as exc:
            result.update(ok=False, error=redact(str(exc), self.key(cfg)))
            self.record_failure(provider_id, result["error"])
        except Exception as exc:  # an adapter bug must not take the page down, or leak through its message
            result.update(ok=False, error=f"unexpected {type(exc).__name__}")
            self.record_failure(provider_id, result["error"])
        result["seconds"] = round((datetime.now(KST) - started).total_seconds(), 1)
        health = self.health(provider_id)
        health["last_test"] = result
        self._set_health(provider_id, health)
        return result

    # --- usage and budget ----------------------------------------------------

    def _reset_day(self, cfg: ProviderConfig | None) -> int:
        return (cfg.reset_day if cfg else None) or self.settings.stt_budget_reset_day

    def usage(self, cfg: ProviderConfig, now: datetime | None = None) -> dict:
        """Hours and USD this provider used in its current budget period."""
        now = now or datetime.now(KST)
        start = period_start(self._reset_day(cfg), now)
        row = self.store.one("SELECT COALESCE(SUM(audio_seconds), 0) AS s, COALESCE(SUM(cost_usd), 0) AS usd "
                             "FROM stt_usage WHERE provider_id=? AND at>=?", (cfg.id, start.isoformat()))
        return {"hours": row["s"] / 3600, "usd": row["usd"], "period_start": start.date().isoformat(),
                "resets": period_end(self._reset_day(cfg), now).date().isoformat()}

    def total_usd(self, now: datetime | None = None) -> float:
        """What all providers together cost in the current period of the global reset day."""
        start = period_start(self.settings.stt_budget_reset_day, now or datetime.now(KST))
        return self.store.one("SELECT COALESCE(SUM(cost_usd), 0) AS usd FROM stt_usage WHERE at>=?",
                              (start.isoformat(),))["usd"]

    def cost(self, cfg: ProviderConfig, hours: float, used_hours: float) -> float:
        """USD for `hours` more after `used_hours` this period: only what goes beyond the plan's included hours."""
        beyond = max(0.0, used_hours + hours - cfg.included_hours) - max(0.0, used_hours - cfg.included_hours)
        return round(beyond * cfg.cost_per_hour, 4)

    def record_usage(self, cfg: ProviderConfig, audio_seconds: float, *, kind: str = "transcribe",
                     job_id: int | None = None, item_id: str | None = None, now: datetime | None = None) -> float:
        now = now or datetime.now(KST)
        cost = self.cost(cfg, audio_seconds / 3600, self.usage(cfg, now)["hours"])
        self.store.execute("INSERT INTO stt_usage (provider_id, at, audio_seconds, cost_usd, kind, job_id, item_id) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (cfg.id, now.isoformat(), audio_seconds, cost, kind, job_id, item_id))
        return cost

    def reserve(self, job_id: int, provider_id: str, hours: float) -> None:
        """Counts a running job's hours against the cap before they are billed, so two jobs can't both squeeze in."""
        self._reserved[job_id] = (provider_id, hours)

    def release(self, job_id: int) -> None:
        self._reserved.pop(job_id, None)

    def _reserved_hours(self, provider_id: str | None = None, but: int | None = None) -> float:
        return sum(h for job, (pid, h) in self._reserved.items()
                   if job != but and (provider_id is None or pid == provider_id))

    def budget(self, cfg: ProviderConfig, now: datetime | None = None, but: int | None = None) -> dict:
        """This period's use against the caps. `left_hours` is None without a cap."""
        used = self.usage(cfg, now)
        held = self._reserved_hours(cfg.id, but)
        left: list[float] = []
        if cfg.monthly_hours_cap is not None:
            left.append(cfg.monthly_hours_cap - used["hours"] - held)
        if cfg.monthly_usd_cap is not None and cfg.cost_per_hour > 0:
            usd_left = cfg.monthly_usd_cap - used["usd"] - self.cost(cfg, held, used["hours"])
            free_left = max(0.0, cfg.included_hours - used["hours"] - held)
            left.append(free_left + usd_left / cfg.cost_per_hour)
        return {"used_hours": round(used["hours"], 2), "used_usd": round(used["usd"], 2),
                "cap_hours": cfg.monthly_hours_cap, "cap_usd": cfg.monthly_usd_cap,
                "left_hours": round(max(0.0, min(left)), 2) if left else None, "resets": used["resets"]}

    def over_budget(self, cfg: ProviderConfig, hours: float, now: datetime | None = None,
                    but: int | None = None) -> str | None:
        """Why `hours` more would break a cap, or None when they fit."""
        budget = self.budget(cfg, now, but)
        if budget["left_hours"] is not None and hours > budget["left_hours"] + 1e-6:
            cap = (f"{budget['cap_hours']:g} h" if cfg.monthly_hours_cap is not None
                   else f"${budget['cap_usd']:g}")
            return (f"over its monthly cap of {cap}: {budget['left_hours']:.1f} h left, this needs {hours:.1f} h "
                    f"(resets {budget['resets']})")
        total_cap = self.settings.stt_monthly_usd_cap
        if total_cap > 0:
            used = self.usage(cfg, now)["hours"] + self._reserved_hours(cfg.id, but)
            if self.total_usd(now) + self.cost(cfg, hours, used) > total_cap + 1e-9:
                return f"over the total monthly cap of ${total_cap:g} for all providers (stt_monthly_usd_cap)"
        return None

    # --- choosing ------------------------------------------------------------

    def not_eligible(self, cfg: ProviderConfig, *, hours: float = 0.0, language: str | None = None,
                     now: datetime | None = None, but: int | None = None) -> tuple[str, str] | None:
        """(reason, kind) when this provider can't take a lecture now, else None. kind: disabled, key, health,
        language, timestamps or budget."""
        if not cfg.enabled:
            return "disabled", "disabled"
        status = self.key_status(cfg)
        if status["needed"] and not status["set"]:
            where = f"set {cfg.api_key_env} or store one" if cfg.api_key_env else "store one on the STT models page"
            return f"no API key ({where})", "key"
        if until := self.skipped_until(cfg.id, now):
            last = self.health(cfg.id).get("last_error") or "errors"
            return f"failing, skipped until {until[11:16]} (last: {last})", "health"
        if language and cfg.languages and language.lower() not in cfg.languages:
            return f"not set up for language {language!r} (it has: {', '.join(cfg.languages)})", "language"
        need = self.settings.stt_min_timestamps
        have = self.capabilities(cfg).timestamps
        if TIMESTAMPS.index(have) < TIMESTAMPS.index(need):
            return f"returns {have} timestamps, and at least {need} are required (stt_min_timestamps)", "timestamps"
        if reason := self.over_budget(cfg, hours, now, but):
            return reason, "budget"
        return None

    def working(self, **need: Any) -> list[str]:
        return [c.id for c in self.all() if self.not_eligible(c, **need) is None]

    def default_id(self) -> str | None:
        configs = self.all()
        wanted = self.settings.stt_default_provider
        if any(c.id == wanted for c in configs):
            return wanted
        return next((c.id for c in configs if c.enabled), None)

    def _rule_for(self, course: dict | None, language: str | None) -> dict | None:
        names = {str(course.get(k)).lower() for k in ("id", "code") if course and course.get(k)}
        for rule in self.settings.stt_routing_rules:
            if rule.get("course") and rule["course"].lower() not in names:
                continue
            if rule.get("language") and (not language or rule["language"].lower() != language.lower()):
                continue
            return rule
        return None

    def choose(self, *, model: str | None = None, course: dict | None = None, language: str | None = None,
               hours: float = 0.0, now: datetime | None = None, but: int | None = None) -> list[Choice]:
        """The providers to try, in order, each with the reason it is there. Raises Unavailable when there is none."""
        need = dict(hours=hours, language=language, now=now, but=but)
        configs = {c.id: c for c in self.all()}
        if model:
            cfg = configs.get(model)
            if cfg is None:
                raise Unavailable(f"no speech-to-text provider is called {model!r}", self.working(**need))
            if blocked := self.not_eligible(cfg, **need):
                raise Unavailable(f"{model} can't take this now: {blocked[0]}", self.working(**need),
                                  budget=blocked[1] == "budget")
            return [Choice(cfg, f"asked for by id ({model})")]

        order: list[tuple[str, str]] = []
        if rule := self._rule_for(course, language):
            matched = " and ".join(f"{k} {rule[k]}" for k in ("course", "language") if rule.get(k))
            order.append((rule["provider"], f"routing rule ({matched})"))
        if default := self.default_id():
            order.append((default, "default provider"))
        order += [(pid, "fallback order") for pid in self.settings.stt_fallback_order]
        order += [(c.id, "by priority") for c in configs.values()]
        choices: list[Choice] = []
        skipped: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for pid, why in order:
            if pid in seen or pid not in configs:
                continue
            seen.add(pid)
            if blocked := self.not_eligible(configs[pid], **need):
                skipped.append((pid, *blocked))
                continue
            passed = "; ".join(f"{s[0]} skipped: {s[1]}" for s in skipped)
            choices.append(Choice(configs[pid], why + (f" ({passed})" if passed and not choices else "")))
        if not choices:
            if not configs:
                raise Unavailable("no speech-to-text provider is registered", [])
            budget = any(kind == "budget" for _, _, kind in skipped) and all(
                kind in ("budget", "disabled") for _, _, kind in skipped)
            raise Unavailable("no speech-to-text provider can take this now: "
                              + "; ".join(f"{pid}: {reason}" for pid, reason, _ in skipped), [], budget=budget)
        return choices

    # --- for the API ---------------------------------------------------------

    def describe(self, cfg: ProviderConfig, *, admin: bool = False, now: datetime | None = None) -> dict:
        """What list_stt_models shows: enough to choose by, and nothing secret. With admin, the whole config and
        the key's status (set, source, last four characters)."""
        caps = self.capabilities(cfg)
        blocked = self.not_eligible(cfg, now=now)
        health = self.health(cfg.id)
        key = self.key_status(cfg)
        out = {
            "id": cfg.id, "name": cfg.name or cfg.id, "type": cfg.type, "model": cfg.model,
            "description": cfg.description, "enabled": cfg.enabled, "default": cfg.id == self.default_id(),
            "ready": blocked is None, "not_ready": blocked[0] if blocked else None,
            "cost_per_hour_usd": cfg.cost_per_hour, "included_hours": cfg.included_hours or None,
            "timestamps": caps.timestamps, "word_timestamps": caps.timestamps == "word",
            "languages": list(caps.languages) or "any", "max_upload_mb": caps.max_upload_mb,
            "max_duration_s": caps.max_duration_s, "keyterms": cfg.keyterms,
            "health": {"state": self.health_state(cfg.id, now), "last_ok_at": health.get("last_ok_at"),
                       "last_error_at": health.get("last_error_at"), "last_error": health.get("last_error"),
                       "skip_until": self.skipped_until(cfg.id, now)},
            "month": self.budget(cfg, now),
            "key": "set" if key["set"] else "missing" if key["needed"] else "not needed",
        }
        if admin:
            out.update(config=cfg.model_dump(), key_status=key, last_test=health.get("last_test"))
        return out
