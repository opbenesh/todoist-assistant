from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

import lib.llm as llm
import lib.todoist as todoist
from lib.handlers.auth import WHITELIST_FILTER

logger = logging.getLogger(__name__)


async def deepdive_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /deepdive <task_id>")
        return

    task_id = context.args[0]
    await update.message.reply_text("Analysing task…")

    try:
        task = await asyncio.to_thread(todoist.get_task_by_id, task_id)
    except Exception as exc:
        logger.error("Failed to fetch task %s: %s", task_id, exc)
        await update.message.reply_text(f"Could not find task `{task_id}`.", parse_mode="Markdown")
        return

    try:
        analysis = await llm.generate_deepdive(task)
        await update.message.reply_text(analysis, parse_mode="Markdown")
    except Exception as exc:
        logger.error("Deep dive failed for task %s: %s", task_id, exc)
        await update.message.reply_text("Failed to generate analysis. Please try again.")


deepdive_handler = CommandHandler("deepdive", deepdive_cmd, WHITELIST_FILTER)
