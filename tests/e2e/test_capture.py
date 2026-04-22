"""E2E tests for /list command."""

from __future__ import annotations

import pytest

from tests.e2e.helpers import BotClient, TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e


class TestListCommand:
    def test_list_shows_seeded_tasks(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/list should show today's tasks."""
        todoist.seed(
            tasks=[
                sd.task("Review pull request", due_today=True, priority=3),
                sd.task("Write tests", due_today=True, priority=2),
            ]
        )

        bot.send_message("/list")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "Bot did not respond to /list"
        text = resp[0].get("text", "")
        # Should mention at least one of the seeded tasks
        assert "review" in text.lower() or "write" in text.lower() or "task" in text.lower()

    def test_list_empty(self, bot: BotClient) -> None:
        """/list with no tasks should tell the user there's nothing scheduled."""
        bot.send_message("/list")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "Bot did not respond to /list"
        text = resp[0].get("text", "").lower()
        assert "no tasks" in text, f"Expected 'no tasks' message, got: {text!r}"
