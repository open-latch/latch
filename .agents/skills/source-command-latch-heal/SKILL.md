---
name: source-command-latch-heal
description: Run nightly latch heal with integrity sweep and contradiction resolver. Use when the user invokes $source-command-latch-heal, latch-heal, /latch-heal, or wants the Codex equivalent of Claude Code's /latch-heal command.
---

# source-command-latch-heal

Use this skill when the user asks Codex to run the latch nightly heal pass for
the current project.

## Command Template

Resolve the active latch checkout, then run:

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
  if [ -f "$candidate/src/mcp_server.py" ] && [ -d "$candidate/commands" ]; then
    latch_home="$candidate"
  fi
fi
if [ -z "$latch_home" ] || [ ! -f "$latch_home/src/mcp_server.py" ]; then
  echo "Could not find latch checkout; set LATCH_HOME to your latch install." >&2
  exit 1
fi
if [ -z "${LATCH_MAINTENANCE_BACKEND:-}" ] && [ -z "${CLAUDE_KB_MAINTENANCE_BACKEND:-}" ] && [ -z "${LATCH_MODEL_BACKEND:-}" ] && [ -z "${LATCH_GATE_BACKEND:-}" ] && [ -z "${CLAUDE_KB_GATE_BACKEND:-}" ]; then
  export LATCH_MAINTENANCE_BACKEND=codex
fi
python "$latch_home/src/maintenance.py" nightly "$(pwd)"
```

Report the JSON summary, especially `examined`, `collisions`, `superseded`,
`kept_both`, per-path counts, and `budget_blocked`. If the command fails, check
`maintenance.log` in the selected latch checkout.
