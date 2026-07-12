---
name: source-command-latch-heal
description: Run nightly latch heal and contradiction resolution. Use when the user invokes latch-heal or /latch-heal in Cursor.
---

# Latch heal for Cursor

Latch operation id: latch-heal run

Latch Cursor skill boundary: resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}`
when set, otherwise use the absolute checkout in the project-sync footer.
Select native `cursor` for plugin installs or the backend in that footer. Run
`python "$latch_home/src/maintenance.py" nightly "$PWD"` with
`LATCH_MAINTENANCE_BACKEND` and `LATCH_MODEL_BACKEND` set. Report `examined`,
`collisions`, `superseded`, `kept_both`, per-path counts, and `budget_blocked`.
