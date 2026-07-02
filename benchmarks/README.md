# Latch Benchmarks

Latch's public benchmark surface is aimed at the wedge against generic memory:
agents do not merely need remembered text, they need current project judgment.

The first suite, `wedge_v1`, seeds throwaway KBs and checks whether latch's
deterministic gate assembly retrieves the right decision evidence before any LLM
classifier is involved. It now reports several modes:

- `latch_full`: stale-aware hybrid seeds plus graph traversal over decision
  relations.
- `active_seed_graph`: active-only hybrid seeds plus graph traversal. This asks
  whether the graph can recover the decision chain even when stale/foundational
  nodes are not directly searchable.
- `stale_search`: stale-aware hybrid search only, without graph traversal. This
  asks whether direct retrieval alone is enough.
- `memory_like`: active hybrid search only, with no stale nodes and no graph
  traversal.

Each fixture is a decision story:

- a prompt that revives or probes project history
- a difficulty band (`smoke`, `adversarial`, `negative_control`, later
  `regression`)
- decision nodes with status and rationale
- stale or reconciled alternatives
- source notes for sanitized real-world failures, when a case came from a
  private workflow example
- the memory trap a generic memory feature might fall into
- required supporting phrases that must be present in retrieved decision
  evidence
- gate receipt visibility: evidence must make it obvious that Latch ran the
  gate, what source/rationale it used, and which node status carries current
  authority

Run the default suite:

```bash
bash bin/latch_eval.sh
```

Emit JSON for CI or dashboards:

```bash
bash bin/latch_eval.sh --format json --output reports/wedge_v1.json
```

The runner always uses temporary KBs, even on a pinned latch install. It does
not read or write a user's live project DB. Private/user-specific eval packs can
reuse the same JSONL schema without shipping their transcript-derived fixtures
back into the public repo.

The current score should be read as a comparison, not as a raw 100%. A useful
report shows whether `latch_full` keeps passing as difficulty rises, whether it
beats the `memory_like` baseline on cases that require stale rejected paths,
reconciliation context, or explicit rationale, and whether ablations show what
capability caused the win.

Private/user-provided examples should be anonymized before they enter public
fixtures. Keep raw transcript paths, project names, and domain-specific details
out of the repo unless the fixture pack is intentionally private. The sanitized
case `adversarial_foundational_rationale_recall` came from a real
rationale-recall failure: an agent reported an operational metric that looked
like a reversal, but the governing strategic rationale said the result was
expected and should not change direction by itself.

## Execution Plan

The benchmark should stay sharp enough to defend latch as an agentic knowledge
and judgment surface, not a generic memory feature.

1. **Evidence assembly evals.** The current `wedge_v1` suite checks whether
   latch retrieves the right decision objects, rejected paths, rationale,
   source/receipt fields, current authority, and reconciliation context before
   any LLM classifier runs. It should grow by adding harder fixtures and
   baseline comparisons, not by celebrating an easy 100%.
2. **Gate verdict evals.** Add fixtures with stubbed or recorded classifier
   outputs, then grade catch rate, wrong-block rate, abstention, and citation
   support for prompts that revive ruled-out paths.
3. **Recovered-why evals.** Grade explanation paths as recovered-correct,
   recovered-stale, invented, miss, or correct-abstention. Use LLM judges only
   after deterministic citation checks are exhausted.
4. **Seed report evals.** Feed small transcript bundles into the seed/report
   path and grade whether latch identifies workstreams, decisions, rejected
   paths, next steps, and high-confidence agent mistakes with evidence.

Run the deterministic seed-report evals:

```bash
bash bin/latch_seed_report_eval.sh
```

This runner creates throwaway Claude/Codex transcript bundles, runs the
seed/report path locally, and grades the structured report for
workstream/where-left-off handoff, next-step follow-up, decisions/rejected
paths, preferences, and high-confidence agent-mistake reporting. It makes no
model calls; the agent-mistake check uses a synthetic LLM-shaped candidate from
fixture evidence so the public seed CLI remains LLM-backed while the eval stays
deterministic and cheap.

Schema, traversal, and retrieval refactors should be justified by failures in
these suites. If a decision exists but is not captured, fix capture/schema. If it
is captured but not retrieved, fix search/traversal/ranking. If it is retrieved
but the verdict is wrong, fix evidence packaging or classifier prompts. If latch
invents a reason, tighten faithfulness and citation discipline.
