from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from telegram.ext import Application

import lib.llm as llm
import lib.obsidian as obsidian
import lib.todoist as todoist
from lib.config import OBSIDIAN_POLL_SECONDS, TELEGRAM_USER_ID, UserSettings
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
        morning_digest_job,
        time=time(8, 0, tzinfo=tz),
        chat_id=TELEGRAM_USER_ID,
        name="morning_digest",
    )
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
        first=10,
        name="obsidian_sync",
    )

    logger.info("Scheduler attached (tz=%s)", _settings.timezone)


async def morning_digest_job(context) -> None:
    try:
        tasks = await asyncio.to_thread(todoist.get_today_tasks)
        digest = await llm.generate_digest(tasks)
        await context.bot.send_message(chat_id=TELEGRAM_USER_ID, text=digest, parse_mode="Markdown")
        await asyncio.to_thread(obsidian.append_digest, digest)
        if not await asyncio.to_thread(obsidian.is_day_planned):
            await asyncio.to_thread(obsidian.mark_day_unplanned)
    except Exception as exc:
        logger.error("Morning digest failed: %s", exc)


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
