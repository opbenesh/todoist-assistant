"""Fake Todoist REST + Sync API server.

Implements the exact subset of Todoist API v1 that the assistant app calls.
All state is in-memory; a /test/* control plane lets tests seed data, reset
state, and inspect what the app did.

Endpoints (SDK-driven via todoist_api_python):
  GET  /api/v1/tasks                  — list tasks (paginated, results key)
  GET  /api/v1/tasks/filter           — filter tasks (paginated, results key)
  GET  /api/v1/tasks/{id}             — get single task
  POST /api/v1/tasks                  — create task
  POST /api/v1/tasks/{id}             — update task (SDK uses POST not PATCH)
  POST /api/v1/tasks/{id}/close       — complete task
  POST /api/v1/tasks/{id}/reopen      — uncomplete task
  DELETE /api/v1/tasks/{id}          — delete task
  GET  /api/v1/projects              — list projects (paginated)
  GET  /api/v1/user                  — user settings (timezone)
  POST /api/v1/sync                  — Sync API (item_update commands)

Control plane:
  POST /test/reset                   — clear all state
  POST /test/seed                    — seed tasks/projects
  GET  /test/tasks                   — dump current task state
  GET  /test/history                 — list of all write operations
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI()

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

_tasks: dict[str, dict] = {}
_projects: dict[str, dict] = {}
_history: list[dict] = []

_DEFAULT_PROJECT_ID = "inbox_project"
_NOW_STR = "2024-01-01T00:00:00.000000Z"


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _make_task(
    content: str,
    *,
    task_id: str | None = None,
    description: str = "",
    project_id: str = _DEFAULT_PROJECT_ID,
    section_id: str | None = None,
    parent_id: str | None = None,
    labels: list[str] | None = None,
    priority: int = 1,
    due: dict | None = None,
    deadline: dict | None = None,
    duration: dict | None = None,
    completed_at: str | None = None,
    child_order: int = 0,
    **_extra: Any,
) -> dict:
    now = _now_str()
    return {
        "id": task_id or str(uuid.uuid4()),
        "content": content,
        "description": description,
        "project_id": project_id,
        "section_id": section_id,
        "parent_id": parent_id,
        "labels": labels or [],
        "priority": priority,
        "due": due,
        "deadline": deadline,
        "duration": duration,
        "collapsed": False,
        "child_order": child_order,
        "responsible_uid": None,
        "assigned_by_uid": None,
        "completed_at": completed_at,
        "added_by_uid": "test_user",
        "added_at": now,
        "updated_at": now,
    }


def _make_project(
    name: str,
    *,
    project_id: str | None = None,
    is_inbox: bool = False,
) -> dict:
    now = _now_str()
    return {
        "id": project_id or str(uuid.uuid4()),
        "name": name,
        "description": "",
        "child_order": 0,
        "color": "charcoal",
        "collapsed": False,
        "shared": False,
        "is_favorite": False,
        "is_archived": False,
        "can_assign_tasks": True,
        "view_style": "list",
        "created_at": now,
        "updated_at": now,
        "inbox_project": is_inbox,
        "workspace_id": None,
        "folder_id": None,
        "parent_id": None,
    }


def _reset_state() -> None:
    _tasks.clear()
    _projects.clear()
    _history.clear()
    # Seed a default Inbox project
    inbox = _make_project("Inbox", project_id=_DEFAULT_PROJECT_ID, is_inbox=True)
    _projects[_DEFAULT_PROJECT_ID] = inbox


_reset_state()


# ---------------------------------------------------------------------------
# Filter engine
# ---------------------------------------------------------------------------


def _parse_due_date(due: dict | None) -> date | None:
    if not due:
        return None
    date_str = due.get("date", "")
    if not date_str:
        return None
    try:
        if "T" in date_str:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
        return date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return None


def _task_is_completed(task: dict) -> bool:
    return task.get("completed_at") is not None


def _matches_filter(task: dict, query: str) -> bool:
    """Evaluate a simplified Todoist filter query against a task.

    Supports the filter strings actually used by the app:
      today, overdue, no date, completed, label:<name>
    Combined with | (OR logic).
    """
    today = date.today()
    due_date = _parse_due_date(task.get("due"))
    is_completed = _task_is_completed(task)

    def _matches_part(part: str) -> bool:
        part = part.strip().lower()
        if part == "today":
            return not is_completed and due_date is not None and due_date == today
        if part == "overdue":
            return not is_completed and due_date is not None and due_date < today
        if part == "no date":
            return not is_completed and due_date is None
        if part == "completed":
            return is_completed
        if part.startswith("label:"):
            label = part[6:]
            return label in (task.get("labels") or [])
        return False

    parts = [p.strip() for p in query.split("|")]
    return any(_matches_part(p) for p in parts)


def _paginate(items: list[dict]) -> dict:
    return {"results": items, "next_cursor": None, "has_more": False}


# ---------------------------------------------------------------------------
# Task endpoints (SDK-driven)
# ---------------------------------------------------------------------------


@app.get("/api/v1/tasks")
async def list_tasks(request: Request) -> JSONResponse:
    params = dict(request.query_params)
    project_id = params.get("project_id")
    all_tasks = list(_tasks.values())

    # Direct httpx call from get_completed_tasks uses filter= param
    filter_q = params.get("filter", "")
    if filter_q == "completed":
        results = [t for t in all_tasks if _task_is_completed(t)]
        return JSONResponse({"results": results})

    if project_id:
        results = [
            t for t in all_tasks if t["project_id"] == project_id and not _task_is_completed(t)
        ]
    else:
        results = [t for t in all_tasks if not _task_is_completed(t)]

    return JSONResponse(_paginate(results))


@app.get("/api/v1/tasks/filter")
async def filter_tasks(request: Request) -> JSONResponse:
    query = request.query_params.get("query", "")
    results = [t for t in _tasks.values() if _matches_filter(t, query)]
    return JSONResponse(_paginate(results))


@app.get("/api/v1/tasks/{task_id}")
async def get_task(task_id: str) -> JSONResponse:
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(task)


@app.post("/api/v1/tasks")
async def create_task(request: Request) -> JSONResponse:
    body = await request.json()
    task = _make_task(
        content=body.get("content", ""),
        description=body.get("description", ""),
        project_id=body.get("project_id", _DEFAULT_PROJECT_ID),
        section_id=body.get("section_id"),
        parent_id=body.get("parent_id"),
        labels=body.get("labels"),
        priority=body.get("priority", 1),
        due=_build_due(body),
        deadline=_build_deadline(body),
        duration=_build_duration(body),
    )
    _tasks[task["id"]] = task
    _history.append(
        {
            "op": "create",
            "task_id": task["id"],
            "content": task["content"],
            "project_id": task.get("project_id"),
            "labels": task.get("labels", []),
        }
    )
    return JSONResponse(task, status_code=200)


@app.post("/api/v1/tasks/{task_id}")
async def update_task(task_id: str, request: Request) -> JSONResponse:
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    body = await request.json()
    changes: dict[str, Any] = {}

    if "content" in body:
        changes["content"] = body["content"]
    if "description" in body:
        changes["description"] = body["description"]
    if "labels" in body:
        changes["labels"] = body["labels"]
    if "priority" in body:
        changes["priority"] = body["priority"]
    if "is_collapsed" in body:
        changes["collapsed"] = body["is_collapsed"]
    if "child_order" in body:
        changes["child_order"] = body["child_order"]

    # Due date updates
    new_due = _build_due(body)
    if new_due is not None or "due_date" in body or "due_string" in body or "due_datetime" in body:
        changes["due"] = new_due

    # Duration
    new_dur = _build_duration(body)
    if new_dur is not None or "duration" in body:
        changes["duration"] = new_dur

    task.update(changes)
    task["updated_at"] = _now_str()
    _history.append({"op": "update", "task_id": task_id, "changes": changes})
    return JSONResponse(task)


@app.post("/api/v1/tasks/{task_id}/close")
async def complete_task(task_id: str) -> JSONResponse:
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task["completed_at"] = _now_str()
    task["updated_at"] = _now_str()
    _history.append({"op": "complete", "task_id": task_id})
    return JSONResponse({})


@app.post("/api/v1/tasks/{task_id}/reopen")
async def uncomplete_task(task_id: str) -> JSONResponse:
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task["completed_at"] = None
    task["updated_at"] = _now_str()
    _history.append({"op": "uncomplete", "task_id": task_id})
    return JSONResponse({})


@app.delete("/api/v1/tasks/{task_id}")
async def delete_task(task_id: str) -> JSONResponse:
    task = _tasks.pop(task_id, None)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _history.append({"op": "delete", "task_id": task_id, "content": task.get("content", "")})
    return JSONResponse({})


# ---------------------------------------------------------------------------
# Project endpoints
# ---------------------------------------------------------------------------


@app.get("/api/v1/projects")
async def list_projects() -> JSONResponse:
    return JSONResponse(_paginate(list(_projects.values())))


@app.post("/api/v1/projects")
async def create_project(request: Request) -> JSONResponse:
    body = await request.json()
    name = body.get("name", "")
    project = _make_project(name)
    _projects[project["id"]] = project
    _history.append({"op": "create_project", "project_id": project["id"], "name": name})
    return JSONResponse(project, status_code=200)


# ---------------------------------------------------------------------------
# User settings endpoint
# ---------------------------------------------------------------------------


@app.get("/api/v1/user")
async def get_user() -> JSONResponse:
    return JSONResponse({"timezone": "UTC", "id": "test_user", "email": "test@example.com"})


# ---------------------------------------------------------------------------
# Sync API (used by remove_task_due_date)
# ---------------------------------------------------------------------------


@app.post("/api/v1/sync")
async def sync_api(request: Request) -> JSONResponse:
    body = await request.json()
    commands = body.get("commands", [])
    for cmd in commands:
        if cmd.get("type") == "item_update":
            args = cmd.get("args", {})
            task_id = args.get("id")
            if task_id and task_id in _tasks:
                if "due" in args and args["due"] is None:
                    _tasks[task_id]["due"] = None
                    _tasks[task_id]["updated_at"] = _now_str()
                    _history.append({"op": "remove_due_date", "task_id": task_id})
        elif cmd.get("type") == "item_close":
            args = cmd.get("args", {})
            task_id = args.get("id")
            if task_id and task_id in _tasks:
                _tasks[task_id]["completed_at"] = _now_str()
                _tasks[task_id]["updated_at"] = _now_str()
                _history.append({"op": "complete", "task_id": task_id})
        elif cmd.get("type") == "item_uncomplete":
            args = cmd.get("args", {})
            task_id = args.get("id")
            if task_id and task_id in _tasks:
                _tasks[task_id]["completed_at"] = None
                _tasks[task_id]["updated_at"] = _now_str()
                _history.append({"op": "uncomplete", "task_id": task_id})
    return JSONResponse({"sync_status": {}})


# ---------------------------------------------------------------------------
# Due / deadline / duration helpers
# ---------------------------------------------------------------------------


def _build_due(body: dict) -> dict | None:
    if "due_date" in body and body["due_date"]:
        d = body["due_date"]
        return {
            "date": d if isinstance(d, str) else d.isoformat(),
            "string": str(d),
            "lang": "en",
            "is_recurring": False,
            "timezone": None,
        }
    if "due_datetime" in body and body["due_datetime"]:
        dt = body["due_datetime"]
        return {
            "date": dt if isinstance(dt, str) else dt.isoformat(),
            "string": str(dt),
            "lang": "en",
            "is_recurring": False,
            "timezone": None,
        }
    if "due_string" in body and body["due_string"]:
        return {
            "date": date.today().isoformat(),
            "string": body["due_string"],
            "lang": "en",
            "is_recurring": False,
            "timezone": None,
        }
    return None


def _build_deadline(body: dict) -> dict | None:
    if "deadline_date" in body and body["deadline_date"]:
        d = body["deadline_date"]
        return {"date": d if isinstance(d, str) else d.isoformat(), "lang": "en"}
    return None


def _build_duration(body: dict) -> dict | None:
    if "duration" in body and body["duration"]:
        return {"amount": body["duration"], "unit": body.get("duration_unit", "minute")}
    return None


# ---------------------------------------------------------------------------
# Test control plane
# ---------------------------------------------------------------------------


@app.post("/test/reset")
async def test_reset() -> JSONResponse:
    _reset_state()
    return JSONResponse({"ok": True})


@app.post("/test/seed")
async def test_seed(request: Request) -> JSONResponse:
    body = await request.json()
    for task_data in body.get("tasks", []):
        task = _make_task(**task_data)
        if "id" in task_data:
            task["id"] = task_data["id"]
        _tasks[task["id"]] = task
    for proj_data in body.get("projects", []):
        proj = _make_project(**proj_data)
        _projects[proj["id"]] = proj
    return JSONResponse({"ok": True, "task_count": len(_tasks), "project_count": len(_projects)})


@app.get("/test/tasks")
async def test_get_tasks() -> JSONResponse:
    return JSONResponse({"tasks": list(_tasks.values())})


@app.get("/test/history")
async def test_get_history() -> JSONResponse:
    return JSONResponse({"history": _history})


@app.get("/test/wait_op")
async def test_wait_op(request: Request) -> JSONResponse:
    op = request.query_params.get("op", "")
    timeout = float(request.query_params.get("timeout", 10.0))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matched = next((h for h in _history if h.get("op") == op), None)
        if matched:
            return JSONResponse({"entry": matched})
        await asyncio.sleep(0.05)
    return JSONResponse({"entry": None})


@app.get("/test/wait_task")
async def test_wait_task(request: Request) -> JSONResponse:
    fragment = request.query_params.get("fragment", "").lower()
    timeout = float(request.query_params.get("timeout", 10.0))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matched = next(
            (t for t in _tasks.values() if fragment in t.get("content", "").lower()), None
        )
        if matched:
            return JSONResponse({"task": matched})
        await asyncio.sleep(0.05)
    return JSONResponse({"task": None})


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})
