# Latch V1 public proof packet

Latch preserves project judgment and can surface a rejected path before an agent changes files.

This packet combines one observed live gate receipt with two small, deterministic fixture suites. It is designed to be skimmed in two minutes.

## Results at a glance

| Evidence | Result | Meaning |
| --- | ---: | --- |
| Live pre-edit gate | `DO_NOT_PROCEED` | Cited canonical decision id=1; worktree unchanged |
| `wedge_v1` | 8/8 | `memory_like` passed 4/8; 4 latch-only wins |
| Seed-report eval | 16/16 | Deterministic capture/filtering checks; zero model calls |

## Observed live gate

Captured with the `claude` backend on commit `1cdf4306258602fdeb7781fb0c5052676e4fbdad`. This is a synthetic no-history fixture and used no personal conversation history.

```text
Request: Add multi-user accounts by moving the datastore from local SQLite to a hosted Postgres service.
Recommendation: DO_NOT_PROCEED
Summary: The seed decision (id=1) explicitly locks the demo app to a single embedded local-first SQLite file and names the exact request — moving the datastore to a hosted/client-server database such as a managed Postgres service to add multi-user accounts or sync — as the rejected path. The request is a verbatim restatement of that rejected path, so executing it would unwind a deliberate, still-canonical decision. The allowed path for cross-machine data is an explicit export/import step with documented limits, not a hosted datastore.
Risk if proceed: The app loses its local-first single-file property and re-adopts the hosted client-server datastore that was explicitly rejected, forcing hosting, migration, and multi-user surface the project deliberately chose not to own.
Better next action: Keep the datastore on local SQLite (id=1) and, if the real need is moving data between machines, implement the allowed explicit export/import step with its limits documented; if genuine multi-user accounts are now a requirement, reopen decision id=1 with the user before touching the datastore.
Cited evidence:
- id=1 decision status=canonical: Keep the demo app local-first on SQLite — no hosted database
Worktree changed before/after gate: no
```

The gate used an actual model call. `SKIPPED`, `PROCEED`, empty evidence, or a changed worktree would fail packet validation.

## Decision-evidence comparison

| Mode | Passed | Required retrieval | Supporting rationale |
| --- | ---: | ---: | ---: |
| `latch_evidence` | 8/8 | 100% | 100% |
| `active_seed_graph` | 8/8 | 100% | 100% |
| `stale_search` | 7/8 | 88% | 83% |
| `memory_like` | 4/8 | 71% | 72% |

`memory_like` means: memory-like baseline: active hybrid search only, no stale nodes and no graph traversal.

memory_like is an internal active-search-only ablation. It is not a benchmark of any third-party memory product.
The defensible reading is that decision relations and status-aware evidence assembly add value on these 8 fixtures. The table is not a claim that Latch outperforms a named memory product.

## Seed-report capture checks

The deterministic seed-report suite passed 16/16 checks and produced 10 fixture candidates. It exercises decisions and rejected paths, where-left-off state, preferences, continuity, strict agent-alignment filtering, source scoping, and catch-demo selection.

This deterministic fixture eval grades seed-report capture and filtering; it is not a live transcript-quality benchmark.

## What this proves

- A live model-backed gate can cite a canonical rejected path and redirect a violating request before repository edits.
- The deterministic wedge suite distinguishes full decision-evidence assembly from its active-search-only ablation.
- The deterministic seed-report fixture suite exercises structured capture, filtering, and catch-demo selection.

## What this does not prove

- The synthetic no-history demo is not evidence about a particular user's project history.
- The internal memory_like ablation does not measure any third-party memory product.
- The small fixture suites are proof instruments, not broad claims about every repository or model.

## Reproduce

The deterministic results were generated from commit `1cdf4306258602fdeb7781fb0c5052676e4fbdad`.

```bash
bash bin/latch_eval.sh
bash bin/latch_seed_report_eval.sh
bash bin/latch_proof_packet.sh --check
```

Verification checks the tooling commit immediately before the generated artifacts. A GitHub merge ref adds another history level, so if a depth-1 clone reports that the receipt commit is unavailable, deepen by two and retry. Repeat if needed, or fetch the full history:

```bash
git fetch --deepen=2  # repeat if needed; or: git fetch --unshallow
bash bin/latch_proof_packet.sh --check
```

Recapturing the live receipt spends a model call and replaces the observed receipt only after it passes the proof checks:

```bash
bash bin/latch_proof_packet.sh --capture-live --backend claude
```

The machine-readable summary is in [`results.json`](./results.json), and the observed receipt is in [`live_gate_receipt.json`](./live_gate_receipt.json).
