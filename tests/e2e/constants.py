"""Shared constants for e2e tests.

Centralises callback data strings and priority mappings that would otherwise be
duplicated or hardcoded as magic values across test files.
"""

# Plan flow callback data (mirrors lib/handlers/plan.py)
BS_SKIP = "pf:bs_skip"
BS_START = "pf:bs_start"

# Todoist priority numeric values.
# Todoist's scale is inverted: p1 (urgent) = 4, p4 (none) = 1.
TODOIST_P1 = 4
TODOIST_P2 = 3
TODOIST_P3 = 2
TODOIST_P4 = 1
