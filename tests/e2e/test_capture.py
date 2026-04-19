"""E2E tests for basic capture commands: /task, /list, /done, /completed."""

from __future__ import annotations

import pytest

from tests.e2e.helpers import BotClient, TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e


class TestTaskCommand:
    def test_task_enrichment_and_create(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Send /task, confirm enrichment dialog, confirm creation → task exists in Todoist."""
        bot.send_message("/task Buy groceries")

        # Bot sends "Thinking..." first, then the enriched task details
        resp = bot.wait_responses(2, timeout=15)
        assert resp, "Bot did not respond to /task"
        # Check the last (non-interim) response contains task-related content
        text = resp[-1].get("text", "")
        keywords = ("grocery", "buy", "confirm", "✅", "task")
        assert any(kw in text.lower() for kw in keywords) or "✅" in text, (
            f"Expected task enrichment in final response, got: {text!r}"
        )

        # Press confirm — search all captured responses (proposal keyboard was already consumed
        # by wait_responses above, so press_button_labeled_any is needed here)
        bot.press_button_labeled_any("✅", timeout=3) or bot.press_button_labeled_any(
            "confirm", timeout=1
        )

        # Task should appear in Todoist
        created = todoist.wait_for_task("groceries", timeout=10)
        assert created is not None, "Task 'Buy groceries' was not created in Todoist"
        assert "create" in todoist.history_ops()

    def test_task_minimal_title(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/task with a short title produces an enrichment proposal with the title and a keyboard."""
        bot.send_message("/task Call dentist")
        resp = bot.wait_responses(2, timeout=12)  # "Thinking..." + enrichment proposal
        assert resp, "Bot did not respond"
        proposal = resp[-1]
        assert "dentist" in (proposal.get("text") or "").lower(), (
            f"Expected task title in enrichment proposal, got: {proposal.get('text')!r}"
        )
        assert proposal.get("reply_markup"), "Expected enrichment keyboard (confirm/edit buttons)"


class TestListCommand:
    def test_list_shows_seeded_tasks(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/list should show today's tasks."""
        todoist.seed(
            tasks=[
                sd.task("Review pull request", due_today=True, priority=3),
                sd.task("Write tests", due_today=True, priority=2),
            ]
        )

        bot.send_message("/list")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "Bot did not respond to /list"
        text = resp[0].get("text", "")
        # Should mention at least one of the seeded tasks
        assert "review" in text.lower() or "write" in text.lower() or "task" in text.lower()

    def test_list_empty(self, bot: BotClient) -> None:
        """/list with no tasks should tell the user there's nothing scheduled."""
        bot.send_message("/list")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "Bot did not respond to /list"
        text = resp[0].get("text", "").lower()
        assert "no tasks" in text, f"Expected 'no tasks' message, got: {text!r}"


class TestDoneCommand:
    def test_done_completes_task(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/done <id> marks the task complete in Todoist."""
        todoist.seed(tasks=[sd.task("Fix bug", task_id="task_fix_bug", due_today=True)])

        bot.send_message("/done task_fix_bug")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "Bot did not respond to /done"

        completed = todoist.wait_for_op("complete", timeout=8)
        assert completed is not None, "complete operation was not recorded"
        assert completed.get("task_id") == "task_fix_bug"

    def test_done_invalid_id(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/done with nonexistent ID should respond, not silently crash."""
        bot.send_message("/done nonexistent_task_id_xyz")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "Bot did not respond to /done with invalid task ID"
        text = resp[0].get("text", "")
        assert text, "Expected non-empty text response for /done with invalid ID"


class TestCompletedCommand:
    def test_completed_shows_done_tasks(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/completed lists recently completed tasks, including the seeded one."""
        todoist.seed(
            tasks=[
                sd.task("Finished task", completed=True),
            ]
        )

        bot.send_message("/completed")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "Bot did not respond to /completed"
        text = resp[0].get("text", "").lower()
        assert "finished task" in text, (
            f"Expected seeded completed task title in response, got: {text!r}"
        )


class TestTaskEditFlows:
    def _start_task(self, bot: BotClient) -> None:
        bot.send_message("/task Review quarterly report")
        bot.wait_responses(2, timeout=15)  # "Thinking..." + proposal

    def test_edit_title(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/task → Edit title → new title → confirm → task created with new title."""
        self._start_task(bot)
        bot.press_button_labeled_any("Edit title", timeout=5)
        bot.wait_responses(1, timeout=5)  # prompt "Enter new title:"
        bot.send_message("Updated report title")
        bot.wait_responses(1, timeout=8)  # updated proposal
        bot.press_button_labeled_any("✅ Confirm", timeout=5)
        created = todoist.wait_for_task("updated report title", timeout=8)
        assert created is not None, "Task with updated title was not created"

    def test_edit_due_valid(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/task → Edit due → ISO date → proposal updates due field."""
        self._start_task(bot)
        bot.press_button_labeled_any("Edit due", timeout=5)
        bot.wait_responses(1, timeout=5)  # "Enter due date..."
        bot.send_message("2026-12-31")
        resp = bot.wait_responses(1, timeout=8)  # updated proposal
        assert resp, "No response after entering due date"
        assert "2026-12-31" in (resp[-1].get("text") or ""), (
            "Due date not reflected in updated proposal"
        )

    def test_edit_due_invalid_then_valid(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Invalid due date triggers retry prompt; valid date updates proposal."""
        self._start_task(bot)
        bot.press_button_labeled_any("Edit due", timeout=5)
        bot.wait_responses(1, timeout=5)
        bot.send_message("not-a-date")
        retry = bot.wait_responses(1, timeout=5)
        assert retry, "No retry prompt after invalid date"
        assert (
            "iso" in (retry[-1].get("text") or "").lower()
            or "format" in (retry[-1].get("text") or "").lower()
        ), f"Expected format hint, got: {retry[-1].get('text')!r}"
        bot.send_message("2026-06-01")
        resp = bot.wait_responses(1, timeout=8)
        assert resp and "2026-06-01" in (resp[-1].get("text") or "")

    def test_edit_priority(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/task → Edit priority → p1 → proposal shows P1."""
        self._start_task(bot)
        bot.press_button_labeled_any("Edit priority", timeout=5)
        bot.wait_responses(1, timeout=5)
        bot.send_message("p1")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "No response after entering priority"
        assert "p1" in (resp[-1].get("text") or "").lower(), (
            f"Expected P1 in updated proposal, got: {resp[-1].get('text')!r}"
        )

    def test_edit_priority_invalid_then_valid(
        self, bot: BotClient, todoist: TodoistInspector
    ) -> None:
        """Invalid priority triggers retry; valid value updates proposal."""
        self._start_task(bot)
        bot.press_button_labeled_any("Edit priority", timeout=5)
        bot.wait_responses(1, timeout=5)
        bot.send_message("p9")
        retry = bot.wait_responses(1, timeout=5)
        assert retry, "No retry prompt after invalid priority"
        assert (
            "p1" in (retry[-1].get("text") or "").lower()
            or "p4" in (retry[-1].get("text") or "").lower()
        ), f"Expected p1/p4 hint, got: {retry[-1].get('text')!r}"
        bot.send_message("p2")
        resp = bot.wait_responses(1, timeout=8)
        assert resp and "p2" in (resp[-1].get("text") or "").lower()

    def test_edit_duration(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/task → Edit duration → 45 → proposal shows 45 min."""
        self._start_task(bot)
        bot.press_button_labeled_any("Edit duration", timeout=5)
        bot.wait_responses(1, timeout=5)
        bot.send_message("45")
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "No response after entering duration"
        assert "45" in (resp[-1].get("text") or ""), (
            f"Expected '45' in updated proposal, got: {resp[-1].get('text')!r}"
        )

    def test_edit_duration_invalid_then_valid(
        self, bot: BotClient, todoist: TodoistInspector
    ) -> None:
        """Invalid duration triggers retry; valid number updates proposal."""
        self._start_task(bot)
        bot.press_button_labeled_any("Edit duration", timeout=5)
        bot.wait_responses(1, timeout=5)
        bot.send_message("half an hour")
        retry = bot.wait_responses(1, timeout=5)
        assert retry, "No retry prompt after invalid duration"
        assert (
            "number" in (retry[-1].get("text") or "").lower()
            or "minutes" in (retry[-1].get("text") or "").lower()
        ), f"Expected number hint, got: {retry[-1].get('text')!r}"
        bot.send_message("30")
        resp = bot.wait_responses(1, timeout=8)
        assert resp and "30" in (resp[-1].get("text") or "")

    def test_cancel_button(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/task → press Cancel button → bot says Cancelled, no task created."""
        self._start_task(bot)
        bot.press_button_labeled_any("❌ Cancel", timeout=5)
        resp = bot.wait_responses(1, timeout=5)
        assert resp, "No response after pressing Cancel"
        assert "cancel" in (resp[-1].get("text") or "").lower()
        assert not todoist.history_ops(), "Expected no Todoist operations after cancel"

    def test_breakdown_accept(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/task → Break down → Create all → parent + 3 subtasks created."""
        self._start_task(bot)
        bot.press_button_labeled_any("Break down", timeout=5)
        # Bot sends an intermediate edit ("breaking down task…") then the final subtask proposal.
        resp = bot.wait_responses(2, timeout=15)
        assert resp, "No breakdown proposal received"
        text = (resp[-1].get("text") or "").lower()
        assert "subtask" in text or "research" in text, (
            f"Expected breakdown proposal, got: {text!r}"
        )
        bot.press_button_labeled_any("Create all", timeout=5)
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "No confirmation after Create all"
        assert "created" in (resp[-1].get("text") or "").lower()
        # parent + 3 subtasks = 4 creates
        ops = todoist.history_ops()
        assert ops.count("create") >= 4, f"Expected ≥4 creates, got: {ops}"

    def test_breakdown_cancel(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/task → Break down → Cancel → returns to proposal with enrichment keyboard."""
        self._start_task(bot)
        bot.press_button_labeled_any("Break down", timeout=5)
        bot.wait_responses(1, timeout=15)  # breakdown proposal
        bot.press_button_labeled_any("❌ Cancel", timeout=5)
        resp = bot.wait_responses(1, timeout=8)
        assert resp, "No response after breakdown Cancel"
        markup = resp[-1].get("reply_markup")
        assert markup, "Expected enrichment keyboard after breakdown cancel"
