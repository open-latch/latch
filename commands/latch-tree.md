---
description: Rebuild the hierarchical cluster/summary tree for this project's KB
---

Full rebuild of the RAPTOR-style tree for the per-project knowledge base
under `<KB_HOME>`. One pass:

1. **Retire prior summaries** — all existing `depth > 0` nodes are marked
   `stale` (audit trail preserved, excluded from retrieval). Their children's
   `parent_id` is cleared.
2. **Landmarks** — leaf nodes with `ref_count >= 5` are preserved as-is:
   they stay at `depth=0` with `parent_id=NULL`. Frequently-consulted
   knowledge sits alongside summaries rather than being absorbed.
3. **Cluster the rest** — bounded **average-linkage** (UPGMA) agglomerative
   clustering on cosine similarity (default threshold `0.65`, max cluster size
   40). Average-linkage merges only on *mean* pairwise similarity, so a single
   bridge edge can't chain unrelated topics into one mega-cluster (the
   single-link percolation collapse; KB id=1529/id=1560). Single-link remains
   available as a non-default `linkage="single"` fallback. Clusters with >= 3
   members earn one LLM-generated summary node (`kind='summary'`,
   `status='canonical'`, `depth=1`). Members' `parent_id` is set to the summary.
4. **Singletons** and **sub-minimum clusters** stay at `depth=0` with
   `parent_id=NULL`.

Each summary LLM call uses the selected maintenance model backend (Claude by
default; adapter env can select Codex) and counts against the daily budget. If
the cap is hit mid-run, the remaining clusters are left without summaries this
cycle — report that via `budget_blocked`.

Run with the Bash tool:

```bash
python "<KB_HOME>/src/maintenance.py" tree "$(pwd)"
```

Report the JSON summary: `linkage`, `leaves`, `landmarks`, `clusters`,
`largest_cluster`, `p95_cluster_size`, `summaries_generated`, `singletons`,
`oversized_skipped`, `budget_blocked`, `llm_failed`, `prior_summaries_staled`.

Retrieval is unchanged — summary nodes live in the same FTS+vector pool as
leaves (collapsed-tree search). If anything fails, check
`<KB_HOME>/maintenance.log` and `tree.log`.
