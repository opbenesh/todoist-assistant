"""Test data factories for seeding fake services."""

from __future__ import annotations

from datetime import date, timedelta


def task(
    content: str,
    *,
    task_id: str | None = None,
    priority: int = 1,
    labels: list[str] | None = None,
    due_today: bool = False,
    overdue_days: int = 0,
    no_due_date: bool = False,
    completed: bool = False,
    recurring: bool = False,
    project_id: str = "inbox_project",
    description: str = "",
    duration_minutes: int | None = None,
) -> dict:
    """Build a task seed dict for POST /test/seed."""
    result: dict = {
        "content": content,
        "priority": priority,
        "labels": labels or [],
        "project_id": project_id,
        "description": description,
    }
    if task_id:
        result["id"] = task_id
    if duration_minutes:
        result["duration"] = {"amount": duration_minutes, "unit": "minute"}
    if completed:
        result["completed_at"] = "2024-01-01T00:00:00.000000Z"

    if not no_due_date and not completed:
        if recurring:
            d = date.today() if due_today else date.today()
            result["due"] = {
                "date": d.isoformat(),
                "string": "every day",
                "lang": "en",
                "is_recurring": True,
                "timezone": None,
            }
        elif due_today:
            result["due"] = _due_dict(date.today())
        elif overdue_days > 0:
            result["due"] = _due_dict(date.today() - timedelta(days=overdue_days))

    return result


def project(name: str, *, project_id: str | None = None) -> dict:
    """Build a project seed dict for POST /test/seed."""
    result: dict = {"name": name}
    if project_id:
        result["project_id"] = project_id
    return result


def _due_dict(d: date) -> dict:
    return {
        "date": d.isoformat(),
        "string": d.strftime("%b %-d"),
        "lang": "en",
        "is_recurring": False,
        "timezone": None,
    }
