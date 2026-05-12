# Todoist Assistant — System Specification

Complete specification for reimplementing this system from scratch.

---

## 1. Purpose & Scope

A single-user Telegram chatbot that acts as a personal productivity assistant. It orchestrates three external services:

- **Todoist** — task storage and source of truth
- **Obsidian** — human-readable daily notes (bidirectional sync)
- **Claude (Anthropic)** — LLM for enrichment, planning, and analysis

The bot is deployed on a Linux server as a persistent process managed by PM2. There is exactly one authorized user; all other Telegram messages are silently ignored.

---

## 2. Technology Stack

| Layer | Choice |
|-------|--------|
| Language | Python 3.12+ |
| Async framework | `python-telegram-bot` v20+ (PTB), pure async |
| Scheduling | APScheduler via PTB's `[job-queue]` extra |
| LLM client | `anthropic` SDK |
| Todoist client | `todoist-api-python` v3+ SDK + direct `httpx` for REST endpoints the SDK doesn't cover |
| HTTP (low-level) | `httpx` (sync) |
| Package manager | `uv` |
| Process manager | PM2 (`ecosystem.config.cjs`) |
| Testing | `pytest` + `pytest-asyncio` + `pytest-xdist` |
| Linting | `ruff` |

**Runtime dependencies:**
```
python-telegram-bot[job-queue]>=21.0
anthropic>=0.40.0
todoist-api-python>=3.0.0
python-dotenv>=1.0.0
tzdata>=2024.1
requests>=2.32.0
httpx
```

**Dev dependencies:**
```
pytest pytest-asyncio pytest-xdist pytest-env ruff fastapi uvicorn pyinstrument pre-commit
```

---

## 3. Environment Variables

### Required

| Variable | Type | Description |
|----------|------|-------------|
| `TELEGRAM_BOT_KEY` | str | Telegram bot token |
| `TODOIST_KEY` | str | Todoist API key |
| `ANTHROPIC_KEY` | str | Anthropic/Claude API key |
| `TELEGRAM_USER_ID` | int | The one authorized Telegram user ID |

### Optional / Overrides

| Variable | Default | Description |
|----------|---------|-------------|
| `VAULT_PATH` | `/home/ubuntu/vault` | Absolute path to Obsidian vault |
| `OBSIDIAN_POLL_SECONDS` | `300` | Vault sync interval (seconds) |
| `OBSIDIAN_SYNC_FIRST_SECONDS` | `10` | Delay before first vault sync after startup |
| `TODOIST_POLL_SECONDS` | `60` | Todoist fetch/sync interval |
| `TODOIST_POLL_FIRST_SECONDS` | `30` | Delay before first Todoist poll after startup |
| `DEBUG_LOGGING` | `false` | Enable verbose logging |
| `TODOIST_BASE_URL` | `https://api.todoist.com` | Override for test staging |
| `ANTHROPIC_BASE_URL` | *(SDK default)* | Override for test staging |
| `TELEGRAM_API_BASE_URL` | *(PTB default)* | Override for test staging |
| `STATE_PATH` | `data/state.json` | Override state file path |
| `CLI_MODE` | *(unset)* | Set by `cli.py`; switches state to `data/state_cli.json` |

---

## 4. Repository Layout

```
todoist-assistant/
├── main.py                     # Entry point: build app, register handlers, start polling
├── cli.py                      # Debug REPL: mirrors bot without Telegram auth
├── dedup_tasks.py              # One-off utility: remove Todoist duplicate tasks
├── pyproject.toml              # Python project config (deps, pytest, ruff)
├── ecosystem.config.cjs        # PM2 process config
├── CLAUDE.md                   # Developer quick-reference
├── .pre-commit-config.yaml     # Git hooks (pytest + gitleaks)
├── lib/
│   ├── config.py               # Env loading, UserSettings dataclass
│   ├── models.py               # Task, TaskStore, EnrichmentState dataclasses
│   ├── todoist.py              # Todoist API wrapper
│   ├── llm.py                  # Claude API calls + prompt templates
│   ├── obsidian.py             # Vault I/O, daily note parsing/writing
│   ├── scheduler.py            # APScheduler job definitions
│   ├── sync.py                 # Bidirectional Obsidian ↔ Todoist sync
│   ├── audit.py                # Append-only audit log (data/audit.jsonl)
│   ├── interaction_log.py      # LoggedBot subclass + interaction log writer
│   └── handlers/
│       ├── auth.py             # WHITELIST_FILTER (single-user gate)
│       ├── common.py           # /help, /start, /session
│       ├── capture.py          # /list
│       ├── plan.py             # /plan (multi-phase conversation flow)
│       ├── brainstorm.py       # /brainstorm (standalone)
│       ├── unblock.py          # /unblock (standalone)
│       └── insights.py         # /insights
├── scripts/
│   └── diagnose.py             # Bot diagnostics (state, logs, session dump)
├── tests/
│   ├── conftest.py             # Shared fixtures
│   ├── test_config.py
│   ├── test_todoist.py
│   ├── test_obsidian.py
│   ├── test_llm.py
│   ├── test_scheduler.py
│   ├── test_sync.py
│   ├── staging/                # Fake service implementations for E2E
│   │   ├── fake_todoist.py
│   │   ├── fake_llm.py
│   │   ├── fake_telegram.py
│   │   └── seed_data.py
│   └── e2e/
│       ├── conftest.py
│       ├── constants.py
│       ├── helpers.py
│       ├── test_plan.py
│       ├── test_triage.py
│       ├── test_brainstorm.py
│       ├── test_unblock.py
│       ├── test_capture.py
│       ├── test_sync.py
│       ├── test_session.py
│       ├── test_postpone_resumption.py
│       ├── test_insights.py
│       └── test_todoist_sync_back.py
└── data/                       # Runtime state (gitignored)
    ├── state.json              # Persistent TaskStore
    ├── state_cli.json          # CLI-mode state (separate)
    ├── audit.jsonl             # Immutable append-only audit log
    └── interactions.jsonl      # Telegram interaction log
```

---

## 5. Data Models

### 5.1 Task

```python
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

Priority = Literal["p1", "p2", "p3", "p4"]

@dataclass
class Task:
    title: str
    notes: str = ""
    due_date: date | None = None
    priority: Priority = "p4"
    labels: list[str] = field(default_factory=list)
    project: str = "Inbox"
    duration_minutes: int | None = None
    todoist_id: str | None = None
```

### 5.2 UserSettings

```python
@dataclass
class UserSettings:
    timezone: str = "UTC"
    first_day_of_week: int = 0        # 0=Monday, 6=Sunday
    time_format_24h: bool = True
    work_start: str = "09:00"
    work_end: str = "18:00"
    morning_block: str = "09:00-12:00"
    afternoon_block: str = "12:00-17:00"
    evening_block: str = "17:00-21:00"
    default_project: str = "Inbox"
    stale_task_days: int = 3
```

Built from two sources merged in this order (second wins on conflict):
1. Todoist API `GET /api/v1/user` → `timezone` only
2. Obsidian `vault/Assistant/profile.md` → all fields (YAML-like `key: value` lines)

### 5.3 TaskStore

Persistent state. Serialized to/from JSON atomically (write to `.tmp` → `os.replace`).

```python
@dataclass
class TaskStore:
    sync_cursor: str | None = None          # Todoist Sync API cursor
    last_sync_ts: float | None = None       # Unix timestamp of last Obsidian sync
    plan_session: dict | None = None        # Persisted planning session state
    known_active_ids: set[str] = field(default_factory=set)  # Active task IDs snapshot
    unblocked_task_ids: dict[str, float] = field(default_factory=dict)  # id → timestamp (24h TTL)
    quarantine_timestamps: dict[str, float] = field(default_factory=dict)  # id → timestamp
```

Methods:
- `load(path) → TaskStore` — Load from JSON, create if missing
- `save(path)` — Atomic write
- `save_plan_session(phase, session_dict)` — Checkpoint planning session
- `clear_plan_session()` — Discard session

### 5.4 EnrichmentState

Used during interactive task enrichment flows:

```python
@dataclass
class EnrichmentState:
    chat_id: int
    raw_title: str
    task: Task | None = None
    step: str = "awaiting_proposal"
    subtasks: list[str] = field(default_factory=list)
```

---

## 6. Todoist Integration (`lib/todoist.py`)

### 6.1 Clients

- **SDK client**: `TodoistAPI(TODOIST_KEY)` — used for most CRUD
- **HTTP client**: `httpx.Client` with `Authorization: Bearer {key}` — used for:
  - `GET /api/v1/tasks?filter=completed` (SDK doesn't support completed tasks)
  - Sync API (`POST /api/v1/sync`) for bulk operations

The Todoist base URL is overridable via `TODOIST_BASE_URL` for testing (custom `httpx.BaseTransport` that rewrites hostnames).

### 6.2 Priority Mapping

Todoist uses integers 1–4 (4=highest), system uses strings p1–p4 (p1=highest):

```python
PRIORITY_TO_TODOIST = {"p1": 4, "p2": 3, "p3": 2, "p4": 1}
TODOIST_TO_PRIORITY = {4: "p1", 3: "p2", 2: "p3", 1: "p4"}
```

### 6.3 Age & Quarantine Labels

Tasks accumulate labels to track how many times they've been postponed:

- **Age labels**: `age0`, `age1`, `age2`, `age3`
- **Quarantine label**: `quarantined`
- `MAX_TRIAGE_AGE = 3` — after 3 postponements, quarantine option appears

Behavior:
- `bump_task_age(task_id, labels)` — Increments the age label (age0 → age1, etc.)
- `quarantine_task(task_id, labels)` — Adds `quarantined` label, records timestamp in store
- `strip_age_labels(task_id, labels)` — Removes all age/quarantine labels (called on complete or delete)
- `reset_next_project_task_age(project_id)` — Strip age from the first uncompleted task in a project (called after a project task is completed during triage)
- `days_since_quarantined(task_id) → int` — Days since quarantine timestamp in store

### 6.4 API Functions

All sync functions; callers wrap with `asyncio.to_thread()`.

| Function | Endpoint / SDK call | Notes |
|----------|-------------------|-------|
| `get_user_settings()` | `GET /api/v1/user` + profile.md | Cached for session |
| `get_today_tasks()` | SDK `get_tasks(filter="today\|overdue")` | Excludes quarantined |
| `get_triage_tasks()` | SDK `get_tasks(filter="today\|overdue\|no date")` | Deduped by ID |
| `get_overdue_tasks()` | SDK `get_tasks(filter="overdue")` | |
| `get_all_tasks()` | SDK `get_tasks()` | Deduped by ID |
| `get_completed_tasks(since_days=7)` | `GET /api/v1/tasks?filter=completed` | since_days controls lookback |
| `get_quarantined_tasks()` | SDK `get_tasks(filter="label:quarantined")` | |
| `create_todoist_task(task, parent_id, project_id)` | SDK `add_task(...)` | Updates `store.known_active_ids` |
| `create_todoist_project(name)` | SDK `add_project(name=name)` | Returns project ID |
| `complete_todoist_task(task_id)` | SDK `close_task(task_id)` | Removes from `store.known_active_ids` |
| `uncomplete_todoist_task(task_id)` | SDK `reopen_task(task_id)` | |
| `update_todoist_task(task_id, **kwargs)` | SDK `update_task(task_id, **kwargs)` | kwargs: priority, due_string, due_datetime, labels, description, duration, duration_unit |
| `delete_todoist_task(task_id)` | SDK `delete_task(task_id)` | Removes from `store.known_active_ids` |
| `batch_update_task_status(complete_ids, uncomplete_ids)` | Sync API `POST /api/v1/sync` | Chunks of 100 commands |
| `remove_task_due_date(task_id)` | Sync API (item_update command with `due: null`) | REST ignores `due_string=""` |
| `get_all_projects()` | SDK `get_projects()` | Returns `{id: name}`, cached 5 min |
| `get_projects_info()` | SDK `get_projects()` | Returns `({id: name}, inbox_id)`, cached |

### 6.5 Recurring Task Handling

During triage, recurring tasks cannot be deleted. When the user picks "Postpone" on a recurring task, the task is instead marked complete (which Todoist reschedules to the next occurrence).

---

## 7. LLM Integration (`lib/llm.py`)

### 7.1 Models

```python
HAIKU = "claude-haiku-4-5-20251001"   # Fast, enrichment/extraction
SONNET = "claude-sonnet-4-6"           # Slower, analysis/review
```

### 7.2 Client Setup

```python
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, base_url=ANTHROPIC_BASE_URL)
```

Prompt caching is enabled on system messages via `{"type": "ephemeral"}` cache control.

### 7.3 LLM Functions

All async. Return parsed Python objects (dicts, lists, or strings).

#### `propose_enrichment(raw_title: str) → Task`
Model: Haiku. Takes a raw user-typed task title. Returns an enriched `Task` dataclass with suggested title cleanup, notes, due_date, priority, labels, duration_minutes. Reads task-guidelines.md from vault as part of system prompt.

System prompt structure:
```
You are a task enrichment assistant. Today is {date}. User timezone: {tz}.
Task guidelines: {guidelines}
Return JSON: {title, notes, due_date (YYYY-MM-DD or null), priority (p1-p4),
              labels (list[str]), duration_minutes (int or null)}
```

#### `propose_task_optimization(task: Task, projects: dict) → dict`
Model: Haiku. Takes an existing task. Returns a dict of suggested improvements: `{title?, priority?, project?, labels?, due_date?}`. Only changed fields included.

#### `generate_digest(tasks: list[Task]) → str`
Model: Haiku. Returns a short Markdown morning digest (2–4 sentences) summarizing the day's tasks.

#### `generate_weekly_review(completed: list[Task], overdue: list[Task]) → str`
Model: Sonnet. Returns a Markdown weekly review with accomplishments, patterns, and suggestions.

#### `generate_nudge(overdue: list[Task]) → str`
Model: Haiku. Returns a short motivating message (1–2 sentences) about overdue tasks.

#### `generate_deepdive(task: Task) → str`
Model: Sonnet. Returns a detailed Markdown analysis of a single task (what it entails, blockers, suggested next actions).

#### `generate_project_plan(project_name: str, tasks: list[Task]) → str`
Model: Sonnet. Returns a Markdown project plan.

#### `brainstorm_extract_tasks(text: str) → list[str]`
Model: Haiku. Extracts a list of actionable task titles from free-form brainstorm text.

System prompt: instructs model to return a JSON array of concise task title strings.

#### `generate_insights(completed, overdue, all_active, quarantined) → str`
Model: Sonnet. Returns a Markdown insights report with productivity patterns, blockers, and recommendations. Input spans 14 days of completed tasks.

#### `breakdown_tasks_for_unblock(original_task: Task, user_plan: str) → dict`
Model: Haiku. Takes a quarantined task + user's rough plan text. Returns:
```json
{
  "project_slug": "short-kebab-case-name",
  "tasks": ["Subtask 1", "Subtask 2", ...]
}
```

### 7.4 Utility Functions

- `_strip_fences(text: str) → str` — Remove ` ```json ` / ` ``` ` wrapping from LLM output
- `restore_links(original: str, processed: str) → str` — Re-inject Markdown links that the LLM dropped from processed text
- All JSON parses logged on failure before raising

---

## 8. Obsidian Integration (`lib/obsidian.py`)

### 8.1 Vault Structure

```
vault/
├── Daily/
│   └── YYYY-MM-DD.md           # One file per day
└── Assistant/
    ├── profile.md              # User settings (YAML-like)
    ├── task-guidelines.md      # Task hygiene rules (read by LLM)
    ├── insights-YYYY-MM-DD.md  # Auto-generated insights reports
    └── weekly-YYYY-MM-DD.md    # Auto-generated weekly reviews
```

### 8.2 Daily Note Format

Each daily note may contain a `## Tasks` section. The sync engine reads and writes only this section; the rest of the file is untouched.

```markdown
## Tasks
- [ ] Unscheduled task title 🔴  (30min)
- [x] Completed task

### 🌅 Morning
- [ ] [ProjectName] Morning task 🟠
- [ ] Another morning task

### ☀️ Afternoon
- [ ] Afternoon task

### 🌙 Evening
- [ ] Evening task
```

**Task line format:**
```
- [checkbox] [ProjectName] title priorityEmoji  (Nmin)
```

- `checkbox`: `[ ]` or `[x]`
- `[ProjectName]`: optional prefix, omitted for Inbox tasks
- `priorityEmoji`: `🔴` (p1), `🟠` (p2), `🟡` (p3), omitted for p4
- `(Nmin)`: optional duration suffix

### 8.3 Section Organization

Tasks in the `## Tasks` section are organized into subsections based on time slots. Within each subsection, tasks are ordered p1 → p4.

| Subsection | Condition |
|-----------|-----------|
| *(top-level, no header)* | No due time, or due time outside work blocks |
| `### 🌅 Morning` | Due time within `morning_block` |
| `### ☀️ Afternoon` | Due time within `afternoon_block` |
| `### 🌙 Evening` | Due time within `evening_block` |

### 8.4 I/O Functions

All sync; callers wrap with `asyncio.to_thread()`.

| Function | Description |
|----------|-------------|
| `read_tasks_section(day=None) → list[str]` | Return raw task lines from ## Tasks section of given day's note (today if None) |
| `write_tasks_section(task_lines, day=None)` | Overwrite ## Tasks section atomically |
| `parse_task_line(line) → tuple[str, bool] \| None` | Parse `- [x] title` → `(clean_title, is_checked)`. Returns None if not a task line. Strips duration suffix, priority emoji, project prefix. |
| `is_day_planned(day=None) → bool` | True if the day's note has real tasks (not just an "Unplanned" marker) |
| `read_task_guidelines() → str` | Read `vault/Assistant/task-guidelines.md`, return content |
| `write_insight(filename, content)` | Write content to `vault/Assistant/{filename}` atomically |
| `format_task_line(title, checked, priority, duration, project) → str` | Render a task line in canonical format |
| `build_tasks_section(tasks, settings) → list[str]` | Build full ordered list of task lines from Task objects |

### 8.5 profile.md Format

```
timezone: America/New_York
first_day_of_week: 0
time_format_24h: true
work_start: 08:00
work_end: 18:00
morning_block: 08:00-12:00
afternoon_block: 12:00-17:00
evening_block: 17:00-21:00
default_project: Work
stale_task_days: 3
```

Parsed as `key: value` pairs; lines not matching the pattern are ignored.

---

## 9. Sync Engine (`lib/sync.py`)

### 9.1 Overview

Bidirectional sync between today's Obsidian daily note `## Tasks` section and Todoist tasks due today. Runs on a repeating schedule (`TODOIST_POLL_SECONDS`).

**Primary invariant**: the last writer wins. Obsidian wins over Todoist if the daily note was modified since the last sync (`mtime > store.last_sync_ts`).

### 9.2 Sync Algorithm

Entry point: `run_sync(store, settings, bot, chat_id)`

```
1. Load today's Obsidian task lines (parse_task_line for each)
2. Load Todoist tasks due today (get_today_tasks)
3. Compute note_modified = daily_note.mtime > store.last_sync_ts
4. Match tasks by title (case-insensitive, stripped)
5. For each Obsidian line:
   a. If title not in Todoist: create in Todoist (unless checked = skip)
   b. If title in Todoist AND note_modified:
      - If obs_checked AND NOT todo_completed: complete in Todoist
      - If NOT obs_checked AND todo_completed: uncomplete in Todoist
6. For each Todoist task not in Obsidian:
   a. If completed: write checked line to Obsidian
   b. If active: append unchecked line to Obsidian
7. For each Todoist task in both with note_modified=False:
   - If todo_completed AND obs_unchecked: check line in Obsidian (Todoist wins)
8. Collect all completes and uncompletes; send via batch_update_task_status
9. Rewrite Obsidian tasks section with updated lines
10. Update store.last_sync_ts = now
```

### 9.3 Edge Cases

- **Rescheduled tasks**: If a Todoist task's due date was moved to a future day, it disappears from today's Todoist results. The Obsidian line is left as-is (not deleted).
- **Recurring tasks**: Completing them in Todoist reschedules; the Obsidian line stays checked.
- **Quarantined tasks**: `get_today_tasks()` excludes them, so they never appear in today's note.
- **Batch operations**: All complete/uncomplete calls are batched into a single Sync API request in chunks of ≤100 commands.

---

## 10. Scheduler (`lib/scheduler.py`)

All jobs registered via `app.job_queue` (PTB's APScheduler wrapper). Jobs run in user's local timezone.

### 10.1 Job Definitions

| Job | Type | Schedule | Action |
|-----|------|----------|--------|
| `weekly_review_job` | `run_monthly` (weekly) | Friday 20:00 user-TZ | Generate weekly review (Sonnet), write to vault, send to user |
| `plan_reminder_job` | `run_daily` | 09:00 user-TZ | If day not planned, send reminder message |
| `plan_nag_job` | `run_repeating` | Hourly starting 10:00 | Nag logic (see below) |
| `obsidian_sync_job` | `run_repeating` | Every `OBSIDIAN_POLL_SECONDS` | `run_sync()` |
| `todoist_sync_job` | `run_repeating` | Every `TODOIST_POLL_SECONDS` | Check for external Todoist changes (tasks added/deleted outside bot) |

### 10.2 Plan Nag Logic

```
if plan session active:
    if time_since_last_action > 15 minutes AND NOT session.nudge_sent:
        send "Resume your planning session?" nudge
        session.nudge_sent = True
    else:
        suppress nag (user is actively planning)
elif day is not planned:
    send normal nag message
```

---

## 11. Command Handlers

Authentication: all handlers are wrapped with `WHITELIST_FILTER` that checks `update.effective_user.id == TELEGRAM_USER_ID`. Non-matching updates are silently dropped.

### 11.1 /start, /help, /session (`lib/handlers/common.py`)

- `/start` — Reply "Assistant ready."
- `/help` — Reply with formatted list of all commands
- `/session` — Reply with current `store.plan_session` serialized (debug)

### 11.2 /list (`lib/handlers/capture.py`)

Fetch today's + overdue tasks from Todoist. Format and send:

```
1. ○ Task title [P2] — 2026-05-12  `abc123`
2. ✓ Completed task
```

Symbols: `○` = active, `✓` = completed. Priority shown if p1/p2/p3. Task ID shown for debugging.

### 11.3 /insights (`lib/handlers/insights.py`)

1. `get_completed_tasks(since_days=14)` + `get_overdue_tasks()` + `get_all_tasks()` + `get_quarantined_tasks()`
2. `generate_insights(...)` (Sonnet)
3. Write to `vault/Assistant/insights-YYYY-MM-DD.md`
4. Send Markdown report to user

### 11.4 /brainstorm (`lib/handlers/brainstorm.py`)

Standalone brainstorm session:

```
User: /brainstorm
Bot: "What's on your mind? Dump everything."
User: [free-form text]
Bot: [Shows first proposed task with Accept/Reject buttons]
User: Accept → creates in Todoist (due today), shows next proposal
User: Reject → skips, shows next proposal
... until all proposals exhausted
Bot: "Done. Created N tasks."
```

### 11.5 /unblock (`lib/handlers/unblock.py`)

Standalone unblock session for quarantined tasks:

```
User: /unblock
Bot: [List of quarantined tasks with inline keyboard]
User: [Picks a task]
Bot: "Break down or mark done?"
User: Break down →
    Bot: "What's your rough plan?"
    User: [Text]
    Bot: LLM breakdown → shows subtask proposals
    User: Accepts/rejects each
    Bot: Creates accepted subtasks in Todoist (new project named project_slug)
         Deletes original quarantined task
User: Mark done →
    Bot: Completes task immediately
```

### 11.6 /plan (`lib/handlers/plan.py`)

The core command. Multi-phase guided planning conversation.

#### Phase 0: Brainstorm Prompt

```
Bot: "Ready to plan your day! Start with a brain dump?"
Buttons: [▶ Start] [⏭ Skip]
```

- "▶ Start" → Phase 1 (BS_INPUT)
- "⏭ Skip" → Check for quarantined tasks → Phase 3 (UNBLOCK_PROMPT) or Phase 4 (TRIAGING)

#### Phase 1: BS_INPUT

```
Bot: "Go ahead — dump everything on your mind."
User: [Free-form text]
Bot: Calls brainstorm_extract_tasks(text) → Phase 2
```

#### Phase 2: BS_REVIEW

Shows each extracted task one at a time:

```
Bot: "How about: 'Draft Q3 report'?"
Buttons: [✅ Accept] [❌ Reject] [➕ More] [▶ Next step]
```

- "✅ Accept" → `create_todoist_task` (due today), advance to next proposal
- "❌ Reject" → Skip, advance to next proposal
- "➕ More" → Return to Phase 1 (accept more brainstorm input)
- "▶ Next step" → When no proposals left, check quarantined → Phase 3 or 4

#### Phase 3: UNBLOCK_PROMPT (optional)

Only shown if `get_quarantined_tasks()` is non-empty.

```
Bot: "You have N quarantined tasks. Want to unblock any?"
Buttons: [☣️ Unblock] [⏭ Skip]
```

- "☣️ Unblock" → UNBLOCK_PICKING: show list of quarantined tasks
- "⏭ Skip" → Phase 4

UNBLOCK_PICKING → same sub-flow as /unblock handler. After completing, offer "Another?" or "Continue to triage."

#### Phase 4: TRIAGING

Main triage loop. Fetch `get_triage_tasks()` (today | overdue | no-date tasks). Filter:
- Skip tasks already processed in this session
- For non-Inbox projects: only show the **first** incomplete task per project (sequential)

For each task:
```
Bot: "📋 [ProjectName] Task title
      Labels: waiting, age2 | Priority: P3 | Due: 2026-05-10
      3 of 12 tasks"
Buttons: [P1] [P2] [P3] [✅ Done] [⏸ Postpone] [🚫 Quarantine] [🗑 Delete]
```

Note: "🚫 Quarantine" only appears if task has `age2` or `age3` label.

After user picks priority (P1/P2/P3):
```
Bot: "Which time slot?"
Buttons: [🌅 Morning] [☀️ Afternoon] [🌙 Evening] [No slot]
```
→ `update_todoist_task` with priority + due_datetime for chosen slot.

Other actions:
- **✅ Done**: `complete_todoist_task` + `strip_age_labels`
- **⏸ Postpone**: 
  - If recurring: `complete_todoist_task` (reschedules)
  - Else: `remove_task_due_date`
  - Do NOT bump age here; age is bumped at session end for all postponed tasks
- **🚫 Quarantine**: `quarantine_task` (only if age >= MAX_TRIAGE_AGE=3)
- **🗑 Delete**: `delete_todoist_task` + `strip_age_labels` (blocked for recurring tasks)

After all tasks processed, bump age for all postponed tasks (`bump_task_age` for each).

Finish:
1. `build_tasks_section` → `write_tasks_section` (update daily note)
2. Send summary:
   ```
   ✅ Plan complete!
   Scheduled: 4 tasks
   Postponed: 2 | Done: 1 | Quarantined: 0
   ```

#### Session Persistence

At each phase transition, `store.save_plan_session(phase, session_dict)` is called. On `/plan`:
1. Check if `store.plan_session` exists and was saved today
2. If yes: resume from saved phase (show "Resuming..." message)
3. If no: start fresh

Session expires if from a previous calendar day. A "stale session" message is shown and the session is cleared.

Session timeout (within phase): 600 seconds of inactivity. Callback queries that arrive after timeout get an error reply.

#### PlanFlowSession Fields

```python
# Brainstorm state
bs_proposed: list[str]       # LLM-extracted task titles
bs_index: int                # Current proposal index
bs_created: list[str]        # Accepted task IDs

# Triage state  
triage_tasks: list[Task]     # Tasks to triage (ordered)
triage_index: int            # Current task index
triage_processed: set[str]   # Task IDs processed this session
triage_pending_priority: str | None  # Priority awaiting timeslot pick

# Projects (ephemeral, not persisted)
projects: dict[str, str]     # project_id → project_name
project_incomplete_counts: dict[str, int]  # project_id → count

# Resumption tracking
last_user_action_ts: float
nudge_sent: bool

# Unblock sub-session (not persisted)
ub_tasks: list[Task]
ub_task: Task | None
ub_proposals: list[str]
ub_index: int
```

---

## 12. Audit Log (`lib/audit.py`)

Append-only JSONL file at `data/audit.jsonl`. Every persistent mutation is recorded.

### Record Schema

```json
{
  "ts": "2026-05-12T14:30:00.123Z",
  "action": "complete",
  "source": "plan/triage",
  "trigger": "user_accept",
  "task_id": "abc123",
  "title": "Draft Q3 report",
  "changes": {"priority": "p2"},
  "extra": {}
}
```

**action values**: `create`, `update`, `complete`, `uncomplete`, `delete`, `remove_due_date`

**source values** (examples): `plan/brainstorm`, `plan/triage`, `plan/unblock`, `brainstorm`, `unblock`, `sync`, `insights`

**trigger values** (examples): `user_accept`, `user_reject`, `user_done`, `user_postpone`, `user_quarantine`, `auto`, `obsidian_checked`, `todoist_completed`

---

## 13. Interaction Log (`lib/interaction_log.py`)

Rotated JSONL file at `data/interactions.jsonl`. Max 10 MB; rotates to `interactions.jsonl.1`.

### LoggedBot

Subclass of PTB's `ExtBot`. Overrides:
- `send_message` — logs outgoing
- `edit_message_text` — logs outgoing
- `answer_callback_query` — logs outgoing

Handler middleware logs:
- Incoming commands: `{type: "command", text: "/plan", update_id: 123}`
- Incoming messages: `{type: "message", text: "...", update_id: 123}`
- Incoming callbacks: `{type: "callback", data: "triage_done_abc123", update_id: 123}`
- Outgoing: `{method: "send_message", text: "...", chat_id: 456}`
- Errors: `{type: "error", exception: "...", traceback: "..."}`

---

## 14. Entry Point (`main.py`)

```python
async def main():
    settings = await asyncio.to_thread(load_user_settings)
    store = TaskStore.load(STATE_PATH)
    
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_KEY)
        .base_url(TELEGRAM_API_BASE_URL)
        .bot_class(LoggedBot)
        .build()
    )
    
    setup_scheduler(app, settings, store)
    register_handlers(app, store, settings)
    
    await app.run_polling(drop_pending_updates=True)

def register_handlers(app, store, settings):
    app.add_handler(CommandHandler("start", start_handler, filters=WHITELIST_FILTER))
    app.add_handler(CommandHandler("help", help_handler, filters=WHITELIST_FILTER))
    app.add_handler(CommandHandler("session", session_handler, filters=WHITELIST_FILTER))
    app.add_handler(CommandHandler("list", list_handler, filters=WHITELIST_FILTER))
    app.add_handler(CommandHandler("plan", plan_handler, filters=WHITELIST_FILTER))
    app.add_handler(CommandHandler("brainstorm", brainstorm_handler, filters=WHITELIST_FILTER))
    app.add_handler(CommandHandler("unblock", unblock_handler, filters=WHITELIST_FILTER))
    app.add_handler(CommandHandler("insights", insights_handler, filters=WHITELIST_FILTER))
    app.add_handler(CallbackQueryHandler(plan_callback, pattern=r"^plan_"))
    app.add_handler(CallbackQueryHandler(brainstorm_callback, pattern=r"^bs_"))
    app.add_handler(CallbackQueryHandler(unblock_callback, pattern=r"^ub_"))
    app.add_handler(MessageHandler(
        WHITELIST_FILTER & filters.TEXT & ~filters.COMMAND,
        message_router  # Routes free-text to active conversation session
    ))
    app.add_error_handler(error_handler)
```

### BotCommand list (registered with Telegram)

```
/list      — Show today's tasks
/plan      — Start planning session
/unblock   — Unblock a quarantined task
/insights  — Generate productivity insights
/brainstorm — Capture new tasks
/cancel    — Cancel current session
/session   — Show session state (debug)
```

---

## 15. CLI Debug Client (`cli.py`)

Interactive REPL that mirrors the bot without Telegram:

```
$ uv run python cli.py
> /plan
[Bot]: Ready to plan your day! ...
> ▶ Start
[Bot]: Go ahead — dump everything...
> fix the login bug, call dentist, review PRs
[Bot]: How about: 'Fix login bug'?
> accept
...
```

Uses `CliBot` (prints to stdout). State stored in `data/state_cli.json`. All handlers are the same code paths.

---

## 16. Deployment (`ecosystem.config.cjs`)

```javascript
module.exports = {
  apps: [{
    name: "assistant",
    script: "/home/ubuntu/dev/assistant/.venv/bin/python",
    args: "main.py",
    cwd: "/home/ubuntu/dev/assistant",
    watch: false,
    max_restarts: 10,
    restart_delay: 3000,
    error_file: "/home/ubuntu/.pm2/logs/assistant-error.log",
    out_file: "/home/ubuntu/.pm2/logs/assistant-out.log",
    env: {
      // Env vars injected from shell or .env file
    }
  }]
}
```

---

## 17. Testing

### Test Markers

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "-m 'not e2e' -n auto"
markers = ["e2e: end-to-end tests against fake services"]
```

- Default run (`uv run pytest`): unit tests only, parallelized
- E2E run: `uv run pytest -m e2e`

### Unit Tests

Cover individual modules in isolation using mocks:
- `test_config.py` — `UserSettings` construction from env vars and profile.md
- `test_todoist.py` — Task CRUD, age label logic, priority mapping
- `test_obsidian.py` — Daily note parsing, task line formatting, section building
- `test_llm.py` — Prompt construction, JSON parsing, fence stripping
- `test_sync.py` — Sync resolution logic (all table rows from §9.2)
- `test_scheduler.py` — Job timing, nag suppression

### E2E Tests

Use fake service implementations started as local servers:
- `tests/staging/fake_todoist.py` — FastAPI server mimicking Todoist API v1 + Sync API
- `tests/staging/fake_llm.py` — Returns deterministic canned responses
- `tests/staging/fake_telegram.py` — Records outgoing messages; simulates incoming
- `tests/staging/seed_data.py` — Standard fixture data (tasks, projects)

Tests (`tests/e2e/`):
- `test_plan.py` — Full /plan happy path
- `test_triage.py` — Each triage action (done, postpone, quarantine, delete, priority+slot)
- `test_brainstorm.py` — /brainstorm accept/reject flow
- `test_unblock.py` — /unblock breakdown and mark-done flows
- `test_capture.py` — /list output
- `test_sync.py` — Bidirectional sync scenarios
- `test_session.py` — Session persistence + resumption after restart
- `test_postpone_resumption.py` — Age bumping after postpone
- `test_insights.py` — /insights output + vault write
- `test_todoist_sync_back.py` — Changes made in Todoist propagate to Obsidian

### Fixtures (`tests/conftest.py`)

- `mock_todoist_api` — Patches `TodoistAPI` with in-memory store
- `tmp_vault` — Temporary directory with vault structure
- `tmp_state` — Temporary state file

---

## 18. Design Invariants

1. **Single user only.** `WHITELIST_FILTER` gates every handler. There is no multi-tenancy.

2. **Sync, not async, at I/O boundaries.** All Todoist, LLM, and file I/O is synchronous. Every call site wraps with `asyncio.to_thread()`. Never call sync blocking code directly in an async handler.

3. **Atomic writes everywhere.** State JSON and Obsidian files are written via `write tmp → os.replace`. No partial writes.

4. **Quarantine is the escape valve.** Tasks that are repeatedly deferred accumulate age labels (`age0`–`age3`). At max age they can be quarantined. Quarantined tasks are invisible to normal planning until explicitly unblocked via `/unblock` or the unblock phase of `/plan`.

5. **Project-sequential triage.** During /plan triage, only the first incomplete task per non-Inbox project is shown. This enforces working through projects sequentially rather than cherry-picking.

6. **Obsidian wins on conflict.** If the daily note has been modified since the last sync, Obsidian's checked state takes precedence over Todoist's completion state.

7. **Session persistence across restarts.** The planning session is checkpointed to `state.json` at each phase boundary. A restart mid-session resumes correctly on the next `/plan` command (same day only).

8. **No in-memory state between turns.** Conversation context lives in `state.json` and Telegram callback data. The bot can restart between any two user messages without losing progress.

9. **Prompt caching enabled.** System prompts use Anthropic's ephemeral cache to reduce latency and cost on repeated calls.

10. **Audit log is immutable.** `audit.jsonl` is append-only. Never modify or delete entries.
