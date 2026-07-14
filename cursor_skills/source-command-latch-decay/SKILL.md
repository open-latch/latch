---
name: source-command-latch-decay
description: Apply weekly latch decay and promote staging nodes. Use when the user invokes latch-decay or /latch-decay in Cursor.
---

# Latch decay for Cursor

Latch operation id: latch-decay run

Before any Shell call, read the workspace `.cursor/mcp.json` and use the exact
absolute `mcpServers.latch.command` as `LATCH_PYTHON`. Never fall back to a
PATH `python3`; the MCP interpreter owns latch's native dependencies.
Use `latch_home` only to construct the absolute script path. Do not export
`LATCH_HOME` or `CLAUDE_KB_HOME` in the Shell call; managed Cursor operation
receipts do not allow those environment assignments.

Latch Cursor skill boundary: resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}`
when set, otherwise use the absolute checkout in the project-sync footer.
Select native `cursor` for plugin installs or the backend in that footer. Run
`<CURSOR_MCP_PYTHON> "$latch_home/src/maintenance.py" weekly "$PWD"` with
`LATCH_PYTHON` set to that same absolute interpreter and
`LATCH_MAINTENANCE_BACKEND` and `LATCH_MODEL_BACKEND` set to that backend.
Report `decayed_rows`, `promoted_count`, and `promoted_ids`.
