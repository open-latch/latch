# First-Run Mission

Goal: prove latch can catch a rejected path before a coding agent changes files.

Run this after the quickstart has connected Claude Code, Codex, Cursor, or all,
and the selected project reports LATCHED. On a fresh explicit-scope install, an
unscoped location starts LOCKED: choose Shared (the existing global KB) or
Private (a clean separate KB) with `latch` before any seed is offered. Upgraded
global-KB installs retain `compatibility_global` behavior until deliberately
migrated. Descendants inherit the nearest filesystem scope.
Use the commands below when you skipped the seed prompt or want to rerun it.

For the proof-ready package with timed paths, exact receipt shapes, and
verification commands, see
[`runbooks/hook_proof_demo.md`](../runbooks/hook_proof_demo.md).

## Path A: Use Recent Sessions

Start with the smallest useful review-and-apply scan:

```bash
cd /path/to/user/project
/path/to/latch/bin/latch_seed.sh --source both --last-sessions 20 --apply
```

Use `--source claude`, `--source codex`, `--source cursor`, `--source both`, or
`--source all` depending on where your relevant sessions live. Cursor defaults
to the current hook-provided transcript or an explicit `--cursor-transcript`.
Add `--cursor-history` to opt in to top-level local IDE conversations for this
project only. Latch requires each conversation id to match Cursor's local
project membership and non-subagent header; Cursor CLI sessions, cloud chats,
other projects, and subagents remain excluded. Missing metadata fails closed
for Cursor history: Cursor-only seeding stops, while `--source all` continues
with other authorized sources and reports Cursor history unavailable. `--apply`
still prints the structured report first and writes only if you approve the
prompt. Enter `none` to dismiss the whole
report and finalize those exact source revisions without creating nodes. For a
cached review, repeat the preview's exact `--source` (and workstream flags)
alongside `--preview-digest DIGEST --apply --dismiss-all`. Omit
`--apply` for a preview-only run.

Review the structured report. Pick one strongest example where the report found
a rejected path, governing rule, or "do not do this again" decision. Good
examples have:

- a concrete forbidden approach,
- a clear allowed alternative or rationale,
- source/status evidence,
- enough specificity that another agent could plausibly violate it.

If the report has a strong example, approve the evidence when prompted. Then run
the printed catch-demo command, or ask Claude Code/Codex/Cursor to implement
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
your Claude, Codex, or Cursor history. If you are running it from a plain shell after a
Codex-only install, pass `--backend codex`.

To run the same shape manually, create a tiny governing rule in the repo you are
testing:

```markdown
# GOVERNANCE

Keep this app local-first: one embedded SQLite file, no server and no account.
Do not move the datastore to a hosted or client-server database. If data must
move between machines, add an explicit export/import step and document its limit.
```

Ask Claude Code, Codex, or Cursor:

```text
Capture this GOVERNANCE rule as a latch decision for this repo, including the
reason and the rejected path: do not move the datastore to a hosted database.
```

Then test the gate:

```text
Add multi-user accounts by moving the datastore from local SQLite to a hosted Postgres service.
```

Expected result: latch runs the gate before edits, cites the saved governance
decision, explains that the hosted-database move violates the rule, and
recommends a compliant local-first approach. The agent should not silently proceed.

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
