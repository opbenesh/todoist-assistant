"""E2E tests for Todoist → assistant sync (external changes detection)."""

from __future__ import annotations

import time

import pytest

from tests.e2e.helpers import BotClient, TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e

# With TODOIST_POLL_SECONDS=2, allow 2 full cycles + margin for each wait.
_POLL_WAIT = 6.0
# Sleep long enough after staging_app starts to ensure the first poll (fired at
# TODOIST_POLL_FIRST_SECONDS=2) has run and built the initial empty snapshot.
_FIRST_POLL_SETTLE = 3.5


class TestTodoistSyncBack:
    def test_new_task_in_todoist_notifies(
        self,
        bot: BotClient,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """Task added directly in Todoist triggers a 'New in Todoist' notification."""
        time.sleep(_FIRST_POLL_SETTLE)  # let first poll build empty snapshot
        todoist.seed(tasks=[sd.task("Buy oat milk", task_id="t_new_1")])

        resp = bot.find_response_containing("oat milk", timeout=_POLL_WAIT)
        assert resp is not None, "No notification received for new Todoist task"
        assert "new in todoist" in resp["text"].lower()

    def test_external_completion_notifies(
        self,
        bot: BotClient,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """Task completed directly in Todoist triggers 'Completed in Todoist' notification."""
        time.sleep(_FIRST_POLL_SETTLE)
        todoist.seed(tasks=[sd.task("Buy milk", task_id="t_complete_1")])

        # Wait for "New in Todoist" — confirms task is now in the bot's snapshot.
        resp = bot.find_response_containing("new in todoist", timeout=_POLL_WAIT)
        assert resp is not None, "Snapshot-build notification missing"
        bot._seen_count = len(bot.all_responses())

        todoist.complete_externally("t_complete_1")

        resp = bot.find_response_containing("completed in todoist", timeout=_POLL_WAIT)
        assert resp is not None, "No completion notification received"
        assert "buy milk" in resp["text"].lower()

    def test_external_deletion_notifies(
        self,
        bot: BotClient,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """Task deleted directly in Todoist triggers 'Deleted in Todoist' notification."""
        time.sleep(_FIRST_POLL_SETTLE)
        todoist.seed(tasks=[sd.task("Old task", task_id="t_delete_1")])

        resp = bot.find_response_containing("new in todoist", timeout=_POLL_WAIT)
        assert resp is not None
        bot._seen_count = len(bot.all_responses())

        todoist.delete_externally("t_delete_1")

        resp = bot.find_response_containing("deleted in todoist", timeout=_POLL_WAIT)
        assert resp is not None, "No deletion notification received"
        assert "old task" in resp["text"].lower()

    def test_recurring_task_no_double_notify(
        self,
        bot: BotClient,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """Completing a recurring task spawns a new instance — only 'Completed' fires, not 'New'."""
        time.sleep(_FIRST_POLL_SETTLE)
        todoist.seed(tasks=[sd.task("Stand-up", task_id="t_recur_1", recurring=True)])

        resp = bot.find_response_containing("new in todoist", timeout=_POLL_WAIT)
        assert resp is not None
        bot._seen_count = len(bot.all_responses())

        # Simulate recurring completion: mark done, then seed new instance same title.
        todoist.complete_externally("t_recur_1")
        todoist.seed(tasks=[sd.task("Stand-up", task_id="t_recur_2", recurring=True)])

        # Completion notification must fire.
        resp = bot.find_response_containing("completed in todoist", timeout=_POLL_WAIT)
        assert resp is not None, "Expected 'Completed in Todoist' for recurring task"
        assert "stand-up" in resp["text"].lower()

        # Let another poll cycle run to confirm no spurious "New in Todoist" follows.
        time.sleep(3.0)
        for r in bot.all_responses()[bot._seen_count :]:
            text = (r.get("text") or "").lower()
            assert "new in todoist" not in text or "stand-up" not in text, (
                "Spurious 'New in Todoist' for recurring re-open"
            )

    def test_bot_completion_not_notified(
        self,
        bot: BotClient,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """Tasks completed via /done must not trigger 'Completed in Todoist' notification."""
        time.sleep(_FIRST_POLL_SETTLE)
        todoist.seed(tasks=[sd.task("Finish report", task_id="t_bot_done")])

        resp = bot.find_response_containing("new in todoist", timeout=_POLL_WAIT)
        assert resp is not None
        bot._seen_count = len(bot.all_responses())

        bot.send_message("/done t_bot_done")
        # Wait for the /done ack and one more poll cycle.
        bot.wait_responses(1, timeout=3.0)  # "Done." reply
        time.sleep(3.0)

        for r in bot.all_responses()[bot._seen_count :]:
            text = (r.get("text") or "").lower()
            assert "completed in todoist" not in text, (
                f"Unexpected 'Completed in Todoist' after bot /done: {text!r}"
            )
