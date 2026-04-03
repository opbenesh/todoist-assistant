Single-user Telegram + Todoist + Obsidian assistant. Pure async Python (PTB v20+).
Entry point: main.py. Deps: uv. Process: PM2 (ecosystem.config.cjs).
Vault: /home/ubuntu/vault/. State: data/state.json (atomic JSON writes).
Dev: uv run pytest | uv run ruff check . | uv run ruff format . | uv run ty check

Env vars: TELEGRAM_BOT_KEY, TODOIST_KEY, ANTHROPIC_KEY, TELEGRAM_USER_ID
All Todoist and Anthropic SDK calls are sync — wrap with asyncio.to_thread at call sites.
Use app.job_queue for all scheduled jobs (PTB bundles APScheduler via [job-queue] extra).
