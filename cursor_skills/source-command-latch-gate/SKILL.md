---
name: source-command-latch-gate
description: Manually run latch gate on a coding or build request. Use when the user invokes latch-gate or /latch-gate in Cursor.
---

# Latch gate for Cursor

Latch Cursor skill boundary: this workflow is safe for project-synced skills
and the Cursor plugin.

Before any Shell fallback, read the workspace `.cursor/mcp.json` and use
`mcpServers.latch.env.LATCH_PYTHON` when present, otherwise
`mcpServers.latch.command`, as `<CURSOR_MCP_PYTHON>` and `LATCH_PYTHON`. Never fall back to
a PATH `python3`; the MCP interpreter owns latch's native dependencies.
Use `latch_home` only to construct the absolute wrapper path. Do not export
`LATCH_HOME` or `CLAUDE_KB_HOME` in the Shell fallback.

Prefer the `latch_gate` MCP tool and pass the user's request verbatim. If MCP is
unavailable, resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}` when set, otherwise
use the absolute checkout in the project-sync footer, select native `cursor`
for plugin installs or the backend in that footer, and run
`bin/run_latch_gate.sh` with `LATCH_GATE_BACKEND` and `LATCH_MODEL_BACKEND` set.
For a contextual approval/follow-up, MCP also needs a concise, self-contained
`task_context`; the shell fallback needs a self-contained request instead.

Always show the returned **Latch gate** findings before edits, including the
recommendation, rationale, cited node ids/titles/status, source receipt, better
next action, and uncovered claims. A skipped/error gate does not authorize a
mutation. Do not silently redirect a MODIFY or DO_NOT_PROCEED verdict.
