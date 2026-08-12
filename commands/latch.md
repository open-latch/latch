---
description: Choose this filesystem scope's Shared or Private KB
argument-hint: ""
---

Latch operation id: latch inspect

The runtime is installed once. In Global Shared mode, every repo keeps using
the installed global KB and this command changes nothing unless the user
explicitly enables project scopes. In project mode, latch only the current
filesystem scope; unscoped locations are LOCKED and descendants inherit the
nearest scope. Do not change another scope or the install-level pin.

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

If status reports `shared_global` and the user explicitly wants consulting
mode, explain that the choice is one-way and every other unscoped location
becomes LOCKED. Ask for the same exact `latch` confirmation, then run one
explicit first-root choice:

```bash
bash "<KB_HOME>/bin/latch.sh" --confirm latch --enable-project-scopes --shared
bash "<KB_HOME>/bin/latch.sh" --confirm latch --enable-project-scopes --private --new-kb
```

This transition creates the current boundary. It does not move content.

A new KB starts clean. Never copy or import KB content automatically. When the
binding changed, tell the user to start a fresh agent task in this project and
not resume the old one; an idempotent receipt needs no new task. Do not describe KB
selection as a complete NDA clean-room boundary for install-level artifacts.
