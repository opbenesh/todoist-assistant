from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(f"Required environment variable {key!r} is not set")
    return value


TELEGRAM_BOT_KEY: str = _require("TELEGRAM_BOT_KEY")
TODOIST_KEY: str = _require("TODOIST_KEY")
ANTHROPIC_KEY: str = _require("ANTHROPIC_KEY")
TELEGRAM_USER_ID: int = int(_require("TELEGRAM_USER_ID"))
VAULT_PATH: str = os.environ.get("VAULT_PATH", "/home/ubuntu/vault")


def _parse_int(key: str, default: int) -> int:
    raw = os.environ.get(key, str(default))
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"Environment variable {key!r} must be an integer, got {raw!r}")


OBSIDIAN_POLL_SECONDS: int = _parse_int("OBSIDIAN_POLL_SECONDS", 300)
OBSIDIAN_SYNC_FIRST_SECONDS: int = _parse_int("OBSIDIAN_SYNC_FIRST_SECONDS", 10)
DEBUG_LOGGING: bool = os.environ.get("DEBUG_LOGGING", "false").lower() in ("1", "true", "yes")

# Staging / testing overrides — point at fake service servers
TODOIST_BASE_URL: str = os.environ.get("TODOIST_BASE_URL", "https://api.todoist.com")
ANTHROPIC_BASE_URL: str | None = os.environ.get("ANTHROPIC_BASE_URL")
TELEGRAM_API_BASE_URL: str | None = os.environ.get("TELEGRAM_API_BASE_URL")
STATE_PATH: str | None = os.environ.get("STATE_PATH")


@dataclass
class UserSettings:
    timezone: str = "UTC"
    first_day_of_week: int = 0  # 0=Monday, 6=Sunday
    time_format_24h: bool = True
    work_start: str = "09:00"
    work_end: str = "18:00"
    morning_block: str = "09:00-12:00"
    afternoon_block: str = "12:00-17:00"
    evening_block: str = "17:00-21:00"
    default_project: str = "Inbox"
    stale_task_days: int = 3
