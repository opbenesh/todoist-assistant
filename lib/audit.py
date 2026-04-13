"""WAL-style audit log for all persistent state mutations.

Every Todoist mutation (create/update/complete/uncomplete/delete/remove_due_date)
is recorded as a JSON line in data/audit.jsonl. Entries are also emitted via
the standard logger at INFO level so they appear in PM2 logs and are grep-able.

Entry fields:
  ts         — ISO-8601 UTC timestamp with millisecond precision
  action     — mutation type: create | update | complete | uncomplete | delete | remove_due_date
  source     — which handler/phase triggered it (e.g. "plan/triage", "sync")
  trigger    — what caused the action (e.g. "user_accept", "auto", "obsidian_checked")
  task_id    — Todoist task ID (empty string for creates before ID is known)
  title      — task content at time of mutation
  changes    — dict of changed fields (for update actions)
  extra      — any additional context fields passed as kwargs
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

_LOG_PATH = Path(__file__).parent.parent / "data" / "audit.jsonl"
logger = logging.getLogger(__name__)


def log(
    action: str,
    *,
    source: str,
    trigger: str,
    task_id: str = "",
    title: str = "",
    changes: dict | None = None,
    **extra: object,
) -> None:
    """Append one audit entry. Never raises — errors are swallowed after logging."""
    entry: dict = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "action": action,
        "source": source,
        "trigger": trigger,
        "task_id": task_id,
        "title": title,
    }
    if changes:
        entry["changes"] = changes
    entry.update(extra)

    line = json.dumps(entry, ensure_ascii=False)
    logger.info("[AUDIT] %s", line)
    try:
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:
        logger.error("audit log write failed: %s", exc)
