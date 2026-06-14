import time

from telegram import Update
from telegram.ext import ContextTypes

from lib.models import store

_PHASE_LABELS = {
    0: "brainstorm prompt",
    1: "brainstorm input",
    2: "brainstorm review",
    3: "unblock prompt",
    4: "unblock reviewing",
    5: "unblock brainstorm input",
    6: "unblock brainstorm review",
    7: "triage",
    8: "resume confirm",
    9: "triage timeslot",
}

HELP_TEXT = """
*Commands*
/task <title> — capture and enrich a new task
/list — show today's tasks
/done <id> — complete a task
/completed — list recently completed tasks
/plan — generate a timeblocked plan for today
/digest — get today's morning digest
/unblock — break down a stuck task into actionable steps
/deepdive — deep-dive analysis on a single task
/insights — pattern insights: habits, workload, clusters
/project — project-level plan and next actions
/brainstorm — extract tasks from free-form text
/cancel — cancel current operation
""".strip()


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    await update.message.reply_text("Assistant ready. Use /help to see available commands.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def session_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    persisted = store.load_plan_session()
    if not persisted:
        await update.message.reply_text("No active plan session.")
        return
    phase, data = persisted
    label = _PHASE_LABELS.get(phase, str(phase))
    triage_tasks = len(data.get("triage_tasks", []))
    triage_index = data.get("triage_index", 0)
    triage_processed = len(data.get("triage_processed", []))
    bs_proposed = len(data.get("bs_proposed", []))
    last_ts = data.get("last_user_action_ts", 0)
    age_mins = int((time.time() - last_ts) / 60)
    lines = [f"*Plan session:* phase={phase} ({label})"]
    if bs_proposed:
        lines.append(f"Brainstorm: {bs_proposed} proposed, {data.get('bs_created', 0)} created")
    if triage_tasks:
        lines.append(f"Triage: {triage_tasks} tasks, index={triage_index}, {triage_processed} done")
    lines.append(f"Last action: {age_mins}m ago")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
