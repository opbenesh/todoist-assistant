from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

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
from lib.models import PRIORITY_TO_TODOIST

logger = logging.getLogger(__name__)

REVIEWING = 0


@dataclass
class OptimizeSession:
    tasks: list[dict]
    projects: list[str]  # project names (for LLM)
    projects_map: dict[str, str]  # name -> id (for updates)
    current_task: dict | None = None
    current_proposal: dict = field(default_factory=dict)
    applied: int = 0
    skipped: int = 0
    total: int = 0


_sessions: dict[int, OptimizeSession] = {}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_message(session: OptimizeSession) -> str:
    task = session.current_task
    proposal = session.current_proposal
    remaining = len(session.tasks)

    lines = [
        f"🔍 *{task['title']}*",
        f"Project: {task.get('project', '—')} | Priority: {task['priority']}",
        "",
        "*Suggested changes:*",
    ]
    for key in ("title", "priority", "project", "labels", "due_date"):
        if key not in proposal:
            continue
        val = proposal[key]
        if isinstance(val, list):
            val = ", ".join(val) if val else "none"
        label = key.replace("_", " ").title()
        lines.append(f"• {label} → {val}")

    if remaining:
        lines.append(f"\n_{remaining} task{'s' if remaining > 1 else ''} left in queue_")

    return "\n".join(lines)


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Apply", callback_data="opt:apply"),
                InlineKeyboardButton("⏭ Skip", callback_data="opt:skip"),
            ],
            [
                InlineKeyboardButton("🗑 Archive", callback_data="opt:archive"),
                InlineKeyboardButton("🛑 Stop", callback_data="opt:stop"),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _fetch_data() -> tuple[list[dict], list[str], dict[str, str]]:
    """Sync: fetch tasks to optimize and project info."""
    projects_id_to_name = todoist.get_all_projects()
    tasks = todoist.get_tasks_to_optimize(projects_id_to_name)
    project_names = sorted(projects_id_to_name.values())
    projects_name_to_id = {v: k for k, v in projects_id_to_name.items()}
    return tasks, project_names, projects_name_to_id


async def _advance(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> int:
    """Pop tasks one by one until we find one with LLM suggestions, then show it."""
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END

    while session.tasks:
        task = session.tasks.pop(0)
        session.current_task = task
        proposal = await asyncio.to_thread(llm.propose_task_optimization, task, session.projects)
        if proposal:
            session.current_proposal = proposal
            text = _format_message(session)
            keyboard = _keyboard()
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text, reply_markup=keyboard, parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")
            return REVIEWING
        # LLM found nothing to improve — silently move on

    # All tasks exhausted
    summary = f"✅ All done! Applied: {session.applied}, skipped by you: {session.skipped}."
    _sessions.pop(chat_id, None)
    if update.callback_query:
        await update.callback_query.edit_message_text(summary)
    else:
        await update.message.reply_text(summary)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def optimize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Fetching tasks to review…")

    tasks, project_names, projects_map = await asyncio.to_thread(_fetch_data)
    if not tasks:
        await update.message.reply_text("No tasks matched optimization criteria.")
        return ConversationHandler.END

    session = OptimizeSession(
        tasks=tasks,
        projects=project_names,
        projects_map=projects_map,
        total=len(tasks),
    )
    _sessions[update.effective_chat.id] = session

    return await _advance(update, context, update.effective_chat.id)


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


async def apply_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or not session.current_task:
        return ConversationHandler.END

    task_id = session.current_task["id"]
    proposal = session.current_proposal

    kwargs: dict = {}
    if "title" in proposal:
        kwargs["content"] = proposal["title"]
    if "priority" in proposal:
        kwargs["priority"] = PRIORITY_TO_TODOIST[proposal["priority"]]
    if "labels" in proposal:
        kwargs["labels"] = proposal["labels"]
    if "due_date" in proposal:
        kwargs["due_date"] = proposal["due_date"]
    if "project" in proposal:
        project_id = session.projects_map.get(proposal["project"])
        if project_id:
            kwargs["project_id"] = project_id

    try:
        await asyncio.to_thread(todoist.update_todoist_task, task_id, **kwargs)
        session.applied += 1
    except Exception as exc:
        logger.error("Failed to update task %s: %s", task_id, exc)
        await query.answer("Failed to update task.", show_alert=True)

    return await _advance(update, context, chat_id)


async def archive_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Yes, delete", callback_data="opt:archive_confirm"),
                    InlineKeyboardButton("No, keep", callback_data="opt:archive_cancel"),
                ]
            ]
        )
    )
    return REVIEWING


async def archive_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or not session.current_task:
        return ConversationHandler.END

    task_id = session.current_task["id"]
    try:
        await asyncio.to_thread(todoist.delete_todoist_task, task_id)
        session.applied += 1
    except Exception as exc:
        logger.error("Failed to delete task %s: %s", task_id, exc)
        await query.answer("Failed to delete task.", show_alert=True)

    return await _advance(update, context, chat_id)


async def archive_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    # Restore the original proposal keyboard
    await query.edit_message_reply_markup(reply_markup=_keyboard())
    return REVIEWING


async def skip_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if session:
        session.skipped += 1
    return await _advance(update, context, chat_id)


async def stop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.pop(chat_id, None)
    counts = f"Applied: {session.applied}, skipped: {session.skipped}." if session else ""
    await query.edit_message_text(f"Stopped. {counts}")
    return ConversationHandler.END


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Handler export
# ---------------------------------------------------------------------------


optimize_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("optimize", optimize_cmd, WHITELIST_FILTER)],
    states={
        REVIEWING: [
            CallbackQueryHandler(apply_cb, pattern="^opt:apply$"),
            CallbackQueryHandler(skip_cb, pattern="^opt:skip$"),
            CallbackQueryHandler(archive_cb, pattern="^opt:archive$"),
            CallbackQueryHandler(archive_confirm_cb, pattern="^opt:archive_confirm$"),
            CallbackQueryHandler(archive_cancel_cb, pattern="^opt:archive_cancel$"),
            CallbackQueryHandler(stop_cb, pattern="^opt:stop$"),
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_cmd, WHITELIST_FILTER)],
    per_chat=True,
    conversation_timeout=600,
    name="task_optimize",
)
