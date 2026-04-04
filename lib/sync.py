from __future__ import annotations

import asyncio
import logging
import time

import lib.todoist as todoist
from lib.models import DEFAULT_PRIORITY, Task, TaskStore
from lib.obsidian import (
    daily_note_path,
    format_task_line,
    is_day_planned,
    parse_task_line,
    read_tasks_section,
    write_tasks_section,
)

logger = logging.getLogger(__name__)

store = TaskStore()


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
                    else:
                        todoist.update_todoist_task(tod["id"], is_completed=False)
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
        else:
            # Task exists in Obsidian but not Todoist — create it
            task = Task(title=title)
            try:
                task_id = todoist.create_todoist_task(task)
                logger.info("Created Todoist task from Obsidian: '%s' (%s)", title, task_id)
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
