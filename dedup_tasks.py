#!/usr/bin/env python3
"""One-shot utility: delete duplicate Todoist tasks (same title).

Keeps the task with the most metadata (due date > priority > notes).
Run with: uv run python dedup_tasks.py [--dry-run]
"""
import sys

import lib.todoist as todoist


def _score(t: dict) -> int:
    return (
        int(t.get("due_date") is not None) * 4
        + int(t.get("priority", "p4") != "p4") * 2
        + int(bool(t.get("notes")))
    )


def main(dry_run: bool = False) -> None:
    print("Fetching all tasks…")
    all_tasks = todoist.get_all_tasks()

    by_title: dict[str, list[dict]] = {}
    for t in all_tasks:
        by_title.setdefault(t["title"], []).append(t)

    duplicates = {title: tasks for title, tasks in by_title.items() if len(tasks) > 1}

    if not duplicates:
        print("No duplicates found.")
        return

    deleted = 0
    for title, tasks in duplicates.items():
        tasks.sort(key=_score, reverse=True)
        keeper = tasks[0]
        for dupe in tasks[1:]:
            tag = "[DRY RUN] " if dry_run else ""
            print(f"{tag}Delete '{title}' ({dupe['id']}) — keeping ({keeper['id']})")
            if not dry_run:
                todoist.delete_todoist_task(dupe["id"])
            deleted += 1

    action = "Would remove" if dry_run else "Removed"
    print(f"\n{action} {deleted} duplicate(s) across {len(duplicates)} title(s).")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
