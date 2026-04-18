"""E2E tests for /brainstorm conversation handler."""
from __future__ import annotations

import pytest

from tests.e2e.helpers import BotClient, TodoistInspector

pytestmark = pytest.mark.e2e


class TestBrainstormCommand:
    def test_accept_all_done(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/brainstorm → type text → LLM extracts tasks → accept both → Done → tasks added."""
        bot.send_message("/brainstorm")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "No response to /brainstorm"
        assert "brainstorm" in (resp[-1].get("text") or "").lower()

        bot.send_message("I need to review the quarterly report and schedule a team sync")
        # "Extracting tasks…" + first task card
        bot.wait_responses(2, timeout=15)

        # Accept all proposed tasks (LLM returns 2 by default)
        bot.press_button_labeled_any("✅ Accept", timeout=5)
        bot.wait_responses(1, timeout=8)
        bot.press_button_labeled_any("✅ Accept", timeout=5)
        # Wrapup message
        wrapup = bot.wait_responses(1, timeout=8)
        assert wrapup, "No wrapup after accepting all tasks"

        bot.press_button_labeled_any("✔ Done", timeout=5)
        finish = bot.wait_responses(1, timeout=8)
        assert finish, "No finish message after Done"
        text = (finish[-1].get("text") or "").lower()
        assert "task" in text and ("added" in text or "complete" in text), \
            f"Expected completion summary, got: {text!r}"

        ops = todoist.history_ops()
        assert ops.count("create") >= 2, f"Expected ≥2 create ops, got: {ops}"

    def test_reject_one(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Accept 1, reject 1 → Done → 1 task added."""
        bot.send_message("/brainstorm")
        bot.wait_responses(1, timeout=8)

        bot.send_message("Draft blog post and update the wiki page")
        bot.wait_responses(2, timeout=15)  # extracting + first card

        bot.press_button_labeled_any("✅ Accept", timeout=5)
        bot.wait_responses(1, timeout=8)
        bot.press_button_labeled_any("❌ Reject", timeout=5)
        wrapup = bot.wait_responses(1, timeout=8)
        assert wrapup, "No wrapup after review"

        bot.press_button_labeled_any("✔ Done", timeout=5)
        finish = bot.wait_responses(1, timeout=8)
        assert finish, "No finish message after Done"

        ops = todoist.history_ops()
        assert ops.count("create") == 1, f"Expected exactly 1 create op, got: {ops}"

    def test_continue_brainstorming(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Accept both → Keep brainstorming → type more → accept 1 → Done → 3 tasks added."""
        bot.send_message("/brainstorm")
        bot.wait_responses(1, timeout=8)

        bot.send_message("Set up CI pipeline and write unit tests")
        bot.wait_responses(2, timeout=15)

        bot.press_button_labeled_any("✅ Accept", timeout=5)
        bot.wait_responses(1, timeout=8)
        bot.press_button_labeled_any("✅ Accept", timeout=5)
        wrapup = bot.wait_responses(1, timeout=8)
        assert wrapup, "No wrapup"

        bot.press_button_labeled_any("➕ Keep brainstorming", timeout=5)
        cont = bot.wait_responses(1, timeout=8)
        assert cont, "No response after Keep brainstorming"

        bot.send_message("Also need to fix the deployment script")
        bot.wait_responses(2, timeout=15)  # extracting + first card

        bot.press_button_labeled_any("✅ Accept", timeout=5)
        wrapup2 = bot.wait_responses(1, timeout=8)
        assert wrapup2, "No wrapup after second round"

        bot.press_button_labeled_any("✔ Done", timeout=5)
        finish = bot.wait_responses(1, timeout=8)
        assert finish, "No finish message after Done"

        ops = todoist.history_ops()
        assert ops.count("create") >= 3, f"Expected ≥3 create ops, got: {ops}"

    def test_cancel(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/brainstorm → /cancel → 'Brainstorm cancelled', no tasks created."""
        bot.send_message("/brainstorm")
        bot.wait_responses(1, timeout=8)

        bot.send_message("/cancel")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "No response to /cancel"
        text = (resp[-1].get("text") or "").lower()
        assert "cancel" in text, f"Expected cancellation message, got: {text!r}"
        assert not todoist.history_ops(), "Expected no Todoist ops after cancel"
