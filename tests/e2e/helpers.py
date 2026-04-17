"""Test helper classes for e2e tests.

BotClient       — inject Telegram messages, await bot responses
TodoistInspector — inspect/seed/reset the fake Todoist server
LLMInspector    — inspect/configure the fake LLM server
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx


class BotClient:
    """Send messages to the fake Telegram server and read bot responses."""

    def __init__(self, telegram_url: str, chat_id: int = 99999, user_id: int = 99999) -> None:
        self._url = telegram_url
        self._chat_id = chat_id
        self._user_id = user_id
        self._seen_count = 0  # index of responses already consumed

    def send_message(self, text: str) -> None:
        """Inject a text message from the test user."""
        httpx.post(f"{self._url}/test/inject_message", json={"text": text}, timeout=5.0)

    def press_button(self, callback_data: str, message_id: int = 1, message_text: str = "") -> None:
        """Simulate pressing an inline keyboard button."""
        httpx.post(
            f"{self._url}/test/inject_callback",
            json={"data": callback_data, "message_id": message_id, "message_text": message_text},
            timeout=5.0,
        )

    def wait_responses(self, n: int = 1, timeout: float = 3.0) -> list[dict]:
        """Wait until at least `self._seen_count + n` responses are captured.

        Returns the new responses (those not seen before this call).
        """
        import sys
        target = self._seen_count + n
        r = httpx.get(
            f"{self._url}/test/wait_responses",
            params={"min_count": target, "timeout": timeout},
            timeout=timeout + 2.0,
        )
        all_responses = r.json().get("responses", [])
        new = all_responses[self._seen_count:]
        if len(new) < n:
            print(
                f"\n[wait_responses TIMEOUT] wanted {n} new response(s) after index "
                f"{self._seen_count}, got {len(new)}. All responses ({len(all_responses)}):",
                file=sys.stderr,
            )
            for i, resp in enumerate(all_responses):
                print(f"  [{i}] {resp.get('type')} | {resp.get('text', '')[:100]!r}", file=sys.stderr)
        self._seen_count = len(all_responses)
        return new

    def last_text(self, timeout: float = 3.0) -> str:
        """Return the text of the most recent bot message (waits for 1 if none yet)."""
        responses = self.wait_responses(1, timeout=timeout)
        if responses:
            return responses[-1].get("text", "")
        return ""

    def last_keyboard(self, timeout: float = 3.0) -> list[list[str]]:
        """Return button labels from the most recent inline keyboard."""
        buttons = self.last_buttons(timeout=timeout)
        return [[btn["text"] for btn in row] for row in buttons]

    def last_buttons(self, timeout: float = 3.0) -> list[list[dict]]:
        """Return full button dicts (text + callback_data) from the most recent keyboard.

        Each button dict has at least: {"text": "...", "callback_data": "..."}
        """
        all_res = self.all_responses()
        if not all_res:
            # If no responses yet, wait for one
            self.wait_responses(1, timeout=timeout)
            all_res = self.all_responses()

        if not all_res:
            return []

        markup = all_res[-1].get("reply_markup")
        if not markup:
            return []
        if isinstance(markup, str):
            markup = json.loads(markup)
        return markup.get("inline_keyboard", [])

    def press_button_labeled(self, label_fragment: str, timeout: float = 5.0) -> bool:
        """Find a button whose label contains label_fragment and press it via its callback_data.

        This is the correct way to press triage/timeslot buttons — it sends the real
        callback_data (e.g. 'plan_triage:postpone') rather than the display label.
        Returns True if a matching button was found and pressed, False otherwise.
        """
        for row in self.last_buttons(timeout=timeout):
            for btn in row:
                if label_fragment.lower() in btn.get("text", "").lower():
                    self.press_button(btn.get("callback_data", btn["text"]))
                    return True
        return False

    def press_button_labeled_any(self, label_fragment: str, timeout: float = 3.0) -> bool:
        """Like press_button_labeled but scans ALL captured responses, not just new ones.

        Searches from most-recent to oldest. This handles the race where a message
        with a keyboard was already consumed by a previous wait_responses() call
        (e.g. triage header + triage task arriving simultaneously).
        Returns True if a matching button was found and pressed, False if timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for resp in reversed(self.all_responses()):
                markup = resp.get("reply_markup")
                if not markup:
                    continue
                if isinstance(markup, str):
                    markup = json.loads(markup)
                for row in markup.get("inline_keyboard", []):
                    for btn in row:
                        if label_fragment.lower() in btn.get("text", "").lower():
                            self.press_button(
                                btn.get("callback_data", btn["text"]),
                                message_id=resp.get("message_id", 1),
                                message_text=resp.get("text", "")
                            )
                            return True
            time.sleep(0.1)
        return False

    def wait_and_press_button(self, label_fragment: str, timeout: float = 3.0) -> bool:
        """Wait for an UNSEEN response containing label_fragment and press that button.

        Unlike press_button_labeled_any, only searches responses at/after _seen_count
        so it won't re-press buttons from already-processed messages. Advances
        _seen_count past the matched response when found.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            all_resp = self.all_responses()
            for idx, resp in enumerate(all_resp[self._seen_count:], start=self._seen_count):
                markup = resp.get("reply_markup")
                if not markup:
                    continue
                if isinstance(markup, str):
                    markup = json.loads(markup)
                for row in markup.get("inline_keyboard", []):
                    for btn in row:
                        if label_fragment.lower() in btn.get("text", "").lower():
                            self._seen_count = idx + 1
                            self.press_button(
                                btn.get("callback_data", btn["text"]),
                                message_id=resp.get("message_id", 1),
                                message_text=resp.get("text", "")
                            )
                            return True
            time.sleep(0.1)
        return False

    def press_button_by_callback_prefix(self, prefix: str, timeout: float = 3.0) -> bool:
        """Press the first button whose callback_data starts with `prefix`.

        Like press_button_labeled_any but matches on callback_data instead of label text.
        Useful for selecting any available timeslot regardless of which ones are valid.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for resp in reversed(self.all_responses()):
                markup = resp.get("reply_markup")
                if not markup:
                    continue
                if isinstance(markup, str):
                    markup = json.loads(markup)
                for row in markup.get("inline_keyboard", []):
                    for btn in row:
                        if btn.get("callback_data", "").startswith(prefix):
                            self.press_button(
                                btn["callback_data"],
                                message_id=resp.get("message_id", 1),
                                message_text=resp.get("text", "")
                            )
                            return True
            time.sleep(0.1)
        return False

    def all_responses(self) -> list[dict]:
        """Return all captured responses so far."""
        r = httpx.get(f"{self._url}/test/responses", timeout=5.0)
        return r.json().get("responses", [])

    def reset(self) -> None:
        """Clear captured responses and reset the seen counter."""
        httpx.post(f"{self._url}/test/reset", timeout=5.0)
        self._seen_count = 0

    def find_response_containing(self, substring: str, timeout: float = 3.0) -> dict | None:
        """Poll until a response containing `substring` appears, or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for resp in self.all_responses()[self._seen_count:]:
                if substring.lower() in (resp.get("text") or "").lower():
                    return resp
            time.sleep(0.2)
        return None


class TodoistInspector:
    """Inspect and seed the fake Todoist server."""

    def __init__(self, todoist_url: str) -> None:
        self._url = todoist_url

    def tasks(self) -> list[dict]:
        r = httpx.get(f"{self._url}/test/tasks", timeout=5.0)
        return r.json().get("tasks", [])

    def find(self, title_fragment: str) -> dict | None:
        """Find a task whose content contains title_fragment (case-insensitive)."""
        fragment = title_fragment.lower()
        for t in self.tasks():
            if fragment in t.get("content", "").lower():
                return t
        return None

    def history(self) -> list[dict]:
        r = httpx.get(f"{self._url}/test/history", timeout=5.0)
        return r.json().get("history", [])

    def history_ops(self) -> list[str]:
        """Return just the operation names from history."""
        return [h["op"] for h in self.history()]

    def seed(self, tasks: list[dict] | None = None, projects: list[dict] | None = None) -> None:
        payload: dict[str, Any] = {}
        if tasks:
            payload["tasks"] = tasks
        if projects:
            payload["projects"] = projects
        httpx.post(f"{self._url}/test/seed", json=payload, timeout=5.0)

    def reset(self) -> None:
        httpx.post(f"{self._url}/test/reset", timeout=5.0)

    def wait_for_op(self, op: str, timeout: float = 3.0) -> dict | None:
        """Wait until a history entry with the given op appears."""
        import sys
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for entry in self.history():
                if entry.get("op") == op:
                    return entry
            time.sleep(0.2)
        ops = [h.get("op") for h in self.history()]
        print(f"\n[wait_for_op TIMEOUT] waited {timeout}s for {op!r}, got: {ops}", file=sys.stderr)
        return None

    def wait_for_task(self, title_fragment: str, timeout: float = 3.0) -> dict | None:
        """Wait until a task matching title_fragment exists."""
        import sys
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.find(title_fragment)
            if found:
                return found
            time.sleep(0.2)
        print(f"\n[wait_for_task TIMEOUT] waited {timeout}s for {title_fragment!r}", file=sys.stderr)
        return None


class LLMInspector:
    """Inspect and configure the fake LLM server."""

    def __init__(self, llm_url: str) -> None:
        self._url = llm_url

    def calls(self) -> list[dict]:
        r = httpx.get(f"{self._url}/test/calls", timeout=5.0)
        return r.json().get("calls", [])

    def call_count(self) -> int:
        return len(self.calls())

    def set_next_response(self, text: str, count: int = 1) -> None:
        """Override the next `count` LLM responses with a fixed text."""
        httpx.post(
            f"{self._url}/test/set_next_response",
            json={"text": text, "count": count},
            timeout=5.0,
        )

    def reset(self) -> None:
        httpx.post(f"{self._url}/test/reset", timeout=5.0)
