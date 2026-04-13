Single-user Telegram + Todoist + Obsidian assistant. Pure async Python (PTB v20+).
Entry point: main.py. Deps: uv. Process: PM2 (ecosystem.config.cjs).
Vault: /home/ubuntu/vault/. State: data/state.json (atomic JSON writes).
Dev: uv run pytest | uv run ruff check . | uv run ruff format . | uv run ty check

Env vars: TELEGRAM_BOT_KEY, TODOIST_KEY, ANTHROPIC_KEY, TELEGRAM_USER_ID
All Todoist and Anthropic SDK calls are sync — wrap with asyncio.to_thread at call sites.
Use app.job_queue for all scheduled jobs (PTB bundles APScheduler via [job-queue] extra).

## CLI (debug client)
`uv run python cli.py` — interactive REPL that mirrors the Telegram bot without auth.
- Type any command (e.g. `/list`, `/completed`, `/task Buy milk`) to test bot responses.
- Type a number to select an inline keyboard button.
- Ctrl-C to quit.
Use the CLI to test bot commands instead of one-off Python scripts.

## Key API notes
- Todoist completed tasks: use `GET /api/v1/tasks?filter=completed` (the old /tasks/completed/get_all endpoint is gone).
- Handlers live in lib/handlers/. Register new ones in main.py (register_handlers) and add a BotCommand entry.

## Commands index
/task, /list, /done, /completed, /plan, /digest, /optimize, /deepdive, /insights, /project, /brainstorm
