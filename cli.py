#!/usr/bin/env python3
"""CLI debug client — mirrors the Telegram bot interface without authentication."""

import asyncio
import os

os.environ["CLI_MODE"] = "1"  # use data/state_cli.json, not data/state.json
from datetime import datetime, timezone
from typing import Any

from telegram import (
    Bot,
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    Update,
    User,
)
from telegram.ext import Application

from lib.config import TELEGRAM_BOT_KEY, TELEGRAM_USER_ID
from main import _post_init, load_settings, register_handlers

# ── shared state ──────────────────────────────────────────────────────────────

_update_id = 0
_msg_id = 0
_current_keyboard: InlineKeyboardMarkup | None = None
_last_msg: Message | None = None

CHAT_ID = int(TELEGRAM_USER_ID)
_DEBUG_USER = User(id=CHAT_ID, first_name="You", is_bot=False, username="debug")
_DEBUG_CHAT = Chat(id=CHAT_ID, type=Chat.PRIVATE)


# ── terminal rendering ────────────────────────────────────────────────────────


def _print_keyboard(markup: InlineKeyboardMarkup) -> None:
    buttons = [btn for row in markup.inline_keyboard for btn in row]
    parts = [f"[{i + 1}] {btn.text}" for i, btn in enumerate(buttons)]
    print("  " + "   ".join(parts))


def _render_message(text: str, reply_markup: Any, edited: bool = False) -> None:
    global _current_keyboard
    prefix = "🤖 ✏ " if edited else "🤖  "
    print(f"\n{prefix}{text}")
    if isinstance(reply_markup, InlineKeyboardMarkup):
        _current_keyboard = reply_markup
        _print_keyboard(reply_markup)
    elif not edited:
        _current_keyboard = None
    print()


# ── CliBot ────────────────────────────────────────────────────────────────────


class CliBot(Bot):
    """Prints to stdout instead of calling the Telegram API."""

    async def initialize(self) -> None:
        self._bot_user = await self.get_me()
        self._initialized = True

    async def shutdown(self) -> None:
        pass

    async def get_me(self) -> User:
        return User(
            id=0,
            first_name="CliBot",
            is_bot=True,
            username="clibot",
            can_join_groups=True,
            can_read_all_group_messages=False,
            supports_inline_queries=False,
        )

    async def set_my_commands(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def send_message(  # type: ignore[override]
        self,
        chat_id: Any,
        text: str,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> Message:
        global _msg_id, _last_msg
        _msg_id += 1
        _render_message(text, reply_markup)
        msg = Message(
            message_id=_msg_id,
            date=datetime.now(timezone.utc),
            chat=_DEBUG_CHAT,
            from_user=_DEBUG_USER,
            text=text,
        )
        msg.set_bot(self)
        _last_msg = msg
        return msg

    async def edit_message_text(  # type: ignore[override]
        self,
        text: str,
        chat_id: Any = None,
        message_id: Any = None,
        inline_message_id: Any = None,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> Message:
        global _msg_id, _last_msg
        _msg_id += 1
        _render_message(text, reply_markup, edited=True)
        msg = Message(
            message_id=_msg_id,
            date=datetime.now(timezone.utc),
            chat=_DEBUG_CHAT,
            from_user=_DEBUG_USER,
            text=text,
        )
        msg.set_bot(self)
        _last_msg = msg
        return msg

    async def edit_message_reply_markup(  # type: ignore[override]
        self,
        chat_id: Any = None,
        message_id: Any = None,
        inline_message_id: Any = None,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> Message | bool:
        global _current_keyboard
        if isinstance(reply_markup, InlineKeyboardMarkup):
            _current_keyboard = reply_markup
            print()
            _print_keyboard(reply_markup)
            print()
        else:
            _current_keyboard = None
        return True

    async def answer_callback_query(  # type: ignore[override]
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
        **kwargs: Any,
    ) -> bool:
        if text and show_alert:
            print(f"\n⚠️  {text}\n")
        return True


# ── Update factories ──────────────────────────────────────────────────────────


def _next_update_id() -> int:
    global _update_id
    _update_id += 1
    return _update_id


def _next_msg_id() -> int:
    global _msg_id
    _msg_id += 1
    return _msg_id


def _make_message_update(text: str, bot: CliBot) -> Update:
    entities = []
    if text.startswith("/"):
        cmd = text.split()[0]
        entities = [MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(cmd))]

    msg = Message(
        message_id=_next_msg_id(),
        date=datetime.now(timezone.utc),
        chat=_DEBUG_CHAT,
        from_user=_DEBUG_USER,
        text=text,
        entities=entities,
    )
    msg.set_bot(bot)
    update = Update(update_id=_next_update_id(), message=msg)
    return update


def _make_callback_update(data: str, bot: CliBot) -> Update:
    cq = CallbackQuery(
        id=str(_next_update_id()),
        from_user=_DEBUG_USER,
        chat_instance=str(CHAT_ID),
        data=data,
        message=_last_msg,
    )
    cq.set_bot(bot)
    update = Update(update_id=_next_update_id(), callback_query=cq)
    return update


# ── REPL ─────────────────────────────────────────────────────────────────────


async def run() -> None:
    load_settings()

    cli_bot = CliBot(token=TELEGRAM_BOT_KEY)
    app: Application = Application.builder().bot(cli_bot).post_init(_post_init).build()
    register_handlers(app)

    await app.initialize()

    print("Telegram CLI  —  type a command or message, or a number to pick a button.")
    print("Ctrl-C to quit.\n")

    loop = asyncio.get_event_loop()

    while True:
        try:
            line: str = await loop.run_in_executor(None, input, "You: ")
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        line = line.strip()
        if not line:
            continue

        # Button selection
        if line.isdigit() and _current_keyboard is not None:
            idx = int(line) - 1
            buttons = [btn for row in _current_keyboard.inline_keyboard for btn in row]
            if 0 <= idx < len(buttons):
                btn = buttons[idx]
                print(f"  → {btn.text}")
                update = _make_callback_update(btn.callback_data or "", cli_bot)
                await app.process_update(update)
            else:
                print(f"  Pick 1–{len(buttons)}")
            continue

        update = _make_message_update(line, cli_bot)
        await app.process_update(update)

    await app.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
