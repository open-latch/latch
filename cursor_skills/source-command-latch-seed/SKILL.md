---
name: source-command-latch-seed
description: Preview and apply latch seed candidates from the exact current Cursor conversation. Use when the user invokes latch-seed or /latch-seed in Cursor.
---

# Latch seed for Cursor

Latch Cursor skill boundary: use only the current `sessionStart` marker or a
transcript path the user explicitly supplied. Never scan private Cursor history
directories.

Resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}` when set, otherwise use the
absolute checkout in the project-sync footer. Select native `cursor` for plugin
installs or the backend in that footer. Run the host-appropriate `bin/latch_seed.sh` or `.ps1`
wrapper with `--source cursor` and the seed/model backend environment set.
First run preview-only and show the receipt/candidates. Only after the user
approves that preview, rerun with `--apply --yes`. Report inserted staging node
ids and the catch-demo command. A missing/mismatched marker is a hard stop.
