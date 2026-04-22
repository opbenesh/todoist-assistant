from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

import lib.todoist as todoist
from lib.handlers.auth import WHITELIST_FILTER


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks = await asyncio.to_thread(todoist.get_today_tasks)
    if not tasks:
        await update.message.reply_text("No tasks for today.")
        return

    lines = []
    for i, t in enumerate(tasks, 1):
        status = "✓" if t["is_completed"] else "○"
        priority = f" [{t['priority'].upper()}]" if t["priority"] != "p4" else ""
        due = f" — {t['due_date']}" if t["due_date"] else ""
        recurring = " 🔁" if t.get("is_recurring") else ""
        lines.append(f"{i}. {status} {t['title']}{priority}{due}{recurring}  `{t['id']}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


list_handler = CommandHandler("list", list_cmd, WHITELIST_FILTER)
