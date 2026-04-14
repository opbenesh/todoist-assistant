from __future__ import annotations

import logging

import httpx
from todoist_api_python.api import TodoistAPI

from lib.config import TODOIST_KEY, UserSettings
from lib.models import PRIORITY_TO_TODOIST, TODOIST_TO_PRIORITY, Task

logger = logging.getLogger(__name__)

_api = TodoistAPI(TODOIST_KEY)


# ---------------------------------------------------------------------------
# User settings
# ---------------------------------------------------------------------------


def get_user_settings() -> dict:
    """Fetch user settings from Todoist API v1.

    Returns dict with at least: timezone.
    start_day / time_format are not available in v1 — profile.md handles those.
    Returns empty dict on failure.
    """
    try:
        r = httpx.get(
            "https://api.todoist.com/api/v1/user",
            headers={"Authorization": f"Bearer {TODOIST_KEY}"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.warning("Could not fetch Todoist user settings: %s", exc)
        return {}


# Todoist uses non-IANA timezone names for some regions; map them here.
_TODOIST_TZ_MAP: dict[str, str] = {
    "Jerusalem": "Asia/Jerusalem",
}


def _normalize_tz(tz: str) -> str:
    return _TODOIST_TZ_MAP.get(tz, tz)


def build_user_settings(todoist_data: dict, profile: dict) -> UserSettings:
    """Merge Todoist API data with profile.md fallback values."""
    # v1 API returns timezone directly (no tz_info wrapper)
    tz = _normalize_tz(todoist_data.get("timezone") or profile.get("timezone", "UTC"))

    # start_day / time_format not in v1 — use profile.md
    raw = profile.get("first_day_of_week", "monday").lower()
    first_day = 6 if raw == "sunday" else 0
    time_24h = profile.get("time_format", "24h") == "24h"

    return UserSettings(
        timezone=tz,
        first_day_of_week=first_day,
        time_format_24h=time_24h,
        work_start=profile.get("work_start", "09:00"),
        work_end=profile.get("work_end", "18:00"),
        morning_block=profile.get("morning_block", "09:00-12:00"),
        afternoon_block=profile.get("afternoon_block", "12:00-17:00"),
        evening_block=profile.get("evening_block", "17:00-21:00"),
        default_project=profile.get("default_project", "Inbox"),
        stale_task_days=int(profile.get("stale_task_days", 3)),
    )


# ---------------------------------------------------------------------------
# Task operations
# ---------------------------------------------------------------------------


def create_todoist_task(task: Task, parent_id: str | None = None) -> str:
    """Create a task in Todoist. Returns the new task's id."""
    kwargs: dict = {
        "priority": PRIORITY_TO_TODOIST[task.priority],
        "labels": task.labels or [],
    }
    if task.notes:
        kwargs["description"] = task.notes
    if task.due_date:
        kwargs["due_date"] = task.due_date  # v4 takes a date object directly
    if task.duration_minutes:
        kwargs["duration"] = task.duration_minutes
        kwargs["duration_unit"] = "minute"
    if parent_id:
        kwargs["parent_id"] = parent_id

    result = _api.add_task(task.title, **kwargs)  # content is positional in v4
    return result.id


def get_today_tasks() -> list[dict]:
    """Return tasks due today or overdue."""
    paginator = _api.filter_tasks(query="today | overdue")
    return [_task_to_dict(t) for page in paginator for t in page]


def get_triage_tasks() -> list[dict]:
    """Return tasks for triage: due today, overdue, or with no due date. Deduplicated."""
    paginator = _api.filter_tasks(query="today | overdue | no date")
    seen: set[str] = set()
    result = []
    for page in paginator:
        for t in page:
            if t.id not in seen and not t.is_completed:
                seen.add(t.id)
                result.append(_task_to_dict(t))
    return result


def get_overdue_tasks() -> list[dict]:
    """Return only overdue tasks (due before today)."""
    paginator = _api.filter_tasks(query="overdue")
    return [_task_to_dict(t) for page in paginator for t in page]


def get_all_tasks() -> list[dict]:
    """Return all active (non-completed) tasks across all projects. Deduplicated."""
    paginator = _api.get_tasks()
    seen: set[str] = set()
    result = []
    for page in paginator:
        for t in page:
            if t.id not in seen:
                seen.add(t.id)
                result.append(_task_to_dict(t))
    return result


def get_task_by_id(task_id: str) -> dict:
    """Fetch a single task by ID."""
    t = _api.get_task(task_id)
    return _task_to_dict(t)


def get_tasks_by_project(project_id: str) -> list[dict]:
    """Return all active tasks in a given project."""
    paginator = _api.get_tasks(project_id=project_id)
    return [_task_to_dict(t) for page in paginator for t in page]


def get_completed_tasks(since_days: int = 7) -> list[dict]:
    """Return tasks completed in the last since_days days.

    Uses the filter API (filter=completed) since the dedicated completed
    endpoint was removed. Results lack completed_at metadata.
    """
    try:
        r = httpx.get(
            "https://api.todoist.com/api/v1/tasks",
            headers={"Authorization": f"Bearer {TODOIST_KEY}"},
            params={"filter": "completed", "limit": 200},
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("results", [])
        return [
            {
                "id": item.get("id", ""),
                "title": item.get("content", ""),
                "completed_at": item.get("completed_at", ""),
                "project_id": item.get("project_id", ""),
            }
            for item in items
        ]
    except Exception as exc:
        logger.warning("Could not fetch completed tasks: %s", exc)
        return []


def complete_todoist_task(task_id: str) -> None:
    _api.complete_task(task_id)


def uncomplete_todoist_task(task_id: str) -> None:
    _api.uncomplete_task(task_id)


def update_todoist_task(task_id: str, **kwargs) -> None:
    _api.update_task(task_id, **kwargs)


def delete_todoist_task(task_id: str) -> None:
    _api.delete_task(task_id)


def get_all_projects() -> dict[str, str]:
    """Return mapping of project_id -> project_name."""
    paginator = _api.get_projects()
    return {p.id: p.name for page in paginator for p in page}


def get_tasks_to_optimize(
    projects: dict[str, str] | None = None,
    exclude_ids: frozenset[str] = frozenset(),
) -> list[dict]:
    """Return tasks needing hygiene improvements, sorted by neediness (cap 20).

    Untriaged criteria (any one triggers): p4 priority, Inbox project, no due date,
    or title longer than 60 chars.
    Tasks in exclude_ids (recently optimized) are skipped.
    """
    if projects is None:
        projects = get_all_projects()
    paginator = _api.get_tasks()
    tasks = []
    seen: set[str] = set()
    for page in paginator:
        for t in page:
            if t.id in seen or t.id in exclude_ids:
                continue
            seen.add(t.id)
            d = _task_to_dict(t)
            d["project_id"] = t.project_id
            d["project"] = projects.get(t.project_id, "Unknown")
            needs_work = (
                d["priority"] == "p4"
                or d["project"] == "Inbox"
                or d["due_date"] is None
                or len(d["title"]) > 60
            )
            if needs_work:
                tasks.append(d)

    def _score(t: dict) -> int:
        return -(
            int(t["priority"] == "p4") + int(t["project"] == "Inbox") + int(t["due_date"] is None)
        )

    tasks.sort(key=_score)
    return tasks[:20]


def remove_task_due_date(task_id: str) -> None:
    """Remove the due date from a task via Sync API (due_string='' is ignored by REST)."""
    import uuid

    r = httpx.post(
        "https://api.todoist.com/api/v1/sync",
        headers={"Authorization": f"Bearer {TODOIST_KEY}"},
        json={
            "commands": [
                {
                    "type": "item_update",
                    "uuid": str(uuid.uuid4()),
                    "args": {"id": task_id, "due": None},
                }
            ]
        },
        timeout=10,
    )
    r.raise_for_status()


def _task_to_dict(t) -> dict:
    due = t.due.date if t.due else None
    deadline = t.deadline.date if t.deadline else None
    return {
        "id": t.id,
        "title": t.content,
        "notes": t.description or "",
        "priority": TODOIST_TO_PRIORITY.get(t.priority, "p4"),
        "labels": t.labels or [],
        "due_date": due.isoformat() if due else None,
        "deadline": deadline.isoformat() if deadline else None,
        "duration_minutes": t.duration.amount if t.duration else None,
        "is_completed": t.is_completed,
        "is_recurring": t.due.is_recurring if t.due else False,
        "due_string": t.due.string if t.due else None,
    }
