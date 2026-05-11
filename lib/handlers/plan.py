from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
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
import lib.obsidian as obsidian
import lib.todoist as todoist
from lib.handlers.auth import WHITELIST_FILTER
from lib.models import PRIORITY_TO_TODOIST, Task, store
from lib.scheduler import _settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

BRAINSTORM_PROMPT = 0
BS_INPUT = 1
BS_REVIEW = 2

TRIAGING = 3
RESUME_CONFIRM = 4
TRIAGE_TIMESLOT = 5

UNBLOCK_PROMPT = 6
UNBLOCK_PICKING = 7
UNBLOCK_ACTION = 8
UNBLOCK_BRAINSTORM = 9
UNBLOCK_PRJ_CONFIRM = 10
UNBLOCK_REVIEW = 11

# ---------------------------------------------------------------------------
# Callback data constants
# ---------------------------------------------------------------------------

_BS_START = "pf:bs_start"
_BS_SKIP = "pf:bs_skip"
_BS_ACCEPT = "pf:bs_accept"
_BS_REJECT = "pf:bs_reject"
_BS_CONTINUE = "pf:bs_continue"
_BS_NEXT = "pf:bs_next"

_RESTART = "pf:restart"

_UB_UNBLOCK = "pf:ub_unblock"
_UB_SKIP = "pf:ub_skip"
_UB_PICK_PREFIX = "pf:ub_pick:"
_UB_BREAKDOWN = "pf:ub_breakdown"
_UB_DONE = "pf:ub_done"
_UB_CONFIRM_PROJ = "pf:ub_confirm_proj"
_UB_ACCEPT = "pf:ub_accept"
_UB_REJECT = "pf:ub_reject"
_UB_ANOTHER = "pf:ub_another"
_UB_CONTINUE = "pf:ub_continue"

_UB_MAX_LIST = 15
_UB_SLUG_RE = re.compile(r"[^a-z0-9-]+")

_TODOIST_TASK_URL = "https://todoist.com/app/task/{id}"

MAX_TRIAGE_AGE = todoist.MAX_TRIAGE_AGE

# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class PlanFlowSession:
    # brainstorm
    bs_proposed: list[str] = field(default_factory=list)
    bs_index: int = 0
    bs_created: int = 0

    # triage
    triage_tasks: list[dict] = field(default_factory=list)
    triage_index: int = 0
    triage_pending_priority: str | None = None  # set while awaiting timeslot pick
    triage_processed: list[tuple[str, str, list[str]]] = field(default_factory=list)
    # each entry: (task_id, action, labels_at_triage_time)
    projects: dict[str, str] = field(default_factory=dict)  # project_id -> name (ephemeral)
    project_incomplete_counts: dict[str, int] = field(default_factory=dict)  # ephemeral

    # resumption metadata
    last_user_action_ts: float = field(default_factory=_time.time)
    nudge_sent: bool = False

    # unblock sub-session (ephemeral — not persisted)
    ub_tasks: list = field(default_factory=list)
    ub_task: dict | None = None
    ub_proposals: list[str] = field(default_factory=list)
    ub_proposal_index: int = 0
    ub_project_slug: str | None = None
    ub_project_id: str | None = None
    ub_created: int = 0

    def to_dict(self) -> dict:
        return {
            "bs_proposed": self.bs_proposed,
            "bs_index": self.bs_index,
            "bs_created": self.bs_created,
            "triage_tasks": self.triage_tasks,
            "triage_index": self.triage_index,
            "triage_pending_priority": self.triage_pending_priority,
            "triage_processed": [list(t) for t in self.triage_processed],
            "last_user_action_ts": self.last_user_action_ts,
            "nudge_sent": self.nudge_sent,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlanFlowSession":
        s = cls()
        s.bs_proposed = d.get("bs_proposed", [])
        s.bs_index = d.get("bs_index", 0)
        s.bs_created = d.get("bs_created", 0)
        s.triage_tasks = d.get("triage_tasks", [])
        s.triage_index = d.get("triage_index", 0)
        s.triage_pending_priority = d.get("triage_pending_priority")
        s.triage_processed = [tuple(t) for t in d.get("triage_processed", [])]
        s.last_user_action_ts = d.get("last_user_action_ts", 0.0)
        s.nudge_sent = d.get("nudge_sent", False)
        return s


_sessions: dict[int, PlanFlowSession] = {}


def has_active_plan_session(chat_id: int) -> bool:
    """Return True if there is an in-memory plan session for this chat."""
    return chat_id in _sessions


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def _checkpoint(chat_id: int, phase: int) -> None:
    """Save current session state to persistent storage."""
    session = _sessions.get(chat_id)
    if session:
        session.last_user_action_ts = _time.time()
        d = session.to_dict()
        logger.info(
            "[plan/checkpoint] phase=%d triage_tasks=%d triage_index=%d",
            phase,
            len(d.get("triage_tasks", [])),
            d.get("triage_index", 0),
        )
        store.save_plan_session(phase, d)


def _phase_label(phase: int, session: PlanFlowSession) -> str:
    if phase == BRAINSTORM_PROMPT:
        return "brainstorm"
    if phase == BS_INPUT:
        return "brainstorm entry"
    if phase == BS_REVIEW:
        total = len(session.bs_proposed)
        if total:
            idx = min(session.bs_index + 1, total)
            return f"brainstorm review ({idx}/{total})"
        return "brainstorm review"
    if phase == TRIAGING:
        total = len(session.triage_tasks)
        return f"triage ({session.triage_index + 1}/{total})" if total else "triage"
    if phase == TRIAGE_TIMESLOT:
        total = len(session.triage_tasks)
        return (
            f"triage timeslot ({session.triage_index + 1}/{total})" if total else "triage timeslot"
        )
    if phase in (
        UNBLOCK_PROMPT,
        UNBLOCK_PICKING,
        UNBLOCK_ACTION,
        UNBLOCK_BRAINSTORM,
        UNBLOCK_PRJ_CONFIRM,
        UNBLOCK_REVIEW,
    ):
        return "unblock"
    return "planning"


async def _show_resume_header(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE, phase: int, session: PlanFlowSession
) -> None:
    label = _phase_label(phase, session)
    await context.bot.send_message(
        chat_id,
        f"↩ Resuming your planning session — *{label}*",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔄 Start fresh", callback_data=_RESTART),
                ]
            ]
        ),
        parse_mode="Markdown",
    )


async def _show_phase_ui(chat_id: int, context: ContextTypes.DEFAULT_TYPE, phase: int) -> None:
    """Re-display the UI for the given conversation phase."""
    if phase == BRAINSTORM_PROMPT:
        await _show_brainstorm_prompt(chat_id, context)
    elif phase == BS_INPUT:
        await context.bot.send_message(chat_id, "What's on your mind? Type freely:")
    elif phase == BS_REVIEW:
        await _show_bs_proposal(chat_id, context)
    elif phase == TRIAGING:
        session = _sessions.get(chat_id)
        if session and not session.triage_tasks:
            await _start_triage(chat_id, context)  # re-fetch if tasks weren't checkpointed
        else:
            await _show_triage_task(chat_id, context)
    elif phase == TRIAGE_TIMESLOT:
        session = _sessions.get(chat_id)
        if session:
            task = session.triage_tasks[session.triage_index]
            valid_slots = _valid_timeslots()
            if valid_slots:
                prio = session.triage_pending_priority or "P?"
                await context.bot.send_message(
                    chat_id,
                    f"*{task['title']}*\n_Priority: {prio} — when should this run?_",
                    reply_markup=_timeslot_keyboard(valid_slots),
                    parse_mode="Markdown",
                )
            else:
                # All timeslots have passed — fall back to advancing triage
                await _advance_triage(chat_id, context, session)


async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    if await asyncio.to_thread(obsidian.is_day_planned):
        await update.message.reply_text("Today is already planned.")
        return ConversationHandler.END

    persisted = store.load_plan_session()
    if persisted:
        phase, data = persisted
        session = PlanFlowSession.from_dict(data)
        tz = ZoneInfo(_settings.timezone) if _settings.timezone else ZoneInfo("UTC")
        session_date = datetime.fromtimestamp(session.last_user_action_ts, tz=tz).date()
        if session_date < date.today():
            logger.info(
                "[plan] discarding stale session from %s for chat %s", session_date, chat_id
            )
            store.clear_plan_session()
            persisted = None
        else:
            session.last_user_action_ts = _time.time()
            _sessions[chat_id] = session
            logger.info("[plan] resuming session at phase=%s for chat %s", phase, chat_id)
            await _show_resume_header(chat_id, context, phase, session)
            await _show_phase_ui(chat_id, context, phase)
            return phase

    logger.info("[plan] session started for chat %s", chat_id)
    session = PlanFlowSession()
    _sessions[chat_id] = session
    _checkpoint(chat_id, BRAINSTORM_PROMPT)  # persist immediately so nag suppression takes effect
    await update.message.reply_text("Starting your planning session.")
    await _show_brainstorm_prompt(chat_id, context)
    return BRAINSTORM_PROMPT


# ---------------------------------------------------------------------------
# Phase 1: Brainstorm (optional)
# ---------------------------------------------------------------------------


async def _show_brainstorm_prompt(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(
        chat_id,
        "🧠 *Brainstorm* — capture anything on your mind before planning.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("▶ Start", callback_data=_BS_START),
                    InlineKeyboardButton("⏭ Skip", callback_data=_BS_SKIP),
                ]
            ]
        ),
        parse_mode="Markdown",
    )


async def _transition_after_brainstorm(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    """After brainstorm, move to unblock if quarantined tasks exist, otherwise triage."""
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END
    if not session.ub_tasks:
        quarantined = await asyncio.to_thread(todoist.get_quarantined_tasks)
        session.ub_tasks = quarantined[:_UB_MAX_LIST]
    if session.ub_tasks:
        await _show_unblock_prompt(chat_id, context)
        return UNBLOCK_PROMPT
    _checkpoint(chat_id, TRIAGING)
    await _start_triage(chat_id, context)
    return TRIAGING


async def restart_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Reset to a fresh session, carrying over already-triaged IDs to prevent re-triaging."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)

    store.clear_plan_session()
    session = PlanFlowSession()
    _sessions[chat_id] = session
    store.save_plan_session(BRAINSTORM_PROMPT, session.to_dict())
    logger.info("[plan] restarted fresh for chat %s", chat_id)
    await context.bot.send_message(chat_id, "Starting fresh.")
    await _show_brainstorm_prompt(chat_id, context)
    return BRAINSTORM_PROMPT


async def bs_prompt_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)
    if query.data == _BS_START:
        logger.info("[plan] phase=brainstorm chat=%s", chat_id)
        await context.bot.send_message(chat_id, "What's on your mind? Type freely:")
        _checkpoint(chat_id, BS_INPUT)  # first save — user has engaged
        return BS_INPUT
    logger.info("[plan] phase=brainstorm skipped chat=%s", chat_id)
    return await _transition_after_brainstorm(chat_id, context)


async def bs_input_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END

    await update.message.reply_text("Extracting tasks…")
    try:
        proposed = await llm.brainstorm_extract_tasks(update.message.text.strip())
    except Exception as exc:
        logger.error("[plan/bs] LLM extraction failed: %s", exc)
        await update.message.reply_text(
            "Couldn't reach AI — please try again.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⏭ Skip", callback_data=_BS_SKIP)]]
            ),
        )
        return BS_INPUT
    logger.info("[plan/bs] extracted %d tasks", len(proposed))
    if not proposed:
        await update.message.reply_text(
            "Couldn't find tasks in that — try again or tap Skip.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("⏭ Skip", callback_data=_BS_SKIP),
                    ]
                ]
            ),
        )
        return BS_INPUT

    session.bs_proposed = proposed
    session.bs_index = 0
    await _show_bs_proposal(chat_id, context)
    return BS_REVIEW


def _bs_proposal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Accept", callback_data=_BS_ACCEPT),
                InlineKeyboardButton("❌ Reject", callback_data=_BS_REJECT),
            ]
        ]
    )


def _bs_wrapup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ More", callback_data=_BS_CONTINUE),
                InlineKeyboardButton("▶ Next step", callback_data=_BS_NEXT),
            ]
        ]
    )


async def _show_bs_proposal(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session:
        return
    if session.bs_index >= len(session.bs_proposed):
        n = session.bs_created
        await context.bot.send_message(
            chat_id,
            f"Created {n} task{'s' if n != 1 else ''}. Continue brainstorming or move on?",
            reply_markup=_bs_wrapup_keyboard(),
        )
        return
    title = session.bs_proposed[session.bs_index]
    idx = session.bs_index + 1
    total = len(session.bs_proposed)
    await context.bot.send_message(
        chat_id,
        f"*Task {idx} of {total}*\n\n{title}",
        reply_markup=_bs_proposal_keyboard(),
        parse_mode="Markdown",
    )


async def bs_accept_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or session.bs_index >= len(session.bs_proposed):
        return ConversationHandler.END

    title = session.bs_proposed[session.bs_index]
    try:
        task_id = await asyncio.to_thread(
            todoist.create_todoist_task, Task(title=title, due_date=date.today())
        )
        session.bs_created += 1
        audit.log(
            "create", source="plan/brainstorm", trigger="user_accept", task_id=task_id, title=title
        )
        await query.edit_message_text(f"✅ _Created:_ {title}", parse_mode="Markdown")
    except Exception as exc:
        logger.error("[plan/bs] failed to create '%s': %s", title, exc)
        await query.answer("Failed to create task.", show_alert=True)

    session.bs_index += 1
    _checkpoint(chat_id, BS_REVIEW)
    await _show_bs_proposal(chat_id, context)
    return BS_REVIEW


async def bs_reject_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or session.bs_index >= len(session.bs_proposed):
        return ConversationHandler.END

    title = session.bs_proposed[session.bs_index]
    await query.edit_message_text(f"❌ _Rejected:_ {title}", parse_mode="Markdown")
    logger.info("[plan/bs] rejected %r (index %d)", title, session.bs_index)
    session.bs_index += 1
    _checkpoint(chat_id, BS_REVIEW)
    await _show_bs_proposal(chat_id, context)
    return BS_REVIEW


async def bs_continue_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)
    _checkpoint(chat_id, BS_INPUT)
    await context.bot.send_message(chat_id, "Keep going — what else?")
    return BS_INPUT


async def bs_next_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)
    return await _transition_after_brainstorm(chat_id, context)


# ---------------------------------------------------------------------------
# Phase 0: Unblock (optional, before brainstorm)
# ---------------------------------------------------------------------------


def _ub_slugify(text: str) -> str:
    return _UB_SLUG_RE.sub("-", text.lower().strip()).strip("-")


def _ub_number_title(title: str, n: int) -> str:
    if " " in title and ord(title[0]) > 127:
        emoji, rest = title.split(" ", 1)
        return f"{emoji} {n} {rest}"
    return f"{n} {title}"


async def _show_unblock_prompt(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session or not session.ub_tasks:
        return
    tasks = session.ub_tasks
    lines = [f"☣️ {len(tasks)} quarantined task{'s' if len(tasks) != 1 else ''}:\n"]
    for i, t in enumerate(tasks, 1):
        days = todoist.days_since_quarantined(t["id"])
        day_str = f"  ({days}d)" if days > 0 else ""
        lines.append(f"{i}. {t['title']}{day_str}")
    await context.bot.send_message(
        chat_id,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("☣️ Unblock", callback_data=_UB_UNBLOCK),
                    InlineKeyboardButton("⏭ Skip", callback_data=_UB_SKIP),
                ]
            ]
        ),
    )


async def _ub_unblock_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or not session.ub_tasks:
        return ConversationHandler.END

    await query.edit_message_reply_markup(reply_markup=None)
    buttons = []
    for t in session.ub_tasks:
        label = t["title"]
        if len(label) > 64:
            label = label[:61] + "…"
        buttons.append([InlineKeyboardButton(label, callback_data=f"{_UB_PICK_PREFIX}{t['id']}")])
    await context.bot.send_message(
        chat_id,
        "Pick a task to break down:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return UNBLOCK_PICKING


async def _ub_skip_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)
    _checkpoint(chat_id, TRIAGING)
    await _start_triage(chat_id, context)
    return TRIAGING


async def _ub_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END

    task_id = query.data[len(_UB_PICK_PREFIX) :]
    task = next((t for t in session.ub_tasks if t["id"] == task_id), None)
    if not task:
        await query.answer("Task not found.", show_alert=True)
        return UNBLOCK_PICKING

    session.ub_task = task
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        chat_id,
        f"*{task['title']}*\n\nWhat would you like to do?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔨 Break down", callback_data=_UB_BREAKDOWN),
                    InlineKeyboardButton("✅ Mark done", callback_data=_UB_DONE),
                ]
            ]
        ),
        parse_mode="Markdown",
    )
    return UNBLOCK_ACTION


async def _ub_breakdown_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or not session.ub_task:
        return ConversationHandler.END

    task = session.ub_task
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        chat_id,
        f"What's your plan for *{task['title']}*?\n\nDescribe freely — I'll turn it into tasks:",
        parse_mode="Markdown",
    )
    return UNBLOCK_BRAINSTORM


async def _ub_done_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or not session.ub_task:
        return ConversationHandler.END

    task = session.ub_task
    task_id = task["id"]
    labels = task.get("labels") or []
    try:
        await asyncio.gather(
            asyncio.to_thread(todoist.strip_age_labels, task_id, labels),
            asyncio.to_thread(todoist.complete_todoist_task, task_id),
        )
        audit.log(
            "complete",
            source="plan/unblock",
            trigger="user_complete",
            task_id=task_id,
            title=task.get("title", ""),
        )
        logger.info("[plan/unblock] completed task %s directly", task_id)
        await query.edit_message_text(f"✅ _Completed:_ {task['title']}", parse_mode="Markdown")
    except Exception as exc:
        logger.error("[plan/unblock] failed to complete task %s: %s", task_id, exc)
        await query.answer("Failed to complete task.", show_alert=True)
        return UNBLOCK_ACTION

    done_id = task_id
    session.ub_task = None
    session.ub_tasks = [t for t in session.ub_tasks if t["id"] != done_id]
    await context.bot.send_message(
        chat_id,
        "Want to unblock another?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("☣️ Unblock another", callback_data=_UB_ANOTHER),
                    InlineKeyboardButton("▶ Continue to triage", callback_data=_UB_CONTINUE),
                ]
            ]
        ),
    )
    return UNBLOCK_PROMPT


async def _ub_brainstorm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = _sessions.get(chat_id)
    if not session or not session.ub_task:
        return ConversationHandler.END

    user_text = update.message.text.strip()
    try:
        proposed, slug = await llm.breakdown_tasks_for_unblock(session.ub_task, user_text)
    except Exception as exc:
        logger.error("[plan/unblock] LLM breakdown failed: %s", exc)
        await update.message.reply_text("Couldn't reach AI — try again, or /cancel to exit.")
        return UNBLOCK_BRAINSTORM

    if not proposed:
        await update.message.reply_text(
            "Couldn't extract tasks from that — try again, or /cancel to exit."
        )
        return UNBLOCK_BRAINSTORM

    session.ub_proposals = proposed
    session.ub_proposal_index = 0
    session.ub_project_slug = slug or _ub_slugify(session.ub_task["title"])[:30]
    await _ub_show_project_confirm(chat_id, context)
    return UNBLOCK_PRJ_CONFIRM


async def _ub_show_project_confirm(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session:
        return
    full_name = f"#proj-{session.ub_project_slug}"
    await context.bot.send_message(
        chat_id,
        f"Suggested project: *{full_name}*\n\n"
        "Reply with a different name to change it, or confirm:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Confirm", callback_data=_UB_CONFIRM_PROJ)]]
        ),
        parse_mode="Markdown",
    )


async def _ub_confirm_proj_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END
    await query.edit_message_reply_markup(reply_markup=None)
    return await _ub_create_project_and_start(chat_id, context, session.ub_project_slug or "")


async def _ub_proj_name_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END
    raw = update.message.text.strip()
    if raw.lower().startswith("#proj-"):
        raw = raw[6:]
    session.ub_project_slug = _ub_slugify(raw)[:50] or session.ub_project_slug or "task"
    return await _ub_create_project_and_start(chat_id, context, session.ub_project_slug)


async def _ub_create_project_and_start(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE, slug: str
) -> int:
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END
    full_name = f"#proj-{slug}"
    try:
        project_id = await asyncio.to_thread(todoist.create_todoist_project, full_name)
        session.ub_project_id = project_id
        audit.log(
            "create_project",
            source="plan/unblock",
            trigger="user_confirm",
            project_id=project_id,
            name=full_name,
        )
        logger.info("[plan/unblock] created project %s (%s)", full_name, project_id)
    except Exception as exc:
        logger.error("[plan/unblock] failed to create project '%s': %s", full_name, exc)
        await context.bot.send_message(chat_id, f"Failed to create project: {exc}")
        return ConversationHandler.END
    await context.bot.send_message(
        chat_id, f"Project *{full_name}* created.", parse_mode="Markdown"
    )
    await _ub_show_proposal(chat_id, context)
    return UNBLOCK_REVIEW


def _ub_proposal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Accept", callback_data=_UB_ACCEPT),
                InlineKeyboardButton("❌ Reject", callback_data=_UB_REJECT),
            ]
        ]
    )


async def _ub_show_proposal(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session:
        return
    if session.ub_proposal_index >= len(session.ub_proposals):
        await _ub_finalize(chat_id, context)
        return
    title = session.ub_proposals[session.ub_proposal_index]
    idx = session.ub_proposal_index + 1
    total = len(session.ub_proposals)
    await context.bot.send_message(
        chat_id,
        f"*Proposal {idx} of {total}*\n\n{title}",
        reply_markup=_ub_proposal_keyboard(),
        parse_mode="Markdown",
    )


async def _ub_accept_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or session.ub_proposal_index >= len(session.ub_proposals):
        return ConversationHandler.END

    raw_title = session.ub_proposals[session.ub_proposal_index]
    n = session.ub_created + 1
    title = _ub_number_title(raw_title, n)

    original = session.ub_task
    original_title = original["title"]
    task_url = _TODOIST_TASK_URL.format(id=original["id"])
    notes = f"From: [{original_title}]({task_url})"
    if original.get("notes"):
        notes += f"\n\n{original['notes']}"

    task = Task(title=title, notes=notes, priority=original.get("priority", "p4"), labels=[])
    try:
        task_id = await asyncio.to_thread(
            todoist.create_todoist_task, task, project_id=session.ub_project_id
        )
        session.ub_created += 1
        audit.log(
            "create",
            source="plan/unblock",
            trigger="user_accept",
            task_id=task_id,
            title=title,
            original_task_id=original["id"],
            project_id=session.ub_project_id,
        )
        await query.edit_message_text(f"✅ _Created:_ {title}", parse_mode="Markdown")
    except Exception as exc:
        logger.error("[plan/unblock] failed to create task '%s': %s", title, exc)
        await query.answer("Failed to create task.", show_alert=True)

    session.ub_proposal_index += 1
    is_last = session.ub_proposal_index >= len(session.ub_proposals)
    await _ub_show_proposal(chat_id, context)
    return UNBLOCK_PROMPT if is_last else UNBLOCK_REVIEW


async def _ub_reject_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or session.ub_proposal_index >= len(session.ub_proposals):
        return ConversationHandler.END

    title = session.ub_proposals[session.ub_proposal_index]
    await query.edit_message_text(f"❌ _Rejected:_ {title}", parse_mode="Markdown")
    session.ub_proposal_index += 1
    is_last = session.ub_proposal_index >= len(session.ub_proposals)
    await _ub_show_proposal(chat_id, context)
    return UNBLOCK_PROMPT if is_last else UNBLOCK_REVIEW


async def _ub_finalize(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session or not session.ub_task:
        return

    original = session.ub_task
    original_id = original["id"]
    try:
        await asyncio.to_thread(todoist.delete_todoist_task, original_id)
        audit.log(
            "delete",
            source="plan/unblock",
            trigger="auto",
            task_id=original_id,
            title=original.get("title", ""),
            note="deleted after breakdown in plan session",
        )
        logger.info("[plan/unblock] deleted original task %s", original_id)
    except Exception as exc:
        logger.error("[plan/unblock] failed to delete original task %s: %s", original_id, exc)

    created = session.ub_created
    project_name = f"#proj-{session.ub_project_slug}" if session.ub_project_slug else "project"
    await context.bot.send_message(
        chat_id,
        f"✅ Broke down *{original['title']}* into {created} task{'s' if created != 1 else ''} "
        f"in *{project_name}*, original deleted.",
        parse_mode="Markdown",
    )
    # Reset sub-session fields for a potential next unblock
    done_id = original_id
    session.ub_task = None
    session.ub_proposals = []
    session.ub_proposal_index = 0
    session.ub_project_slug = None
    session.ub_project_id = None
    session.ub_created = 0
    session.ub_tasks = [t for t in session.ub_tasks if t["id"] != done_id]

    await context.bot.send_message(
        chat_id,
        "Want to unblock another?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("☣️ Unblock another", callback_data=_UB_ANOTHER),
                    InlineKeyboardButton("▶ Continue to triage", callback_data=_UB_CONTINUE),
                ]
            ]
        ),
    )


async def _ub_another_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END
    await query.edit_message_reply_markup(reply_markup=None)

    if not session.ub_tasks:
        await context.bot.send_message(chat_id, "✅ No more quarantined tasks.")
        _checkpoint(chat_id, TRIAGING)
        await _start_triage(chat_id, context)
        return TRIAGING

    await _show_unblock_prompt(chat_id, context)
    return UNBLOCK_PROMPT


async def _ub_continue_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)
    _checkpoint(chat_id, TRIAGING)
    await _start_triage(chat_id, context)
    return TRIAGING


# ---------------------------------------------------------------------------
# Phase 2: Triage (mandatory)
# ---------------------------------------------------------------------------


async def _start_triage(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("[plan] phase=triage chat=%s", chat_id)
    try:
        (tasks, (projects, inbox_id)) = await asyncio.gather(
            asyncio.to_thread(todoist.get_triage_tasks),
            asyncio.to_thread(todoist.get_projects_info),
        )
    except Exception as exc:
        logger.error("[plan/triage] Todoist fetch failed: %s", exc)
        await context.bot.send_message(
            chat_id,
            "Couldn't reach Todoist — please try /plan again in a moment.",
        )
        return
    logger.info("[plan/triage] fetched %d tasks", len(tasks))

    # For each project in the triage set, fetch all incomplete tasks to determine
    # which step is first and how many remain. Inbox tasks are treated as
    # independent (no project), so exclude inbox_id from grouping.
    project_ids = {
        t["project_id"] for t in tasks if t.get("project_id") and t["project_id"] != inbox_id
    }
    if project_ids:
        try:
            ptask_lists = await asyncio.gather(
                *[asyncio.to_thread(todoist.get_tasks_by_project, pid) for pid in project_ids]
            )
        except Exception as exc:
            logger.error("[plan/triage] project task fetch failed: %s", exc)
            await context.bot.send_message(
                chat_id,
                "Couldn't reach Todoist — please try /plan again in a moment.",
            )
            return
        project_tasks = dict(zip(project_ids, ptask_lists))
        first_ids = {pid: ptasks[0]["id"] for pid, ptasks in project_tasks.items() if ptasks}
        project_counts = {pid: len(ptasks) for pid, ptasks in project_tasks.items()}
        _n = len(tasks)
        tasks = [
            t
            for t in tasks
            if not t.get("project_id")
            or t["project_id"] == inbox_id
            or t["id"] == first_ids.get(t["project_id"], t["id"])
        ]
        logger.info(
            "[plan/triage] project filter: %d→%d tasks (%d non-first-step excluded)",
            _n,
            len(tasks),
            _n - len(tasks),
        )
    else:
        project_counts = {}

    session = _sessions.get(chat_id)
    if not session:
        return
    session.projects = projects
    session.project_incomplete_counts = project_counts
    already_done = {tid for tid, _, _ in session.triage_processed}
    if already_done:
        _n = len(tasks)
        tasks = [t for t in tasks if t["id"] not in already_done]
        logger.info("[plan/triage] already-done filter: %d→%d tasks", _n, len(tasks))
    if not tasks:
        logger.info("[plan/triage] no tasks remain — sending summary")
        await context.bot.send_message(chat_id, "No tasks to triage.")
        try:
            tasks_today, (projects, inbox_id) = await asyncio.gather(
                asyncio.to_thread(todoist.get_today_tasks),
                asyncio.to_thread(todoist.get_projects_info),
            )
        except Exception as exc:
            logger.error("[plan/triage] fetch failed: %s", exc)
            await context.bot.send_message(
                chat_id,
                "Couldn't reach Todoist — please try /plan again in a moment.",
            )
            return
        lines = obsidian.build_tasks_section(
            tasks_today,
            projects,
            inbox_id,
            _settings.morning_block,
            _settings.afternoon_block,
            _settings.evening_block,
        )
        await asyncio.to_thread(obsidian.write_tasks_section, lines)
        store.clear_plan_session()
        _sessions.pop(chat_id, None)
        await context.bot.send_message(
            chat_id,
            _format_plan_summary(lines, len(tasks_today), 0, 0),
            parse_mode="Markdown",
        )
        return
    session.triage_tasks = tasks
    session.triage_index = 0
    _checkpoint(chat_id, TRIAGING)  # persist tasks before showing first one
    logger.info("[plan/triage] presenting %d tasks: %s", len(tasks), [t["title"] for t in tasks])
    total = len(tasks)
    await context.bot.send_message(
        chat_id,
        f"📋 *Triage* — {total} task{'s' if total != 1 else ''} to review.",
        parse_mode="Markdown",
    )
    await _show_triage_task(chat_id, context)


def _block_midpoint(block: str) -> int:
    """Return the midpoint hour (rounded down) of a 'HH:MM-HH:MM' block string."""
    start, end = block.split("-")
    sh = int(start.split(":")[0])
    eh = int(end.split(":")[0])
    return (sh + eh) // 2


def _timeslots() -> list[tuple[str, str, int]]:
    return [
        ("🌅 Morning", "plan_timeslot:morning", _block_midpoint(_settings.morning_block)),
        ("☀️ Afternoon", "plan_timeslot:afternoon", _block_midpoint(_settings.afternoon_block)),
        ("🌙 Evening", "plan_timeslot:evening", _block_midpoint(_settings.evening_block)),
    ]


def _valid_timeslots() -> list[tuple[str, str, int]]:
    """Return timeslots not yet within 30 minutes of passing."""
    tz = ZoneInfo(_settings.timezone)
    now = datetime.now(tz)
    cutoff = now.hour * 60 + now.minute + 30
    return [(label, cb, hour) for label, cb, hour in _timeslots() if hour * 60 >= cutoff]


def _timeslot_keyboard(slots: list[tuple[str, str, int]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=cb) for label, cb, _ in slots]]
    )


def _triage_keyboard(task_age: int = 0) -> InlineKeyboardMarkup:
    priority_row = [
        InlineKeyboardButton("P1 🔴", callback_data="plan_triage:p1"),
        InlineKeyboardButton("P2 🟠", callback_data="plan_triage:p2"),
        InlineKeyboardButton("P3 🟡", callback_data="plan_triage:p3"),
    ]
    if task_age >= MAX_TRIAGE_AGE:
        return InlineKeyboardMarkup(
            [
                priority_row,
                [
                    InlineKeyboardButton("✅ Done", callback_data="plan_triage:complete"),
                    InlineKeyboardButton("🚫 Quarantine", callback_data="plan_triage:quarantine"),
                    InlineKeyboardButton("🗑 Delete", callback_data="plan_triage:delete"),
                ],
                [
                    InlineKeyboardButton(
                        f"⚠️ Postpone anyway (age {task_age})",
                        callback_data="plan_triage:postpone",
                    ),
                ],
            ]
        )
    if task_age >= 1:
        return InlineKeyboardMarkup(
            [
                priority_row,
                [
                    InlineKeyboardButton("✅ Done", callback_data="plan_triage:complete"),
                    InlineKeyboardButton("⏸ Postpone", callback_data="plan_triage:postpone"),
                    InlineKeyboardButton("🚫 Quarantine", callback_data="plan_triage:quarantine"),
                    InlineKeyboardButton("🗑 Delete", callback_data="plan_triage:delete"),
                ],
            ]
        )
    return InlineKeyboardMarkup(
        [
            priority_row,
            [
                InlineKeyboardButton("✅ Done", callback_data="plan_triage:complete"),
                InlineKeyboardButton("⏸ Postpone", callback_data="plan_triage:postpone"),
                InlineKeyboardButton("🗑 Delete", callback_data="plan_triage:delete"),
            ],
        ]
    )


async def _show_triage_task(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session:
        return

    task = session.triage_tasks[session.triage_index]
    idx = session.triage_index + 1
    total = len(session.triage_tasks)
    priority = task.get("priority", "p4").upper()
    duration = task.get("duration_minutes")
    is_recurring = task.get("is_recurring", False)
    age = 0 if is_recurring else todoist.get_task_age(task.get("labels") or [])

    pid = task.get("project_id") or ""
    project_name = session.projects.get(pid, "")
    project_part = f" · 📁 {project_name}" if project_name else ""
    step_count = session.project_incomplete_counts.get(pid, 1)
    step_part = f" · step 1 of {step_count}" if step_count > 1 else ""
    age_part = f" · age {age}" if age > 0 else ""

    meta = priority
    if is_recurring:
        rule = task.get("due_string") or "recurring"
        meta += f" · {rule} · 🔁"
    if duration:
        meta += f" · {duration}min"
    meta += project_part + step_part + age_part

    await context.bot.send_message(
        chat_id,
        f"*Task {idx} of {total}*\n\n{task['title']}\n_{meta}_",
        reply_markup=_triage_keyboard(age),
        parse_mode="Markdown",
    )


async def _advance_triage(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    session: PlanFlowSession,
) -> int:
    session.triage_index += 1
    if session.triage_index >= len(session.triage_tasks):
        await _finish_triage(chat_id, context)
        return ConversationHandler.END
    await _show_triage_task(chat_id, context)
    return TRIAGING


async def _handle_triage_priority(
    chat_id: int,
    query: CallbackQuery,
    session: PlanFlowSession,
    task_id: str,
    title: str,
    action: str,
) -> int | None:
    valid_slots = _valid_timeslots()
    if valid_slots:
        session.triage_pending_priority = action
        await query.edit_message_text(
            f"*{title}*\n_Priority: {action.upper()} — when should this run?_",
            reply_markup=_timeslot_keyboard(valid_slots),
            parse_mode="Markdown",
        )
        _checkpoint(chat_id, TRIAGE_TIMESLOT)
        return TRIAGE_TIMESLOT
    # No slots left today — schedule without a time
    await asyncio.to_thread(
        todoist.update_todoist_task,
        task_id,
        priority=PRIORITY_TO_TODOIST[action],
        due_string="today",
    )
    audit.log(
        "update",
        source="plan/triage",
        trigger=f"user_set_{action}",
        task_id=task_id,
        title=title,
        changes={"priority": action, "due": "today"},
    )
    logger.info("[plan/triage] set %s → %s (no timeslot available)", task_id, action)
    return None


async def _handle_triage_complete(task: dict, task_id: str, title: str, labels: list[str]) -> None:
    project_id = task.get("project_id")
    await asyncio.gather(
        asyncio.to_thread(todoist.strip_age_labels, task_id, labels),
        asyncio.to_thread(todoist.complete_todoist_task, task_id),
    )
    _, inbox_id = await asyncio.to_thread(todoist.get_projects_info)
    if project_id and project_id != inbox_id:
        await asyncio.to_thread(todoist.reset_next_project_task_age, project_id)
    audit.log(
        "complete",
        source="plan/triage",
        trigger="user_complete",
        task_id=task_id,
        title=title,
    )
    logger.info("[plan/triage] completed %s", task_id)


async def _handle_triage_postpone(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    task_id: str,
    title: str,
    is_recurring: bool,
    labels: list[str],
) -> None:
    if is_recurring:
        # Removing the due date would destroy the recurrence pattern.
        # Complete this occurrence instead so Todoist advances the schedule.
        await asyncio.to_thread(todoist.complete_todoist_task, task_id)
        audit.log(
            "complete",
            source="plan/triage",
            trigger="user_postpone_recurring",
            task_id=task_id,
            title=title,
        )
        logger.info("[plan/triage] recurring postpone → completed %s", task_id)
    else:
        age = todoist.get_task_age(labels)
        await asyncio.to_thread(todoist.remove_task_due_date, task_id)
        audit.log(
            "remove_due_date",
            source="plan/triage",
            trigger="user_postpone",
            task_id=task_id,
            title=title,
        )
        logger.info("[plan/triage] postponed %s", task_id)
        if age >= MAX_TRIAGE_AGE:
            await context.bot.send_message(
                chat_id,
                f"⚠️ _{title}_ has been postponed {age} times.",
                parse_mode="Markdown",
            )


async def _handle_triage_quarantine(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE, task_id: str, title: str, labels: list[str]
) -> None:
    await asyncio.to_thread(todoist.quarantine_task, task_id, labels)
    audit.log(
        "update",
        source="plan/triage",
        trigger="user_quarantine",
        task_id=task_id,
        title=title,
        changes={"labels": "+quarantined"},
    )
    logger.info("[plan/triage] quarantined %s", task_id)
    await context.bot.send_message(
        chat_id,
        f"🚫 _{title}_ quarantined — hidden from future planning.",
        parse_mode="Markdown",
    )


async def _handle_triage_delete(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    task_id: str,
    title: str,
    is_recurring: bool,
    labels: list[str],
) -> bool:
    if is_recurring:
        # Deleting a recurring task removes all future occurrences permanently.
        # Block and let the user choose a different action.
        await context.bot.send_message(
            chat_id,
            "⚠️ Recurring task — deleting removes all future occurrences. "
            "Use ✅ Done to skip this occurrence, or delete from Todoist directly.",
        )
        return True
    else:
        await asyncio.gather(
            asyncio.to_thread(todoist.strip_age_labels, task_id, labels),
            asyncio.to_thread(todoist.delete_todoist_task, task_id),
        )
        audit.log(
            "delete",
            source="plan/triage",
            trigger="user_delete",
            task_id=task_id,
            title=title,
        )
        logger.info("[plan/triage] deleted %s", task_id)
        return False


async def triage_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or session.triage_index >= len(session.triage_tasks):
        return ConversationHandler.END

    task = session.triage_tasks[session.triage_index]
    task_id = task["id"]
    action = query.data.split(":")[1]
    title = task["title"]
    is_recurring = task.get("is_recurring", False)
    skip_advance = False
    labels = task.get("labels") or []
    session.triage_processed.append((task_id, action, labels))

    try:
        if action in ("p1", "p2", "p3"):
            res = await _handle_triage_priority(chat_id, query, session, task_id, title, action)
            if res is not None:
                return res
        elif action == "complete":
            await _handle_triage_complete(task, task_id, title, labels)
        elif action == "postpone":
            await _handle_triage_postpone(chat_id, context, task_id, title, is_recurring, labels)
        elif action == "quarantine":
            await _handle_triage_quarantine(chat_id, context, task_id, title, labels)
        elif action == "delete":
            # returns True if we should block advancement (e.g. for recurring tasks)
            skip_advance = await _handle_triage_delete(
                chat_id, context, task_id, title, is_recurring, labels
            )
    except Exception as exc:
        logger.error("Triage action %s on %s failed: %s", action, task_id, exc)
        await query.answer("Failed to update task.", show_alert=True)

    if skip_advance:
        _checkpoint(chat_id, TRIAGING)
        return TRIAGING

    await query.edit_message_reply_markup(reply_markup=None)
    _checkpoint(chat_id, TRIAGING)
    return await _advance_triage(chat_id, context, session)


async def timeslot_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or session.triage_pending_priority is None:
        return ConversationHandler.END

    task = session.triage_tasks[session.triage_index]
    task_id = task["id"]
    title = task["title"]
    action = session.triage_pending_priority
    slot_key = query.data.split(":")[1]  # "morning" | "afternoon" | "evening"

    hour = next((h for _, cb, h in _timeslots() if cb.endswith(slot_key)), None)
    if hour is None:
        logger.error("[plan/timeslot] unrecognised slot key: %s", slot_key)
        return ConversationHandler.END
    tz = ZoneInfo(_settings.timezone)
    due_dt = datetime.combine(datetime.now(tz).date(), dt_time(hour, 0), tzinfo=tz)

    try:
        await asyncio.to_thread(
            todoist.update_todoist_task,
            task_id,
            priority=PRIORITY_TO_TODOIST[action],
            due_datetime=due_dt,
        )
        audit.log(
            "update",
            source="plan/triage",
            trigger=f"user_set_{action}",
            task_id=task_id,
            title=title,
            changes={"priority": action, "due_time": f"{hour:02d}:00"},
        )
        logger.info("[plan/triage] set %s → %s @ %02d:00", task_id, action, hour)
    except Exception as exc:
        logger.error("Timeslot update %s on %s failed: %s", slot_key, task_id, exc)
        await query.answer("Failed to update task.", show_alert=True)

    session.triage_pending_priority = None
    await query.edit_message_reply_markup(reply_markup=None)
    _checkpoint(chat_id, TRIAGING)
    return await _advance_triage(chat_id, context, session)


async def _bulk_bump_ages(tasks: list[tuple[str, list[str]]]) -> None:
    """Increment age labels for a list of (task_id, labels) pairs in parallel."""

    async def _bump(task_id: str, labels: list[str]) -> None:
        try:
            await asyncio.to_thread(todoist.bump_task_age, task_id, labels)
        except Exception as exc:
            logger.warning("[plan/triage] age bump failed for %s: %s", task_id, exc)

    await asyncio.gather(*[_bump(tid, lbls) for tid, lbls in tasks])


def _format_plan_summary(lines: list[str], remaining: int, completed: int, deferred: int) -> str:
    parts = ["*📅 Today's Plan*"]
    for line in lines:
        if line.startswith("### "):
            parts.append(f"\n*{line[4:]}*")
        elif line.startswith("- [ ] "):
            parts.append(f"• {line[6:]}")
    if not any(p.startswith("•") for p in parts):
        parts.append("\n_Nothing scheduled for today._")
    stat = f"_{remaining} task{'s' if remaining != 1 else ''}"
    if completed:
        stat += f" · {completed} completed"
    if deferred:
        stat += f" · {deferred} deferred"
    stat += "_"
    parts.append(f"\n{stat}")
    return "\n".join(parts)


async def _finish_triage(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("[plan] triage complete chat=%s", chat_id)
    session = _sessions.get(chat_id)
    await context.bot.send_message(chat_id, "Triage complete ✓")

    # Increment age for tasks that passed through triage without being resolved or skipped.
    # Recurring tasks are excluded — their schedule is managed by Todoist, not by age labels.
    if session:
        _SKIP_AGE = {"complete", "delete", "quarantine"}
        task_map = {t["id"]: t for t in session.triage_tasks}
        age_tasks = [
            (task_id, labels)
            for task_id, action, labels in session.triage_processed
            if action not in _SKIP_AGE and not task_map.get(task_id, {}).get("is_recurring", False)
        ]
        if age_tasks:
            await _bulk_bump_ages(age_tasks)
            logger.info("[plan/triage] bumped age for %d tasks", len(age_tasks))

    store.clear_plan_session()

    # Persist today's task list to daily note, then send grouped summary
    tasks_today, (projects, inbox_id) = await asyncio.gather(
        asyncio.to_thread(todoist.get_today_tasks),
        asyncio.to_thread(todoist.get_projects_info),
    )
    lines = obsidian.build_tasks_section(
        tasks_today,
        projects,
        inbox_id,
        _settings.morning_block,
        _settings.afternoon_block,
        _settings.evening_block,
    )
    await asyncio.to_thread(obsidian.write_tasks_section, lines)

    processed = session.triage_processed if session else []
    completed = sum(1 for _, action, _ in processed if action == "complete")
    _DEFER_ACTIONS = {"postpone", "quarantine", "delete"}
    deferred = sum(1 for _, action, _ in processed if action in _DEFER_ACTIONS)
    _sessions.pop(chat_id, None)
    await context.bot.send_message(
        chat_id,
        _format_plan_summary(lines, len(tasks_today), completed, deferred),
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _sessions.pop(update.effective_chat.id, None)
    store.clear_plan_session()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Handler export
# ---------------------------------------------------------------------------


_restart_handler = CallbackQueryHandler(restart_cb, pattern=f"^{_RESTART}$")

plan_handler = ConversationHandler(
    entry_points=[CommandHandler("plan", plan_cmd, WHITELIST_FILTER)],
    states={
        UNBLOCK_PROMPT: [
            CallbackQueryHandler(_ub_unblock_cb, pattern=f"^{_UB_UNBLOCK}$"),
            CallbackQueryHandler(_ub_skip_cb, pattern=f"^{_UB_SKIP}$"),
            CallbackQueryHandler(_ub_another_cb, pattern=f"^{_UB_ANOTHER}$"),
            CallbackQueryHandler(_ub_continue_cb, pattern=f"^{_UB_CONTINUE}$"),
        ],
        UNBLOCK_PICKING: [
            CallbackQueryHandler(_ub_pick_cb, pattern=f"^{_UB_PICK_PREFIX}"),
        ],
        UNBLOCK_ACTION: [
            CallbackQueryHandler(_ub_breakdown_action_cb, pattern=f"^{_UB_BREAKDOWN}$"),
            CallbackQueryHandler(_ub_done_action_cb, pattern=f"^{_UB_DONE}$"),
        ],
        UNBLOCK_BRAINSTORM: [
            MessageHandler(filters.TEXT & ~filters.COMMAND & WHITELIST_FILTER, _ub_brainstorm_cb),
        ],
        UNBLOCK_PRJ_CONFIRM: [
            CallbackQueryHandler(_ub_confirm_proj_cb, pattern=f"^{_UB_CONFIRM_PROJ}$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND & WHITELIST_FILTER, _ub_proj_name_cb),
        ],
        UNBLOCK_REVIEW: [
            CallbackQueryHandler(_ub_accept_cb, pattern=f"^{_UB_ACCEPT}$"),
            CallbackQueryHandler(_ub_reject_cb, pattern=f"^{_UB_REJECT}$"),
        ],
        BRAINSTORM_PROMPT: [
            CallbackQueryHandler(bs_prompt_cb, pattern=f"^({_BS_START}|{_BS_SKIP})$"),
            _restart_handler,
        ],
        BS_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND & WHITELIST_FILTER, bs_input_cb),
            CallbackQueryHandler(bs_prompt_cb, pattern=f"^{_BS_SKIP}$"),
            _restart_handler,
        ],
        BS_REVIEW: [
            CallbackQueryHandler(bs_accept_cb, pattern=f"^{_BS_ACCEPT}$"),
            CallbackQueryHandler(bs_reject_cb, pattern=f"^{_BS_REJECT}$"),
            CallbackQueryHandler(bs_continue_cb, pattern=f"^{_BS_CONTINUE}$"),
            CallbackQueryHandler(bs_next_cb, pattern=f"^{_BS_NEXT}$"),
            _restart_handler,
        ],
        RESUME_CONFIRM: [
            _restart_handler,
        ],
        TRIAGING: [
            CallbackQueryHandler(triage_cb, pattern="^plan_triage:"),
            _restart_handler,
        ],
        TRIAGE_TIMESLOT: [
            CallbackQueryHandler(timeslot_cb, pattern="^plan_timeslot:"),
            _restart_handler,
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_cmd, WHITELIST_FILTER)],
    per_chat=True,
    per_message=False,
    conversation_timeout=600,
    name="plan",
)
