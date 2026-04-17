from __future__ import annotations

from unittest.mock import MagicMock, patch

from lib.models import PRIORITY_TO_TODOIST, Task
from lib.todoist import (
    build_user_settings,
    complete_todoist_task,
    create_todoist_task,
    get_all_tasks,
    get_triage_tasks,
    get_user_settings,
    uncomplete_todoist_task,
)

# ---------------------------------------------------------------------------
# Priority mapping
# ---------------------------------------------------------------------------


def test_priority_mapping_p1():
    assert PRIORITY_TO_TODOIST["p1"] == 4


def test_priority_mapping_p4():
    assert PRIORITY_TO_TODOIST["p4"] == 1


def test_create_task_uses_correct_priority():
    task = Task(title="Test", priority="p1")
    mock_result = MagicMock()
    mock_result.id = "abc123"

    with patch("lib.todoist._api") as mock_api:
        mock_api.add_task.return_value = mock_result
        result_id = create_todoist_task(task)

    mock_api.add_task.assert_called_once()
    call_args = mock_api.add_task.call_args
    assert call_args.args[0] == "Test"  # content is positional
    assert call_args.kwargs["priority"] == 4  # p1 → 4
    assert result_id == "abc123"


def test_create_task_p4_priority():
    task = Task(title="Someday task", priority="p4")
    mock_result = MagicMock()
    mock_result.id = "xyz"

    with patch("lib.todoist._api") as mock_api:
        mock_api.add_task.return_value = mock_result
        create_todoist_task(task)

    call_kwargs = mock_api.add_task.call_args.kwargs
    assert call_kwargs["priority"] == 1  # p4 → 1


def test_create_task_with_due_date():
    from datetime import date

    task = Task(title="Event", due_date=date(2026, 5, 1))
    mock_result = MagicMock()
    mock_result.id = "1"

    with patch("lib.todoist._api") as mock_api:
        mock_api.add_task.return_value = mock_result
        create_todoist_task(task)

    call_kwargs = mock_api.add_task.call_args.kwargs
    assert call_kwargs["due_date"] == date(2026, 5, 1)  # v4 takes date object


# ---------------------------------------------------------------------------
# User settings
# ---------------------------------------------------------------------------


def test_get_user_settings_returns_dict_on_success():
    mock_response = MagicMock()
    mock_response.json.return_value = {"timezone": "Europe/Amsterdam", "lang": "en"}
    mock_response.raise_for_status = MagicMock()

    with patch("lib.todoist.httpx.get", return_value=mock_response):
        result = get_user_settings()

    assert result["timezone"] == "Europe/Amsterdam"


def test_get_user_settings_returns_empty_on_failure():
    with patch("lib.todoist.httpx.get", side_effect=Exception("network error")):
        result = get_user_settings()

    assert result == {}


def test_build_user_settings_from_todoist_data():
    # v1 API: timezone is a top-level key, no start_day/time_format
    todoist_data = {"timezone": "America/New_York"}
    settings = build_user_settings(todoist_data, {})
    assert settings.timezone == "America/New_York"
    # start_day / time_format fall back to profile defaults
    assert settings.first_day_of_week == 0
    assert settings.time_format_24h is True


def test_build_user_settings_falls_back_to_profile():
    profile = {
        "timezone": "Asia/Tokyo",
        "first_day_of_week": "monday",
        "time_format": "24h",
        "stale_task_days": "5",
    }
    settings = build_user_settings({}, profile)
    assert settings.timezone == "Asia/Tokyo"
    assert settings.first_day_of_week == 0
    assert settings.time_format_24h is True
    assert settings.stale_task_days == 5


def test_build_user_settings_defaults_when_both_empty():
    settings = build_user_settings({}, {})
    assert settings.timezone == "UTC"
    assert settings.first_day_of_week == 0
    assert settings.default_project == "Inbox"


# ---------------------------------------------------------------------------
# Deduplication bugs
# ---------------------------------------------------------------------------


def _make_mock_task(
    task_id: str = "T1",
    title: str = "Test task",
    is_completed: bool = False,
    labels: list[str] | None = None,
    is_recurring: bool = False,
    due_string: str | None = None,
) -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.content = title
    t.description = ""
    t.priority = 1
    t.labels = labels or []
    t.deadline = None
    t.duration = None
    t.is_completed = is_completed
    if is_recurring or due_string:
        due = MagicMock()
        due.date = None
        due.is_recurring = is_recurring
        due.string = due_string or "every day"
        t.due = due
    else:
        t.due = None
    return t


def test_get_all_tasks_deduplicates_across_pages(mock_todoist_api):
    """get_all_tasks should return each task only once even if the paginator
    yields the same task on multiple pages.

    BUG: get_all_tasks() has no deduplication — unlike get_triage_tasks() which
    guards with a `seen` set. When the Todoist SDK paginator returns the same task
    on more than one page (possible for tasks that appear in multiple views or
    during pagination boundary races), the optimize flow queues the task N times,
    breaks it down N times, and produces N copies of the same sub-tasks.
    """
    task = _make_mock_task(task_id="dup-1", title="לשלם ליאיר!")
    # Simulate the same task appearing on two pages of the paginator
    mock_todoist_api.get_tasks.return_value = [[task], [task]]
    result = get_all_tasks()

    ids = [t["id"] for t in result]
    assert ids.count("dup-1") == 1, (
        f"Expected task 'dup-1' once, got {ids.count('dup-1')} copies. "
        "get_all_tasks() does not deduplicate across pages, causing the optimize "
        "flow to process and break down the same task multiple times."
    )


def test_get_triage_tasks_excludes_completed(mock_todoist_api):
    """get_triage_tasks should never surface a completed task.

    BUG: _task_to_dict preserves is_completed from the API response and
    get_triage_tasks() has no guard filtering it out. If the Todoist filter
    endpoint ever returns a completed task (e.g., timing window, API quirk,
    or a recurring-task edge case), it passes straight through to the triage
    UI and the user sees 'items they've already completed' in their plan session.
    """
    completed = _make_mock_task(task_id="done-1", title="Already done", is_completed=True)
    mock_todoist_api.filter_tasks.return_value = [[completed]]
    result = get_triage_tasks()

    completed_in_result = [t for t in result if t["is_completed"]]
    assert completed_in_result == [], (
        f"Expected no completed tasks in triage, got {completed_in_result}. "
        "get_triage_tasks() does not filter is_completed=True tasks."
    )


# ---------------------------------------------------------------------------
# Recurring task handling
# ---------------------------------------------------------------------------


def test_task_to_dict_captures_is_recurring(mock_todoist_api):
    """_task_to_dict must propagate is_recurring=True from t.due.is_recurring.

    Without this field, triage and other flows cannot distinguish recurring tasks
    from one-offs, leading to destructive actions (removing recurrence via postpone,
    or deleting the entire series via delete).
    """
    from lib.todoist import _task_to_dict

    task = _make_mock_task(
        task_id="r1", title="Water the plants", is_recurring=True, due_string="every day"
    )
    result = _task_to_dict(task)

    assert result["is_recurring"] is True, (
        f"Expected is_recurring=True for a recurring task, got {result['is_recurring']!r}"
    )
    assert result["due_string"] == "every day", (
        f"Expected due_string='every day', got {result['due_string']!r}"
    )


def test_task_to_dict_non_recurring_defaults_false(mock_todoist_api):
    """_task_to_dict must set is_recurring=False for a non-recurring task with a due date."""
    from lib.todoist import _task_to_dict

    task = _make_mock_task(task_id="nr1", title="Pay bill")
    due = MagicMock()
    due.date = None
    due.is_recurring = False
    due.string = "today"
    task.due = due

    result = _task_to_dict(task)

    assert result["is_recurring"] is False, (
        f"Expected is_recurring=False for a non-recurring task, got {result['is_recurring']!r}"
    )


def test_task_to_dict_no_due_is_not_recurring(mock_todoist_api):
    """_task_to_dict must set is_recurring=False when there is no due date at all."""
    from lib.todoist import _task_to_dict

    task = _make_mock_task(task_id="nd1", title="Someday task")
    result = _task_to_dict(task)

    assert result["is_recurring"] is False
    assert result["due_string"] is None


# ---------------------------------------------------------------------------
# SDK contract tests — catch method renames at test time
# ---------------------------------------------------------------------------


def test_complete_todoist_task_calls_sdk_complete(mock_todoist_api):
    """complete_todoist_task must call _api.complete_task, not close_task or any other name.

    Using a spec-bound mock means accessing a nonexistent method raises AttributeError,
    so a future SDK rename would fail here before reaching production.
    """
    complete_todoist_task("t1")
    mock_todoist_api.complete_task.assert_called_once_with("t1")


def test_uncomplete_todoist_task_calls_sdk_uncomplete(mock_todoist_api):
    """uncomplete_todoist_task must call _api.uncomplete_task."""
    uncomplete_todoist_task("t1")
    mock_todoist_api.uncomplete_task.assert_called_once_with("t1")
