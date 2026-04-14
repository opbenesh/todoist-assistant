from __future__ import annotations

import asyncio
import logging
from datetime import date

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

import lib.llm as llm
import lib.obsidian as obsidian
import lib.todoist as todoist
from lib.handlers.auth import WHITELIST_FILTER

logger = logging.getLogger(__name__)


async def insights_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Generating insights… (this may take a moment)")

    try:
        completed, overdue, all_tasks, quarantined = await asyncio.gather(
            asyncio.to_thread(todoist.get_completed_tasks, 14),
            asyncio.to_thread(todoist.get_overdue_tasks),
            asyncio.to_thread(todoist.get_all_tasks),
            asyncio.to_thread(todoist.get_quarantined_tasks),
        )
        report = await llm.generate_insights(completed, overdue, all_tasks, quarantined)

        filename = f"insights-{date.today().isoformat()}.md"
        await asyncio.gather(
            asyncio.to_thread(obsidian.write_insight, filename, report),
            update.message.reply_text(report, parse_mode="Markdown"),
        )
        await update.message.reply_text(f"_Saved to vault: {filename}_", parse_mode="Markdown")
    except Exception as exc:
        logger.error("Insights generation failed: %s", exc)
        await update.message.reply_text("Failed to generate insights. Please try again.")


insights_handler = CommandHandler("insights", insights_cmd, WHITELIST_FILTER)
