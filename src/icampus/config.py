from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

KST = ZoneInfo("Asia/Seoul")
CANVAS = "https://canvas.skku.edu"


def parse_tokens(raw: str) -> dict[str, str]:
    """'laptop:abc,phone:def' -> {'abc': 'laptop', 'def': 'phone'} (token -> label)."""
    tokens: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        label, sep, token = part.partition(":")
        token = token.strip()
        if not sep or not label.strip() or len(token) < 20 or "change-me" in token:
            raise ValueError("tokens must look like 'label:token' with 20+ random characters "
                             "(make one with: openssl rand -hex 24)")
        tokens[token] = label.strip()
    return tokens


class Settings(BaseSettings):
    """Collector + REST API settings (ICAMPUS_*)."""

    model_config = SettingsConfigDict(env_prefix="ICAMPUS_", env_file=".env", extra="ignore")

    username: str = ""
    password: SecretStr = SecretStr("")
    api_tokens: str = ""
    admin_labels: str = "admin"  # token labels allowed to clear a login block (retry_login)
    host: str = "127.0.0.1"
    port: int = 9013
    data_dir: Path = Path("var")

    # Empty sync_times disables the schedule (manual syncs still work).
    sync_times: str = "07:30,12:30,18:00,22:30"
    sync_jitter_min: int = 10
    manual_cooldown_min: int = 10
    max_logins_per_day: int = 3
    stale_hours: int = 12
    sync_timeout_s: int = 600

    term: str = ""  # e.g. "2026년 2학기"; empty = detect from Canvas
    mypage_path: str = "/accounts/1/external_tools/346?launch_type=global_navigation"
    headless: bool = True
    # Lecture videos kept in var/videos; the least recently used go first once the total passes the cap.
    video_cache_gb: float = 30
    video_max_gb: float = 4  # bigger videos aren't downloaded
    # The other kinds of file under var/ (cache.py). Each kind has a cap; cache_total_gb caps them all together.
    attachment_cache_gb: float = 2
    attachment_reuse_hours: int = 12  # a download whose version iCampus didn't give is reused this long
    attachment_max_mb: int = 30  # bigger attachments aren't downloaded
    audio_cache_gb: float = 5
    frames_cache_gb: float = 5
    cache_total_gb: float = 40

    # Transcription (pipeline.py, stt/). Providers themselves live in the database; these are the defaults around them.
    prefetch_limit: int = 2  # jobs past "queued" at a time, so downloads never run far ahead of transcription
    keep_video_after_processing: bool = True  # False: delete a video once its audio is extracted
    download_retries: int = 3
    ffmpeg_threads: int = 1
    stt_default_provider: str = ""  # a provider id; empty = the enabled provider with the lowest priority number
    stt_fallback_order: list[str] = []
    stt_routing_rules: list[dict] = []  # [{"course": "CSE1001", "language": "ko", "provider": "<id>"}], first match wins
    stt_default_language: str = ""  # ISO 639-1; empty = let the provider detect it
    stt_course_languages: dict[str, str] = {}  # course id or code -> language
    stt_course_terms: dict[str, str] = {}  # course id or code -> "term, term, ...", for providers with keyterms on
    stt_min_timestamps: str = "segment"  # word, segment or none: the least a transcript must carry to be accepted
    stt_chunk_minutes: int = 20  # target length of a piece when a provider's limits force a split
    stt_audio_bitrate_kbps: int = 24
    stt_fail_threshold: int = 3  # failures in a row before a provider is skipped
    stt_skip_minutes: int = 15
    stt_auto_transcribe: bool = False  # after each sync, queue new Commons-hosted lectures of stt_auto_courses
    stt_auto_courses: list[str] = []
    stt_auto_retranscribe: bool = False  # queue a transcript again when a sync sees a newer video
    stt_monthly_usd_cap: float = 0  # all providers together; 0 = no cap beyond each provider's own
    stt_budget_reset_day: int = 1  # day of the month a budget period starts (a provider's config can set its own)
    # First boot only, while no provider is registered: a JSON file, or JSON inline, with a list of provider configs
    # (docs/stt-providers.md). Never a place for keys: a config names the environment variable that holds its key.
    stt_seed_file: Path | None = None
    stt_seed: str = ""
    kuma_push_url: str = ""
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "icampus.db"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "session.json"


class McpSettings(BaseSettings):
    """MCP server settings (ICAMPUS_MCP_*). Never holds SKKU credentials."""

    model_config = SettingsConfigDict(env_prefix="ICAMPUS_MCP_", env_file=".env", extra="ignore")

    api_url: str = "http://127.0.0.1:9013"
    api_token: str = ""
    tokens: str = ""  # bearer tokens for direct (tailnet) clients
    access_team: str = ""  # e.g. myteam.cloudflareaccess.com
    access_aud: str = ""
    access_emails: str = ""
    allowed_hosts: str = ""  # comma list; empty = loopback only, "*" = no Host check
    public_url: str = ""  # e.g. https://mcp.example.com: export_video links point here
    log_level: str = "INFO"
