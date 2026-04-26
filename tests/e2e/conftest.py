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
import socket
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
# Server management helpers
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).parent.parent.parent


def find_free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class _ServerThread(threading.Thread):
    """Run a uvicorn server in a background daemon thread."""

    def __init__(self, asgi_app, port: int) -> None:
        super().__init__(daemon=True)
        self._config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port, log_level="error")
        self._server = uvicorn.Server(self._config)
        self.port = port

    def run(self) -> None:
        self._server.run()

    def stop(self) -> None:
        self._server.should_exit = True


def _wait_bot_ready(telegram_url: str, timeout: float = 3.0) -> None:
    """Wait until the bot has issued its first getUpdates call after a reset.

    deleteWebhook fires during PTB's initialize() phase, before start_polling()
    and before the first getUpdates call. Polling for get_updates_count > 0 is
    the only deterministic signal that PTB's polling loop has fully started and
    is ready to receive injected messages.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{telegram_url}/test/state", timeout=1.0)
            if r.json().get("get_updates_count", 0) > 0:
                return
        except Exception:
            pass
        time.sleep(0.05)


def _wait_healthy(url: str, retries: int = 20, delay: float = 0.1) -> None:
    """Wait for a service to become healthy. Faster retries for dynamic ports."""
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
def bot_profiles() -> Generator[list, None, None]:
    """Accumulate per-test pyinstrument JSON profile paths; print locations at session end."""
    paths: list[str] = []
    yield paths
    if not paths:
        return
    print(f"\n\n=== BOT SUBPROCESS PROFILES ({len(paths)} files) ===")
    for p in paths:
        print(f"  {p}")
    print("Inspect with: pyinstrument --load-prev <path>")


@pytest.fixture(scope="session")
def staging_services() -> Generator[dict, None, None]:
    todoist_port = find_free_port()
    llm_port = find_free_port()
    telegram_port = find_free_port()

    todoist_url = f"http://localhost:{todoist_port}"
    llm_url = f"http://localhost:{llm_port}"
    telegram_url = f"http://localhost:{telegram_port}"

    servers = [
        _ServerThread(todoist_app, todoist_port),
        _ServerThread(llm_app, llm_port),
        _ServerThread(telegram_app, telegram_port),
    ]
    for s in servers:
        s.start()

    _wait_healthy(todoist_url)
    _wait_healthy(llm_url)
    _wait_healthy(telegram_url)

    yield {
        "todoist_url": todoist_url,
        "llm_url": llm_url,
        "telegram_url": telegram_url,
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
    bot_profiles: list,
) -> Generator[dict, None, None]:
    # Use dynamic URLs from fixture
    todoist_url = staging_services["todoist_url"]
    llm_url = staging_services["llm_url"]
    telegram_url = staging_services["telegram_url"]

    # Reset fake services
    for url in [todoist_url, llm_url, telegram_url]:
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

    import uuid
    state_file = tmp_path / f"state_{uuid.uuid4().hex}.json"
    data_dir = tmp_path / f"data_{uuid.uuid4().hex}"
    data_dir.mkdir()

    env = {
        **os.environ,
        "TELEGRAM_BOT_KEY": "test-token-12345",
        "TODOIST_KEY": "fake-key",
        "ANTHROPIC_KEY": "sk-fake-key",
        "TELEGRAM_USER_ID": "99999",
        "TELEGRAM_API_BASE_URL": telegram_url,
        "TODOIST_BASE_URL": todoist_url,
        "ANTHROPIC_BASE_URL": llm_url,
        "VAULT_PATH": str(vault_dir),
        "STATE_PATH": str(state_file),
        "OBSIDIAN_POLL_SECONDS": "2",  # fast sync for tests
        "OBSIDIAN_SYNC_FIRST_SECONDS": "2",  # first sync fires quickly in tests
        "TODOIST_POLL_SECONDS": "2",  # fast Todoist sync for tests
        "TODOIST_POLL_FIRST_SECONDS": "2",  # first Todoist poll fires quickly in tests
        "DEBUG_LOGGING": "false",
        # Unset dotenv loading so test env is clean
        "DOTENV_PATH": "",
    }

    profile_out = tmp_path / "bot.prof.json"
    if os.environ.get("E2E_PROFILE"):
        cmd = [
            sys.executable,
            "-m",
            "pyinstrument",
            "--renderer=json",
            "-o",
            str(profile_out),
            "main.py",
        ]
    else:
        cmd = [sys.executable, "main.py"]
    proc = subprocess.Popen(
        cmd,
        cwd=str(APP_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    # Wait for bot to call deleteWebhook (~1s) — PTB's first API call during init.
    # 5-second deadline gives generous margin even on slow machines.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
            raise RuntimeError(f"Bot process exited unexpectedly:\n{out}")
        try:
            r = httpx.get(f"{telegram_url}/test/state", timeout=1.0)
            if r.json().get("bot_initialized"):
                break
        except Exception:
            pass
        time.sleep(0.05)
    else:
        if proc.poll() is not None:
            out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
            raise RuntimeError(f"Bot process exited:\n{out}")

    # Reset so tests start with a clean slate (clears deleteWebhook init signal)
    httpx.post(f"{telegram_url}/test/reset", timeout=5.0)
    # Wait until PTB's polling loop has issued its first getUpdates after the reset.
    # deleteWebhook fires in initialize() before start_polling(), so bot_initialized
    # is an insufficient signal — we need the polling loop itself to be running.
    _wait_bot_ready(telegram_url)

    urls = {**staging_services, "vault_dir": str(vault_dir), "state_file": str(state_file)}

    yield {"proc": proc, "urls": urls, "vault_dir": vault_dir, "state_file": state_file}

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    if os.environ.get("E2E_PROFILE") and profile_out.exists() and profile_out.stat().st_size > 0:
        bot_profiles.append(str(profile_out))


# ---------------------------------------------------------------------------
# Convenience fixtures
# ---------------------------------------------------------------------------


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Append diagnostic context (bot stdout, Telegram responses, Todoist history, LLM calls)
    to the pytest failure report so failures are self-explanatory without manual log digging."""
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    staging_app = item.funcargs.get("staging_app")
    if staging_app is None:
        return
    proc = staging_app.get("proc")
    urls = staging_app.get("urls", {})
    lines = ["\n" + "=" * 60, "E2E FAILURE DIAGNOSTICS", "=" * 60]

    if proc and proc.stdout:
        try:
            import select

            out_chunks = []
            while select.select([proc.stdout], [], [], 0.0)[0]:
                chunk = proc.stdout.read1(65536).decode(errors="replace")
                if not chunk:
                    break
                out_chunks.append(chunk)
            bot_out = "".join(out_chunks) if out_chunks else "(no output buffered)"
        except Exception as e:
            bot_out = f"(error: {e})"
        lines.append(f"\n--- Bot stdout ---\n{bot_out}")

    tg_url = urls.get("telegram_url", "")
    if tg_url:
        try:
            resps = httpx.get(f"{tg_url}/test/responses", timeout=2.0).json().get("responses", [])
            lines.append(f"\n--- Telegram responses ({len(resps)}) ---")
            for i, r in enumerate(resps[-10:]):
                lines.append(f"  [{i}] {r.get('type')} | {r.get('text', '')[:120]!r}")
        except Exception as e:
            lines.append(f"  (unavailable: {e})")

    td_url = urls.get("todoist_url", "")
    if td_url:
        try:
            hist = httpx.get(f"{td_url}/test/history", timeout=2.0).json().get("history", [])
            lines.append(f"\n--- Todoist ops ({len(hist)}) ---")
            for entry in hist:
                lines.append(f"  {entry}")
        except Exception as e:
            lines.append(f"  (unavailable: {e})")

    llm_url = urls.get("llm_url", "")
    if llm_url:
        try:
            calls = httpx.get(f"{llm_url}/test/calls", timeout=2.0).json().get("calls", [])
            lines.append(f"\n--- LLM calls ({len(calls)}) ---")
            for c in calls:
                lines.append(f"  {c.get('system_snippet', '')!r} / {c.get('user_snippet', '')!r}")
        except Exception as e:
            lines.append(f"  (unavailable: {e})")

    lines.append("=" * 60)
    report.sections.append(("E2E Diagnostics", "\n".join(lines)))


@pytest.fixture
def bot(staging_app: dict) -> BotClient:
    return BotClient(staging_app["urls"]["telegram_url"])


@pytest.fixture
def todoist(staging_app: dict) -> TodoistInspector:
    return TodoistInspector(staging_app["urls"]["todoist_url"])


@pytest.fixture
def llm(staging_app: dict) -> LLMInspector:
    return LLMInspector(staging_app["urls"]["llm_url"])
