from __future__ import annotations

from unittest.mock import patch

import pytest
from todoist_api_python.api import TodoistAPI


@pytest.fixture
def mock_todoist_api():
    """Patch lib.todoist._api with a spec-bound MagicMock.

    Using spec=TodoistAPI means accessing a method that doesn't exist on the
    real SDK (e.g. _api.close_task) raises AttributeError immediately, catching
    SDK renames at test time rather than waiting for a production failure.
    """
    with patch("lib.todoist._api", spec=TodoistAPI) as mock_api:
        yield mock_api


def make_mock_paginator(*pages):
    """Return a list-of-lists iterator simulating a multi-page Todoist paginator.

    Usage:
        make_mock_paginator([task1, task2], [task3])  # two pages
        make_mock_paginator([task1])                  # single page (still an iterator)
    """
    return list(pages)
