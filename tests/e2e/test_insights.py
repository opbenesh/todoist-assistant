"""E2E tests for /insights command handler."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.e2e.helpers import BotClient, LLMInspector, TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e


class TestInsightsCommand:
    def test_insights_full_flow(
        self,
        bot: BotClient,
        todoist: TodoistInspector,
        llm: LLMInspector,
        staging_app: dict,
    ) -> None:
        """/insights → LLM generates report → sent to chat + saved to vault."""
        todoist.seed(tasks=[
            sd.task("Overdue task", overdue_days=3),
            sd.task("Completed task", completed=True),
        ])

        bot.send_message("/insights")
        # "Generating insights…" + report + "Saved to vault: …"
        resp = bot.wait_responses(3, timeout=30)
        assert resp, "No response to /insights"

        texts = [r.get("text", "") for r in resp]
        combined = " ".join(texts).lower()
        assert "generating" in combined or "insight" in combined, \
            f"Expected insights content, got: {texts!r}"
        assert "saved" in combined or "vault" in combined, \
            f"Expected vault confirmation, got: {texts!r}"

        assert llm.call_count() > 0, "Expected LLM call for insights"

        # Verify vault file was created
        vault_dir = Path(staging_app["vault_dir"])
        assistant_dir = vault_dir / "Assistant"
        deadline = time.monotonic() + 5
        insight_files = []
        while time.monotonic() < deadline:
            insight_files = list(assistant_dir.glob("insights-*.md"))
            if insight_files:
                break
            time.sleep(0.1)
        assert insight_files, \
            f"Expected insights-*.md file in vault/Assistant/, found: {list(assistant_dir.iterdir())}"
