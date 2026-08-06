---
name: source-command-latch-heal
description: Run nightly latch heal with integrity sweep and contradiction resolver. Use when the user invokes $source-command-latch-heal, latch-heal, /latch-heal, or wants the Codex equivalent of Claude Code's /latch-heal command.
---

# source-command-latch-heal

Use this skill when the user asks Codex to run the latch nightly heal pass for
the current project.

Use the exact current Codex task id from `$CODEX_THREAD_ID`. If unavailable,
stop and ask the user; never infer or reuse another task's id.

## Command Template

Resolve the active latch checkout, then run:

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
test -n "$codex_task_id" || { echo "Current Codex task id unavailable." >&2; exit 1; }
if [ -z "${LATCH_MAINTENANCE_BACKEND:-}" ] && [ -z "${CLAUDE_KB_MAINTENANCE_BACKEND:-}" ] && [ -z "${LATCH_MODEL_BACKEND:-}" ] && [ -z "${LATCH_GATE_BACKEND:-}" ] && [ -z "${CLAUDE_KB_GATE_BACKEND:-}" ]; then
  export LATCH_MAINTENANCE_BACKEND=codex
fi
LATCH_SESSION_ID="$codex_task_id" python "$latch_home/src/maintenance.py" nightly "$(pwd)"
```

Report the JSON summary, especially `examined`, `collisions`, `superseded`,
`kept_both`, per-path counts, and `budget_blocked`. If the command fails, check
`maintenance.log` in the selected latch checkout.
