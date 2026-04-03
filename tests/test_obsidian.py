from __future__ import annotations

import time
from datetime import date
from unittest.mock import patch

from lib.obsidian import (
    _atomic_write,
    _extract_task_lines,
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


def test_parse_with_metadata():
    assert parse_task_line("- [ ] Call doctor  (p1, 30min)") == ("Call doctor", False)


def test_parse_invalid_line():
    assert parse_task_line("Just some text") is None


def test_parse_invalid_line_header():
    assert parse_task_line("## Tasks") is None


# ---------------------------------------------------------------------------
# format_task_line
# ---------------------------------------------------------------------------


def test_format_unchecked_no_meta():
    assert format_task_line("Buy milk", False) == "- [ ] Buy milk"


def test_format_checked():
    assert format_task_line("Buy milk", True) == "- [x] Buy milk"


def test_format_with_priority_and_duration():
    line = format_task_line("Review PR", False, priority="p2", duration=30)
    assert "p2" in line
    assert "30min" in line


def test_format_p4_omits_priority():
    line = format_task_line("Someday task", False, priority="p4")
    assert "p4" not in line


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
        patch("lib.sync._store", mock_store),
        patch("lib.sync.daily_note_path", return_value=note),
        patch("lib.sync.read_tasks_section", return_value=["- [ ] New from obsidian"]),
        patch("lib.sync.todoist.get_today_tasks", return_value=[]),
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
        patch("lib.sync._store", mock_store),
        patch("lib.sync.daily_note_path", return_value=note),
        patch("lib.sync.read_tasks_section", return_value=[]),
        patch("lib.sync.todoist.get_today_tasks", return_value=todoist_tasks),
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
        patch("lib.sync._store", mock_store),
        patch("lib.sync.daily_note_path", return_value=note),
        patch("lib.sync.read_tasks_section", return_value=["- [x] Buy milk"]),
        patch("lib.sync.todoist.get_today_tasks", return_value=todoist_tasks),
        patch("lib.sync.todoist.complete_todoist_task") as mock_complete,
        patch("lib.sync.write_tasks_section"),
    ):
        from lib.sync import _sync

        _sync()

    mock_complete.assert_called_once_with("t1")
