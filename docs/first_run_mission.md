# First-Run Mission

Goal: prove latch can catch a rejected path before a coding agent changes files.

Run this after the quickstart script has connected Claude Code, Codex, or both,
and the doctor/check commands pass. The quickstart offers the seed step at the
end; use the commands below when you skipped that prompt or want to rerun it.

For the proof-ready package with timed paths, exact receipt shapes, and
verification commands, see
[`runbooks/hook_proof_demo.md`](../runbooks/hook_proof_demo.md).

## Path A: Use Recent Sessions

Start with the smallest useful review-and-apply scan:

```bash
cd /path/to/user/project
/path/to/latch/bin/latch_seed.sh --source both --last-sessions 20 --apply
```

Use `--source claude`, `--source codex`, or `--source both` depending on where
your relevant sessions live. `--apply` still prints the structured report first
and writes only if you approve the prompt. Omit `--apply` for a preview-only
run.

Review the structured report. Pick one strongest example where the report found
a rejected path, governing rule, or "do not do this again" decision. Good
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

For the shell proof, verify the gate did not edit files:

```bash
git status --short > /tmp/latch-proof.before
/path/to/latch/bin/run_latch_gate.sh '<generated request>' | tee /tmp/latch-gate-proof.json
git status --short > /tmp/latch-proof.after
diff -u /tmp/latch-proof.before /tmp/latch-proof.after
```

The diff should be empty. Treat `SKIPPED`, `recommendation: null`, empty cited
evidence, or `PROCEED` on a plainly violating request as a failed proof target;
widen the session window once or switch sources before falling back to Path B.

## Path B: No Useful History Yet

If you do not have prior sessions to seed, run the turnkey fixture:

```bash
/path/to/latch/bin/latch_demo_no_history.sh
# Windows: C:\path\to\latch\bin\latch_demo_no_history.ps1
```

It creates a throwaway sample repo and throwaway KB, seeds one public-safe
rejected-path decision, runs the gate, and prints the receipt. It does not read
your Claude or Codex history. If you are running it from a plain shell after a
Codex-only install, pass `--backend codex`.

To run the same shape manually, create a tiny governing rule in the repo you are
testing:

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

Default to one high-confidence example. Offering to scan more sessions is fine
when the first pass is weak, but the first-run proof should stay narrow:
install, seed, choose one rejected path, see the gate fire.

The pass is successful when:

- the gate receipt appears before file edits,
- the receipt cites a specific saved decision or rule,
- the recommendation is `MODIFY`, `DO_NOT_PROCEED`, or
  `NEEDS_HUMAN_JUDGMENT`,
- the agent does not silently proceed down the rejected path,
- the user can tell why latch intervened without learning internal machinery.
