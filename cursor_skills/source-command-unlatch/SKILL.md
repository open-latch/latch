---
name: source-command-unlatch
description: Confirmed toggle for latch Unlatched mode. Use when the user invokes unlatch or /unlatch in Cursor.
---

# Unlatch for Cursor

Latch Cursor skill boundary: resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}`
when set, otherwise use the absolute checkout in the project-sync footer.
Inspect state with the host-appropriate
`bin/unlatch.sh` or `.ps1` wrapper. Never mutate immediately. If latched, ask
the user to reply exactly `unlatch`; if unlatched, ask for exactly `latch`.
Only after that reply run the wrapper with `--confirm unlatch` or
`--confirm latch`. Explain that KB data remains, latch stays off across repos
until re-latched, and this is an escape hatch—not a controlled benchmark.
