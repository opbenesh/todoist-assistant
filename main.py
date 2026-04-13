import logging

from telegram import BotCommand
from telegram.ext import Application, CommandHandler

from lib.config import DEBUG_LOGGING, TELEGRAM_BOT_KEY, TELEGRAM_USER_ID
from lib.handlers.auth import WHITELIST_FILTER
from lib.handlers.brainstorm import brainstorm_handler
from lib.handlers.capture import (
    completed_handler,
    done_handler,
    list_handler,
    task_conversation_handler,
)
from lib.handlers.common import help_cmd, start_cmd
from lib.handlers.deepdive import deepdive_handler
from lib.handlers.digest import digest_handler
from lib.handlers.insights import insights_handler
from lib.handlers.optimize import optimize_conversation_handler
from lib.handlers.plan import plan_handler
from lib.handlers.project_plan import project_handler
from lib.obsidian import read_tasks_section  # noqa: F401 — ensure vault is accessible
from lib.scheduler import attach_scheduler, configure
from lib.todoist import build_user_settings, get_user_settings

logging.basicConfig(
    level=logging.DEBUG if DEBUG_LOGGING else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def _load_profile() -> dict:
    """Parse vault/Assistant/profile.md into a flat key: value dict."""
    from pathlib import Path

    from lib.config import VAULT_PATH

    path = Path(VAULT_PATH) / "Assistant" / "profile.md"
    if not path.exists():
        return {}

    profile: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            profile[key.strip()] = value.strip()
    return profile


_COMMANDS = [
    BotCommand("task", "Add and enrich a new task"),
    BotCommand("list", "Show today's tasks"),
    BotCommand("done", "Complete a task by ID"),
    BotCommand("completed", "List recently completed tasks"),
    BotCommand("plan", "Generate a timeblocked plan for today"),
    BotCommand("digest", "Get today's task digest"),
    BotCommand("optimize", "Review and improve task hygiene"),
    BotCommand("deepdive", "Deep-dive analysis on a single task"),
    BotCommand("insights", "Pattern insights: habits, workload, clusters"),
    BotCommand("project", "Project-level plan and next actions"),
    BotCommand("brainstorm", "Extract tasks from free-form text"),
    BotCommand("help", "Show available commands"),
    BotCommand("cancel", "Cancel current operation"),
]


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(_COMMANDS)


def load_settings() -> None:
    """Load user settings from Todoist API + profile.md and configure scheduler."""
    profile = _load_profile()
    todoist_data = get_user_settings()
    settings = build_user_settings(todoist_data, profile)
    configure(settings)
    logger.info("User settings: tz=%s, first_day=%s", settings.timezone, settings.first_day_of_week)


def register_handlers(app: Application) -> None:
    """Register all bot handlers onto the given Application."""
    app.add_handler(CommandHandler("start", start_cmd, WHITELIST_FILTER))
    app.add_handler(CommandHandler("help", help_cmd, WHITELIST_FILTER))
    app.add_handler(brainstorm_handler)
    app.add_handler(task_conversation_handler)
    app.add_handler(list_handler)
    app.add_handler(done_handler)
    app.add_handler(completed_handler)
    app.add_handler(plan_handler)
    app.add_handler(digest_handler)
    app.add_handler(optimize_conversation_handler)
    app.add_handler(deepdive_handler)
    app.add_handler(insights_handler)
    app.add_handler(project_handler)


def main() -> None:
    load_settings()

    app = Application.builder().token(TELEGRAM_BOT_KEY).post_init(_post_init).build()

    register_handlers(app)
    attach_scheduler(app)

    logger.info("Starting assistant bot (polling), user_id=%s", TELEGRAM_USER_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
