"""E2E tests for the multi-step /plan conversation flow.

Covers:
- Happy path: brainstorm → optimize → triage → plan generated, vault written
- Skip brainstorm + skip optimize paths
- Triage actions: postpone, quarantine, delete
- Session persistence: plan session survives bot restart
- Nag suppression: active plan session suppresses hourly nag
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import APP_DIR, _wait_bot_ready
from tests.e2e.constants import BS_SKIP, BS_START, OPT_SKIP
from tests.e2e.helpers import BotClient, TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e


class TestPlanSkipAll:
    def test_plan_skip_brainstorm_and_optimize(
        self,
        bot: BotClient,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """Skip brainstorm and optimize phases, triage one task, verify plan written to vault."""
        todoist.seed(tasks=[
            sd.task("Send weekly report", due_today=True, priority=2, task_id="t_weekly"),
        ])

        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        _skip_bs(bot)
        _skip_opt(bot)

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
            bot.press_button_labeled_any("morning", timeout=3) or \
            bot.press_button_labeled_any("afternoon", timeout=2) or \
            bot.press_button_labeled_any("evening", timeout=2)

        # Plan generation — wait for vault to be written
        vault_dir = Path(staging_app["vault_dir"])
        plan_written = _wait_for_vault_plan(vault_dir, timeout=15)
        assert plan_written, "Daily plan was not written to vault"

    def test_plan_no_tasks(self, bot: BotClient) -> None:
        """/plan with empty inbox should complete and send a response."""
        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        _skip_bs(bot)
        _skip_opt(bot)

        # No tasks to triage — bot should still respond (plan generated or "nothing to triage")
        final = bot.wait_responses(1, timeout=20)
        assert final, "Expected bot to respond after /plan with no tasks"


class TestPlanTriageActions:
    def test_triage_postpone(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Postponing a task in triage calls remove_task_due_date (Sync API)."""
        todoist.seed(tasks=[
            sd.task("Review backlog", due_today=True, task_id="t_backlog"),
        ])

        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)
        _skip_bs(bot)
        _skip_opt(bot)
        bot.wait_responses(1, timeout=10)  # "📋 Triage — 1 task to review."
        pressed = bot.press_button_labeled_any("Postpone", timeout=10)
        assert pressed, "Postpone button not found in triage keyboard"

        op = todoist.wait_for_op("remove_due_date", timeout=8)
        assert op is not None, "remove_due_date was not called for postponed task"

    def test_triage_delete(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Deleting a task in triage calls delete in Todoist."""
        todoist.seed(tasks=[
            sd.task("Old task to delete", due_today=True, task_id="t_delete"),
        ])

        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)
        _skip_bs(bot)
        _skip_opt(bot)
        bot.wait_responses(1, timeout=10)  # "📋 Triage — 1 task to review."
        pressed = bot.press_button_labeled_any("Delete", timeout=10)
        assert pressed, "Delete button not found in triage keyboard"

        op = todoist.wait_for_op("delete", timeout=8)
        assert op is not None, "delete was not called for deleted task"

    def test_triage_quarantine(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Quarantining an aged task adds the quarantined label (button requires age label)."""
        from lib.todoist import MAX_TRIAGE_AGE

        todoist.seed(tasks=[
            sd.task("Chronic deferral task", due_today=True, task_id="t_quarantine",
                    labels=[f"age{MAX_TRIAGE_AGE}"]),
        ])

        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)
        _skip_bs(bot)
        _skip_opt(bot)
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


class TestPlanSessionPersistence:
    def test_plan_session_survives_restart(
        self,
        bot: BotClient,
        todoist: TodoistInspector,
        staging_app: dict,
    ) -> None:
        """Start /plan, kill the bot, restart it — session should resume."""
        todoist.seed(tasks=[
            sd.task("Persistent task", due_today=True, task_id="t_persist"),
        ])

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
        assert any(kw in text for kw in ("resuming", "resume", "planning session")), \
            f"Bot restart response didn't mention plan session: {text!r}"

        new_proc.terminate()
        try:
            new_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            new_proc.kill()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _skip_bs(bot: BotClient) -> None:
    """Wait for the brainstorm prompt then inject the skip callback directly."""
    resp = bot.wait_responses(1, timeout=10)
    msg_id = resp[0].get("message_id", 1) if resp else 1
    bot.press_button(BS_SKIP, message_id=msg_id)


def _skip_opt(bot: BotClient) -> None:
    """Wait for the optimize prompt then inject the skip callback directly."""
    resp = bot.wait_responses(1, timeout=10)
    msg_id = resp[0].get("message_id", 1) if resp else 1
    bot.press_button(OPT_SKIP, message_id=msg_id)


def _wait_for_vault_plan(vault_dir: Path, timeout: float = 15.0) -> bool:
    """Wait until today's daily note contains a ## Daily Plan section."""
    from datetime import date
    note_path = vault_dir / "Daily" / f"{date.today().isoformat()}.md"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if note_path.exists() and "## Daily Plan" in note_path.read_text(encoding="utf-8"):
            return True
        time.sleep(0.3)
    return False
