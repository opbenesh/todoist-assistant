"""Fake Anthropic messages API server.

Implements POST /v1/messages and returns scripted, deterministic JSON
responses based on keywords found in the system prompt. Tests can override
the next response via the /test/set_next_response control plane endpoint.

Routing logic (checked in order):
  "extraction assistant"   → brainstorm extract array
  "breakdown assistant"    → breakdown subtasks array
  "task planning assistant"→ breakdown subtasks array
  "enrichment assistant"   → enrichment JSON object
  "daily plan"             → plan JSON (plan_markdown key)
  "morning digest"         → digest markdown string
  "weekly review"          → weekly review markdown string
  "nudge"                  → nudge plain text
  "hygiene assistant"      → optimization JSON (suggestions)
  "deep-dive"              → deepdive markdown string
  "project manager"        → project plan markdown string
  "productivity analyst"   → insights markdown string
  (fallback)               → empty JSON object {}

Control plane:
  POST /test/set_next_response  — override next N responses with a fixed text
  POST /test/reset              — clear overrides and call log
  GET  /test/calls              — list of all requests received
"""

from __future__ import annotations

import json
import uuid
from collections import deque

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_call_log: list[dict] = []
_response_queue: deque[str] = deque()


def _msg_response(text: str, model: str = "claude-haiku-4-5-20251001") -> dict:
    return {
        "id": f"msg_{uuid.uuid4().hex[:20]}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 50, "output_tokens": 100},
    }


# ---------------------------------------------------------------------------
# Canned response generators
# ---------------------------------------------------------------------------


def _enrich_response(user_msg: str) -> str:
    # Return a minimal valid enrichment JSON
    return json.dumps(
        {
            "title": user_msg.strip()[:80],
            "notes": "",
            "due_date": None,
            "priority": "p3",
            "labels": [],
            "duration_minutes": 30,
        }
    )


def _brainstorm_response(_user_msg: str) -> str:
    return json.dumps(["Review emails", "Follow up on pending items"])


def _breakdown_optimize_response(_user_msg: str) -> str:
    return json.dumps(
        {
            "project_slug": "task-breakdown",
            "tasks": ["🔧 Research options", "🔧 Draft plan", "🔧 Execute first step"],
        }
    )


def _breakdown_response(_user_msg: str) -> str:
    return json.dumps(["Research options", "Draft plan", "Execute first step"])


def _plan_response(_user_msg: str) -> str:
    return json.dumps(
        {
            "plan_markdown": (
                "## Morning\n- Work on priority tasks\n\n"
                "## Afternoon\n- Follow-ups and meetings\n\n"
                "## Evening\n- Review and wrap up\n\n"
                "_Focus: make progress on the most important task first._"
            )
        }
    )


def _digest_response(_user_msg: str) -> str:
    return "Good morning! Here's your daily digest.\n\n- **High priority**: focus tasks first.\n- Check your calendar for meetings.\n\n_Suggested focus: tackle the top task before noon._"


def _weekly_review_response(_user_msg: str) -> str:
    return "## Weekly Review\n\n**Wins:** Good progress on key tasks.\n\n**Patterns:** Some tasks deferred repeatedly — worth revisiting.\n\n**Next week:** Prioritise clearing overdue items."


def _nudge_response(_user_msg: str) -> str:
    return "You have some overdue tasks. Pick the most important one and spend 25 minutes on it right now. Small steps add up — you've got this."


def _optimization_response(_user_msg: str) -> str:
    # Return empty object = task looks fine
    return json.dumps({})


def _deepdive_response(user_msg: str) -> str:
    return (
        "## Deep Dive\n\n"
        "**Goal:** Complete the task successfully.\n\n"
        "**Steps:**\n1. Understand requirements\n2. Plan approach\n3. Execute\n\n"
        "**Blockers:** None identified.\n\n"
        "**Next action:** Start immediately."
    )


def _project_plan_response(_user_msg: str) -> str:
    return (
        "## Project Overview\n\n"
        "**Status:** Active tasks in progress.\n\n"
        "**Next 3 actions:**\n1. Complete highest priority item\n2. Review blockers\n3. Update estimates\n\n"
        "**Effort:** Approximately 4-6 hours total."
    )


def _insights_response(_user_msg: str) -> str:
    return (
        "## Productivity Insights\n\n"
        "**Habit gaps:** Some recurring tasks were deferred.\n\n"
        "**Workload:** Completion rate is moderate.\n\n"
        "**Clusters:** Most tasks in the Inbox — consider triaging."
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _route(system: str, user_msg: str, model: str) -> str:
    s = system.lower()
    if "extraction assistant" in s:
        return _brainstorm_response(user_msg)
    if "breakdown assistant" in s:
        return _breakdown_optimize_response(user_msg)
    if "task planning assistant" in s:
        return _breakdown_response(user_msg)
    if "enrichment assistant" in s:
        return _enrich_response(user_msg)
    if "daily plan" in s or ("productivity coach" in s and "plan" in s):
        return _plan_response(user_msg)
    if "morning digest" in s:
        return _digest_response(user_msg)
    if "weekly review" in s:
        return _weekly_review_response(user_msg)
    if "motivating nudge" in s or "nudge" in s:
        return _nudge_response(user_msg)
    if "hygiene assistant" in s:
        return _optimization_response(user_msg)
    if "deep-dive" in s:
        return _deepdive_response(user_msg)
    if "project manager" in s:
        return _project_plan_response(user_msg)
    if "productivity analyst" in s or "insights report" in s:
        return _insights_response(user_msg)
    return "{}"


# ---------------------------------------------------------------------------
# Anthropic messages endpoint
# ---------------------------------------------------------------------------


@app.post("/v1/messages")
async def create_message(request: Request) -> JSONResponse:
    body = await request.json()
    system = body.get("system", "")
    if isinstance(system, list):
        system = " ".join(block.get("text", "") for block in system if block.get("type") == "text")
    messages = body.get("messages", [])
    model = body.get("model", "claude-haiku-4-5-20251001")

    # Extract user message text
    user_msg = ""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                user_msg = content
            elif isinstance(content, list):
                user_msg = " ".join(
                    block.get("text", "") for block in content if block.get("type") == "text"
                )

    _call_log.append(
        {"system_snippet": system[:100], "user_snippet": user_msg[:100], "model": model}
    )

    # Use override queue if populated
    if _response_queue:
        text = _response_queue.popleft()
    else:
        text = _route(system, user_msg, model)

    return JSONResponse(_msg_response(text, model))


# ---------------------------------------------------------------------------
# Control plane
# ---------------------------------------------------------------------------


@app.post("/test/set_next_response")
async def set_next_response(request: Request) -> JSONResponse:
    body = await request.json()
    text = body.get("text", "{}")
    count = int(body.get("count", 1))
    for _ in range(count):
        _response_queue.append(text)
    return JSONResponse({"ok": True, "queued": count})


@app.post("/test/reset")
async def test_reset() -> JSONResponse:
    _call_log.clear()
    _response_queue.clear()
    return JSONResponse({"ok": True})


@app.get("/test/calls")
async def test_get_calls() -> JSONResponse:
    return JSONResponse({"calls": _call_log})


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})
