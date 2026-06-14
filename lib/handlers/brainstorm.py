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

WAITING_INPUT = 0
REVIEWING_TASK = 1

_ACCEPT_CB = "brainstorm:accept"
_REJECT_CB = "brainstorm:reject"
_CONTINUE_CB = "brainstorm:continue"
_DONE_CB = "brainstorm:done"


@dataclass
class BrainstormSession:
    proposed: list[str] = field(default_factory=list)
    index: int = 0
    created: int = 0
    rejected: int = 0


_sessions: dict[int, BrainstormSession] = {}


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


async def brainstorm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and update.effective_chat is not None
    _sessions[update.effective_chat.id] = BrainstormSession()
    await update.message.reply_text(
        "Brainstorm mode — tell me what's on your plate. "
        "I'll extract tasks for you to accept or reject.\n\n"
        "Just type your thoughts freely:"
    )
    return WAITING_INPUT


# ---------------------------------------------------------------------------
# Input → extraction
# ---------------------------------------------------------------------------


async def input_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and update.effective_chat is not None
    assert update.message.text is not None
    chat_id = update.effective_chat.id
    session = _sessions.get(chat_id)
    if session is None:
        return ConversationHandler.END

    await update.message.reply_text("Extracting tasks…")
    try:
        tasks = await llm.brainstorm_extract_tasks(update.message.text.strip())
    except Exception as exc:
        logger.error("[brainstorm] LLM extraction failed: %s", exc)
        await update.message.reply_text(
            "Couldn't reach AI — please try again.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⏭ Skip", callback_data=_DONE_CB)]]
            ),
        )
        return WAITING_INPUT

    if not tasks:
        await update.message.reply_text(
            "Couldn't find any tasks in that. Try again or /cancel to exit.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("⏭ Skip", callback_data=_DONE_CB),
                    ]
                ]
            ),
        )
        return WAITING_INPUT

    session.proposed = tasks
    session.index = 0
    await _show_task(update.message.chat_id, context, new_message=True)
    return REVIEWING_TASK


# ---------------------------------------------------------------------------
# Task review
# ---------------------------------------------------------------------------


def _review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Accept", callback_data=_ACCEPT_CB),
                InlineKeyboardButton("❌ Reject", callback_data=_REJECT_CB),
            ]
        ]
    )


def _wrapup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ Keep brainstorming", callback_data=_CONTINUE_CB),
                InlineKeyboardButton("✔ Done", callback_data=_DONE_CB),
            ]
        ]
    )


async def _show_task(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE, new_message: bool = False
) -> None:
    session = _sessions.get(chat_id)
    if session is None:
        return

    if session.index >= len(session.proposed):
        await _show_wrapup(chat_id, context)
        return

    title = session.proposed[session.index]
    idx = session.index + 1
    total = len(session.proposed)
    text = f"*Task {idx} of {total}*\n\n{title}"
    await context.bot.send_message(
        chat_id, text, reply_markup=_review_keyboard(), parse_mode="Markdown"
    )


async def _show_wrapup(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if session is None:
        return
    text = (
        f"All done! Created {session.created} task{'s' if session.created != 1 else ''}, "
        f"rejected {session.rejected}.\n\n"
        "Continue brainstorming or finish?"
    )
    await context.bot.send_message(chat_id, text, reply_markup=_wrapup_keyboard())


async def accept_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None and update.effective_chat is not None
    await query.answer()
    chat_id = update.effective_chat.id
    session = _sessions.get(chat_id)
    if session is None or session.index >= len(session.proposed):
        return ConversationHandler.END

    title = session.proposed[session.index]
    try:
        task = Task(title=title)
        task_id = await asyncio.to_thread(todoist.create_todoist_task, task)
        session.created += 1
        audit.log(
            "create", source="brainstorm", trigger="user_accept", task_id=task_id, title=title
        )
        logger.info("[brainstorm] created task: %s", title)
        await query.edit_message_text(f"✅ _Created:_ {title}", parse_mode="Markdown")
    except Exception as exc:
        logger.error("[brainstorm] failed to create task '%s': %s", title, exc)
        await query.answer("Failed to create task.", show_alert=True)

    session.index += 1
    await _show_task(chat_id, context)
    return REVIEWING_TASK


async def reject_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None and update.effective_chat is not None
    await query.answer()
    chat_id = update.effective_chat.id
    session = _sessions.get(chat_id)
    if session is None or session.index >= len(session.proposed):
        return ConversationHandler.END

    title = session.proposed[session.index]
    session.rejected += 1
    session.index += 1
    logger.info("[brainstorm] rejected task: %s", title)
    audit.log("reject", source="brainstorm", trigger="user_reject", title=title)
    await query.edit_message_text(f"❌ _Rejected:_ {title}", parse_mode="Markdown")
    await _show_task(chat_id, context)
    return REVIEWING_TASK


async def continue_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    await query.edit_message_text("Keep going — what else is on your mind?")
    return WAITING_INPUT


async def done_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None and update.effective_chat is not None
    await query.answer()
    chat_id = update.effective_chat.id
    session = _sessions.pop(chat_id, None)
    created = session.created if session else 0
    await query.edit_message_text(
        f"Brainstorm complete. {created} task{'s' if created != 1 else ''} added to Todoist."
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and update.effective_chat is not None
    _sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text("Brainstorm cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Handler export
# ---------------------------------------------------------------------------


brainstorm_handler = ConversationHandler(
    entry_points=[CommandHandler("brainstorm", brainstorm_cmd, WHITELIST_FILTER)],  # type: ignore
    states={  # type: ignore
        WAITING_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND & WHITELIST_FILTER, input_received),
        ],
        REVIEWING_TASK: [
            CallbackQueryHandler(accept_cb, pattern=f"^{_ACCEPT_CB}$"),
            CallbackQueryHandler(reject_cb, pattern=f"^{_REJECT_CB}$"),
            CallbackQueryHandler(continue_cb, pattern=f"^{_CONTINUE_CB}$"),
            CallbackQueryHandler(done_cb, pattern=f"^{_DONE_CB}$"),
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_cmd, WHITELIST_FILTER)],  # type: ignore
    per_chat=True,
    per_message=False,  # single conversation state per chat, not per message
    conversation_timeout=600,
    name="brainstorm",
)
