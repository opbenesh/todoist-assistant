Single-user Telegram + Todoist + Obsidian assistant. Pure async Python (PTB v20+).
Entry point: main.py. Deps: uv. Process: PM2 (ecosystem.config.cjs).
Vault: /home/ubuntu/vault/. State: data/state.json (atomic JSON writes).

Env vars: TELEGRAM_BOT_KEY, TODOIST_KEY, ANTHROPIC_KEY, TELEGRAM_USER_ID
All Todoist and Anthropic SDK calls are sync — wrap with asyncio.to_thread at call sites.
Use app.job_queue for all scheduled jobs (PTB bundles APScheduler via [job-queue] extra).

## Commands
- Test E2E: `uv run pytest -m e2e`
- Test Unit: `uv run pytest -m "not e2e"`
- Lint/Format: `uv run ruff check .` | `uv run ruff format .`
- Debug REPL: `uv run python cli.py`

## CLI (debug client)
`uv run python cli.py` — interactive REPL that mirrors the Telegram bot without auth. Use instead of one-off scripts.

## Testing
**Bug workflow:** add a failing e2e test first, confirm it fails, fix, confirm it passes.

## Logging
Interactions → `data/interactions.jsonl`.

## Key API notes
- Todoist completed tasks: use `GET /api/v1/tasks?filter=completed` (the old /tasks/completed/get_all endpoint is gone).
- Handlers live in lib/handlers/. Register new ones in main.py (register_handlers) and add a BotCommand entry.

## Commands index
/list, /plan, /optimize, /insights, /brainstorm, /cancel, /session
