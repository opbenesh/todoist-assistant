# Assistant — CLAUDE.md

Single-user Telegram + Todoist + Obsidian assistant. Pure async Python (PTB v20+).

```
Entry point:  main.py
Deps:         uv  (uv sync installs everything)
Process mgr:  PM2  (ecosystem.config.cjs)
Vault:        /home/ubuntu/vault/   (override via VAULT_PATH env var)
State file:   data/state.json       (gitignored, atomic writes)
```

**Dev commands**

```bash
uv run pytest                  # run all tests
uv run ruff check .            # lint
uv run ruff format .           # format
uv run ty check                # type-check (ty, not mypy)
```

## Required env vars

`TELEGRAM_BOT_KEY`, `TODOIST_KEY`, `ANTHROPIC_KEY`, `TELEGRAM_USER_ID`
Optional: `VAULT_PATH` (default `/home/ubuntu/vault`), `OBSIDIAN_POLL_SECONDS` (default `300`), `DEBUG_LOGGING` (default `false`)

## Rules

1. **All Todoist/Anthropic SDK calls are sync** — wrap every call site with `asyncio.to_thread()`. Never block the event loop.
2. **JSON from LLM always validated** — `_strip_fences()` → `json.loads()` → field-by-field with fallbacks. See `propose_enrichment` in `lib/llm.py` for the canonical pattern.
3. **Vault writes must be atomic** — use `lib/obsidian._atomic_write(path, content)` (`.tmp` + rename).
4. **Handler errors** — catch, log at `ERROR`, send user-facing message. Never let exceptions propagate silently.
5. **Scheduler job errors** — catch and log, never re-raise (job must not crash the scheduler).
6. **`WHITELIST_FILTER`** — pass to every `CommandHandler` and every `ConversationHandler` entry point.

## Gotchas

- `lib/handlers/plan.py` imports private `_settings` directly from `lib/scheduler` — use `get_settings()` instead if touching that file.
- `generate_nudge()` in `lib/llm.py` is defined but never called.
- Sync (`lib/sync.py`) matches tasks by title only — duplicate tasks can be created if the same title is edited in both places.
- `UserSettings.work_start` / `work_end` are defined but unused; the planner uses `morning_block`, `afternoon_block`, `evening_block`.
- Vault path is resolved at import time — the vault directory must exist before starting the bot.
