from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from lib.llm import brainstorm_extract_tasks, propose_enrichment, restore_links


def _mock_response(text: str):
    content_block = MagicMock()
    content_block.text = text
    response = MagicMock()
    response.content = [content_block]
    return response


def _patch_call(text: str):
    return patch("lib.llm._call", return_value=text)


# ---------------------------------------------------------------------------
# propose_enrichment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_enrichment_returns_task():
    payload = json.dumps(
        {
            "title": "Renew car insurance",
            "notes": "Check with current provider first",
            "due_date": "2026-04-10",
            "priority": "p2",
            "labels": ["finance"],
            "duration_minutes": 30,
        }
    )
    with _patch_call(payload):
        task = await propose_enrichment("renew car insurance")

    assert task.title == "Renew car insurance"
    assert task.due_date == date(2026, 4, 10)
    assert task.priority == "p2"
    assert task.labels == ["finance"]
    assert task.duration_minutes == 30
    assert task.notes == "Check with current provider first"


@pytest.mark.asyncio
async def test_propose_enrichment_handles_null_fields():
    payload = json.dumps(
        {
            "title": "Buy milk",
            "notes": "",
            "due_date": None,
            "priority": "p4",
            "labels": [],
            "duration_minutes": None,
        }
    )
    with _patch_call(payload):
        task = await propose_enrichment("buy milk")

    assert task.due_date is None
    assert task.duration_minutes is None
    assert task.labels == []


@pytest.mark.asyncio
async def test_propose_enrichment_drops_unknown_labels():
    payload = json.dumps(
        {
            "title": "Task",
            "notes": "",
            "due_date": None,
            "priority": "p3",
            "labels": ["work", "invalid_label", "health"],
            "duration_minutes": None,
        }
    )
    with _patch_call(payload):
        task = await propose_enrichment("task")

    assert "invalid_label" not in task.labels
    assert "work" in task.labels
    assert "health" in task.labels


@pytest.mark.asyncio
async def test_propose_enrichment_raises_on_invalid_json():
    with _patch_call("this is not json"):
        with pytest.raises(ValueError, match="invalid JSON"):
            await propose_enrichment("something")


@pytest.mark.asyncio
async def test_propose_enrichment_invalid_priority_defaults_to_p4():
    payload = json.dumps(
        {
            "title": "Task",
            "notes": "",
            "due_date": None,
            "priority": "urgent",  # invalid
            "labels": [],
            "duration_minutes": None,
        }
    )
    with _patch_call(payload):
        task = await propose_enrichment("task")

    assert task.priority == "p4"


@pytest.mark.asyncio
async def test_propose_enrichment_invalid_due_date_ignored():
    payload = json.dumps(
        {
            "title": "Task",
            "notes": "",
            "due_date": "not-a-date",
            "priority": "p3",
            "labels": [],
            "duration_minutes": None,
        }
    )
    with _patch_call(payload):
        task = await propose_enrichment("task")

    assert task.due_date is None


@pytest.mark.asyncio
async def test_propose_enrichment_falls_back_to_raw_title_on_missing_title():
    payload = json.dumps(
        {
            "title": "",
            "notes": "",
            "due_date": None,
            "priority": "p4",
            "labels": [],
            "duration_minutes": None,
        }
    )
    with _patch_call(payload):
        task = await propose_enrichment("original title")

    assert task.title == "original title"


# ---------------------------------------------------------------------------
# restore_links
# ---------------------------------------------------------------------------


def test_restore_links_no_links_in_original():
    original = "Just a normal task"
    proposed = "A very normal task"
    assert restore_links(original, proposed) == "A very normal task"


def test_restore_links_all_links_preserved():
    original = "Review [PR 123](https://github.com/org/repo/pull/123)"
    proposed = "Review [PR 123](https://github.com/org/repo/pull/123) and merge"
    assert restore_links(original, proposed) == proposed


def test_restore_links_all_links_missing():
    original = "Review [PR 123](https://github.com/org/repo/pull/123)"
    proposed = "Review PR 123"
    expected = "Review PR 123 [PR 123](https://github.com/org/repo/pull/123)"
    assert restore_links(original, proposed) == expected


def test_restore_links_some_links_missing():
    original = "Review [PR 123](https://example.com/1) and [Issue 456](https://example.com/2)"
    proposed = "Review [PR 123](https://example.com/1) and Issue 456"
    expected = "Review [PR 123](https://example.com/1) and Issue 456 [Issue 456](https://example.com/2)"
    assert restore_links(original, proposed) == expected


def test_restore_links_with_trailing_whitespace():
    original = "Review [PR 123](https://example.com/1)"
    proposed = "Review PR 123    "
    expected = "Review PR 123 [PR 123](https://example.com/1)"
    assert restore_links(original, proposed) == expected


def test_restore_links_preserves_new_links_in_proposed():
    original = "Task with [old](https://old.com)"
    proposed = "Task with [new](https://new.com)"
    expected = "Task with [new](https://new.com) [old](https://old.com)"
    assert restore_links(original, proposed) == expected


# ---------------------------------------------------------------------------
# brainstorm_extract_tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brainstorm_extract_tasks_returns_list():
    payload = json.dumps(["Buy groceries", "Call the dentist"])
    with _patch_call(payload):
        result = await brainstorm_extract_tasks("buy groceries, call the dentist")
    assert result == ["Buy groceries", "Call the dentist"]


@pytest.mark.asyncio
async def test_brainstorm_extract_tasks_returns_empty_on_invalid_json():
    with _patch_call("not json"):
        result = await brainstorm_extract_tasks("something")
    assert result == []


@pytest.mark.asyncio
async def test_brainstorm_extract_tasks_preserves_hebrew():
    """LLM must return Hebrew titles unchanged — no translation."""
    hebrew_tasks = ["לקנות חלב", "לשלוח מייל לעמית"]
    payload = json.dumps(hebrew_tasks)
    with _patch_call(payload):
        result = await brainstorm_extract_tasks("לקנות חלב, לשלוח מייל לעמית")
    assert result == hebrew_tasks, f"Hebrew titles must be preserved, got: {result}"


def test_brainstorm_prompt_forbids_consolidation():
    """_BRAINSTORM_SYSTEM must explicitly forbid merging comma-separated tasks.

    Without this rule the LLM may turn "plan may, plan june, plan july" into
    a single "plan may-july" instead of three separate tasks.
    """
    from lib.llm import _BRAINSTORM_SYSTEM

    lowered = _BRAINSTORM_SYSTEM.lower()
    assert any(
        phrase in lowered
        for phrase in ["do not merge", "never merge", "do not consolidate", "never consolidate"]
    ), (
        "_BRAINSTORM_SYSTEM must contain an explicit rule forbidding the LLM from merging "
        "or consolidating comma-separated tasks (e.g. 'never merge', 'do not consolidate')"
    )
