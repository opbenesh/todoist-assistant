from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

Priority = Literal["p1", "p2", "p3", "p4"]
VALID_PRIORITIES: tuple[str, ...] = ("p1", "p2", "p3", "p4")
DEFAULT_PRIORITY: Priority = "p4"

PRIORITY_TO_TODOIST = {"p1": 4, "p2": 3, "p3": 2, "p4": 1}
TODOIST_TO_PRIORITY = {4: "p1", 3: "p2", 2: "p3", 1: "p4"}

_DEFAULT_STATE_PATH = Path(__file__).parent.parent / "data" / "state.json"


@dataclass
class Task:
    title: str
    notes: str = ""
    due_date: date | None = None
    priority: Priority = DEFAULT_PRIORITY
    labels: list[str] = field(default_factory=list)
    project: str = "Inbox"
    duration_minutes: int | None = None
    todoist_id: str | None = None


@dataclass
class EnrichmentState:
    chat_id: int
    raw_title: str
    task: Task | None = None
    step: str = "awaiting_proposal"
    subtasks: list[str] = field(default_factory=list)


class TaskStore:
    """Persistent state via atomic JSON writes."""

    def __init__(self, path: Path = _DEFAULT_STATE_PATH) -> None:
        self.path = path
        self._data: dict = {"sync_cursor": "*", "last_sync_ts": None}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, default=str), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def sync_cursor(self) -> str:
        return self._data.get("sync_cursor", "*")

    @sync_cursor.setter
    def sync_cursor(self, value: str) -> None:
        self._data["sync_cursor"] = value

    @property
    def last_sync_ts(self) -> float | None:
        return self._data.get("last_sync_ts")

    @last_sync_ts.setter
    def last_sync_ts(self, value: float) -> None:
        self._data["last_sync_ts"] = value

    def mark_unblocked(self, task_id: str) -> None:
        """Record that a task was handled in an unblock session."""
        ids = self._data.setdefault("unblocked_task_ids", {})
        ids[task_id] = time.time()
        cutoff = time.time() - 24 * 3600
        self._data["unblocked_task_ids"] = {k: v for k, v in ids.items() if v > cutoff}
        self.save()

    def recently_unblocked_ids(self, cutoff_hours: int = 24) -> set[str]:
        """Return task IDs handled within the last cutoff_hours."""
        cutoff = time.time() - cutoff_hours * 3600
        return {k for k, v in self._data.get("unblocked_task_ids", {}).items() if v > cutoff}

    def save_plan_session(self, phase: int, session_data: dict) -> None:
        """Persist a planning session checkpoint."""
        self._data["plan_session"] = {"phase": phase, "session": session_data}
        self.save()

    def load_plan_session(self) -> tuple[int, dict] | None:
        """Return (phase, session_data) if a checkpoint exists, else None."""
        self.load()
        entry = self._data.get("plan_session")
        if not entry:
            return None
        return entry["phase"], entry["session"]

    def clear_plan_session(self) -> None:
        """Remove any persisted planning session checkpoint."""
        self._data.pop("plan_session", None)
        self.save()

    @property
    def known_active_ids(self) -> dict[str, str] | None:
        """Snapshot of active task IDs → titles from the last Todoist poll.

        Returns None if the snapshot has never been initialized (first run).
        Returns {} if initialized but all tasks have been removed.
        """
        return self._data.get("known_active_ids")

    @known_active_ids.setter
    def known_active_ids(self, value: dict[str, str]) -> None:
        self._data["known_active_ids"] = value

    def add_known_task(self, task_id: str, title: str) -> None:
        """Record a bot-created task in the snapshot so it isn't flagged as external."""
        self._data.setdefault("known_active_ids", {})[task_id] = title
        self.save()

    def remove_known_task(self, task_id: str) -> None:
        """Remove a bot-completed/deleted task from the snapshot."""
        known = self._data.get("known_active_ids")
        if known is not None:
            known.pop(task_id, None)
            self.save()

    def set_quarantine_ts(self, task_id: str) -> None:
        """Record when a task was quarantined."""
        self._data.setdefault("quarantine_timestamps", {})[task_id] = time.time()
        self.save()

    def get_quarantine_ts(self, task_id: str) -> float | None:
        """Return the quarantine timestamp for a task, or None."""
        return self._data.get("quarantine_timestamps", {}).get(task_id)

    def clear_quarantine_ts(self, task_id: str) -> None:
        """Remove quarantine timestamp when a task is unblocked/deleted/completed."""
        ts = self._data.get("quarantine_timestamps")
        if ts is not None and task_id in ts:
            del ts[task_id]
            self.save()


def _resolve_state_path() -> Path:
    if os.getenv("CLI_MODE"):
        return Path("data/state_cli.json")
    env_path = os.environ.get("STATE_PATH")
    if env_path:
        return Path(env_path)
    return _DEFAULT_STATE_PATH


store = TaskStore(path=_resolve_state_path())
