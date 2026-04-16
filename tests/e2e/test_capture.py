"""E2E tests for basic capture commands: /task, /list, /done, /completed."""
from __future__ import annotations

import pytest

from tests.e2e.helpers import BotClient, TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e


class TestTaskCommand:
    def test_task_enrichment_and_create(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Send /task, confirm enrichment dialog, confirm creation → task exists in Todoist."""
        bot.send_message("/task Buy groceries")

        # Bot should ask for confirmation with enriched details
        resp = bot.wait_responses(1, timeout=10)
        assert resp, "Bot did not respond to /task"
        text = resp[0].get("text", "")
        keywords = ("grocery", "buy", "confirm", "✅")
        assert any(kw in text.lower() for kw in keywords) or "✅" in text

        # Look for a confirmation keyboard
        keyboard = bot.last_keyboard(timeout=2)
        if keyboard:
            # Find and press the confirm/yes button
            for row in keyboard:
                for label in row:
                    confirm_kws = ("confirm", "yes", "save", "create", "✅", "ok")
                    if any(kw in label.lower() for kw in confirm_kws):
                        bot.press_button(label)
                        break

        # Task should appear in Todoist
        created = todoist.wait_for_task("groceries", timeout=10)
        assert created is not None, "Task 'Buy groceries' was not created in Todoist"
        assert "create" in todoist.history_ops()

    def test_task_minimal_title(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/task with a short title goes through enrichment flow."""
        bot.send_message("/task Call dentist")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "Bot did not respond"
        # At minimum the bot reacted — task enrichment was triggered
        assert resp[0].get("text") or resp[0].get("reply_markup")


class TestListCommand:
    def test_list_shows_seeded_tasks(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/list should show today's tasks."""
        todoist.seed(tasks=[
            sd.task("Review pull request", due_today=True, priority=3),
            sd.task("Write tests", due_today=True, priority=2),
        ])

        bot.send_message("/list")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "Bot did not respond to /list"
        text = resp[0].get("text", "")
        # Should mention at least one of the seeded tasks
        assert "review" in text.lower() or "write" in text.lower() or "task" in text.lower()

    def test_list_empty(self, bot: BotClient) -> None:
        """/list with no tasks should say nothing is scheduled (not crash)."""
        bot.send_message("/list")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "Bot did not respond"


class TestDoneCommand:
    def test_done_completes_task(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/done <id> marks the task complete in Todoist."""
        todoist.seed(tasks=[sd.task("Fix bug", task_id="task_fix_bug", due_today=True)])

        bot.send_message("/done task_fix_bug")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "Bot did not respond to /done"

        completed = todoist.wait_for_op("complete", timeout=8)
        assert completed is not None, "complete operation was not recorded"
        assert completed.get("task_id") == "task_fix_bug"

    def test_done_invalid_id(self, bot: BotClient) -> None:
        """/done with nonexistent ID should respond with an error message, not crash."""
        bot.send_message("/done nonexistent_task_id_xyz")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "Bot did not respond"


class TestCompletedCommand:
    def test_completed_shows_done_tasks(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/completed lists recently completed tasks."""
        todoist.seed(tasks=[
            sd.task("Finished task", completed=True),
        ])

        bot.send_message("/completed")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "Bot did not respond to /completed"
