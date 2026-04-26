"""Decision-table tests for lib/sync._sync().

The sync function processes each Obsidian task title through a 4-variable
state machine (obs_checked × in_today × in_all × note_newer). This file
exhaustively covers every branch so regressions are caught immediately.

Branch map (mirrors lib/sync.py:61-116):
  1a  obs_checked=T, in_today=T, states match       → keep line, no API call
  1b  obs_checked=T, in_today=T, note_newer=T       → complete_todoist_task
  1c  obs_checked=F, in_today=T, note_newer=T       → uncomplete_todoist_task
  1d  obs_checked=any, in_today=T, note_newer=F     → todoist wins (obs updated)
  2   in_today=F, in_all=T                          → keep line, no create
  3   obs_checked=T, in_today=F, in_all=F           → keep line, no create  ← Bug 4
  4   obs_checked=F, in_today=F, in_all=F           → create_todoist_task
  append  in_today task not in obsidian             → append to note
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _todoist_task(title: str, is_completed: bool = False) -> dict:
    return {
        "id": f"id-{title}",
        "title": title,
        "is_completed": is_completed,
        "priority": "p4",
        "due_date": None,
        "duration_minutes": None,
    }


def _run_sync(
    *,
    obsidian_lines: list[str],
    today_tasks: list[dict],
    all_tasks: list[dict] | None = None,
    note_newer: bool = True,
    is_day_planned: bool = True,
):
    """Run _sync() with fully controlled inputs.

    Returns (written_lines, mock_create, mock_complete, mock_uncomplete).
    """
    if all_tasks is None:
        all_tasks = today_tasks[:]

    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        note = pathlib.Path(tmp) / "2026-04-06.md"
        note.write_text("# Day\n\n## Tasks\n", encoding="utf-8")

        if note_newer:
            future = time.time() + 100
            os.utime(note, (future, future))

        mock_store = MagicMock()
        mock_store.last_sync_ts = time.time() - 200 if note_newer else time.time() + 200

        written: list[str] = []

        with (
            patch("lib.sync.is_day_planned", return_value=is_day_planned),
            patch("lib.sync.store", mock_store),
            patch("lib.sync.daily_note_path", return_value=note),
            patch("lib.sync.read_tasks_section", return_value=obsidian_lines),
            patch("lib.sync.todoist.get_today_tasks", return_value=today_tasks),
            patch("lib.sync.todoist.get_all_tasks", return_value=all_tasks),
            patch("lib.sync.todoist.get_completed_tasks", return_value=[]),
            patch("lib.sync.todoist.create_todoist_task", return_value="new-id") as mock_create,
            patch("lib.sync.todoist.batch_update_task_status") as mock_batch_update,
            patch("lib.sync.write_tasks_section", side_effect=lambda lines: written.extend(lines)),
            patch("lib.sync.audit.log"),
        ):
            from lib.sync import _sync

            _sync()

        return written, mock_create, mock_batch_update


# ---------------------------------------------------------------------------
# Branch 1a: obs_checked matches todoist (both unchecked) — no API call
# ---------------------------------------------------------------------------


def test_branch_1a_states_match_no_api_call():
    """obs_checked=F, todoist=uncomplete, note_newer=T → keep line, no API call."""
    task = _todoist_task("Buy milk", is_completed=False)
    written, create, batch_update = _run_sync(
        obsidian_lines=["- [ ] Buy milk"],
        today_tasks=[task],
        note_newer=True,
    )
    batch_update.assert_not_called()
    create.assert_not_called()


# ---------------------------------------------------------------------------
# Branch 1b: obs_checked=T, todoist=uncomplete, note_newer=T → complete
# ---------------------------------------------------------------------------


def test_branch_1b_obsidian_wins_complete():
    """Checking a task in Obsidian when note is newer should complete it in Todoist."""
    task = _todoist_task("Buy milk", is_completed=False)
    written, create, batch_update = _run_sync(
        obsidian_lines=["- [x] Buy milk"],
        today_tasks=[task],
        note_newer=True,
    )
    batch_update.assert_called_once_with(["id-Buy milk"], [])


# ---------------------------------------------------------------------------
# Branch 1c: obs_checked=F, todoist=complete, note_newer=T → uncomplete
# ---------------------------------------------------------------------------


def test_branch_1c_obsidian_wins_uncomplete():
    """Unchecking a completed Todoist task in Obsidian (note newer) should uncomplete it."""
    task = _todoist_task("Buy milk", is_completed=True)
    written, create, batch_update = _run_sync(
        obsidian_lines=["- [ ] Buy milk"],
        today_tasks=[task],
        note_newer=True,
    )
    batch_update.assert_called_once_with([], ["id-Buy milk"])


# ---------------------------------------------------------------------------
# Branch 1d: note NOT newer → Todoist wins, obs_checked overridden
# ---------------------------------------------------------------------------


def test_branch_1d_todoist_wins_when_note_older():
    """When note is older than last sync, Todoist state wins — no API calls, obs updated."""
    task = _todoist_task("Buy milk", is_completed=True)
    written, create, batch_update = _run_sync(
        obsidian_lines=["- [ ] Buy milk"],  # unchecked in obsidian
        today_tasks=[task],  # but completed in todoist
        note_newer=False,
    )
    batch_update.assert_not_called()
    create.assert_not_called()
    # Todoist wins: line should reflect completed state
    assert any("[x]" in line and "Buy milk" in line for line in written)


# ---------------------------------------------------------------------------
# Branch 2: in_today=F, in_all=T (rescheduled task) → keep line, no create
# ---------------------------------------------------------------------------


def test_branch_2_rescheduled_task_not_recreated():
    """Task rescheduled to future date is absent from today_tasks but present in all_tasks.
    Should NOT be created as a new task."""
    rescheduled = _todoist_task("Rescheduled task")
    written, create, batch_update = _run_sync(
        obsidian_lines=["- [ ] Rescheduled task"],
        today_tasks=[],  # not due today
        all_tasks=[rescheduled],  # but exists in Todoist
    )
    create.assert_not_called()


# ---------------------------------------------------------------------------
# Branch 3: obs_checked=T, in_today=F, in_all=F → keep line, no create (Bug 4)
# ---------------------------------------------------------------------------


def test_branch_3_checked_absent_task_not_recreated():
    """Checked task that is absent from all Todoist lists should NOT be re-created.

    This is the exact bug from session 78be62f4: when complete_todoist_task silently
    failed, checked tasks disappeared from Todoist. The next sync saw them as checked
    + absent and (incorrectly) re-created them. The fix: checked + absent → already
    done, skip creation.
    """
    written, create, batch_update = _run_sync(
        obsidian_lines=["- [x] Already done"],
        today_tasks=[],  # absent from today
        all_tasks=[],  # absent from all tasks
    )
    create.assert_not_called()


# ---------------------------------------------------------------------------
# Branch 4: obs_checked=F, in_today=F, in_all=F → create new task
# ---------------------------------------------------------------------------


def test_branch_4_unchecked_absent_task_is_created():
    """Unchecked task absent from all Todoist lists should be created."""
    written, create, batch_update = _run_sync(
        obsidian_lines=["- [ ] New task from obsidian"],
        today_tasks=[],
        all_tasks=[],
    )
    create.assert_called_once()
    call_task = create.call_args.args[0]
    assert call_task.title == "New task from obsidian"


# ---------------------------------------------------------------------------
# Append branch: todoist task not in obsidian → append to note
# ---------------------------------------------------------------------------


def test_append_todoist_task_not_in_obsidian():
    """A Todoist task absent from the Obsidian note should be appended."""
    task = _todoist_task("From todoist")
    written, create, batch_update = _run_sync(
        obsidian_lines=[],
        today_tasks=[task],
    )
    assert any("From todoist" in line for line in written)
    create.assert_not_called()


# ---------------------------------------------------------------------------
# Guard: sync skips entirely when day is not planned
# ---------------------------------------------------------------------------


def test_sync_skips_when_not_planned():
    """_sync() should be a no-op if is_day_planned() returns False."""
    written, create, batch_update = _run_sync(
        obsidian_lines=["- [ ] Some task"],
        today_tasks=[],
        is_day_planned=False,
    )
    create.assert_not_called()
    batch_update.assert_not_called()
    assert written == []
