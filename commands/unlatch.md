---
description: Confirmed toggle for latch Unlatched mode
argument-hint: ""
---

Use this when the user wants latch out of the way, wants to compare against
vanilla agent behavior, or wants to re-latch after using the escape hatch.

Unlatched mode is in-place and install-level. It does not clone the project,
delete KB data, uninstall latch, call a cloud service, or collect telemetry. It
creates a visible UNLATCHED receipt plus the normal full-disable sentinel so
latch's automatic project-judgment layer is off for this latch install until the
user re-latches. If the user changes repos before re-latching, latch remains off
and should say so loudly. It also masks latch's managed `CLAUDE.md` /
`AGENTS.md` regions in the current project/ancestor files while unlatched, then
restores them when re-latched, so native project-instruction loading does not
keep carrying latch's contract in the repo where the user unlatched.

Never mutate state immediately from this command, even in dangerous/yolo
permission modes. First inspect state and ask for explicit confirmation.

## Inspect

```bash
bash <KB_HOME>/bin/unlatch.sh
```

If the output says LATCHED, tell the user:

```text
Latch is currently LATCHED.

Switch to UNLATCHED mode?
This turns latch's project-judgment layer off for this latch install, masks latch-managed CLAUDE.md/AGENTS.md regions in this project, and leaves KB data intact.
Latch remains off for this latch install, even if you change repos, until you re-latch.
To re-latch later, run /unlatch again.

Reply exactly: unlatch
```

If the output says UNLATCHED, tell the user:

```text
Latch is currently UNLATCHED.

Switch back to LATCHED mode?
Latch hooks will resume on the next prompt unless LATCH_UNLATCHED is set.
If LATCH_UNLATCHED is set, unset it too.

Reply exactly: latch
```

Stop there. Do not run the state-changing command until the user replies with
the exact confirmation word.

## Confirmed Action

Only after the user replies exactly `unlatch`:

```bash
bash <KB_HOME>/bin/unlatch.sh --confirm unlatch
```

Only after the user replies exactly `latch`:

```bash
bash <KB_HOME>/bin/unlatch.sh --confirm latch
```

After it returns, show the receipt plainly. If latch is now unlatched, say:
"Latch is currently UNLATCHED. Run /unlatch to re-latch. If LATCH_UNLATCHED is
set, unset it too." If latch is latched again, say latch hooks resume on the
next prompt unless an environment disable flag remains set. Do not describe this
as a controlled benchmark or statistical A/B test.
