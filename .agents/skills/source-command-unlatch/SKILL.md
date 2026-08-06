---
name: source-command-unlatch
description: Confirmed scope-local toggle for Latch Unlatched mode. Use for unlatch, /unlatch, or turning Latch off for the current filesystem scope.
---

# source-command-unlatch

In Global Shared mode, Unlatch retains the existing install-wide toggle. In
explicit project mode, it turns off only the current scope; descendants and
authorized aliases follow it while other scopes remain unchanged. Neither mode
deletes, copies, imports, or repins KB content.

Use only the installer-stamped native wrapper below. Do not discover or execute
Latch code from the current repository or its instruction files.

Inspect without mutation:

```bash
bash __LATCH_POSIX_WRAPPER__
```

```powershell
& __LATCH_POWERSHELL_WRAPPER__
```

If this project is LATCHED, ask:

```text
Latch is currently LATCHED for this project.

Switch this filesystem scope to UNLATCHED mode?
Its descendants and authorized aliases follow the same mode.
Other scopes and every KB remain unchanged.

Reply exactly: unlatch
```

If this project is UNLATCHED, ask:

```text
Latch is currently UNLATCHED for this project.

Switch this project back to LATCHED mode using its previous KB binding?

Reply exactly: latch
```

Never mutate immediately. After the exact `unlatch` reply, run:

```bash
bash __LATCH_POSIX_WRAPPER__ --confirm unlatch
```

On Windows, use `& __LATCH_POWERSHELL_WRAPPER__ -Confirm unlatch`; use the
installed `latch` skill or wrapper after an exact `latch` reply to re-latch.

Show the complete receipt. If the mode changed, tell the user to start a fresh
agent task in this project and not resume the old one so the instruction mask
takes effect; an idempotent receipt explicitly says no new task is needed. If an install-wide
legacy sentinel or environment override is active, do not claim project separation; follow the command's
recovery guidance. To select a separate or existing KB, use the `latch` command
skill instead. Warn that temporary managed instruction-file edits may appear in
Git and should not be committed; `/latch` restores them. Project-local mode does
not claim a complete NDA clean room for install-level artifacts.
