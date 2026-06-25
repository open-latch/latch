---
description: Manual gate (LLM go/no-go judgement) on a coding/build request
argument-hint: "<request — the prompt you'd otherwise act on directly>"
---

Manually invoke the per-project gate (heavyweight LLM go/no-go judgement
layer) for the request in `$ARGUMENTS`. The MCP `kb_gate` tool is the
autonomous path; this slash is the manual escape hatch when you want an
explicit second opinion without relying on auto-invocation.

The classifier returns one of {PROCEED | MODIFY | DO_NOT_PROCEED |
NEEDS_HUMAN_JUDGMENT} with cited node ids, a recommended better-next-action,
and a side-note summary. It does NOT auto-redirect — the user/agent decides.

Renamed from `/kb-preflight` 2026-05-19 as part of the two-tier validation
model (gate = heavyweight LLM judgement; a lightweight per-fact deterministic
`kb_verify` tier is planned to sit alongside it).

Budget-gated (counts against the daily LLM cap shared with `/kb-compact`
and nightly heal). Skip and report the budget message if the cap is hit.

## Run

```bash
bash <KB_HOME>/bin/run_kb_gate.sh "$ARGUMENTS"
```

After the command returns, show an explicit **Latch gate** block. Prefer the
returned `findings` object; it is already shaped for chat display and should be
shown even when the recommendation is `PROCEED`. Lead with provenance: Latch ran
`kb_gate` on the request/plan. Then show the verdict, summary/rationale,
`receipt` / source basis when present, cited evidence nodes with status/current
authority, `better_next_action`, and uncovered claims. Don't re-render the full
chains object — point the user at the cited ids and offer to `kb_get` any of
them for full bodies.
