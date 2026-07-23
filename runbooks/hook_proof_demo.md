# Proof-ready hook demo

Use this runbook to prove one thing: latch can surface a rejected path or
governance rule before a coding agent changes files. Keep the demo narrow.
This package stops at the hook/gate proof. Adapter-specific work, including
Cursor work, belongs in the next step after this proof is crisp.

## Success criteria

- A seed or fixture creates one concrete rejected path or governance rule.
- A later request tries to violate it.
- Latch shows an explicit gate receipt before edits.
- The receipt cites the decision/rule, rationale/source/status, and a better
  compliant path.
- The operator can verify the seed path, gate path, receipt shape, and clean
  working tree with commands.

## Preflight

Run from the latch checkout that is actually installed for the user:

```bash
cd /path/to/latch
bash bin/latch_status.sh
bash bin/latch_doctor.sh --skip-embed
```

For the proof path, `latch_status.sh` must report `[ENABLED ]`. If it says
`[DISABLED]`, use an enabled checkout or re-enable with `bash bin/latch_enable.sh`.
If it says `[UNLATCHED]`, run `/unlatch` and confirm re-latching before
continuing.

## 3-minute cold-start path

Use this when the user has no useful Claude/Codex history yet. It uses a
throwaway repo and throwaway KB, and does not read personal transcripts.

Offline fixture smoke test:

```bash
bash bin/latch_demo_no_history.sh --no-llm --keep --work-dir /tmp/latch-no-history-proof
```

The `--no-llm` command verifies fixture creation, seed insertion, and
gate-context retrieval without calling a classifier. It is not the live proof.
The live proof is the backend run that returns `MODIFY`, `DO_NOT_PROCEED`, or
`NEEDS_HUMAN_JUDGMENT` for the violating request.

Expected smoke-test receipt shape:

```text
Latch no-history demo
=====================

This fixture used no personal Claude/Codex history.
Fixture project: <tmp>/project/no-history-demo-app
Fixture KB: <tmp>/kb
Seeded decision: id=<n> (Keep the demo app local-first on SQLite — no hosted database)
Request: Add multi-user accounts by moving the datastore from local SQLite to a hosted Postgres service.

Latch gate receipt:
Latch ran latch_gate on the fixture request.
Recommendation: SKIPPED
Summary: Gate did not produce a recommendation: use_llm=False.
Gate note: use_llm=False
Cited evidence: classifier skipped or errored, but gate assembly retrieved the seeded decision id=<n>.

Expected proof:
A live classifier should return MODIFY or DO_NOT_PROCEED, cite the seeded governance decision, and recommend the local-first SQLite path before files change.
Offline mode: --no-llm skipped classifier judgment by design.
Kept fixture at: <tmp>
```

Live cold proof:

```bash
bash bin/latch_demo_no_history.sh --backend codex
# or, when Claude is the configured gate backend:
bash bin/latch_demo_no_history.sh --backend claude
```

Expected live receipt shape:

```text
Latch gate receipt:
Latch ran latch_gate on the fixture request.
Recommendation: MODIFY or DO_NOT_PROCEED
Summary: <states that the hosted-database move conflicts with the saved rule>
Risk if proceed: <names the hosted-database / network-dependency risk>
Cited evidence:
- id=<n> decision status=canonical: Keep the demo app local-first on SQLite — no hosted database
```

If the live run says `Recommendation: SKIPPED`, it is not the proof. Check
`latch_status.sh`, backend availability, and daily budget, then rerun.

## 15-minute concierge path

Use this with a real project and a real user. The target is one high-confidence
catch, not a broad knowledge-base tour.

Run the whole path from the user's project repo, not from the latch checkout,
unless latch itself is the project being demonstrated.

### Operator card

0-2 minutes: confirm the install is live.

```bash
cd /path/to/user/project
bash /path/to/latch/bin/latch_status.sh
bash /path/to/latch/bin/latch_doctor.sh --skip-embed

# If this project uses Codex, also verify the Codex surface.
bash /path/to/latch/bin/install_codex.sh --check
bash /path/to/latch/bin/latch_codex_doctor.sh
```

`latch_status.sh` must report `[ENABLED ]`. If the user chose only Claude Code
or only Codex, run the matching check/doctor pair for that surface. A skipped
gate, missing MCP server, or unlatched status is not the proof.

2-8 minutes: seed recent local sessions.

```bash
bash /path/to/latch/bin/latch_seed.sh --source both --last-sessions 20 --apply
```

Use `--source claude`, `--source codex`, `--source cursor`, `--source both`, or
`--source all` to match the
user's actual history. `--apply` prints the report first and writes only after
approval. If the first pass is thin, rerun once with a wider window such as
`--last-sessions 50`; do not turn the demo into an open-ended transcript tour.

8-11 minutes: pick exactly one proof target.

Choose the strongest rejected path, governance rule, or high-confidence prior
agent mistake. Good fuel has all of these:

- a concrete forbidden approach another coding agent might plausibly attempt,
- a reason or risk for the rejection,
- an allowed redirect or current path,
- source/status evidence in the seed report.

Skip vague preferences, broad product direction, workstream summaries, and
anything where the generated request would not obviously violate the saved
judgment.

11-13 minutes: run the generated catch demo and prove no edits happened.

The seed report should include this text shape:

```text
Latch receipt:
Latch built this first-wow report from <n> selected local source(s); it is a proof receipt, not a dashboard.
Why this mattered: It surfaced <counts> ... that future gates can cite before code changes.
Next proof: After applying this seed, run the catch-demo command below to watch latch_gate challenge the strongest rejected path or prior agent mistake before files change.

Try the catch demo:
- Claude Code: /latch-gate "<request>"
- Shell: bash /path/to/latch/bin/run_latch_gate.sh '<request>'
Expected: After you apply the seed, Latch should cite this seeded rejected path or prior agent-mistake evidence and ask whether to hold the line, redirect, or override it.
```

For the shell proof, copy the generated shell request into this check:

```bash
git status --short > /tmp/latch-proof.before
bash /path/to/latch/bin/run_latch_gate.sh '<generated request>' | tee /tmp/latch-gate-proof.json
git status --short > /tmp/latch-proof.after
diff -u /tmp/latch-proof.before /tmp/latch-proof.after
```

The diff must be empty. In a live agent demo, ask a fresh Claude Code or Codex
session to do the violating work and stop at the first foreground **Latch
gate** block; the transcript should show the gate block before any edit/write
tool call.

13-15 minutes: decide whether it was a proof.

Pass:

- the seed report showed a visible `Latch receipt`,
- the approved seed produced a catch-demo command,
- the gate returned `MODIFY`, `DO_NOT_PROCEED`, or `NEEDS_HUMAN_JUDGMENT`,
- the receipt cited a saved decision/rule/mistake with current status,
- the manual shell diff was empty, or the agent transcript showed the gate
  before edits.

Not a proof:

- `Recommendation: SKIPPED`, `recommendation: null`, or backend/budget errors,
- `PROCEED` on a request that plainly violates the saved judgment,
- no cited evidence nodes,
- any file edit before the gate receipt.

If the real-history path fails because the report has no strong target, run one
more focused seed pass with a wider session window or the other source. If it
still has no target, switch to the no-history fixture and say so plainly: the
fixture proves the mechanism, not the user's own project memory.

Capture the proof artifacts while they are fresh: seed report excerpt,
generated request, gate receipt, cited node ids/titles/status, before/after
`git status` diff, and the user's reaction.

## Expected JSON shapes

Seed report JSON:

```json
{
  "receipt": {
    "label": "Latch seed receipt",
    "source": "latch_seed",
    "must_display_to_user": true,
    "summary": "Latch built this first-wow report from <n> selected local source(s); it is a proof receipt, not a dashboard.",
    "why_it_matters": "It surfaced <counts> ... that future gates can cite before code changes.",
    "next_proof": "After applying this seed, run the catch-demo command below to watch latch_gate challenge the strongest rejected path or prior agent mistake before files change.",
    "used": {
      "sources": 1,
      "source_counts": {"claude": 0, "codex": 1},
      "candidates": 1,
      "direction_priorities": 0,
      "sections": {
        "decisions_and_rejected_paths": 1,
        "continuity_notes": 0,
        "where_left_off": 0,
        "patterns_and_preferences": 0,
        "agent_alignment_check": 0
      },
      "catch_demo": true
    }
  },
  "catch_demo": {
    "request": "Revive this rejected path: <evidence>",
    "slash_command": "/latch-gate \"Revive this rejected path: <evidence>\"",
    "shell_command": "bash /path/to/latch/bin/run_latch_gate.sh 'Revive this rejected path: <evidence>'",
    "requires_apply": true,
    "expected_outcome": "After you apply the seed, Latch should cite this seeded rejected path and ask whether to hold the line, redirect, or override it."
  }
}
```

Gate result JSON:

```json
{
  "ok": true,
  "request": "<request>",
  "findings": {
    "label": "Latch gate findings",
    "must_display_to_user": true,
    "source": "latch_gate",
    "recommendation": "MODIFY",
    "summary": "<plain-language conflict>",
    "risk_if_proceed": "<what breaks if the rejected path is revived>",
    "better_next_action": "<compliant redirect>",
    "decision_chain": [123],
    "abandoned_paths": [124],
    "active_constraints": [],
    "current_direction": [],
    "evidence_nodes": [
      {"id": 123, "kind": "decision", "title": "<saved rule>", "status": "canonical"}
    ],
    "load_bearing_claims": [
      {
        "claim": "<claim checked against KB evidence>",
        "evidence_type": "kb_node",
        "evidence_ref": 123,
        "gap_type": null
      }
    ],
    "uncovered_claims": [],
    "receipt": {
      "summary": "Latch ran the gate on this request and used <basis> to produce the verdict; cited node status carries current authority.",
      "source": "latch_gate",
      "used": {
        "decision_chain": 1,
        "abandoned_paths": 1,
        "active_constraints": 0,
        "current_direction": 0,
        "evidence_nodes": 1,
        "load_bearing_claims": 1,
        "uncovered_claims": 0
      },
      "authority": "Use evidence_nodes[].status as the visible current-authority surface; decision_chain, abandoned_paths, current_direction, and load_bearing_claims explain the rationale and source basis."
    },
    "why_it_matters": "Latch ran the gate on this request and used <basis> to produce the verdict; cited node status carries current authority."
  }
}
```

Acceptable live recommendations are `MODIFY`, `DO_NOT_PROCEED`, or
`NEEDS_HUMAN_JUDGMENT` when the request conflicts with the saved rule.
`PROCEED` means the proof target was weak or the gate missed it. `null` with
`SKIPPED` means no live judgment was produced.

## Verification commands for this package

Run these from the latch repo:

```bash
.venv/bin/python -m pytest tests/test_no_history_demo.py tests/test_run_kb_gate_wrapper.py tests/test_seed.py
.venv/bin/python -m pytest tests/test_gate.py
bash bin/latch_demo_no_history.sh --no-llm --keep --work-dir /tmp/latch-no-history-proof
```

The first pytest command verifies the no-history fixture, wrapper interpreter
resolution, seed receipt, and generated catch-demo payload. The gate pytest
command verifies gate assembly and receipt formatting. The no-history command
prints the local fixture smoke-test receipt.

## Recommended next step

After this package works in one live concierge install, build the minimal Cursor
adapter around the same proof path. Start with MCP config/installer/doctor and
the AGENTS/rules contract, then add a Cursor gate backend only if a
design-partner install needs Cursor-only model-backed gate calls.
