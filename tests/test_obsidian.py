from __future__ import annotations

import time
from datetime import date
from unittest.mock import patch

from lib.obsidian import (
    PRIORITY_EMOJI,
    _atomic_write,
    _extract_task_lines,
    _time_section,
    build_tasks_section,
    format_task_line,
    parse_task_line,
    write_tasks_section,
)

# ---------------------------------------------------------------------------
# parse_task_line
# ---------------------------------------------------------------------------


def test_parse_unchecked():
    assert parse_task_line("- [ ] Buy milk") == ("Buy milk", False)


def test_parse_checked():
    assert parse_task_line("- [x] Pay rent") == ("Pay rent", True)


def test_parse_with_metadata_old_format():
    assert parse_task_line("- [ ] Call doctor  (p1, 30min)") == ("Call doctor", False)


def test_parse_with_metadata_new_format():
    assert parse_task_line("- [ ] Call doctor 🔴  (30min)") == ("Call doctor", False)


def test_parse_invalid_line():
    assert parse_task_line("Just some text") is None


def test_parse_invalid_line_header():
    assert parse_task_line("## Tasks") is None


def test_parse_strips_project_prefix():
    assert parse_task_line("- [ ] [Work] Review PR 🟠  (30min)") == ("Review PR", False)


def test_parse_strips_emoji_no_project():
    assert parse_task_line("- [ ] Pay rent 🔴") == ("Pay rent", False)


def test_parse_p4_no_emoji_unchanged():
    assert parse_task_line("- [ ] Someday task") == ("Someday task", False)


def test_parse_section_header_returns_none():
    assert parse_task_line("### 🌅 Morning") is None
    assert parse_task_line("### ☀️ Afternoon") is None


# ---------------------------------------------------------------------------
# format_task_line
# ---------------------------------------------------------------------------


def test_format_unchecked_no_meta():
    assert format_task_line("Buy milk", False) == "- [ ] Buy milk"


def test_format_checked():
    assert format_task_line("Buy milk", True) == "- [x] Buy milk"


def test_format_with_priority_and_duration():
    line = format_task_line("Review PR", False, priority="p2", duration=30)
    assert "🟠" in line
    assert "30min" in line
    assert "p2" not in line


def test_format_p4_omits_priority():
    line = format_task_line("Someday task", False, priority="p4")
    assert "p4" not in line
    assert "🔴" not in line
    assert "🟠" not in line
    assert "🟡" not in line


def test_format_with_project_prefix():
    line = format_task_line("Review PR", False, priority="p2", project="Work")
    assert line.startswith("- [ ] [Work] Review PR")
    assert "🟠" in line


def test_format_no_project_no_prefix():
    line = format_task_line("Pay rent", False, priority="p1", project=None)
    assert "🔴" in line
    assert "] Pay rent" in line  # no [Project] prefix between checkbox and title


def test_format_p4_no_emoji_no_meta():
    assert format_task_line("Someday task", False, priority="p4") == "- [ ] Someday task"


def test_format_with_project_and_duration():
    line = format_task_line("Deep work", False, priority="p3", duration=90, project="Research")
    assert "[Research]" in line
    assert "🟡" in line
    assert "90min" in line


# ---------------------------------------------------------------------------
# PRIORITY_EMOJI constant
# ---------------------------------------------------------------------------


def test_priority_emoji_p4_is_empty():
    assert PRIORITY_EMOJI["p4"] == ""


def test_priority_emoji_p1_is_red():
    assert PRIORITY_EMOJI["p1"] == "🔴"


# ---------------------------------------------------------------------------
# _extract_task_lines
# ---------------------------------------------------------------------------


def test_extract_task_lines_basic():
    text = "# Monday\n\n## Tasks\n- [ ] Task A\n- [x] Task B\n\n## Notes\nsome note"
    lines = _extract_task_lines(text)
    assert lines == ["- [ ] Task A", "- [x] Task B"]


def test_extract_task_lines_no_section():
    text = "# Monday\n\nJust some content."
    assert _extract_task_lines(text) == []


def test_extract_task_lines_empty_section():
    text = "# Monday\n\n## Tasks\n\n## Notes\n"
    assert _extract_task_lines(text) == []


def test_extract_task_lines_includes_section_headers():
    text = "# Monday\n\n## Tasks\n### 🌅 Morning\n- [ ] Task A\n\n## Notes\n"
    lines = _extract_task_lines(text)
    assert "### 🌅 Morning" in lines
    assert "- [ ] Task A" in lines


# ---------------------------------------------------------------------------
# _time_section
# ---------------------------------------------------------------------------

_M = "09:00-12:00"
_A = "12:00-17:00"
_E = "17:00-21:00"


def test_time_section_none_is_unscheduled():
    assert _time_section(None, _M, _A, _E) == "Unscheduled"


def test_time_section_date_only_is_unscheduled():
    assert _time_section("2024-01-01", _M, _A, _E) == "Unscheduled"


def test_time_section_morning_start():
    assert _time_section("2024-01-01T09:00:00", _M, _A, _E) == "Morning"


def test_time_section_morning_mid():
    assert _time_section("2024-01-01T10:30:00", _M, _A, _E) == "Morning"


def test_time_section_morning_end_is_afternoon():
    assert _time_section("2024-01-01T12:00:00", _M, _A, _E) == "Afternoon"


def test_time_section_afternoon():
    assert _time_section("2024-01-01T14:00:00", _M, _A, _E) == "Afternoon"


def test_time_section_evening():
    assert _time_section("2024-01-01T17:00:00", _M, _A, _E) == "Evening"


def test_time_section_before_morning_is_unscheduled():
    assert _time_section("2024-01-01T08:00:00", _M, _A, _E) == "Unscheduled"


def test_time_section_after_evening_end_is_unscheduled():
    assert _time_section("2024-01-01T21:00:00", _M, _A, _E) == "Unscheduled"


def test_time_section_evening_last_minute():
    assert _time_section("2024-01-01T20:59:00", _M, _A, _E) == "Evening"


# ---------------------------------------------------------------------------
# build_tasks_section
# ---------------------------------------------------------------------------

_PROJECTS = {"p_work": "Work", "p_personal": "Personal", "p_inbox": "Inbox"}
_INBOX_ID = "p_inbox"


def _task(title, *, priority="p4", due=None, project_id="p_inbox", duration=None, completed=False):
    return {
        "title": title,
        "priority": priority,
        "due_date": due,
        "project_id": project_id,
        "duration_minutes": duration,
        "is_completed": completed,
    }


def test_build_empty_tasks():
    assert build_tasks_section([], _PROJECTS, _INBOX_ID, _M, _A, _E) == []


def test_build_all_unscheduled_no_header():
    lines = build_tasks_section([_task("A"), _task("B")], _PROJECTS, _INBOX_ID, _M, _A, _E)
    assert not any(line.startswith("###") for line in lines)
    assert len(lines) == 2


def test_build_timed_task_gets_section_header():
    tasks = [_task("Meeting", due="2024-01-01T10:00:00")]
    lines = build_tasks_section(tasks, _PROJECTS, _INBOX_ID, _M, _A, _E)
    assert "### 🌅 Morning" in lines
    assert any("Meeting" in ln for ln in lines)


def test_build_section_order():
    tasks = [
        _task("Evening task", due="2024-01-01T19:00:00"),
        _task("Morning task", due="2024-01-01T09:00:00"),
        _task("Unscheduled task"),
    ]
    lines = build_tasks_section(tasks, _PROJECTS, _INBOX_ID, _M, _A, _E)
    unscheduled_idx = next(i for i, ln in enumerate(lines) if "Unscheduled task" in ln)
    morning_idx = next(i for i, ln in enumerate(lines) if "Morning task" in ln)
    evening_idx = next(i for i, ln in enumerate(lines) if "Evening task" in ln)
    assert unscheduled_idx < morning_idx < evening_idx


def test_build_priority_sort_within_section():
    tasks = [_task("Low", priority="p4"), _task("High", priority="p1"), _task("Med", priority="p2")]
    lines = build_tasks_section(tasks, _PROJECTS, _INBOX_ID, _M, _A, _E)
    task_lines = [ln for ln in lines if ln.startswith("- ")]
    assert "High" in task_lines[0]
    assert "Med" in task_lines[1]
    assert "Low" in task_lines[2]


def test_build_inbox_task_no_prefix():
    tasks = [_task("Pay rent", project_id="p_inbox")]
    lines = build_tasks_section(tasks, _PROJECTS, _INBOX_ID, _M, _A, _E)
    assert all("[Inbox]" not in ln for ln in lines)


def test_build_non_inbox_task_gets_prefix():
    tasks = [_task("Write report", project_id="p_work")]
    lines = build_tasks_section(tasks, _PROJECTS, _INBOX_ID, _M, _A, _E)
    assert any("[Work]" in ln for ln in lines)


def test_build_empty_section_omitted():
    tasks = [_task("Evening task", due="2024-01-01T19:00:00")]
    lines = build_tasks_section(tasks, _PROJECTS, _INBOX_ID, _M, _A, _E)
    assert "### 🌅 Morning" not in lines
    assert "### ☀️ Afternoon" not in lines
    assert "### 🌙 Evening" in lines


def test_build_unknown_project_id_no_prefix():
    tasks = [_task("Orphan", project_id="unknown")]
    lines = build_tasks_section(tasks, _PROJECTS, _INBOX_ID, _M, _A, _E)
    task_line = next(ln for ln in lines if "Orphan" in ln)
    assert "] Orphan" in task_line  # no [ProjectName] prefix between checkbox and title


def test_build_inbox_id_none_shows_all_project_prefixes():
    tasks = [_task("Inbox task", project_id="p_inbox")]
    lines = build_tasks_section(
        tasks, _PROJECTS, inbox_id=None, morning_block=_M, afternoon_block=_A, evening_block=_E
    )
    task_line = next(ln for ln in lines if "Inbox task" in ln)
    assert "[Inbox]" in task_line


def test_build_completed_task_checked():
    tasks = [_task("Done", completed=True)]
    lines = build_tasks_section(tasks, _PROJECTS, _INBOX_ID, _M, _A, _E)
    assert any("- [x]" in ln for ln in lines)


# ---------------------------------------------------------------------------
# write_tasks_section
# ---------------------------------------------------------------------------


def test_write_creates_section_if_absent(tmp_path):
    note = tmp_path / "2026-04-02.md"
    note.write_text("# Thursday\n\n## Notes\nsome note\n", encoding="utf-8")

    with patch("lib.obsidian.daily_note_path", return_value=note):
        write_tasks_section(["- [ ] Task A"])

    result = note.read_text(encoding="utf-8")
    assert "## Tasks" in result
    assert "- [ ] Task A" in result
    assert "## Notes" in result  # existing content preserved


def test_write_replaces_existing_section(tmp_path):
    note = tmp_path / "2026-04-02.md"
    note.write_text("# Thursday\n\n## Tasks\n- [ ] Old task\n\n## Notes\nnote\n", encoding="utf-8")

    with patch("lib.obsidian.daily_note_path", return_value=note):
        write_tasks_section(["- [ ] New task"])

    result = note.read_text(encoding="utf-8")
    assert "Old task" not in result
    assert "New task" in result
    assert "## Notes" in result  # content after Tasks preserved


def test_write_creates_note_if_absent(tmp_path):
    note = tmp_path / "2026-05-01.md"

    with patch("lib.obsidian.daily_note_path", return_value=note):
        with patch("lib.obsidian.date") as mock_date:
            mock_date.today.return_value = date(2026, 5, 1)
            write_tasks_section(["- [ ] Task A"])

    assert note.exists()
    assert "## Tasks" in note.read_text(encoding="utf-8")


def test_write_preserves_section_headers(tmp_path):
    note = tmp_path / "2026-04-02.md"
    note.write_text("# Thursday\n\n## Notes\nnote\n", encoding="utf-8")
    lines = ["### 🌅 Morning", "- [ ] Standup 🔴", "### ☀️ Afternoon", "- [ ] Review PR 🟠"]

    with patch("lib.obsidian.daily_note_path", return_value=note):
        write_tasks_section(lines)

    result = note.read_text(encoding="utf-8")
    assert "### 🌅 Morning" in result
    assert "### ☀️ Afternoon" in result


def test_atomic_write_cleans_up_tmp(tmp_path):
    target = tmp_path / "file.md"
    _atomic_write(target, "content")
    assert target.exists()
    assert not (tmp_path / "file.tmp").exists()


# ---------------------------------------------------------------------------
# sync logic (unit-level)
# ---------------------------------------------------------------------------


def test_sync_adds_obsidian_task_to_todoist(tmp_path):
    from unittest.mock import MagicMock

    note = tmp_path / "2026-04-02.md"
    note.write_text("# Day\n\n## Tasks\n- [ ] New from obsidian\n", encoding="utf-8")

    mock_store = MagicMock()
    mock_store.last_sync_ts = 0.0

    with (
        patch("lib.sync.is_day_planned", return_value=True),
        patch("lib.sync.store", mock_store),
        patch("lib.sync.daily_note_path", return_value=note),
        patch("lib.sync.read_tasks_section", return_value=["- [ ] New from obsidian"]),
        patch("lib.sync.todoist.get_today_tasks", return_value=[]),
        patch("lib.sync.todoist.get_all_tasks", return_value=[]),
        patch("lib.sync.todoist.create_todoist_task", return_value="tid1") as mock_create,
        patch("lib.sync.write_tasks_section"),
    ):
        from lib.sync import _sync

        _sync()

    mock_create.assert_called_once()
    call_task = mock_create.call_args.args[0]
    assert call_task.title == "New from obsidian"


def test_sync_appends_todoist_task_to_note(tmp_path):
    from unittest.mock import MagicMock

    note = tmp_path / "2026-04-02.md"
    note.write_text("# Day\n\n## Tasks\n", encoding="utf-8")

    mock_store = MagicMock()
    mock_store.last_sync_ts = 0.0

    todoist_tasks = [
        {
            "id": "t1",
            "title": "From todoist",
            "is_completed": False,
            "priority": "p3",
            "due_date": None,
            "duration_minutes": None,
        }
    ]

    written = []

    with (
        patch("lib.sync.is_day_planned", return_value=True),
        patch("lib.sync.store", mock_store),
        patch("lib.sync.daily_note_path", return_value=note),
        patch("lib.sync.read_tasks_section", return_value=[]),
        patch("lib.sync.todoist.get_today_tasks", return_value=todoist_tasks),
        patch("lib.sync.todoist.get_all_tasks", return_value=todoist_tasks),
        patch("lib.sync.write_tasks_section", side_effect=lambda lines: written.extend(lines)),
    ):
        from lib.sync import _sync

        _sync()

    assert any("From todoist" in line for line in written)


def test_sync_obsidian_wins_when_note_newer(tmp_path):
    from unittest.mock import MagicMock

    note = tmp_path / "2026-04-02.md"
    note.write_text("# Day\n\n## Tasks\n- [x] Buy milk\n", encoding="utf-8")

    future_mtime = time.time() + 100
    note.touch()
    import os

    os.utime(note, (future_mtime, future_mtime))

    mock_store = MagicMock()
    mock_store.last_sync_ts = time.time() - 200  # note is newer

    todoist_tasks = [
        {
            "id": "t1",
            "title": "Buy milk",
            "is_completed": False,
            "priority": "p4",
            "due_date": None,
            "duration_minutes": None,
        }
    ]

    with (
        patch("lib.sync.is_day_planned", return_value=True),
        patch("lib.sync.store", mock_store),
        patch("lib.sync.daily_note_path", return_value=note),
        patch("lib.sync.read_tasks_section", return_value=["- [x] Buy milk"]),
        patch("lib.sync.todoist.get_today_tasks", return_value=todoist_tasks),
        patch("lib.sync.todoist.get_all_tasks", return_value=todoist_tasks),
        patch("lib.sync.todoist.batch_update_task_status") as mock_batch,
        patch("lib.sync.write_tasks_section"),
    ):
        from lib.sync import _sync

        _sync()

    mock_batch.assert_called_once_with(["t1"], [])
