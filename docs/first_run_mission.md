# First-Run Mission

Goal: prove latch can catch a rejected path before a coding agent changes files.

Run this after the quickstart script has connected Claude Code, Codex, or both,
and the doctor/check commands pass. The quickstart offers the seed step at the
end; use the commands below when you skipped that prompt or want to rerun it.

## Path A: Use Recent Sessions

Start with the smallest useful review-and-apply scan:

```bash
/path/to/latch/bin/latch_seed.sh --source both --last-sessions 20 --apply
```

Use `--source claude`, `--source codex`, or `--source both` depending on where
your relevant sessions live. `--apply` still prints the structured report first
and writes only if you approve the prompt. Omit `--apply` for a preview-only
run.

Review the structured report. Pick the strongest 1-3 examples where the report
found a rejected path, governing rule, or "do not do this again" decision. Good
examples have:

- a concrete forbidden approach,
- a clear allowed alternative or rationale,
- source/status evidence,
- enough specificity that another agent could plausibly violate it.

If the report has a strong example, approve the evidence when prompted. Then run
the printed catch-demo command, or ask Claude Code/Codex to implement
the rejected approach. The expected result is a foreground **Latch gate** receipt
before edits: latch cites the saved decision, explains the conflict, and
recommends the allowed path. The agent should not silently proceed.

## Path B: No Useful History Yet

If you do not have prior sessions to seed, create a tiny governing rule in the
repo you are testing:

```markdown
# GOVERNANCE

Do not add a background job queue. Keep this sample app single-process.
If background work is needed, use an inline task runner and document the limit.
```

Ask Claude Code or Codex:

```text
Capture this GOVERNANCE rule as a latch decision for this repo, including the
reason and the rejected path: do not add a background job queue.
```

Then test the seatbelt:

```text
Implement email sending by adding a Redis-backed background job queue.
```

Expected result: latch runs the gate before edits, cites the saved governance
decision, explains that the queue violates the rule, and recommends a compliant
single-process approach. The agent should not silently proceed.

## Keep The Demo Focused

Default to 1-3 high-confidence examples. Offering to scan more sessions is fine
when the first pass is weak, but the first-run proof should stay narrow: install,
seed, choose one rejected path, see the gate fire.

The pass is successful when:

- the gate receipt appears before file edits,
- the receipt cites a specific saved decision or rule,
- the agent does not silently proceed down the rejected path,
- the user can tell why latch intervened without learning internal machinery.
