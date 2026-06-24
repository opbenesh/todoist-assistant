#!/usr/bin/env python3
"""Hook script for turn-based timing and Telegram notifications."""

import os
import subprocess
import sys
import time

TEMP_FILE = os.path.join(os.path.dirname(__file__), "turn_start.tmp")


def get_workspace_name() -> str:
    return os.path.basename(os.path.abspath(os.getcwd()))


def get_git_branch() -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], stderr=subprocess.DEVNULL)
            .decode("utf-8")
            .strip()
        )
    except Exception:
        return "unknown"


def start_timer() -> None:
    try:
        with open(TEMP_FILE, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def stop_timer() -> None:
    if not os.path.exists(TEMP_FILE):
        return

    try:
        with open(TEMP_FILE, "r", encoding="utf-8") as f:
            start_time = float(f.read().strip())
        os.remove(TEMP_FILE)
    except Exception:
        return

    duration = time.time() - start_time
    if duration >= 10:
        send_notification()


def send_notification() -> None:
    workspace = get_workspace_name()
    branch = get_git_branch()
    message = f"🤖 [{workspace}/{branch}] Agent is waiting for your input!"

    try:
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        send_msg_path = os.path.join(project_root, "send_msg.py")

        # Launch send_msg.py asynchronously in the background and detach it
        subprocess.Popen(
            [sys.executable, send_msg_path, "-m", message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=project_root,
            start_new_session=True,
        )
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(1)

    action = sys.argv[1]
    if action == "start":
        start_timer()
    elif action == "stop":
        stop_timer()
