---
name: source-command-unlatch
description: Confirmed toggle for latch Unlatched mode. Use when the user invokes unlatch or /unlatch in Cursor.
---

# Unlatch for Cursor

Latch operation id: unlatch inspect

Before any Shell call, read the workspace `.cursor/mcp.json` and use the exact
absolute `mcpServers.latch.command` as `LATCH_PYTHON`. Never fall back to a
PATH `python3`; the MCP interpreter owns latch's native dependencies.
Use `latch_home` only to construct the absolute wrapper path. Do not export
`LATCH_HOME` or `CLAUDE_KB_HOME` in the Shell call; managed Cursor operation
receipts do not allow those environment assignments.

Latch Cursor skill boundary: resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}`
when set, otherwise use the absolute checkout in the project-sync footer.
Inspect state with the host-appropriate
`bin/unlatch.sh` or `.ps1` wrapper. Never mutate immediately. If latched, ask
the user to reply exactly `unlatch`; if unlatched, ask for exactly `latch`.
Only after that reply run the wrapper with `--confirm unlatch` or
`--confirm latch`. Explain that KB data remains, latch stays off across repos
until re-latched, and this is an escape hatch—not a controlled benchmark.
