from __future__ import annotations

import asyncio
import logging

import lib.audit as audit
import lib.todoist as todoist
from lib.llm import restore_links

logger = logging.getLogger(__name__)


async def apply_actionable_label(r: dict, task_by_id: dict, source: str) -> None:
    """Add 'actionable' label and apply clean_title for a judged task result."""
    original = task_by_id.get(r["id"], {})
    labels = list(original.get("labels") or [])
    if "actionable" not in labels:
        labels.append("actionable")
    kwargs: dict = {"labels": labels}
    clean = r.get("clean_title")
    if clean:
        kwargs["content"] = restore_links(original.get("title", ""), clean)
    try:
        await asyncio.to_thread(todoist.update_todoist_task, r["id"], **kwargs)
        audit.log(
            "update",
            source=source,
            trigger="auto",
            task_id=r["id"],
            title=original.get("title", ""),
            changes=kwargs,
        )
    except Exception as exc:
        logger.error("[%s] auto-label failed for %s: %s", source, r["id"], exc)
