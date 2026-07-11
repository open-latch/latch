---
name: source-command-latch-tree
description: Rebuild the latch hierarchical cluster and summary tree. Use when the user invokes latch-tree or /latch-tree in Cursor.
---

# Latch tree for Cursor

Latch operation id: latch-tree run

Latch Cursor skill boundary: resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}`
when set, otherwise use the absolute checkout in the project-sync footer.
Select native `cursor` for plugin installs or the backend in that footer. Run
`python "$latch_home/src/maintenance.py" tree "$(pwd)"` with
`LATCH_MAINTENANCE_BACKEND` and `LATCH_MODEL_BACKEND` set. Report linkage,
leaves, landmarks, clusters, summary counts, singletons, skipped oversize
clusters, budget blocking, LLM failures, and stale prior summaries.
