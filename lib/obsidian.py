from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from lib.config import VAULT_PATH
from lib.models import DEFAULT_PRIORITY

VAULT = Path(VAULT_PATH)
_TASK_LINE_RE = re.compile(r"^- \[([ x])\] (.+)$")


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
    in_section = False
    lines = []
    for line in text.splitlines():
        if re.match(r"^## Tasks", line):
            in_section = True
            continue
        if in_section and re.match(r"^## ", line):
            break
        if in_section and line.strip():
            lines.append(line)
    return lines


def parse_task_line(line: str) -> tuple[str, bool] | None:
    """Parse '- [ ] Title' or '- [x] Title' into (title, is_checked).

    Returns None if line doesn't match the task format.
    """
    m = _TASK_LINE_RE.match(line.strip())
    if not m:
        return None
    checked = m.group(1) == "x"
    # Strip metadata annotations like (p2, 15min)
    title = re.sub(r"\s*\([^)]+\)\s*$", "", m.group(2)).strip()
    return title, checked


def format_task_line(
    title: str, checked: bool, priority: str = DEFAULT_PRIORITY, duration: int | None = None
) -> str:
    check = "x" if checked else " "
    meta_parts = []
    if priority != DEFAULT_PRIORITY:
        meta_parts.append(priority)
    if duration:
        meta_parts.append(f"{duration}min")
    meta = f"  ({', '.join(meta_parts)})" if meta_parts else ""
    return f"- [{check}] {title}{meta}"


def write_tasks_section(task_lines: list[str], day: date | None = None) -> None:
    """Atomically rewrite the ## Tasks section in the daily note.

    Creates the note if it doesn't exist.
    """
    path = daily_note_path(day)
    day = day or date.today()

    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        text = f"# {day.strftime('%A, %B %d')}\n\n"

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


def append_digest(content: str, day: date | None = None) -> None:
    """Append the morning digest to today's daily note."""
    path = daily_note_path(day)
    day = day or date.today()

    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        text = f"# {day.strftime('%A, %B %d')}\n\n"

    text = text.rstrip("\n") + f"\n\n## Morning Digest\n{content}\n"
    _atomic_write(path, text)


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


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
