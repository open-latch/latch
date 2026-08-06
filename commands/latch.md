---
description: Choose this filesystem scope's Shared or Private KB
argument-hint: ""
---

Latch operation id: latch inspect

Latch only the current filesystem scope. The runtime is installed once. On a
fresh explicit-scope install, an unscoped location is LOCKED until its Shared or
Private KB is chosen, and descendants inherit the nearest scope. An upgraded
global-KB install instead reports `compatibility_global` and keeps using its
exact previous global KB until the user deliberately migrates. Do not change
another scope or the install-level pin.

Resolve `<KB_HOME>` to the installed Latch checkout, then inspect first:

```bash
bash "<KB_HOME>/bin/latch.sh"
```

Show the root, state, policy, KB, and binding source. Ask whether to keep an
UNLATCHED scope's previous binding, use the global KB as Shared, or use a clean
separate KB as Private. Make no change until the user replies exactly `latch`.

Then run one command and show its complete receipt:

```bash
bash "<KB_HOME>/bin/latch.sh" --confirm latch
bash "<KB_HOME>/bin/latch.sh" --confirm latch --shared
bash "<KB_HOME>/bin/latch.sh" --confirm latch --private --kb-dir "/absolute/kb/path"
bash "<KB_HOME>/bin/latch.sh" --confirm latch --private --new-kb
```

If status reports `compatibility_global` and the user explicitly wants every
other unscoped location to become LOCKED, explain the effect and ask for the
same exact `latch` confirmation before running this from a compatibility/Shared
location (never from a Private scope):

```bash
bash "<KB_HOME>/bin/latch.sh" --confirm latch --shared --require-explicit-scopes
```

This migration creates the current Shared boundary. It does not move content or
change any existing explicit scope.

A new KB starts clean. Never copy or import KB content automatically. When the
binding changed, tell the user to start a fresh agent task in this project and
not resume the old one; an idempotent receipt needs no new task. Do not describe KB
selection as a complete NDA clean-room boundary for install-level artifacts.
