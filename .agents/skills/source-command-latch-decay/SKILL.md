---
name: source-command-latch-decay
description: Apply weekly decay and promote staging nodes that cleared the ref_count bar. Use when the user invokes $source-command-latch-decay, latch-decay, /latch-decay, or wants the Codex equivalent of Claude Code's /latch-decay command.
---

# source-command-latch-decay

Use this skill when the user asks Codex to run the latch weekly maintenance
decay pass for the current project.

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
LATCH_SESSION_ID="$codex_task_id" python "$latch_home/src/maintenance.py" weekly "$(pwd)"
```

Report the JSON summary, especially `decayed_rows`, `promoted_count`, and
`promoted_ids`. If the command fails, check `maintenance.log` in the selected
latch checkout.
