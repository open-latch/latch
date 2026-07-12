---
name: source-command-latch-seed
description: Preview and apply latch seed candidates from the exact current Cursor conversation. Use when the user invokes latch-seed or /latch-seed in Cursor.
---

# Latch seed for Cursor

Latch operation id: latch-seed preview

Latch Cursor skill boundary: use only the current `sessionStart` marker or a
transcript path the user explicitly supplied. Never scan private Cursor history
directories.

Read the exact `Latch Cursor session id` from the current SessionStart context
and pass `--cursor-session-id ID` to the wrapper. Never omit it or reuse an id
from another chat.

Resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}` when set, otherwise use the
absolute checkout in the project-sync footer. Select native `cursor` for plugin
installs or the backend in that footer. Run the host-appropriate `bin/latch_seed.sh` or `.ps1`
wrapper with `--source cursor --format json` and the seed/model backend
environment set. First run preview-only and show the receipt/candidates. The
attempt alone does not arm apply: only a matching successful JSON result
verified by `postToolUse` does. Failed, malformed, missing, cross-session, or
unexecuted previews require a new preview. Only after the user
approves that preview, ask them to reply exactly `/latch-seed apply`, then rerun
with `--apply --yes`. Report inserted staging node
ids and the catch-demo command. A missing/mismatched marker is a hard stop.
