---
name: source-command-latch-budget-approve
description: Unlock the rest of today for unlimited latch LLM invocations in this project. Use when the user invokes $source-command-latch-budget-approve, latch-budget-approve, /latch-budget-approve, or wants the Codex equivalent of Claude Code's /latch-budget-approve command.
---

# source-command-latch-budget-approve

Use this skill only when the user explicitly asks to approve the latch LLM
budget for the current UTC day.

## Command Template

Resolve the active latch checkout, then run the budget approval command:

```bash
latch_home="${LATCH_HOME:-}"
if [ -z "$latch_home" ] && [ -n "${CLAUDE_KB_HOME:-}" ]; then
  latch_home="$CLAUDE_KB_HOME"
fi
if [ -z "$latch_home" ]; then
  search_dir="$PWD"
  while [ "$search_dir" != "/" ]; do
    if [ -f "$search_dir/AGENTS.md" ]; then
      latch_home="$(sed -n 's|.*Follow `\([^`]*\)/README\.md` per-user setup.*|\1|p' "$search_dir/AGENTS.md" | head -n 1)"
      [ -n "$latch_home" ] && break
    fi
    search_dir="$(dirname "$search_dir")"
  done
fi
if [ -z "$latch_home" ]; then
  candidate="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  if { [ -f "$candidate/src/latch/mcp/mcp_server.py" ] || [ -f "$candidate/src/mcp_server.py" ]; } && [ -d "$candidate/commands" ]; then
    latch_home="$candidate"
  fi
fi
if [ -z "$latch_home" ] || { [ ! -f "$latch_home/src/latch/mcp/mcp_server.py" ] && [ ! -f "$latch_home/src/mcp_server.py" ]; }; then
  echo "Could not find latch checkout; set LATCH_HOME to your latch install." >&2
  exit 1
fi
budget_script="$latch_home/src/latch/gate/budget.py"
if [ ! -f "$budget_script" ]; then
  budget_script="$latch_home/src/budget.py"
fi
python "$budget_script" approve "$(pwd)"
```

Report the JSON output, especially `date`, `count_nonheal`, `count_heal`, and
`approved_dates`. Do not run this proactively; the daily caps are a cost-safety
backstop.

For a read-only status check, run:

```bash
budget_script="$latch_home/src/latch/gate/budget.py"
if [ ! -f "$budget_script" ]; then
  budget_script="$latch_home/src/budget.py"
fi
python "$budget_script" status "$(pwd)"
```
