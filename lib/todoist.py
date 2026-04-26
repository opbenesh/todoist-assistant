from __future__ import annotations

import logging
import time
import uuid

import httpx
from todoist_api_python.api import TodoistAPI

from lib import config as _config
from lib.config import TODOIST_KEY, UserSettings
from lib.models import PRIORITY_TO_TODOIST, TODOIST_TO_PRIORITY, Task, store

logger = logging.getLogger(__name__)

_TODOIST_BASE = _config.TODOIST_BASE_URL.rstrip("/")
_http = httpx.Client(timeout=15)

_PROJECTS_CACHE_TTL = 300
_projects_cache: tuple[dict[str, str], str | None] | None = None
_projects_cache_time: float = 0.0


class _BaseUrlTransport(httpx.BaseTransport):
    """Rewrites every request's host to point at a different base URL.

    Used in staging/testing to redirect all Todoist SDK HTTP calls to a
    local fake server without modifying any of the SDK internals.
    """

    def __init__(self, fake_base: str) -> None:
        self._fake = httpx.URL(fake_base.rstrip("/"))
        self._inner = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        new_url = request.url.copy_with(
            scheme=self._fake.scheme,
            host=self._fake.host,
            port=self._fake.port,
        )
        new_request = httpx.Request(
            method=request.method,
            url=new_url,
            headers=request.headers,
            content=request.content,
        )
        return self._inner.handle_request(new_request)


if _TODOIST_BASE != "https://api.todoist.com":
    _api = TodoistAPI(TODOIST_KEY, client=httpx.Client(transport=_BaseUrlTransport(_TODOIST_BASE)))
else:
    _api = TodoistAPI(TODOIST_KEY)


# ---------------------------------------------------------------------------
# User settings
# ---------------------------------------------------------------------------

_USER_SETTINGS_CACHE: dict | None = None


def get_user_settings() -> dict:
    """Fetch user settings from Todoist API v1.

    Returns dict with at least: timezone.
    start_day / time_format are not available in v1 — profile.md handles those.
    Returns empty dict on failure.
    """
    global _USER_SETTINGS_CACHE
    if _USER_SETTINGS_CACHE is not None:
        return _USER_SETTINGS_CACHE

    try:
        r = httpx.get(
            f"{_TODOIST_BASE}/api/v1/user",
            headers={"Authorization": f"Bearer {TODOIST_KEY}"},
            timeout=10,
        )
        r.raise_for_status()
        _USER_SETTINGS_CACHE = r.json()
        return _USER_SETTINGS_CACHE
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


def create_todoist_task(
    task: Task,
    parent_id: str | None = None,
    project_id: str | None = None,
) -> str:
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
    if project_id:
        kwargs["project_id"] = project_id

    result = _api.add_task(task.title, **kwargs)  # content is positional in v4
    store.add_known_task(result.id, task.title)
    return result.id


def create_todoist_project(name: str) -> str:
    """Create a Todoist project with the given name. Returns the new project's id."""
    project = _api.add_project(name)
    return project.id


def get_today_tasks() -> list[dict]:
    """Return tasks due today or overdue, excluding quarantined tasks."""
    paginator = _api.filter_tasks(query="today | overdue")
    return [
        _task_to_dict(t)
        for page in paginator
        for t in page
        if "quarantined" not in (t.labels or [])
    ]


def get_triage_tasks() -> list[dict]:
    """Return tasks for triage: due today, overdue, or with no due date. Deduplicated."""
    paginator = _api.filter_tasks(query="today | overdue | no date")
    seen: set[str] = set()
    result = []
    for page in paginator:
        for t in page:
            if t.id not in seen and not t.is_completed:
                seen.add(t.id)
                d = _task_to_dict(t)
                if "quarantined" not in (d.get("labels") or []):
                    result.append(d)
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
        r = _http.get(
            f"{_TODOIST_BASE}/api/v1/tasks",
            headers={"Authorization": f"Bearer {TODOIST_KEY}"},
            params={"filter": "completed", "limit": 200},
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
    store.remove_known_task(task_id)


def uncomplete_todoist_task(task_id: str) -> None:
    _api.uncomplete_task(task_id)


def update_todoist_task(task_id: str, **kwargs) -> None:
    _api.update_task(task_id, **kwargs)


def delete_todoist_task(task_id: str) -> None:
    _api.delete_task(task_id)
    store.remove_known_task(task_id)


def batch_update_task_status(complete_ids: list[str], uncomplete_ids: list[str]) -> None:
    """Batch complete and uncomplete tasks via the Sync API (chunked to 100 commands max)."""

    if not complete_ids and not uncomplete_ids:
        return

    commands = []
    for task_id in complete_ids:
        commands.append(
            {
                "type": "item_close",
                "uuid": str(uuid.uuid4()),
                "args": {"id": task_id},
            }
        )

    for task_id in uncomplete_ids:
        commands.append(
            {
                "type": "item_uncomplete",
                "uuid": str(uuid.uuid4()),
                "args": {"id": task_id},
            }
        )

    # Todoist Sync API accepts a max of 100 commands per request
    for i in range(0, len(commands), 100):
        chunk = commands[i : i + 100]
        r = httpx.post(
            f"{_TODOIST_BASE}/api/v1/sync",
            headers={"Authorization": f"Bearer {TODOIST_KEY}"},
            json={"commands": chunk},
            timeout=10,
        )
        r.raise_for_status()

    for task_id in complete_ids:
        store.remove_known_task(task_id)


# ---------------------------------------------------------------------------
# Task age / quarantine helpers
# ---------------------------------------------------------------------------

MAX_TRIAGE_AGE = 3


def _is_age_label(lbl: str) -> bool:
    return lbl.startswith("age") and lbl[3:].isdigit()


def get_task_age(labels: list[str]) -> int:
    """Return current triage age from labels (0 if none)."""
    for lbl in labels:
        if _is_age_label(lbl):
            return int(lbl[3:])
    return 0


def bump_task_age(task_id: str, current_labels: list[str]) -> None:
    """Remove existing ageN label and add age(N+1)."""
    age = get_task_age(current_labels)
    new_labels = [lbl for lbl in current_labels if not _is_age_label(lbl)]
    new_labels.append(f"age{age + 1}")
    update_todoist_task(task_id, labels=new_labels)


def quarantine_task(task_id: str, current_labels: list[str]) -> None:
    """Add quarantined label to a task."""
    if "quarantined" not in current_labels:
        update_todoist_task(task_id, labels=current_labels + ["quarantined"])


def strip_age_labels(task_id: str, current_labels: list[str] | None = None) -> None:
    """Remove all ageN and quarantined labels (call on complete/delete).

    If current_labels is not provided, fetches the task first.
    """
    if current_labels is None:
        task = get_task_by_id(task_id)
        current_labels = task.get("labels") or []
    clean = [lbl for lbl in current_labels if not _is_age_label(lbl) and lbl != "quarantined"]
    if len(clean) != len(current_labels):
        update_todoist_task(task_id, labels=clean)


def reset_next_project_task_age(project_id: str) -> None:
    """Strip age labels from the first incomplete task in a project (Todoist native order)."""
    tasks = get_tasks_by_project(project_id)
    if tasks:
        strip_age_labels(tasks[0]["id"], tasks[0].get("labels") or [])


def get_quarantined_tasks() -> list[dict]:
    """Return all tasks with quarantined label."""
    paginator = _api.filter_tasks(query="label:quarantined")
    return [_task_to_dict(t) for page in paginator for t in page]


def get_all_projects() -> dict[str, str]:
    """Return mapping of project_id -> project_name."""
    projects, _ = get_projects_info()
    return projects


def get_projects_info() -> tuple[dict[str, str], str | None]:
    """Return (id→name mapping, inbox_project_id), caching the result."""
    global _projects_cache, _projects_cache_time
    now = time.time()

    if _projects_cache is not None and now - _projects_cache_time < _PROJECTS_CACHE_TTL:
        return _projects_cache

    projects: dict[str, str] = {}
    inbox_id: str | None = None
    for page in _api.get_projects():
        for p in page:
            projects[p.id] = p.name
            if p.is_inbox_project:
                inbox_id = p.id

    _projects_cache = (projects, inbox_id)
    _projects_cache_time = now
    return _projects_cache


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
        f"{_TODOIST_BASE}/api/v1/sync",
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
        "project_id": t.project_id,
        "due_date": due.isoformat() if due else None,
        "deadline": deadline.isoformat() if deadline else None,
        "duration_minutes": t.duration.amount if t.duration else None,
        "is_completed": t.is_completed,
        "is_recurring": t.due.is_recurring if t.due else False,
        "due_string": t.due.string if t.due else None,
    }
