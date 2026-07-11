---
name: source-command-latch-budget-approve
description: Unlock today's latch LLM budget. Use only when the user explicitly invokes latch-budget-approve or /latch-budget-approve.
---

# Latch budget approve for Cursor

Latch operation id: latch-budget-approve run

Latch Cursor skill boundary: this workflow is safe for project-synced skills
and the Cursor plugin. Never approve the budget proactively.

Resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}` when set, otherwise use the
absolute checkout in the project-sync footer,
then run `python "$latch_home/src/budget.py" approve "$PWD"`. Report the JSON
fields `date`, `count_nonheal`, `count_heal`, and `approved_dates`. For a
read-only check, use the same command with `status` instead of `approve`.
