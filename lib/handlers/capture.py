from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import lib.audit as audit
import lib.llm as llm
import lib.todoist as todoist
from lib.handlers.auth import WHITELIST_FILTER
from lib.models import VALID_PRIORITIES, EnrichmentState, Task

logger = logging.getLogger(__name__)

# Conversation states
CONFIRM_OR_EDIT, EDIT_TITLE, EDIT_DUE, EDIT_PRIORITY, EDIT_DURATION, BREAKDOWN = range(6)

# In-memory session store keyed by chat_id
_sessions: dict[int, EnrichmentState] = {}


def _get_session(update: Update) -> EnrichmentState | None:
    return _sessions.get(update.effective_chat.id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_proposal(task: Task) -> str:
    lines = [f"*{task.title}*"]
    if task.notes:
        lines.append(f"_{task.notes}_")
    lines.append("")
    lines.append(f"📅 Due: {task.due_date or 'not set'}")
    lines.append(f"🔴 Priority: {task.priority.upper()}")
    lines.append(f"🏷 Labels: {', '.join(task.labels) if task.labels else 'none'}")
    dur = f"{task.duration_minutes} min" if task.duration_minutes else "not set"
    lines.append(f"⏱ Duration: {dur}")
    return "\n".join(lines)


def _enrichment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Confirm", callback_data="confirm")],
            [
                InlineKeyboardButton("📅 Edit due", callback_data="edit:due"),
                InlineKeyboardButton("🔴 Edit priority", callback_data="edit:priority"),
            ],
            [
                InlineKeyboardButton("⏱ Edit duration", callback_data="edit:duration"),
                InlineKeyboardButton("✏️ Edit title", callback_data="edit:title"),
            ],
            [
                InlineKeyboardButton("🔀 Break down", callback_data="breakdown"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
            ],
        ]
    )


async def _show_proposal(update: Update, state: EnrichmentState, edit: bool = False) -> None:
    text = _format_proposal(state.task)
    keyboard = _enrichment_keyboard()
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=keyboard, parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = " ".join(context.args) if context.args else ""
    if not raw:
        await update.message.reply_text("Usage: /task <title>")
        return ConversationHandler.END

    await update.message.reply_text("Thinking...")

    try:
        task = await llm.propose_enrichment(raw)
    except Exception as exc:
        logger.error("Enrichment failed: %s", exc)
        # Fall back to bare task
        task = Task(title=raw)

    state = EnrichmentState(chat_id=update.effective_chat.id, raw_title=raw, task=task)
    _sessions[update.effective_chat.id] = state

    await _show_proposal(update, state)
    return CONFIRM_OR_EDIT


# ---------------------------------------------------------------------------
# Confirmation / editing callbacks
# ---------------------------------------------------------------------------


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    state = _sessions.pop(query.message.chat_id, None)
    if not state:
        await query.edit_message_text("Session expired. Use /task to start again.")
        return ConversationHandler.END

    try:
        task_id = await asyncio.to_thread(todoist.create_todoist_task, state.task)
        state.task.todoist_id = task_id
        audit.log(
            "create",
            source="capture/task",
            trigger="user_confirm",
            task_id=task_id,
            title=state.task.title,
        )
        await query.edit_message_text(f"Saved: *{state.task.title}*", parse_mode="Markdown")
    except Exception as exc:
        logger.error("Failed to create Todoist task: %s", exc)
        await query.edit_message_text("Failed to save task. Please try again.")

    return ConversationHandler.END


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _sessions.pop(query.message.chat_id, None)
    await query.edit_message_text("Cancelled.")
    return ConversationHandler.END


async def edit_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    field = query.data.split(":")[1]
    await query.answer()

    prompts = {
        "due": "Enter due date (e.g. tomorrow, 2026-05-01, or 'none'):",
        "priority": "Enter priority (p1, p2, p3, or p4):",
        "duration": "Enter duration in minutes (or 0 to clear):",
        "title": "Enter new title:",
    }
    states = {
        "due": EDIT_DUE,
        "priority": EDIT_PRIORITY,
        "duration": EDIT_DURATION,
        "title": EDIT_TITLE,
    }

    await query.message.reply_text(prompts.get(field, "Enter new value:"))
    return states.get(field, CONFIRM_OR_EDIT)


# ---------------------------------------------------------------------------
# Field edit handlers
# ---------------------------------------------------------------------------


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = _get_session(update)
    if not state:
        return ConversationHandler.END
    state.task.title = update.message.text.strip()
    await _show_proposal(update, state)
    return CONFIRM_OR_EDIT


async def receive_due(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from datetime import date as date_type

    state = _get_session(update)
    if not state:
        return ConversationHandler.END

    text = update.message.text.strip().lower()
    if text in ("none", "clear", ""):
        state.task.due_date = None
    else:
        try:
            state.task.due_date = date_type.fromisoformat(text)
        except ValueError:
            await update.message.reply_text("Use ISO format (2026-05-01) or 'none'. Try again:")
            return EDIT_DUE

    await _show_proposal(update, state)
    return CONFIRM_OR_EDIT


async def receive_priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = _get_session(update)
    if not state:
        return ConversationHandler.END

    text = update.message.text.strip().lower()
    if text not in VALID_PRIORITIES:
        await update.message.reply_text("Enter p1, p2, p3, or p4:")
        return EDIT_PRIORITY

    state.task.priority = text  # type: ignore[assignment]
    await _show_proposal(update, state)
    return CONFIRM_OR_EDIT


async def receive_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = _get_session(update)
    if not state:
        return ConversationHandler.END

    text = update.message.text.strip()
    try:
        minutes = int(text)
        state.task.duration_minutes = minutes if minutes > 0 else None
    except ValueError:
        await update.message.reply_text("Enter a number (minutes), or 0 to clear:")
        return EDIT_DURATION

    await _show_proposal(update, state)
    return CONFIRM_OR_EDIT


async def breakdown_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask LLM to break the task into subtasks, then show confirmation."""
    query = update.callback_query
    await query.answer()
    state = _get_session(update)
    if not state or not state.task:
        return ConversationHandler.END

    await query.edit_message_text("Breaking down task…")

    try:
        subtasks = await asyncio.to_thread(llm.propose_breakdown, state.task)
    except Exception as exc:
        logger.error("Breakdown failed: %s", exc)
        subtasks = []

    if not subtasks:
        await query.edit_message_text("Couldn't generate subtasks. Try again.")
        await _show_proposal(update, state, edit=False)
        return CONFIRM_OR_EDIT

    state.subtasks = subtasks
    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(subtasks, 1))
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Create all", callback_data="breakdown:confirm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="breakdown:cancel")],
        ]
    )
    await query.edit_message_text(
        f"*{state.task.title}*\n\nSubtasks:\n{numbered}",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return BREAKDOWN


async def breakdown_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Create parent task then all subtasks in Todoist."""
    query = update.callback_query
    await query.answer()
    state = _sessions.pop(query.message.chat_id, None)
    if not state or not state.task:
        return ConversationHandler.END

    try:
        parent_id = await asyncio.to_thread(todoist.create_todoist_task, state.task)
        audit.log(
            "create",
            source="capture/breakdown",
            trigger="user_confirm",
            task_id=parent_id,
            title=state.task.title,
        )
        for title in state.subtasks:
            sub = Task(
                title=title,
                priority=state.task.priority,
                labels=state.task.labels,
                due_date=state.task.due_date,
            )
            sub_id = await asyncio.to_thread(todoist.create_todoist_task, sub, parent_id)
            audit.log(
                "create",
                source="capture/breakdown",
                trigger="user_confirm",
                task_id=sub_id,
                title=title,
                parent_id=parent_id,
            )
        await query.edit_message_text(
            f"Created *{state.task.title}* with {len(state.subtasks)} subtasks.",
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.error("Failed to create breakdown tasks: %s", exc)
        await query.edit_message_text("Failed to create tasks. Please try again.")

    return ConversationHandler.END


async def breakdown_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return to the enrichment proposal."""
    query = update.callback_query
    await query.answer()
    state = _get_session(update)
    if not state:
        return ConversationHandler.END
    state.subtasks = []
    await _show_proposal(update, state, edit=True)
    return CONFIRM_OR_EDIT


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def timeout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id:
        _sessions.pop(chat_id, None)
        await context.bot.send_message(chat_id, "Task capture timed out. Use /task to start again.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Simple capture handlers (no enrichment)
# ---------------------------------------------------------------------------


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


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /done <task_id>")
        return

    task_id = context.args[0]
    await asyncio.to_thread(todoist.strip_age_labels, task_id)
    await asyncio.to_thread(todoist.complete_todoist_task, task_id)
    audit.log("complete", source="capture/done_cmd", trigger="user_cmd", task_id=task_id)
    await update.message.reply_text("Done.")


async def completed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks = await asyncio.to_thread(todoist.get_completed_tasks)
    if not tasks:
        await update.message.reply_text("No recently completed tasks found.")
        return

    lines = [f"*Recently completed ({len(tasks)}):*"]
    for t in tasks:
        lines.append(f"• {t['title']}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Handler exports
# ---------------------------------------------------------------------------


task_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("task", task_cmd, WHITELIST_FILTER)],
    states={
        CONFIRM_OR_EDIT: [
            CallbackQueryHandler(confirm_callback, pattern="^confirm$"),
            CallbackQueryHandler(cancel_callback, pattern="^cancel$"),
            CallbackQueryHandler(edit_field_callback, pattern="^edit:"),
            CallbackQueryHandler(breakdown_callback, pattern="^breakdown$"),
        ],
        EDIT_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
        EDIT_DUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_due)],
        EDIT_PRIORITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_priority)],
        EDIT_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_duration)],
        BREAKDOWN: [
            CallbackQueryHandler(breakdown_confirm_callback, pattern="^breakdown:confirm$"),
            CallbackQueryHandler(breakdown_cancel_callback, pattern="^breakdown:cancel$"),
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_cmd, WHITELIST_FILTER)],
    per_chat=True,
    per_message=False,
    conversation_timeout=600,
    name="task_capture",
)

list_handler = CommandHandler("list", list_cmd, WHITELIST_FILTER)
done_handler = CommandHandler("done", done_cmd, WHITELIST_FILTER)
completed_handler = CommandHandler("completed", completed_cmd, WHITELIST_FILTER)
