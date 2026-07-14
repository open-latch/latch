# Agent contract trim verification brief

This pass treats the managed markdown contract as soft bootstrap. It prepares
repeatable evidence for a later live-host and long-running quality evaluation;
it does not claim that rendering tests prove agent compliance.

## Questions for the deep pass

1. Does the trimmed contract improve or preserve required behavior in Claude
   Code, Codex, and Cursor across more than one supported model version?
2. Do agents fetch full nodes, follow every reconciliation banner, and use
   current authority rather than relying on search teasers?
3. Does a usable gate receipt appear before mutation, including for natural
   prompts that do not explicitly mention Latch?
4. Do non-PROCEED verdicts actually stop edits until the user resolves them?
5. Are uncovered claims resolved through graph traversal, code evidence, or an
   explicit assumption rather than being silently filled?
6. Are durable decisions, rejected paths, overrides, and refusals captured
   accurately without increasing low-value rows?
7. Do relation and authority mechanics remain healthy over time even when the
   markdown contract is ignored?

## Candidate leading indicators

- Full-node fetch rate after a teaser-bearing search result.
- Reconciliation-banner follow rate before the next action.
- Verbatim gate-receipt rate before the first mutation-capable tool call.
- Non-PROCEED stop rate and explicit user-resolution rate.
- Uncovered-claim remedy completion rate.
- Human-facing activity-receipt compliance by host.
- Confirmed decision/rejected-path capture precision and noise rate.
- Structural violations: canonical losers behind `supersedes`, reversed
  `reconciled_by`, stale prerequisites, and stale targets still used as current.
- Change-flow mix for supersede, reconcile, keep-both, and in-place claim edits.
- High-similarity canonical pairs without a typed authority relation.

Relation-use counts are advisory until labeled examples establish useful
thresholds. A low `reconciled_by` count is not automatically a defect.

## Baselines to retain

- Exact rendered line, word, byte, and digest measurements for the current
  public-main contract and the trimmed candidate.
- Always-loaded footprint by host, with Cursor measured as shared `AGENTS.md`
  plus its rule rather than as AGENTS alone.
- Focused deterministic authority-case results.
- One-time older-wiring repair, unmanaged preservation, newer-wiring refusal,
  and rollback evidence.
- Quick lifecycle smoke receipts for all three hosts, separated from live model
  behavior.

## Missing evidence after this pass

- A labeled set of moments when a new decision should have created
  `reconciled_by` rather than superseding or keeping both.
- A multi-model, repeated live-host run with consistent transcript and tool-call
  capture across Claude Code, Codex, and Cursor.
- A stable user usefulness measure that can detect gradual loss of decision
  continuity before users describe the KB as less helpful.
- Calibrated alert thresholds and age-gated reporting for relation underuse.
- A causal comparison separating contract wording effects from hook, gate,
  retrieval, model, and project-data changes.

## Recommended next objective

Build and run a deep three-host Latch behavior and authority-health evaluation
from the committed obligation matrix, scenario corpus, baseline footprints, and
smoke receipts. Execute repeated Claude Code, Codex, and Cursor trials; capture
tool sequences and human-facing receipts; score every shared obligation; add
deterministic reconciliation-health reporting with advisory thresholds; compare
the trimmed contract with the retained baseline; and recommend keep, revise, or
rollback using reproduced evidence rather than subjective impressions.
