"""Unit tests for TaskStore.get/update/clear_today_triaged."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from lib.models import TaskStore


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    return TaskStore(path=tmp_path / "state.json")


class TestTodayTriaged:
    def test_empty_when_no_state(self, store: TaskStore) -> None:
        assert store.get_today_triaged() == set()

    def test_update_and_retrieve(self, store: TaskStore) -> None:
        store.update_today_triaged(["id1", "id2"])
        assert store.get_today_triaged() == {"id1", "id2"}

    def test_update_merges_incrementally(self, store: TaskStore) -> None:
        store.update_today_triaged(["id1"])
        store.update_today_triaged(["id2", "id3"])
        assert store.get_today_triaged() == {"id1", "id2", "id3"}

    def test_stale_date_returns_empty(self, store: TaskStore, tmp_path: Path) -> None:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        store._data["today_triaged"] = {"date": yesterday, "task_ids": ["id1"]}
        store.save()
        assert store.get_today_triaged() == set()

    def test_clear_removes_state(self, store: TaskStore) -> None:
        store.update_today_triaged(["id1"])
        store.clear_today_triaged()
        assert store.get_today_triaged() == set()

    def test_persists_across_reload(self, tmp_path: Path) -> None:
        s1 = TaskStore(path=tmp_path / "state.json")
        s1.update_today_triaged(["id1", "id2"])

        s2 = TaskStore(path=tmp_path / "state.json")
        assert s2.get_today_triaged() == {"id1", "id2"}
