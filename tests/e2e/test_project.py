"""E2E tests for /project conversation handler."""
from __future__ import annotations

import json

import pytest

from tests.e2e.helpers import BotClient, LLMInspector, TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e


class TestProjectCommand:
    def test_project_plan(
        self, bot: BotClient, todoist: TodoistInspector, llm: LLMInspector
    ) -> None:
        """/project → keyboard shows project → press it → LLM plan sent."""
        todoist.seed(
            projects=[sd.project("Work", project_id="p_work")],
            tasks=[
                sd.task("Write proposal", project_id="p_work"),
                sd.task("Review budget", project_id="p_work"),
            ],
        )

        bot.send_message("/project")
        # "Fetching projects…" + keyboard
        resp = bot.wait_responses(2, timeout=10)
        assert resp, "No response to /project"

        keyboard_resp = resp[-1]
        assert keyboard_resp.get("reply_markup"), "Expected project selection keyboard"

        # Press the Work project button
        pressed = bot.press_button_labeled_any("Work", timeout=5)
        assert pressed, "Work button not found on project keyboard"

        # "Analysing Work…" (edit 1) → plan (edit 2)
        plan_resp = bot.wait_responses(2, timeout=20)
        assert plan_resp, "No plan response after selecting project"
        text = (plan_resp[-1].get("text") or "").lower()
        assert len(text) > 20, f"Expected plan content, got: {text!r}"
        assert llm.call_count() > 0, "Expected LLM call for project plan"

    def test_inbox_always_present(self, bot: BotClient) -> None:
        """Inbox project is always available — /project shows at least one project."""
        bot.send_message("/project")
        resp = bot.wait_responses(2, timeout=10)
        assert resp, "No response to /project"

        keyboard_resp = resp[-1]
        markup = keyboard_resp.get("reply_markup")
        assert markup, "Expected project selection keyboard"
        if isinstance(markup, str):
            markup = json.loads(markup)

        # Flatten button labels
        rows = markup.get("inline_keyboard", [])
        labels = [btn.get("text", "") for row in rows for btn in row]
        assert labels, "Expected at least one project button, got empty keyboard"
