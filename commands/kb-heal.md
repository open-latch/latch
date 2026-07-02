---
description: Nightly heal — integrity sweep + three-pass contradiction resolver
---

Run the nightly heal against the per-project knowledge base under
`<KB_HOME>`. Two phases:

1. **Integrity** — clean up orphan edges and sync the `vec_nodes` virtual
   table with the main `nodes` table (delete orphans, backfill missing).
2. **Contradiction sweep** — for every non-stale node with an embedding,
   find near-duplicates at cosine similarity ≥ 0.70 (the range the on-insert
   heal deferred). For each pair:
   - Skip if an edge already exists between them (idempotent — prior sweeps'
     decisions stick).
   - Apply **three-pass arbitration**:
     - Pass A (recency): newer wins if age diff > 30d AND newer is still
       fresh (updated in last 30d).
     - Pass B (ref_count): the dominant side wins if ratio ≥ 3 AND both
       have been referenced at least once.
     - Pass C (LLM): only invoked when A and B are inconclusive. Uses the
       selected maintenance model backend (Claude by default; adapter env can
       select Codex) and counts against the daily budget.
   - Verdict: **supersede** marks loser stale + adds a `supersedes` edge.
     **keep_both** adds a `related_to` edge so this pair is skipped on
     future sweeps.

Run with the Bash tool:

```bash
python "<KB_HOME>/src/maintenance.py" nightly "$(pwd)"
```

Report the JSON summary: `examined`, `collisions`, `superseded`, `kept_both`,
per-path counts (`recency` / `ref_count` / `llm`), and `budget_blocked` if any
collisions fell back to keep_both because the daily cap was hit.

If anything fails, check `<KB_HOME>/maintenance.log`.
