---
name: source-command-latch-tree
description: Rebuild the hierarchical cluster and summary tree for this project's latch KB. Use when the user invokes $source-command-latch-tree, latch-tree, /latch-tree, or wants the Codex equivalent of Claude Code's /latch-tree command.
---

# source-command-latch-tree

Use this skill when the user asks Codex to rebuild the latch KB hierarchy for
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
  if [ -f "$candidate/src/mcp_server.py" ] && [ -d "$candidate/commands" ]; then
    latch_home="$candidate"
  fi
fi
if [ -z "$latch_home" ] || [ ! -f "$latch_home/src/mcp_server.py" ]; then
  echo "Could not find latch checkout; set LATCH_HOME to your latch install." >&2
  exit 1
fi
codex_task_id="${CODEX_THREAD_ID:-}"
test -n "$codex_task_id" || { echo "Current Codex task id unavailable." >&2; exit 1; }
if [ -z "${LATCH_MAINTENANCE_BACKEND:-}" ] && [ -z "${CLAUDE_KB_MAINTENANCE_BACKEND:-}" ] && [ -z "${LATCH_MODEL_BACKEND:-}" ] && [ -z "${LATCH_GATE_BACKEND:-}" ] && [ -z "${CLAUDE_KB_GATE_BACKEND:-}" ]; then
  export LATCH_MAINTENANCE_BACKEND=codex
fi
LATCH_SESSION_ID="$codex_task_id" python "$latch_home/src/maintenance.py" tree "$(pwd)"
```

Report the JSON summary, especially `linkage`, `leaves`, `landmarks`,
`clusters`, `largest_cluster`, `p95_cluster_size`, `summaries_generated`,
`singletons`, `oversized_skipped`, `budget_blocked`, `llm_failed`, and
`prior_summaries_staled`. If the command fails, check `maintenance.log` and
`tree.log` in the selected latch checkout.
