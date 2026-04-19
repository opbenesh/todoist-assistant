"""Structured interaction logger.

Records every Telegram update and bot reply to data/interactions.jsonl.
"""

import json
import logging
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.ext import ExtBot

_LOG_PATH = Path(__file__).parent.parent / "data" / "interactions.jsonl"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB

logger = logging.getLogger(__name__)


def _write(entry: dict) -> None:
    line = json.dumps(entry, ensure_ascii=False, default=str)
    try:
        if _LOG_PATH.exists() and _LOG_PATH.stat().st_size >= _MAX_BYTES:
            rotated = _LOG_PATH.with_suffix(".jsonl.1")
            _LOG_PATH.rename(rotated)
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        logger.warning("interaction_log: failed to write entry", exc_info=True)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log_incoming(update: Update) -> None:
    entry: dict = {"ts": _ts(), "dir": "in", "update_id": update.update_id}

    if update.effective_chat:
        entry["chat_id"] = update.effective_chat.id
    if update.effective_user:
        entry["user_id"] = update.effective_user.id

    if update.message:
        msg = update.message
        if msg.text and msg.text.startswith("/"):
            parts = msg.text.split()
            entry["type"] = "command"
            entry["cmd"] = parts[0]
            if len(parts) > 1:
                entry["args"] = parts[1:]
        elif msg.text:
            entry["type"] = "message"
            entry["text"] = msg.text
        else:
            entry["type"] = "message"
            entry["text"] = None
    elif update.callback_query:
        cq = update.callback_query
        entry["type"] = "callback"
        entry["data"] = cq.data
        if cq.message:
            entry["message_id"] = cq.message.message_id
    else:
        entry["type"] = "other"

    _write(entry)


def log_outgoing(method: str, chat_id: int | None, text: str | None, **kwargs: Any) -> None:
    entry: dict = {
        "ts": _ts(),
        "dir": "out",
        "method": method,
        "chat_id": chat_id,
    }
    if text is not None:
        entry["text"] = text
    if "reply_markup" in kwargs and kwargs["reply_markup"] is not None:
        entry["has_keyboard"] = True
    if "parse_mode" in kwargs and isinstance(kwargs["parse_mode"], str):
        entry["parse_mode"] = kwargs["parse_mode"]
    _write(entry)


def log_error(update: Update | None, exc: Exception | None) -> None:
    entry: dict = {"ts": _ts(), "dir": "error"}
    if update is not None:
        entry["update_id"] = update.update_id
        if update.effective_chat:
            entry["chat_id"] = update.effective_chat.id
    if exc is not None:
        entry["exc"] = str(exc)
        entry["traceback"] = traceback.format_exc()
    _write(entry)


class LoggedBot(ExtBot):
    """ExtBot subclass that logs all outgoing messages to interactions.jsonl."""

    async def send_message(self, chat_id: Any, text: str | None = None, **kwargs: Any) -> Any:
        result = await super().send_message(chat_id, text=text, **kwargs)
        log_outgoing("send_message", chat_id, text, **kwargs)
        return result

    async def edit_message_text(self, text: str, **kwargs: Any) -> Any:
        result = await super().edit_message_text(text, **kwargs)
        log_outgoing(
            "edit_message_text",
            kwargs.get("chat_id"),
            text,
            **{k: v for k, v in kwargs.items() if k != "chat_id"},
        )
        return result

    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> Any:
        result = await super().answer_callback_query(callback_query_id, **kwargs)
        filtered = {k: v for k, v in kwargs.items() if k != "text"}
        log_outgoing("answer_callback_query", None, kwargs.get("text"), **filtered)
        return result


# Expose log path for external tooling (e.g. CLI inspection)
LOG_PATH: str = os.fspath(_LOG_PATH)
