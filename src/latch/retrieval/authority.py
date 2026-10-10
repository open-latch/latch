"""Shared decision-authority labels for workstream-centered reads.

Authority is deliberately graded, not a visibility boundary.  Callers pass
the lane whose context is being rendered; the returned tier is presentation
and weighting metadata only.  It must never be used to discard evidence.
"""
from __future__ import annotations

from typing import Any

from latch.store import db


FOUNDATIONAL = "foundational"
GOVERNING = "governing"
LANE_LOCAL = "lane-local"
DECISION_EVIDENCE = "decision-evidence"

AUTHORITY_RELATIONS: frozenset[str] = frozenset({
    "constrains",
    "depends_on",
    "motivates",
    "replaces",
    "supersedes",
    "reconciled_by",
})

# An unscoped decision connected through one of these relations expresses a
# project-wide premise rather than merely governing one lane.
FOUNDATIONAL_RELATIONS: frozenset[str] = frozenset({
    "constrains",
    "depends_on",
    "motivates",
})

_TIER_ORDER = {
    FOUNDATIONAL: 0,
    GOVERNING: 1,
    LANE_LOCAL: 2,
    DECISION_EVIDENCE: 3,
}


def decision_authority_tier(
    *,
    relation: str | None,
    decision_workstream_id: Any,
    owning_workstream_id: Any,
) -> str:
    """Return a decision's authority tier in one owning-lane context.

    ``lane-local`` is relative to the lane being rendered.  A scoped decision
    reached while rendering another lane remains visible as decision evidence;
    an authority-bearing edge can still make it governing there.  Foundational
    decisions are lane-independent by construction (unscoped + a project-wide
    authority relation).
    """
    canonical = db.canonicalize_relation(str(relation or "related_to"))
    decision_lane = _int_or_none(decision_workstream_id)
    owning_lane = _int_or_none(owning_workstream_id)

    if canonical in FOUNDATIONAL_RELATIONS and decision_lane is None:
        return FOUNDATIONAL
    if canonical in AUTHORITY_RELATIONS:
        return GOVERNING
    if owning_lane is not None and decision_lane == owning_lane:
        return LANE_LOCAL
    return DECISION_EVIDENCE


def authority_sort_key(tier: str | None) -> tuple[int, str]:
    """Stable strongest-first ordering for report presentation."""
    value = str(tier or DECISION_EVIDENCE)
    return (_TIER_ORDER.get(value, len(_TIER_ORDER)), value)


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
