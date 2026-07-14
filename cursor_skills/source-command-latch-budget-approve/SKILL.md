---
name: source-command-latch-budget-approve
description: Unlock today's latch LLM budget. Use only when the user explicitly invokes latch-budget-approve or /latch-budget-approve.
---

# Latch budget approve for Cursor

Latch operation id: latch-budget-approve run

Latch Cursor skill boundary: this workflow is safe for project-synced skills
and the Cursor plugin. Never approve the budget proactively.

Before any Shell call, read the workspace `.cursor/mcp.json` and use
`mcpServers.latch.env.LATCH_PYTHON` when present, otherwise
`mcpServers.latch.command`, as `<CURSOR_MCP_PYTHON>` and `LATCH_PYTHON`. Never fall back to a
PATH `python3`; the MCP interpreter owns latch's native dependencies.
Use `latch_home` only to construct the absolute script path. Do not export
`LATCH_HOME` or `CLAUDE_KB_HOME` in the Shell call; managed Cursor operation
receipts do not allow those environment assignments.

Resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}` when set, otherwise use the
absolute checkout in the project-sync footer,
then invoke `<CURSOR_MCP_PYTHON> "$latch_home/src/budget.py" approve "$PWD"`,
with `LATCH_PYTHON` set to that same absolute interpreter. Report the JSON
fields `date`, `count_nonheal`, `count_heal`, and `approved_dates`. For a
read-only check, use the same command with `status` instead of `approve`.
