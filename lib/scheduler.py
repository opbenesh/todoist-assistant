from __future__ import annotations

import asyncio
import logging
from datetime import date, time
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
        days=(6,),  # Sunday (PTB: Mon=0 … Sun=6)
        chat_id=TELEGRAM_USER_ID,
        name="weekly_review",
    )
    jq.run_daily(
        stale_nudge_job,
        time=time(15, 0, tzinfo=tz),
        chat_id=TELEGRAM_USER_ID,
        name="stale_nudge",
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
    except Exception as exc:
        logger.error("Morning digest failed: %s", exc)


async def weekly_review_job(context) -> None:
    try:
        completed, overdue = await asyncio.gather(
            asyncio.to_thread(todoist.get_today_tasks),
            asyncio.to_thread(todoist.get_overdue_tasks),
        )
        review = await llm.generate_weekly_review(completed, overdue)

        filename = f"weekly-{date.today().isoformat()}.md"
        await asyncio.to_thread(obsidian.write_insight, filename, review)
        await context.bot.send_message(chat_id=TELEGRAM_USER_ID, text=review, parse_mode="Markdown")
    except Exception as exc:
        logger.error("Weekly review failed: %s", exc)


async def stale_nudge_job(context) -> None:
    try:
        overdue = await asyncio.to_thread(todoist.get_overdue_tasks)
        if not overdue:
            return
        nudge = await llm.generate_nudge(overdue)
        await context.bot.send_message(chat_id=TELEGRAM_USER_ID, text=nudge)
    except Exception as exc:
        logger.error("Stale nudge failed: %s", exc)


async def obsidian_sync_job(context) -> None:
    await run_sync()
