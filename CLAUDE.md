Single-user Telegram + Todoist + Obsidian assistant. Pure async Python (PTB v20+).
Entry point: main.py. Deps: uv. Process: PM2 (ecosystem.config.cjs).
Vault: /home/ubuntu/vault/. State: data/state.json (atomic JSON writes).
Dev: uv run pytest | uv run ruff check . | uv run ruff format . | uv run ty check

Env vars: TELEGRAM_BOT_KEY, TODOIST_KEY, ANTHROPIC_KEY, TELEGRAM_USER_ID
All Todoist and Anthropic SDK calls are sync — wrap with asyncio.to_thread at call sites.
Use app.job_queue for all scheduled jobs (PTB bundles APScheduler via [job-queue] extra).

## Commands
- Test Full: `uv run pytest -m e2e`
- Test Unit: `uv run pytest -m "not e2e"`
- Test File: `uv run pytest tests/e2e/test_postpone_resumption.py -m e2e`
- Lint/Format: `uv run ruff check .` | `uv run ruff format .`
- Debug REPL: `uv run python cli.py`

## CLI (debug client)
`uv run python cli.py` — interactive REPL that mirrors the Telegram bot without auth.
- Type any command (e.g. `/list`, `/completed`, `/task Buy milk`) to test bot responses.
- Type a number to select an inline keyboard button.
- Ctrl-C to quit.
Use the CLI to test bot commands instead of one-off Python scripts.

## Testing
E2E: `uv run pytest -m e2e` — fake Telegram/Todoist/LLM servers + real bot, isolated vault/state.
Unit only: `uv run pytest tests/ --ignore=tests/e2e`

## Logging
Interactions → `data/interactions.jsonl` (auto-rotates 10 MB). Covers commands, callbacks, replies, errors.

## Key API notes
- Todoist completed tasks: use `GET /api/v1/tasks?filter=completed` (the old /tasks/completed/get_all endpoint is gone).
- Handlers live in lib/handlers/. Register new ones in main.py (register_handlers) and add a BotCommand entry.

## Commands index
/task, /list, /done, /completed, /plan, /digest, /optimize, /deepdive, /insights, /project, /brainstorm, /cancel, /session
