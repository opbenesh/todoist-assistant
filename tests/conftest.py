from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from todoist_api_python.api import TodoistAPI

# Redirect logs/state to temp files during tests to avoid polluting production logs/state
_pid = os.getpid()
_tmp_dir = Path(tempfile.gettempdir())
os.environ.setdefault("AUDIT_PATH", str(_tmp_dir / f"test_audit_{_pid}.jsonl"))
os.environ.setdefault("INTERACTIONS_PATH", str(_tmp_dir / f"test_interactions_{_pid}.jsonl"))
os.environ.setdefault("STATE_PATH", str(_tmp_dir / f"test_state_{_pid}.json"))


@pytest.fixture
def mock_todoist_api():
    """Patch lib.todoist._api with a spec-bound MagicMock, and block real HTTP calls
    from get_completed_tasks by replacing the module-level _http client.

    Using spec=TodoistAPI means accessing a method that doesn't exist on the
    real SDK (e.g. _api.close_task) raises AttributeError immediately, catching
    SDK renames at test time rather than waiting for a production failure.
    """
    mock_http = MagicMock()
    mock_http.get.return_value.raise_for_status = MagicMock()
    mock_http.get.return_value.json.return_value = {"results": []}
    with (
        patch("lib.todoist._api", spec=TodoistAPI) as mock_api,
        patch("lib.todoist._http", mock_http),
    ):
        yield mock_api


def make_mock_paginator(*pages):
    """Return a list-of-lists iterator simulating a multi-page Todoist paginator.

    Usage:
        make_mock_paginator([task1, task2], [task3])  # two pages
        make_mock_paginator([task1])                  # single page (still an iterator)
    """
    return list(pages)
