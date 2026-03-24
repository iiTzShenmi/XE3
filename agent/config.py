import os
import base64
import hashlib
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

load_dotenv(ENV_FILE)


def _get_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def project_root():
    return PROJECT_ROOT


def data_root():
    custom = os.getenv("AGENT_DATA_DIR", "").strip()
    root = Path(custom) if custom else PROJECT_ROOT / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def e3_data_root():
    root = data_root() / "e3"
    root.mkdir(parents=True, exist_ok=True)
    return root


def legacy_e3_runtime_root():
    return PROJECT_ROOT / "agent" / "features" / "e3" / "runtime"


def e3_runtime_root():
    custom = os.getenv("E3_RUNTIME_ROOT", "").strip()
    root = Path(custom) if custom else e3_data_root() / "runtime"
    root.mkdir(parents=True, exist_ok=True)
    return root


def legacy_agent_db_path():
    return PROJECT_ROOT / "agent" / "features" / "e3" / "e3_agent.db"


def agent_db_path():
    custom = os.getenv("AGENT_DB_PATH", "").strip()
    path = Path(custom) if custom else e3_data_root() / "e3_agent.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def e3_root():
    default_root = PROJECT_ROOT.parent / "e3"
    return Path(os.getenv("E3_ROOT", str(default_root))).expanduser()


def line_channel_secret():
    return os.getenv("LINE_CHANNEL_SECRET", "").strip()


def line_channel_access_token():
    return os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()


def line_notify_user_id():
    return os.getenv("LINE_NOTIFY_USER_ID", "").strip()


def discord_bot_token():
    return os.getenv("DISCORD_BOT_TOKEN", "").strip()


def discord_command_prefix():
    return os.getenv("DISCORD_COMMAND_PREFIX", "!").strip() or "!"


def discord_guild_id():
    value = os.getenv("DISCORD_GUILD_ID", "").strip()
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def app_service_name():
    return os.getenv("APP_SERVICE_NAME", "multi-task-agent.service").strip() or "multi-task-agent.service"


def e3_encryption_key() -> bytes:
    secret = (
        os.getenv("E3_ENCRYPTION_KEY", "").strip()
        or os.getenv("FILE_PROXY_SECRET", "").strip()
        or line_channel_secret()
    )
    if not secret:
        raise RuntimeError("Missing E3_ENCRYPTION_KEY/FILE_PROXY_SECRET for E3 secret encryption.")
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def public_base_url():
    state_file = cloudflared_url_file()
    if state_file.exists():
        try:
            url = state_file.read_text(encoding="utf-8").strip().rstrip("/")
        except OSError:
            url = ""
        if url:
            return url
    value = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if value and value.lower() != "auto":
        return value
    return ""


def file_proxy_secret():
    custom = os.getenv("FILE_PROXY_SECRET", "").strip()
    if custom:
        return custom
    return line_channel_secret()


def auto_reload_enabled():
    return _get_bool("AUTO_RELOAD", False)


def port():
    return _get_int("PORT", 5000)


def e3_sync_interval_minutes():
    return max(15, _get_int("E3_SYNC_INTERVAL_MINUTES", 60))


def e3_cache_ttl_minutes():
    return max(1, _get_int("E3_CACHE_TTL_MINUTES", 15))


def e3_reminder_poll_seconds():
    return _get_int("E3_REMINDER_POLL_SECONDS", 60)


def e3_file_proxy_ttl_seconds():
    return max(60, _get_int("E3_FILE_PROXY_TTL_SECONDS", 300))


def e3_file_proxy_max_bytes():
    return max(1024 * 1024, _get_int("E3_FILE_PROXY_MAX_BYTES", 25 * 1024 * 1024))


def e3_file_proxy_max_uses():
    return max(1, _get_int("E3_FILE_PROXY_MAX_USES", 3))


def tunnel_data_root():
    root = data_root() / "cloudflared"
    root.mkdir(parents=True, exist_ok=True)
    return root


def cloudflared_url_file():
    return tunnel_data_root() / "current_url"


def cloudflared_log_file():
    return tunnel_data_root() / "cloudflared.log"


def tunnel_watchdog_state_file():
    return tunnel_data_root() / "watchdog_state.json"


def reminder_worker_lock_file():
    return e3_data_root() / "reminder_worker.lock"


def discord_attachment_max_bytes():
    return max(1024 * 1024, _get_int("DISCORD_ATTACHMENT_MAX_BYTES", 10 * 1024 * 1024))
