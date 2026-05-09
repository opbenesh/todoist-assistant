from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

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


@dataclass
class TodoistState:
    today_tasks: dict[str, dict]
    all_titles: set[str]
    completed_today: dict[str, dict]


def _get_obsidian_tasks(raw_lines: list[str]) -> dict[str, bool]:
    obsidian_tasks: dict[str, bool] = {}
    for line in raw_lines:
        parsed = parse_task_line(line)
        if parsed:
            title, checked = parsed
            obsidian_tasks[title] = checked
    return obsidian_tasks


async def _async_fetch_todoist_state() -> TodoistState:
    today_tasks, all_tasks, completed_today = await asyncio.gather(
        asyncio.to_thread(todoist.get_today_tasks),
        asyncio.to_thread(todoist.get_all_tasks),
        asyncio.to_thread(todoist.get_completed_tasks, since_days=1),
    )

    todoist_tasks = {t["title"]: t for t in today_tasks}
    all_todoist_titles = {t["title"] for t in all_tasks}
    completed_by_title = {t["title"]: t for t in completed_today}

    return TodoistState(todoist_tasks, all_todoist_titles, completed_by_title)


def _fetch_todoist_state() -> TodoistState:
    return asyncio.run(_async_fetch_todoist_state())


def _process_obsidian_task(
    title: str,
    obs_checked: bool,
    todoist_state: TodoistState,
    note_changed: bool,
    complete_ids: list[str],
    uncomplete_ids: list[str],
) -> tuple[str, bool]:
    """Process one Obsidian task line against Todoist state.

    Appends task IDs to complete_ids / uncomplete_ids for deferred batch execution.
    May add title to todoist_state.all_titles to prevent double-creation within a run.
    Returns (formatted_line, dirty).
    """
    dirty = False
    updated_line = ""

    if title in todoist_state.today_tasks:
        tod = todoist_state.today_tasks[title]
        tod_completed = tod["is_completed"]

        if obs_checked != tod_completed:
            dirty = True
            if note_changed:
                if obs_checked:
                    complete_ids.append(tod["id"])
                    audit.log(
                        "complete",
                        source="sync",
                        trigger="obsidian_checked",
                        task_id=tod["id"],
                        title=title,
                    )
                else:
                    uncomplete_ids.append(tod["id"])
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

        updated_line = format_task_line(
            title,
            obs_checked,
            tod.get("priority", DEFAULT_PRIORITY),
            tod.get("duration_minutes"),
        )
    elif title in todoist_state.all_titles:
        # Task exists in Todoist but not due today (rescheduled) — keep Obsidian line as-is
        logger.debug("Task '%s' exists in Todoist but not due today — skipping create", title)
        updated_line = format_task_line(title, obs_checked)
    elif obs_checked:
        # Checked in Obsidian but absent from Todoist — already completed, skip
        logger.debug("Task '%s' checked in Obsidian but absent from Todoist — skipping", title)
        updated_line = format_task_line(title, obs_checked)
    elif title in todoist_state.completed_today and not obs_checked:
        # Task was recently completed but user unchecked it — uncomplete
        if note_changed:
            task = todoist_state.completed_today[title]
            uncomplete_ids.append(task["id"])
            audit.log(
                "uncomplete",
                source="sync",
                trigger="obsidian_unchecked",
                task_id=task["id"],
                title=title,
            )
            dirty = True
            logger.debug("Obsidian unchecks completed task '%s'", title)
        updated_line = format_task_line(title, obs_checked)
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
            todoist_state.all_titles.add(title)
            dirty = True
        except Exception as exc:
            logger.error("Failed to create Todoist task '%s': %s", title, exc)
        updated_line = format_task_line(title, obs_checked)

    return updated_line, dirty


def _sync() -> None:
    if not is_day_planned():
        return  # tasks only appear in daily note after /plan is run

    now = time.time()
    last_sync_ts = store.last_sync_ts or 0.0

    raw_lines = read_tasks_section()
    obsidian_tasks = _get_obsidian_tasks(raw_lines)
    todoist_state = _fetch_todoist_state()

    note_path = daily_note_path()
    note_mtime = note_path.stat().st_mtime if note_path.exists() else 0.0
    note_changed = note_mtime > last_sync_ts

    updated_lines: list[str] = []
    processed_titles: set[str] = set()
    dirty = False

    complete_ids: list[str] = []
    uncomplete_ids: list[str] = []

    for title, obs_checked in obsidian_tasks.items():
        processed_titles.add(title)
        line, line_dirty = _process_obsidian_task(
            title, obs_checked, todoist_state, note_changed, complete_ids, uncomplete_ids
        )
        updated_lines.append(line)
        if line_dirty:
            dirty = True

    for title, tod in todoist_state.today_tasks.items():
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

    if complete_ids or uncomplete_ids:
        todoist.batch_update_task_status(complete_ids, uncomplete_ids)

    if dirty or updated_lines != raw_lines:
        write_tasks_section(updated_lines)

    store.last_sync_ts = now
    store.save()
