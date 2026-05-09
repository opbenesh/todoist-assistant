from __future__ import annotations

import asyncio
import logging
import re
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
PROJECT_CONFIRM = 2
BRAINSTORM_REVIEW = 3

_PICK_PREFIX = "opt_pick:"
_CONFIRM_PROJECT_CB = "opt:confirm_proj"
_ACCEPT_CB = "opt:accept"
_REJECT_CB = "opt:reject"

_MAX_LIST = 15
_TODOIST_TASK_URL = "https://todoist.com/app/task/{id}"
_SLUG_RE = re.compile(r"[^a-z0-9-]+")


@dataclass
class UnblockSession:
    tasks: list[dict]
    current_task: dict | None = None
    proposed: list[str] = field(default_factory=list)
    proposal_index: int = 0
    project_slug: str = ""
    project_id: str | None = None
    created: int = 0


_sessions: dict[int, UnblockSession] = {}


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


def _slugify(text: str) -> str:
    """Convert text to a lowercase hyphenated slug."""
    return _SLUG_RE.sub("-", text.lower().strip()).strip("-")


def _number_title(title: str, n: int) -> str:
    """Insert sequential number into a title: '🔧 Research' → '🔧 1 Research'."""
    if " " in title and ord(title[0]) > 127:
        emoji, rest = title.split(" ", 1)
        return f"{emoji} {n} {rest}"
    return f"{n} {title}"


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


async def unblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    await update.message.reply_text("Fetching tasks…")

    all_tasks = await asyncio.to_thread(todoist.get_all_tasks)
    quarantined = [t for t in all_tasks if "quarantined" in (t.get("labels") or [])]
    if not quarantined:
        await update.message.reply_text("No quarantined tasks found.")
        return ConversationHandler.END

    sorted_tasks = quarantined[:_MAX_LIST]
    _sessions[chat_id] = UnblockSession(tasks=sorted_tasks)

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
    proposed, slug = await llm.breakdown_tasks_for_unblock(session.current_task, user_text)

    if not proposed:
        await update.message.reply_text(
            "Couldn't extract tasks from that — try again, or /cancel to exit."
        )
        return BRAINSTORM_INPUT

    session.proposed = proposed
    session.proposal_index = 0
    session.project_slug = slug or _slugify(session.current_task["title"])[:30]

    await _show_project_confirm(chat_id, context)
    return PROJECT_CONFIRM


# ---------------------------------------------------------------------------
# Project confirmation
# ---------------------------------------------------------------------------


async def _show_project_confirm(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session:
        return
    full_name = f"#proj-{session.project_slug}"
    await context.bot.send_message(
        chat_id,
        f"Suggested project: *{full_name}*\n\n"
        "Reply with a different name to change it, or confirm:",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Confirm", callback_data=_CONFIRM_PROJECT_CB),
                ]
            ]
        ),
        parse_mode="Markdown",
    )


async def confirm_project_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END

    await query.edit_message_reply_markup(reply_markup=None)
    return await _create_project_and_start(chat_id, context, session.project_slug)


async def project_name_input_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END

    raw = update.message.text.strip()
    # Strip leading '#proj-' if user pasted it back
    if raw.lower().startswith("#proj-"):
        raw = raw[6:]
    slug = _slugify(raw)[:50] or session.project_slug
    session.project_slug = slug
    return await _create_project_and_start(chat_id, context, slug)


async def _create_project_and_start(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE, slug: str
) -> int:
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END

    full_name = f"#proj-{slug}"
    try:
        project_id = await asyncio.to_thread(todoist.create_todoist_project, full_name)
        session.project_id = project_id
        audit.log(
            "create_project",
            source="unblock/breakdown",
            trigger="user_confirm",
            project_id=project_id,
            name=full_name,
        )
        logger.info("[unblock] created project %s (%s)", full_name, project_id)
    except Exception as exc:
        logger.error("[unblock] failed to create project '%s': %s", full_name, exc)
        await context.bot.send_message(chat_id, f"Failed to create project: {exc}")
        return ConversationHandler.END

    await context.bot.send_message(
        chat_id, f"Project *{full_name}* created.", parse_mode="Markdown"
    )
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

    raw_title = session.proposed[session.proposal_index]
    n = session.created + 1
    title = _number_title(raw_title, n)

    original = session.current_task
    original_title = original["title"]
    task_url = _TODOIST_TASK_URL.format(id=original["id"])
    notes = f"From: [{original_title}]({task_url})"
    if original.get("notes"):
        notes += f"\n\n{original['notes']}"

    task = Task(title=title, notes=notes, priority=original.get("priority", "p4"), labels=[])
    try:
        task_id = await asyncio.to_thread(
            todoist.create_todoist_task, task, project_id=session.project_id
        )
        session.created += 1
        audit.log(
            "create",
            source="unblock/breakdown",
            trigger="user_accept",
            task_id=task_id,
            title=title,
            original_task_id=original["id"],
            original_title=original_title,
            project_id=session.project_id,
        )
        logger.info("[unblock] created breakdown task: %s", title)
        await query.edit_message_text(f"✅ _Created:_ {title}", parse_mode="Markdown")
    except Exception as exc:
        logger.error("[unblock] failed to create task '%s': %s", title, exc)
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
            source="unblock/breakdown_finalize",
            trigger="auto",
            task_id=original_id,
            title=original.get("title", ""),
            note="deleted after user broke it down into subtasks",
        )
        logger.info("[unblock] deleted original task %s after breakdown", original_id)
    except Exception as exc:
        logger.error("[unblock] failed to delete original task %s: %s", original_id, exc)

    created = session.created
    project_name = f"#proj-{session.project_slug}" if session.project_slug else "project"
    _sessions.pop(chat_id, None)
    await context.bot.send_message(
        chat_id,
        f"Done — created {created} task{'s' if created != 1 else ''} in *{project_name}*, "
        f"original deleted.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text("Unblock cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Handler export
# ---------------------------------------------------------------------------


unblock_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("unblock", unblock_cmd, WHITELIST_FILTER)],
    states={
        PICKING: [
            CallbackQueryHandler(pick_cb, pattern=f"^{_PICK_PREFIX}"),
        ],
        BRAINSTORM_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND & WHITELIST_FILTER, brainstorm_input_cb),
        ],
        PROJECT_CONFIRM: [
            CallbackQueryHandler(confirm_project_cb, pattern=f"^{_CONFIRM_PROJECT_CB}$"),
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & WHITELIST_FILTER, project_name_input_cb
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
    name="unblock",
)
