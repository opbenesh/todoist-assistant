from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date

import anthropic
from anthropic.types import TextBlock

from lib import config as _config
from lib.config import ANTHROPIC_KEY
from lib.models import DEFAULT_PRIORITY, VALID_PRIORITIES, Task

logger = logging.getLogger(__name__)

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"

_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, base_url=_config.ANTHROPIC_BASE_URL)

_VALID_LABELS = {"work", "personal", "health", "finance", "home"}

_ENRICHMENT_SYSTEM = """You are a task enrichment assistant.
Given a raw task title, return a JSON object with these exact fields:
- "title": string (cleaned/clarified task title, fix typos)
- "notes": string (brief optional context, empty string if none)
- "due_date": string or null (ISO date YYYY-MM-DD if obvious from context, null if unclear)
- "priority": "p1", "p2", "p3", or "p4" (p1=urgent/important, p4=someday/low)
- "labels": array of strings (zero or more from: work, personal, health, finance, home)
- "duration_minutes": integer or null (realistic time estimate in minutes, null if unknown)

Return ONLY valid JSON with no prose, no markdown, no code fences.
Today is {today}."""

_DIGEST_SYSTEM = """You are a personal assistant generating a morning digest.
Be concise, practical, and encouraging. Use Markdown formatting.
Structure: one-sentence greeting, bullet list of tasks grouped by priority,
a suggested morning focus.
Maximum 200 words. Today is {today}, {weekday}."""

_WEEKLY_REVIEW_SYSTEM = """You are a thoughtful productivity advisor writing a weekly review.
Identify patterns, celebrate wins, and surface recurring blockers.
Use Markdown. Be honest but constructive. Maximum 400 words."""

_NUDGE_SYSTEM = """You are a personal assistant sending a brief motivating nudge.
The user has overdue tasks. Write 2-3 sentences: acknowledge the backlog,
pick the single most important task to focus on now, and encourage action.
No lists. Plain text only."""

_OPTIMIZATION_SYSTEM = """You are a task hygiene assistant reviewing a Todoist task.
Apply the following guidelines when evaluating a task:

{guidelines}

Return a JSON object with ONLY the fields that need improvement. Possible fields:
- "title": string (cleaned/clarified title — only if vague, has typos, or unclear;
  if title contains a markdown link [text](url), preserve the URL and only clean surrounding text)
- "priority": "p1", "p2", "p3", or "p4" (suggest if current is p4/unset — pick the right level)
- "project": string (suggest if Inbox or wrong project; must be from available projects)
- "labels": list of strings from: work, personal, health, finance, home (only if missing or wrong)
- "due_date": "YYYY-MM-DD" (suggest if null and task has a clear timeframe)

Rules:
- Return {{}} (empty object) if the task looks fine as-is
- Only suggest changes you are confident about
- Do NOT change fields that are already appropriate
- Return ONLY valid JSON, no prose, no markdown fences.
Today is {today}. Available projects: {projects}."""

_DEFAULT_GUIDELINES = (
    "A task is untriaged if: priority is p4, project is Inbox, or no due date is set."
)

_BRAINSTORM_SYSTEM = """You are a task extraction assistant.
The user will write free-form text describing what they want or need to do.
Extract every distinct actionable task and return them as a JSON array of short, clean task titles.
Rules:
- Each title should be clear and actionable (start with a verb where natural)
- Preserve the original language — NEVER translate; if the input is Hebrew, output Hebrew titles
- Fix obvious typos and capitalise the first word only
- Omit vague filler phrases ("maybe", "probably") but include the task if there's real intent
- Titles under 80 characters each
- Return ONLY a valid JSON array of strings, no prose, no markdown fences.
Example input: "wash all dishes, take the garbage out, clean the fridge, maybe deal with kzradio"
Example output: ["Wash all dishes", "Take the garbage out", "Clean the fridge",
  "Deal with kzradio"]"""

_BREAKDOWN_OPTIMIZE_SYSTEM = """You are a task breakdown assistant helping convert a \
non-actionable task into the minimum number of concrete, atomic, actionable subtasks.

Rules:
- Lean strongly toward ONE subtask unless the work genuinely requires separate atomic steps
- Each subtask must be actionable: specific, achievable, unambiguous
- Pick ONE context-appropriate emoji for the parent task and start EVERY subtask title with it
- Subtask titles under 80 chars each
- Derive a short project slug: lowercase, hyphen-separated, 2–4 words, no special characters

Return a JSON object with exactly two keys:
- "project_slug": string (e.g. "tax-filing", "website-redesign")
- "tasks": array of strings (the subtask titles)

Example: {"project_slug": "tax-filing", "tasks": ["📄 Gather documents", "📄 Submit online"]}
Return ONLY valid JSON object, no prose, no markdown fences.
Original task context will be provided alongside the user's free-form plan."""

_BREAKDOWN_SYSTEM = """You are a task planning assistant.
Given a task, break it down into 2–5 concrete, actionable subtasks.
Return a JSON array of strings, each a short subtask title (under 60 chars).
Return ONLY valid JSON array, no prose, no markdown fences."""

_DEEPDIVE_SYSTEM = """You are a personal productivity coach doing a deep-dive on a single task.
Provide:
1. **Clarified goal & success criteria** — what done looks like
2. **Concrete steps** — 3–6 ordered actions
3. **Likely blockers / unknowns** — what could slow this down
4. **Recommended next action** — the single thing to do right now

Use Markdown. Be specific and actionable. Under 300 words."""

_PROJECT_PLAN_SYSTEM = """You are a project manager reviewing a Todoist project.
Given all open tasks in the project, produce:
1. **State summary** — task count, age spread, priority breakdown
2. **Next 3 actions** — highest-leverage tasks to tackle first
3. **Blocked/stale items** — tasks needing attention or removal
4. **Rough effort estimate** — total estimated time if available

Use Markdown with clear sections. Under 400 words. Today is {today}."""

_INSIGHTS_SYSTEM = """You are a productivity analyst writing a formal insights report.
Analyse:
- **Habit gaps** — recurring tasks frequently pushed or missed
- **Workload** — completion rate, overdue ratio
- **Topic clusters** — which projects/labels accumulated most debt
- **Quarantined tasks** — if any are present, identify what they have in common and recommend
  whether to delete, delegate, or reframe each one
- **Recommendations** — 2–3 concrete changes for next week

Tone: strictly formal and analytical throughout — no motivational language, no direct address,
no rhetorical questions, no informal asides. State observations and recommendations as facts.
Use Markdown. Under 500 words."""


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


def restore_links(original: str, proposed: str) -> str:
    """Re-inject markdown links from original into proposed title.

    If the original title contains markdown links and the proposed title has
    lost them, append the missing links so URLs are never silently dropped.
    """
    original_links = _MD_LINK_RE.findall(original)  # list of (text, url) tuples
    if not original_links:
        return proposed
    proposed_urls = {url for _, url in _MD_LINK_RE.findall(proposed)}
    missing = [f"[{text}]({url})" for text, url in original_links if url not in proposed_urls]
    if not missing:
        return proposed
    return proposed.rstrip() + " " + " ".join(missing)


def _strip_fences(text: str) -> str:
    """Strip markdown code fences from LLM output before JSON parsing."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]  # drop ```json or ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def _today_fmt() -> dict[str, str]:
    today = date.today()
    return {"today": today.isoformat(), "weekday": today.strftime("%A")}


def _call(model: str, system: str, user_content: str, max_tokens: int = 500) -> str:
    response = _client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    block = response.content[0]
    assert isinstance(block, TextBlock), f"Unexpected content block type: {type(block)}"
    return block.text


async def propose_enrichment(raw_title: str) -> Task:
    """Call Haiku to enrich a raw task title. Returns a Task dataclass."""
    today = date.today().isoformat()
    system = _ENRICHMENT_SYSTEM.format(today=today)

    raw_json = await asyncio.to_thread(_call, HAIKU, system, raw_title, 300)

    try:
        data = json.loads(_strip_fences(raw_json))
    except json.JSONDecodeError as exc:
        logger.warning("LLM returned invalid JSON: %s — raw: %s", exc, raw_json)
        raise ValueError(f"LLM returned invalid JSON: {exc}") from exc

    due_date = None
    if data.get("due_date"):
        try:
            due_date = date.fromisoformat(data["due_date"])
        except ValueError:
            logger.warning("LLM returned invalid due_date: %s", data["due_date"])

    labels = [lbl for lbl in (data.get("labels") or []) if lbl in _VALID_LABELS]

    priority = data.get("priority", DEFAULT_PRIORITY)
    if priority not in VALID_PRIORITIES:
        priority = DEFAULT_PRIORITY

    duration = data.get("duration_minutes")
    if duration is not None:
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = None

    return Task(
        title=data.get("title") or raw_title,
        notes=data.get("notes") or "",
        due_date=due_date,
        priority=priority,
        labels=labels,
        duration_minutes=duration,
    )


def propose_task_optimization(task: dict, projects: list[str]) -> dict:
    """Sync: analyze a task and return suggested improvements as a dict.

    Returns an empty dict if no changes are needed.
    Call via asyncio.to_thread at the call site.
    """
    from lib.obsidian import read_task_guidelines

    guidelines = read_task_guidelines() or _DEFAULT_GUIDELINES
    today = date.today().isoformat()
    system = _OPTIMIZATION_SYSTEM.format(
        today=today, projects=", ".join(projects), guidelines=guidelines
    )
    raw_json = _call(HAIKU, system, json.dumps(task, default=str), 200)
    try:
        data = json.loads(_strip_fences(raw_json))
        if not isinstance(data, dict):
            return {}
        result: dict = {}
        if "title" in data and isinstance(data["title"], str) and data["title"]:
            result["title"] = restore_links(task.get("title", ""), data["title"])
        if "priority" in data and data["priority"] in VALID_PRIORITIES:
            result["priority"] = data["priority"]
        if "project" in data and isinstance(data["project"], str) and data["project"] in projects:
            result["project"] = data["project"]
        if "labels" in data and isinstance(data["labels"], list):
            valid = [lbl for lbl in data["labels"] if lbl in _VALID_LABELS]
            if valid:
                result["labels"] = valid
        if "due_date" in data and isinstance(data["due_date"], str) and data["due_date"]:
            result["due_date"] = data["due_date"]
        return result
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("Optimization LLM returned invalid JSON: %s — raw: %s", exc, raw_json)
        return {}


async def generate_digest(tasks: list[dict]) -> str:
    """Generate a morning digest using Haiku."""
    system = _DIGEST_SYSTEM.format(**_today_fmt())
    tasks_text = json.dumps(tasks, default=str)
    return await asyncio.to_thread(_call, HAIKU, system, f"Today's tasks:\n{tasks_text}", 400)


async def generate_weekly_review(completed: list[dict], overdue: list[dict]) -> str:
    """Generate a weekly review using Sonnet."""
    content = (
        f"Completed this week:\n{json.dumps(completed, default=str)}\n\n"
        f"Incomplete/overdue:\n{json.dumps(overdue, default=str)}"
    )
    return await asyncio.to_thread(_call, SONNET, _WEEKLY_REVIEW_SYSTEM, content, 800)


async def generate_nudge(overdue: list[dict]) -> str:
    """Generate a short motivating nudge for overdue tasks using Haiku."""
    content = f"Overdue tasks:\n{json.dumps(overdue, default=str)}"
    return await asyncio.to_thread(_call, HAIKU, _NUDGE_SYSTEM, content, 150)


def propose_breakdown(task: Task) -> list[str]:
    """Sync: break a task into 2–5 subtask titles using Haiku.

    Returns list of subtask title strings, empty on failure.
    Call via asyncio.to_thread at the call site.
    """
    content = f"Task: {task.title}"
    if task.notes:
        content += f"\nNotes: {task.notes}"
    raw_json = _call(HAIKU, _BREAKDOWN_SYSTEM, content, 300)
    try:
        data = json.loads(_strip_fences(raw_json))
        if isinstance(data, list):
            return [s for s in data if isinstance(s, str) and s][:5]
        return []
    except json.JSONDecodeError as exc:
        logger.warning("Breakdown LLM returned invalid JSON: %s — raw: %s", exc, raw_json)
        return []


async def generate_deepdive(task: dict) -> str:
    """Generate a deep-dive analysis for a single task using Sonnet."""
    content = f"Task:\n{json.dumps(task, default=str)}"
    return await asyncio.to_thread(_call, SONNET, _DEEPDIVE_SYSTEM, content, 600)


async def generate_project_plan(project_name: str, tasks: list[dict]) -> str:
    """Generate a project-level plan using Sonnet."""
    system = _PROJECT_PLAN_SYSTEM.format(**_today_fmt())
    content = f"Project: {project_name}\n\nTasks:\n{json.dumps(tasks, default=str)}"
    return await asyncio.to_thread(_call, SONNET, system, content, 800)


async def brainstorm_extract_tasks(text: str) -> list[str]:
    """Extract actionable task titles from free-form brainstorm text using Haiku.

    Returns a list of task title strings, empty on failure.
    """
    raw = await asyncio.to_thread(_call, HAIKU, _BRAINSTORM_SYSTEM, text, 400)
    try:
        data = json.loads(_strip_fences(raw))
        if isinstance(data, list):
            return [s for s in data if isinstance(s, str) and s]
        return []
    except json.JSONDecodeError as exc:
        logger.warning("brainstorm_extract_tasks: invalid JSON: %s — raw: %s", exc, raw)
        return []


async def generate_insights(
    completed: list[dict],
    overdue: list[dict],
    all_active: list[dict],
    quarantined: list[dict] | None = None,
) -> str:
    """Generate a deep insights report using Sonnet."""
    content = (
        f"Completed recently:\n{json.dumps(completed, default=str)}\n\n"
        f"Overdue:\n{json.dumps(overdue, default=str)}\n\n"
        f"All active tasks:\n{json.dumps(all_active, default=str)}"
    )
    if quarantined:
        content += (
            f"\n\nQuarantined tasks (chronically deferred, excluded from planning):\n"
            f"{json.dumps(quarantined, default=str)}"
        )
    return await asyncio.to_thread(_call, SONNET, _INSIGHTS_SYSTEM, content, 1000)


async def breakdown_tasks_for_optimize(
    original_task: dict, user_plan: str
) -> tuple[list[str], str]:
    """Propose minimum atomic breakdown tasks and a project slug using Haiku.

    Returns (tasks, project_slug). tasks is a list of title strings with emoji prefix.
    On failure returns ([], "").
    """
    task_summary = json.dumps(
        {"title": original_task["title"], "notes": original_task.get("notes", "")},
        default=str,
    )
    content = f"Original task: {task_summary}\n\nUser's plan: {user_plan}"
    raw = await asyncio.to_thread(_call, HAIKU, _BREAKDOWN_OPTIMIZE_SYSTEM, content, 400)
    try:
        data = json.loads(_strip_fences(raw))
        if isinstance(data, dict):
            tasks = [s for s in data.get("tasks", []) if isinstance(s, str) and s]
            slug = str(data.get("project_slug", "")).strip().lower().replace(" ", "-")
            return tasks, slug
        return [], ""
    except json.JSONDecodeError as exc:
        logger.warning("breakdown_tasks_for_optimize: invalid JSON: %s — raw: %s", exc, raw)
        return [], ""
