---
name: source-command-latch
description: Choose this filesystem scope's Shared or Private KB without changing other scopes.
---

# Latch this project

Latch operation id: latch inspect

Latch Cursor skill boundary: resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}`
when set, otherwise use the absolute checkout in the project-sync footer. This
workflow changes only the current filesystem scope. Latch's runtime is installed
once; on a fresh explicit-scope install an unscoped location is LOCKED until the
user explicitly chooses Shared or Private. An upgraded global-KB install instead
reports `compatibility_global` and keeps using the exact old global KB until the
user deliberately migrates. Descendants inherit the nearest scope.

Before any Shell call, read the workspace `.cursor/mcp.json` and use
`mcpServers.latch.env.LATCH_PYTHON` when present, otherwise
`mcpServers.latch.command`, as `LATCH_PYTHON`. Never fall back to PATH `python3`;
the MCP interpreter owns Latch's runtime. Use `latch_home` only to
construct the absolute script path. Do not export `LATCH_HOME` or
`CLAUDE_KB_HOME` in the Shell call.

Run the installed Latch checkout's host-appropriate wrapper without arguments
first and show the root, state, policy, KB, and binding source. Ask whether to
keep an UNLATCHED scope's previous binding, use the existing global KB as
Shared, or use a clean separate KB as Private.

Only after the exact confirmation `latch`, run one:

```bash
bash "$latch_home/bin/latch.sh" --confirm latch
bash "$latch_home/bin/latch.sh" --confirm latch --shared
bash "$latch_home/bin/latch.sh" --confirm latch --private --kb-dir "/absolute/kb/path"
bash "$latch_home/bin/latch.sh" --confirm latch --private --new-kb
```

In PowerShell, use the native wrapper instead:

```powershell
& "$latch_home/bin/latch.ps1" -Confirm latch
& "$latch_home/bin/latch.ps1" -Confirm latch -Shared
& "$latch_home/bin/latch.ps1" -Confirm latch -Private -KbDir "C:\absolute\kb\path"
& "$latch_home/bin/latch.ps1" -Confirm latch -Private -NewKb
```

If status reports `compatibility_global` and the user explicitly wants every
other unscoped location to become LOCKED, explain the effect and require the
same exact `latch` confirmation. Run only from a compatibility/Shared location,
never from a Private client scope:

```bash
bash "$latch_home/bin/latch.sh" --confirm latch --shared --require-explicit-scopes
```

```powershell
& "$latch_home/bin/latch.ps1" -Confirm latch -Shared -RequireExplicitScopes
```

The migration creates the current Shared boundary and changes no existing
explicit scope or KB content.

This is project-local. Never change the install-level KB pin or move/copy/import
content. Show the complete receipt and, when the binding changed, tell the user
to start a fresh agent task in this project and not resume the old one. An
idempotent receipt needs no new task. A
separate KB is not a complete NDA clean-room boundary for
install-level artifacts.
