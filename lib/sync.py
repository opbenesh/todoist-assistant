from __future__ import annotations

import asyncio
import logging
import time

import lib.audit as audit
import lib.todoist as todoist
from lib.models import DEFAULT_PRIORITY, Task, store
from lib.obsidian import (
    daily_note_path,
    format_task_line,
    is_day_planned,
    parse_task_line,
    read_tasks_section,
    write_tasks_section,
)

logger = logging.getLogger(__name__)


async def run_sync() -> None:
    """Sync today's Obsidian ## Tasks section with Todoist. Last-write wins."""
    try:
        await asyncio.to_thread(_sync)
    except Exception as exc:
        logger.error("Sync failed: %s", exc)


def _sync() -> None:
    if not is_day_planned():
        return  # tasks only appear in daily note after /plan is run

    now = time.time()
    last_sync_ts = store.last_sync_ts or 0.0

    raw_lines = read_tasks_section()
    obsidian_tasks: dict[str, bool] = {}
    for line in raw_lines:
        parsed = parse_task_line(line)
        if parsed:
            title, checked = parsed
            obsidian_tasks[title] = checked

    today_tasks = todoist.get_today_tasks()
    todoist_tasks: dict[str, dict] = {t["title"]: t for t in today_tasks}

    note_path = daily_note_path()
    note_mtime = note_path.stat().st_mtime if note_path.exists() else 0.0
    note_changed = note_mtime > last_sync_ts

    # All-task titles used to detect rescheduled tasks (avoid creating inbox duplicates)
    all_tasks = todoist.get_all_tasks()
    all_todoist_titles: set[str] = {t["title"] for t in all_tasks}

    # Recently-completed tasks (today) — needed so unchecking a just-completed task
    # calls uncomplete instead of creating a duplicate
    completed_today = todoist.get_completed_tasks(since_days=1)
    completed_by_title: dict[str, dict] = {t["title"]: t for t in completed_today}

    updated_lines: list[str] = []
    processed_titles: set[str] = set()
    dirty = False

    for title, obs_checked in obsidian_tasks.items():
        processed_titles.add(title)

        if title in todoist_tasks:
            tod = todoist_tasks[title]
            tod_completed = tod["is_completed"]

            if obs_checked != tod_completed:
                dirty = True
                if note_changed:
                    if obs_checked:
                        todoist.complete_todoist_task(tod["id"])
                        audit.log(
                            "complete",
                            source="sync",
                            trigger="obsidian_checked",
                            task_id=tod["id"],
                            title=title,
                        )
                    else:
                        todoist.uncomplete_todoist_task(tod["id"])
                        audit.log(
                            "uncomplete",
                            source="sync",
                            trigger="obsidian_unchecked",
                            task_id=tod["id"],
                            title=title,
                        )
                    logger.debug("Obsidian wins for '%s': checked=%s", title, obs_checked)
                else:
                    obs_checked = tod_completed
                    logger.debug("Todoist wins for '%s': completed=%s", title, tod_completed)

            updated_lines.append(
                format_task_line(
                    title,
                    obs_checked,
                    tod.get("priority", DEFAULT_PRIORITY),
                    tod.get("duration_minutes"),
                )
            )
        elif title in all_todoist_titles:
            # Task exists in Todoist but not due today (rescheduled) — keep Obsidian line as-is
            logger.debug("Task '%s' exists in Todoist but not due today — skipping create", title)
            updated_lines.append(format_task_line(title, obs_checked))
            continue
        elif obs_checked:
            # Checked in Obsidian but absent from Todoist — already completed.
            # We explicitly skip creation to avoid incorrectly recreating a checked, absent task.
            logger.debug("Task '%s' checked in Obsidian but absent from Todoist — skipping creation", title)
            updated_lines.append(format_task_line(title, obs_checked))
            continue
        elif title in completed_by_title and not obs_checked:
            # Task was recently completed but user unchecked it — uncomplete
            if note_changed:
                task = completed_by_title[title]
                todoist.uncomplete_todoist_task(task["id"])
                audit.log(
                    "uncomplete",
                    source="sync",
                    trigger="obsidian_unchecked",
                    task_id=task["id"],
                    title=title,
                )
                dirty = True
                logger.debug("Obsidian unchecks completed task '%s'", title)
            updated_lines.append(format_task_line(title, obs_checked))
        else:
            # Unchecked task not in Todoist at all — create it
            task = Task(title=title)
            try:
                task_id = todoist.create_todoist_task(task)
                audit.log(
                    "create",
                    source="sync",
                    trigger="obsidian_new_task",
                    task_id=task_id,
                    title=title,
                )
                logger.info("Created Todoist task from Obsidian: '%s' (%s)", title, task_id)
                # prevent double-create if title appears again this run
                all_todoist_titles.add(title)
                dirty = True
            except Exception as exc:
                logger.error("Failed to create Todoist task '%s': %s", title, exc)
            updated_lines.append(format_task_line(title, obs_checked))

    for title, tod in todoist_tasks.items():
        if title not in processed_titles:
            updated_lines.append(
                format_task_line(
                    title,
                    tod["is_completed"],
                    tod.get("priority", DEFAULT_PRIORITY),
                    tod.get("duration_minutes"),
                )
            )
            logger.info("Appended Todoist task to daily note: '%s'", title)
            dirty = True

    if dirty or updated_lines != raw_lines:
        write_tasks_section(updated_lines)

    store.last_sync_ts = now
    store.save()
