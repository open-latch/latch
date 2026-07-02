---
description: Read-only report over recent latch gate activity
argument-hint: "[--days 14 | --start YYYY-MM-DD --end YYYY-MM-DD] [--limit 10]"
---

Show a read-only, friendly proof receipt over recent `latch_gate` activity. This
command summarizes structural logs only: gate verdicts, outcome rows, adversary
deltas, explicit decision signals, top cited KB node ids/status, priority
evidence, and uncovered-claim/gap counts.

It does not run a new gate, does not read raw prompt text or KB bodies from the
logs, and does not write decisions. Treat it as a compact value report for how
latch has been applying project judgment recently, not as
analytics/RL/dashboarding.

## Run

```bash
bash <KB_HOME>/bin/latch_gate_report.sh $ARGUMENTS
```

After the command returns, show the report as-is. If the user asks for a cited
node body, fetch that node with `latch_get(<id>)` separately.
