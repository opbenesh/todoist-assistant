from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from lib.config import VAULT_PATH
from lib.models import DEFAULT_PRIORITY

VAULT = Path(VAULT_PATH)
_TASK_LINE_RE = re.compile(r"^- \[([ x])\] (.+)$")

PRIORITY_EMOJI: dict[str, str] = {"p1": "🔴", "p2": "🟠", "p3": "🟡", "p4": ""}
_PRIORITY_ORDER: dict[str, int] = {"p1": 0, "p2": 1, "p3": 2, "p4": 3}
_SECTION_ORDER: list[str] = ["Unscheduled", "Morning", "Afternoon", "Evening"]
_SECTION_HEADER: dict[str, str] = {
    "Morning": "### 🌅 Morning",
    "Afternoon": "### ☀️ Afternoon",
    "Evening": "### 🌙 Evening",
}


def daily_note_path(day: date | None = None) -> Path:
    day = day or date.today()
    return VAULT / "Daily" / f"{day.isoformat()}.md"


def read_tasks_section(day: date | None = None) -> list[str]:
    """Return raw task lines from the ## Tasks section of today's daily note."""
    try:
        return _extract_task_lines(daily_note_path(day).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def _extract_task_lines(text: str) -> list[str]:
    match = re.search(r"^## Tasks[^\n]*(?:\n|$)", text, flags=re.MULTILINE)
    if not match:
        return []

    start_pos = match.end()
    next_header_match = re.search(r"^## ", text[start_pos:], flags=re.MULTILINE)

    if next_header_match:
        section_text = text[start_pos : start_pos + next_header_match.start()]
    else:
        section_text = text[start_pos:]

    return [line for line in section_text.splitlines() if line.strip()]


def parse_task_line(line: str) -> tuple[str, bool] | None:
    """Parse '- [ ] Title' or '- [x] Title' into (title, is_checked).

    Returns None if line doesn't match the task format.
    """
    m = _TASK_LINE_RE.match(line.strip())
    if not m:
        return None
    checked = m.group(1) == "x"
    # Strip trailing metadata like (30min) or (p2, 15min)
    title = re.sub(r"\s*\([^)]+\)\s*$", "", m.group(2)).strip()
    # Strip [ProjectName] prefix if present
    title = re.sub(r"^\[[^\]]+\]\s*", "", title)
    # Strip priority emoji suffix (🔴🟠🟡) if present
    title = re.sub(r"\s*[🔴🟠🟡]\s*$", "", title).strip()
    return title, checked


def format_task_line(
    title: str,
    checked: bool,
    priority: str = DEFAULT_PRIORITY,
    duration: int | None = None,
    project: str | None = None,
) -> str:
    check = "x" if checked else " "
    prefix = f"[{project}] " if project else ""
    emoji = PRIORITY_EMOJI.get(priority, "")
    emoji_suffix = f" {emoji}" if emoji else ""
    meta_parts = []
    if duration:
        meta_parts.append(f"{duration}min")
    meta = f"  ({', '.join(meta_parts)})" if meta_parts else ""
    return f"- [{check}] {prefix}{title}{emoji_suffix}{meta}"


def _time_section(
    due_date_iso: str | None,
    morning_block: str,
    afternoon_block: str,
    evening_block: str,
) -> str:
    """Classify a task's due_date ISO string into a section name.

    Uses the same block boundaries as the /plan session (from UserSettings).
    Returns one of: 'Unscheduled', 'Morning', 'Afternoon', 'Evening'.
    Times outside all three blocks → 'Unscheduled'.
    """
    if not due_date_iso or "T" not in due_date_iso:
        return "Unscheduled"
    h, m = map(int, due_date_iso[11:16].split(":"))
    total_minutes = h * 60 + m

    def bounds(block: str) -> tuple[int, int]:
        s, e = block.split("-")
        sh, sm = map(int, s.split(":"))
        eh, em = map(int, e.split(":"))
        return sh * 60 + sm, eh * 60 + em

    m_s, m_e = bounds(morning_block)
    a_s, a_e = bounds(afternoon_block)
    e_s, e_e = bounds(evening_block)

    if m_s <= total_minutes < m_e:
        return "Morning"
    if a_s <= total_minutes < a_e:
        return "Afternoon"
    if e_s <= total_minutes < e_e:
        return "Evening"
    return "Unscheduled"


def build_tasks_section(
    tasks: list[dict],
    projects: dict[str, str],
    inbox_id: str | None,
    morning_block: str,
    afternoon_block: str,
    evening_block: str,
) -> list[str]:
    """Group, sort, and format tasks for write_tasks_section().

    Order: Unscheduled (no header) → 🌅 Morning → ☀️ Afternoon → 🌙 Evening.
    Within each group: p1 first → p4 last (stable sort).
    Inbox tasks and tasks with no project get no [Project] prefix.
    """
    buckets: dict[str, list[dict]] = {s: [] for s in _SECTION_ORDER}
    for task in tasks:
        section = _time_section(task.get("due_date"), morning_block, afternoon_block, evening_block)
        buckets[section].append(task)

    for section_tasks in buckets.values():
        section_tasks.sort(key=lambda t: _PRIORITY_ORDER.get(t.get("priority", "p4"), 3))

    lines: list[str] = []
    for section_name in _SECTION_ORDER:
        section_tasks = buckets[section_name]
        if not section_tasks:
            continue
        if section_name != "Unscheduled":
            lines.append(_SECTION_HEADER[section_name])
        for task in section_tasks:
            pid = task.get("project_id")
            project_name = projects.get(pid, "") if pid else ""
            is_inbox = pid is not None and pid == inbox_id
            prefix = None if (not project_name or is_inbox) else project_name
            lines.append(
                format_task_line(
                    task["title"],
                    task.get("is_completed", False),
                    task.get("priority", "p4"),
                    task.get("duration_minutes"),
                    prefix,
                )
            )
    return lines


def write_tasks_section(task_lines: list[str], day: date | None = None) -> None:
    """Atomically rewrite the ## Tasks section in the daily note.

    Creates the note if it doesn't exist.
    """
    path = daily_note_path(day)
    day = day or date.today()

    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        text = ""

    new_section = "## Tasks\n" + "\n".join(task_lines) + "\n"

    if "## Tasks" in text:
        text = re.sub(
            r"## Tasks\n.*?(?=\n## |\Z)",
            new_section,
            text,
            flags=re.DOTALL,
        )
    else:
        text = text.rstrip("\n") + "\n\n" + new_section

    _atomic_write(path, text)


def _append_section(heading: str, content: str, day: date | None = None) -> None:
    path = daily_note_path(day)
    day = day or date.today()
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        text = ""
    text = text.rstrip("\n") + f"\n\n## {heading}\n{content}\n"
    _atomic_write(path, text)


def append_digest(content: str, day: date | None = None) -> None:
    """Append the morning digest to today's daily note."""
    _append_section("Morning Digest", content, day)


def append_plan(content: str, day: date | None = None) -> None:
    """Append the daily plan to today's daily note."""
    _append_section("Daily Plan", content, day)


def read_task_guidelines() -> str:
    """Read task hygiene guidelines from vault/Assistant/task-guidelines.md.

    Returns file contents, or empty string if the file doesn't exist.
    """
    path = VAULT / "Assistant" / "task-guidelines.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_insight(filename: str, content: str) -> None:
    """Write content to vault/Assistant/<filename>."""
    assistant_dir = VAULT / "Assistant"
    assistant_dir.mkdir(exist_ok=True)
    path = assistant_dir / filename
    _atomic_write(path, content)


def is_day_planned(day: date | None = None) -> bool:
    """Return True if today's Tasks section has real tasks (not just the 'Unplanned' marker)."""
    lines = read_tasks_section(day)
    if not lines:
        return False
    return not (len(lines) == 1 and lines[0].strip() == "Unplanned")


def mark_day_unplanned(day: date | None = None) -> None:
    """Write a single 'Unplanned' marker to the Tasks section."""
    write_tasks_section(["Unplanned"], day)


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
