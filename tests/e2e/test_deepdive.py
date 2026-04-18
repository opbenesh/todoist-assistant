"""E2E tests for /deepdive command handler."""
from __future__ import annotations

import pytest

from tests.e2e.helpers import BotClient, LLMInspector, TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e


class TestDeepdiveCommand:
    def test_valid_task(
        self, bot: BotClient, todoist: TodoistInspector, llm: LLMInspector
    ) -> None:
        """/deepdive <task_id> → 'Analysing…' + markdown analysis from LLM."""
        todoist.seed(tasks=[
            sd.task("Refactor authentication module", task_id="t_deep"),
        ])

        bot.send_message("/deepdive t_deep")
        resp = bot.wait_responses(2, timeout=20)  # "Analysing…" + analysis
        assert resp, "No response to /deepdive"

        texts = [r.get("text", "") for r in resp]
        combined = " ".join(texts).lower()
        assert "analys" in combined, \
            f"Expected 'Analysing' or analysis content, got: {texts!r}"
        # LLM should have been called
        assert llm.call_count() > 0, "Expected LLM call for deep dive"

    def test_invalid_task_id(self, bot: BotClient) -> None:
        """/deepdive <nonexistent_id> → error message, no crash."""
        bot.send_message("/deepdive nonexistent_task_xyz")
        resp = bot.wait_responses(2, timeout=15)  # "Analysing…" + error
        assert resp, "No response to /deepdive with invalid ID"
        text = " ".join(r.get("text", "") for r in resp).lower()
        assert "could not find" in text or "not found" in text or "failed" in text, \
            f"Expected error message for missing task, got: {text!r}"

    def test_no_args(self, bot: BotClient) -> None:
        """/deepdive with no args → usage hint."""
        bot.send_message("/deepdive")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "No response to /deepdive without args"
        text = (resp[-1].get("text") or "").lower()
        assert "usage" in text or "deepdive" in text, \
            f"Expected usage hint, got: {text!r}"
