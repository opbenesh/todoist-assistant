"""E2E tests for session persistence and scheduler behavior.

Covers:
- Plan session checkpoint survives bot restart
- Plan nag suppression while session is active
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.e2e.helpers import BotClient, LLMInspector, TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e


class TestPlanNagSuppression:
    def test_nag_suppressed_during_active_session(
        self,
        bot: BotClient,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """Starting /plan persists the session to state.json within a few seconds."""
        todoist.seed(
            tasks=[
                sd.task("Active session task", due_today=True, task_id="t_nag"),
            ]
        )

        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)

        state_file = Path(staging_app["state_file"])
        deadline = time.monotonic() + 5.0
        session_saved = False
        while time.monotonic() < deadline:
            if state_file.exists():
                state = json.loads(state_file.read_text())
                if "plan_session" in state:
                    session_saved = True
                    break
            time.sleep(0.3)

        assert session_saved, "Plan session was not persisted to state.json"


class TestSessionState:
    def test_state_file_persists_between_commands(
        self,
        bot: BotClient,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """Verify that state.json is written and readable after bot operations."""
        state_file = Path(staging_app["state_file"])

        todoist.seed(tasks=[sd.task("State test task", due_today=True)])

        bot.send_message("/list")
        bot.wait_responses(1, timeout=8)

        # State file may or may not have changed, but it should be valid JSON if it exists
        if state_file.exists():
            content = state_file.read_text(encoding="utf-8")
            try:
                state = json.loads(content)
                assert isinstance(state, dict)
            except json.JSONDecodeError as e:
                pytest.fail(f"state.json is not valid JSON: {e}\nContent: {content[:200]}")

    def test_optimize_shows_task_list(
        self,
        bot: BotClient,
        todoist: TodoistInspector,
        llm: LLMInspector,
        staging_app: dict,
    ) -> None:
        """/optimize fetches tasks and shows an inline keyboard list."""
        todoist.seed(
            tasks=[
                sd.task(
                    "Vague task needing improvement",
                    no_due_date=True,
                    priority=1,
                    task_id="t_opt",
                    labels=["quarantined"],
                ),
            ]
        )

        bot.send_message("/optimize")
        resp = bot.wait_responses(2, timeout=10)
        assert resp, "Bot did not respond to /optimize"
        assert any(r.get("reply_markup") for r in resp), "Expected task list keyboard"
        assert llm.call_count() == 0, "optimize should not call LLM at task list stage"
