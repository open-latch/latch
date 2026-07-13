---
name: source-command-latch-gate-report
description: Show a read-only report over recent latch gate activity. Use when the user invokes latch-gate-report or /latch-gate-report in Cursor.
---

# Latch gate report for Cursor

Latch operation id: latch-gate-report run

Before any Shell call, read the workspace `.cursor/mcp.json` and use the exact
absolute `mcpServers.latch.command` as `LATCH_PYTHON`. Never fall back to a
PATH `python3`; the MCP interpreter owns latch's native dependencies.

Latch Cursor skill boundary: resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}`
when set, otherwise use the absolute checkout in the project-sync footer.
Translate any user-supplied date/limit filters
into arguments and run the host-appropriate `bin/latch_gate_report.sh` or
`.ps1` wrapper. Show the report as-is. This workflow reads structural logs; it
does not run a new gate, read raw prompts, or write decisions.
