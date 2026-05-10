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
    active = [{"id": "t2", "title": "New task", "project_id": "p1"}]

    with (
        patch("lib.scheduler.store", store),
        patch("lib.scheduler.todoist.get_all_tasks", return_value=active),
    ):
        completed, deleted, new_tasks = _check_todoist_changes()

    assert new_tasks == [{"id": "t2", "title": "New task", "project_id": "p1"}]
    assert completed == []
    assert deleted == []


def test_check_todoist_changes_recurring_no_double_notify(tmp_path: Path) -> None:
    """Recurring task completed + re-opened → only 'Completed', not 'New'."""
    from lib.scheduler import _check_todoist_changes

    store = _make_store(tmp_path, known={"t_old": "Stand-up"})
    # New recurring instance has a different ID but same title
    active = [{"id": "t_new", "title": "Stand-up", "project_id": "p1"}]

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


@pytest.mark.asyncio
async def test_todoist_sync_job_groups_new_tasks_by_project() -> None:
    """Multiple new tasks from the same project appear on one line, not separately."""
    from lib.scheduler import todoist_sync_job

    context = MagicMock()
    context.bot.send_message = AsyncMock()

    new_tasks = [
        {"id": "t1", "title": "Step 1", "project_id": "p_work"},
        {"id": "t2", "title": "Step 2", "project_id": "p_work"},
        {"id": "t3", "title": "Personal thing", "project_id": "p_personal"},
    ]
    projects = {"p_work": "Work", "p_personal": "Personal"}

    with (
        patch("lib.scheduler._check_todoist_changes", return_value=([], [], new_tasks)),
        patch("lib.scheduler.todoist.get_all_projects", return_value=projects),
    ):
        await todoist_sync_job(context)

    context.bot.send_message.assert_awaited_once()
    text = context.bot.send_message.call_args.kwargs["text"]
    # Work tasks grouped on one line
    assert "New in Todoist (Work): Step 1, Step 2" in text
    # Personal task on its own line
    assert "New in Todoist (Personal): Personal thing" in text
    # Only 2 lines total for new tasks (not 3 separate items)
    new_lines = [line for line in text.splitlines() if "New in Todoist" in line]
    assert len(new_lines) == 2


@pytest.mark.asyncio
async def test_todoist_sync_job_single_new_task_shows_project() -> None:
    """A single new task is shown with its project name."""
    from lib.scheduler import todoist_sync_job

    context = MagicMock()
    context.bot.send_message = AsyncMock()

    new_tasks = [{"id": "t1", "title": "Buy milk", "project_id": "p_inbox"}]
    projects = {"p_inbox": "Inbox"}

    with (
        patch("lib.scheduler._check_todoist_changes", return_value=([], [], new_tasks)),
        patch("lib.scheduler.todoist.get_all_projects", return_value=projects),
    ):
        await todoist_sync_job(context)

    text = context.bot.send_message.call_args.kwargs["text"]
    assert "New in Todoist (Inbox): Buy milk" in text
