from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
)

import lib.llm as llm
import lib.todoist as todoist
from lib.handlers.auth import WHITELIST_FILTER

logger = logging.getLogger(__name__)

PICKING_PROJECT = 0

# Max projects shown as inline buttons
_MAX_PROJECTS = 20


async def project_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Fetching projects…")

    try:
        projects_map = await asyncio.to_thread(todoist.get_all_projects)
    except Exception as exc:
        logger.error("Failed to fetch projects: %s", exc)
        await update.message.reply_text("Failed to fetch projects. Please try again.")
        return ConversationHandler.END

    if not projects_map:
        await update.message.reply_text("No projects found.")
        return ConversationHandler.END

    # Build inline keyboard — one button per project (sorted, capped)
    names = sorted(projects_map.values())[:_MAX_PROJECTS]
    buttons = [[InlineKeyboardButton(name, callback_data=f"proj:{name}")] for name in names]
    keyboard = InlineKeyboardMarkup(buttons)
    context.user_data["projects_map"] = projects_map
    await update.message.reply_text("Pick a project:", reply_markup=keyboard)
    return PICKING_PROJECT


async def project_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    project_name = query.data.removeprefix("proj:")

    await query.edit_message_text(f"Analysing *{project_name}*…", parse_mode="Markdown")

    try:
        projects_map: dict[str, str] = context.user_data.get(
            "projects_map"
        ) or await asyncio.to_thread(todoist.get_all_projects)
        project_id = next((pid for pid, name in projects_map.items() if name == project_name), None)
        if not project_id:
            await query.edit_message_text("Project not found.")
            return ConversationHandler.END

        tasks = await asyncio.to_thread(todoist.get_tasks_by_project, project_id)
        plan = await llm.generate_project_plan(project_name, tasks)
        await query.edit_message_text(plan, parse_mode="Markdown")
    except Exception as exc:
        logger.error("Project plan failed for '%s': %s", project_name, exc)
        await query.edit_message_text("Failed to generate project plan. Please try again.")

    return ConversationHandler.END


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


project_handler = ConversationHandler(
    entry_points=[CommandHandler("project", project_cmd, WHITELIST_FILTER)],
    states={
        PICKING_PROJECT: [
            CallbackQueryHandler(project_pick_cb, pattern="^proj:"),
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_cmd, WHITELIST_FILTER)],
    per_chat=True,
    per_message=False,
    conversation_timeout=600,
    name="project_plan",
)
