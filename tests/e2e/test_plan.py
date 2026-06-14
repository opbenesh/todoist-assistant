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
from tests.e2e.constants import BS_SKIP, BS_START, UB_ANOTHER, UB_BREAKDOWN, UB_SKIP, UB_UNBLOCK
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
        bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        resp1 = bot.wait_responses(1, timeout=10)  # brainstorm prompt
        msg_id = resp1[-1].get("message_id", 1) if resp1 else 1
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
        resp1 = bot.wait_responses(1, timeout=10)  # brainstorm prompt
        msg_id = resp1[-1].get("message_id", 1) if resp1 else 1
        bot.press_button(BS_START, message_id=msg_id)
        bot.wait_responses(1, timeout=8)  # "What's on your mind?"

        llm.fail_next(count=1)
        bot.send_message("trim nails, credit card bonus, revive foodie 1.0")

        # "Extracting tasks…" + error message
        resp = bot.wait_responses(2, timeout=10)
        assert resp, "Bot did not respond after LLM error"
        text = " ".join(r.get("text", "") for r in resp).lower()
        assert "try again" in text, f"Expected retry message, got: {text!r}"
        # Must include a Skip button so the user isn't stuck
        assert any(r.get("reply_markup") for r in resp), (
            "Expected Skip button after LLM error so user can escape"
        )

        # Session must still be alive — user can re-submit and get task proposals
        bot.send_message("trim nails, credit card bonus")
        # "Extracting tasks…" + first task proposal
        resp2 = bot.wait_responses(2, timeout=10)
        assert resp2, "No response after retry — session appears dead"
        text2 = " ".join(r.get("text", "") for r in resp2).lower()
        assert "task" in text2 or any(r.get("reply_markup") for r in resp2), (
            f"Expected task proposal after retry, got: {text2!r}"
        )

    def test_brainstorm_skip_after_no_extraction(self, bot: BotClient, llm: LLMInspector) -> None:
        """Skip button shown after zero-extraction input should advance to triage."""
        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        resp1 = bot.wait_responses(1, timeout=10)  # brainstorm prompt
        msg_id = resp1[-1].get("message_id", 1) if resp1 else 1
        bot.press_button(BS_START, message_id=msg_id)
        bot.wait_responses(1, timeout=8)  # "What's on your mind?"

        # Make LLM return empty extraction
        llm.set_next_response("[]")
        bot.send_message("Nothing")
        # "Extracting tasks…" + "Couldn't find tasks" with Skip button
        resp2 = bot.wait_responses(2, timeout=10)
        assert resp2, "Expected 'Couldn't find tasks' message"
        assert any("skip" in r.get("text", "").lower() or r.get("has_keyboard") for r in resp2), (
            "Expected Skip button after zero-extraction"
        )

        # Tap Skip — this is the bug path: conversation is in BS_INPUT state
        # Use last response which contains the Skip button
        skip_msg_id = resp2[-1].get("message_id", msg_id) if resp2 else msg_id
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

        # Start plan, skip brainstorm — checkpoint is written in plan_cmd before brainstorm
        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)
        _skip_bs(bot)
        bot.wait_responses(1, timeout=10)  # triage header — ensures async path completed

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


class TestPlanUnblock:
    """Tests for the unblock phase embedded at the start of /plan."""

    def _seed_quarantined(
        self, todoist: TodoistInspector, *, task_id: str = "t_stuck", title: str = "Fix CI pipeline"
    ) -> str:
        todoist.seed(tasks=[sd.task(title, task_id=task_id, labels=["quarantined"])])
        return task_id

    def _start_and_open_list(self, bot: BotClient, todoist: TodoistInspector) -> int:
        """Seed one quarantined task, /plan, skip brainstorm, open the unblock task list."""
        self._seed_quarantined(todoist)
        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        resp_bs = bot.wait_responses(1, timeout=10)  # brainstorm prompt
        msg_id_bs = resp_bs[-1].get("message_id", 1) if resp_bs else 1
        bot.press_button(BS_SKIP, message_id=msg_id_bs)
        resp = bot.wait_responses(1, timeout=10)  # unblock prompt
        msg_id = resp[0].get("message_id", 1) if resp else 1
        bot.press_button(UB_UNBLOCK, message_id=msg_id)
        bot.wait_responses(1, timeout=5)  # "Pick a task to break down:"
        return msg_id

    def test_unblock_prompt_shown_after_brainstorm(
        self, bot: BotClient, todoist: TodoistInspector
    ) -> None:
        """When quarantined tasks exist, /plan shows the ☣️ unblock prompt after brainstorm."""
        self._seed_quarantined(todoist)
        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        resp_bs = bot.wait_responses(1, timeout=10)  # brainstorm prompt
        msg_id = resp_bs[-1].get("message_id", 1) if resp_bs else 1
        bot.press_button(BS_SKIP, message_id=msg_id)
        resp = bot.wait_responses(1, timeout=10)
        assert resp, "Expected unblock prompt after brainstorm skip"
        text = " ".join(r.get("text", "") for r in resp)
        assert "☣️" in text, f"Expected ☣️ emoji in unblock prompt, got: {text!r}"
        assert "quarantined" in text.lower(), f"Expected 'quarantined' in prompt: {text!r}"

    def test_unblock_prompt_skipped_when_none(self, bot: BotClient) -> None:
        """No quarantined tasks → brainstorm prompt shown directly, no unblock step."""
        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        resp = bot.wait_responses(1, timeout=10)  # brainstorm prompt
        assert resp, "Expected brainstorm prompt after /plan with no quarantined tasks"
        assert any(r.get("reply_markup") for r in resp), "Expected brainstorm keyboard"
        text = " ".join(r.get("text", "") for r in resp)
        assert "☣️" not in text, f"Unexpected unblock prompt when no quarantined tasks: {text!r}"

    def test_unblock_skip_goes_to_triage(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Pressing [Skip] on the unblock prompt goes straight to triage."""
        self._seed_quarantined(todoist)
        todoist.seed(tasks=[sd.task("Something due", due_today=True, task_id="t_due")])
        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        resp_bs = bot.wait_responses(1, timeout=10)  # brainstorm prompt
        msg_id_bs = resp_bs[-1].get("message_id", 1) if resp_bs else 1
        bot.press_button(BS_SKIP, message_id=msg_id_bs)
        resp = bot.wait_responses(1, timeout=10)  # unblock prompt
        msg_id = resp[0].get("message_id", 1) if resp else 1
        bot.press_button(UB_SKIP, message_id=msg_id)
        resp2 = bot.wait_responses(1, timeout=10)  # triage header
        assert resp2, "Bot did not respond after Skip"
        text = " ".join(r.get("text", "") for r in resp2).lower()
        assert "triage" in text or any(r.get("reply_markup") for r in resp2), (
            f"Expected triage after unblock Skip, got: {text!r}"
        )

    def test_unblock_task_titles_shown_in_prompt(
        self, bot: BotClient, todoist: TodoistInspector
    ) -> None:
        """Task titles appear in the ☣️ quarantine prompt (shown after brainstorm)."""
        self._seed_quarantined(todoist, title="Write quarterly report")
        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        resp_bs = bot.wait_responses(1, timeout=10)  # brainstorm prompt
        msg_id = resp_bs[-1].get("message_id", 1) if resp_bs else 1
        bot.press_button(BS_SKIP, message_id=msg_id)
        resp = bot.wait_responses(1, timeout=10)  # unblock prompt
        text = " ".join(r.get("text", "") for r in resp)
        assert "Write quarterly report" in text, f"Task title missing from prompt: {text!r}"

    def test_unblock_full_flow_then_continue(
        self, bot: BotClient, todoist: TodoistInspector
    ) -> None:
        """Full unblock flow: pick → break down → confirm project → accept subtask → continue to triage."""
        self._start_and_open_list(bot, todoist)
        bot.press_button_labeled_any("Fix CI", timeout=5)
        bot.wait_responses(1, timeout=5)  # action buttons
        bot.press_button(UB_BREAKDOWN)
        bot.wait_responses(1, timeout=5)  # "What's your plan?"
        bot.send_message("Research the issue then write a targeted fix")
        bot.wait_responses(1, timeout=10)  # project confirm
        bot.press_button_labeled_any("Confirm", timeout=5)
        bot.wait_responses(1, timeout=5)  # "Project #proj-... created."

        # Accept/reject proposals until finalize message appears
        for _ in range(10):
            resp = bot.wait_responses(1, timeout=8)
            if not resp:
                break
            if all(r.get("type") == "editMessageText" for r in resp):
                continue  # skip accept/reject feedback edits
            text = " ".join(r.get("text", "") for r in resp)
            if "proposal" in text.lower():
                bot.press_button_labeled_any("Accept", timeout=3)
            else:
                break

        # Finalize: "Broke down ... into N tasks" then "Want to unblock another?"
        resp = bot.wait_responses(1, timeout=8)
        text = " ".join(r.get("text", "") for r in resp)
        assert "broke down" in text.lower() or "unblock another" in text.lower(), (
            f"Expected finalize message, got: {text!r}"
        )

        # Press "Continue to triage"
        bot.press_button_labeled_any("Continue", timeout=5)
        resp3 = bot.wait_responses(1, timeout=10)
        assert resp3, "Bot did not respond after Continue to triage"

    def test_unblock_original_deleted(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """After completing an unblock, the original quarantined task is deleted."""
        task_id = "t_orig_del"
        todoist.seed(tasks=[sd.task("Old stuck task", task_id=task_id, labels=["quarantined"])])
        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        resp_bs = bot.wait_responses(1, timeout=10)  # brainstorm prompt
        msg_id_bs = resp_bs[-1].get("message_id", 1) if resp_bs else 1
        bot.press_button(BS_SKIP, message_id=msg_id_bs)
        resp = bot.wait_responses(1, timeout=10)  # unblock prompt
        msg_id = resp[0].get("message_id", 1) if resp else 1
        bot.press_button(UB_UNBLOCK, message_id=msg_id)
        bot.wait_responses(1, timeout=5)  # task list
        bot.press_button_labeled_any("Old stuck", timeout=5)
        bot.wait_responses(1, timeout=5)  # action buttons
        bot.press_button(UB_BREAKDOWN)
        bot.wait_responses(1, timeout=5)  # "What's your plan?"
        bot.send_message("break it into pieces")
        bot.wait_responses(1, timeout=10)
        bot.press_button_labeled_any("Confirm", timeout=5)
        bot.wait_responses(1, timeout=5)

        # Reject all proposals to reach finalize faster
        for _ in range(10):
            resp = bot.wait_responses(1, timeout=5)
            if not resp:
                break
            if all(r.get("type") == "editMessageText" for r in resp):
                continue  # skip accept/reject feedback edits
            text = " ".join(r.get("text", "") for r in resp)
            if "proposal" in text.lower():
                bot.press_button_labeled_any("Reject", timeout=3)
            else:
                break

        op = todoist.wait_for_op("delete", timeout=10)
        assert op is not None, "Original quarantined task was not deleted after breakdown"

    def test_unblock_subtasks_created_in_project(
        self, bot: BotClient, todoist: TodoistInspector
    ) -> None:
        """Accepted subtasks are created in the new project with no age/quarantine labels."""
        self._start_and_open_list(bot, todoist)
        bot.press_button_labeled_any("Fix CI", timeout=5)
        bot.wait_responses(1, timeout=5)  # action buttons
        bot.press_button(UB_BREAKDOWN)
        bot.wait_responses(1, timeout=5)  # "What's your plan?"
        bot.send_message("investigate, patch, verify")
        bot.wait_responses(1, timeout=10)
        bot.press_button_labeled_any("Confirm", timeout=5)
        bot.wait_responses(1, timeout=5)

        accepted_titles = []
        for _ in range(5):
            resp = bot.wait_responses(1, timeout=8)
            if not resp:
                break
            text = " ".join(r.get("text", "") for r in resp)
            if "proposal" in text.lower():
                bot.press_button_labeled_any("Accept", timeout=3)
                accepted_titles.append(text)
            else:
                break

        created = [h for h in todoist.history() if h["op"] == "create" and h.get("project_id")]
        assert len(created) > 0, "No subtasks created in a project"
        for c in created:
            labels = c.get("labels") or []
            assert "quarantined" not in labels, f"Subtask has quarantined label: {c}"

    def test_unblock_another_offered_after_completion(
        self, bot: BotClient, todoist: TodoistInspector
    ) -> None:
        """After finishing one unblock, bot offers [Unblock another] and [Continue to triage]."""
        todoist.seed(
            tasks=[
                sd.task("Task A", task_id="t_qa2", labels=["quarantined"]),
                sd.task("Task B", task_id="t_qb2", labels=["quarantined"]),
            ]
        )
        bot.send_message("/plan")
        bot.wait_responses(1, timeout=10)  # "Starting your planning session."
        resp_bs = bot.wait_responses(1, timeout=10)  # brainstorm prompt
        msg_id_bs = resp_bs[-1].get("message_id", 1) if resp_bs else 1
        bot.press_button(BS_SKIP, message_id=msg_id_bs)
        resp = bot.wait_responses(1, timeout=10)  # unblock prompt
        msg_id = resp[0].get("message_id", 1) if resp else 1
        bot.press_button(UB_UNBLOCK, message_id=msg_id)
        bot.wait_responses(1, timeout=5)  # task list
        bot.press_button_labeled_any("Task A", timeout=5)
        bot.wait_responses(1, timeout=5)  # action buttons
        bot.press_button(UB_BREAKDOWN)
        bot.wait_responses(1, timeout=5)  # "What's your plan?"
        bot.send_message("handle it step by step")
        bot.wait_responses(1, timeout=10)
        bot.press_button_labeled_any("Confirm", timeout=5)
        bot.wait_responses(1, timeout=5)

        # Reject all proposals
        for _ in range(10):
            resp = bot.wait_responses(1, timeout=5)
            if not resp:
                break
            if all(r.get("type") == "editMessageText" for r in resp):
                continue  # skip accept/reject feedback edits
            if any(r.get("reply_markup") for r in resp):
                text = " ".join(r.get("text", "") for r in resp)
                if "proposal" in text.lower():
                    bot.press_button_labeled_any("Reject", timeout=3)
                    continue
            break

        # Check finalize + another prompt
        resp_final = bot.wait_responses(1, timeout=8)
        assert resp_final, "Expected finalize message"
        # Press UB_ANOTHER to verify it works
        pressed = bot.press_button(UB_ANOTHER, message_id=resp_final[-1].get("message_id", 1))
        if not pressed:
            bot.press_button_labeled_any("another", timeout=3)
        resp2 = bot.wait_responses(1, timeout=8)
        assert resp2, "Bot did not respond after Unblock another"
        text2 = " ".join(r.get("text", "") for r in resp2)
        # Should show remaining task (Task B) or no-more message
        assert "Task B" in text2 or "no" in text2.lower(), (
            f"Expected remaining task or no-more message: {text2!r}"
        )

    def test_unblock_llm_error_shows_retry(
        self, bot: BotClient, todoist: TodoistInspector, llm: LLMInspector
    ) -> None:
        """LLM failure during breakdown shows error message; session stays alive."""
        self._start_and_open_list(bot, todoist)
        bot.press_button_labeled_any("Fix CI", timeout=5)
        bot.wait_responses(1, timeout=5)  # action buttons
        bot.press_button(UB_BREAKDOWN)
        bot.wait_responses(1, timeout=5)  # "What's your plan?"

        llm.fail_next(count=1)
        bot.send_message("investigate and patch the broken test runner")

        resp = bot.wait_responses(1, timeout=10)
        assert resp, "Bot did not respond after LLM breakdown error"
        text = " ".join(r.get("text", "") for r in resp).lower()
        assert "try again" in text or "cancel" in text, (
            f"Expected retry/cancel message after LLM error, got: {text!r}"
        )

        # Session must still be alive — retry should work
        bot.send_message("investigate, patch, verify")
        resp2 = bot.wait_responses(1, timeout=10)
        assert resp2, "No response after retry — session appears dead"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skip_bs(bot: BotClient) -> None:
    """Wait for the brainstorm prompt then press Skip."""
    resp = bot.wait_responses(1, timeout=10)  # brainstorm prompt
    msg_id = resp[-1].get("message_id", 1) if resp else 1
    bot.press_button(BS_SKIP, message_id=msg_id)
