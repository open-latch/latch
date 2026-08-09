# V2 — "genuine rejection" rubric (pre-registered)

Phase V item **V2** (decision 3948) introduces a typed `rejected` status with fields
`{option, reason, ratifier, date, scope predicate}` and backfills existing decisions.
Its PASS bar is **≥20 genuine rejections**; below 20 fires the RETHINK branch
(wedge is steering-only; claim ladder repositions per 3938).

3948's operative definition #1 requires that where judgment is unavoidable, the rubric
is **written and the threshold fixed before measurement**. V1's `AMBIGUOUS ≤20%` bar was
set without one (open question 4015); this file exists so V2 does not repeat that.

Fixed before any count was produced. Threshold: **20**, from 3948, unchanged.

## Denominator

All nodes of `kind='decision'`, any status, in the project vault at measurement time:
**506**. Stale decisions are included — a rejection that was later superseded was still
a genuine rejection when made. The denominator is recorded with the count; no
sampling, no top-N.

## A node qualifies as a genuine rejection when all four hold

1. **Identifiable rejected option.** The body names a specific alternative course of
   action that was available and was not taken. "We chose X" alone does not qualify;
   the *not-taken* option must be recoverable as a noun phrase.
2. **Stated reason.** The body gives why the option was rejected. A bare "rejected Y"
   with no rationale does not qualify — the `reason` field would be empty, and an
   unexplained rejection cannot support a revival check.
3. **Project-scoped.** The rejected option is a choice about this project's code,
   architecture, product, process, or positioning. Rejections of a *claim about the
   world* (e.g. "the hypothesis was not supported") are facts, not rejected paths.
4. **Authority is recoverable.** The body attributes the rejection to the founder, a
   ratified decision, or a recorded review verdict — enough to populate `ratifier`.
   Agent-only speculation with no human ruling does not qualify.

## Disqualifiers (explicit, because the current detector trips on all of them)

- **D1 — Self-reference.** The node mentions Latch's own rejection machinery
  (`typed rejected`, `rejected-path catch`, `ratified/rejected`, plan text
  describing V2) rather than recording a rejection. Sampling found this to be the
  dominant false-positive class: 3948, 3950, 3952, 3938, 3939, 3931, 3926 all match
  the current substring detector on self-reference alone.
- **D2 — Vocabulary-only match.** The word appears in unrelated grammar —
  "founder ruled", "ruling", "ruled discharged" — with no option being rejected.
- **D3 — Superseded-by, not rejected.** A decision replaced by a later one is
  `stale`/`supersedes`, already modelled. `rejected` is for options never adopted.
- **D4 — Deferral.** "Not now" is a scheduling choice, not a rejection. A deferred
  option may be revived legitimately; blocking it would be a wrong-block.
- **D5 — Duplicate.** The same rejection restated across several nodes counts once,
  attributed to the earliest node that records it with a reason.

## What is being measured, and what is not

This rubric establishes the **backfill population** and the ≥20 count. It deliberately
does not measure whether typed rejections change gate verdicts in the field — that is
**V4** (≥5% firing rate over 200 gate calls) and cannot be measured until the type
exists and the backfill has landed.

## Recall note

The current detector (`src/gate_report.py:448`) matches
`"rejected" | "discarded" | "ruled"` in the body. This rubric is **not** scoped to
that keyword set: a rejection phrased "we are not going to do X because Y" is genuine
under criteria 1–4 and invisible to `:448`. Candidate generation must therefore widen
beyond those three words, and the recall gap between the two is reported, not hidden.
