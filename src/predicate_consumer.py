"""Host-neutral library seam for private A2 policy snapshot evaluation.

``PolicyEvaluation.verdict`` is the exact, private ``predicate-v1`` core.
``PolicyEvaluation.receipt`` is the only log/stdout-safe representation: it
contains structural ids, counts, digests, and aggregate reason codes, never
policy text, action text, or filesystem paths.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import json
from pathlib import Path
import re
import sys
from typing import Mapping, Sequence

import predicate
import predicate_snapshot


RECEIPT_CONTRACT = "predicate-policy-receipt-v1"
_SAFE_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*(?::[a-z][a-z0-9_]*)?$")


@dataclass(frozen=True)
class PolicyEvaluation:
    verdict: Mapping[str, object]
    receipt: Mapping[str, object]


def evaluate_policy(
    snapshot_path: str | Path,
    action: Mapping[str, object] | object,
) -> PolicyEvaluation:
    """Load fresh policy state and evaluate one canonical action envelope."""
    if not isinstance(action, Mapping):
        return failure_evaluation(None, ("malformed_action",))
    domain = action.get("policy_domain_id")
    if not _nonempty_text(domain):
        return failure_evaluation(None, ("policy_domain_missing",))
    assert isinstance(domain, str)
    expected_token = action.get("expected_freshness_token")
    if expected_token is not None and not _nonempty_text(expected_token):
        return failure_evaluation(domain, ("malformed_freshness_token",))

    loaded = predicate_snapshot.load_policy_snapshot(
        snapshot_path,
        policy_domain_id=domain,
        expected_freshness_token=(
            str(expected_token) if expected_token is not None else None
        ),
    )
    if loaded.snapshot is None:
        return failure_evaluation(domain, loaded.reason_codes)
    return evaluate_loaded_policy(loaded.snapshot, action)


def evaluate_loaded_policy(
    snapshot: predicate_snapshot.LoadedPolicySnapshot,
    action: Mapping[str, object] | object,
) -> PolicyEvaluation:
    """Evaluate a preloaded snapshot, rechecking its source on every call."""
    if not isinstance(action, Mapping):
        return failure_evaluation(
            snapshot.policy_domain_id,
            ("malformed_action",),
            snapshot=snapshot,
        )
    if action.get("policy_domain_id") != snapshot.policy_domain_id:
        return failure_evaluation(
            snapshot.policy_domain_id,
            ("wrong_policy_domain",),
            snapshot=snapshot,
        )
    freshness_issues = predicate_snapshot.check_loaded_snapshot_freshness(snapshot)
    if freshness_issues:
        return failure_evaluation(
            snapshot.policy_domain_id,
            freshness_issues,
            snapshot=snapshot,
        )

    try:
        context = _context_from_action(action)
        verdict = predicate.evaluate(snapshot.binding_checks, context)
    except (TypeError, ValueError):
        return failure_evaluation(
            snapshot.policy_domain_id,
            ("evaluation_error",),
            snapshot=snapshot,
        )
    post_evaluation_freshness_issues = (
        predicate_snapshot.check_loaded_snapshot_freshness(snapshot)
    )
    if post_evaluation_freshness_issues:
        return failure_evaluation(
            snapshot.policy_domain_id,
            post_evaluation_freshness_issues,
            snapshot=snapshot,
        )
    if set(verdict) != {"engine", "decision", "llm_calls", "matches"}:
        return failure_evaluation(
            snapshot.policy_domain_id,
            ("runtime_contract_mismatch",),
            snapshot=snapshot,
        )
    if (
        verdict.get("engine") != predicate.ENGINE
        or verdict.get("decision") not in {"block", "flag", "pass"}
        or verdict.get("llm_calls") != 0
        or not isinstance(verdict.get("matches"), list)
    ):
        return failure_evaluation(
            snapshot.policy_domain_id,
            ("runtime_contract_mismatch",),
            snapshot=snapshot,
        )

    evidence_issues = _context_evidence_issues(context)
    if verdict["decision"] == "flag" and not evidence_issues:
        evidence_issues = ("predicate_flag",)
    receipt = _receipt(
        decision=str(verdict["decision"]),
        domain=snapshot.policy_domain_id,
        snapshot=snapshot,
        matches=verdict["matches"],
        reason_codes=evidence_issues,
    )
    return PolicyEvaluation(verdict=verdict, receipt=receipt)


def failure_evaluation(
    policy_domain_id: str | None,
    reason_codes: Sequence[str],
    *,
    snapshot: predicate_snapshot.LoadedPolicySnapshot | None = None,
) -> PolicyEvaluation:
    """Create an exact private flag verdict and a redacted structural receipt."""
    verdict: dict[str, object] = {
        "engine": predicate.ENGINE,
        "decision": "flag",
        "llm_calls": 0,
        "matches": [],
    }
    return PolicyEvaluation(
        verdict=verdict,
        receipt=_receipt(
            decision="flag",
            domain=policy_domain_id,
            snapshot=snapshot,
            matches=(),
            reason_codes=reason_codes,
        ),
    )


def _context_from_action(action: Mapping[str, object]) -> predicate.ToolCallContext:
    field_names = {field.name for field in fields(predicate.ToolCallContext)}
    values = {key: value for key, value in action.items() if key in field_names}
    return predicate.ToolCallContext(**values)


def _context_evidence_issues(
    context: predicate.ToolCallContext,
) -> tuple[str, ...]:
    checker = getattr(predicate, "context_evidence_issues", None)
    if not callable(checker):
        return ()
    issues = checker(context)
    if not isinstance(issues, Sequence) or isinstance(issues, str):
        return ("runtime_contract_mismatch",)
    return tuple(
        issue for issue in issues if isinstance(issue, str) and _nonempty_text(issue)
    )


def _receipt(
    *,
    decision: str,
    domain: str | None,
    snapshot: predicate_snapshot.LoadedPolicySnapshot | None,
    matches: Sequence[object],
    reason_codes: Sequence[str],
) -> dict[str, object]:
    rejected_ids: set[int] = set()
    node_ids: set[int] = set()
    for match in matches:
        if not isinstance(match, Mapping):
            continue
        rejected_id = match.get("rejected_path_id")
        node_id = match.get("node_id")
        if isinstance(rejected_id, int) and not isinstance(rejected_id, bool):
            rejected_ids.add(rejected_id)
        if isinstance(node_id, int) and not isinstance(node_id, bool):
            node_ids.add(node_id)
    codes = sorted(
        {
            code
            for code in reason_codes
            if isinstance(code, str) and _SAFE_REASON_CODE_RE.fullmatch(code)
        }
    )
    return {
        "contract": RECEIPT_CONTRACT,
        "engine": predicate.ENGINE,
        "decision": decision,
        "llm_calls": 0,
        "policy_domain_id": domain,
        "policy_digest": snapshot.digest if snapshot is not None else None,
        "freshness_token": (
            snapshot.freshness_token if snapshot is not None else None
        ),
        "binding_rows": snapshot.binding_rows if snapshot is not None else 0,
        "binding_compiled": (
            snapshot.binding_compiled if snapshot is not None else 0
        ),
        "advisory_rows": snapshot.advisory_rows if snapshot is not None else 0,
        "uncompilable_rows": (
            snapshot.uncompilable_rows if snapshot is not None else 0
        ),
        "matched_rejected_path_ids": sorted(rejected_ids),
        "matched_node_ids": sorted(node_ids),
        "reason_codes": codes,
        "advisory_reason_counts": (
            dict(snapshot.advisory_reason_counts) if snapshot is not None else {}
        ),
    }


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and _SAFE_OPAQUE_ID_RE.fullmatch(value) is not None


def reference_main(argv: Sequence[str] | None = None) -> int:
    """JSON stdin/stdout reference consumer; stdout is receipt-only."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        sys.stderr.write("usage: predicate_consumer.py SNAPSHOT\n")
        return 2
    try:
        action = json.load(sys.stdin)
    except (UnicodeDecodeError, json.JSONDecodeError):
        evaluation = failure_evaluation(None, ("malformed_action",))
    else:
        evaluation = evaluate_policy(arguments[0], action)
    json.dump(evaluation.receipt, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


__all__ = [
    "PolicyEvaluation",
    "RECEIPT_CONTRACT",
    "evaluate_loaded_policy",
    "evaluate_policy",
    "failure_evaluation",
    "reference_main",
]
