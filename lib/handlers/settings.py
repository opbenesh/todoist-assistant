from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from lib.handlers.auth import WHITELIST_FILTER
from lib.models import store

logger = logging.getLogger(__name__)

# Callback data prefix
_SET_REMINDERS_PREFIX = "set_reminders:"


def _get_settings_keyboard() -> InlineKeyboardMarkup:
    current = store.reminder_settings

    # Hourly, Daily, and Off buttons with checkmark for selected option
    btn_hourly = InlineKeyboardButton(
        "✅ Hourly" if current == "hourly" else "Hourly",
        callback_data=f"{_SET_REMINDERS_PREFIX}hourly",
    )
    btn_daily = InlineKeyboardButton(
        "✅ Daily" if current == "daily" else "Daily",
        callback_data=f"{_SET_REMINDERS_PREFIX}daily",
    )
    btn_off = InlineKeyboardButton(
        "✅ Off" if current == "off" else "Off",
        callback_data=f"{_SET_REMINDERS_PREFIX}off",
    )

    return InlineKeyboardMarkup([[btn_hourly, btn_daily, btn_off]])


def _get_settings_text() -> str:
    current = store.reminder_settings
    current_label = current.capitalize()

    text = (
        "*Settings*\n\n"
        "*Reminder Notifications*\n"
        "Choose how often you want to be proactively reminded to plan your day:\n"
        "• *Hourly*: Morning reminder (9:00 AM) + hourly nags.\n"
        "• *Daily*: Morning reminder (9:00 AM) only.\n"
        "• *Off*: No proactive reminders.\n\n"
        f"Current setting: *{current_label}*"
    )
    return text


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return

    await msg.reply_text(
        text=_get_settings_text(),
        reply_markup=_get_settings_keyboard(),
        parse_mode="Markdown",
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    new_setting = query.data.split(":", 1)[1]
    if new_setting not in ("hourly", "daily", "off"):
        await query.answer("Invalid setting selection.")
        return

    old_setting = store.reminder_settings
    if new_setting == old_setting:
        await query.answer(f"Reminders are already set to {new_setting}.")
        return

    # Update store
    store.reminder_settings = new_setting

    # Also update the active scheduler settings so it takes effect immediately
    from lib.scheduler import get_settings

    try:
        scheduler_settings = get_settings()
        if scheduler_settings:
            scheduler_settings.reminder_settings = new_setting
    except Exception as exc:
        logger.error("Failed to update scheduler settings in-memory: %s", exc)

    # Answer query and edit message in place
    await query.answer(f"Reminder notifications set to {new_setting}.")

    await query.edit_message_text(
        text=_get_settings_text(),
        reply_markup=_get_settings_keyboard(),
        parse_mode="Markdown",
    )

    # Send a separate brief text confirmation as per recommended feedback choice
    chat_id = update.effective_chat.id if update.effective_chat else query.message.chat_id
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Reminder notifications updated to *{new_setting.capitalize()}*.",
        parse_mode="Markdown",
    )


settings_handler = CommandHandler("settings", settings_cmd, WHITELIST_FILTER)
settings_callback_handler = CallbackQueryHandler(
    settings_callback, pattern=f"^{_SET_REMINDERS_PREFIX}"
)
