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

import lib.llm as llm
import lib.obsidian as obsidian
import lib.todoist as todoist
from lib.handlers.auth import WHITELIST_FILTER
from lib.llm import restore_links
from lib.models import PRIORITY_TO_TODOIST, Task
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
AWAITING_ANSWER   = 8

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

_TODOIST_TASK_URL = "https://todoist.com/app/task/{id}"

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

    # triage
    triage_tasks: list[dict] = field(default_factory=list)
    triage_index: int = 0

    # Q&A + plan
    qa_tasks: list[dict] = field(default_factory=list)
    qa_questions: list[dict] = field(default_factory=list)
    qa_answers: list[str] = field(default_factory=list)
    qa_current_q: int = 0


_sessions: dict[int, PlanFlowSession] = {}

# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    logger.info("[plan] session started for chat %s", chat_id)
    _sessions[chat_id] = PlanFlowSession()
    await update.message.reply_text(
        "Starting your planning session.",
    )
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
        return BS_INPUT
    logger.info("[plan] phase=brainstorm skipped chat=%s", chat_id)
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
            "Couldn't find tasks in that — try again or tap Skip."
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
        await asyncio.to_thread(todoist.create_todoist_task, Task(title=title))
        session.bs_created += 1
        await query.edit_message_text(f"✅ _Created:_ {title}", parse_mode="Markdown")
    except Exception as exc:
        logger.error("[plan/bs] failed to create '%s': %s", title, exc)
        await query.answer("Failed to create task.", show_alert=True)

    session.bs_index += 1
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
    await _show_bs_proposal(chat_id, context)
    return BS_REVIEW


async def bs_continue_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(query.message.chat_id, "Keep going — what else?")
    return BS_INPUT


async def bs_next_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    await query.edit_message_reply_markup(reply_markup=None)
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
    non_actionable = [r for r in results if not r["actionable"]]

    async def _apply(r: dict) -> None:
        original = task_by_id.get(r["id"], {})
        labels = list(original.get("labels") or [])
        if "actionable" not in labels:
            labels.append("actionable")
        kwargs: dict = {"labels": labels}
        clean = r.get("clean_title")
        if clean:
            kwargs["content"] = restore_links(original.get("title", ""), clean)
        try:
            await asyncio.to_thread(todoist.update_todoist_task, r["id"], **kwargs)
        except Exception as exc:
            logger.error("[plan/opt] auto-label failed for %s: %s", r["id"], exc)

    await asyncio.gather(*[_apply(r) for r in actionable])

    session.opt_auto_labeled = len(actionable)
    session.opt_queue = non_actionable

    summary = f"Auto-labeled {len(actionable)} tasks as actionable."
    if not non_actionable:
        await context.bot.send_message(chat_id, summary + " All done!")
        await _start_triage(chat_id, context)
        return TRIAGING

    summary += f" {len(non_actionable)} need your attention."
    await context.bot.send_message(chat_id, summary)
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
    title = task.get("clean_title") or task["title"]
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
    labels = list(task.get("labels") or [])
    if "actionable" not in labels:
        labels.append("actionable")
    try:
        await asyncio.to_thread(todoist.update_todoist_task, task["id"], labels=labels)
        logger.info("[plan/opt] override task %s '%s'", task["id"], task["title"][:60])
    except Exception as exc:
        logger.error("[plan/opt] override failed for %s: %s", task["id"], exc)
        await query.answer("Failed to update task.", show_alert=True)

    await query.edit_message_reply_markup(reply_markup=None)
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
    title = session.opt_current_task.get("clean_title") or session.opt_current_task["title"]
    await query.edit_message_reply_markup(reply_markup=None)
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
    clean = task.get("clean_title")
    if clean:
        try:
            await asyncio.to_thread(
                todoist.update_todoist_task, task["id"],
                content=restore_links(task["title"], clean),
            )
        except Exception as exc:
            logger.error("[plan/opt] skip cleanup failed for %s: %s", task["id"], exc)

    await query.edit_message_reply_markup(reply_markup=None)
    await _show_opt_task(chat_id, context)
    return OPT_REVIEWING


async def opt_delete_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session or not session.opt_queue:
        return ConversationHandler.END

    task = session.opt_queue.pop(0)
    try:
        await asyncio.to_thread(todoist.delete_todoist_task, task["id"])
    except Exception as exc:
        logger.error("[plan/opt] delete failed for %s: %s", task["id"], exc)
        await query.answer("Failed to delete task.", show_alert=True)

    await query.edit_message_reply_markup(reply_markup=None)
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
        await asyncio.to_thread(
            todoist.create_todoist_task,
            Task(title=title, notes=notes, priority=original.get("priority", "p4"),
                 labels=["actionable"]),
        )
        session.opt_created += 1
        await query.edit_message_text(f"✅ _Created:_ {title}", parse_mode="Markdown")
    except Exception as exc:
        logger.error("[plan/opt] failed to create '%s': %s", title, exc)
        await query.answer("Failed to create task.", show_alert=True)

    session.opt_proposal_index += 1
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
    await _show_opt_proposal(chat_id, context)
    return OPT_BS_REVIEW


async def _finalize_opt_breakdown(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session or not session.opt_current_task:
        return
    try:
        await asyncio.to_thread(todoist.delete_todoist_task, session.opt_current_task["id"])
        session.opt_broken_down += 1
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
    if not tasks:
        await context.bot.send_message(chat_id, "No tasks to triage — moving to planning.")
        await _start_qa(chat_id, context, tasks=[])
        return
    session.triage_tasks = tasks
    session.triage_index = 0
    total = len(tasks)
    await context.bot.send_message(
        chat_id,
        f"📋 *Triage* — {total} task{'s' if total != 1 else ''} to review.",
        parse_mode="Markdown",
    )
    await _show_triage_task(chat_id, context)


def _triage_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("P1 🔴", callback_data="plan_triage:p1"),
            InlineKeyboardButton("P2 🟠", callback_data="plan_triage:p2"),
            InlineKeyboardButton("P3 🟡", callback_data="plan_triage:p3"),
        ],
        [
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

    meta = f"Priority: {priority} · Due: {due}"
    if duration:
        meta += f" · {duration}min"

    await context.bot.send_message(
        chat_id,
        f"*Task {idx} of {total}*\n\n{task['title']}\n_{meta}_",
        reply_markup=_triage_keyboard(),
        parse_mode="Markdown",
    )


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

    try:
        if action in ("p1", "p2", "p3"):
            await asyncio.to_thread(
                todoist.update_todoist_task, task_id,
                priority=PRIORITY_TO_TODOIST[action], due_string="today",
            )
            logger.info("[plan/triage] set %s → %s", task_id, action)
        elif action == "postpone":
            await asyncio.to_thread(todoist.remove_task_due_date, task_id)
            logger.info("[plan/triage] postponed %s", task_id)
        elif action == "delete":
            await asyncio.to_thread(todoist.delete_todoist_task, task_id)
            logger.info("[plan/triage] deleted %s", task_id)
    except Exception as exc:
        logger.error("Triage action %s on %s failed: %s", action, task_id, exc)
        await query.answer("Failed to update task.", show_alert=True)

    await query.edit_message_reply_markup(reply_markup=None)
    session.triage_index += 1

    if session.triage_index >= len(session.triage_tasks):
        await _finish_triage(chat_id, context)
        return ConversationHandler.END

    await _show_triage_task(chat_id, context)
    return TRIAGING


async def _finish_triage(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("[plan] triage complete chat=%s", chat_id)
    await context.bot.send_message(chat_id, "Triage complete ✓")
    # Persist today's task list to daily note
    tasks = await asyncio.to_thread(todoist.get_today_tasks)
    lines = [
        obsidian.format_task_line(
            t["title"], t["is_completed"],
            t.get("priority", "p4"), t.get("duration_minutes"),
        )
        for t in tasks
    ]
    await asyncio.to_thread(obsidian.write_tasks_section, lines)
    _sessions.pop(chat_id, None)


# ---------------------------------------------------------------------------
# Phase 4 & 5: Q&A + Plan generation (mandatory)
# ---------------------------------------------------------------------------


async def _start_qa(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE, tasks: list[dict]
) -> None:
    logger.info("[plan] phase=qa tasks=%d chat=%s", len(tasks), chat_id)
    session = _sessions.get(chat_id)
    if not session:
        return

    # Exclude postponed tasks from plan context
    tasks = [t for t in tasks if "postpone" not in (t.get("labels") or [])]
    if not tasks:
        await context.bot.send_message(chat_id, "No tasks left to plan. All done!")
        _sessions.pop(chat_id, None)
        return

    questions = await llm.generate_planning_questions(tasks)
    session.qa_tasks = tasks
    session.qa_questions = questions
    await _send_qa_question(chat_id, context)


def _qa_keyboard(options: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(options), 3):
        rows.append([
            InlineKeyboardButton(opt, callback_data=f"plan_qa:{i + j}")
            for j, opt in enumerate(options[i:i + 3])
        ])
    return InlineKeyboardMarkup(rows)


async def _send_qa_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(chat_id)
    if not session:
        return
    q_data = session.qa_questions[session.qa_current_q]
    total = len(session.qa_questions)
    idx = session.qa_current_q + 1
    await context.bot.send_message(
        chat_id,
        f"*{idx}/{total}* — {q_data['question']}",
        reply_markup=_qa_keyboard(q_data["options"]),
        parse_mode="Markdown",
    )


async def _record_answer(answer: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END

    session.qa_answers.append(answer)
    session.qa_current_q += 1

    if session.qa_current_q < len(session.qa_questions):
        await _send_qa_question(chat_id, context)
        return AWAITING_ANSWER

    return await _finalize_planning(chat_id, context)


async def qa_button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _sessions.get(chat_id)
    if not session:
        return ConversationHandler.END

    idx = int(query.data.split(":")[1])
    options = session.qa_questions[session.qa_current_q]["options"]
    answer = options[idx] if idx < len(options) else str(idx)

    await query.edit_message_reply_markup(reply_markup=None)
    await query.edit_message_text(
        query.message.text + f"\n\n_You chose: {answer}_",
        parse_mode="Markdown",
    )
    return await _record_answer(answer, chat_id, context)


async def qa_text_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _record_answer(
        update.message.text.strip(), update.effective_chat.id, context
    )


async def _finalize_planning(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info("[plan] phase=generate chat=%s", chat_id)
    session = _sessions.pop(chat_id, None)
    if not session:
        return ConversationHandler.END

    context_str = "\n\n".join(
        f"Q: {q['question']}\nA: {a}"
        for q, a in zip(session.qa_questions, session.qa_answers)
    )

    await context.bot.send_message(chat_id, "Generating your plan…")
    try:
        plan_md = await llm.generate_plan(session.qa_tasks, _settings, context_str)
        await asyncio.gather(
            context.bot.send_message(chat_id, plan_md, parse_mode="Markdown"),
            asyncio.to_thread(obsidian.append_plan, plan_md),
        )
    except Exception as exc:
        logger.error("Plan generation failed: %s", exc)
        await context.bot.send_message(chat_id, "Failed to generate plan. Please try again.")

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Handler export
# ---------------------------------------------------------------------------


plan_handler = ConversationHandler(
    entry_points=[CommandHandler("plan", plan_cmd, WHITELIST_FILTER)],
    states={
        BRAINSTORM_PROMPT: [
            CallbackQueryHandler(bs_prompt_cb, pattern=f"^({_BS_START}|{_BS_SKIP})$"),
        ],
        BS_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND & WHITELIST_FILTER, bs_input_cb),
        ],
        BS_REVIEW: [
            CallbackQueryHandler(bs_accept_cb, pattern=f"^{_BS_ACCEPT}$"),
            CallbackQueryHandler(bs_reject_cb, pattern=f"^{_BS_REJECT}$"),
            CallbackQueryHandler(bs_continue_cb, pattern=f"^{_BS_CONTINUE}$"),
            CallbackQueryHandler(bs_next_cb, pattern=f"^{_BS_NEXT}$"),
        ],
        OPTIMIZE_PROMPT: [
            CallbackQueryHandler(opt_prompt_cb, pattern=f"^({_OPT_START}|{_OPT_SKIP})$"),
        ],
        OPT_REVIEWING: [
            CallbackQueryHandler(opt_optimize_cb, pattern=f"^{_OPT_OPT}$"),
            CallbackQueryHandler(opt_skip_task_cb, pattern=f"^{_OPT_SKIP_T}$"),
            CallbackQueryHandler(opt_delete_cb, pattern=f"^{_OPT_DELETE}$"),
            CallbackQueryHandler(opt_override_cb, pattern=f"^{_OPT_OVERRIDE}$"),
        ],
        OPT_BS_INPUT: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & WHITELIST_FILTER, opt_bs_input_cb
            ),
        ],
        OPT_BS_REVIEW: [
            CallbackQueryHandler(opt_accept_cb, pattern=f"^{_OPT_ACCEPT}$"),
            CallbackQueryHandler(opt_reject_cb, pattern=f"^{_OPT_REJECT}$"),
        ],
        TRIAGING: [
            CallbackQueryHandler(triage_cb, pattern="^plan_triage:"),
        ],
        # AWAITING_ANSWER and plan generation states are wired up but not active yet
    },
    fallbacks=[CommandHandler("cancel", cancel_cmd, WHITELIST_FILTER)],
    per_chat=True,
    conversation_timeout=600,
    name="plan",
)
