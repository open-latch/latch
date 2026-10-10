---
name: source-command-latch-decay
description: Apply weekly decay and promote staging nodes that cleared the ref_count bar. Use when the user invokes $source-command-latch-decay, latch-decay, /latch-decay, or wants the Codex equivalent of Claude Code's /latch-decay command.
---

# source-command-latch-decay

Use this skill when the user asks Codex to run the latch weekly maintenance
decay pass for the current project.

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
  if { [ -f "$candidate/src/latch/mcp/mcp_server.py" ] || [ -f "$candidate/src/mcp_server.py" ]; } && [ -d "$candidate/commands" ]; then
    latch_home="$candidate"
  fi
fi
if [ -z "$latch_home" ] || { [ ! -f "$latch_home/src/latch/mcp/mcp_server.py" ] && [ ! -f "$latch_home/src/mcp_server.py" ]; }; then
  echo "Could not find latch checkout; set LATCH_HOME to your latch install." >&2
  exit 1
fi
maintenance_script="$latch_home/src/latch/pipeline/maintenance.py"
if [ ! -f "$maintenance_script" ]; then
  maintenance_script="$latch_home/src/maintenance.py"
fi
python "$maintenance_script" weekly "$(pwd)"
```

Report the JSON summary, especially `decayed_rows`, `promoted_count`, and
`promoted_ids`. If the command fails, check `maintenance.log` in the selected
latch checkout.
