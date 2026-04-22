from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from telegram.ext import Application

import lib.llm as llm
import lib.obsidian as obsidian
import lib.todoist as todoist
from lib.config import (
    OBSIDIAN_POLL_SECONDS,
    OBSIDIAN_SYNC_FIRST_SECONDS,
    TELEGRAM_USER_ID,
    TODOIST_POLL_FIRST_SECONDS,
    TODOIST_POLL_SECONDS,
    UserSettings,
)
from lib.models import store
from lib.sync import run_sync

logger = logging.getLogger(__name__)

_settings: UserSettings = UserSettings()


def configure(settings: UserSettings) -> None:
    """Set user settings so scheduler jobs use the correct timezone."""
    global _settings
    _settings = settings


def get_settings() -> UserSettings:
    return _settings


def attach_scheduler(app: Application) -> None:
    jq = app.job_queue
    tz = ZoneInfo(_settings.timezone)

    jq.run_daily(
        weekly_review_job,
        time=time(20, 0, tzinfo=tz),
        days=(4,),  # Friday (PTB: Mon=0 … Sun=6)
        chat_id=TELEGRAM_USER_ID,
        name="weekly_review",
    )
    jq.run_daily(
        plan_reminder_job,
        time=time(9, 0, tzinfo=tz),
        chat_id=TELEGRAM_USER_ID,
        name="plan_reminder",
    )
    jq.run_repeating(
        plan_nag_job,
        interval=3600,
        first=time(10, 0, tzinfo=tz),
        chat_id=TELEGRAM_USER_ID,
        name="plan_nag",
    )
    jq.run_repeating(
        obsidian_sync_job,
        interval=OBSIDIAN_POLL_SECONDS,
        first=OBSIDIAN_SYNC_FIRST_SECONDS,
        name="obsidian_sync",
    )
    jq.run_repeating(
        todoist_sync_job,
        interval=TODOIST_POLL_SECONDS,
        first=TODOIST_POLL_FIRST_SECONDS,
        name="todoist_sync",
    )

    logger.info("Scheduler attached (tz=%s)", _settings.timezone)


async def stale_nudge_job(context) -> None:
    try:
        overdue = await asyncio.to_thread(todoist.get_overdue_tasks)
        if not overdue:
            return
        nudge = await llm.generate_nudge(overdue)
        await context.bot.send_message(chat_id=TELEGRAM_USER_ID, text=nudge, parse_mode="Markdown")
    except Exception as exc:
        logger.error("Stale nudge failed: %s", exc)


async def weekly_review_job(context) -> None:
    try:
        completed, overdue = await asyncio.gather(
            asyncio.to_thread(todoist.get_completed_tasks, 7),
            asyncio.to_thread(todoist.get_overdue_tasks),
        )
        review = await llm.generate_weekly_review(completed, overdue)

        filename = f"weekly-{date.today().isoformat()}.md"
        await asyncio.to_thread(obsidian.write_insight, filename, review)
        await context.bot.send_message(chat_id=TELEGRAM_USER_ID, text=review, parse_mode="Markdown")
    except Exception as exc:
        logger.error("Weekly review failed: %s", exc)


async def plan_reminder_job(context) -> None:
    try:
        if not await asyncio.to_thread(obsidian.is_day_planned):
            await context.bot.send_message(
                chat_id=TELEGRAM_USER_ID,
                text="Time to plan your day! Use /plan",
            )
    except Exception as exc:
        logger.error("Plan reminder failed: %s", exc)


async def plan_nag_job(context) -> None:
    try:
        if await asyncio.to_thread(obsidian.is_day_planned):
            return

        # Suppress nag when a planning session is active or recently active.
        # Send a single resume nudge after the 15-min grace period, then fall
        # through to the normal nag on subsequent intervals.
        persisted = store.load_plan_session()
        if persisted:
            phase, session_data = persisted
            last_ts = session_data.get("last_user_action_ts", 0)
            if date.fromtimestamp(last_ts) < date.today():
                persisted = None  # stale session from a previous day — don't suppress nags
        if persisted:
            phase, session_data = persisted
            elapsed = _time.time() - session_data.get("last_user_action_ts", 0)
            if elapsed < 15 * 60:
                return  # recently active — don't interrupt
            if not session_data.get("nudge_sent", False):
                await context.bot.send_message(
                    chat_id=TELEGRAM_USER_ID,
                    text="You have a planning session in progress — tap /plan to continue.",
                )
                session_data["nudge_sent"] = True
                store.save_plan_session(phase, session_data)
                return
            # Nudge already sent; fall through to normal nag below.

        tz = ZoneInfo(_settings.timezone)
        hour = datetime.now(tz).hour
        if 9 <= hour < 21:
            await context.bot.send_message(
                chat_id=TELEGRAM_USER_ID,
                text="Hey — you still haven't planned your day. Use /plan when you're ready.",
            )
    except Exception as exc:
        logger.error("Plan nag failed: %s", exc)


async def obsidian_sync_job(context) -> None:
    await run_sync()


def _check_todoist_changes() -> tuple[list[str], list[str], list[str]]:
    """Diff current active tasks against the stored snapshot.

    Returns (completed_titles, deleted_titles, new_titles).
    All side effects: updates store.known_active_ids and saves.
    """
    active = todoist.get_all_tasks()
    active_map = {t["id"]: t["title"] for t in active}
    known = store.known_active_ids  # None = first run, {} = initialized but empty

    if known is None:
        store.known_active_ids = active_map
        store.save()
        return [], [], []

    disappeared = set(known) - set(active_map)
    appeared = set(active_map) - set(known)

    completed_ids: set[str] = set()
    if disappeared:
        completed_ids = {t["id"] for t in todoist.get_completed_tasks(since_days=1)}

    completed = [known[tid] for tid in disappeared if tid in completed_ids]
    deleted = [known[tid] for tid in disappeared if tid not in completed_ids]

    completed_titles = set(completed)
    new_tasks = [active_map[tid] for tid in appeared if active_map[tid] not in completed_titles]

    store.known_active_ids = active_map
    store.save()
    return completed, deleted, new_tasks


async def todoist_sync_job(context) -> None:
    try:
        completed, deleted, new_tasks = await asyncio.to_thread(_check_todoist_changes)
        lines: list[str] = []
        for t in completed:
            lines.append(f"Completed in Todoist: {t}")
        for t in deleted:
            lines.append(f"Deleted in Todoist: {t}")
        for t in new_tasks:
            lines.append(f"New in Todoist: {t}")
        if lines:
            await context.bot.send_message(chat_id=TELEGRAM_USER_ID, text="\n".join(lines))
    except Exception as exc:
        logger.error("Todoist sync failed: %s", exc)
