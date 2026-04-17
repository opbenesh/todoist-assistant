"""Fake Telegram Bot API server.

Implements the Bot API subset that python-telegram-bot (PTB) v21+ uses during
polling mode. Supports long-polling (getUpdates with timeout), message sending,
and callback query answering.

Bot API endpoints (under /bot{token}/):
  GET  getMe                 — fixed bot info
  POST deleteWebhook         — no-op (called at polling start)
  POST setMyCommands         — no-op
  GET  getUpdates            — long-polling: blocks until update available or timeout
  POST sendMessage           — capture response, return message object
  POST editMessageText       — capture response, return ok
  POST answerCallbackQuery   — capture response, return ok
  POST sendChatAction        — no-op (typing indicator)
  GET  getChat               — stub chat info

Control plane (under /test/):
  POST /test/inject_message        — queue a text message update from the test user
  POST /test/inject_callback       — queue a callback_query update (button press)
  GET  /test/responses             — return all captured bot output
  GET  /test/wait_responses        — block until min_count responses arrive (or timeout)
  POST /test/reset                 — clear queue, responses, and counters
"""

from __future__ import annotations

import asyncio
import json as _json
import time
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


async def _parse_body(request: Request) -> dict:
    """Parse the request body as either JSON or form-encoded data.

    PTB v20+ sends parameters as application/x-www-form-urlencoded (no
    python-multipart needed — we parse the raw bytes with urllib.parse).
    For form-encoded requests, reply_markup / inline_keyboard values arrive as
    JSON strings — callers must handle str-valued fields (BotClient.last_buttons
    already does json.loads on str reply_markup).
    """
    content_type = request.headers.get("content-type", "")
    raw = await request.body()
    if not raw:
        return {}
    if "application/json" in content_type:
        try:
            return _json.loads(raw)
        except Exception:
            return {}
    # URL-encoded form data (PTB default) — parse manually without python-multipart
    if "application/x-www-form-urlencoded" in content_type or "multipart" not in content_type:
        try:
            parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            # parse_qs returns lists; take first value for each key (single-value params)
            return {k: v[0] for k, v in parsed.items()}
        except Exception:
            pass
    # Last resort: try JSON
    try:
        return _json.loads(raw)
    except Exception:
        return {}


app = FastAPI()

# ---------------------------------------------------------------------------
# Shared state (single global bot session — tests reset between runs)
# ---------------------------------------------------------------------------

_update_queue: asyncio.Queue = asyncio.Queue()
_responses: list[dict] = []
_update_id_counter: int = 1
_message_id_counter: int = 1000
_test_user_id: int = 99999
_test_chat_id: int = 99999

# Track the highest offset acknowledged by PTB so we don't re-deliver
_last_acked_offset: int = 0

# Generation counter: incremented on each reset so zombie getUpdates handlers
# (from terminated bot processes) exit immediately instead of stealing updates
# intended for the new bot instance.
_generation: int = 0


def _next_update_id() -> int:
    global _update_id_counter
    _update_id_counter += 1
    return _update_id_counter


def _next_message_id() -> int:
    global _message_id_counter
    _message_id_counter += 1
    return _message_id_counter


def _reset_state() -> None:
    global _message_id_counter, _update_id_counter, _last_acked_offset, _generation
    # Increment generation so any zombie getUpdates handlers (from a just-killed
    # bot process) detect the reset and exit immediately on their next poll,
    # rather than competing with the new bot for updates.
    _generation += 1
    # Drain the queue
    while not _update_queue.empty():
        try:
            _update_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    _responses.clear()
    # Reset counters so that injected updates always have IDs > _last_acked_offset.
    # This is safe because the generation increment above ensures zombie handlers
    # from the previous bot process exit without consuming new updates.
    _update_id_counter = 1
    _last_acked_offset = 0
    _message_id_counter = 1000


def _make_message(text: str, message_id: int | None = None) -> dict:
    msg: dict = {
        "message_id": message_id or _next_message_id(),
        "from": {
            "id": _test_user_id,
            "is_bot": False,
            "first_name": "Test",
            "username": "testuser",
        },
        "chat": {"id": _test_chat_id, "type": "private"},
        "date": int(time.time()),
        "text": text,
    }
    # PTB's CommandHandler uses parse_entities("bot_command") to detect commands.
    # Without the entities field, /commands are treated as plain text and ignored.
    if text.startswith("/"):
        cmd_token = text.split()[0]
        msg["entities"] = [{"offset": 0, "length": len(cmd_token), "type": "bot_command"}]
    return msg


def _make_update_message(text: str) -> dict:
    uid = _next_update_id()
    return {
        "update_id": uid,
        "message": _make_message(text),
    }


def _make_update_callback(
    callback_data: str,
    message_text: str = "placeholder",
    message_id: int = 1,
) -> dict:
    uid = _next_update_id()
    return {
        "update_id": uid,
        "callback_query": {
            "id": str(uid),
            "from": {
                "id": _test_user_id,
                "is_bot": False,
                "first_name": "Test",
                "username": "testuser",
            },
            "message": {
                "message_id": message_id,
                "chat": {"id": _test_chat_id, "type": "private"},
                "date": int(time.time()),
                "text": message_text,
            },
            "chat_instance": "test_instance",
            "data": callback_data,
        },
    }


# ---------------------------------------------------------------------------
# Bot API endpoints
# ---------------------------------------------------------------------------


@app.api_route("/bot{token}/getMe", methods=["GET", "POST"])
async def get_me(token: str) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "result": {
                "id": 1,
                "is_bot": True,
                "first_name": "StagingBot",
                "username": "staging_assistant_bot",
                "can_join_groups": True,
                "can_read_all_group_messages": False,
                "supports_inline_queries": False,
            },
        }
    )


@app.post("/bot{token}/deleteWebhook")
async def delete_webhook(token: str, request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "result": True})


@app.post("/bot{token}/setMyCommands")
async def set_my_commands(token: str, request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "result": True})


@app.api_route("/bot{token}/getChat", methods=["GET", "POST"])
async def get_chat(token: str, request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "result": {"id": _test_chat_id, "type": "private", "first_name": "Test"},
        }
    )


@app.post("/bot{token}/sendChatAction")
async def send_chat_action(token: str, request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "result": True})


@app.api_route("/bot{token}/getUpdates", methods=["GET", "POST"])
async def get_updates(token: str, request: Request) -> JSONResponse:
    global _last_acked_offset
    # PTB sends getUpdates as POST with a form-encoded or JSON body; also GET query params
    params = dict(request.query_params)
    if not params and request.method == "POST":
        params = await _parse_body(request)
    timeout = min(int(params.get("timeout", 0)), 30)  # cap at 30s for tests

    # Capture generation BEFORE the polling loop. If _reset_state() is called mid-poll
    # (new test starting), the loop detects the generation change and exits immediately
    # so zombie handlers from killed bot processes don't steal updates from new bots.
    my_generation = _generation

    # NOTE: We deliberately do NOT advance _last_acked_offset here. In our fake server
    # the queue is the source of truth and is drained on every reset — re-delivery cannot
    # happen. More importantly, zombie bots (PTB graceful shutdown after SIGTERM) keep
    # making getUpdates calls with their last high offset. If we advanced _last_acked_offset
    # from those calls, new test updates (IDs starting near 0 after reset) would be
    # silently discarded as "already acknowledged".

    # Poll for an update, checking generation on each iteration.
    # This ensures that if the bot process is killed and a new test starts (which calls
    # /test/reset, incrementing _generation), this zombie handler exits immediately
    # instead of stealing the next injected update from the new bot.
    deadline = time.monotonic() + max(timeout, 1)
    while time.monotonic() < deadline:
        # If reset occurred (new test starting), exit — the new bot's handler will pick up.
        if _generation != my_generation:
            return JSONResponse({"ok": True, "result": []})
        # Check for pending update without blocking
        try:
            update = _update_queue.get_nowait()
            return JSONResponse({"ok": True, "result": [update]})
        except asyncio.QueueEmpty:
            await asyncio.sleep(0.05)
    return JSONResponse({"ok": True, "result": []})


@app.post("/bot{token}/sendMessage")
async def send_message(token: str, request: Request) -> JSONResponse:
    body = await _parse_body(request)
    msg_id = _next_message_id()
    entry = {
        "type": "sendMessage",
        "chat_id": body.get("chat_id"),
        "text": body.get("text", ""),
        "message_id": msg_id,
        "reply_markup": body.get("reply_markup"),
        "parse_mode": body.get("parse_mode"),
    }
    _responses.append(entry)
    return JSONResponse(
        {
            "ok": True,
            "result": {
                "message_id": msg_id,
                "chat": {"id": body.get("chat_id"), "type": "private"},
                "date": int(time.time()),
                "text": body.get("text", ""),
            },
        }
    )


@app.post("/bot{token}/editMessageText")
async def edit_message_text(token: str, request: Request) -> JSONResponse:
    body = await _parse_body(request)
    msg_id = body.get("message_id", _next_message_id())
    entry = {
        "type": "editMessageText",
        "chat_id": body.get("chat_id"),
        "message_id": msg_id,
        "text": body.get("text", ""),
        "reply_markup": body.get("reply_markup"),
        "parse_mode": body.get("parse_mode"),
    }
    _responses.append(entry)
    return JSONResponse(
        {
            "ok": True,
            "result": {
                "message_id": msg_id,
                "chat": {"id": body.get("chat_id"), "type": "private"},
                "date": int(time.time()),
                "text": body.get("text", ""),
            },
        }
    )


@app.post("/bot{token}/answerCallbackQuery")
async def answer_callback_query(token: str, request: Request) -> JSONResponse:
    # Not captured in _responses — plumbing noise that disrupts test response counting
    return JSONResponse({"ok": True, "result": True})


@app.post("/bot{token}/editMessageReplyMarkup")
async def edit_message_reply_markup(token: str, request: Request) -> JSONResponse:
    # Not captured — keyboard removals are not useful for test assertions
    body = await _parse_body(request)
    msg_id = body.get("message_id", _next_message_id())
    return JSONResponse(
        {
            "ok": True,
            "result": {
                "message_id": msg_id,
                "chat": {"id": body.get("chat_id"), "type": "private"},
                "date": int(time.time()),
                "text": "",
            },
        }
    )


# ---------------------------------------------------------------------------
# Control plane
# ---------------------------------------------------------------------------


@app.post("/test/inject_message")
async def inject_message(request: Request) -> JSONResponse:
    body = await request.json()
    text = body.get("text", "")
    update = _make_update_message(text)
    await _update_queue.put(update)
    return JSONResponse({"ok": True, "update_id": update["update_id"]})


@app.post("/test/inject_callback")
async def inject_callback(request: Request) -> JSONResponse:
    body = await request.json()
    callback_data = body.get("data", "")
    message_text = body.get("message_text", "placeholder")
    message_id = int(body.get("message_id", 1))
    update = _make_update_callback(callback_data, message_text, message_id)
    await _update_queue.put(update)
    return JSONResponse({"ok": True, "update_id": update["update_id"]})


@app.get("/test/responses")
async def test_get_responses() -> JSONResponse:
    return JSONResponse({"responses": _responses})


@app.get("/test/wait_responses")
async def test_wait_responses(request: Request) -> JSONResponse:
    params = dict(request.query_params)
    min_count = int(params.get("min_count", 1))
    timeout = float(params.get("timeout", 10.0))
    deadline = time.monotonic() + timeout
    while len(_responses) < min_count and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    return JSONResponse({"responses": _responses, "count": len(_responses)})


@app.post("/test/reset")
async def test_reset() -> JSONResponse:
    _reset_state()
    return JSONResponse({"ok": True})


@app.get("/test/state")
async def test_state() -> JSONResponse:
    """Diagnostic: current internal state for debugging test isolation issues."""
    return JSONResponse(
        {
            "generation": _generation,
            "update_id_counter": _update_id_counter,
            "last_acked_offset": _last_acked_offset,
            "queue_size": _update_queue.qsize(),
            "response_count": len(_responses),
        }
    )


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})
