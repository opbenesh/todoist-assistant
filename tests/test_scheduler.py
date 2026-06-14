from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lib.config import UserSettings
from lib.models import TaskStore

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


def test_plan_nag_jitter_applied():
    """plan_nag first-fire time uses a random jitter so it's not always :00."""
    from datetime import time
    from unittest.mock import MagicMock, patch
    from zoneinfo import ZoneInfo

    import lib.scheduler as sched

    settings = UserSettings(timezone="UTC")
    sched.configure(settings)

    mock_jq = MagicMock()
    mock_app = MagicMock()
    mock_app.job_queue = mock_jq

    # Force a known jitter: 7 minutes and 43 seconds = 463 seconds
    with patch("lib.scheduler.random.randint", return_value=463):
        sched.attach_scheduler(mock_app)

    # Find the run_repeating call for plan_nag
    nag_call = next(
        c for c in mock_jq.run_repeating.call_args_list if c.kwargs.get("name") == "plan_nag"
    )
    tz = ZoneInfo("UTC")
    assert nag_call.kwargs["first"] == time(10, 7, 43, tzinfo=tz)


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


# ---------------------------------------------------------------------------
# _check_todoist_changes
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path, known: dict | None = None) -> TaskStore:
    s = TaskStore(path=tmp_path / "state.json")
    if known is not None:
        s._data["known_active_ids"] = known
        s.save()
    return s


def test_check_todoist_changes_first_run(tmp_path: Path) -> None:
    """First run (snapshot is None): builds snapshot, returns no changes."""
    from lib.scheduler import _check_todoist_changes

    store = _make_store(tmp_path)  # known_active_ids key absent
    active = [{"id": "t1", "title": "Task A"}, {"id": "t2", "title": "Task B"}]

    with (
        patch("lib.scheduler.store", store),
        patch("lib.scheduler.todoist.get_all_tasks", return_value=active),
    ):
        completed, deleted, new_tasks = _check_todoist_changes()

    assert completed == []
    assert deleted == []
    assert new_tasks == []
    assert store.known_active_ids == {"t1": "Task A", "t2": "Task B"}


def test_check_todoist_changes_no_changes(tmp_path: Path) -> None:
    """Snapshot matches active tasks → nothing reported."""
    from lib.scheduler import _check_todoist_changes

    store = _make_store(tmp_path, known={"t1": "Task A"})
    active = [{"id": "t1", "title": "Task A"}]

    with (
        patch("lib.scheduler.store", store),
        patch("lib.scheduler.todoist.get_all_tasks", return_value=active),
    ):
        completed, deleted, new_tasks = _check_todoist_changes()

    assert completed == []
    assert deleted == []
    assert new_tasks == []


def test_check_todoist_changes_external_completion(tmp_path: Path) -> None:
    """Task in snapshot but not active, found in completed list → 'Completed'."""
    from lib.scheduler import _check_todoist_changes

    store = _make_store(tmp_path, known={"t1": "Buy milk"})

    with (
        patch("lib.scheduler.store", store),
        patch("lib.scheduler.todoist.get_all_tasks", return_value=[]),
        patch(
            "lib.scheduler.todoist.get_completed_tasks",
            return_value=[{"id": "t1", "title": "Buy milk"}],
        ),
    ):
        completed, deleted, new_tasks = _check_todoist_changes()

    assert completed == ["Buy milk"]
    assert deleted == []
    assert new_tasks == []


def test_check_todoist_changes_external_deletion(tmp_path: Path) -> None:
    """Task in snapshot but not active or completed → 'Deleted'."""
    from lib.scheduler import _check_todoist_changes

    store = _make_store(tmp_path, known={"t1": "Old task"})

    with (
        patch("lib.scheduler.store", store),
        patch("lib.scheduler.todoist.get_all_tasks", return_value=[]),
        patch("lib.scheduler.todoist.get_completed_tasks", return_value=[]),
    ):
        completed, deleted, new_tasks = _check_todoist_changes()

    assert completed == []
    assert deleted == ["Old task"]
    assert new_tasks == []


def test_check_todoist_changes_new_task(tmp_path: Path) -> None:
    """Task in active but not in snapshot → 'New'."""
    from lib.scheduler import _check_todoist_changes

    store = _make_store(tmp_path, known={})  # initialized but empty
    active = [{"id": "t2", "title": "New task"}]

    with (
        patch("lib.scheduler.store", store),
        patch("lib.scheduler.todoist.get_all_tasks", return_value=active),
    ):
        completed, deleted, new_tasks = _check_todoist_changes()

    assert new_tasks == ["New task"]
    assert completed == []
    assert deleted == []


def test_check_todoist_changes_recurring_no_double_notify(tmp_path: Path) -> None:
    """Recurring task completed + re-opened → only 'Completed', not 'New'."""
    from lib.scheduler import _check_todoist_changes

    store = _make_store(tmp_path, known={"t_old": "Stand-up"})
    # New recurring instance has a different ID but same title
    active = [{"id": "t_new", "title": "Stand-up"}]

    with (
        patch("lib.scheduler.store", store),
        patch("lib.scheduler.todoist.get_all_tasks", return_value=active),
        patch(
            "lib.scheduler.todoist.get_completed_tasks",
            return_value=[{"id": "t_old", "title": "Stand-up"}],
        ),
    ):
        completed, deleted, new_tasks = _check_todoist_changes()

    assert completed == ["Stand-up"]
    assert deleted == []
    assert new_tasks == []  # recurring re-open suppressed


def test_check_todoist_changes_bot_completed_not_reported(tmp_path: Path) -> None:
    """Bot-completed task (removed from snapshot via remove_known_task) is not reported."""
    from lib.scheduler import _check_todoist_changes

    store = _make_store(tmp_path, known={"t1": "Task A", "t2": "Task B"})
    # Bot completed t1 via /done → removed from snapshot
    store.remove_known_task("t1")

    # Active tasks: only t2 (t1 is completed but not in snapshot)
    active = [{"id": "t2", "title": "Task B"}]

    with (
        patch("lib.scheduler.store", store),
        patch("lib.scheduler.todoist.get_all_tasks", return_value=active),
    ):
        completed, deleted, new_tasks = _check_todoist_changes()

    assert completed == []
    assert deleted == []
    assert new_tasks == []


@pytest.mark.asyncio
async def test_todoist_sync_job_sends_message() -> None:
    """todoist_sync_job sends a Telegram message when changes are detected."""
    from lib.scheduler import todoist_sync_job

    context = MagicMock()
    context.bot.send_message = AsyncMock()

    with patch(
        "lib.scheduler._check_todoist_changes",
        return_value=(["Buy milk"], [], []),
    ):
        await todoist_sync_job(context)

    context.bot.send_message.assert_awaited_once()
    text = context.bot.send_message.call_args.kwargs["text"]
    assert "Completed in Todoist: Buy milk" in text


@pytest.mark.asyncio
async def test_todoist_sync_job_silent_when_no_changes() -> None:
    """todoist_sync_job sends nothing when there are no external changes."""
    from lib.scheduler import todoist_sync_job

    context = MagicMock()
    context.bot.send_message = AsyncMock()

    with patch("lib.scheduler._check_todoist_changes", return_value=([], [], [])):
        await todoist_sync_job(context)

    context.bot.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# plan_nag_job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_nag_job_does_nothing_if_day_planned() -> None:
    from lib.scheduler import plan_nag_job

    context = MagicMock()
    context.bot.send_message = AsyncMock()

    with patch("lib.scheduler.obsidian.is_day_planned", return_value=True):
        await plan_nag_job(context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_nag_job_recently_active_suppressed() -> None:
    import time

    from lib.scheduler import plan_nag_job

    context = MagicMock()
    context.bot.send_message = AsyncMock()

    # Session active 5 minutes ago
    session_data = {"last_user_action_ts": time.time() - 300, "nudge_sent": False}

    with (
        patch("lib.scheduler.obsidian.is_day_planned", return_value=False),
        patch("lib.scheduler.store.load_plan_session", return_value=(0, session_data)),
    ):
        await plan_nag_job(context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_nag_job_sends_session_in_progress_nudge_once() -> None:
    import time

    from lib.scheduler import plan_nag_job

    context = MagicMock()
    context.bot.send_message = AsyncMock()

    # Session active 20 minutes ago, nudge not yet sent
    session_data = {"last_user_action_ts": time.time() - 1200, "nudge_sent": False}

    with (
        patch("lib.scheduler.obsidian.is_day_planned", return_value=False),
        patch("lib.scheduler.store.load_plan_session", return_value=(0, session_data)),
        patch("lib.scheduler.store.save_plan_session") as mock_save,
    ):
        await plan_nag_job(context)

    context.bot.send_message.assert_awaited_once()
    assert "planning session in progress" in context.bot.send_message.call_args.kwargs["text"]
    mock_save.assert_called_once_with(0, session_data)
    assert session_data["nudge_sent"] is True


@pytest.mark.asyncio
async def test_plan_nag_job_suppresses_generic_nag_when_already_nudged() -> None:
    import time

    from lib.scheduler import plan_nag_job

    context = MagicMock()
    context.bot.send_message = AsyncMock()

    # Session active 20 minutes ago, nudge already sent
    session_data = {"last_user_action_ts": time.time() - 1200, "nudge_sent": True}

    with (
        patch("lib.scheduler.obsidian.is_day_planned", return_value=False),
        patch("lib.scheduler.store.load_plan_session", return_value=(0, session_data)),
        patch("lib.scheduler.store.save_plan_session") as mock_save,
    ):
        await plan_nag_job(context)

    # Should return early and NOT send any message (neither planning session nudge nor generic nag)
    context.bot.send_message.assert_not_awaited()
    mock_save.assert_not_called()


@pytest.mark.asyncio
async def test_plan_nag_job_sends_generic_nag_during_daytime_when_no_session() -> None:
    from datetime import datetime

    from lib.scheduler import plan_nag_job

    context = MagicMock()
    context.bot.send_message = AsyncMock()

    # Mock datetime to return 14:00 (inside 9-21 daytime window)
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = datetime(2026, 6, 6, 14, 0, 0)

    with (
        patch("lib.scheduler.obsidian.is_day_planned", return_value=False),
        patch("lib.scheduler.store.load_plan_session", return_value=None),
        patch("lib.scheduler.datetime", mock_dt),
    ):
        await plan_nag_job(context)

    context.bot.send_message.assert_awaited_once()
    assert "still haven't planned your day" in context.bot.send_message.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_plan_nag_job_auto_expires_stale_session() -> None:
    import time

    from lib.scheduler import plan_nag_job

    context = MagicMock()
    context.bot.send_message = AsyncMock()

    # Session active 5 days ago (stale)
    stale_ts = time.time() - (5 * 24 * 3600)
    session_data = {"last_user_action_ts": stale_ts, "nudge_sent": False}

    # Mock datetime to return 14:00 (inside 9-21 daytime window)
    from datetime import datetime

    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = datetime(2026, 6, 6, 14, 0, 0)

    with (
        patch("lib.scheduler.obsidian.is_day_planned", return_value=False),
        patch("lib.scheduler.store.load_plan_session", return_value=(0, session_data)),
        patch("lib.scheduler.store.clear_plan_session") as mock_clear,
        patch("lib.scheduler.store.save") as mock_save,
        patch("lib.scheduler.datetime", mock_dt),
    ):
        await plan_nag_job(context)

    # Stale session should be cleared and saved
    mock_clear.assert_called_once()
    mock_save.assert_called_once()
    # Stale session shouldn't suppress the generic nag, so we should get one
    context.bot.send_message.assert_awaited_once()
    assert "still haven't planned your day" in context.bot.send_message.call_args.kwargs["text"]
