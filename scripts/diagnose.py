#!/usr/bin/env python3
"""
Diagnostic script for the assistant bot.

Usage:
    uv run python scripts/diagnose.py          # quick scan (~5s)
    uv run python scripts/diagnose.py --full   # full scan (~15s)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow importing from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

REPO = Path(__file__).parent.parent
DATA = REPO / "data"
PM2_LOG = Path.home() / ".pm2/logs/assistant-error.log"
AUDIT_LOG = DATA / "audit.jsonl"
INTERACTIONS_LOG = DATA / "interactions.jsonl"
STATE_FILE = DATA / "state.json"
VAULT_DAILY = Path.home() / "vault/Daily"

PHASE_NAMES = {
    0: "BRAINSTORM_PROMPT",
    1: "BS_INPUT",
    2: "BS_REVIEW",
    3: "TRIAGING",
    4: "RESUME_CONFIRM",
    5: "TRIAGE_TIMESLOT",
}


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def _pm2_status() -> None:
    _section("PROCESS")
    try:
        out = subprocess.run(
            ["pm2", "show", "assistant"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        status = uptime = restarts = "?"
        for line in out.splitlines():
            if "│ status" in line:
                status = line.split("│")[2].strip()
            elif "│ uptime" in line:
                uptime = line.split("│")[2].strip()
            elif "│ restarts" in line:
                restarts = line.split("│")[2].strip()
        print(f"assistant: {status}  uptime={uptime}  restarts={restarts}")
    except Exception as exc:
        print(f"pm2 error: {exc}")


def _plan_session() -> None:
    _section("PLAN SESSION")
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        print("state.json unreadable or missing")
        return

    ps = state.get("plan_session")
    if not ps:
        print("no active plan session")
        return

    phase = ps.get("phase", "?")
    phase_name = PHASE_NAMES.get(phase, str(phase))
    sess = ps.get("session", {})
    triage_tasks = sess.get("triage_tasks", [])
    triage_index = sess.get("triage_index", 0)
    last_ts = sess.get("last_user_action_ts")

    age_str = ""
    if last_ts:
        age_min = int((datetime.now(timezone.utc).timestamp() - last_ts) / 60)
        age_str = f"  age={age_min}min"

    print(
        f"phase={phase} ({phase_name})  "
        f"triage_tasks={len(triage_tasks)}  "
        f"triage_index={triage_index}{age_str}"
    )
    if triage_tasks:
        current = triage_tasks[triage_index] if triage_index < len(triage_tasks) else None
        if current:
            print(f"current task: {current.get('title', '?')!r}")


def _plan_log_lines() -> None:
    _section("RECENT PLAN DECISIONS (last 30 [plan/...] log lines)")
    if not PM2_LOG.exists():
        print("log not found:", PM2_LOG)
        return
    try:
        result = subprocess.run(
            ["grep", "-E", r"\[plan|\[sync|ERROR|WARNING", str(PM2_LOG)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = result.stdout.splitlines()[-30:]
        if lines:
            for line in lines:
                # Trim the PM2 date prefix (first two fields) for readability
                parts = line.split(": ", 1)
                print(parts[-1] if len(parts) > 1 else line)
        else:
            print("(no matching log lines)")
    except Exception as exc:
        print(f"grep error: {exc}")


def _audit_tail(n: int = 5) -> None:
    _section(f"LAST {n} AUDIT ENTRIES")
    if not AUDIT_LOG.exists():
        print("audit.jsonl not found")
        return
    try:
        lines = AUDIT_LOG.read_text().splitlines()[-n:]
        for line in lines:
            try:
                e = json.loads(line)
                ts = e.get("ts", "")[-8:-4] + e.get("ts", "")[-4:-1]  # HH:MM
                ts = e.get("ts", "")[11:16]  # HH:MM from ISO string
                action = e.get("action", "?")
                source = e.get("source", "?")
                title = e.get("title", "")
                changes = e.get("changes", {})
                suffix = f"  {changes}" if changes else ""
                print(f"{ts}  {action}  {source}  {title!r}{suffix}")
            except json.JSONDecodeError:
                print(line)
    except Exception as exc:
        print(f"audit read error: {exc}")


def _recent_errors(n: int = 5) -> None:
    _section(f"RECENT ERRORS (last {n})")
    if not INTERACTIONS_LOG.exists():
        print("interactions.jsonl not found")
        return
    try:
        lines = INTERACTIONS_LOG.read_text().splitlines()
        errors = [ln for ln in lines if '"dir": "error"' in ln][-n:]
        if errors:
            for line in errors:
                try:
                    e = json.loads(line)
                    ts = e.get("ts", "")[11:16]
                    exc = e.get("exc", "?")
                    print(f"{ts}  {exc}")
                except json.JSONDecodeError:
                    print(line)
        else:
            print("(none)")
    except Exception as exc:
        print(f"interactions read error: {exc}")


# ---------------------------------------------------------------------------
# Full mode extras
# ---------------------------------------------------------------------------


def _interactions_tail(n: int = 10) -> None:
    _section(f"LAST {n} INTERACTIONS")
    if not INTERACTIONS_LOG.exists():
        print("interactions.jsonl not found")
        return
    try:
        lines = INTERACTIONS_LOG.read_text().splitlines()[-n:]
        for line in lines:
            try:
                e = json.loads(line)
                ts = e.get("ts", "")[11:16]
                direction = e.get("dir", "?")
                if direction == "in":
                    kind = e.get("type", "?")
                    text = e.get("cmd") or e.get("text") or e.get("data") or "?"
                    print(f"{ts}  ← {kind}: {text!r}")
                elif direction == "out":
                    method = e.get("method", "send")
                    text = (e.get("text") or "")[:60]
                    print(f"{ts}  → {method}: {text!r}")
                elif direction == "error":
                    exc = e.get("exc", "?")
                    print(f"{ts}  ERROR: {exc}")
            except json.JSONDecodeError:
                print(line)
    except Exception as exc:
        print(f"interactions read error: {exc}")


def _state_summary() -> None:
    _section("STATE SUMMARY")
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        print("state.json unreadable")
        return
    known = state.get("known_active_ids", {})
    last_sync = state.get("last_sync_ts")
    sync_age = ""
    if last_sync:
        age_min = int((datetime.now(timezone.utc).timestamp() - last_sync) / 60)
        sync_age = f" ({age_min}min ago)"
    print(f"known_active_ids: {len(known)} tasks")
    print(f"last_sync_ts: {last_sync}{sync_age}")
    print(f"sync_cursor: {state.get('sync_cursor', '?')[:20]}...")


def _todoist_list() -> None:
    _section("TODOIST ACTIVE TASKS")
    try:
        os.environ.setdefault("TODOIST_KEY", "")  # will fail gracefully if missing
        import lib.todoist as todoist  # noqa: PLC0415

        tasks = todoist.get_today_tasks()
        if tasks:
            for t in tasks:
                priority = t.get("priority", "p4")
                due = t.get("due_date") or "no date"
                print(f"  [{priority}] {t['title']!r}  due={due}")
        else:
            print("(no tasks due today or overdue)")
    except Exception as exc:
        print(f"todoist error: {exc}")


def _vault_today() -> None:
    _section("TODAY'S VAULT TASKS")
    today = datetime.now().strftime("%Y-%m-%d")
    note = VAULT_DAILY / f"{today}.md"
    if not note.exists():
        print(f"no daily note for {today}")
        return
    text = note.read_text()
    in_tasks = False
    count = 0
    for line in text.splitlines():
        if line.startswith("## Tasks"):
            in_tasks = True
            continue
        if in_tasks:
            if line.startswith("##"):
                break
            if line.strip():
                print(f"  {line}")
                count += 1
    if not count:
        print("(tasks section empty or missing)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    full = "--full" in sys.argv

    _pm2_status()
    _plan_session()
    _plan_log_lines()
    _audit_tail(5)
    _recent_errors(5)

    if full:
        _interactions_tail(10)
        _state_summary()
        _todoist_list()
        _vault_today()

    print()


if __name__ == "__main__":
    main()
