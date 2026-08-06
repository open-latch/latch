---
description: Confirmed Latch Unlatched-mode toggle
argument-hint: ""
---

Latch operation id: unlatch inspect

In Global Shared mode, Unlatch retains its install-wide behavior: every repo is
off until `latch` restores the installation. In project mode, it turns Latch off
only for the current scope; descendants and authorized aliases follow it while
other scopes remain unchanged. Neither mode copies, imports, or deletes KB
content.

Inspect first:

```bash
bash "<KB_HOME>/bin/unlatch.sh"
```

If LATCHED, ask the user to reply exactly `unlatch`; explain the reported
install-wide or same-scope effect before confirmation. If UNLATCHED, ask for exactly
`latch`; explain that its previous KB binding will be preserved. Stop until the
exact confirmation.

After `unlatch`:

```bash
bash "<KB_HOME>/bin/unlatch.sh" --confirm unlatch
```

Warn that temporary Latch-owned edits to managed `CLAUDE.md` / `AGENTS.md`
regions may appear in Git and should not be committed; `/latch` restores them.

After `latch`:

```bash
bash "<KB_HOME>/bin/latch.sh" --confirm latch
```

Show the full receipt. If an install-wide sentinel or environment override is
reported, do not claim project separation. Do not describe
project-local mode as a complete NDA clean room for install-level artifacts.
When the mode changed, tell the user to start a fresh agent task and not resume
the old one so the instruction mask takes effect; an idempotent receipt needs
no new task.
