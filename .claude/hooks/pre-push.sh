#!/bin/bash
# Pre-push gate: blocks if there are open high/critical Dependabot alerts.
# Invoked by Claude Code as a PreToolUse hook on the Bash tool.
# Exit 2 to block the push; exit 0 to allow it.

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command',''))" 2>/dev/null)

if ! echo "$COMMAND" | grep -qE "git push"; then
  exit 0
fi

cd /home/ubuntu/dev/assistant

REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)
if [ -z "$REPO" ]; then
  echo "--- pre-push: could not determine repo, skipping Dependabot check ---" >&2
  exit 0
fi

echo "--- pre-push: checking Dependabot alerts for $REPO ---" >&2

OPEN=$(gh api "repos/$REPO/dependabot/alerts" \
  --jq '[.[] | select(.state == "open" and (.security_advisory.severity == "high" or .security_advisory.severity == "critical"))] | length' 2>/dev/null)

if [ -z "$OPEN" ]; then
  echo "--- pre-push: Dependabot check skipped (gh API unavailable) ---" >&2
  exit 0
fi

if [ "$OPEN" -gt 0 ]; then
  echo "BLOCKED: $OPEN open high/critical Dependabot alert(s) on $REPO — fix before pushing" >&2
  gh api "repos/$REPO/dependabot/alerts" \
    --jq '.[] | select(.state == "open" and (.security_advisory.severity == "high" or .security_advisory.severity == "critical")) | "  #\(.number) [\(.security_advisory.severity)] \(.dependency.package.name): \(.security_advisory.summary)"' >&2
  exit 2
fi

echo "--- pre-push: no open high/critical Dependabot alerts ---" >&2
exit 0
