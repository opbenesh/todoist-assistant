import logging

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from lib.config import DEBUG_LOGGING, TELEGRAM_API_BASE_URL, TELEGRAM_BOT_KEY, TELEGRAM_USER_ID
from lib.handlers.auth import WHITELIST_FILTER
from lib.handlers.brainstorm import brainstorm_handler
from lib.handlers.capture import (
    completed_handler,
    done_handler,
    list_handler,
    task_conversation_handler,
)
from lib.handlers.common import help_cmd, session_cmd, start_cmd
from lib.handlers.deepdive import deepdive_handler
from lib.handlers.digest import digest_handler
from lib.handlers.insights import insights_handler
from lib.handlers.optimize import optimize_conversation_handler
from lib.handlers.plan import has_active_plan_session, plan_handler
from lib.handlers.project_plan import project_handler
from lib.interaction_log import LoggedBot, log_error, log_incoming
from lib.obsidian import read_tasks_section  # noqa: F401 — ensure vault is accessible
from lib.scheduler import attach_scheduler, configure
from lib.todoist import build_user_settings, get_user_settings

logging.basicConfig(
    level=logging.DEBUG if DEBUG_LOGGING else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# Silence third-party loggers that produce routine noise
for _noisy in ("httpx", "httpcore", "apscheduler.executors.default", "apscheduler.scheduler"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
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
    BotCommand("session", "Show current plan session state"),
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


async def _log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_incoming(update)
    if update.message and update.message.text:
        user = update.effective_user
        name = user.username or user.first_name if user else "unknown"
        logger.info("← %s  [%s]", update.message.text.split("\n")[0][:80], name)
    elif update.callback_query:
        logger.info("← callback: %s", update.callback_query.data)


async def _stale_plan_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch plan_triage / plan_timeslot callbacks that the ConversationHandler didn't handle.

    This runs at group=1 (after group=0 ConversationHandler). If there's already an active
    in-memory session for this chat, the ConversationHandler handled it — do nothing.
    Otherwise the bot was restarted mid-triage and the user is pressing a stale button:
    show a visible alert so they know to resume with /plan.
    """
    query = update.callback_query
    if not query:
        return
    chat_id = query.message.chat_id if query.message else None
    if chat_id and has_active_plan_session(chat_id):
        return  # ConversationHandler handled it
    await query.answer("Session expired — send /plan to resume.", show_alert=True)


async def _log_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    real_update = update if isinstance(update, Update) else None
    log_error(real_update, context.error)
    if real_update and real_update.effective_chat:
        chat_id = real_update.effective_chat.id
        try:
            await context.bot.send_message(
                chat_id,
                "Something went wrong — please try again or use /cancel to reset.",
            )
        except Exception:
            pass


def register_handlers(app: Application) -> None:
    """Register all bot handlers onto the given Application."""
    app.add_handler(TypeHandler(Update, _log_update), group=-1)
    app.add_error_handler(_log_error_handler)
    app.add_handler(CommandHandler("start", start_cmd, WHITELIST_FILTER))
    app.add_handler(CommandHandler("help", help_cmd, WHITELIST_FILTER))
    app.add_handler(CommandHandler("session", session_cmd, WHITELIST_FILTER))
    app.add_handler(brainstorm_handler)
    app.add_handler(task_conversation_handler)
    app.add_handler(list_handler)
    app.add_handler(done_handler)
    app.add_handler(completed_handler)
    app.add_handler(plan_handler)
    # Stale-session fallback: catches plan_triage/plan_timeslot callbacks when the
    # ConversationHandler has no state (e.g. after bot restart). Runs at group=1
    # so it only fires if no group-0 handler consumed the update.
    app.add_handler(
        CallbackQueryHandler(_stale_plan_cb, pattern="^(plan_triage:|plan_timeslot:)"),
        group=1,
    )
    app.add_handler(digest_handler)
    app.add_handler(optimize_conversation_handler)
    app.add_handler(deepdive_handler)
    app.add_handler(insights_handler)
    app.add_handler(project_handler)


def main() -> None:
    load_settings()

    _bot_kwargs: dict = {"token": TELEGRAM_BOT_KEY}
    if TELEGRAM_API_BASE_URL:
        _bot_kwargs["base_url"] = f"{TELEGRAM_API_BASE_URL}/bot"
    app = Application.builder().bot(LoggedBot(**_bot_kwargs)).post_init(_post_init).build()

    register_handlers(app)
    attach_scheduler(app)

    logger.info("Starting assistant bot (polling), user_id=%s", TELEGRAM_USER_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
