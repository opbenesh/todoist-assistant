from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

Priority = Literal["p1", "p2", "p3", "p4"]
VALID_PRIORITIES: tuple[str, ...] = ("p1", "p2", "p3", "p4")
DEFAULT_PRIORITY: Priority = "p4"

PRIORITY_TO_TODOIST = {"p1": 4, "p2": 3, "p3": 2, "p4": 1}
TODOIST_TO_PRIORITY = {4: "p1", 3: "p2", 2: "p3", 1: "p4"}

_STATE_PATH = Path(__file__).parent.parent / "data" / "state.json"


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


class TaskStore:
    """Persistent state via atomic JSON writes."""

    def __init__(self, path: Path = _STATE_PATH) -> None:
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
