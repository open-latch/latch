"""Structural-only RL log streams for the adversarial-gate + decision-capture
pipeline (scope: KB id=1343; builds id=1303 self-improvement loop / id=1279
decision-capture stream).

Two daily JSONL streams, both following the locked logging conventions
(id=1108 / id=1091): one file per concern, common header prepended by
``log_utils.emit_event``, the structural-only invariant (NO node titles,
bodies, raw prompt text, objection text, or fork-question text — ids,
closed-set labels, counts, hashes, and booleans only), 30d-hot / 1y-warm
retention via ``log_utils.maintain_log_retention`` (the ``[a-z_]+`` daily-file
regex already matches both stream names — no registration needed), and
post-hoc correlation.

* ``adversary.log`` — one row per adversary call (Feature 1). Captures the
  point-in-time state AT the call: the pre-adversary verdict, the delta the
  adversary would apply, the cited counter-node (or ``None`` under the
  cite-or-PROCEED guard), the number of design-decision forks raised, backend,
  latency, and tokens. Confirmation of those forks is deliberately NOT here — it
  lands per-decision in ``decision.log`` and is joined post-hoc by the correlator.
  (Point-in-time invariant, id=1108: ``n_forks_confirmed`` is a correlator-
  derived field, not an emit-time one.)

* ``decision.log`` — one row per captured decision signal (Feature 2 / id=1279).
  The KB ``kind="decision"`` node id(s) materialized (empty when a Type-2
  inferred signal is logged WITHOUT materializing a node — the no-auto-mutate
  line, id=1338), the confidence tier, the provenance, whether the user
  confirmed, and a join hash. ``was_later_corrected`` is NOT a field here — it
  is correlator-derived from the existing correction/supersede streams (mirrors
  ``cited_ids_corrected``).

Structural-only is enforced *by construction*: the emit helpers accept only
typed scalar / id / list-of-id params — there is no ``**kwargs`` passthrough
through which body text could leak. ``tests/test_capture_streams.py`` is the
regression that pins this.
"""
from __future__ import annotations

from typing import Sequence

import log_utils


ADVERSARY_STREAM = "adversary"
DECISION_STREAM = "decision"
# Reserved structural stream for deterministic detection experiments. It stays
# structural-only: counts + a closed-set action + a transcript join hash, never
# prompt or answer text.
DETECTION_STREAM = "detection"

# Closed-set discriminators. Kept LOCAL (not imported from gate.py) to avoid
# pulling gate's heavy scientific-stack imports into lightweight call paths —
# see the scipy daemon-thread loader-race postmortem (id=Phase-1 deadlock fix).
# VERDICT_LABELS must stay in sync with ``gate.CLASSIFIER_LABELS``.
VERDICT_LABELS = ("PROCEED", "MODIFY", "DO_NOT_PROCEED", "NEEDS_HUMAN_JUDGMENT")
VERDICT_DELTAS = ("none", "MODIFY", "DO_NOT_PROCEED")
CONFIDENCE_TIERS = ("explicit_user", "agent_confirmed", "agent_inferred")
DECISION_PROVENANCES = ("adversary_fork", "gate_question", "inline_capture")
# What the human DID with the gate's verdict — the gold-label decision signal
# (id=1279 / id=1784). approve = took the plan as-is; modify = constrained it;
# reject = killed it; override = ratified judgment DIVERGING from the machine
# verdict (proceeded against a MODIFY/DO_NOT_PROCEED, or rejected a PROCEED).
# "override" is the highest-signal row. Structural-only: a closed-set label.
HUMAN_ACTIONS = ("approve", "modify", "reject", "override")
# Which adversary discipline produced the row. This snapshot keeps the default
# counter-node reviewer only.
ADVERSARY_MODES = ("counter_node",)
# What a deterministic detector did this turn: "none" = scanned, nothing
# flagged; "nudge_queued" = a follow-up nudge was queued.
DETECTION_ACTIONS = ("none", "nudge_queued")


def emit_adversary_event(
    *,
    verdict_before: str,
    verdict_delta: str,
    counter_node_id: int | None,
    n_forks_raised: int,
    latency_ms: int,
    query_hash: str | None = None,
    tokens: int | None = None,
    mode: str = "counter_node",
    backend: str | None = None,
    project_path: str | None = None,
    session_id: str | None = None,
) -> None:
    """Emit one ``adversary.log`` row (point-in-time, at the adversary call).

    Structural-only; never raises (delegates to ``log_utils.emit_event``, which
    swallows write failures so logging can't break the gate).

    Args:
        verdict_before: the classifier's recommendation before the adversary
            ran (a member of ``VERDICT_LABELS``).
        verdict_delta: what the adversary would flip the verdict to — a member
            of ``VERDICT_DELTAS`` (``"none"`` = no flip, verdict stands).
        counter_node_id: the cited node that refutes/re-scopes the plan, or
            ``None`` under the cite-or-PROCEED guard (no citable counter found).
        n_forks_raised: count of design-decision forks the adversary surfaced.
        latency_ms: wall-clock of the adversary call.
        query_hash: the gate.log ``query_hash`` (sha1[:12]) to join back to the
            originating prompt/verdict. A hash, never raw text.
        tokens: LLM tokens spent on the adversary call, if known.
        backend: model backend that produced the adversary result, if known.
    """
    log_utils.emit_event(
        ADVERSARY_STREAM,
        {
            "verdict_before": verdict_before,
            "verdict_delta": verdict_delta,
            "counter_node_id": counter_node_id,
            "n_forks_raised": int(n_forks_raised),
            "latency_ms": int(latency_ms),
            "query_hash": query_hash,
            "tokens": tokens,
            "mode": mode,
            "backend": backend,
        },
        project_path=project_path,
        session_id=session_id,
    )


def emit_detection_event(
    *,
    n_claims: int,
    n_flagged: int,
    action: str,
    scanned: bool = True,
    transcript_hash: str | None = None,
    project_path: str | None = None,
    session_id: str | None = None,
) -> None:
    """Emit one ``detection.log`` row (point-in-time, at the Stop-hook scan).

    Structural-only; never raises. Each scanned turn emits a row (including the
    all-clear ``n_flagged=0`` case) so both numerator and denominator are on
    record for later precision checks.

    Args:
        n_claims: windows containing a current-value/code/config claim.
        n_flagged: of those, how many lacked an in-window ``file:line`` cite.
        action: a member of ``DETECTION_ACTIONS`` — what the detector did.
        scanned: False only if the scan was skipped (e.g. empty transcript).
        transcript_hash: sha1[:12] of the scanned assistant text — a join key
            back to the turn, never the text itself.
    """
    log_utils.emit_event(
        DETECTION_STREAM,
        {
            "scanned": bool(scanned),
            "n_claims": int(n_claims),
            "n_flagged": int(n_flagged),
            "action": action,
            "transcript_hash": transcript_hash,
        },
        project_path=project_path,
        session_id=session_id,
    )


def emit_decision_event(
    *,
    node_ids: Sequence[int],
    confidence_tier: str,
    provenance: str,
    was_confirmed: bool,
    human_action: str | None = None,
    query_hash: str | None = None,
    project_path: str | None = None,
    session_id: str | None = None,
) -> None:
    """Emit one ``decision.log`` row (point-in-time, at capture).

    Structural-only; never raises.

    Args:
        node_ids: the materialized ``kind="decision"`` KB node id(s). Empty for
            a Type-2 inferred signal logged without a graph write (id=1338
            no-auto-mutate: detection auto, materialization deferred/confirmed).
        confidence_tier: a member of ``CONFIDENCE_TIERS``.
        provenance: a member of ``DECISION_PROVENANCES`` — the trigger that
            surfaced the decision.
        was_confirmed: whether the user confirmed the decision (True for the
            explicit/confirmed Type-1 slice; False for inferred-not-confirmed).
        human_action: a member of ``HUMAN_ACTIONS`` (approve | modify | reject |
            override) — what the user did with the gate verdict. The gold RL
            label; None for a Type-2 inferred signal with no human action.
        query_hash: optional join hash to the originating prompt.
    """
    log_utils.emit_event(
        DECISION_STREAM,
        {
            "node_ids": [int(n) for n in node_ids],
            "confidence_tier": confidence_tier,
            "provenance": provenance,
            "was_confirmed": bool(was_confirmed),
            "human_action": human_action,
            "query_hash": query_hash,
        },
        project_path=project_path,
        session_id=session_id,
    )
