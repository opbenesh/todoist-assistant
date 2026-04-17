from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lib.config import UserSettings

# ---------------------------------------------------------------------------
# stale_nudge_job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_nudge_does_not_send_when_no_overdue():
    from lib.scheduler import stale_nudge_job

    context = MagicMock()
    context.bot.send_message = AsyncMock()

    with patch("lib.scheduler.todoist.get_overdue_tasks", return_value=[]):
        await stale_nudge_job(context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_nudge_sends_when_overdue():
    from lib.scheduler import stale_nudge_job

    context = MagicMock()
    context.bot.send_message = AsyncMock()

    overdue = [
        {
            "id": "2",
            "title": "Old task",
            "priority": "p2",
            "is_completed": False,
            "due_date": "2026-03-01",
            "duration_minutes": None,
        }
    ]

    with (
        patch("lib.scheduler.todoist.get_overdue_tasks", return_value=overdue),
        patch(
            "lib.scheduler.llm.generate_nudge",
            new=AsyncMock(return_value="You have overdue tasks!"),
        ),  # noqa: E501
    ):
        await stale_nudge_job(context)

    context.bot.send_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# Scheduler uses correct timezone
# ---------------------------------------------------------------------------


def test_scheduler_uses_user_timezone():

    from lib.scheduler import configure

    settings = UserSettings(timezone="Asia/Tokyo")
    configure(settings)

    import lib.scheduler as sched

    assert sched._settings.timezone == "Asia/Tokyo"


# ---------------------------------------------------------------------------
# Weekly review writes to vault
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weekly_review_writes_insight():
    from lib.scheduler import weekly_review_job

    context = MagicMock()
    context.bot.send_message = AsyncMock()

    with (
        patch("lib.scheduler.todoist.get_today_tasks", return_value=[]),
        patch("lib.scheduler.todoist.get_overdue_tasks", return_value=[]),
        patch(
            "lib.scheduler.llm.generate_weekly_review",
            new=AsyncMock(return_value="# Weekly Review"),
        ),  # noqa: E501
        patch("lib.scheduler.obsidian.write_insight") as mock_write,
    ):
        await weekly_review_job(context)

    mock_write.assert_called_once()
    filename, content = mock_write.call_args.args
    assert filename.startswith("weekly-")
    assert "Weekly Review" in content
