"""E2E tests for /unblock — single-task on-demand breakdown flow."""

from __future__ import annotations

import pytest

from tests.e2e.helpers import BotClient, TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e

_TASK_ID = "t_opt_task"
_AGED_ID = "t_opt_aged"
_QUARANTINE_ID = "t_opt_quarantined"
_QUARANTINE_ID2 = "t_opt_quarantined2"


class TestUnblockTaskList:
    def test_shows_quarantined_task_list(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """/unblock with a quarantined task shows an inline keyboard."""
        todoist.seed(
            tasks=[
                sd.task("Stalled project", task_id=_QUARANTINE_ID, labels=["quarantined"]),
            ]
        )
        bot.send_message("/unblock")
        resp = bot.wait_responses(2, timeout=15)  # "Fetching…" + list
        assert any(r.get("reply_markup") for r in resp), "Expected inline keyboard with task list"

    def test_no_tasks(self, bot: BotClient) -> None:
        """/unblock with empty inbox shows a 'no quarantined tasks' message."""
        bot.send_message("/unblock")
        resp = bot.wait_responses(2, timeout=15)
        text = " ".join(r.get("text", "") for r in resp).lower()
        assert "no" in text and "quarantine" in text or "no tasks" in text, (
            f"Expected empty-state message, got: {text!r}"
        )

    def test_no_quarantined_tasks(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Tasks exist but none quarantined → 'No quarantined tasks found' message."""
        todoist.seed(
            tasks=[
                sd.task("Regular task", task_id=_TASK_ID),
                sd.task("Aged task", task_id=_AGED_ID, labels=["age3"]),
            ]
        )
        bot.send_message("/unblock")
        resp = bot.wait_responses(2, timeout=15)
        text = " ".join(r.get("text", "") for r in resp).lower()
        assert "quarantine" in text, (
            f"Expected 'quarantined' in response when no quarantined tasks exist, got: {text!r}"
        )
        assert not any(r.get("reply_markup") for r in resp), (
            "Expected no task keyboard when no quarantined tasks"
        )

    def test_only_quarantined_shown(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Only quarantined tasks appear in the list; regular tasks are excluded."""
        import json

        todoist.seed(
            tasks=[
                sd.task("Regular task", task_id=_TASK_ID),
                sd.task("Quarantined task", task_id=_QUARANTINE_ID, labels=["quarantined"]),
            ]
        )
        bot.send_message("/unblock")
        resp = bot.wait_responses(2, timeout=15)
        keyboard_resp = next((r for r in resp if r.get("reply_markup")), None)
        assert keyboard_resp, "Expected task list keyboard"
        markup = keyboard_resp["reply_markup"]
        if isinstance(markup, str):
            markup = json.loads(markup)
        buttons = [btn["text"] for row in markup["inline_keyboard"] for btn in row]
        assert any("Quarantined task" in b for b in buttons), (
            f"Expected quarantined task in list, got: {buttons}"
        )
        assert not any("Regular task" in b for b in buttons), (
            f"Non-quarantined task should not appear in unblock list, got: {buttons}"
        )


class TestUnblockBreakdown:
    def _start_and_pick(self, bot: BotClient, todoist: TodoistInspector) -> None:
        todoist.seed(
            tasks=[sd.task("Plan the project", task_id=_QUARANTINE_ID, labels=["quarantined"])]
        )
        bot.send_message("/unblock")
        bot.wait_responses(2, timeout=15)  # "Fetching…" + list
        # Pick the only task by pressing its button
        pressed = bot.press_button_labeled_any("Plan the project", timeout=8)
        assert pressed, "Could not press task button in list"

    def test_breakdown_flow(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Pick a task → describe plan → confirm project → accept 1 proposal → original deleted."""
        self._start_and_pick(bot, todoist)

        prompt = bot.wait_responses(1, timeout=8)
        assert prompt, "No plan prompt after picking task"
        assert "plan" in (prompt[-1].get("text") or "").lower()

        bot.send_message("Research first, then write a draft")
        bot.wait_responses(1, timeout=15)  # project confirm
        bot.press_button_labeled_any("✅ Confirm", timeout=5)

        bot.wait_responses(2, timeout=15)  # "Project created" + first proposal
        bot.press_button_labeled_any("✅ Accept", timeout=5)

        # Drain remaining proposals — use press_button_labeled_any which polls all seen responses
        for _ in range(6):
            if not bot.press_button_labeled_any("❌ Reject", timeout=4):
                break  # no more proposal buttons

        finish = bot.find_response_containing("done", timeout=10) or bot.find_response_containing(
            "created", timeout=5
        )
        assert finish, "No finish summary message"

        assert todoist.wait_for_op("delete", timeout=5) is not None, (
            "Expected original task deleted after breakdown"
        )
        assert todoist.wait_for_op("create", timeout=5) is not None, (
            "Expected new subtask created from accepted proposal"
        )

    def test_new_tasks_have_no_age_labels(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Subtasks created from breakdown should not carry age or quarantine labels."""
        todoist.seed(
            tasks=[
                sd.task(
                    "Aged task to break down",
                    task_id=_QUARANTINE_ID,
                    labels=["quarantined", "age5"],
                ),
            ]
        )
        bot.send_message("/unblock")
        bot.wait_responses(2, timeout=15)
        pressed = bot.press_button_labeled_any("Aged task", timeout=8)
        assert pressed, "Could not find aged task in list"

        bot.wait_responses(1, timeout=8)  # plan prompt
        bot.send_message("Split into two parts")
        bot.wait_responses(1, timeout=15)  # project confirm
        bot.press_button_labeled_any("✅ Confirm", timeout=5)
        bot.wait_responses(2, timeout=15)  # "Project created" + first proposal
        bot.press_button_labeled_any("✅ Accept", timeout=5)

        # Drain remaining proposals
        for _ in range(5):
            resp = bot.wait_responses(1, timeout=8)
            if not resp:
                break
            if any(r.get("reply_markup") for r in resp):
                bot.press_button_labeled_any("❌ Reject", timeout=5)
            else:
                break

        bot.wait_responses(1, timeout=10)  # finish

        create_op = todoist.wait_for_op("create", timeout=5)
        assert create_op is not None, "Expected create op"
        created_labels = create_op.get("labels") or []
        assert "age5" not in created_labels, "New task should not inherit age label"
        assert "quarantined" not in created_labels, "New task should not inherit quarantined label"


class TestUnblockProject:
    """Tests for project creation and task numbering during breakdown."""

    def _pick_task(self, bot: BotClient, todoist: TodoistInspector, title: str) -> None:
        todoist.seed(tasks=[sd.task(title, task_id=_QUARANTINE_ID, labels=["quarantined"])])
        bot.send_message("/unblock")
        bot.wait_responses(2, timeout=15)
        pressed = bot.press_button_labeled_any(title[:20], timeout=8)
        assert pressed, f"Could not press button for task: {title!r}"
        bot.wait_responses(1, timeout=8)  # plan prompt

    def test_project_confirmation_shown(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """After user describes plan, a project name confirmation message appears."""
        self._pick_task(bot, todoist, "Plan the project")
        bot.send_message("Research first, then write a draft")
        # "Breaking down task…" + project confirm
        resp = bot.wait_responses(2, timeout=15)
        assert resp, "No response after plan input"
        text = (resp[-1].get("text") or "").lower()
        assert "#proj-" in text, f"Expected project suggestion with #proj- prefix, got: {text!r}"
        assert resp[-1].get("reply_markup"), "Expected ✅ Confirm button"

    def test_project_created_on_confirm(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Pressing ✅ Confirm creates the project and starts proposals."""
        self._pick_task(bot, todoist, "Plan the project")
        bot.send_message("Research first, then write a draft")
        bot.wait_responses(2, timeout=15)  # "Breaking down task…" + project confirm msg
        bot.press_button_labeled_any("✅ Confirm", timeout=5)

        # Should see "Project created" + first proposal
        resp = bot.wait_responses(2, timeout=15)
        texts = " ".join(r.get("text", "") for r in resp).lower()
        assert "#proj-" in texts, f"Expected project created message, got: {texts!r}"

        proj_op = todoist.wait_for_op("create_project", timeout=5)
        assert proj_op is not None, "Expected create_project op in history"
        assert proj_op.get("name", "").startswith("#proj-"), (
            f"Project name should start with #proj-, got: {proj_op.get('name')!r}"
        )

    def test_custom_project_name(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Replying with a name instead of confirming uses the custom name."""
        self._pick_task(bot, todoist, "Plan the project")
        bot.send_message("Research first, then write a draft")
        bot.wait_responses(2, timeout=15)  # "Breaking down task…" + project confirm msg
        bot.send_message("my-custom-plan")
        bot.wait_responses(2, timeout=15)  # project created + first proposal

        proj_op = todoist.wait_for_op("create_project", timeout=5)
        assert proj_op is not None, "Expected create_project op"
        assert "#proj-my-custom-plan" in proj_op.get("name", ""), (
            f"Expected custom name in project, got: {proj_op.get('name')!r}"
        )

    def test_tasks_assigned_to_project(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Accepted subtasks are created inside the new project."""
        self._pick_task(bot, todoist, "Plan the project")
        bot.send_message("Research first, then write a draft")
        bot.wait_responses(2, timeout=15)  # "Breaking down task…" + project confirm
        bot.press_button_labeled_any("✅ Confirm", timeout=5)
        bot.wait_responses(2, timeout=15)  # project created + first proposal

        bot.press_button_labeled_any("✅ Accept", timeout=5)

        # Drain remaining proposals
        for _ in range(5):
            resp = bot.wait_responses(1, timeout=8)
            if not resp:
                break
            if any(r.get("reply_markup") for r in resp):
                bot.press_button_labeled_any("❌ Reject", timeout=5)
            else:
                break
        bot.wait_responses(1, timeout=10)  # finish message

        proj_op = todoist.wait_for_op("create_project", timeout=5)
        project_id = proj_op["project_id"]

        create_op = todoist.wait_for_op("create", timeout=5)
        assert create_op is not None, "Expected task create op"
        assert create_op.get("project_id") == project_id, (
            f"Task should be in project {project_id}, got: {create_op.get('project_id')!r}"
        )

    def test_tasks_are_numbered(self, bot: BotClient, todoist: TodoistInspector) -> None:
        """Accepted subtask titles include a sequential number after the emoji."""
        self._pick_task(bot, todoist, "Plan the project")
        bot.send_message("Research first, then write a draft")
        bot.wait_responses(2, timeout=15)  # "Breaking down task…" + project confirm
        bot.press_button_labeled_any("✅ Confirm", timeout=5)
        bot.wait_responses(2, timeout=15)  # project created + first proposal

        bot.press_button_labeled_any("✅ Accept", timeout=5)

        # Drain remaining proposals
        for _ in range(5):
            resp = bot.wait_responses(1, timeout=8)
            if not resp:
                break
            if any(r.get("reply_markup") for r in resp):
                bot.press_button_labeled_any("❌ Reject", timeout=5)
            else:
                break
        bot.wait_responses(1, timeout=10)

        created_tasks = [t for t in todoist.tasks() if t.get("content", "").strip()]
        titles = [t["content"] for t in created_tasks if t["id"] != _QUARANTINE_ID]
        assert titles, "No created task titles found"
        # First accepted task should be numbered "1"
        first = titles[0]
        assert " 1 " in first or first.startswith("1 "), (
            f"Expected task title to contain ' 1 ', got: {first!r}"
        )
