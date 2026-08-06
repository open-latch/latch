---
name: source-command-latch-budget-approve
description: Unlock the rest of today for unlimited latch LLM invocations in this project. Use when the user invokes $source-command-latch-budget-approve, latch-budget-approve, /latch-budget-approve, or wants the Codex equivalent of Claude Code's /latch-budget-approve command.
---

# source-command-latch-budget-approve

Use this skill only when the user explicitly asks to approve the latch LLM
budget for the current UTC day.

Use the exact current Codex task id. Prefer `$CODEX_THREAD_ID`; if it is not
exposed, stop and ask the user for the id, then set `codex_task_id` explicitly.
Never infer or reuse an id from another task.

## Command Template

Resolve the active latch checkout, then run the budget approval command:

```bash
latch_home="${LATCH_HOME:-}"
if [ -z "$latch_home" ] && [ -n "${CLAUDE_KB_HOME:-}" ]; then
  latch_home="$CLAUDE_KB_HOME"
fi
installed_latch_home=__LATCH_INSTALLED_HOME__
if [ -z "$latch_home" ] && [ -f "$installed_latch_home/src/mcp_server.py" ]; then
  latch_home="$installed_latch_home"
fi
if [ -z "$latch_home" ] || [ ! -f "$latch_home/src/mcp_server.py" ]; then
  echo "Installed Latch checkout is unavailable; re-run the Codex installer." >&2
  exit 1
fi
codex_task_id="${CODEX_THREAD_ID:-}"
if [ -z "$codex_task_id" ]; then
  echo "Current Codex task id unavailable; ask the user before running this command." >&2
  exit 1
fi
python "$latch_home/src/budget.py" approve "$(pwd)" --session-id "$codex_task_id"
```

Report the JSON output, especially `date`, `count_nonheal`, `count_heal`, and
`approved_dates`. Do not run this proactively; the daily caps are a cost-safety
backstop.

For a read-only status check, run:

```bash
codex_task_id="${CODEX_THREAD_ID:-}"
test -n "$codex_task_id" || { echo "Current Codex task id unavailable." >&2; exit 1; }
python "$latch_home/src/budget.py" status "$(pwd)" --session-id "$codex_task_id"
```
