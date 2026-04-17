"""pytest fixtures for e2e tests.

`staging_services` (session-scoped): starts fake_todoist, fake_llm, and
fake_telegram FastAPI servers in background threads. Yields a dict with the
base URLs for each service.

`staging_app` (function-scoped): resets all fake services, creates an
isolated vault + state dir, then starts the real main.py process as a
subprocess pointed at the fake services. Yields a dict with service URLs
and the subprocess handle.

`bot` (function-scoped): convenience wrapper — yields a BotClient configured
against the running staging_app.
`todoist_inspector` (function-scoped): yields a TodoistInspector for the
current test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Generator

import httpx
import pytest
import uvicorn

from tests.e2e.helpers import BotClient, LLMInspector, TodoistInspector
from tests.staging.fake_llm import app as llm_app
from tests.staging.fake_telegram import app as telegram_app
from tests.staging.fake_todoist import app as todoist_app

# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

TODOIST_PORT = 18468
LLM_PORT = 18469
TELEGRAM_PORT = 18467

TODOIST_URL = f"http://localhost:{TODOIST_PORT}"
LLM_URL = f"http://localhost:{LLM_PORT}"
TELEGRAM_URL = f"http://localhost:{TELEGRAM_PORT}"

APP_DIR = Path(__file__).parent.parent.parent


# ---------------------------------------------------------------------------
# Server management helpers
# ---------------------------------------------------------------------------


class _ServerThread(threading.Thread):
    """Run a uvicorn server in a background daemon thread."""

    def __init__(self, asgi_app, port: int) -> None:
        super().__init__(daemon=True)
        self._config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port, log_level="error")
        self._server = uvicorn.Server(self._config)

    def run(self) -> None:
        self._server.run()

    def stop(self) -> None:
        self._server.should_exit = True


def _wait_healthy(url: str, retries: int = 40, delay: float = 0.15) -> None:
    for _ in range(retries):
        try:
            r = httpx.get(f"{url}/health", timeout=1.0)
            if r.is_success:
                return
        except Exception:
            pass
        time.sleep(delay)
    raise RuntimeError(f"Service at {url} did not become healthy in time")


# ---------------------------------------------------------------------------
# Session-scoped: start fake services once per test session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def staging_services() -> Generator[dict, None, None]:
    servers = [
        _ServerThread(todoist_app, TODOIST_PORT),
        _ServerThread(llm_app, LLM_PORT),
        _ServerThread(telegram_app, TELEGRAM_PORT),
    ]
    for s in servers:
        s.start()

    _wait_healthy(TODOIST_URL)
    _wait_healthy(LLM_URL)
    _wait_healthy(TELEGRAM_URL)

    yield {
        "todoist_url": TODOIST_URL,
        "llm_url": LLM_URL,
        "telegram_url": TELEGRAM_URL,
    }

    for s in servers:
        s.stop()


# ---------------------------------------------------------------------------
# Function-scoped: reset services + launch app subprocess for each test
# ---------------------------------------------------------------------------


@pytest.fixture
def staging_app(
    staging_services: dict,
    tmp_path: Path,
) -> Generator[dict, None, None]:
    # Reset fake services
    for url in staging_services.values():
        httpx.post(f"{url}/test/reset", timeout=5.0)

    # Create isolated vault structure
    vault_dir = tmp_path / "vault"
    (vault_dir / "Daily").mkdir(parents=True)
    (vault_dir / "Assistant").mkdir(parents=True)
    (vault_dir / "Assistant" / "task-guidelines.md").write_text(
        "# Task Guidelines\n\nA good task is specific and actionable.\n",
        encoding="utf-8",
    )
    (vault_dir / "Assistant" / "profile.md").write_text(
        "timezone: UTC\nfirst_day_of_week: monday\ntime_format: 24h\n"
        "work_start: 09:00\nwork_end: 18:00\n"
        "morning_block: 09:00-12:00\nafternoon_block: 12:00-17:00\nevening_block: 17:00-21:00\n"
        "default_project: Inbox\nstale_task_days: 3\n",
        encoding="utf-8",
    )

    state_file = tmp_path / "state.json"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    env = {
        **os.environ,
        "TELEGRAM_BOT_KEY": "test-token-12345",
        "TODOIST_KEY": "fake-key",
        "ANTHROPIC_KEY": "sk-fake-key",
        "TELEGRAM_USER_ID": "99999",
        "TELEGRAM_API_BASE_URL": TELEGRAM_URL,
        "TODOIST_BASE_URL": TODOIST_URL,
        "ANTHROPIC_BASE_URL": LLM_URL,
        "VAULT_PATH": str(vault_dir),
        "STATE_PATH": str(state_file),
        "OBSIDIAN_POLL_SECONDS": "2",  # fast sync for tests
        "DEBUG_LOGGING": "false",
        # Unset dotenv loading so test env is clean
        "DOTENV_PATH": "",
    }

    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=str(APP_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    # Wait for bot to come up: poll for setMyCommands call or a sendMessage
    # We give it up to 15 seconds to start and contact the fake Telegram server
    deadline = time.monotonic() + 15.0
    started = False
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{TELEGRAM_URL}/test/responses", timeout=2.0)
            responses = r.json().get("responses", [])
            if responses:
                started = True
                break
        except Exception:
            pass
        if proc.poll() is not None:
            # Process died — collect output for diagnosis
            out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
            raise RuntimeError(f"Bot process exited unexpectedly:\n{out}")
        time.sleep(0.3)

    if not started:
        # Bot may have started but sent nothing — check it's still alive
        if proc.poll() is not None:
            out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
            raise RuntimeError(f"Bot process exited:\n{out}")

    # Reset responses so tests start clean (clear the startup messages)
    httpx.post(f"{TELEGRAM_URL}/test/reset", timeout=5.0)

    urls = {**staging_services, "vault_dir": str(vault_dir), "state_file": str(state_file)}

    yield {"proc": proc, "urls": urls, "vault_dir": vault_dir, "state_file": state_file}

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------------------
# Convenience fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bot(staging_app: dict) -> BotClient:
    return BotClient(TELEGRAM_URL)


@pytest.fixture
def todoist(staging_app: dict) -> TodoistInspector:
    return TodoistInspector(TODOIST_URL)


@pytest.fixture
def llm(staging_app: dict) -> LLMInspector:
    return LLMInspector(LLM_URL)
