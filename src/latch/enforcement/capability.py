"""Strict A4 capability declarations and evidence-backed enforce gating."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, Sequence

from latch.enforcement import timeout_harness


CAPABILITY_CONTRACT = "latch-capability-declaration-v1"
CODEX_MINIMUM_VERSION = "0.124.0"
MODES = frozenset({"enforce", "observe", "ci-only"})
TIMEOUT_BASES = frozenset({"documented", "empirical", "unknown"})
TIMEOUT_BEHAVIORS = frozenset({"fail-open", "fail-closed", "unknown"})
SECOND_LINE_SCOPES = frozenset({"local", "self-hosted"})
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MECHANISM_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class CapabilityDeclarationError(ValueError):
    """A machine-readable capability declaration violated the closed schema."""


@dataclass(frozen=True)
class ToolCoverage:
    intercepted: tuple[str, ...]
    not_intercepted: tuple[str, ...]
    complete: bool


@dataclass(frozen=True)
class TimeoutPosture:
    basis: str
    behavior: str
    receipt_on_timeout: bool | None
    fixture_ids: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityDeclaration:
    contract: str
    host_id: str
    minimum_host_version: str
    hook_event: str
    deny_mechanisms: tuple[str, ...]
    claimed_mode: str
    tool_coverage: ToolCoverage
    timeout: TimeoutPosture
    platform_caveats: tuple[str, ...]
    vocabulary: tuple[str, ...]
    corpus_fixture_ids: tuple[str, ...]
    second_line_required: bool

    def to_json_object(self) -> dict[str, object]:
        return {
            "contract": self.contract,
            "host_id": self.host_id,
            "minimum_host_version": self.minimum_host_version,
            "hook_event": self.hook_event,
            "deny_mechanisms": list(self.deny_mechanisms),
            "claimed_mode": self.claimed_mode,
            "tool_coverage": {
                "intercepted": list(self.tool_coverage.intercepted),
                "not_intercepted": list(self.tool_coverage.not_intercepted),
                "complete": self.tool_coverage.complete,
            },
            "timeout": {
                "basis": self.timeout.basis,
                "behavior": self.timeout.behavior,
                "receipt_on_timeout": self.timeout.receipt_on_timeout,
                "fixture_ids": list(self.timeout.fixture_ids),
            },
            "platform_caveats": list(self.platform_caveats),
            "vocabulary": list(self.vocabulary),
            "corpus_fixture_ids": list(self.corpus_fixture_ids),
            "second_line_required": self.second_line_required,
        }


@dataclass(frozen=True)
class RuntimeFacts:
    host_id: str
    host_version: str
    platform: str
    legacy_feature_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnforcementEvidence:
    timeout_receipts: tuple[timeout_harness.TimeoutProbeReceipt, ...] = ()
    corpus_fixture_ids: tuple[str, ...] = ()
    corpus_manifest_digest: str | None = None
    corpus_candidate_count: int = 0
    corpus_complete: bool = False
    second_line_available: bool = False
    second_line_scope: str | None = None


@dataclass(frozen=True)
class CapabilityAssessment:
    effective_mode: str
    enforce_ready: bool
    reason_codes: tuple[str, ...]


_DECLARATION_KEYS = {
    "contract",
    "host_id",
    "minimum_host_version",
    "hook_event",
    "deny_mechanisms",
    "claimed_mode",
    "tool_coverage",
    "timeout",
    "platform_caveats",
    "vocabulary",
    "corpus_fixture_ids",
    "second_line_required",
}


def parse_declaration(raw: Mapping[str, object] | object) -> CapabilityDeclaration:
    if not isinstance(raw, Mapping) or set(raw) != _DECLARATION_KEYS:
        raise CapabilityDeclarationError("declaration keys do not match the schema")
    if raw.get("contract") != CAPABILITY_CONTRACT:
        raise CapabilityDeclarationError("unsupported capability contract")

    host_id = _token(raw.get("host_id"), "host_id")
    minimum_version = _version(
        raw.get("minimum_host_version"), "minimum_host_version"
    )
    if host_id == "codex" and _version_tuple(minimum_version) < _version_tuple(
        CODEX_MINIMUM_VERSION
    ):
        raise CapabilityDeclarationError(
            f"Codex minimum_host_version must be at least {CODEX_MINIMUM_VERSION}"
        )
    hook_event = _token(raw.get("hook_event"), "hook_event")
    claimed_mode = raw.get("claimed_mode")
    if claimed_mode not in MODES:
        raise CapabilityDeclarationError("claimed_mode is invalid")

    deny_mechanisms = _token_sequence(
        raw.get("deny_mechanisms"),
        "deny_mechanisms",
        pattern=_MECHANISM_RE,
        require_nonempty=True,
    )
    if any("ask" in re.split(r"[._:-]", item) for item in deny_mechanisms):
        raise CapabilityDeclarationError(
            "deny_mechanisms may use deny behavior only; ask is unsupported"
        )

    coverage_raw = raw.get("tool_coverage")
    if not isinstance(coverage_raw, Mapping) or set(coverage_raw) != {
        "intercepted",
        "not_intercepted",
        "complete",
    }:
        raise CapabilityDeclarationError("tool_coverage has an invalid shape")
    if not isinstance(coverage_raw.get("complete"), bool):
        raise CapabilityDeclarationError("tool_coverage.complete must be boolean")
    intercepted = _token_sequence(
        coverage_raw.get("intercepted"),
        "tool_coverage.intercepted",
        require_nonempty=claimed_mode == "enforce",
    )
    not_intercepted = _token_sequence(
        coverage_raw.get("not_intercepted"),
        "tool_coverage.not_intercepted",
    )
    if set(intercepted).intersection(not_intercepted):
        raise CapabilityDeclarationError(
            "tool coverage cannot classify one tool as both intercepted and not"
        )
    coverage = ToolCoverage(
        intercepted=intercepted,
        not_intercepted=not_intercepted,
        complete=coverage_raw["complete"],
    )

    timeout_raw = raw.get("timeout")
    if not isinstance(timeout_raw, Mapping) or set(timeout_raw) != {
        "basis",
        "behavior",
        "receipt_on_timeout",
        "fixture_ids",
    }:
        raise CapabilityDeclarationError("timeout has an invalid shape")
    basis = timeout_raw.get("basis")
    behavior = timeout_raw.get("behavior")
    receipt_truth = timeout_raw.get("receipt_on_timeout")
    if basis not in TIMEOUT_BASES:
        raise CapabilityDeclarationError("timeout.basis is invalid")
    if behavior not in TIMEOUT_BEHAVIORS:
        raise CapabilityDeclarationError("timeout.behavior is invalid")
    if receipt_truth is not None and not isinstance(receipt_truth, bool):
        raise CapabilityDeclarationError(
            "timeout.receipt_on_timeout must be boolean or null"
        )
    timeout = TimeoutPosture(
        basis=str(basis),
        behavior=str(behavior),
        receipt_on_timeout=receipt_truth,
        fixture_ids=_token_sequence(
            timeout_raw.get("fixture_ids"), "timeout.fixture_ids"
        ),
    )

    platform_caveats = _token_sequence(
        raw.get("platform_caveats"), "platform_caveats"
    )
    if "native-windows-pending" not in platform_caveats:
        raise CapabilityDeclarationError(
            "platform_caveats must record native-windows-pending"
        )
    vocabulary = _token_sequence(raw.get("vocabulary"), "vocabulary")
    if vocabulary != ("allow", "deny"):
        raise CapabilityDeclarationError(
            "vocabulary must contain exactly allow and deny"
        )
    corpus_fixture_ids = _token_sequence(
        raw.get("corpus_fixture_ids"), "corpus_fixture_ids"
    )
    second_line_required = raw.get("second_line_required")
    if second_line_required is not True:
        raise CapabilityDeclarationError("second_line_required must be true")

    return CapabilityDeclaration(
        contract=CAPABILITY_CONTRACT,
        host_id=host_id,
        minimum_host_version=minimum_version,
        hook_event=hook_event,
        deny_mechanisms=deny_mechanisms,
        claimed_mode=str(claimed_mode),
        tool_coverage=coverage,
        timeout=timeout,
        platform_caveats=platform_caveats,
        vocabulary=vocabulary,
        corpus_fixture_ids=corpus_fixture_ids,
        second_line_required=True,
    )


def assess_capability(
    declaration: CapabilityDeclaration,
    runtime: RuntimeFacts,
    evidence: EnforcementEvidence,
) -> CapabilityAssessment:
    if declaration.claimed_mode != "enforce":
        return CapabilityAssessment(
            effective_mode=declaration.claimed_mode,
            enforce_ready=False,
            reason_codes=(),
        )

    reasons: list[str] = []
    if runtime.host_id != declaration.host_id:
        reasons.append("host_id_mismatch")
    try:
        runtime_version = _version(runtime.host_version, "runtime.host_version")
    except CapabilityDeclarationError:
        reasons.append("host_version_invalid")
    else:
        if _version_tuple(runtime_version) < _version_tuple(
            declaration.minimum_host_version
        ):
            reasons.append("host_version_below_minimum")
    if declaration.host_id == "codex" and "features.codex_hooks" in set(
        runtime.legacy_feature_flags
    ):
        reasons.append("legacy_codex_hooks_flag_present")
    if declaration.tool_coverage.complete is not True:
        reasons.append("tool_coverage_incomplete")

    if declaration.timeout.basis != "empirical":
        reasons.append("empirical_timeout_not_declared")
    if not evidence.timeout_receipts:
        reasons.append("empirical_timeout_missing")
    elif not _matching_timeout_receipt(declaration, runtime, evidence):
        reasons.append("empirical_timeout_invalid")

    declared_corpus = set(declaration.corpus_fixture_ids)
    observed_corpus = set(evidence.corpus_fixture_ids)
    if not declared_corpus or not declared_corpus.intersection(observed_corpus):
        reasons.append("corpus_conformance_missing")
    elif (
        evidence.corpus_complete is not True
        or isinstance(evidence.corpus_candidate_count, bool)
        or evidence.corpus_candidate_count <= 0
        or not isinstance(evidence.corpus_manifest_digest, str)
        or _DIGEST_RE.fullmatch(evidence.corpus_manifest_digest) is None
    ):
        reasons.append("corpus_conformance_incomplete")

    if evidence.second_line_available is not True:
        reasons.append("second_line_missing")
    elif evidence.second_line_scope not in SECOND_LINE_SCOPES:
        reasons.append("second_line_scope_unsupported")

    reason_codes = tuple(dict.fromkeys(reasons))
    return CapabilityAssessment(
        effective_mode="observe" if reason_codes else "enforce",
        enforce_ready=not reason_codes,
        reason_codes=reason_codes,
    )


def canonical_hook_decision(value: object) -> str:
    if value not in {"allow", "deny"}:
        raise ValueError("hook decision must be allow or deny")
    return str(value)


def _matching_timeout_receipt(
    declaration: CapabilityDeclaration,
    runtime: RuntimeFacts,
    evidence: EnforcementEvidence,
) -> bool:
    expected_behavior = declaration.timeout.behavior
    expected_receipt = declaration.timeout.receipt_on_timeout
    for receipt in evidence.timeout_receipts:
        if not timeout_harness.valid_timeout_receipt(receipt):
            continue
        observed_behavior = "fail-open" if receipt.action_continued else "fail-closed"
        if (
            receipt.fixture_id in declaration.timeout.fixture_ids
            and receipt.host_id == declaration.host_id == runtime.host_id
            and receipt.host_version == runtime.host_version
            and receipt.hook_event == declaration.hook_event
            and receipt.platform == runtime.platform
            and receipt.timed_out is True
            and observed_behavior == expected_behavior
            and receipt.receipt_observed is expected_receipt
        ):
            return True
    return False


def _token(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise CapabilityDeclarationError(
            f"{field_name} must be a bounded opaque token"
        )
    return value


def _token_sequence(
    value: object,
    field_name: str,
    *,
    pattern: re.Pattern[str] = _TOKEN_RE,
    require_nonempty: bool = False,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        raise CapabilityDeclarationError(f"{field_name} must be a list of tokens")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or pattern.fullmatch(item) is None:
            raise CapabilityDeclarationError(f"{field_name} contains an invalid token")
        if item in result:
            raise CapabilityDeclarationError(f"{field_name} contains duplicates")
        result.append(item)
    if require_nonempty and not result:
        raise CapabilityDeclarationError(f"{field_name} must not be empty")
    return tuple(result)


def _version(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _VERSION_RE.fullmatch(value) is None:
        raise CapabilityDeclarationError(
            f"{field_name} must be a normalized numeric version"
        )
    return value


def _version_tuple(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


__all__ = [
    "CAPABILITY_CONTRACT",
    "CODEX_MINIMUM_VERSION",
    "CapabilityAssessment",
    "CapabilityDeclaration",
    "CapabilityDeclarationError",
    "EnforcementEvidence",
    "RuntimeFacts",
    "assess_capability",
    "canonical_hook_decision",
    "parse_declaration",
]
