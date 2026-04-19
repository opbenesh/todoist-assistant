"""E2E tests for /optimize — single-task on-demand breakdown flow."""

from __future__ import annotations

import pytest

from tests.e2e.helpers import BotClient, TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e

_TASK_ID = "t_opt_task"
_AGED_ID = "t_opt_aged"
_QUARANTINE_ID = "t_opt_quarantined"


class TestOptimizeTaskList:
    def test_shows_task_list(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/optimize fetches tasks and shows an inline keyboard list."""
        todoist.seed(
            tasks=[
                sd.task("Write the report", task_id=_TASK_ID),
            ]
        )
        bot.send_message("/optimize")
        resp = bot.wait_responses(2, timeout=15)  # "Fetching…" + list
        assert any(r.get("has_keyboard") for r in resp), "Expected inline keyboard with task list"

    def test_no_tasks(self, bot: BotClient) -> None:
        """/optimize with empty inbox shows 'no tasks' message."""
        bot.send_message("/optimize")
        resp = bot.wait_responses(2, timeout=15)
        text = " ".join(r.get("text", "") for r in resp).lower()
        assert "no tasks" in text or "nothing" in text, (
            f"Expected empty-state message, got: {text!r}"
        )

    def test_quarantined_listed_first(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Quarantined and aged tasks appear in the list (sorted by age)."""
        todoist.seed(
            tasks=[
                sd.task("Regular task", task_id=_TASK_ID),
                sd.task("Aged task", task_id=_AGED_ID, labels=["age3"]),
                sd.task("Quarantined task", task_id=_QUARANTINE_ID, labels=["quarantined"]),
            ]
        )
        bot.send_message("/optimize")
        resp = bot.wait_responses(2, timeout=15)
        # All tasks should be listed; check that the keyboard exists
        assert any(r.get("has_keyboard") for r in resp), "Expected task list keyboard"


class TestOptimizeBreakdown:
    def _start_and_pick(self, bot: BotClient, todoist: TodoistInspector) -> None:
        todoist.seed(tasks=[sd.task("Plan the project", task_id=_TASK_ID)])
        bot.send_message("/optimize")
        bot.wait_responses(2, timeout=15)  # "Fetching…" + list
        # Pick the only task by pressing its button
        pressed = bot.press_button_labeled_any("Plan the project", timeout=8)
        assert pressed, "Could not press task button in list"

    def test_breakdown_flow(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Pick a task → describe plan → accept 1 proposal → original deleted, subtask created."""
        self._start_and_pick(bot, todoist)

        prompt = bot.wait_responses(1, timeout=8)
        assert prompt, "No plan prompt after picking task"
        assert "plan" in (prompt[-1].get("text") or "").lower()

        bot.send_message("Research first, then write a draft")
        prop1 = bot.wait_responses(1, timeout=15)
        assert prop1, "No breakdown proposal"
        bot.press_button_labeled_any("✅ Accept", timeout=5)

        for _ in range(5):
            resp = bot.wait_responses(1, timeout=8)
            if not resp:
                break
            if any(r.get("has_keyboard") for r in resp):
                bot.press_button_labeled_any("❌ Reject", timeout=5)
            else:
                break

        finish = bot.wait_responses(1, timeout=10)
        assert finish, "No finish message"
        text = (finish[-1].get("text") or "").lower()
        assert "done" in text or "created" in text, f"Expected finish summary, got: {text!r}"

        assert todoist.wait_for_op("delete", timeout=5) is not None, (
            "Expected original task deleted after breakdown"
        )
        assert todoist.wait_for_op("create", timeout=5) is not None, (
            "Expected new subtask created from accepted proposal"
        )

    def test_new_tasks_have_no_age_labels(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Subtasks created from breakdown should not carry age or quarantine labels."""
        todoist.seed(
            tasks=[
                sd.task("Aged task to break down", task_id=_AGED_ID, labels=["age5"]),
            ]
        )
        bot.send_message("/optimize")
        bot.wait_responses(2, timeout=15)
        pressed = bot.press_button_labeled_any("Aged task", timeout=8)
        assert pressed, "Could not find aged task in list"

        bot.wait_responses(1, timeout=8)  # plan prompt
        bot.send_message("Split into two parts")
        bot.wait_responses(1, timeout=15)  # first proposal
        bot.press_button_labeled_any("✅ Accept", timeout=5)

        # Drain remaining proposals
        for _ in range(5):
            resp = bot.wait_responses(1, timeout=8)
            if not resp:
                break
            if any(r.get("has_keyboard") for r in resp):
                bot.press_button_labeled_any("❌ Reject", timeout=5)
            else:
                break

        bot.wait_responses(1, timeout=10)  # finish

        create_op = todoist.wait_for_op("create", timeout=5)
        assert create_op is not None, "Expected create op"
        created_labels = create_op.get("labels") or []
        assert "age5" not in created_labels, "New task should not inherit age label"
        assert "quarantined" not in created_labels, "New task should not inherit quarantined label"
