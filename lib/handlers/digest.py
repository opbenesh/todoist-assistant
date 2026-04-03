from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

import lib.llm as llm
import lib.todoist as todoist
from lib.handlers.auth import WHITELIST_FILTER

logger = logging.getLogger(__name__)


async def digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        tasks = await asyncio.to_thread(todoist.get_today_tasks)
        digest = await llm.generate_digest(tasks)
        await update.message.reply_text(digest, parse_mode="Markdown")
    except Exception as exc:
        logger.error("Digest generation failed: %s", exc)
        await update.message.reply_text("Failed to generate digest. Please try again.")


digest_handler = CommandHandler("digest", digest_cmd, WHITELIST_FILTER)
