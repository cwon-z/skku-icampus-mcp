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
    log_level: str = "INFO"
