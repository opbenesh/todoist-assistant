from __future__ import annotations

from unittest.mock import MagicMock, patch

from lib.models import PRIORITY_TO_TODOIST, Task
from lib.todoist import build_user_settings, create_todoist_task, get_user_settings

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
