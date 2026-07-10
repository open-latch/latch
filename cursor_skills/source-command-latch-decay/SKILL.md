---
name: source-command-latch-decay
description: Apply weekly latch decay and promote staging nodes. Use when the user invokes latch-decay or /latch-decay in Cursor.
---

# Latch decay for Cursor

Latch Cursor skill boundary: resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}`
when set, otherwise use the absolute checkout in the project-sync footer.
Select native `cursor` for plugin installs or the backend in that footer. Run
`python "$latch_home/src/maintenance.py" weekly "$(pwd)"` with
`LATCH_MAINTENANCE_BACKEND` and `LATCH_MODEL_BACKEND` set to that backend.
Report `decayed_rows`, `promoted_count`, and `promoted_ids`.
