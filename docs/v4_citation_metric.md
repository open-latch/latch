# V4 citation metric — operationalized definition

Phase V item V4 (decision 3948) verbatim:

> V4 firing rate over next 200 gate calls: % citing ≥1 typed rejection that
> changed the verdict (pre-registered rubric: recommendation or
> better_next_action differs). PASS ≥5%. RETHINK <5%: steering-first ladder;
> catch demoted to credibility demo.

This document is the pre-registered, mechanically-evaluable reading of that
sentence, adopted under the V4 build handoff (KB id=4626 item 1) as the
NARROWEST reading the gate's persisted logging supports. It is implemented,
byte-for-byte in field names, by `src/v4_citation_rate.py` and evaluated over
gate.log rows only — no prose judgment, no transcript reads, no second model
call.

## The metric in one measurable sentence

**V4 = 100 × |{eligible gate.log rows where `cited_rejected_paths` is
non-empty AND `recommendation` ∈ {"MODIFY", "DO_NOT_PROCEED"}}| ÷ |eligible
rows|, where a row is eligible iff `skipped` == false AND `error` == null AND
`recommendation` is one of the four classifier labels AND the key
`surfaced_rejected_paths` is present in the row; PASS iff V4 ≥ 5.0 over the
first ~200 eligible rows of the live measurement window.**

## Exact field definitions

- **"cites a typed rejection"** := the row's `cited_rejected_paths` list is
  non-empty. `cited_rejected_paths` is a list of int `rejected_path.id`
  values (the V2 table's primary key, KB id=4369) that the classifier named
  in its verdict, clamped at parse time to the subset actually surfaced in
  that call's prompt (`surfaced_rejected_paths`), so a hallucinated id can
  never count.
- **"changed the verdict"** := on that same row, `recommendation` ∈
  {"MODIFY", "DO_NOT_PROCEED"}.
- **eligible row** := `skipped` == false, `error` == null, `recommendation` ∈
  {"PROCEED", "MODIFY", "DO_NOT_PROCEED", "NEEDS_HUMAN_JUDGMENT"}, and
  `surfaced_rejected_paths` present (the key marks a runtime with citation
  capability; rows written by older runtimes structurally cannot cite and
  would deflate the denominator).

## Declared deviations from the literal 3948 rubric (for founder ratification)

1. The literal rubric — "recommendation or better_next_action differs" — is a
   counterfactual comparison. Evaluating it literally requires either
   persisting verdict prose or running a paired no-rejections classifier call
   per gate invocation. **better_next_action is never persisted** (canonical
   id=3915, ratified reading id=3985: gate.log verdict fields are int-id
   lists and enums only), and a paired counterfactual call changes gate cost
   and behavior — out of 4626 scope. The narrowest persisted proxy is the
   `recommendation` enum: a non-PROCEED label while citing a typed rejection
   is a verdict that differs from the default go-ahead with the rejection on
   the table.
2. **"NEEDS_HUMAN_JUDGMENT" is excluded from the changed-verdict numerator.**
   It is a routing outcome, not a changed verdict. The counter reports the
   cited-NEEDS_HUMAN_JUDGMENT count separately so the exclusion is visible.
3. Both narrowings UNDER-count the numerator: a PROCEED whose
   better_next_action was rejection-informed does not count, and a cited
   NEEDS_HUMAN_JUDGMENT does not count. The ≥5% PASS bar is therefore
   evaluated against a strictly conservative numerator (the denominator is
   the same eligible-row set under either reading).
4. **The window denominator is re-scoped from 3948's "next 200 gate calls"
   to the first ~200 eligible rows** (surfaced round 1 of the cross-vendor
   review, 2026-08-08). Skipped and errored calls produce no verdict to
   change, and rows written by pre-capability runtimes structurally cannot
   cite; counting either would make the rate measure runtime mix and outage
   luck rather than citation behavior. The excluded-row accounting
   (`capability_missing` / `skipped` / `errored` / `invalid_recommendation` /
   `unparsable` / `non_gate`) is reported by the counter precisely so this
   re-scope stays auditable — the founder can recompute the literal
   all-calls denominator from the same output if they re-pin.

## What the counter reports (secondary observables, not the metric)

- `citing_rate`: share of eligible rows with any non-empty
  `cited_rejected_paths`, including PROCEED rows.
- `cited_needs_human`: cited rows labeled "NEEDS_HUMAN_JUDGMENT".
- `capability_missing`, `skipped`, `errored`, `unparsable`: excluded-row
  accounting, so the denominator is auditable.

## Window

The live ~200-call window starts only after this branch is merged and the
runtime installed (same install-gap as V1's T0, KB id=4626). Rows produced
before install lack `surfaced_rejected_paths` and are excluded by
construction.
