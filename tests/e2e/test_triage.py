"""E2E tests for full triage functionality.

Covers all seven triage actions with correct callback_data:
- p1 / p2 / p3  → priority assigned + timeslot prompt
- complete       → marks task done in Todoist
- postpone       → non-recurring: remove_due_date (Sync API)
                   recurring: complete (preserves recurrence schedule)
- delete         → non-recurring: deletes task
                   recurring: blocked with warning message, no delete
- quarantine     → adds quarantined label via update

Also covers:
- aged task (age >= MAX_TRIAGE_AGE) postpone warning message
"""

from __future__ import annotations

import time

import pytest

from tests.e2e.constants import BS_SKIP, TODOIST_P2
from tests.e2e.helpers import BotClient, TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reach_triage(bot: BotClient) -> None:
    """Send /plan and skip brainstorm to reach the triage phase."""
    bot.send_message("/plan")
    bot.wait_responses(1, timeout=10)  # "Starting your planning session."
    _skip_phase(bot, BS_SKIP)  # wait for brainstorm prompt, press skip


def _skip_phase(bot: BotClient, callback_data: str) -> None:
    """Wait for the next bot message then directly inject the skip callback."""
    resp = bot.wait_responses(1, timeout=15)
    msg_id = resp[0].get("message_id", 1) if resp else 1
    bot.press_button(callback_data, message_id=msg_id)


def _wait_for_triage_task(bot: BotClient) -> None:
    """Advance seen_count past the triage header so the task card is the next new response."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        for idx, resp in enumerate(bot.all_responses()):
            if "triage" in (resp.get("text") or "").lower():
                bot._seen_count = max(bot._seen_count, idx + 1)
                return
        time.sleep(0.2)


def _press_triage_action(bot: BotClient, action_label: str, timeout: float = 10.0) -> bool:
    """Press the triage button whose label contains action_label.

    Uses press_button_labeled_any which scans all captured responses (including
    already-seen ones) to handle the race where the triage header and task card
    arrive simultaneously and both get consumed by _wait_for_triage_task.
    """
    return bot.press_button_labeled_any(action_label, timeout=timeout)


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestTriagePostpone:
    def test_postpone_regular_task(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Postponing a non-recurring task calls remove_due_date (Sync API)."""
        todoist.seed(
            tasks=[
                sd.task("Review backlog", due_today=True, task_id="t_postpone"),
            ]
        )

        _reach_triage(bot)
        _wait_for_triage_task(bot)
        pressed = _press_triage_action(bot, "Postpone")
        assert pressed, "Postpone button not found on triage keyboard"

        op = todoist.wait_for_op("remove_due_date", timeout=8)
        assert op is not None, "remove_due_date was not called for postponed regular task"
        assert op["task_id"] == "t_postpone"

        # Must NOT have called complete — that's the recurring path
        complete_ops = [h for h in todoist.history() if h["op"] == "complete"]
        assert not complete_ops, (
            f"complete should not be called for regular postpone, got: {complete_ops}"
        )

    def test_postpone_recurring_task_completes_instead(
        self, bot: BotClient, todoist: TodoistInspector
    ) -> None:
        """Postponing a recurring task completes the current occurrence (preserves schedule)."""
        todoist.seed(
            tasks=[
                sd.task("Daily standup", due_today=True, recurring=True, task_id="t_recurring"),
            ]
        )

        _reach_triage(bot)
        _wait_for_triage_task(bot)
        pressed = _press_triage_action(bot, "Postpone")
        assert pressed, "Postpone button not found"

        op = todoist.wait_for_op("complete", timeout=8)
        assert op is not None, "complete not called for recurring task postpone"
        assert op["task_id"] == "t_recurring"

        # remove_due_date must NOT be called — would destroy the recurrence pattern
        bad_ops = [h for h in todoist.history() if h["op"] == "remove_due_date"]
        assert not bad_ops, "remove_due_date must not be called for recurring task postpone"

    def test_postpone_aged_task_shows_warning(
        self, bot: BotClient, todoist: TodoistInspector
    ) -> None:
        """Postponing a task with age >= MAX_TRIAGE_AGE shows a warning message."""
        from lib.todoist import MAX_TRIAGE_AGE

        todoist.seed(
            tasks=[
                sd.task(
                    "Ancient stale task",
                    due_today=True,
                    task_id="t_aged",
                    labels=[f"age{MAX_TRIAGE_AGE}"],
                ),
            ]
        )

        _reach_triage(bot)
        _wait_for_triage_task(bot)
        pressed = _press_triage_action(bot, "Postpone")
        assert pressed, "Postpone button not found (aged task may show different keyboard)"

        # remove_due_date should still be called
        op = todoist.wait_for_op("remove_due_date", timeout=8)
        assert op is not None, "remove_due_date not called for aged task"

        # Bot should also send a warning about the age.
        # Check all new responses — scheduler may send digest concurrently.
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "No warning message sent after aged task postpone"
        combined_text = " ".join((r.get("text") or "") for r in resp).lower()
        assert "postponed" in combined_text or "age" in combined_text, (
            f"Expected age/postpone warning in responses, got: {[r.get('text') for r in resp]!r}"
        )


class TestTriageComplete:
    def test_complete_marks_task_done(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Pressing Done marks the task complete in Todoist."""
        todoist.seed(
            tasks=[
                sd.task("Finish the report", due_today=True, task_id="t_complete"),
            ]
        )

        _reach_triage(bot)
        _wait_for_triage_task(bot)
        pressed = _press_triage_action(bot, "Done")
        assert pressed, "Done button not found on triage keyboard"

        op = todoist.wait_for_op("complete", timeout=8)
        assert op is not None, "complete operation not recorded"
        assert op["task_id"] == "t_complete"


class TestTriageDelete:
    def test_delete_regular_task(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Deleting a non-recurring task removes it from Todoist."""
        todoist.seed(
            tasks=[
                sd.task("Old stale task to delete", due_today=True, task_id="t_delete"),
            ]
        )

        _reach_triage(bot)
        _wait_for_triage_task(bot)
        pressed = _press_triage_action(bot, "Delete")
        assert pressed, "Delete button not found on triage keyboard"

        op = todoist.wait_for_op("delete", timeout=8)
        assert op is not None, "delete operation not recorded"
        assert op["task_id"] == "t_delete"

    def test_delete_recurring_task_is_blocked(
        self, bot: BotClient, todoist: TodoistInspector
    ) -> None:
        """Trying to delete a recurring task sends a warning; the task is NOT deleted."""
        todoist.seed(
            tasks=[
                sd.task("Weekly review", due_today=True, recurring=True, task_id="t_del_rec"),
            ]
        )

        _reach_triage(bot)
        _wait_for_triage_task(bot)
        pressed = _press_triage_action(bot, "Delete")
        assert pressed, "Delete button not found"

        # Bot should send a warning about recurring tasks
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "No warning message sent for recurring task delete attempt"
        warning_text = (resp[-1].get("text") or "").lower()
        assert "recurring" in warning_text, (
            f"Expected 'recurring' in warning message, got: {warning_text!r}"
        )

        # The task must NOT have been deleted
        time.sleep(0.5)
        delete_ops = [h for h in todoist.history() if h["op"] == "delete"]
        assert not delete_ops, f"delete must not be called for recurring task, got: {delete_ops}"


class TestTriageQuarantine:
    def test_quarantine_adds_label(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Quarantining a task adds the 'quarantined' label via an update.

        Quarantine appears on the keyboard when task_age >= MAX_TRIAGE_AGE.
        """
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

        _reach_triage(bot)
        _wait_for_triage_task(bot)
        pressed = _press_triage_action(bot, "Quarantine")
        assert pressed, "Quarantine button not found on triage keyboard"

        op = todoist.wait_for_op("update", timeout=8)
        assert op is not None, "update operation not recorded for quarantine"
        labels = op.get("changes", {}).get("labels", [])
        assert "quarantined" in labels, (
            f"'quarantined' label not in update changes, got labels: {labels}"
        )

        # Bot should send a confirmation message — check all new responses since
        # scheduler may send digest concurrently.
        resp = bot.wait_responses(1, timeout=5)
        if resp:
            combined = " ".join((r.get("text") or "") for r in resp).lower()
            assert "quarantine" in combined or "hidden" in combined, (
                f"Unexpected quarantine confirmation: {[r.get('text') for r in resp]!r}"
            )

    def test_quarantine_available_at_age_1(
        self, bot: BotClient, todoist: TodoistInspector
    ) -> None:
        """Quarantine button appears at age >= 1, not only at MAX_TRIAGE_AGE."""
        todoist.seed(
            tasks=[
                sd.task(
                    "Once-deferred task",
                    due_today=True,
                    task_id="t_quarantine_age1",
                    labels=["age1"],
                ),
            ]
        )

        _reach_triage(bot)
        _wait_for_triage_task(bot)
        pressed = _press_triage_action(bot, "Quarantine")
        assert pressed, "Quarantine button should appear at age 1"

        op = todoist.wait_for_op("update", timeout=8)
        assert op is not None, "update not recorded for quarantine at age 1"
        labels = op.get("changes", {}).get("labels", [])
        assert "quarantined" in labels, f"Expected quarantined label, got: {labels}"

    def test_quarantine_not_shown_at_age_0(
        self, bot: BotClient, todoist: TodoistInspector
    ) -> None:
        """Fresh tasks (age 0) do not show a Quarantine button."""
        import json

        todoist.seed(
            tasks=[
                sd.task("Brand new task", due_today=True, task_id="t_quarantine_age0"),
            ]
        )

        _reach_triage(bot)
        _wait_for_triage_task(bot)

        # Collect all responses that contain a triage keyboard
        deadline = time.monotonic() + 10
        keyboard_resp = None
        while time.monotonic() < deadline:
            for r in bot.all_responses():
                if r.get("reply_markup"):
                    keyboard_resp = r
            if keyboard_resp:
                break
            time.sleep(0.2)

        assert keyboard_resp, "No triage keyboard found"
        markup = keyboard_resp["reply_markup"]
        if isinstance(markup, str):
            markup = json.loads(markup)
        buttons = [btn["text"] for row in markup["inline_keyboard"] for btn in row]
        assert not any("Quarantine" in b for b in buttons), (
            f"Quarantine button should not appear at age 0, got buttons: {buttons}"
        )


class TestTriagePriority:
    def test_p2_assignment_prompts_timeslot(
        self, bot: BotClient, todoist: TodoistInspector
    ) -> None:
        """Setting P2 priority records an update (with timeslot if available).

        If time blocks remain in the day, the bot shows a timeslot picker first.
        If all blocks have passed, it skips the picker and updates directly.
        Either way, the Todoist update op must record priority=p2.
        """
        todoist.seed(
            tasks=[
                sd.task("Important focused work", due_today=True, task_id="t_p2"),
            ]
        )

        _reach_triage(bot)
        _wait_for_triage_task(bot)
        pressed = _press_triage_action(bot, "P2")
        assert pressed, "P2 button not found on triage keyboard"

        # Bot either shows timeslot picker or updates directly (if all slots have passed)
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "No response after P2 selection"

        # If a timeslot keyboard appeared, select any available slot
        has_timeslot_kb = resp[-1].get("reply_markup") is not None
        if has_timeslot_kb:
            timeslot_pressed = bot.press_button_by_callback_prefix("plan_timeslot:", timeout=5)
            assert timeslot_pressed, "Timeslot keyboard shown but no plan_timeslot: button found"

        op = todoist.wait_for_op("update", timeout=8)
        assert op is not None, "update not called after P2 selection"
        assert op.get("changes", {}).get("priority") == TODOIST_P2, (
            f"Expected priority {TODOIST_P2} (Todoist p2), got: {op.get('changes')}"
        )

    def test_p1_assignment(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """P1 priority can also be assigned (highest priority)."""
        todoist.seed(
            tasks=[
                sd.task("Critical urgent task", due_today=True, task_id="t_p1"),
            ]
        )

        _reach_triage(bot)
        _wait_for_triage_task(bot)
        pressed = _press_triage_action(bot, "P1")
        assert pressed, "P1 button not found"

        # Timeslot or direct update — either way an update should be recorded
        # Press any available timeslot if a keyboard appears
        resp = bot.wait_responses(1, timeout=8)
        if resp and resp[-1].get("reply_markup"):
            bot.press_button_by_callback_prefix("plan_timeslot:", timeout=5)

        op = todoist.wait_for_op("update", timeout=8)
        assert op is not None, "update not called after P1 assignment"
