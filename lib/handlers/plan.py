from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

import lib.llm as llm
import lib.todoist as todoist
from lib.handlers.auth import WHITELIST_FILTER

logger = logging.getLogger(__name__)


async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Generating your plan...")
    try:
        tasks = await asyncio.to_thread(todoist.get_today_tasks)
        plan = await llm.generate_plan(tasks)
        await update.message.reply_text(plan, parse_mode="Markdown")
    except Exception as exc:
        logger.error("Plan generation failed: %s", exc)
        await update.message.reply_text("Failed to generate plan. Please try again.")


plan_handler = CommandHandler("plan", plan_cmd, WHITELIST_FILTER)
