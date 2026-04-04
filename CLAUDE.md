# Assistant — CLAUDE.md

Single-user Telegram + Todoist + Obsidian productivity assistant. Pure async Python (PTB v20+).

## Quick reference

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

---

## Required environment variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_KEY` | Bot token from @BotFather |
| `TODOIST_KEY` | Todoist personal API token |
| `ANTHROPIC_KEY` | Anthropic API key |
| `TELEGRAM_USER_ID` | Your Telegram numeric user ID |

Optional:

| Variable | Default | Description |
|---|---|---|
| `VAULT_PATH` | `/home/ubuntu/vault` | Absolute path to Obsidian vault |
| `OBSIDIAN_POLL_SECONDS` | `300` | How often the sync job runs |
| `DEBUG_LOGGING` | `false` | Set `true` to enable DEBUG log level |

All env vars are loaded from `.env` at startup via `python-dotenv`. The `.env` file is gitignored.

---

## Codebase map

```
main.py                      Bot entry point — wires handlers, scheduler, settings
lib/
  config.py                  Env vars + UserSettings dataclass
  models.py                  Task, EnrichmentState, TaskStore (state.json)
  llm.py                     All Claude API calls (sync helpers, async wrappers)
  todoist.py                 Todoist API v4 client + v1 HTTP for completed tasks
  obsidian.py                Vault I/O — daily notes, digest/plan append, insights
  sync.py                    Obsidian ↔ Todoist bidirectional sync (last-write-wins)
  scheduler.py               APScheduler jobs via PTB's job_queue
  handlers/
    auth.py                  WHITELIST_FILTER — gates all handlers to TELEGRAM_USER_ID
    common.py                /start, /help
    capture.py               /task conversation + /list + /done
    plan.py                  /plan conversation
    digest.py                /digest (one-shot)
    optimize.py              /optimize conversation
    deepdive.py              /deepdive (one-shot)
    insights.py              /insights (one-shot)
    project_plan.py          /project conversation
tests/
  test_config.py
  test_llm.py
  test_obsidian.py
  test_scheduler.py
  test_todoist.py
```

---

## Architecture

### Async/sync boundary

All Todoist SDK and Anthropic SDK calls are **synchronous**. Wrap every call site with `asyncio.to_thread()`:

```python
# correct
result = await asyncio.to_thread(todoist.get_today_tasks)
text = await asyncio.to_thread(llm.propose_task_optimization, task, projects)

# wrong — blocks the event loop
result = todoist.get_today_tasks()
```

Functions in `lib/llm.py` are a mix: async wrappers for multi-step flows (`propose_enrichment`, `generate_plan`, …) that internally call `asyncio.to_thread(_call, …)`, and sync helpers (`propose_task_optimization`, `propose_breakdown`) meant to be called via `asyncio.to_thread` at the call site. The docstring says which convention applies.

### Telegram handlers

All handlers are registered in `main.py`. Every handler uses `WHITELIST_FILTER` (from `lib/handlers/auth.py`) which restricts the bot to a single `TELEGRAM_USER_ID`.

There are two handler patterns in use:

1. **ConversationHandler** — multi-step dialogs with inline keyboard buttons and message input. Used by `/task`, `/plan`, `/optimize`, `/project`.
2. **CommandHandler** — one-shot commands. Used by `/list`, `/done`, `/digest`, `/deepdive`, `/insights`.

### Session state

Multi-step handlers store session data in module-level dicts keyed by `chat_id`:

```python
# capture.py, plan.py, optimize.py — module-level dicts
_sessions: dict[int, SomeState] = {}

# project_plan.py — uses PTB's built-in per-user storage
context.user_data["projects_map"] = projects_map
```

**Note:** The `_sessions` pattern and `context.user_data` are inconsistent across handlers. Prefer `_sessions` for new handlers unless PTB's built-in storage is clearly more convenient.

### Scheduler

Jobs are registered in `lib/scheduler.attach_scheduler()` using PTB's `app.job_queue` (which wraps APScheduler). All jobs run against a single `TELEGRAM_USER_ID`.

| Job | Schedule | Purpose |
|---|---|---|
| `morning_digest_job` | Daily 08:00 (user tz) | Send digest, append to daily note, mark unplanned |
| `plan_reminder_job` | Daily 09:00 (user tz) | Nudge to run /plan if day not yet planned |
| `plan_nag_job` | Hourly 10:00–21:00 (user tz) | Repeat nudge until day is planned |
| `weekly_review_job` | Fridays 20:00 (user tz) | Generate weekly review, save to vault |
| `obsidian_sync_job` | Every `OBSIDIAN_POLL_SECONDS` | Sync daily note ↔ Todoist |

Timezone is loaded once at startup from Todoist API (`get_user_settings`) with fallback to `profile.md`, then to `"UTC"`. A bot restart is required to pick up timezone changes.

User settings are stored in the module-level `_settings` (a `UserSettings` instance) in `lib/scheduler.py`. Access it via `get_settings()` — avoid importing `_settings` directly.

### Obsidian vault layout

```
vault/
  Daily/
    YYYY-MM-DD.md          Today's daily note
  Assistant/
    profile.md             User settings (key: value lines)
    task-guidelines.md     Custom guidelines for /optimize (optional)
    insights-YYYY-MM-DD.md Written by /insights
    weekly-YYYY-MM-DD.md   Written by weekly_review_job
```

The `## Tasks` section of the daily note is the sync boundary. The sync job (`lib/sync.py`) reads this section and reconciles it with Todoist. The day is considered "planned" if the Tasks section has real task lines (anything other than a lone `Unplanned` marker).

**Atomic writes:** All vault writes use a `.tmp` file + `rename` pattern (`lib/obsidian._atomic_write`) to prevent partial writes from corrupting the vault.

### Priority system

Internal priorities (`p1`–`p4`) map to Todoist's numeric priorities as follows:

| Internal | Todoist | Meaning |
|---|---|---|
| `p1` | 4 | Urgent / important |
| `p2` | 3 | High |
| `p3` | 2 | Medium |
| `p4` | 1 | Someday / low (default) |

Mappings live in `lib/models.py`: `PRIORITY_TO_TODOIST`, `TODOIST_TO_PRIORITY`.

### Claude models

Defined as module-level constants in `lib/llm.py`:

```python
HAIKU  = "claude-haiku-4-5-20251001"   # fast, cheap — enrichment, optimize, digest, nudge
SONNET = "claude-sonnet-4-6"           # capable — plan, deepdive, insights, project, review
```

Use Haiku for per-task operations (called in loops). Use Sonnet for whole-day or whole-week analysis.

### Todoist API notes

- `lib/todoist._api` is a module-level `TodoistAPI` singleton (v4).
- Active task queries use the v4 SDK paginator pattern: `for page in paginator: for t in page`.
- **Completed tasks** are fetched via the v1 REST API (`httpx.get`) because the v4 SDK doesn't expose them.
- `_task_to_dict(t)` is the canonical conversion from SDK objects to plain dicts passed to LLM functions.

---

## Key conventions

1. **All sync SDK calls wrapped with `asyncio.to_thread`** at the call site (never in the library function itself, unless the function is already declared `async`).
2. **JSON from LLM always validated** before use. Use `_strip_fences()` to remove code fences, then `json.loads()`, then field-by-field validation with fallbacks (see `propose_enrichment` for the canonical pattern).
3. **Atomic file writes** for anything in the vault. Use `lib/obsidian._atomic_write(path, content)`.
4. **Errors in handlers** are caught, logged at `ERROR` level, and a user-facing message is sent. Don't let exceptions propagate out of handlers silently.
5. **Errors in scheduler jobs** are caught and logged but not re-raised (the job must not crash the scheduler).
6. **`WHITELIST_FILTER`** must be passed to every `CommandHandler` and every `ConversationHandler` entry point.
7. **State persistence** uses `lib/models.TaskStore` (wraps `data/state.json`). Call `.save()` after mutating state.

---

## Known issues / gotchas

- **`lib/handlers/plan.py` imports `_settings` directly** from `lib/scheduler` (`from lib.scheduler import _settings`). This is a private-variable import — use `from lib.scheduler import get_settings` instead if modifying that file.
- **`generate_nudge()` and `generate_weekly_review()`** are defined in `lib/llm.py` but `generate_nudge` is never called by any handler or scheduler job. `generate_weekly_review` is called only by `weekly_review_job`.
- **Sync matches tasks by title only.** If the same task title exists in both Obsidian and Todoist after being independently edited, a duplicate can be created.
- **`UserSettings.work_start` / `work_end`** are defined but currently unused. Time blocks (`morning_block`, `afternoon_block`, `evening_block`) are what the planner uses.
- **`plan_nag_job` hour range** (`9 <= hour < 21`) is evaluated in the user's local timezone, but the check itself happens in the correct tz (line: `hour = datetime.now(tz).hour`).
- **`data/` directory** is created on demand by `TaskStore.save()` — no need to create it manually.
- **Vault must exist** at startup. The import `from lib.obsidian import read_tasks_section` in `main.py` will fail if `VAULT_PATH` doesn't exist and the vault directory is missing, because `obsidian.py` resolves `VAULT = Path(VAULT_PATH)` at import time. Ensure the vault path exists before starting.

---

## Testing

Tests live in `tests/`. Run with `uv run pytest`. All test files use `pytest-asyncio` with `asyncio_mode = "auto"` (no explicit `@pytest.mark.asyncio` needed).

Test coverage by module:

| Module | Status |
|---|---|
| `lib/config.py` | Covered |
| `lib/llm.py` | Covered (enrichment, optimization, parsing helpers) |
| `lib/todoist.py` | Covered (priority mapping, settings merge, task creation) |
| `lib/obsidian.py` | Covered (task line parsing, section writes, sync scenarios) |
| `lib/scheduler.py` | Partially covered — `morning_digest_job`, `weekly_review_job` tested; `plan_nag_job`, `obsidian_sync_job` not |
| `lib/sync.py` | Partially covered — basic sync scenarios |
| `lib/handlers/*` | Not directly tested — tested via integration scenarios in other modules |
| `main.py` | Not tested |

When adding tests, use `unittest.mock.patch` and `AsyncMock` for PTB bot calls and external API calls. See `tests/test_scheduler.py` for examples.

---

## Deployment (PM2)

```bash
pm2 start ecosystem.config.cjs    # start
pm2 restart assistant             # restart
pm2 logs assistant                # tail logs
pm2 stop assistant                # stop
```

Log files: `~/.pm2/logs/assistant-out.log`, `~/.pm2/logs/assistant-error.log`

The `ecosystem.config.cjs` has hardcoded paths (`/home/ubuntu/dev/assistant/`) and enables `DEBUG_LOGGING=true` by default. Adjust for your environment. The `.env` file in the project root is loaded at runtime; secrets are not in `ecosystem.config.cjs`.
