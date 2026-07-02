---
description: Unlock the rest of today for unlimited latch LLM invocations in this project
---

The KB auto-tooling has two daily caps as a backstop against runaway hook loops:

- **100 non-heal calls/day** — compactor, latch_gate, tree summarization,
  on-insert heal arbitration.
- **50 heal calls/day** — nightly heal LLM arbitration only.

When either cap is hit, the matching path no-ops (compactor returns
`budget_cap`, heal falls back to `keep_both`, etc.) until UTC rollover.
This command unlocks BOTH categories for the remainder of today.

Only run this when the user has explicitly asked for it. The caps exist for
cost safety — do not approve on your own initiative.

Run with the Bash tool:

```bash
python "<KB_HOME>/src/budget.py" approve "$(pwd)"
```

Report back the current state from the command's JSON output (`date`,
`count_nonheal`, `count_heal`, `approved_dates`). After approval:

- Both counters reset to 0
- Today's date is added to `approved_dates`
- Further LLM calls in either category bypass the caps until UTC rollover
- Tomorrow's UTC day returns to normal cap behaviour

To see the current budget state without approving, run:

```bash
python "<KB_HOME>/src/budget.py" status "$(pwd)"
```
