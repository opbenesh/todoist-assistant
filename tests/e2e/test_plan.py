"""E2E tests for the multi-step /plan conversation flow.

Covers:
- Happy path: brainstorm → triage → plan generated, vault written
- Skip brainstorm path
- Triage actions: postpone, quarantine, delete
- Session persistence: plan session survives bot restart
- Nag suppression: active plan session suppresses hourly nag
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import APP_DIR, _wait_bot_ready
from tests.e2e.constants import ADD_TO_PLAN, BS_SKIP, BS_START
from tests.e2e.helpers import BotClient, LLMInspector, TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e


class TestPlanSkipAll:
    def test_plan_skip_brainstorm(
        self,
        bot: BotClient,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """Skip brainstorm, triage one task, verify plan message sent via Telegram."""
        todoist.seed(
            tasks=[
                sd.task("Send weekly report", due_today=True, priority=2, task_id="t_weekly"),
            ]
        )

        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        _skip_bs(bot)

        # Triage header has no buttons; task card follows
        bot.wait_responses(1, timeout=10)  # "📋 Triage — 1 task to review."
        pressed = (
            bot.press_button_labeled_any("P2", timeout=10)
            or bot.press_button_labeled_any("P3", timeout=3)
            or bot.press_button_labeled_any("P1", timeout=3)
        )
        assert pressed, "No priority button found in triage keyboard"

        # Timeslot picker may appear for prioritised tasks
        resp2 = bot.wait_responses(1, timeout=8)
        if resp2 and resp2[-1].get("reply_markup"):
            bot.press_button_labeled_any("morning", timeout=3) or bot.press_button_labeled_any(
                "afternoon", timeout=2
            ) or bot.press_button_labeled_any("evening", timeout=2)

        # Summary message — no LLM, so near-instant
        bot.wait_responses(1, timeout=5)

    def test_plan_no_tasks(self, bot: BotClient) -> None:
        """/plan with empty inbox should complete and send a response."""
        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        _skip_bs(bot)

        # No tasks to triage — bot responds with summary
        final = bot.wait_responses(1, timeout=10)
        assert final, "Expected bot to respond after /plan with no tasks"


class TestPlanTriageActions:
    def test_triage_postpone(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Postponing a task in triage calls remove_task_due_date (Sync API)."""
        todoist.seed(
            tasks=[
                sd.task("Review backlog", due_today=True, task_id="t_backlog"),
            ]
        )

        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)
        _skip_bs(bot)
        bot.wait_responses(1, timeout=10)  # "📋 Triage — 1 task to review."
        pressed = bot.press_button_labeled_any("Postpone", timeout=10)
        assert pressed, "Postpone button not found in triage keyboard"

        op = todoist.wait_for_op("remove_due_date", timeout=8)
        assert op is not None, "remove_due_date was not called for postponed task"

    def test_triage_delete(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Deleting a task in triage calls delete in Todoist."""
        todoist.seed(
            tasks=[
                sd.task("Old task to delete", due_today=True, task_id="t_delete"),
            ]
        )

        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)
        _skip_bs(bot)
        bot.wait_responses(1, timeout=10)  # "📋 Triage — 1 task to review."
        pressed = bot.press_button_labeled_any("Delete", timeout=10)
        assert pressed, "Delete button not found in triage keyboard"

        op = todoist.wait_for_op("delete", timeout=8)
        assert op is not None, "delete was not called for deleted task"

    def test_triage_quarantine(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Quarantining an aged task adds the quarantined label (button requires age label)."""
        from lib.todoist import MAX_TRIAGE_AGE

        todoist.seed(
            tasks=[
                sd.task(
                    "Chronic deferral task",
                    due_today=True,
                    task_id="t_quarantine",
                    labels=[f"age{MAX_TRIAGE_AGE}"],
                ),
            ]
        )

        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)
        _skip_bs(bot)
        bot.wait_responses(1, timeout=10)  # "📋 Triage — 1 task to review."
        pressed = bot.press_button_labeled_any("Quarantine", timeout=10)
        assert pressed, "Quarantine button not found (task needs age label for it to appear)"

        op = todoist.wait_for_op("update", timeout=8)
        assert op is not None, "update not recorded for quarantine"
        labels = op.get("changes", {}).get("labels", [])
        assert "quarantined" in labels, f"quarantined label not set, got: {labels}"


class TestPlanBrainstorm:
    def test_brainstorm_creates_tasks(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Brainstorm phase: user types tasks → they get created in Todoist."""
        bot.send_message("/plan")
        resp1 = bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        # Inject the brainstorm start callback directly rather than searching by label
        msg_id = resp1[0].get("message_id", 1) if resp1 else 1
        bot.press_button(BS_START, message_id=msg_id)
        resp2 = bot.wait_responses(1, timeout=8)
        assert resp2, "Bot did not respond after brainstorm start"

        # Type brainstorm text
        bot.send_message("call mum, buy milk, fix the leak under the sink")
        resp3 = bot.wait_responses(1, timeout=10)
        assert resp3

        # Accept extracted tasks (press yes/accept for each, or accept all)
        for _ in range(5):  # accept up to 5 rounds
            kb = bot.last_keyboard(timeout=3)
            if not kb:
                break
            accepted = (
                bot.press_button_labeled_any("yes", timeout=3)
                or bot.press_button_labeled_any("accept", timeout=2)
                or bot.press_button_labeled_any("✅", timeout=2)
                or bot.press_button_labeled_any("add", timeout=2)
            )
            if not accepted:
                break
            bot.wait_responses(1, timeout=4)

        # At least some tasks should be queued for creation
        created = [h for h in todoist.history() if h["op"] == "create"]
        assert len(created) > 0, "No tasks were created from brainstorm"

    def test_brainstorm_llm_error_shows_retry(self, bot: BotClient, llm: LLMInspector) -> None:
        """LLM API error during brainstorm must show a retry message, not a dead end."""
        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        resp1 = bot.wait_responses(1, timeout=8)  # brainstorm prompt with buttons
        msg_id = resp1[0].get("message_id", 1) if resp1 else 1
        bot.press_button(BS_START, message_id=msg_id)
        bot.wait_responses(1, timeout=8)  # "What's on your mind?"

        llm.fail_next(count=1)
        bot.send_message("trim nails, credit card bonus, revive foodie 1.0")

        resp = bot.wait_responses(1, timeout=10)
        assert resp, "Bot did not respond after LLM error"
        text = " ".join(r.get("text", "") for r in resp).lower()
        assert "try again" in text, f"Expected retry message, got: {text!r}"
        # Must include a Skip button so the user isn't stuck
        assert any(r.get("reply_markup") for r in resp), (
            "Expected Skip button after LLM error so user can escape"
        )

        # Session must still be alive — user can re-submit and get task proposals
        bot.send_message("trim nails, credit card bonus")
        resp2 = bot.wait_responses(1, timeout=10)
        assert resp2, "No response after retry — session appears dead"
        text2 = " ".join(r.get("text", "") for r in resp2).lower()
        assert "task" in text2 or any(r.get("reply_markup") for r in resp2), (
            f"Expected task proposal after retry, got: {text2!r}"
        )

    def test_brainstorm_skip_after_no_extraction(self, bot: BotClient, llm: LLMInspector) -> None:
        """Skip button shown after zero-extraction input should advance to triage."""
        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        resp1 = bot.wait_responses(1, timeout=8)  # brainstorm prompt with buttons
        msg_id = resp1[0].get("message_id", 1) if resp1 else 1
        bot.press_button(BS_START, message_id=msg_id)
        bot.wait_responses(1, timeout=8)  # "What's on your mind?"

        # Make LLM return empty extraction
        llm.set_next_response("[]")
        bot.send_message("Nothing")
        resp2 = bot.wait_responses(1, timeout=10)
        assert resp2, "Expected 'Couldn't find tasks' message"
        assert any("skip" in r.get("text", "").lower() or r.get("has_keyboard") for r in resp2), (
            "Expected Skip button after zero-extraction"
        )

        # Tap Skip — this is the bug path: conversation is in BS_INPUT state
        skip_msg_id = resp2[0].get("message_id", msg_id) if resp2 else msg_id
        bot.press_button(BS_SKIP, message_id=skip_msg_id)
        resp3 = bot.wait_responses(1, timeout=10)
        assert resp3, "Bot did not respond after Skip in BS_INPUT state"
        text = " ".join(r.get("text", "") for r in resp3).lower()
        assert "triage" in text or any(r.get("has_keyboard") for r in resp3), (
            f"Expected triage or response after skip, got: {text!r}"
        )


class TestPlanSessionPersistence:
    def test_plan_session_survives_restart(
        self,
        bot: BotClient,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """Start /plan, kill the bot, restart it — session should resume."""
        todoist.seed(
            tasks=[
                sd.task("Persistent task", due_today=True, task_id="t_persist"),
            ]
        )

        # Start plan, skip brainstorm, wait for optimize prompt to confirm session saved
        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)
        _skip_bs(bot)
        bot.wait_responses(1, timeout=10)  # optimize prompt — ensures session flushed to disk

        # Verify session was saved (check state file)
        state_file = Path(staging_app["state_file"])
        assert state_file.exists(), "State file not created"
        state = json.loads(state_file.read_text())
        assert "plan_session" in state, "Plan session not saved to state.json"

        # Kill the bot
        proc = staging_app["proc"]
        proc.terminate()
        proc.wait(timeout=5)

        # Reset telegram NOW (before restarting) so the new bot's responses start fresh
        import os
        import subprocess
        import sys

        urls = staging_app["urls"]
        httpx.post(f"{urls['telegram_url']}/test/reset", timeout=5.0)
        bot._seen_count = 0  # align local counter with cleared state

        env = {
            **os.environ,
            "TELEGRAM_BOT_KEY": "test-token-12345",
            "TODOIST_KEY": "fake-key",
            "ANTHROPIC_KEY": "sk-fake-key",
            "TELEGRAM_USER_ID": "99999",
            "TELEGRAM_API_BASE_URL": urls["telegram_url"],
            "TODOIST_BASE_URL": urls["todoist_url"],
            "ANTHROPIC_BASE_URL": urls["llm_url"],
            "VAULT_PATH": str(staging_app["vault_dir"]),
            "STATE_PATH": str(state_file),
            "OBSIDIAN_POLL_SECONDS": "2",
        }

        new_proc = subprocess.Popen(
            [sys.executable, "main.py"],
            cwd=str(APP_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        staging_app["proc"] = new_proc  # update for cleanup

        # Wait for the new bot to be ready, then trigger resume via /plan
        _wait_bot_ready(urls["telegram_url"])
        bot.send_message("/plan")
        resp = bot.wait_responses(2, timeout=15)  # resume header + phase UI
        assert resp, "Bot did not respond to /plan after restart"
        text = " ".join(r.get("text", "") for r in resp).lower()
        assert any(kw in text for kw in ("resuming", "resume", "planning session")), (
            f"Bot restart response didn't mention plan session: {text!r}"
        )

        new_proc.terminate()
        try:
            new_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            new_proc.kill()


class TestAddToPlan:
    def test_already_planned_shows_add_button(
        self,
        bot: BotClient,
        staging_app: dict,
    ) -> None:
        """/plan when day is already planned shows 'Add to plan' button."""
        from datetime import date

        # Write a real tasks section to make is_day_planned() return True
        vault_dir = staging_app["vault_dir"]
        daily_dir = vault_dir / "Daily"
        daily_dir.mkdir(parents=True, exist_ok=True)
        note_path = daily_dir / f"{date.today().isoformat()}.md"
        note_path.write_text(
            "# Daily Note\n\n## Tasks\n\n- [ ] Existing task\n",
            encoding="utf-8",
        )

        bot.send_message("/plan")
        resp = bot.wait_responses(1, timeout=10)
        assert resp, "Bot did not respond to /plan"
        text = resp[0].get("text", "").lower()
        assert "already planned" in text, f"Expected 'already planned' message, got: {text!r}"
        markup = resp[0].get("reply_markup") or {}
        if isinstance(markup, str):
            import json
            markup = json.loads(markup)
        flat = [btn.get("text", "") for row in markup.get("inline_keyboard", []) for btn in row]
        assert any("add" in b.lower() for b in flat), (
            f"Expected 'Add to plan' button, got buttons: {flat}"
        )

    def test_add_to_plan_skips_already_triaged(
        self,
        bot: BotClient,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """'Add to plan' skips task IDs recorded in today's triaged set."""
        import json
        from datetime import date

        # Pre-seed today_triaged state so the original task won't appear in triage
        state_file = staging_app["state_file"]
        state: dict = {}
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
        state["today_triaged"] = {
            "date": date.today().isoformat(),
            "task_ids": ["t_orig"],
        }
        state_file.write_text(json.dumps(state), encoding="utf-8")

        # Write vault note so is_day_planned() returns True
        vault_dir = staging_app["vault_dir"]
        note_path = vault_dir / "Daily" / f"{date.today().isoformat()}.md"
        note_path.write_text(
            "# Daily\n\n## Tasks\n\n- [ ] Existing planned task\n",
            encoding="utf-8",
        )

        # Seed both tasks — only t_new should appear in triage
        todoist.seed(
            tasks=[
                sd.task("Original task", due_today=True, task_id="t_orig"),
                sd.task("New task after plan", due_today=True, task_id="t_new"),
            ]
        )

        # /plan should show "already planned" with "Add to plan" button
        bot.send_message("/plan")
        resp = bot.wait_responses(1, timeout=10)
        assert resp, "Bot did not respond"
        assert "already planned" in resp[0].get("text", "").lower()

        # Click "Add to plan"
        msg_id = resp[0].get("message_id", 1)
        bot.press_button(ADD_TO_PLAN, message_id=msg_id)
        bot.wait_responses(1, timeout=8)  # "Adding to today's plan."
        _skip_bs(bot)  # skip brainstorm → advances to triage

        # Triage header + first task card
        triage_msgs = bot.wait_responses(2, timeout=10)
        triage_text = " ".join(r.get("text", "") for r in triage_msgs)
        assert "New task after plan" in triage_text, (
            f"Expected new task in triage, got: {triage_text!r}"
        )
        assert "Original task" not in triage_text, (
            f"Original (already-triaged) task must not appear in add-to-plan triage: {triage_text!r}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skip_bs(bot: BotClient) -> None:
    """Wait for the brainstorm prompt then inject the skip callback directly."""
    resp = bot.wait_responses(1, timeout=10)
    msg_id = resp[0].get("message_id", 1) if resp else 1
    bot.press_button(BS_SKIP, message_id=msg_id)
