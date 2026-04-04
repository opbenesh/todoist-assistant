from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date

import anthropic

from lib.config import ANTHROPIC_KEY
from lib.models import DEFAULT_PRIORITY, VALID_PRIORITIES, Task

logger = logging.getLogger(__name__)

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"

_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

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

_PLAN_SYSTEM = """You are a personal productivity coach creating a realistic daily plan.

Each task has a "due_date" (when it was scheduled for today) and optionally a "deadline"
(hard cutoff date).
Rules:
- Tasks with deadline == today or deadline in the past MUST be kept for today.
- Tasks with no deadline or a future deadline are candidates to push.
- Be aggressive: prefer a small, achievable today list. If the total duration of today tasks
  exceeds the available time blocks, push the excess. When in doubt, push.
- Use duration_minutes and the time blocks to judge realistic capacity.

Return ONLY a valid JSON object with exactly two keys:
- "plan_markdown": string — the human-readable plan in Markdown with ## Morning / ## Afternoon /
  ## Evening headers listing only the tasks kept for today, plus a brief focus note.
- "push_tasks": array of objects with "id" and "title" — tasks recommended to defer.
  Each object must include the exact task id string from the input.

Return ONLY valid JSON, no prose, no code fences.
Today is {today}, {weekday}.
Schedule context: morning {morning}, afternoon {afternoon}, evening {evening}."""

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
- **Recommendations** — 2–3 concrete changes for next week

Tone: strictly formal and analytical throughout — no motivational language, no direct address,
no rhetorical questions, no informal asides. State observations and recommendations as facts.
Use Markdown. Under 500 words."""


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


def _restore_links(original: str, proposed: str) -> str:
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
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return response.content[0].text


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
            result["title"] = _restore_links(task.get("title", ""), data["title"])
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


async def generate_plan(
    tasks: list[dict], settings=None, context: str = ""
) -> dict:
    """Generate a timeblocked daily plan using Sonnet.

    Returns dict with keys:
      "plan_markdown": str  — human-readable plan
      "push_tasks": list[{"id": str, "title": str}]  — tasks to defer
    Falls back to plan_markdown=raw_text, push_tasks=[] on parse failure.
    """
    morning = settings.morning_block if settings else "09:00-12:00"
    afternoon = settings.afternoon_block if settings else "12:00-17:00"
    evening = settings.evening_block if settings else "17:00-21:00"
    system = _PLAN_SYSTEM.format(
        **_today_fmt(), morning=morning, afternoon=afternoon, evening=evening
    )
    tasks_text = json.dumps(tasks, default=str)
    user_content = f"Tasks:\n{tasks_text}"
    if context:
        user_content += f"\n\nAdditional context: {context}"

    raw = await asyncio.to_thread(_call, SONNET, system, user_content, 1000)

    try:
        data = json.loads(_strip_fences(raw))
        plan_md = str(data.get("plan_markdown") or "")
        push_raw = data.get("push_tasks") or []
        push_tasks = [
            {"id": str(t["id"]), "title": str(t["title"])}
            for t in push_raw
            if isinstance(t, dict) and t.get("id") and t.get("title")
        ]
        return {"plan_markdown": plan_md, "push_tasks": push_tasks}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("generate_plan: failed to parse JSON response: %s", exc)
        return {"plan_markdown": raw, "push_tasks": []}


async def generate_insights(
    completed: list[dict], overdue: list[dict], all_active: list[dict]
) -> str:
    """Generate a deep insights report using Sonnet."""
    content = (
        f"Completed recently:\n{json.dumps(completed, default=str)}\n\n"
        f"Overdue:\n{json.dumps(overdue, default=str)}\n\n"
        f"All active tasks:\n{json.dumps(all_active, default=str)}"
    )
    return await asyncio.to_thread(_call, SONNET, _INSIGHTS_SYSTEM, content, 1000)
