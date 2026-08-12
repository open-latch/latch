---
name: source-command-unlatch
description: Confirmed toggle for Latch Unlatched mode.
---

# source-command-unlatch

Latch operation id: unlatch inspect

Latch Cursor skill boundary: resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}`
when set, otherwise use the absolute checkout in the project-sync footer. In
Global Shared mode the workflow turns Latch off install-wide. In project
mode it changes only the current filesystem scope; descendants and authorized
root aliases of that same scope follow its mode.

Before any Shell call, read the workspace `.cursor/mcp.json` and use
`mcpServers.latch.env.LATCH_PYTHON` when present, otherwise
`mcpServers.latch.command`, as `LATCH_PYTHON`. Never fall back to PATH `python3`;
the MCP interpreter owns Latch's runtime. Use `latch_home` only to
construct the absolute script path. Do not export `LATCH_HOME` or
`CLAUDE_KB_HOME` in the Shell call.

Run `bash "$latch_home/bin/unlatch.sh"` to inspect on POSIX/Git Bash, or
`& "$latch_home/bin/unlatch.ps1"` in PowerShell. Explain the status receipt's
install-wide or project-local effect before confirmation. Require an
exact `unlatch` reply before running the matching wrapper with
`--confirm unlatch` / `-Confirm unlatch`, or exact `latch` before running the
Latch wrapper with `--confirm latch` / `-Confirm latch`. Show the full receipt.
When the mode changed, tell the user to start a fresh agent task and not resume
the old one so the instruction mask takes effect; an idempotent receipt needs
no new task.
Warn that temporary managed instruction-file edits may appear in Git and should
not be committed; latching restores them.
Never claim project separation when status reports a legacy install-wide or
environment override, or a complete NDA clean room for install-level artifacts.
