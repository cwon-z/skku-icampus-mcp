"""Runtime settings: one typed registry of everything that can change while the server runs.

A value set in the admin UI (or with PUT /api/v1/settings) is stored in SQLite and overrides the ICAMPUS_* variable
of the same name, which overrides the built-in default. `Runtime` reads like `Settings`, so the code that uses a
setting asks for it each time it needs it and a change applies at once. Anything that decides who gets in (tokens,
the SKKU login, login limits, where the server listens) is not in the registry: it stays in the environment and is
only shown, never changed, from the UI.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .config import KST, Settings
from .store import Store

_TIME = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class Spec:
    key: str
    kind: str  # int, float, bool, str, enum, times, list, rules, map
    group: str
    help: str
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] = ()
    unit: str = ""
    applies: str = "now"  # "now", or "restart" when a running server keeps the old value


SPECS: dict[str, Spec] = {}


def register(*specs: Spec) -> None:
    for spec in specs:
        SPECS[spec.key] = spec


register(
    Spec("video_cache_gb", "float", "Storage", "Lecture videos kept on the server. Past this, the least recently used "
         "go first; pinned videos and ones a job still needs stay.", 0.1, 4000, unit="GB"),
    Spec("video_max_gb", "float", "Storage", "Bigger videos aren't downloaded.", 0.01, 100, unit="GB"),
    Spec("download_concurrency", "int", "Storage", "How many lecture videos download at once. The queue runs in "
         "the order the downloads were asked for; more than one only helps if the content server limits each "
         "connection rather than each address.", 1, 4),
    Spec("export_retries", "int", "Storage", "How often an exported video's download is tried again by itself "
         "after a failure that may pass (the network, a stall, a full cache), with pauses growing from 30 seconds "
         "to an hour. After that the export is failed and says why.", 0, 20),
    Spec("local_downloads", "enum", "Storage", "Whether this server downloads lecture videos itself when fetchers "
         "(other machines of yours, docs/fetchers.md) can. on: it does, download_concurrency at a time, and "
         "fetchers take what waits behind. fallback: only while no fetcher has been heard from for two minutes. "
         "off: never, so the queue waits for a fetcher. Without fetchers, leave it on.",
         choices=("on", "fallback", "off")),
    Spec("attachment_cache_gb", "float", "Storage", "Announcement and assignment attachments kept on the server. "
         "An evicted file is downloaded again on its next read, which iCampus records as another open.",
         0.01, 500, unit="GB"),
    Spec("attachment_reuse_hours", "int", "Storage", "How long a downloaded attachment is reused when iCampus gave "
         "no version for it (one with a version is reused until a sync sees a new one).", 1, 720, unit="h"),
    Spec("attachment_max_mb", "int", "Storage", "Bigger attachments aren't downloaded. They are read into memory, "
         "so keep this well under the container's memory limit.", 1, 200, unit="MB"),
    Spec("audio_cache_gb", "float", "Storage", "Audio extracted from lecture videos for transcription "
         "(about 11 MB per hour).", 0.05, 500, unit="GB"),
    Spec("frames_cache_gb", "float", "Storage", "Slide frames extracted from lecture videos.", 0.05, 500, unit="GB"),
    Spec("cache_total_gb", "float", "Storage", "Everything above together, transcripts included. Past this, the "
         "least recently used files of any kind go first; transcripts are never removed automatically.",
         0.5, 8000, unit="GB"),
    Spec("sync_times", "times", "Sync", "Times of day (Asia/Seoul) a sync runs, e.g. 07:30,12:30. Empty turns the "
         "schedule off; manual syncs still work."),
    Spec("sync_jitter_min", "int", "Sync", "Each scheduled sync starts up to this many minutes early or late.",
         0, 120, unit="min"),
    Spec("manual_cooldown_min", "int", "Sync", "Minimum time between syncs asked for by hand (REST, MCP refresh).",
         0, 1440, unit="min"),
    Spec("stale_hours", "int", "Sync", "Data older than this is reported as stale, and the status as unhealthy.",
         1, 168, unit="h"),
)

register(
    Spec("prefetch_limit", "int", "Transcription jobs", "How many jobs may be past the queue at once (downloading, "
         "extracting, transcribing). It keeps downloads from running far ahead of transcription, so a backlog much "
         "larger than the video cache still fits.", 1, 10),
    Spec("keep_video_after_processing", "bool", "Transcription jobs", "Keep a video once its audio is extracted "
         "(it still leaves when the cache needs the room). Off: delete it right away, unless you pinned it."),
    Spec("download_retries", "int", "Transcription jobs", "How often a transcription job's failed video download "
         "is tried again, with growing pauses (exports have export_retries). Commons was seen ignoring Range, so "
         "a try that can't continue the last one starts from zero.", 0, 10),
    Spec("ffmpeg_threads", "int", "Transcription jobs", "Threads ffmpeg may use. One keeps the container well under "
         "its memory limit.", 1, 8),
    Spec("slides_enabled", "bool", "Transcription jobs", "While a lecture's video is on the server for "
         "transcription, also save a picture of each slide, with the time it was on screen. Off: transcripts only."),
    Spec("slides_scene_threshold", "float", "Transcription jobs", "How much the picture must change to count as a "
         "new slide (ffmpeg's scene score, 0 to 1). Lower finds more slides; near-identical ones are merged anyway.",
         0.01, 1),
    Spec("slides_width", "int", "Transcription jobs", "Slide pictures are scaled down to this width.", 320, 1920,
         unit="px"),
    Spec("stt_auto_transcribe", "bool", "Transcription jobs", "After each sync, queue new lecture videos of the "
         "courses below, using automatic routing. Spends from the provider's budget without asking."),
    Spec("stt_auto_courses", "list", "Transcription jobs", "Course IDs or codes that auto-transcribe covers."),
    Spec("stt_auto_retranscribe", "bool", "Transcription jobs", "When a sync sees a newer version of a video, queue "
         "its transcript again instead of only marking it outdated."),
    Spec("stt_default_provider", "str", "Speech-to-text", "The provider automatic routing uses when no rule "
         "matches. Empty: the enabled provider with the lowest priority number."),
    Spec("stt_fallback_order", "list", "Speech-to-text", "Provider ids to try, in order, when the first choice is "
         "unavailable or fails. Providers not listed come after, by priority."),
    Spec("stt_routing_rules", "rules", "Speech-to-text", 'Rules such as [{"course": "CSE1001", "provider": "id"}] or '
         '[{"language": "ko", "provider": "id"}]. The first rule that matches a lecture picks its provider.'),
    Spec("stt_default_language", "str", "Speech-to-text", "Language given to the provider (ISO 639-1, e.g. ko) when "
         "a request names none and the course has none. Empty: the provider detects it."),
    Spec("stt_course_languages", "map", "Speech-to-text", 'Language per course, e.g. {"CSE1001": "en"}. Keys are '
         "course IDs or codes."),
    Spec("stt_course_terms", "map", "Speech-to-text", 'Terms to listen for per course, e.g. {"CSE1001": "eigenvalue, '
         'Krylov"}. Only sent to providers with key terms turned on (ElevenLabs bills them extra).'),
    Spec("stt_min_timestamps", "enum", "Speech-to-text", "The least a transcript must carry to be accepted: word "
         "times, segment times, or none (plain text from models that give no timestamps).",
         choices=("word", "segment", "none")),
    Spec("stt_chunk_minutes", "int", "Speech-to-text", "Target length of a piece when a provider's upload or "
         "duration limit forces a lecture to be split. Pieces are cut at silences.", 1, 240, unit="min"),
    Spec("stt_audio_bitrate_kbps", "int", "Speech-to-text", "Bitrate of the extracted audio (24 kbps Opus is about "
         "11 MB per hour). A provider's config can set its own.", 8, 256, unit="kbps"),
    Spec("stt_fail_threshold", "int", "Speech-to-text", "Failures in a row after which a provider is skipped.", 1, 20),
    Spec("stt_skip_minutes", "int", "Speech-to-text", "How long a failing provider is skipped.", 1, 1440, unit="min"),
    Spec("stt_monthly_usd_cap", "float", "Budget", "Spending cap for all providers together per budget period. "
         "0: only each provider's own caps apply.", 0, 100000, unit="USD"),
    Spec("stt_budget_reset_day", "int", "Budget", "Day of the month a budget period starts, to match a billing "
         "cycle. A provider's config can set its own.", 1, 31),
)

# Shown on the settings page, never editable there. (key, what it is, secret)
ENV_ONLY: list[tuple[str, str, bool]] = [
    ("username", "SKKU login ID", False),
    ("password", "SKKU password", True),
    ("api_tokens", "API bearer tokens (label:token)", True),
    ("admin_labels", "Token labels with admin rights", False),
    ("fetcher_tokens", "Fetcher tokens (name:token), for /api/v1/fetchers only", True),
    ("max_logins_per_day", "Password logins allowed per day", False),
    ("host", "Address the API listens on", False),
    ("port", "Port the API listens on", False),
    ("data_dir", "Where everything is stored", False),
    ("kuma_push_url", "Uptime Kuma heartbeat address", True),
    ("admin_ui", "Whether this admin UI is served", False),
    ("admin_cookie_secure", "Session cookie only over HTTPS", False),
    ("admin_session_hours", "How long a sign-in lasts", False),
]


def coerce(spec: Spec, raw: Any) -> Any:
    """A value from a form, the environment or JSON, as the setting's type. ValueError says how to fix it."""
    kind = spec.kind
    if isinstance(raw, str):
        raw = raw.strip()
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in ("1", "true", "yes", "on"):
            return True
        if str(raw).lower() in ("0", "false", "no", "off", ""):
            return False
        raise ValueError("Use true or false")
    if kind in ("int", "float"):
        try:
            if isinstance(raw, bool):
                raise ValueError
            value = float(raw)
            if kind == "int":
                if value != int(value):
                    raise ValueError
                value = int(value)
        except (TypeError, ValueError):
            raise ValueError("Use a whole number" if kind == "int" else "Use a number") from None
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("Use a number")
        low, high = spec.min, spec.max
        if (low is not None and value < low) or (high is not None and value > high):
            raise ValueError(f"Use a value from {low:g} to {high:g}" if low is not None and high is not None
                             else f"Use a value of at least {low:g}" if low is not None
                             else f"Use a value of at most {high:g}")
        return value
    if kind == "enum":
        if raw not in spec.choices:
            raise ValueError(f"Use one of: {', '.join(spec.choices)}")
        return raw
    if kind == "str":
        if not isinstance(raw, str) or len(raw) > 500 or "\n" in raw:
            raise ValueError("Use one line of text (up to 500 characters)")
        return raw
    if kind == "times":
        parts = raw if isinstance(raw, list) else str(raw).split(",")
        times = [str(p).strip() for p in parts if str(p).strip()]
        for t in times:
            if not _TIME.match(t):
                raise ValueError(f"Use 24-hour times like 07:30, separated by commas ({t!r} isn't one)")
        return ",".join(f"{int(t.split(':')[0]):02d}:{t.split(':')[1]}" for t in times)
    if kind in ("list", "rules", "map") and isinstance(raw, str):
        if kind == "list" and not raw.startswith("["):
            raw = [p.strip() for p in raw.split(",") if p.strip()]
        else:
            try:
                raw = json.loads(raw) if raw else ({} if kind == "map" else [])
            except ValueError:
                raise ValueError("Use valid JSON") from None
    if kind == "list":
        if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw) or len(raw) > 100:
            raise ValueError("Use a list of names")
        return [x.strip() for x in raw]
    if kind == "map":
        if not isinstance(raw, dict) or not all(isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
                                                for k, v in raw.items()) or len(raw) > 200:
            raise ValueError('Use pairs like {"CSE1001": "ko"}')
        return {k.strip(): v.strip() for k, v in raw.items()}
    if kind == "rules":
        if not isinstance(raw, list) or len(raw) > 100:
            raise ValueError("Use a list of rules")
        rules = []
        for rule in raw:
            if not isinstance(rule, dict) or not isinstance(rule.get("provider"), str) or not rule["provider"].strip():
                raise ValueError('Each rule needs a "provider", plus a "course" and/or a "language"')
            clean = {k: str(rule[k]).strip() for k in ("course", "language", "provider") if rule.get(k)}
            if len(clean) < 2:
                raise ValueError('Each rule needs a "course" and/or a "language" to match on')
            rules.append(clean)
        return rules
    raise ValueError(f"unknown setting type {kind}")


class Runtime:
    """`Settings` with the stored overrides on top. Attribute access works for every Settings field."""

    def __init__(self, settings: Settings, store: Store):
        self._settings = settings
        self._store = store
        self._listeners: list[Callable[[str], None]] = []
        self._overrides: dict[str, Any] = {}
        for row in store.query("SELECT key, value FROM settings"):
            spec = SPECS.get(row["key"])
            if spec is None:
                continue  # a setting from a newer or older version: left in the table, not used
            try:
                self._overrides[spec.key] = coerce(spec, json.loads(row["value"]))
            except ValueError:
                pass  # no longer valid (a range changed): fall back to the environment

    def __getattr__(self, name: str) -> Any:
        overrides = self.__dict__.get("_overrides", {})
        if name in overrides:
            return overrides[name]
        return getattr(self.__dict__["_settings"], name)

    @property
    def env(self) -> Settings:
        return self._settings

    def on_change(self, listener: Callable[[str], None]) -> None:
        self._listeners.append(listener)

    def source(self, key: str) -> str:
        if key in self._overrides:
            return "ui"
        return "env" if key in self._settings.model_fields_set else "default"

    def set(self, key: str, raw: Any) -> Any:
        """Validates and stores an override. Raises KeyError for an unknown or env-only setting, ValueError for a
        bad value."""
        spec = SPECS[key]
        value = coerce(spec, raw)
        self._store.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                            (key, json.dumps(value, ensure_ascii=False), datetime.now(KST).isoformat()))
        self._overrides[key] = value
        self._changed(key)
        return value

    def reset(self, key: str) -> Any:
        """Drops the override: back to the environment or the default."""
        spec = SPECS[key]
        self._store.execute("DELETE FROM settings WHERE key=?", (spec.key,))
        self._overrides.pop(key, None)
        self._changed(key)
        return getattr(self._settings, key)

    def _changed(self, key: str) -> None:
        for listener in self._listeners:
            listener(key)

    def describe(self) -> list[dict]:
        """Every runtime setting with its value and where that comes from, for the settings page and the API."""
        out = []
        for spec in SPECS.values():
            default = type(self._settings).model_fields[spec.key].default
            out.append({"key": spec.key, "group": spec.group, "type": spec.kind, "help": spec.help,
                        "value": getattr(self, spec.key), "source": self.source(spec.key), "default": default,
                        "env": f"ICAMPUS_{spec.key.upper()}", "env_value": getattr(self._settings, spec.key),
                        "min": spec.min, "max": spec.max, "choices": list(spec.choices), "unit": spec.unit,
                        "applies": spec.applies})
        return out

    def describe_env_only(self) -> list[dict]:
        """The settings that stay in the environment. Secrets are reported as set or not set, never shown."""
        out = []
        for key, label, secret in ENV_ONLY:
            value = getattr(self._settings, key, None)
            if key in ("api_tokens", "fetcher_tokens"):  # the labels are useful, the tokens never leave
                shown = ", ".join(sorted({p.partition(":")[0].strip() for p in str(value).split(",") if ":" in p})) \
                    or "not set"
            elif secret:
                raw = value.get_secret_value() if hasattr(value, "get_secret_value") else value
                shown = "set" if raw else "not set"
            else:
                shown = str(value)
            out.append({"key": key, "label": label, "env": f"ICAMPUS_{key.upper()}", "value": shown, "secret": secret})
        return out
