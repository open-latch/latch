---
name: source-command-latch-heal
description: Run nightly latch heal and contradiction resolution. Use when the user invokes latch-heal or /latch-heal in Cursor.
---

# Latch heal for Cursor

Latch operation id: latch-heal run

Before any Shell call, read the workspace `.cursor/mcp.json` and use
`mcpServers.latch.env.LATCH_PYTHON` when present, otherwise
`mcpServers.latch.command`, as `<CURSOR_MCP_PYTHON>` and `LATCH_PYTHON`. Never fall back to a
PATH `python3`; the MCP interpreter owns latch's native dependencies.
Use `latch_home` only to construct the absolute script path. Do not export
`LATCH_HOME` or `CLAUDE_KB_HOME` in the Shell call; managed Cursor operation
receipts do not allow those environment assignments.

Latch Cursor skill boundary: resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}`
when set, otherwise use the absolute checkout in the project-sync footer.
Select native `cursor` for plugin installs or the backend in that footer. Run
`<CURSOR_MCP_PYTHON> "$latch_home/src/latch/pipeline/maintenance.py" nightly "$PWD"` with
`LATCH_PYTHON` set to that same absolute interpreter and
`LATCH_MAINTENANCE_BACKEND` and `LATCH_MODEL_BACKEND` set. Report `examined`,
`collisions`, `superseded`, `kept_both`, per-path counts, and `budget_blocked`.
