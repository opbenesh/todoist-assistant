#!/usr/bin/env python3
"""CLI script to send a one-way message via the Telegram Bot."""

import argparse
import asyncio
import sys


def main() -> None:
    # Attempt to pre-load default chat ID from lib.config
    default_chat_id = None
    try:
        from lib.config import TELEGRAM_USER_ID

        default_chat_id = TELEGRAM_USER_ID
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Send a message from the Telegram bot to a user or chat."
    )
    parser.add_argument(
        "-m",
        "--message",
        help=(
            "The message to send. If not provided, "
            "the message will be read from standard input (stdin)."
        ),
    )
    parser.add_argument(
        "-c",
        "--chat-id",
        type=int,
        default=default_chat_id,
        help="The target Telegram Chat ID. Defaults to TELEGRAM_USER_ID from .env if set.",
    )

    args = parser.parse_args()

    # Validate chat ID
    if args.chat_id is None:
        try:
            from lib.config import TELEGRAM_USER_ID  # noqa: F401
        except Exception as e:
            sys.stderr.write(f"Configuration error: {e}\n")
            sys.exit(1)
        sys.stderr.write(
            "Error: Chat ID is required (either via --chat-id / -c "
            "or in .env as TELEGRAM_USER_ID)\n"
        )
        sys.exit(1)

    # Determine message content
    if args.message is not None:
        message = args.message
    else:
        if sys.stdin.isatty():
            sys.stderr.write("Reading message from stdin (Ctrl-D to send)...\n")
        message = sys.stdin.read()

    if not message.strip():
        sys.stderr.write("Error: Message cannot be empty\n")
        sys.exit(1)

    # Import telegram libraries and environment keys
    try:
        import os

        from dotenv import load_dotenv
        from telegram import Bot
        from telegram.error import TelegramError

        from lib.config import TELEGRAM_API_BASE_URL, TELEGRAM_BOT_KEY

        load_dotenv()
        bot_token = os.environ.get("TELEGRAM_CODING_AGENT_BOT_KEY") or TELEGRAM_BOT_KEY
    except Exception as e:
        sys.stderr.write(f"Error importing dependencies or configuration: {e}\n")
        sys.exit(1)

    async def send_msg() -> None:
        _bot_kwargs = {"token": bot_token}
        if TELEGRAM_API_BASE_URL:
            _bot_kwargs["base_url"] = f"{TELEGRAM_API_BASE_URL}/bot"
        bot = Bot(**_bot_kwargs)
        async with bot:
            await bot.send_message(chat_id=args.chat_id, text=message, parse_mode="MarkdownV2")

    try:
        asyncio.run(send_msg())
        print("Message sent successfully!")
    except TelegramError as e:
        sys.stderr.write(f"Telegram API Error: {e}\n")
        sys.exit(1)
    except Exception as e:
        sys.stderr.write(f"Error sending message: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
