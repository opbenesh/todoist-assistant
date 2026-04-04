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
from lib.models import PRIORITY_TO_TODOIST
from lib.scheduler import _settings

logger = logging.getLogger(__name__)

TRIAGING = 0
AWAITING_ANSWER = 1


@dataclass
class TriageSession:
    tasks: list[dict]
    index: int = 0


@dataclass
class PlanningQASession:
    tasks: list[dict]                    # filtered (no postponed), for plan generation
    questions: list[dict]                # [{"question": str, "options": list[str]}, ...]
    answers: list[str] = field(default_factory=list)
    current_q: int = 0


_triage_sessions: dict[int, TriageSession] = {}
_qa_sessions: dict[int, PlanningQASession] = {}


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    tasks = await asyncio.to_thread(todoist.get_today_tasks)
    if not tasks:
        await update.message.reply_text("No tasks scheduled for today.")
        return ConversationHandler.END

    _triage_sessions[chat_id] = TriageSession(tasks=tasks)
    total = len(tasks)
    await update.message.reply_text(
        f"Starting triage — {total} task{'s' if total != 1 else ''} to review."
    )
    await _show_triage_task(update, context)
    return TRIAGING


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------


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


async def _show_triage_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = _triage_sessions.get(chat_id)
    if session is None or session.index >= len(session.tasks):
        await _start_planning(update, context)
        return

    task = session.tasks[session.index]
    idx = session.index + 1
    total = len(session.tasks)
    priority = task.get("priority", "p4").upper()
    due = task.get("due_date") or "no due date"
    duration = task.get("duration_minutes")
    meta = f"Priority: {priority} · Due: {due}"
    if duration:
        meta += f" · {duration}min"

    text = (
        f"*Task {idx} of {total}*\n\n"
        f"📌 {task['title']}\n"
        f"_{meta}_"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=_triage_keyboard(), parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=_triage_keyboard(), parse_mode="Markdown"
        )


async def triage_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _triage_sessions.get(chat_id)
    if session is None or session.index >= len(session.tasks):
        return ConversationHandler.END

    task = session.tasks[session.index]
    task_id = task["id"]
    action = query.data.split(":")[1]

    try:
        if action in ("p1", "p2", "p3"):
            await asyncio.to_thread(
                todoist.update_todoist_task, task_id, priority=PRIORITY_TO_TODOIST[action]
            )
            logger.info("[triage] set %s → %s", task_id, action)
        elif action == "postpone":
            existing = list(task.get("labels") or [])
            if "postpone" not in existing:
                existing.append("postpone")
            await asyncio.to_thread(todoist.update_todoist_task, task_id, labels=existing)
            await asyncio.to_thread(todoist.remove_task_due_date, task_id)
            logger.info("[triage] postponed %s", task_id)
        elif action == "delete":
            await asyncio.to_thread(todoist.delete_todoist_task, task_id)
            logger.info("[triage] deleted %s", task_id)
    except Exception as exc:
        logger.error("Triage action %s on task %s failed: %s", action, task_id, exc)
        await query.answer("Failed to update task.", show_alert=True)

    session.index += 1
    await _show_triage_task(update, context)
    return TRIAGING


# ---------------------------------------------------------------------------
# Planning Q&A
# ---------------------------------------------------------------------------


async def _start_planning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _triage_sessions.pop(chat_id, None)

    # Re-fetch and exclude postponed tasks
    tasks = await asyncio.to_thread(todoist.get_today_tasks)
    tasks = [t for t in tasks if "postpone" not in (t.get("labels") or [])]

    done_text = "Triage complete ✓"
    if update.callback_query:
        await update.callback_query.edit_message_text(done_text)
    else:
        await update.message.reply_text(done_text)

    if not tasks:
        await context.bot.send_message(
            chat_id, "All tasks postponed — nothing to plan for today."
        )
        return

    questions = await llm.generate_planning_questions(tasks)
    _qa_sessions[chat_id] = PlanningQASession(tasks=tasks, questions=questions)
    await _send_qa_question(context.bot, chat_id)


def _qa_keyboard(options: list[str]) -> InlineKeyboardMarkup:
    """Build inline keyboard rows of up to 3 buttons each."""
    rows = []
    for i in range(0, len(options), 3):
        rows.append([
            InlineKeyboardButton(opt, callback_data=f"plan_qa:{i + j}")
            for j, opt in enumerate(options[i:i + 3])
        ])
    return InlineKeyboardMarkup(rows)


async def _send_qa_question(bot, chat_id: int) -> None:
    session = _qa_sessions.get(chat_id)
    if session is None:
        return
    q_data = session.questions[session.current_q]
    total = len(session.questions)
    idx = session.current_q + 1
    text = f"*{idx}/{total}* — {q_data['question']}"
    await bot.send_message(
        chat_id,
        text,
        reply_markup=_qa_keyboard(q_data["options"]),
        parse_mode="Markdown",
    )


async def _record_answer(answer: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    session = _qa_sessions.get(chat_id)
    if session is None:
        return ConversationHandler.END

    session.answers.append(answer)
    session.current_q += 1

    if session.current_q < len(session.questions):
        await _send_qa_question(context.bot, chat_id)
        return AWAITING_ANSWER

    return await _finalize_planning(chat_id, context)


async def qa_button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = _qa_sessions.get(chat_id)
    if session is None:
        return ConversationHandler.END

    idx = int(query.data.split(":")[1])
    options = session.questions[session.current_q]["options"]
    answer = options[idx] if idx < len(options) else str(idx)

    await query.edit_message_reply_markup(reply_markup=None)
    await query.edit_message_text(
        query.message.text + f"\n\n_You chose: {answer}_",
        parse_mode="Markdown",
    )
    return await _record_answer(answer, chat_id, context)


async def qa_text_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _record_answer(update.message.text.strip(), update.effective_chat.id, context)


# ---------------------------------------------------------------------------
# Plan generation
# ---------------------------------------------------------------------------


async def _finalize_planning(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    session = _qa_sessions.pop(chat_id, None)
    if session is None:
        return ConversationHandler.END

    tasks = session.tasks
    context_str = "\n\n".join(
        f"Q: {q['question']}\nA: {a}"
        for q, a in zip(session.questions, session.answers)
    )

    await context.bot.send_message(chat_id, "Generating your plan…")
    try:
        plan_md = await llm.generate_plan(tasks, _settings, context_str)

        await asyncio.gather(
            context.bot.send_message(chat_id, plan_md, parse_mode="Markdown"),
            asyncio.to_thread(obsidian.append_plan, plan_md),
        )
        await _write_planned_tasks()

    except Exception as exc:
        logger.error("Plan generation failed: %s", exc)
        await context.bot.send_message(chat_id, "Failed to generate plan. Please try again.")

    return ConversationHandler.END


async def _write_planned_tasks() -> None:
    tasks = await asyncio.to_thread(todoist.get_today_tasks)
    lines = [
        obsidian.format_task_line(
            t["title"], t["is_completed"],
            t.get("priority", "p4"), t.get("duration_minutes"),
        )
        for t in tasks
    ]
    await asyncio.to_thread(obsidian.write_tasks_section, lines)


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    _triage_sessions.pop(chat_id, None)
    _qa_sessions.pop(chat_id, None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Handler export
# ---------------------------------------------------------------------------


plan_handler = ConversationHandler(
    entry_points=[CommandHandler("plan", plan_cmd, WHITELIST_FILTER)],
    states={
        TRIAGING: [
            CallbackQueryHandler(triage_cb, pattern="^plan_triage:"),
        ],
        AWAITING_ANSWER: [
            CallbackQueryHandler(qa_button_cb, pattern="^plan_qa:"),
            MessageHandler(filters.TEXT & ~filters.COMMAND & WHITELIST_FILTER, qa_text_cb),
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_cmd, WHITELIST_FILTER)],
    per_chat=True,
    conversation_timeout=600,
    name="plan",
)
