"""Structural-only RL log streams for the gate + decision-capture pipeline.

Daily JSONL streams follow the locked logging conventions
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

Structural-only is enforced *by construction*: emit helpers use explicit
parameters and rebuild nested rows from allowlisted fields — there is no
``**kwargs`` passthrough through which body text could leak.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from typing import Sequence

import log_utils
import paths


ADVERSARY_STREAM = "adversary"
DECISION_STREAM = "decision"
OUTCOME_STREAM = "outcome_event"
OUTCOME_EVENTS_VERSION = "1"
OUTCOME_ROW_KINDS = (
    "gate_verdict",
    "capture_action",
    "decision_capture_link",
)
OUTCOME_NODE_SOURCES = ("priority", "lane", "hybrid", "graph", "focus")
# One row per mission-control assistant turn scanned by the Stop-hook cite
# detector (Slice 3-B, KB id=1436). Structural-only: counts + a closed-set
# action + a transcript join hash, never the claim text. Feeds the precision
# measurement the advisory-posture decision deferred to data (id=1395 / id=1197).
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
# Which adversary discipline produced the row — the profile-selected mode
# (KB id=1420 / id=1428). Tagged on every adversary.log row so the
# counter-node-vs-assumption-hunter comparison has differential data from day
# one (the open question of which mode wins is decided by measurement later).
ADVERSARY_MODES = ("counter_node", "assumption_hunter")
# What the Stop-hook cite detector did this turn: "none" = scanned, nothing
# flagged; "nudge_queued" = an uncited code-class claim was found and the
# advisory next-turn nudge was queued for the UserPromptSubmit hook.
DETECTION_ACTIONS = ("none", "nudge_queued")

_EMPTY_QUERY_HASH = hashlib.sha1(b"").hexdigest()[:12]
_OUTCOME_SETTINGS_CACHE: dict[str, tuple[int, int, int, int, bool]] = {}
_OUTCOME_ROLES = (
    "decision_chain",
    "abandoned_path",
    "active_constraint",
    "current_direction",
)
_OUTCOME_BACKENDS = ("claude", "codex", "cursor")
_OUTCOME_NODE_KINDS = (
    "fact",
    "decision",
    "progress",
    "entity",
    "preference",
    "open_question",
    "idea",
    "workstream",
    "summary",
    "priority",
    "profile",
)
_OUTCOME_NODE_STATUSES = ("staging", "canonical", "stale")
_OUTCOME_AUTHORITY_TIERS = (
    "foundational",
    "governing",
    "lane-local",
    "decision-evidence",
)
_OUTCOME_RELATIONS = (
    "supersedes",
    "replaces",
    "constrains",
    "motivates",
    "tested_against",
    "depends_on",
    "related_to",
    "reconciled_by",
    "merged_into",
)


def new_gate_call_id() -> str:
    """Return a content-free nonce for exact gate/outcome joins."""
    return uuid.uuid4().hex[:12]


def outcome_events_enabled(project_path: str | None = None) -> bool:
    """Return the call-time local outcome-recording policy.

    Recording is on for a clean install. Any explicitly set environment value
    other than ``"1"`` disables it for that process. The vault-local
    ``runtime_settings.json`` key is the durable daemon-safe control. Invalid
    configured policy fails closed and never emits output.
    """
    raw_env = os.environ.get("LATCH_OUTCOME_EVENTS")
    if raw_env is not None:
        return raw_env.strip() == "1"
    try:
        settings_path = (
            paths.project_dir(project_path) / paths.VAULT_RUNTIME_SETTINGS_FILENAME
        )
        if settings_path.is_symlink():
            return False
        try:
            info = settings_path.stat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if not stat.S_ISREG(info.st_mode):
            return False
        cache_key = os.path.abspath(os.fspath(settings_path))
        signature = (
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_ino,
            info.st_size,
        )
        cached = _OUTCOME_SETTINGS_CACHE.get(cache_key)
        if cached is not None and cached[:4] == signature:
            return cached[4]
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        enabled = (
            isinstance(data, dict)
            and data.get("outcome_events", True) is True
        )
        _OUTCOME_SETTINGS_CACHE[cache_key] = (*signature, enabled)
        return enabled
    except Exception:
        return False


def _normalized_hex12(value: str | None) -> str | None:
    """Keep only a content-free 12-character lowercase hex key."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if len(normalized) != 12 or any(c not in "0123456789abcdef" for c in normalized):
        return None
    return normalized


def _normalized_query_hash(query_hash: str | None) -> str | None:
    """Keep a real sha1[:12] join key while dropping the empty-input sentinel."""
    normalized = _normalized_hex12(query_hash)
    return None if normalized == _EMPTY_QUERY_HASH else normalized


def _closed_label(value, allowed: tuple[str, ...]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _optional_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _emit_outcome_row(
    row: dict,
    *,
    project_path: str | None,
    session_id: str | None,
) -> None:
    log_utils.emit_event(
        OUTCOME_STREAM,
        {"events_version": OUTCOME_EVENTS_VERSION, **row},
        project_path=project_path,
        session_id=session_id,
    )


def emit_gate_outcome_event(
    *,
    gate_call_id: str,
    query_hash: str,
    verdict: str | None,
    skipped: bool,
    timed_out: bool,
    error_present: bool,
    backend: str | None,
    adversary: dict | None,
    assembled_nodes: Sequence[dict],
    cited_nodes: Sequence[dict],
    uncovered_claim_count: int,
    project_path: str | None = None,
    session_id: str | None = None,
) -> None:
    """Emit one allowlisted, structural ``gate_verdict`` outcome row."""
    try:
        if not outcome_events_enabled(project_path):
            return
        normalized_gate_call_id = _normalized_hex12(gate_call_id)
        if normalized_gate_call_id is None:
            return
        assembled = [
            {
                "node_id": int(node["node_id"]),
                "source": node["source"],
                "position": int(node["position"]),
                "cited": bool(node["cited"]),
            }
            for node in assembled_nodes
            if node.get("source") in OUTCOME_NODE_SOURCES
        ]
        cited = [
            {
                "node_id": int(node["node_id"]),
                "kind": _closed_label(
                    node.get("kind"), _OUTCOME_NODE_KINDS,
                ),
                "status_at_citation": _closed_label(
                    node.get("status_at_citation"), _OUTCOME_NODE_STATUSES,
                ),
                "workstream_id_at_event": _optional_int(
                    node.get("workstream_id_at_event")
                ),
                "authority_tier_at_citation": _closed_label(
                    node.get("authority_tier_at_citation"),
                    _OUTCOME_AUTHORITY_TIERS,
                ),
                "via_relation": _closed_label(
                    node.get("via_relation"), _OUTCOME_RELATIONS,
                ),
                "roles": [
                    role for role in (node.get("roles") or [])
                    if role in _OUTCOME_ROLES
                ],
                "classifier_load_bearing": bool(
                    node.get("classifier_load_bearing")
                ),
            }
            for node in cited_nodes
        ]
        adversary_row = None
        if isinstance(adversary, dict):
            adversary_row = {
                "verdict_delta": (
                    _closed_label(adversary.get("verdict_delta"), VERDICT_DELTAS)
                ),
                "counter_node_id": _optional_int(
                    adversary.get("counter_node_id")
                ),
                "n_forks": int(adversary.get("n_forks") or 0),
            }
        _emit_outcome_row(
            {
                "row_kind": "gate_verdict",
                "gate_call_id": normalized_gate_call_id,
                "query_hash": _normalized_query_hash(query_hash),
                "verdict": verdict if verdict in VERDICT_LABELS else None,
                "skipped": bool(skipped),
                "timed_out": bool(timed_out),
                "error_present": bool(error_present),
                "backend": _closed_label(backend, _OUTCOME_BACKENDS),
                "adversary": adversary_row,
                "assembled_nodes": assembled,
                "cited_nodes": cited,
                "uncovered_claim_count": int(uncovered_claim_count),
            },
            project_path=project_path,
            session_id=session_id,
        )
    except Exception:
        pass


def _emit_capture_outcomes(
    *,
    node_ids: Sequence[int],
    confidence_tier: str,
    provenance: str,
    was_confirmed: bool,
    human_action: str | None,
    query_hash: str | None,
    project_path: str | None,
    session_id: str | None,
) -> None:
    if not outcome_events_enabled(project_path):
        return
    normalized_ids = [int(node_id) for node_id in node_ids]
    normalized_hash = _normalized_query_hash(query_hash)
    if human_action in HUMAN_ACTIONS:
        _emit_outcome_row(
            {
                "row_kind": "capture_action",
                "query_hash": normalized_hash,
                "human_action": human_action,
                "confidence_tier": (
                    confidence_tier
                    if confidence_tier in CONFIDENCE_TIERS
                    else None
                ),
                "provenance": (
                    provenance if provenance in DECISION_PROVENANCES else None
                ),
                "was_confirmed": bool(was_confirmed),
                "decision_node_ids": normalized_ids,
            },
            project_path=project_path,
            session_id=session_id,
        )
    if normalized_ids:
        _emit_outcome_row(
            {
                "row_kind": "decision_capture_link",
                "decision_node_ids": normalized_ids,
                "query_hash": normalized_hash,
                "provenance": (
                    provenance if provenance in DECISION_PROVENANCES else None
                ),
            },
            project_path=project_path,
            session_id=session_id,
        )


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

    Structural-only; never raises. Only the Stop hook calls this, and only for
    a mission-control-bound actor — every scanned turn emits a row (including
    the all-clear ``n_flagged=0`` case) so both numerator and denominator are on
    record for the precision measurement (id=1197 pattern).

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
    try:
        _emit_capture_outcomes(
            node_ids=node_ids,
            confidence_tier=confidence_tier,
            provenance=provenance,
            was_confirmed=was_confirmed,
            human_action=human_action,
            query_hash=query_hash,
            project_path=project_path,
            session_id=session_id,
        )
    except Exception:
        pass
