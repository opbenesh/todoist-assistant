from telegram import Update
from telegram.ext import ContextTypes

HELP_TEXT = """
*Commands*
/task <title> — capture and enrich a new task
/list — show today's tasks
/done <id> — complete a task
/plan — generate a timeblocked plan for today
/digest — get today's morning digest
/cancel — cancel current operation
""".strip()


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Assistant ready. Use /help to see available commands.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")
