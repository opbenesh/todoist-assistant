"""E2E tests for /optimize conversation handler."""
from __future__ import annotations

import json

import pytest

from tests.e2e.helpers import BotClient, LLMInspector, TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e

_TASK_ID = "t_opt_task"


def _non_actionable_response(task_id: str, title: str) -> str:
    """Build a judge response that marks the given task as non-actionable."""
    return json.dumps([{
        "id": task_id,
        "actionable": False,
        "reason": "Too vague, not a concrete next action",
        "clean_title": title,
    }])


class TestOptimizeAllActionable:
    def test_all_already_labeled(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/optimize when all tasks already have actionable label → early exit."""
        todoist.seed(tasks=[
            sd.task("Write report", task_id="t_already", labels=["actionable"]),
        ])
        bot.send_message("/optimize")
        resp = bot.wait_responses(2, timeout=15)  # "Fetching…" + result
        text = " ".join(r.get("text", "") for r in resp).lower()
        assert "already" in text or "nothing" in text, \
            f"Expected early-exit message, got: {text!r}"

    def test_auto_label(
        self, bot: BotClient, todoist: TodoistInspector, llm: LLMInspector
    ) -> None:
        """/optimize auto-labels actionable tasks without showing review UI."""
        todoist.seed(tasks=[
            sd.task("Send weekly update", task_id="t_auto1"),
            sd.task("Review PR", task_id="t_auto2"),
        ])
        bot.send_message("/optimize")
        # "Fetching…" + "Judging 2…" + summary
        resp = bot.wait_responses(3, timeout=20)
        text = " ".join(r.get("text", "") for r in resp).lower()
        assert "auto-labeled" in text or "labeled" in text, \
            f"Expected auto-label summary, got: {text!r}"
        assert "all done" in text, f"Expected 'All done!' after auto-labeling, got: {text!r}"
        assert llm.call_count() > 0, "Expected LLM call for judge"
        update_op = todoist.wait_for_op("update", timeout=5)
        assert update_op is not None, "Expected update op to add actionable label"


class TestOptimizeReviewLoop:
    def _setup(self, bot: BotClient, todoist: TodoistInspector, llm: LLMInspector) -> None:
        """Seed one task and override judge to mark it non-actionable."""
        todoist.seed(tasks=[sd.task("Sort through old notes", task_id=_TASK_ID)])
        llm.set_next_response(_non_actionable_response(_TASK_ID, "Sort through old notes"))

    def _start_optimize(self, bot: BotClient) -> None:
        """Send /optimize and wait through fetching + judging + summary messages."""
        bot.send_message("/optimize")
        # "Fetching…" + "Judging 1…" + summary + review card
        bot.wait_responses(4, timeout=20)

    def test_skip(
        self, bot: BotClient, todoist: TodoistInspector, llm: LLMInspector
    ) -> None:
        """Skip non-actionable task → title cleanup update + finish summary."""
        self._setup(bot, todoist, llm)
        self._start_optimize(bot)
        bot.press_button_labeled_any("Skip", timeout=5)
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "No finish message after Skip"
        text = (resp[-1].get("text") or "").lower()
        assert "optimize complete" in text or "skipped" in text, \
            f"Expected finish summary, got: {text!r}"
        update_op = todoist.wait_for_op("update", timeout=5)
        assert update_op is not None, "Expected title cleanup update op on skip"

    def test_delete(
        self, bot: BotClient, todoist: TodoistInspector, llm: LLMInspector
    ) -> None:
        """Delete non-actionable task → delete op in history + finish summary."""
        self._setup(bot, todoist, llm)
        self._start_optimize(bot)
        bot.press_button_labeled_any("Delete", timeout=5)
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "No finish message after Delete"
        delete_op = todoist.wait_for_op("delete", timeout=5)
        assert delete_op is not None, "Expected delete op"
        assert delete_op.get("task_id") == _TASK_ID

    def test_override(
        self, bot: BotClient, todoist: TodoistInspector, llm: LLMInspector
    ) -> None:
        """Override non-actionable task → update adds actionable label + finish."""
        self._setup(bot, todoist, llm)
        self._start_optimize(bot)
        bot.press_button_labeled_any("actionable", timeout=5)
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "No finish message after Override"
        update_op = todoist.wait_for_op("update", timeout=5)
        assert update_op is not None, "Expected update op for override"

    def test_optimize_breakdown(
        self, bot: BotClient, todoist: TodoistInspector, llm: LLMInspector
    ) -> None:
        """Optimize flow: press Optimize → describe plan → accept 1, reject others → original deleted."""
        self._setup(bot, todoist, llm)
        self._start_optimize(bot)
        bot.press_button_labeled_any("Optimize", timeout=5)
        prompt_resp = bot.wait_responses(1, timeout=8)  # "What's your plan for…"
        assert prompt_resp, "No prompt after pressing Optimize"
        assert "plan" in (prompt_resp[-1].get("text") or "").lower()

        bot.send_message("I'll research first, then draft a summary")
        # Proposal 1 of 3
        prop1 = bot.wait_responses(1, timeout=15)
        assert prop1, "No breakdown proposal 1"
        bot.press_button_labeled_any("✅ Accept", timeout=5)

        # Proposals 2 and 3
        for _ in range(2):
            bot.wait_responses(1, timeout=8)
            bot.press_button_labeled_any("❌ Reject", timeout=5)

        # After all proposals: finalize (delete original) then finish message
        finish = bot.wait_responses(1, timeout=8)
        assert finish, "No finish message after breakdown"
        text = (finish[-1].get("text") or "").lower()
        assert "optimize complete" in text or "broken down" in text, \
            f"Expected finish summary, got: {text!r}"

        assert todoist.wait_for_op("delete", timeout=5) is not None, \
            "Expected original task deleted after breakdown"
        assert todoist.wait_for_op("create", timeout=5) is not None, \
            "Expected new task created from accepted proposal"
