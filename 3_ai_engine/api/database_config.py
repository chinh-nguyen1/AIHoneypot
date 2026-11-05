"""Environment-backed MySQL settings for AI Engine database consumers."""

import os
from typing import Mapping, Optional


def get_mysql_config(
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, object]:
    settings = os.environ if environ is None else environ
    password = settings.get("MYSQL_PASSWORD", "").strip()
    if not password:
        raise ValueError(
            "MYSQL_PASSWORD is required for the AI Engine database connection"
        )

    user = settings.get("MYSQL_USER", "honeypot_pipeline").strip()
    if not user:
        raise ValueError("MYSQL_USER must not be empty")
    if user.lower() == "root":
        raise ValueError("AI Engine must connect with a non-root MYSQL_USER")

    return {
        "host": settings.get("MYSQL_HOST", "database").strip() or "database",
        "port": _mysql_port(settings),
        "user": user,
        "password": password,
        "database": settings.get("MYSQL_DATABASE", "ai_webhoneypot_db").strip()
        or "ai_webhoneypot_db",
    }


def _mysql_port(environ: Mapping[str, str]) -> int:
    value = environ.get("MYSQL_PORT", "3306").strip()
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError("MYSQL_PORT must be an integer between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError("MYSQL_PORT must be an integer between 1 and 65535")
    return port
