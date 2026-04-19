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
    MessageHandler,
    filters,
)

import lib.audit as audit
import lib.llm as llm
import lib.todoist as todoist
from lib.handlers.auth import WHITELIST_FILTER
from lib.models import Task

logger = logging.getLogger(__name__)

PICKING = 0
BRAINSTORM_INPUT = 1
BRAINSTORM_REVIEW = 2

_PICK_PREFIX = "opt_pick:"
_ACCEPT_CB = "opt:accept"
_REJECT_CB = "opt:reject"

_MAX_LIST = 15
_TODOIST_TASK_URL = "https://todoist.com/app/task/{id}"


@dataclass
class OptimizeSession:
    tasks: list[dict]
    current_task: dict | None = None
    proposed: list[str] = field(default_factory=list)
    proposal_index: int = 0
    created: int = 0


_sessions: dict[int, OptimizeSession] = {}


def _sort_key(task: dict) -> tuple[int, int]:
    labels = task.get("labels") or []
    if "quarantined" in labels:
        return (0, 0)
    age = todoist.get_task_age(labels)
    if age > 0:
        return (1, -age)
    return (2, 0)


def _age_badge(task: dict) -> str:
    labels = task.get("labels") or []
    if "quarantined" in labels:
        return " (quarantined)"
    age = todoist.get_task_age(labels)
    return f" (age{age})" if age > 0 else ""


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


async def optimize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    await update.message.reply_text("Fetching tasks…")

    all_tasks = await asyncio.to_thread(todoist.get_all_tasks)
    if not all_tasks:
        await update.message.reply_text("No tasks found.")
        return ConversationHandler.END

    sorted_tasks = sorted(all_tasks, key=_sort_key)[:_MAX_LIST]
    _sessions[chat_id] = OptimizeSession(tasks=sorted_tasks)

    buttons = []
    for task in sorted_tasks:
        label = f"{task['title']}{_age_badge(task)}"
        if len(label) > 64:
            label = label[:61] + "…"
        buttons.append([InlineKeyboardButton(label, callback_data=f"{_PICK_PREFIX}{task['id']}")])

    await context.bot.send_message(
        chat_id,
        "Pick a task to break down:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return PICKING


# ---------------------------------------------------------------------------
# Pick
# ---------------------------------------------------------------------------


async def pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END

    task_id = query.data[len(_PICK_PREFIX) :]
    task = next((t for t in session.tasks if t["id"] == task_id), None)
    if not task:
        await query.answer("Task not found.", show_alert=True)
        return PICKING

    session.current_task = task
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        chat_id,
        f"What's your plan for *{task['title']}*?\n\nDescribe freely — I'll turn it into tasks:",
        parse_mode="Markdown",
    )
    return BRAINSTORM_INPUT


# ---------------------------------------------------------------------------
# Brainstorm input
# ---------------------------------------------------------------------------


async def brainstorm_input_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = _sessions.get(chat_id)
    if not session or not session.current_task:
        return ConversationHandler.END

    user_text = update.message.text.strip()
    proposed = await llm.breakdown_tasks_for_optimize(session.current_task, user_text)

    if not proposed:
        await update.message.reply_text(
            "Couldn't extract tasks from that — try again, or /cancel to exit."
        )
        return BRAINSTORM_INPUT

    session.proposed = proposed
    session.proposal_index = 0
    await _show_proposal(chat_id, context)
    return BRAINSTORM_REVIEW


# ---------------------------------------------------------------------------
# Proposal review
# ---------------------------------------------------------------------------


def _proposal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Accept", callback_data=_ACCEPT_CB),
                InlineKeyboardButton("❌ Reject", callback_data=_REJECT_CB),
            ]
        ]
    )


async def _show_proposal(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session:
        return
    if session.proposal_index >= len(session.proposed):
        await _finalize_breakdown(chat_id, context)
        return
    title = session.proposed[session.proposal_index]
    idx = session.proposal_index + 1
    total = len(session.proposed)
    await context.bot.send_message(
        chat_id,
        f"*Proposal {idx} of {total}*\n\n{title}",
        reply_markup=_proposal_keyboard(),
        parse_mode="Markdown",
    )


async def accept_proposal_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or session.proposal_index >= len(session.proposed):
        return ConversationHandler.END

    title = session.proposed[session.proposal_index]
    original = session.current_task
    original_title = original["title"]
    task_url = _TODOIST_TASK_URL.format(id=original["id"])
    notes = f"From: [{original_title}]({task_url})"
    if original.get("notes"):
        notes += f"\n\n{original['notes']}"

    task = Task(title=title, notes=notes, priority=original.get("priority", "p4"), labels=[])
    try:
        task_id = await asyncio.to_thread(todoist.create_todoist_task, task)
        session.created += 1
        audit.log(
            "create",
            source="optimize/breakdown",
            trigger="user_accept",
            task_id=task_id,
            title=title,
            original_task_id=original["id"],
            original_title=original_title,
        )
        logger.info("[optimize] created breakdown task: %s", title)
        await query.edit_message_text(f"✅ _Created:_ {title}", parse_mode="Markdown")
    except Exception as exc:
        logger.error("[optimize] failed to create task '%s': %s", title, exc)
        await query.answer("Failed to create task.", show_alert=True)

    session.proposal_index += 1
    await _show_proposal(chat_id, context)
    return BRAINSTORM_REVIEW


async def reject_proposal_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or session.proposal_index >= len(session.proposed):
        return ConversationHandler.END

    title = session.proposed[session.proposal_index]
    await query.edit_message_text(f"❌ _Rejected:_ {title}", parse_mode="Markdown")
    session.proposal_index += 1
    await _show_proposal(chat_id, context)
    return BRAINSTORM_REVIEW


# ---------------------------------------------------------------------------
# Finalize
# ---------------------------------------------------------------------------


async def _finalize_breakdown(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session or not session.current_task:
        return

    original = session.current_task
    original_id = original["id"]
    try:
        await asyncio.to_thread(todoist.delete_todoist_task, original_id)
        audit.log(
            "delete",
            source="optimize/breakdown_finalize",
            trigger="auto",
            task_id=original_id,
            title=original.get("title", ""),
            note="deleted after user broke it down into subtasks",
        )
        logger.info("[optimize] deleted original task %s after breakdown", original_id)
    except Exception as exc:
        logger.error("[optimize] failed to delete original task %s: %s", original_id, exc)

    created = session.created
    _sessions.pop(chat_id, None)
    await context.bot.send_message(
        chat_id,
        f"Done — created {created} task{'s' if created != 1 else ''}, original deleted.",
    )


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text("Optimize cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Handler export
# ---------------------------------------------------------------------------


optimize_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("optimize", optimize_cmd, WHITELIST_FILTER)],
    states={
        PICKING: [
            CallbackQueryHandler(pick_cb, pattern=f"^{_PICK_PREFIX}"),
        ],
        BRAINSTORM_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND & WHITELIST_FILTER, brainstorm_input_cb),
        ],
        BRAINSTORM_REVIEW: [
            CallbackQueryHandler(accept_proposal_cb, pattern=f"^{_ACCEPT_CB}$"),
            CallbackQueryHandler(reject_proposal_cb, pattern=f"^{_REJECT_CB}$"),
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_cmd, WHITELIST_FILTER)],
    per_chat=True,
    per_message=False,
    conversation_timeout=600,
    name="optimize",
)
