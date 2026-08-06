---
name: source-command-latch-gate
description: Manually run latch gate on a coding or build request. Use when the user invokes $source-command-latch-gate, latch-gate, /latch-gate, or wants the Codex equivalent of Claude Code's /latch-gate command.
---

# source-command-latch-gate

Use this skill when the user explicitly asks to run latch gate as a manual
second opinion on a request or implementation plan.

## Command Template

If the user did not provide a request, ask for the request to gate. Prefer the
Codex MCP `latch_gate`/`kb_gate` tool when it is available. If you use the shell
fallback, explicitly substitute the user's request text; Codex does not populate
Claude slash-command argument placeholders.

Use the exact current Codex task id from `$CODEX_THREAD_ID`. If unavailable,
stop and ask the user; never infer or reuse another task's id.

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
request="$(cat <<'EOF'
<exact user-provided request to gate>
EOF
)"
if [ -z "${LATCH_GATE_BACKEND:-}" ] && [ -z "${CLAUDE_KB_GATE_BACKEND:-}" ] && [ -z "${LATCH_MODEL_BACKEND:-}" ]; then
  export LATCH_GATE_BACKEND=codex
fi
LATCH_SESSION_ID="$codex_task_id" bash "$latch_home/bin/run_latch_gate.sh" "$request"
```

After the command returns, show an explicit **Latch gate** block. Prefer the
returned `findings` object when present. If the result says latch is UNLATCHED,
report that latch gate was skipped, do not claim KB evidence was read, and tell
the user to run `/latch` to re-latch. Otherwise, lead with provenance, then
include the recommendation, summary/rationale, receipt or source basis, cited
evidence nodes with status/current authority, any better next action, and
uncovered claims.

Do not treat the gate as an auto-redirect. Surface `MODIFY`,
`DO_NOT_PROCEED`, or `NEEDS_HUMAN_JUDGMENT` clearly and let the user decide
the next step.
