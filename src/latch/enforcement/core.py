"""Shared A4 policy-check PEP over the private A2 predicate evaluator."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Mapping, Sequence

from latch.gate import predicate
from latch.gate import predicate_consumer


POLICY_CHECK_CONTRACT = "latch-policy-check-receipt-v1"
POLICY_CHECK_VERSION = "a4-policy-check-v1"
PASS_EXIT = 0
BLOCK_EXIT = 10
FLAG_EXIT = 20
INVALID_EXIT = 30
_OUTCOME_EXITS = {
    "pass": PASS_EXIT,
    "block": BLOCK_EXIT,
    "flag": FLAG_EXIT,
    "invalid": INVALID_EXIT,
}
_MODES = frozenset({"enforce", "observe", "ci-only"})
_EFFECTS = frozenset({"read-only", "side-effecting"})
READ_ONLY_INSPECTION_TOOLS = frozenset({"latch.policy.inspect"})
_READ_ONLY_ACTION_KEYS = frozenset({"policy_domain_id", "tool_name"})
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]*(?::[a-z][a-z0-9_]*)?$")
_INVALID_EVALUATION_REASONS = frozenset(
    {
        "malformed_action",
        "policy_domain_missing",
        "malformed_freshness_token",
        "wrong_policy_domain",
        "snapshot_missing",
        "snapshot_unreadable",
        "snapshot_corrupt",
        "wrong_snapshot_version",
        "wrong_predicate_engine",
        "wrong_projection_engine",
        "snapshot_invalid",
        "snapshot_digest_mismatch",
        "snapshot_compiler_mismatch",
        "evaluation_error",
        "runtime_contract_mismatch",
        "snapshot_kind_mismatch",
    }
)
_A2_COUNT_FIELDS = (
    "binding_rows",
    "binding_compiled",
    "advisory_rows",
    "uncompilable_rows",
)


@dataclass(frozen=True)
class PolicyCheckResult:
    outcome: str
    decision: str
    exit_code: int
    denied: bool
    exempt: bool
    receipt: Mapping[str, object]


def check_policy(
    snapshot_path: str | Path,
    action: Mapping[str, object] | object,
    *,
    subject: Mapping[str, object] | object,
    adapter_id: str,
    adapter_version: str,
    mode: str,
    effect: str,
    supplemental_advisory_counts: Mapping[str, int] | None = None,
    retry_of: str | None = None,
) -> PolicyCheckResult:
    """Evaluate one action and translate A2 semantics into enforceable exits."""
    prepared = _prepare_metadata(
        action=action,
        subject=subject,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        mode=mode,
        effect=effect,
        supplemental_advisory_counts=supplemental_advisory_counts,
        retry_of=retry_of,
    )
    if isinstance(prepared, PolicyCheckResult):
        return prepared
    metadata = prepared

    if metadata["effect"] == "read-only":
        return _result(
            outcome="pass",
            decision="pass",
            metadata=metadata,
            policy_digest=None,
            freshness_token=None,
            counts={field: 0 for field in _A2_COUNT_FIELDS},
            matched_rejected_path_ids=(),
            matched_node_ids=(),
            reason_codes=("read_only_exempt",),
            advisory_reason_counts={},
            exempt=True,
        )

    try:
        evaluation = predicate_consumer.evaluate_policy(snapshot_path, action)
    except Exception:
        return _invalid_result(
            "evaluation_error",
            metadata=metadata,
        )
    return finalize_policy_evaluation(
        evaluation,
        action=action,
        subject=metadata["subject"],
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        mode=mode,
        effect=effect,
        supplemental_advisory_counts=metadata["supplemental_advisory_counts"],
        retry_of=retry_of,
    )


def finalize_policy_evaluation(
    evaluation: predicate_consumer.PolicyEvaluation | object,
    *,
    action: Mapping[str, object] | object,
    subject: Mapping[str, object] | object,
    adapter_id: str,
    adapter_version: str,
    mode: str,
    effect: str,
    supplemental_advisory_counts: Mapping[str, int] | None = None,
    retry_of: str | None = None,
) -> PolicyCheckResult:
    """Finalize an A2 evaluation through the one shared A4 receipt contract."""
    prepared = _prepare_metadata(
        action=action,
        subject=subject,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        mode=mode,
        effect=effect,
        supplemental_advisory_counts=supplemental_advisory_counts,
        retry_of=retry_of,
    )
    if isinstance(prepared, PolicyCheckResult):
        return prepared
    metadata = prepared
    try:
        normalized = _normalize_evaluation(evaluation)
    except ValueError:
        return _invalid_result("runtime_contract_mismatch", metadata=metadata)
    if normalized["policy_domain_id"] != metadata["policy_domain_id"]:
        return _invalid_result("runtime_contract_mismatch", metadata=metadata)

    decision = normalized["decision"]
    reason_codes = normalized["reason_codes"]
    outcome = (
        "invalid"
        if decision == "flag"
        and any(code in _INVALID_EVALUATION_REASONS for code in reason_codes)
        else decision
    )
    return _result(
        outcome=outcome,
        decision="flag" if outcome == "invalid" else decision,
        metadata=metadata,
        policy_digest=normalized["policy_digest"],
        freshness_token=normalized["freshness_token"],
        counts=normalized["counts"],
        matched_rejected_path_ids=normalized["matched_rejected_path_ids"],
        matched_node_ids=normalized["matched_node_ids"],
        reason_codes=reason_codes,
        advisory_reason_counts=normalized["advisory_reason_counts"],
        exempt=False,
    )


def recheck_after_snapshot_refresh(
    prior_receipt: Mapping[str, object] | object,
    snapshot_path: str | Path,
    action: Mapping[str, object] | object,
    *,
    subject: Mapping[str, object] | object,
    adapter_id: str,
    adapter_version: str,
    mode: str,
    effect: str,
    supplemental_advisory_counts: Mapping[str, int] | None = None,
) -> PolicyCheckResult:
    """Re-evaluate after a caller republishes, bound to the denied action.

    This function never mutates or republishes a snapshot.  It permits only a
    fresh A2 load for the same immutable subject and records the prior receipt
    digest as lineage.
    """
    prepared = _prepare_metadata(
        action=action,
        subject=subject,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        mode=mode,
        effect=effect,
        supplemental_advisory_counts=supplemental_advisory_counts,
        retry_of=None,
    )
    if isinstance(prepared, PolicyCheckResult):
        return prepared
    metadata = prepared
    mismatch_reason = _refresh_mismatch(prior_receipt, metadata)
    if mismatch_reason is not None:
        return _invalid_result(mismatch_reason, metadata=metadata)
    assert isinstance(prior_receipt, Mapping)
    prior_digest = prior_receipt["receipt_digest"]
    assert isinstance(prior_digest, str)
    return check_policy(
        snapshot_path,
        action,
        subject=metadata["subject"],
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        mode=mode,
        effect=effect,
        supplemental_advisory_counts=metadata["supplemental_advisory_counts"],
        retry_of=prior_digest,
    )


def invalid_policy_check(
    reason_code: str,
    *,
    action: Mapping[str, object] | object | None = None,
    subject: Mapping[str, object] | object | None = None,
    adapter_id: str = "invalid",
    adapter_version: str = "invalid",
    mode: str = "enforce",
    effect: str = "side-effecting",
) -> PolicyCheckResult:
    """Build a redacted invalid/error result without serializing exceptions."""
    prepared = _prepare_metadata(
        action={} if action is None else action,
        subject={} if subject is None else subject,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        mode=mode,
        effect=effect,
        supplemental_advisory_counts=None,
        retry_of=None,
        tolerate_invalid=True,
    )
    assert isinstance(prepared, dict)
    safe_reason = reason_code if _safe_reason(reason_code) else "invalid_request"
    return _invalid_result(safe_reason, metadata=prepared)


def reference_main(argv: Sequence[str] | None = None) -> int:
    """Receipt-only JSON stdin/stdout executable with real outcome exits."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        result = invalid_policy_check("invalid_arguments")
    else:
        try:
            request = json.load(sys.stdin)
        except (UnicodeDecodeError, json.JSONDecodeError):
            result = invalid_policy_check("malformed_request")
        else:
            if not isinstance(request, Mapping) or set(request) != {
                "action",
                "subject",
                "adapter",
                "mode",
                "effect",
            }:
                result = invalid_policy_check("malformed_request")
            else:
                adapter = request.get("adapter")
                if not isinstance(adapter, Mapping) or set(adapter) != {
                    "id",
                    "version",
                }:
                    result = invalid_policy_check("malformed_adapter")
                else:
                    result = check_policy(
                        arguments[0],
                        request.get("action"),
                        subject=request.get("subject"),
                        adapter_id=adapter.get("id"),  # type: ignore[arg-type]
                        adapter_version=adapter.get("version"),  # type: ignore[arg-type]
                        mode=request.get("mode"),  # type: ignore[arg-type]
                        effect=request.get("effect"),  # type: ignore[arg-type]
                    )
    json.dump(result.receipt, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return result.exit_code


def _prepare_metadata(
    *,
    action: Mapping[str, object] | object,
    subject: Mapping[str, object] | object,
    adapter_id: object,
    adapter_version: object,
    mode: object,
    effect: object,
    supplemental_advisory_counts: Mapping[str, int] | None,
    retry_of: object,
    tolerate_invalid: bool = False,
) -> dict[str, object] | PolicyCheckResult:
    errors: list[str] = []
    normalized_subject: dict[str, str]
    try:
        normalized_subject = _normalize_subject(subject)
    except ValueError:
        errors.append("invalid_subject")
        normalized_subject = {"kind": "invalid"}
    safe_adapter_id = _safe_token_or_invalid(adapter_id)
    safe_adapter_version = _safe_token_or_invalid(adapter_version)
    if safe_adapter_id == "invalid" and adapter_id != "invalid":
        errors.append("invalid_adapter")
    if safe_adapter_version == "invalid" and adapter_version != "invalid":
        errors.append("invalid_adapter")
    safe_mode = mode if mode in _MODES else "enforce"
    if mode not in _MODES:
        errors.append("invalid_mode")
    safe_effect = effect if effect in _EFFECTS else "side-effecting"
    if effect not in _EFFECTS:
        errors.append("invalid_effect")
    elif effect == "read-only" and not _exact_read_only_inspection(action):
        errors.append("read_only_effect_mismatch")
        safe_effect = "side-effecting"
    domain = action.get("policy_domain_id") if isinstance(action, Mapping) else None
    if not _safe_token(domain):
        errors.append("malformed_action")
        domain = None
    try:
        supplemental = _safe_counts(supplemental_advisory_counts or {})
    except ValueError:
        errors.append("invalid_advisory_counts")
        supplemental = {}
    safe_retry = retry_of if isinstance(retry_of, str) and _DIGEST_RE.fullmatch(retry_of) else None
    if retry_of is not None and safe_retry is None:
        errors.append("invalid_retry_receipt")
    metadata: dict[str, object] = {
        "subject": normalized_subject,
        "subject_digest": _canonical_digest(normalized_subject),
        "adapter_id": safe_adapter_id,
        "adapter_version": safe_adapter_version,
        "mode": safe_mode,
        "effect": safe_effect,
        "policy_domain_id": domain,
        "supplemental_advisory_counts": supplemental,
        "retry_of": safe_retry,
    }
    if errors and not tolerate_invalid:
        return _invalid_result(errors[0], metadata=metadata)
    return metadata


def _normalize_subject(subject: Mapping[str, object] | object) -> dict[str, str]:
    if not isinstance(subject, Mapping):
        raise ValueError("subject must be an object")
    kind = subject.get("kind")
    if kind == "host-action":
        required = {
            "kind",
            "repository_id",
            "host_id",
            "session_id",
            "action_id",
        }
        if set(subject) != required:
            raise ValueError("host subject shape is invalid")
        names = ("repository_id", "host_id", "session_id", "action_id")
    elif kind == "git-range":
        required = {"kind", "repository_id", "base_sha", "head_sha"}
        if set(subject) != required:
            raise ValueError("git subject shape is invalid")
        names = ("repository_id", "base_sha", "head_sha")
    else:
        raise ValueError("subject kind is invalid")
    normalized = {"kind": str(kind)}
    for name in names:
        value = subject.get(name)
        if name in {"base_sha", "head_sha"}:
            if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
                raise ValueError(f"{name} is invalid")
        elif name == "repository_id":
            if not isinstance(value, str) or _REPOSITORY_RE.fullmatch(value) is None:
                raise ValueError("repository_id is invalid")
        elif not _safe_token(value):
            raise ValueError(f"{name} is invalid")
        normalized[name] = str(value)
    return normalized


def _normalize_evaluation(evaluation: object) -> dict[str, object]:
    if not isinstance(evaluation, predicate_consumer.PolicyEvaluation):
        raise ValueError("evaluation has the wrong type")
    verdict = evaluation.verdict
    receipt = evaluation.receipt
    if not isinstance(verdict, Mapping) or not isinstance(receipt, Mapping):
        raise ValueError("evaluation values must be objects")
    decision = verdict.get("decision")
    if (
        set(verdict) != {"engine", "decision", "llm_calls", "matches"}
        or verdict.get("engine") != predicate.ENGINE
        or decision not in {"pass", "block", "flag"}
        or verdict.get("llm_calls") != 0
        or not isinstance(verdict.get("matches"), list)
        or receipt.get("contract") != predicate_consumer.RECEIPT_CONTRACT
        or receipt.get("engine") != predicate.ENGINE
        or receipt.get("decision") != decision
        or receipt.get("llm_calls") != 0
    ):
        raise ValueError("evaluation contract mismatch")
    counts: dict[str, int] = {}
    for field in _A2_COUNT_FIELDS:
        value = receipt.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid policy counts")
        counts[field] = value
    reason_codes = _safe_reason_sequence(receipt.get("reason_codes"))
    advisory_counts = _safe_counts(receipt.get("advisory_reason_counts"))
    rejected_ids = _safe_id_sequence(receipt.get("matched_rejected_path_ids"))
    node_ids = _safe_id_sequence(receipt.get("matched_node_ids"))
    digest = receipt.get("policy_digest")
    if digest is not None and (
        not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None
    ):
        raise ValueError("policy digest is invalid")
    freshness_token = receipt.get("freshness_token")
    if freshness_token is not None and not _safe_token(freshness_token):
        raise ValueError("freshness token is invalid")
    domain = receipt.get("policy_domain_id")
    if domain is not None and not _safe_token(domain):
        raise ValueError("policy domain is invalid")
    return {
        "decision": str(decision),
        "policy_domain_id": domain,
        "policy_digest": digest,
        "freshness_token": freshness_token,
        "counts": counts,
        "matched_rejected_path_ids": rejected_ids,
        "matched_node_ids": node_ids,
        "reason_codes": reason_codes,
        "advisory_reason_counts": advisory_counts,
    }


def _result(
    *,
    outcome: str,
    decision: str,
    metadata: Mapping[str, object],
    policy_digest: object,
    freshness_token: object,
    counts: Mapping[str, int],
    matched_rejected_path_ids: Sequence[int],
    matched_node_ids: Sequence[int],
    reason_codes: Sequence[str],
    advisory_reason_counts: Mapping[str, int],
    exempt: bool,
) -> PolicyCheckResult:
    mode = str(metadata["mode"])
    effect = str(metadata["effect"])
    semantic_exit = _OUTCOME_EXITS[outcome]
    exit_code = (
        PASS_EXIT if mode == "observe" and outcome != "invalid" else semantic_exit
    )
    denied = (
        not exempt
        and effect == "side-effecting"
        and mode in {"enforce", "ci-only"}
        and outcome != "pass"
    )
    combined_advisory = dict(advisory_reason_counts)
    combined_advisory.update(metadata["supplemental_advisory_counts"])  # type: ignore[arg-type]
    receipt: dict[str, object] = {
        "contract": POLICY_CHECK_CONTRACT,
        "core": POLICY_CHECK_VERSION,
        "engine": predicate.ENGINE,
        "adapter": {
            "id": metadata["adapter_id"],
            "version": metadata["adapter_version"],
        },
        "mode": mode,
        "effect": effect,
        "outcome": outcome,
        "decision": decision,
        "exit_code": exit_code,
        "denied": denied,
        "exempt": exempt,
        "policy": {
            "domain_id": metadata["policy_domain_id"],
            "snapshot_digest": policy_digest,
            "freshness_token": freshness_token,
        },
        "subject": metadata["subject"],
        "subject_digest": metadata["subject_digest"],
        **{field: counts[field] for field in _A2_COUNT_FIELDS},
        "matched_rejected_path_ids": list(matched_rejected_path_ids),
        "matched_node_ids": list(matched_node_ids),
        "reason_codes": list(reason_codes),
        "advisory_reason_counts": dict(sorted(combined_advisory.items())),
    }
    retry_of = metadata.get("retry_of")
    if retry_of is not None:
        receipt["retry_of"] = retry_of
    receipt["receipt_digest"] = _canonical_digest(receipt)
    return PolicyCheckResult(
        outcome=outcome,
        decision=decision,
        exit_code=exit_code,
        denied=denied,
        exempt=exempt,
        receipt=receipt,
    )


def _invalid_result(
    reason_code: str,
    *,
    metadata: Mapping[str, object],
) -> PolicyCheckResult:
    return _result(
        outcome="invalid",
        decision="flag",
        metadata=metadata,
        policy_digest=None,
        freshness_token=None,
        counts={field: 0 for field in _A2_COUNT_FIELDS},
        matched_rejected_path_ids=(),
        matched_node_ids=(),
        reason_codes=(reason_code,),
        advisory_reason_counts={},
        exempt=False,
    )


def _refresh_mismatch(
    prior_receipt: Mapping[str, object] | object,
    metadata: Mapping[str, object],
) -> str | None:
    if not _valid_shared_receipt(prior_receipt):
        return "refresh_receipt_invalid"
    assert isinstance(prior_receipt, Mapping)
    if prior_receipt.get("denied") is not True:
        return "refresh_receipt_not_denied"
    if (
        prior_receipt.get("subject_digest") != metadata["subject_digest"]
        or prior_receipt.get("subject") != metadata["subject"]
    ):
        return "refresh_subject_mismatch"
    if prior_receipt.get("adapter") != {
        "id": metadata["adapter_id"],
        "version": metadata["adapter_version"],
    }:
        return "refresh_adapter_mismatch"
    if (
        prior_receipt.get("mode") != metadata["mode"]
        or prior_receipt.get("effect") != metadata["effect"]
    ):
        return "refresh_context_mismatch"
    policy = prior_receipt.get("policy")
    if not isinstance(policy, Mapping) or policy.get("domain_id") != metadata.get(
        "policy_domain_id"
    ):
        return "refresh_policy_domain_mismatch"
    return None


def _valid_shared_receipt(receipt: object) -> bool:
    if not isinstance(receipt, Mapping):
        return False
    digest = receipt.get("receipt_digest")
    if (
        receipt.get("contract") != POLICY_CHECK_CONTRACT
        or not isinstance(digest, str)
        or _DIGEST_RE.fullmatch(digest) is None
    ):
        return False
    payload = dict(receipt)
    payload.pop("receipt_digest", None)
    return digest == _canonical_digest(payload)


def _safe_reason_sequence(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        raise ValueError("reason codes must be a sequence")
    result: list[str] = []
    for item in value:
        if not _safe_reason(item):
            raise ValueError("reason code is invalid")
        if item not in result:
            result.append(item)
    return tuple(result)


def _safe_id_sequence(value: object) -> tuple[int, ...]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        raise ValueError("ids must be a sequence")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError("id is invalid")
        if item not in result:
            result.append(item)
    return tuple(sorted(result))


def _safe_counts(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError("counts must be an object")
    result: dict[str, int] = {}
    for key, count in value.items():
        if (
            not _safe_reason(key)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
        ):
            raise ValueError("count entry is invalid")
        result[str(key)] = count
    return dict(sorted(result.items()))


def _safe_token(value: object) -> bool:
    return isinstance(value, str) and _TOKEN_RE.fullmatch(value) is not None


def _exact_read_only_inspection(action: object) -> bool:
    return (
        isinstance(action, Mapping)
        and set(action) == _READ_ONLY_ACTION_KEYS
        and action.get("tool_name") in READ_ONLY_INSPECTION_TOOLS
    )


def _safe_token_or_invalid(value: object) -> str:
    return str(value) if _safe_token(value) else "invalid"


def _safe_reason(value: object) -> bool:
    return isinstance(value, str) and _REASON_RE.fullmatch(value) is not None


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BLOCK_EXIT",
    "FLAG_EXIT",
    "INVALID_EXIT",
    "PASS_EXIT",
    "POLICY_CHECK_CONTRACT",
    "POLICY_CHECK_VERSION",
    "READ_ONLY_INSPECTION_TOOLS",
    "PolicyCheckResult",
    "check_policy",
    "finalize_policy_evaluation",
    "invalid_policy_check",
    "recheck_after_snapshot_refresh",
    "reference_main",
]
