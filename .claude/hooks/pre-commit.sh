#!/bin/bash
# Pre-commit gate: runs tests, type checks, and linting before every git commit.
# Invoked by Claude Code as a PreToolUse hook on the Bash tool.
# Exit 2 to block the commit; exit 0 to allow it.

set -e

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command',''))" 2>/dev/null)

# Only intercept git commit commands
if ! echo "$COMMAND" | grep -q "git commit"; then
  exit 0
fi

cd /home/ubuntu/dev/assistant

echo "--- pre-commit: running pytest ---" >&2
if ! uv run pytest -q; then
  echo "BLOCKED: tests failed" >&2
  exit 2
fi

echo "--- pre-commit: running ty check ---" >&2
if ! uv run ty check lib/todoist.py lib/sync.py lib/obsidian.py lib/models.py lib/config.py lib/llm.py; then
  echo "BLOCKED: type errors in core modules" >&2
  exit 2
fi

echo "--- pre-commit: running ruff ---" >&2
if ! uv run ruff check .; then
  echo "BLOCKED: ruff violations" >&2
  exit 2
fi

echo "--- pre-commit: all checks passed ---" >&2
exit 0
