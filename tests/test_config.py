import os

import pytest


def test_telegram_user_id_is_int():
    from lib.config import TELEGRAM_USER_ID

    assert isinstance(TELEGRAM_USER_ID, int)


def test_required_keys_present():
    from lib.config import ANTHROPIC_KEY, TELEGRAM_BOT_KEY, TODOIST_KEY

    assert TELEGRAM_BOT_KEY
    assert TODOIST_KEY
    assert ANTHROPIC_KEY


def test_missing_env_var_raises():
    original = os.environ.pop("TELEGRAM_BOT_KEY", None)
    try:
        import lib.config as cfg

        with pytest.raises(RuntimeError, match="TELEGRAM_BOT_KEY"):
            cfg._require("TELEGRAM_BOT_KEY")
    finally:
        if original is not None:
            os.environ["TELEGRAM_BOT_KEY"] = original


def test_default_poll_seconds():
    from lib.config import OBSIDIAN_POLL_SECONDS

    assert OBSIDIAN_POLL_SECONDS == int(os.environ.get("OBSIDIAN_POLL_SECONDS", "300"))


def test_user_settings_defaults():
    from lib.config import UserSettings

    s = UserSettings()
    assert s.timezone == "UTC"
    assert s.first_day_of_week == 0
    assert s.time_format_24h is True
    assert s.stale_task_days == 3
