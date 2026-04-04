from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import lib.llm as llm
import lib.obsidian as obsidian
import lib.todoist as todoist
from lib.handlers.auth import WHITELIST_FILTER
from lib.scheduler import _settings

logger = logging.getLogger(__name__)

AWAITING_CONTEXT = 0
REVIEWING_PUSHES = 1


@dataclass
class PlanSession:
    push_tasks: list[dict]  # [{"id": str, "title": str}, ...]
    pushed: int = 0
    kept: int = 0


_plan_sessions: dict[int, PlanSession] = {}


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Any context for today? (meetings, constraints, energy level — or /skip)"
    )
    return AWAITING_CONTEXT


async def receive_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _generate_and_send(update, update.message.text.strip())


async def skip_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _generate_and_send(update, "")


async def _write_planned_tasks() -> None:
    """Write today's Todoist tasks to the daily note, marking the day as planned."""
    tasks = await asyncio.to_thread(todoist.get_today_tasks)
    lines = [
        obsidian.format_task_line(
            t["title"], t["is_completed"],
            t.get("priority", "p4"), t.get("duration_minutes"),
        )
        for t in tasks
    ]
    await asyncio.to_thread(obsidian.write_tasks_section, lines)


async def _generate_and_send(update: Update, user_context: str) -> int:
    await update.message.reply_text("Generating your plan…")
    try:
        tasks = await asyncio.to_thread(todoist.get_today_tasks)
        result = await llm.generate_plan(tasks, _settings, user_context)
        plan_md = result["plan_markdown"]
        push_tasks = result["push_tasks"]

        await asyncio.gather(
            update.message.reply_text(plan_md, parse_mode="Markdown"),
            asyncio.to_thread(obsidian.append_plan, plan_md),
        )

        if push_tasks:
            chat_id = update.effective_chat.id
            _plan_sessions[chat_id] = PlanSession(push_tasks=list(push_tasks))
            return await _advance_push(update, chat_id)

        await _write_planned_tasks()

    except Exception as exc:
        logger.error("Plan generation failed: %s", exc)
        await update.message.reply_text("Failed to generate plan. Please try again.")

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Push review loop
# ---------------------------------------------------------------------------


def _push_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Keep today", callback_data="plan:keep"),
                InlineKeyboardButton("⏭ Push", callback_data="plan:push"),
            ],
            [InlineKeyboardButton("🛑 Done", callback_data="plan:done")],
        ]
    )


async def _advance_push(update: Update, chat_id: int) -> int:
    session = _plan_sessions.get(chat_id)
    if not session or not session.push_tasks:
        return await _finish_push(update, chat_id)

    task = session.push_tasks[0]
    remaining = len(session.push_tasks)
    text = (
        f"Push forward?\n\n"
        f"📌 *{task['title']}*\n"
        f"No hard deadline — due date will be removed.\n\n"
        f"_{remaining} task{'s' if remaining > 1 else ''} remaining_"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=_push_keyboard(), parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=_push_keyboard(), parse_mode="Markdown"
        )
    return REVIEWING_PUSHES


async def _finish_push(update: Update, chat_id: int) -> int:
    session = _plan_sessions.pop(chat_id, None)
    pushed = session.pushed if session else 0
    kept = session.kept if session else 0
    summary = f"Done. Pushed {pushed}, kept {kept} for today."
    if update.callback_query:
        await update.callback_query.edit_message_text(summary)
    else:
        await update.message.reply_text(summary)
    await _write_planned_tasks()
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


async def push_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _plan_sessions.get(chat_id)
    if not session or not session.push_tasks:
        return ConversationHandler.END

    task = session.push_tasks.pop(0)
    try:
        await asyncio.to_thread(todoist.remove_task_due_date, task["id"])
        session.pushed += 1
        logger.info("[plan] pushed task %s '%s'", task["id"], task["title"][:60])
    except Exception as exc:
        logger.error("Failed to remove due date for task %s: %s", task["id"], exc)
        await query.answer("Failed to update task.", show_alert=True)

    return await _advance_push(update, chat_id)


async def keep_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _plan_sessions.get(chat_id)
    if session and session.push_tasks:
        task = session.push_tasks.pop(0)
        session.kept += 1
        try:
            await asyncio.to_thread(todoist.reschedule_task_to_today, task["id"])
            logger.info("[plan] kept task %s '%s' → rescheduled to today", task["id"], task["title"][:60])
        except Exception as exc:
            logger.error("Failed to reschedule task %s: %s", task["id"], exc)
    return await _advance_push(update, chat_id)


async def done_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await _finish_push(update, query.message.chat_id)


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _plan_sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Handler export
# ---------------------------------------------------------------------------


plan_handler = ConversationHandler(
    entry_points=[CommandHandler("plan", plan_cmd, WHITELIST_FILTER)],
    states={
        AWAITING_CONTEXT: [
            CommandHandler("skip", skip_context, WHITELIST_FILTER),
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_context),
        ],
        REVIEWING_PUSHES: [
            CallbackQueryHandler(push_cb, pattern="^plan:push$"),
            CallbackQueryHandler(keep_cb, pattern="^plan:keep$"),
            CallbackQueryHandler(done_cb, pattern="^plan:done$"),
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_cmd, WHITELIST_FILTER)],
    per_chat=True,
    conversation_timeout=600,
    name="plan",
)
