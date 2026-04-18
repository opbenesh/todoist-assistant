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
from lib.llm import restore_links
from lib.models import Task
from lib.optimize_shared import apply_actionable_label

logger = logging.getLogger(__name__)

REVIEWING = 0
BRAINSTORM_INPUT = 1
BRAINSTORM_REVIEW = 2

_OPT_CB = "opt:optimize"
_SKIP_CB = "opt:skip"
_DELETE_CB = "opt:delete"
_OVERRIDE_CB = "opt:override"
_ACCEPT_CB = "opt:accept"
_REJECT_CB = "opt:reject"

_TODOIST_TASK_URL = "https://todoist.com/app/task/{id}"


@dataclass
class OptimizeSession:
    queue: list[dict]              # non-actionable tasks: {id, title, reason, clean_title}
    auto_labeled: int              # set at creation, never mutated
    current_task: dict | None = None
    proposed: list[str] = field(default_factory=list)
    proposal_index: int = 0
    deleted: int = 0
    skipped: int = 0
    broken_down: int = 0
    created: int = 0


_sessions: dict[int, OptimizeSession] = {}


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


async def optimize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    await update.message.reply_text("Fetching tasks…")

    all_tasks = await asyncio.to_thread(todoist.get_all_tasks)
    unlabeled = [t for t in all_tasks if "actionable" not in (t.get("labels") or [])]

    if not unlabeled:
        await update.message.reply_text(
            "All tasks already have the 'actionable' label — nothing to do!"
        )
        return ConversationHandler.END

    await context.bot.send_message(
        chat_id,
        f"Judging {len(unlabeled)} task{'s' if len(unlabeled) != 1 else ''}…",
    )

    results = await llm.judge_tasks(unlabeled)

    # Build id → original task lookup for label merging
    task_by_id = {t["id"]: t for t in unlabeled}

    actionable = [r for r in results if r["actionable"]]
    # Merge original task fields (title, labels, notes) into judge results so
    # downstream handlers (skip_cb, breakdown) can access them without KeyError.
    non_actionable = [
        {**task_by_id.get(r["id"], {}), **r}
        for r in results
        if not r["actionable"]
    ]

    # Auto-label actionable tasks + apply title cleanup concurrently
    await asyncio.gather(*[
        apply_actionable_label(r, task_by_id, "optimize/auto_label")
        for r in actionable
    ])

    total = len(unlabeled)
    n_labeled = len(actionable)
    n_review = len(non_actionable)

    summary = (
        f"Found {total} task{'s' if total != 1 else ''} without 'actionable' label.\n"
        f"Auto-labeled {n_labeled} as actionable."
    )
    if n_review == 0:
        await context.bot.send_message(chat_id, summary + "\n\nAll done!")
        return ConversationHandler.END

    summary += f"\n{n_review} need{'s' if n_review == 1 else ''} your attention."
    await context.bot.send_message(chat_id, summary)

    _sessions[chat_id] = OptimizeSession(queue=non_actionable, auto_labeled=n_labeled)
    await _show_review_task(chat_id, context)
    return REVIEWING


# ---------------------------------------------------------------------------
# Review loop
# ---------------------------------------------------------------------------


def _review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚙️ Optimize", callback_data=_OPT_CB),
            InlineKeyboardButton("⏭ Skip", callback_data=_SKIP_CB),
            InlineKeyboardButton("🗑 Delete", callback_data=_DELETE_CB),
        ],
        [InlineKeyboardButton("✅ It's actionable", callback_data=_OVERRIDE_CB)],
    ])


async def _show_review_task(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session or not session.queue:
        await _finish_session(chat_id, context)
        return

    task = session.queue[0]
    title = task.get("clean_title") or task["title"]
    reason = task.get("reason") or "Unclear next step"
    idx = len(session.queue)
    text = (
        f"*{title}*\n"
        f"_Why not actionable: {reason}_\n\n"
        f"_{idx} task{'s' if idx != 1 else ''} remaining_"
    )
    await context.bot.send_message(
        chat_id, text, reply_markup=_review_keyboard(), parse_mode="Markdown"
    )


async def skip_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or not session.queue:
        return ConversationHandler.END

    task = session.queue.pop(0)
    session.skipped += 1

    # Apply title cleanup for skipped tasks (they stay in Todoist)
    clean = task.get("clean_title")
    if clean:
        new_content = restore_links(task["title"], clean)
        try:
            await asyncio.to_thread(todoist.update_todoist_task, task["id"], content=new_content)
            audit.log("update", source="optimize/skip", trigger="user_skip",
                      task_id=task["id"], title=task["title"],
                      changes={"content": new_content})
        except Exception as exc:
            logger.error("[optimize] skip title cleanup failed for %s: %s", task["id"], exc)

    await query.edit_message_reply_markup(reply_markup=None)
    await _show_review_task(chat_id, context)
    return REVIEWING


async def delete_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or not session.queue:
        return ConversationHandler.END

    task = session.queue.pop(0)
    session.deleted += 1
    try:
        await asyncio.to_thread(todoist.delete_todoist_task, task["id"])
        audit.log("delete", source="optimize/delete", trigger="user_delete",
                  task_id=task["id"], title=task["title"])
        logger.info("[optimize] deleted task %s '%s'", task["id"], task["title"][:60])
    except Exception as exc:
        logger.error("[optimize] delete failed for %s: %s", task["id"], exc)
        await query.answer("Failed to delete task.", show_alert=True)

    await query.edit_message_reply_markup(reply_markup=None)
    await _show_review_task(chat_id, context)
    return REVIEWING


async def override_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or not session.queue:
        return ConversationHandler.END

    task = session.queue.pop(0)
    existing_labels = list(task.get("labels") or [])
    if "actionable" not in existing_labels:
        existing_labels.append("actionable")
    try:
        await asyncio.to_thread(todoist.update_todoist_task, task["id"], labels=existing_labels)
        audit.log("update", source="optimize/override", trigger="user_override",
                  task_id=task["id"], title=task["title"],
                  changes={"labels": existing_labels})
        logger.info("[optimize] override task %s '%s'", task["id"], task["title"][:60])
    except Exception as exc:
        logger.error("[optimize] override failed for %s: %s", task["id"], exc)
        await query.answer("Failed to update task.", show_alert=True)

    await query.edit_message_reply_markup(reply_markup=None)
    await _show_review_task(chat_id, context)
    return REVIEWING


async def optimize_task_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or not session.queue:
        return ConversationHandler.END

    session.current_task = session.queue.pop(0)
    title = session.current_task.get("clean_title") or session.current_task["title"]
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        chat_id,
        f"What's your plan for *{title}*?\n\nDescribe freely — I'll turn it into tasks:",
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
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=_ACCEPT_CB),
        InlineKeyboardButton("❌ Reject", callback_data=_REJECT_CB),
    ]])


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
    text = f"*Proposal {idx} of {total}*\n\n{title}"
    await context.bot.send_message(
        chat_id, text, reply_markup=_proposal_keyboard(), parse_mode="Markdown"
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
    original_title = original.get("clean_title") or original["title"]
    task_url = _TODOIST_TASK_URL.format(id=original["id"])
    notes = f"From: [{original_title}]({task_url})"
    if original.get("notes"):
        notes += f"\n\n{original['notes']}"

    task = Task(
        title=title,
        notes=notes,
        priority=original.get("priority", "p4"),
        labels=["actionable"],
    )
    try:
        task_id = await asyncio.to_thread(todoist.create_todoist_task, task)
        session.created += 1
        audit.log("create", source="optimize/breakdown", trigger="user_accept",
                  task_id=task_id, title=title, original_task_id=original["id"],
                  original_title=original_title)
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
# Finalize breakdown
# ---------------------------------------------------------------------------


async def _finalize_breakdown(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session or not session.current_task:
        return

    original = session.current_task
    original_id = original["id"]
    try:
        await asyncio.to_thread(todoist.delete_todoist_task, original_id)
        session.broken_down += 1
        audit.log("delete", source="optimize/breakdown_finalize", trigger="auto",
                  task_id=original_id, title=original.get("title", ""),
                  note="deleted after user broke it down into subtasks")
        logger.info("[optimize] deleted original task %s after breakdown", original_id)
    except Exception as exc:
        logger.error("[optimize] failed to delete original task %s: %s", original_id, exc)

    session.current_task = None
    session.proposed = []
    session.proposal_index = 0
    await _show_review_task(chat_id, context)


# ---------------------------------------------------------------------------
# Finish
# ---------------------------------------------------------------------------


async def _finish_session(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.pop(chat_id, None)
    if not session:
        return
    text = (
        "Optimize complete.\n"
        f"• Auto-labeled: {session.auto_labeled}\n"
        f"• Deleted: {session.deleted}\n"
        f"• Skipped: {session.skipped}\n"
        f"• Broken down: {session.broken_down} → {session.created} new task"
        f"{'s' if session.created != 1 else ''}"
    )
    await context.bot.send_message(chat_id, text)


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
        REVIEWING: [
            CallbackQueryHandler(optimize_task_cb, pattern=f"^{_OPT_CB}$"),
            CallbackQueryHandler(skip_cb, pattern=f"^{_SKIP_CB}$"),
            CallbackQueryHandler(delete_cb, pattern=f"^{_DELETE_CB}$"),
            CallbackQueryHandler(override_cb, pattern=f"^{_OVERRIDE_CB}$"),
        ],
        BRAINSTORM_INPUT: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & WHITELIST_FILTER, brainstorm_input_cb
            ),
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
