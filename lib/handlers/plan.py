from __future__ import annotations

import asyncio
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

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
import lib.obsidian as obsidian
import lib.todoist as todoist
from lib.handlers.auth import WHITELIST_FILTER
from lib.llm import restore_links
from lib.models import PRIORITY_TO_TODOIST, Task, store
from lib.optimize_shared import apply_actionable_label
from lib.scheduler import _settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

BRAINSTORM_PROMPT = 0
BS_INPUT          = 1
BS_REVIEW         = 2

OPTIMIZE_PROMPT   = 3
OPT_REVIEWING     = 4
OPT_BS_INPUT      = 5
OPT_BS_REVIEW     = 6

TRIAGING          = 7
RESUME_CONFIRM    = 8
TRIAGE_TIMESLOT   = 9

# ---------------------------------------------------------------------------
# Callback data constants
# ---------------------------------------------------------------------------

_BS_START    = "pf:bs_start"
_BS_SKIP     = "pf:bs_skip"
_BS_ACCEPT   = "pf:bs_accept"
_BS_REJECT   = "pf:bs_reject"
_BS_CONTINUE = "pf:bs_continue"
_BS_NEXT     = "pf:bs_next"

_OPT_START    = "pf:opt_start"
_OPT_SKIP     = "pf:opt_skip"
_OPT_OPT      = "pf:opt_optimize"
_OPT_SKIP_T   = "pf:opt_skip_task"
_OPT_DELETE   = "pf:opt_delete"
_OPT_OVERRIDE = "pf:opt_override"
_OPT_ACCEPT  = "pf:opt_accept"
_OPT_REJECT  = "pf:opt_reject"
_RESTART     = "pf:restart"

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

    # optimize
    opt_queue: list[dict] = field(default_factory=list)
    opt_auto_labeled: int = 0
    opt_current_task: dict | None = None
    opt_proposed: list[str] = field(default_factory=list)
    opt_proposal_index: int = 0
    opt_broken_down: int = 0
    opt_created: int = 0

    # optimize → triage deduplication
    opt_processed_ids: set[str] = field(default_factory=set)

    # triage
    triage_tasks: list[dict] = field(default_factory=list)
    triage_index: int = 0
    triage_pending_priority: str | None = None  # set while awaiting timeslot pick
    triage_processed: list[tuple[str, str, list[str]]] = field(default_factory=list)
    # each entry: (task_id, action, labels_at_triage_time)

    # resumption metadata
    last_user_action_ts: float = field(default_factory=_time.time)
    nudge_sent: bool = False

    def to_dict(self) -> dict:
        return {
            "bs_proposed": self.bs_proposed,
            "bs_index": self.bs_index,
            "bs_created": self.bs_created,
            "opt_queue": self.opt_queue,
            "opt_auto_labeled": self.opt_auto_labeled,
            "opt_current_task": self.opt_current_task,
            "opt_proposed": self.opt_proposed,
            "opt_proposal_index": self.opt_proposal_index,
            "opt_broken_down": self.opt_broken_down,
            "opt_created": self.opt_created,
            "opt_processed_ids": list(self.opt_processed_ids),
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
        s.opt_queue = d.get("opt_queue", [])
        s.opt_auto_labeled = d.get("opt_auto_labeled", 0)
        s.opt_current_task = d.get("opt_current_task")
        s.opt_proposed = d.get("opt_proposed", [])
        s.opt_proposal_index = d.get("opt_proposal_index", 0)
        s.opt_broken_down = d.get("opt_broken_down", 0)
        s.opt_created = d.get("opt_created", 0)
        s.opt_processed_ids = set(d.get("opt_processed_ids", []))
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
        store.save_plan_session(phase, session.to_dict())


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
    if phase == OPTIMIZE_PROMPT:
        return "optimize"
    if phase in (OPT_REVIEWING, OPT_BS_INPUT, OPT_BS_REVIEW):
        return "optimize review"
    if phase == TRIAGING:
        total = len(session.triage_tasks)
        return f"triage ({session.triage_index + 1}/{total})" if total else "triage"
    if phase == TRIAGE_TIMESLOT:
        total = len(session.triage_tasks)
        return (
            f"triage timeslot ({session.triage_index + 1}/{total})" if total else "triage timeslot"
        )
    return "planning"


async def _show_resume_header(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE, phase: int, session: PlanFlowSession
) -> None:
    label = _phase_label(phase, session)
    await context.bot.send_message(
        chat_id,
        f"↩ Resuming your planning session — *{label}*",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Start fresh", callback_data=_RESTART),
        ]]),
        parse_mode="Markdown",
    )


async def _show_phase_ui(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE, phase: int
) -> None:
    """Re-display the UI for the given conversation phase."""
    if phase == BRAINSTORM_PROMPT:
        await _show_brainstorm_prompt(chat_id, context)
    elif phase == BS_INPUT:
        await context.bot.send_message(chat_id, "What's on your mind? Type freely:")
    elif phase == BS_REVIEW:
        await _show_bs_proposal(chat_id, context)
    elif phase == OPTIMIZE_PROMPT:
        await _show_optimize_prompt(chat_id, context)
    elif phase in (OPT_REVIEWING, OPT_BS_INPUT, OPT_BS_REVIEW):
        await _show_opt_task(chat_id, context)
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
            logger.info("[plan] discarding stale session from %s for chat %s",
                        session_date, chat_id)
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
    _sessions[chat_id] = PlanFlowSession()
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
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("▶ Start", callback_data=_BS_START),
            InlineKeyboardButton("⏭ Skip", callback_data=_BS_SKIP),
        ]]),
        parse_mode="Markdown",
    )


async def restart_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Reset to a fresh session, carrying over already-triaged IDs to prevent re-triaging."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)

    old = store.load_plan_session()
    store.clear_plan_session()
    session = PlanFlowSession()
    if old:
        _, old_data = old
        old_sess = PlanFlowSession.from_dict(old_data)
        session.opt_processed_ids = {tid for tid, _, _ in old_sess.triage_processed}
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
        await context.bot.send_message(
            chat_id, "What's on your mind? Type freely:"
        )
        _checkpoint(chat_id, BS_INPUT)  # first save — user has engaged
        return BS_INPUT
    logger.info("[plan] phase=brainstorm skipped chat=%s", chat_id)
    _checkpoint(chat_id, OPTIMIZE_PROMPT)  # first save — user has engaged
    await _show_optimize_prompt(chat_id, context)
    return OPTIMIZE_PROMPT


async def bs_input_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END

    proposed = await llm.brainstorm_extract_tasks(update.message.text.strip())
    logger.info("[plan/bs] extracted %d tasks", len(proposed))
    if not proposed:
        await update.message.reply_text(
            "Couldn't find tasks in that — try again or tap Skip.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Skip", callback_data=_BS_SKIP),
            ]]),
        )
        return BS_INPUT

    session.bs_proposed = proposed
    session.bs_index = 0
    await _show_bs_proposal(chat_id, context)
    return BS_REVIEW


def _bs_proposal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=_BS_ACCEPT),
        InlineKeyboardButton("❌ Reject", callback_data=_BS_REJECT),
    ]])


def _bs_wrapup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ More", callback_data=_BS_CONTINUE),
        InlineKeyboardButton("▶ Next step", callback_data=_BS_NEXT),
    ]])


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
        audit.log("create", source="plan/brainstorm", trigger="user_accept",
                  task_id=task_id, title=title)
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
    _checkpoint(chat_id, OPTIMIZE_PROMPT)
    await _show_optimize_prompt(chat_id, context)
    return OPTIMIZE_PROMPT


# ---------------------------------------------------------------------------
# Phase 2: Optimize (optional)
# ---------------------------------------------------------------------------


async def _show_optimize_prompt(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(
        chat_id,
        "⚙️ *Optimize* — review all tasks for actionability.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("▶ Start", callback_data=_OPT_START),
            InlineKeyboardButton("⏭ Skip", callback_data=_OPT_SKIP),
        ]]),
        parse_mode="Markdown",
    )


async def opt_prompt_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)
    if query.data == _OPT_SKIP:
        logger.info("[plan] phase=optimize skipped chat=%s", chat_id)
        _checkpoint(chat_id, TRIAGING)
        await _start_triage(chat_id, context)
        return TRIAGING
    logger.info("[plan] phase=optimize chat=%s", chat_id)
    await context.bot.send_message(chat_id, "Fetching and judging tasks…")
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END

    all_tasks = await asyncio.to_thread(todoist.get_all_tasks)
    unlabeled = [t for t in all_tasks if "actionable" not in (t.get("labels") or [])]

    if not unlabeled:
        await context.bot.send_message(chat_id, "All tasks are already actionable.")
        await _start_triage(chat_id, context)
        return TRIAGING

    results = await llm.judge_tasks(unlabeled)
    task_by_id = {t["id"]: t for t in unlabeled}
    actionable = [r for r in results if r["actionable"]]
    non_actionable = [
        r for r in results
        if not r["actionable"]
        and not task_by_id.get(r["id"], {}).get("is_recurring", False)
    ]

    await asyncio.gather(*[
        apply_actionable_label(r, task_by_id, "plan/optimize/auto_label")
        for r in actionable
    ])

    session.opt_auto_labeled = len(actionable)
    session.opt_queue = non_actionable

    summary = f"Auto-labeled {len(actionable)} tasks as actionable."
    if not non_actionable:
        await context.bot.send_message(chat_id, summary + " All done!")
        _checkpoint(chat_id, TRIAGING)
        await _start_triage(chat_id, context)
        return TRIAGING

    summary += f" {len(non_actionable)} need your attention."
    await context.bot.send_message(chat_id, summary)
    _checkpoint(chat_id, OPT_REVIEWING)
    await _show_opt_task(chat_id, context)
    return OPT_REVIEWING


def _opt_review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚙️ Optimize", callback_data=_OPT_OPT),
            InlineKeyboardButton("⏭ Skip", callback_data=_OPT_SKIP_T),
            InlineKeyboardButton("🗑 Delete", callback_data=_OPT_DELETE),
        ],
        [InlineKeyboardButton("✅ It's actionable", callback_data=_OPT_OVERRIDE)],
    ])


async def _show_opt_task(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session or not session.opt_queue:
        await _finish_optimize(chat_id, context)
        return
    task = session.opt_queue[0]
    title = task.get("clean_title") or task.get("title", task["id"])
    reason = task.get("reason") or "Unclear next step"
    remaining = len(session.opt_queue)
    await context.bot.send_message(
        chat_id,
        f"*{title}*\n_Why not actionable: {reason}_\n\n_{remaining} remaining_",
        reply_markup=_opt_review_keyboard(),
        parse_mode="Markdown",
    )


async def opt_override_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or not session.opt_queue:
        return ConversationHandler.END

    task = session.opt_queue.pop(0)
    session.opt_processed_ids.add(task["id"])
    labels = list(task.get("labels") or [])
    if "actionable" not in labels:
        labels.append("actionable")
    try:
        await asyncio.to_thread(todoist.update_todoist_task, task["id"], labels=labels)
        title = task.get("clean_title") or task.get("title", task["id"])
        audit.log("update", source="plan/optimize/override", trigger="user_override",
                  task_id=task["id"], title=title, changes={"labels": labels})
        logger.info("[plan/opt] override task %s '%s'", task["id"],
                    (task.get("clean_title") or task.get("title", task["id"]))[:60])
    except Exception as exc:
        logger.error("[plan/opt] override failed for %s: %s", task["id"], exc)
        await query.answer("Failed to update task.", show_alert=True)

    await query.edit_message_reply_markup(reply_markup=None)
    _checkpoint(chat_id, OPT_REVIEWING)
    await _show_opt_task(chat_id, context)
    return OPT_REVIEWING


async def opt_optimize_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or not session.opt_queue:
        return ConversationHandler.END

    session.opt_current_task = session.opt_queue.pop(0)
    session.opt_processed_ids.add(session.opt_current_task["id"])
    title = session.opt_current_task.get("clean_title") or session.opt_current_task["title"]
    await query.edit_message_reply_markup(reply_markup=None)
    _checkpoint(chat_id, OPT_BS_INPUT)
    await context.bot.send_message(
        chat_id,
        f"What's your plan for *{title}*?\n\nDescribe freely:",
        parse_mode="Markdown",
    )
    return OPT_BS_INPUT


async def opt_skip_task_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or not session.opt_queue:
        return ConversationHandler.END

    task = session.opt_queue.pop(0)
    session.opt_processed_ids.add(task["id"])
    clean = task.get("clean_title")
    if clean:
        new_content = restore_links(task.get("title", ""), clean)
        try:
            await asyncio.to_thread(todoist.update_todoist_task, task["id"], content=new_content)
            audit.log("update", source="plan/optimize/skip", trigger="user_skip",
                      task_id=task["id"], title=task.get("title", clean),
                      changes={"content": new_content})
        except Exception as exc:
            logger.error("[plan/opt] skip cleanup failed for %s: %s", task["id"], exc)

    await query.edit_message_reply_markup(reply_markup=None)
    _checkpoint(chat_id, OPT_REVIEWING)
    await _show_opt_task(chat_id, context)
    return OPT_REVIEWING


async def opt_delete_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or not session.opt_queue:
        await query.answer()
        return ConversationHandler.END

    task = session.opt_queue.pop(0)
    session.opt_processed_ids.add(task["id"])

    if task.get("is_recurring"):
        # Deleting a recurring task removes all future occurrences permanently — skip it.
        await query.answer(
            "Recurring task — skipped. Open Todoist to manage the series.",
            show_alert=True,
        )
        await query.edit_message_reply_markup(reply_markup=None)
        await _show_opt_task(chat_id, context)
        return OPT_REVIEWING

    await query.answer()
    try:
        await asyncio.to_thread(todoist.delete_todoist_task, task["id"])
        audit.log("delete", source="plan/optimize/delete", trigger="user_delete",
                  task_id=task["id"],
                  title=task.get("clean_title") or task.get("title", task["id"]))
    except Exception as exc:
        logger.error("[plan/opt] delete failed for %s: %s", task["id"], exc)
        await query.answer("Failed to delete task.", show_alert=True)

    await query.edit_message_reply_markup(reply_markup=None)
    _checkpoint(chat_id, OPT_REVIEWING)
    await _show_opt_task(chat_id, context)
    return OPT_REVIEWING


async def opt_bs_input_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = _sessions.get(chat_id)
    if not session or not session.opt_current_task:
        return ConversationHandler.END

    proposed = await llm.breakdown_tasks_for_optimize(
        session.opt_current_task, update.message.text.strip()
    )
    if not proposed:
        await update.message.reply_text("Couldn't extract tasks — try again or /cancel.")
        return OPT_BS_INPUT

    session.opt_proposed = proposed
    session.opt_proposal_index = 0
    await _show_opt_proposal(chat_id, context)
    return OPT_BS_REVIEW


def _opt_proposal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=_OPT_ACCEPT),
        InlineKeyboardButton("❌ Reject", callback_data=_OPT_REJECT),
    ]])


async def _show_opt_proposal(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session:
        return
    if session.opt_proposal_index >= len(session.opt_proposed):
        await _finalize_opt_breakdown(chat_id, context)
        return
    title = session.opt_proposed[session.opt_proposal_index]
    idx = session.opt_proposal_index + 1
    total = len(session.opt_proposed)
    await context.bot.send_message(
        chat_id,
        f"*Proposal {idx} of {total}*\n\n{title}",
        reply_markup=_opt_proposal_keyboard(),
        parse_mode="Markdown",
    )


async def opt_accept_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or session.opt_proposal_index >= len(session.opt_proposed):
        return ConversationHandler.END

    title = session.opt_proposed[session.opt_proposal_index]
    original = session.opt_current_task
    original_title = original.get("clean_title") or original["title"]
    notes = f"From: [{original_title}]({_TODOIST_TASK_URL.format(id=original['id'])})"
    if original.get("notes"):
        notes += f"\n\n{original['notes']}"

    try:
        task_id = await asyncio.to_thread(
            todoist.create_todoist_task,
            Task(title=title, notes=notes, priority=original.get("priority", "p4"),
                 labels=["actionable"]),
        )
        session.opt_created += 1
        audit.log("create", source="plan/optimize/breakdown", trigger="user_accept",
                  task_id=task_id, title=title, original_task_id=original["id"],
                  original_title=original_title)
        await query.edit_message_text(f"✅ _Created:_ {title}", parse_mode="Markdown")
    except Exception as exc:
        logger.error("[plan/opt] failed to create '%s': %s", title, exc)
        await query.answer("Failed to create task.", show_alert=True)

    session.opt_proposal_index += 1
    _checkpoint(chat_id, OPT_BS_REVIEW)
    await _show_opt_proposal(chat_id, context)
    return OPT_BS_REVIEW


async def opt_reject_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or session.opt_proposal_index >= len(session.opt_proposed):
        return ConversationHandler.END

    title = session.opt_proposed[session.opt_proposal_index]
    await query.edit_message_text(f"❌ _Rejected:_ {title}", parse_mode="Markdown")
    session.opt_proposal_index += 1
    _checkpoint(chat_id, OPT_BS_REVIEW)
    await _show_opt_proposal(chat_id, context)
    return OPT_BS_REVIEW


async def _finalize_opt_breakdown(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session or not session.opt_current_task:
        return
    task = session.opt_current_task
    session.opt_processed_ids.add(task["id"])
    try:
        await asyncio.to_thread(todoist.delete_todoist_task, task["id"])
        session.opt_broken_down += 1
        audit.log("delete", source="plan/optimize/breakdown_finalize", trigger="auto",
                  task_id=task["id"], title=task.get("title", ""),
                  note="deleted after user broke it down into subtasks")
    except Exception as exc:
        logger.error("[plan/opt] delete original failed: %s", exc)
    session.opt_current_task = None
    session.opt_proposed = []
    session.opt_proposal_index = 0
    await _show_opt_task(chat_id, context)


async def _finish_optimize(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session:
        return
    await context.bot.send_message(
        chat_id,
        f"Optimize done — broken down: {session.opt_broken_down}, "
        f"new tasks: {session.opt_created}.",
    )
    await _start_triage(chat_id, context)


# ---------------------------------------------------------------------------
# Phase 3: Triage (mandatory)
# ---------------------------------------------------------------------------


async def _start_triage(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("[plan] phase=triage chat=%s", chat_id)
    tasks = await asyncio.to_thread(todoist.get_triage_tasks)
    session = _sessions.get(chat_id)
    if not session:
        return
    already_done = session.opt_processed_ids | {tid for tid, _, _ in session.triage_processed}
    if already_done:
        tasks = [t for t in tasks if t["id"] not in already_done]
    if not tasks:
        await context.bot.send_message(chat_id, "No tasks to triage — moving to planning.")
        tasks_today = await asyncio.to_thread(todoist.get_today_tasks)
        await _finalize_planning(chat_id, context, tasks_today)
        return
    session.triage_tasks = tasks
    session.triage_index = 0
    _checkpoint(chat_id, TRIAGING)  # persist tasks before showing first one
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
        ("🌅 Morning",   "plan_timeslot:morning",   _block_midpoint(_settings.morning_block)),
        ("☀️ Afternoon", "plan_timeslot:afternoon",  _block_midpoint(_settings.afternoon_block)),
        ("🌙 Evening",   "plan_timeslot:evening",    _block_midpoint(_settings.evening_block)),
    ]


def _valid_timeslots() -> list[tuple[str, str, int]]:
    """Return timeslots not yet within 30 minutes of passing."""
    tz = ZoneInfo(_settings.timezone)
    now = datetime.now(tz)
    cutoff = now.hour * 60 + now.minute + 30
    return [(label, cb, hour) for label, cb, hour in _timeslots() if hour * 60 >= cutoff]


def _timeslot_keyboard(slots: list[tuple[str, str, int]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=cb) for label, cb, _ in slots]
    ])


def _triage_keyboard(task_age: int = 0) -> InlineKeyboardMarkup:
    priority_row = [
        InlineKeyboardButton("P1 🔴", callback_data="plan_triage:p1"),
        InlineKeyboardButton("P2 🟠", callback_data="plan_triage:p2"),
        InlineKeyboardButton("P3 🟡", callback_data="plan_triage:p3"),
    ]
    if task_age >= MAX_TRIAGE_AGE:
        return InlineKeyboardMarkup([
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
        ])
    return InlineKeyboardMarkup([
        priority_row,
        [
            InlineKeyboardButton("✅ Done", callback_data="plan_triage:complete"),
            InlineKeyboardButton("⏸ Postpone", callback_data="plan_triage:postpone"),
            InlineKeyboardButton("🗑 Delete", callback_data="plan_triage:delete"),
        ],
    ])


async def _show_triage_task(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session:
        return

    task = session.triage_tasks[session.triage_index]
    idx = session.triage_index + 1
    total = len(session.triage_tasks)
    priority = task.get("priority", "p4").upper()
    due = task.get("due_date") or "no due date"
    duration = task.get("duration_minutes")
    is_recurring = task.get("is_recurring", False)
    age = 0 if is_recurring else todoist.get_task_age(task.get("labels") or [])

    meta = f"Priority: {priority} · Due: {due}"
    if is_recurring:
        meta += " · 🔁"
    if duration:
        meta += f" · {duration}min"

    age_badge = f" `#age{age}`" if age > 0 else ""
    await context.bot.send_message(
        chat_id,
        f"*Task {idx} of {total}*\n\n{task['title']}{age_badge}\n_{meta}_",
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
                todoist.update_todoist_task, task_id,
                priority=PRIORITY_TO_TODOIST[action], due_string="today",
            )
            audit.log("update", source="plan/triage", trigger=f"user_set_{action}",
                      task_id=task_id, title=title,
                      changes={"priority": action, "due": "today"})
            logger.info("[plan/triage] set %s → %s (no timeslot available)", task_id, action)
        elif action == "complete":
            await asyncio.to_thread(todoist.strip_age_labels, task_id, labels)
            await asyncio.to_thread(todoist.complete_todoist_task, task_id)
            audit.log("complete", source="plan/triage", trigger="user_complete",
                      task_id=task_id, title=title)
            logger.info("[plan/triage] completed %s", task_id)
        elif action == "postpone":
            if is_recurring:
                # Removing the due date would destroy the recurrence pattern.
                # Complete this occurrence instead so Todoist advances the schedule.
                await asyncio.to_thread(todoist.complete_todoist_task, task_id)
                audit.log("complete", source="plan/triage", trigger="user_postpone_recurring",
                          task_id=task_id, title=title)
                logger.info("[plan/triage] recurring postpone → completed %s", task_id)
            else:
                age = todoist.get_task_age(labels)
                await asyncio.to_thread(todoist.remove_task_due_date, task_id)
                audit.log("remove_due_date", source="plan/triage", trigger="user_postpone",
                          task_id=task_id, title=title)
                logger.info("[plan/triage] postponed %s", task_id)
                if age >= MAX_TRIAGE_AGE:
                    await context.bot.send_message(
                        chat_id,
                        f"⚠️ _{title}_ has been postponed {age} times.",
                        parse_mode="Markdown",
                    )
        elif action == "quarantine":
            await asyncio.to_thread(todoist.quarantine_task, task_id, labels)
            audit.log("update", source="plan/triage", trigger="user_quarantine",
                      task_id=task_id, title=title, changes={"labels": "+quarantined"})
            logger.info("[plan/triage] quarantined %s", task_id)
            await context.bot.send_message(
                chat_id,
                f"🚫 _{title}_ quarantined — hidden from future planning.",
                parse_mode="Markdown",
            )
        elif action == "delete":
            if is_recurring:
                # Deleting a recurring task removes all future occurrences permanently.
                # Block and let the user choose a different action.
                await context.bot.send_message(
                    chat_id,
                    "⚠️ Recurring task — deleting removes all future occurrences. "
                    "Use ✅ Done to skip this occurrence, or delete from Todoist directly.",
                )
                skip_advance = True
            else:
                await asyncio.to_thread(todoist.strip_age_labels, task_id, labels)
                await asyncio.to_thread(todoist.delete_todoist_task, task_id)
                audit.log("delete", source="plan/triage", trigger="user_delete",
                          task_id=task_id, title=title)
                logger.info("[plan/triage] deleted %s", task_id)
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
            todoist.update_todoist_task, task_id,
            priority=PRIORITY_TO_TODOIST[action],
            due_datetime=due_dt,
        )
        audit.log("update", source="plan/triage", trigger=f"user_set_{action}",
                  task_id=task_id, title=title,
                  changes={"priority": action, "due_time": f"{hour:02d}:00"})
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
            if action not in _SKIP_AGE
            and not task_map.get(task_id, {}).get("is_recurring", False)
        ]
        if age_tasks:
            await _bulk_bump_ages(age_tasks)
            logger.info("[plan/triage] bumped age for %d tasks", len(age_tasks))

    store.clear_plan_session()

    # Persist today's task list to daily note, then chain to plan + digest
    tasks_today = await asyncio.to_thread(todoist.get_today_tasks)
    lines = [
        obsidian.format_task_line(
            t["title"], t["is_completed"],
            t.get("priority", "p4"), t.get("duration_minutes"),
        )
        for t in tasks_today
    ]
    await asyncio.to_thread(obsidian.write_tasks_section, lines)
    await _finalize_planning(chat_id, context, tasks_today)


# ---------------------------------------------------------------------------
# Phase 4: Plan generation + Digest
# ---------------------------------------------------------------------------


async def _finalize_planning(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE, tasks_today: list[dict]
) -> None:
    logger.info("[plan] phase=generate chat=%s", chat_id)
    await context.bot.send_message(chat_id, "Generating your plan…")
    try:
        plan_md, digest = await asyncio.gather(
            llm.generate_plan(tasks_today, _settings),
            llm.generate_digest(tasks_today),
        )
        await asyncio.gather(
            context.bot.send_message(chat_id, plan_md, parse_mode="Markdown"),
            asyncio.to_thread(obsidian.append_plan, plan_md),
        )
        await context.bot.send_message(chat_id, f"📅 *Digest*\n\n{digest}", parse_mode="Markdown")
    except Exception as exc:
        logger.error("Plan/digest generation failed: %s", exc)
        await context.bot.send_message(chat_id, "Failed to generate plan. Please try again.")
    finally:
        _sessions.pop(chat_id, None)


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
        BRAINSTORM_PROMPT: [
            CallbackQueryHandler(bs_prompt_cb, pattern=f"^({_BS_START}|{_BS_SKIP})$"),
            _restart_handler,
        ],
        BS_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND & WHITELIST_FILTER, bs_input_cb),
            _restart_handler,
        ],
        BS_REVIEW: [
            CallbackQueryHandler(bs_accept_cb, pattern=f"^{_BS_ACCEPT}$"),
            CallbackQueryHandler(bs_reject_cb, pattern=f"^{_BS_REJECT}$"),
            CallbackQueryHandler(bs_continue_cb, pattern=f"^{_BS_CONTINUE}$"),
            CallbackQueryHandler(bs_next_cb, pattern=f"^{_BS_NEXT}$"),
            _restart_handler,
        ],
        OPTIMIZE_PROMPT: [
            CallbackQueryHandler(opt_prompt_cb, pattern=f"^({_OPT_START}|{_OPT_SKIP})$"),
            _restart_handler,
        ],
        OPT_REVIEWING: [
            CallbackQueryHandler(opt_optimize_cb, pattern=f"^{_OPT_OPT}$"),
            CallbackQueryHandler(opt_skip_task_cb, pattern=f"^{_OPT_SKIP_T}$"),
            CallbackQueryHandler(opt_delete_cb, pattern=f"^{_OPT_DELETE}$"),
            CallbackQueryHandler(opt_override_cb, pattern=f"^{_OPT_OVERRIDE}$"),
            _restart_handler,
        ],
        OPT_BS_INPUT: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & WHITELIST_FILTER, opt_bs_input_cb
            ),
            _restart_handler,
        ],
        OPT_BS_REVIEW: [
            CallbackQueryHandler(opt_accept_cb, pattern=f"^{_OPT_ACCEPT}$"),
            CallbackQueryHandler(opt_reject_cb, pattern=f"^{_OPT_REJECT}$"),
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
