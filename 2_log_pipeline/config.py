import math
import os
import posixpath
from pathlib import PurePosixPath
from typing import Mapping, Optional


def _env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _normalize_absolute_path(path_value: str, setting_name: str) -> str:
    path_value = path_value.strip()
    if not PurePosixPath(path_value).is_absolute():
        raise ValueError(f"{setting_name} must be an absolute container path")

    normalized = posixpath.normpath(path_value)
    if normalized.startswith("//"):
        normalized = "/" + normalized.lstrip("/")
    return normalized


def _positive_finite_float(name: str, default: str) -> float:
    value = os.environ.get(name, default).strip()
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a finite number greater than zero") from exc

    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a finite number greater than zero")

    return parsed


def _mysql_port(environ: Mapping[str, str]) -> int:
    value = environ.get("MYSQL_PORT", "3306").strip()
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError("MYSQL_PORT must be an integer between 1 and 65535") from exc

    if not 1 <= port <= 65535:
        raise ValueError("MYSQL_PORT must be an integer between 1 and 65535")

    return port


LOG_FILE_PATH = _normalize_absolute_path(
    _env_or_default("LOG_FILE_PATH", "/app/logs/access.log"), "LOG_FILE_PATH"
)
CHECKPOINT_FILE_PATH = _normalize_absolute_path(
    _env_or_default("CHECKPOINT_FILE_PATH", "/app/state/checkpoint.json"),
    "CHECKPOINT_FILE_PATH",
)
INBOX_DIRECTORY_PATH = _normalize_absolute_path(
    _env_or_default("INBOX_DIRECTORY_PATH", "/app/state/inbox"),
    "INBOX_DIRECTORY_PATH",
)
CONSUMER_STATE_DIRECTORY_PATH = _normalize_absolute_path(
    _env_or_default("CONSUMER_STATE_DIRECTORY_PATH", "/app/state/consumer"),
    "CONSUMER_STATE_DIRECTORY_PATH",
)
CONSUMER_LOCK_FILE_PATH = _normalize_absolute_path(
    _env_or_default(
        "CONSUMER_LOCK_FILE_PATH", "/app/state/consumer.lock"
    ),
    "CONSUMER_LOCK_FILE_PATH",
)
LOCK_FILE_PATH = _normalize_absolute_path(
    _env_or_default("LOCK_FILE_PATH", "/app/state/single-writer.lock"),
    "LOCK_FILE_PATH",
)
REJECTED_FILE_PATH = _normalize_absolute_path(
    _env_or_default("REJECTED_FILE_PATH", "/app/state/rejected.jsonl"),
    "REJECTED_FILE_PATH",
)
POLLING_INTERVAL = _positive_finite_float("POLLING_INTERVAL", "1.0")
CONSUMER_MAX_RETRIES = int(os.environ.get("CONSUMER_MAX_RETRIES", "5"))
CONSUMER_RETRY_BASE_SECONDS = _positive_finite_float(
    "CONSUMER_RETRY_BASE_SECONDS", "1.0"
)
CONSUMER_RETRY_MAX_SECONDS = _positive_finite_float(
    "CONSUMER_RETRY_MAX_SECONDS", "60.0"
)
if CONSUMER_MAX_RETRIES < 1:
    raise ValueError("CONSUMER_MAX_RETRIES must be at least 1")
if CONSUMER_RETRY_MAX_SECONDS < CONSUMER_RETRY_BASE_SECONDS:
    raise ValueError(
        "CONSUMER_RETRY_MAX_SECONDS must be at least CONSUMER_RETRY_BASE_SECONDS"
    )


def _validate_state_path(path_value: str, setting_name: str) -> None:
    path = PurePosixPath(path_value)
    log_directory = PurePosixPath(LOG_FILE_PATH).parent

    if path == log_directory or log_directory in path.parents:
        raise ValueError(
            f"{setting_name} must not be stored in the read-only log mount"
        )


_validate_state_path(CHECKPOINT_FILE_PATH, "CHECKPOINT_FILE_PATH")
_validate_state_path(INBOX_DIRECTORY_PATH, "INBOX_DIRECTORY_PATH")
_validate_state_path(CONSUMER_STATE_DIRECTORY_PATH, "CONSUMER_STATE_DIRECTORY_PATH")
_validate_state_path(CONSUMER_LOCK_FILE_PATH, "CONSUMER_LOCK_FILE_PATH")
_validate_state_path(LOCK_FILE_PATH, "LOCK_FILE_PATH")
_validate_state_path(REJECTED_FILE_PATH, "REJECTED_FILE_PATH")

if len(
    {
        CHECKPOINT_FILE_PATH,
        INBOX_DIRECTORY_PATH,
        CONSUMER_STATE_DIRECTORY_PATH,
        CONSUMER_LOCK_FILE_PATH,
        LOCK_FILE_PATH,
        REJECTED_FILE_PATH,
    }
) != 6:
    raise ValueError(
        "Checkpoint, Inbox, consumer state/lock, watcher lock, and rejected "
        "records must use separate paths"
    )

def get_mysql_config(
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, object]:
    """Return application DB settings, rejecting missing or unsafe credentials."""
    settings = os.environ if environ is None else environ
    password = settings.get("MYSQL_PASSWORD", "").strip()
    if not password:
        raise ValueError(
            "MYSQL_PASSWORD is required for the Pipeline database connection"
        )

    user = settings.get("MYSQL_USER", "honeypot_pipeline").strip()
    if not user:
        raise ValueError("MYSQL_USER must not be empty")
    if user.lower() == "root":
        raise ValueError("Pipeline must connect with a non-root MYSQL_USER")

    return {
        "host": settings.get("MYSQL_HOST", "database").strip() or "database",
        "port": _mysql_port(settings),
        "user": user,
        "password": password,
        "database": settings.get("MYSQL_DATABASE", "ai_webhoneypot_db").strip()
        or "ai_webhoneypot_db",
    }


AI_API_URL = _env_or_default("AI_API_URL", "http://ai_engine:8000/analyze")
LOG_FORMAT = _env_or_default("LOG_FORMAT", "jsonl").lower()

# Giữ cấu hình SMTP để tương thích với email_alert.py.
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "your_email@gmail.com"
SENDER_PASSWORD = "your_app_password"
RECEIVER_EMAIL = "admin_soc@gmail.com"
