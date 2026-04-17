"""E2E tests for Obsidian ↔ Todoist bidirectional sync.

The sync job runs every OBSIDIAN_POLL_SECONDS (set to 2 in staging).
Tests write vault content or Todoist state then wait for sync to propagate.
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pytest

from tests.e2e.helpers import TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e

_TODAY = date.today().isoformat()
_SYNC_WAIT = 4.0  # seconds to wait for sync to run (2s interval, 2 cycles of margin)


def _daily_note_path(vault_dir: Path) -> Path:
    return vault_dir / "Daily" / f"{_TODAY}.md"


def _write_daily_note(vault_dir: Path, content: str) -> None:
    path = _daily_note_path(vault_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _read_daily_note(vault_dir: Path) -> str:
    path = _daily_note_path(vault_dir)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


class TestSyncTodoisToVault:
    def test_todoist_task_appended_to_vault(
        self,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """Task in Todoist but absent from vault → sync appends it to daily note."""
        vault_dir = Path(staging_app["vault_dir"])

        # Write a planned daily note (needed for sync to run)
        _write_daily_note(vault_dir, f"# {_TODAY}\n\n## Tasks\n\n- [ ] Seed task\n")

        todoist.seed(tasks=[
            sd.task("New todoist task", due_today=True, task_id="t_sync_new"),
        ])

        # Wait for sync to run
        deadline = time.monotonic() + _SYNC_WAIT
        while time.monotonic() < deadline:
            note = _read_daily_note(vault_dir)
            if "new todoist task" in note.lower() or "t_sync" in note.lower():
                return
            time.sleep(0.4)

        note = _read_daily_note(vault_dir)
        assert "new todoist task" in note.lower(), \
            f"Todoist task was not synced to vault. Note content:\n{note}"


class TestSyncVaultToTodoist:
    def test_vault_completion_propagates_to_todoist(
        self,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """Checking off a task in vault → sync completes it in Todoist."""
        vault_dir = Path(staging_app["vault_dir"])

        todoist.seed(tasks=[
            sd.task("Walk the dog", due_today=True, task_id="t_walk_dog"),
        ])

        # Write vault with task UNchecked; wait for sync to establish baseline state
        # before marking complete (requires at least one full sync cycle = 2s)
        _write_daily_note(vault_dir, f"# {_TODAY}\n\n## Tasks\n\n- [ ] Walk the dog\n")
        time.sleep(_SYNC_WAIT)

        # Now mark as checked in vault (simulating user checking off in Obsidian)
        _write_daily_note(vault_dir, f"# {_TODAY}\n\n## Tasks\n\n- [x] Walk the dog\n")

        # Wait for sync to propagate the completion
        op = todoist.wait_for_op("complete", timeout=_SYNC_WAIT)
        assert op is not None, "complete was not called after task checked off in vault"

    def test_vault_uncomplete_propagates_to_todoist(
        self,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """Unchecking a task in vault → sync uncompletes it in Todoist."""
        vault_dir = Path(staging_app["vault_dir"])

        todoist.seed(tasks=[
            sd.task("Read book chapter", due_today=True, task_id="t_book"),
        ])

        # Start with task checked in vault, wait for sync to register it as complete.
        # Using wait_for_op instead of sleep avoids a race where the fixed sleep ends
        # before the first sync fires, causing the [x]→complete step to never happen.
        _write_daily_note(vault_dir, f"# {_TODAY}\n\n## Tasks\n\n- [x] Read book chapter\n")
        todoist.wait_for_op("complete", timeout=_SYNC_WAIT)

        # Uncheck it
        _write_daily_note(vault_dir, f"# {_TODAY}\n\n## Tasks\n\n- [ ] Read book chapter\n")

        op = todoist.wait_for_op("uncomplete", timeout=_SYNC_WAIT)
        assert op is not None, "uncomplete was not called after task unchecked in vault"


class TestSyncEdgeCases:
    def test_rescheduled_task_not_duplicated(
        self,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """Task in all_tasks but not in today → should NOT be created again (rescheduled case)."""
        vault_dir = Path(staging_app["vault_dir"])

        # Task with no due date (not in today's view, but in all tasks)
        todoist.seed(tasks=[
            sd.task("Rescheduled task", no_due_date=True, task_id="t_reschedule"),
        ])

        # Write vault with this task
        _write_daily_note(vault_dir, f"# {_TODAY}\n\n## Tasks\n\n- [ ] Rescheduled task\n")

        time.sleep(_SYNC_WAIT)

        # Should NOT see a duplicate 'create' for this task
        creates = [h for h in todoist.history() if h["op"] == "create"]
        for c in creates:
            assert "rescheduled" not in c.get("content", "").lower(), \
                "Rescheduled task was incorrectly recreated"

    def test_no_sync_when_note_absent(
        self,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """No daily note → sync should be a no-op (no creates or completes)."""
        vault_dir = Path(staging_app["vault_dir"])

        # Ensure no daily note exists
        note_path = _daily_note_path(vault_dir)
        if note_path.exists():
            note_path.unlink()

        todoist.seed(tasks=[
            sd.task("Task with no note", due_today=True, task_id="t_no_note"),
        ])

        time.sleep(_SYNC_WAIT)

        # No write ops should have happened
        write_ops = [h for h in todoist.history() if h["op"] in ("create", "complete", "uncomplete")]
        assert len(write_ops) == 0, f"Unexpected write ops when note absent: {write_ops}"
