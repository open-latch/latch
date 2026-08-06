---
description: Confirmed project-local Unlatch toggle
argument-hint: ""
---

Latch operation id: unlatch inspect

Unlatch turns Latch off for the current scope. Descendants and any authorized
root aliases of that same scope follow its mode; every other scope, every KB,
and the installation remain unchanged. It never copies, imports, or deletes KB
content.

Inspect first:

```bash
bash "<KB_HOME>/bin/unlatch.sh"
```

If LATCHED, ask the user to reply exactly `unlatch`; explain the same-scope
descendant/alias effect before confirmation. If UNLATCHED, ask for exactly
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

Show the full receipt. If a legacy install-wide sentinel or environment
override is reported, do not claim project separation. Do not describe
project-local mode as a complete NDA clean room for install-level artifacts.
When the mode changed, tell the user to start a fresh agent task and not resume
the old one so the instruction mask takes effect; an idempotent receipt needs
no new task.
