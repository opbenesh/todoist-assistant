import json
import time
from datetime import date

import pytest

from tests.e2e.helpers import BotClient, TodoistInspector
from tests.staging import seed_data as sd

pytestmark = pytest.mark.e2e

def test_postpone_from_resumed_triage_state(bot: BotClient, todoist: TodoistInspector, staging_app: dict):
    """
    Test starting with a specific resumed state:
    - Plan session: phase=7 (triage)
    - Brainstorm: 2 proposed, 2 created
    - Triage: 24 tasks, index=0, 0 done
    - Last action: 3m ago
    Then postpone the first item.
    """
    # 1. Setup specific initial state
    now = time.time()
    three_min_ago = now - 180
    
    # Use today's date for session validation in plan_cmd
    # (The session_date < date.today() check)
    
    triage_tasks = [
        {
            "id": f"task_{i}",
            "title": f"Task {i}",
            "notes": "",
            "priority": "p4",
            "labels": [],
            "due_date": date.today().isoformat(),
            "is_completed": False,
            "is_recurring": False,
            "due_string": "today",
        }
        for i in range(24)
    ]
    
    # Seed Todoist with these tasks so the actions have something to act upon
    todoist.seed(tasks=[
        sd.task(f"Task {i}", task_id=f"task_{i}", due_today=True)
        for i in range(24)
    ])

    state = {
        "sync_cursor": "*",
        "last_sync_ts": now,
        "plan_session": {
            "phase": 7,  # TRIAGING
            "session": {
                "bs_proposed": ["Idea 1", "Idea 2"],
                "bs_index": 2,
                "bs_created": 2,
                "opt_queue": [],
                "opt_auto_labeled": 0,
                "opt_current_task": None,
                "opt_proposed": [],
                "opt_proposal_index": 0,
                "opt_broken_down": 0,
                "opt_created": 0,
                "opt_processed_ids": [],
                "triage_tasks": triage_tasks,
                "triage_index": 0,
                "triage_pending_priority": None,
                "triage_processed": [],
                "last_user_action_ts": three_min_ago,
                "nudge_sent": False
            }
        }
    }
    
    state_file = staging_app["state_file"]
    state_file.write_text(json.dumps(state))
    
    # 2. Trigger resumption
    bot.send_message("/plan")
    
    # Wait for the task card to appear
    # The bot sends "↩ Resuming..." and then the task card "*Task 1 of 24*"
    deadline = time.monotonic() + 10
    found = False
    while time.monotonic() < deadline:
        responses = bot.all_responses()
        combined_text = " ".join((r.get("text") or "") for r in responses).lower()
        if "resuming" in combined_text and "task 1 of 24" in combined_text:
            found = True
            break
        time.sleep(0.2)
    
    assert found, f"Did not find expected resumption messages. Got: {[r.get('text') for r in bot.all_responses()]}"
    bot._seen_count = len(responses)

    # 3. Postpone the first item
    # The button text for postpone is "⏸ Postpone"
    pressed = bot.press_button_labeled("Postpone")
    assert pressed, "Postpone button not found"
    
    # 4. Verify in Todoist
    # Postponing a non-recurring task calls remove_due_date (Sync API)
    op = todoist.wait_for_op("remove_due_date", timeout=8)
    assert op is not None, "remove_due_date not called"
    assert op["task_id"] == "task_0"
    
    # Bot should advance to the next task
    resp = bot.wait_responses(1, timeout=8)
    assert any("Task 2 of 24" in (r.get("text") or "") for r in resp)
