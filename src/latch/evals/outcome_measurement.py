"""Frozen outcome-measurement contract v2.6.

This module is deliberately independent from the legacy ``correlator``.  It
implements the observation -> invocation -> immutable receipt fold specified by
Latch capture 4164, including source health, snapshot freshness, monotone
lineage, three-valued coverage arithmetic, and deterministic reporting.

The public entry points are:

``measure(source_roots, config, prior_receipts=...)``
    Enumerate the *complete* pinned source roots, take full-file snapshots,
    parse observations, and fold the current protocol generation.

``audit(state, ...)``
    Verify capture/manifest pins before computing O1/O2/O3.

``audit_rows(...)``
    Adapter for report code that already has finalized receipt-like mappings.

No function in this module opens T0 or performs a live canary.  The canary
validator checks captured evidence only; live Codex and Claude Code nonce
canaries remain a post-merge operation.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from latch.proof.project_proof import (
    PROJECT_FOREIGN,
    PROJECT_KEY_EPOCH_MISMATCH,
    PROJECT_MATCH,
    PROJECT_PROOF_INVALID,
    PROJECT_PROOF_MISSING,
    PROJECT_PROOF_VERSION,
    ProjectProofContext,
    compare_project_proofs,
)


CONTRACT_VERSION = "2.6"
CONTRACT_SHA256 = "3d5f309d9cc8e0c99d7d0d2e85692ce0d8f201b00fe3bd2026aa3ef0a030a6d7"
CAPTURE_NODE_ID = 4164
SUPERSEDED_CAPTURE_NODE_ID = 4162
RATIFICATION_NODE_IDS = (4113, 4137)
MEASUREMENT_PROTOCOL_VERSION = "outcome-v2.6.0"
FROZEN_FIXTURE_DATA_SHA256 = {
    "claude-transcript-2026-07-22.sanitized.jsonl": (
        "cf0869e2af7380cac8d6803a1a9d5c76f3fd3f7d6b4079969c1277e96b866f43"
    ),
    "codex-rollout-2026-07-29.sanitized.jsonl": (
        "f3c1eb261ab5890a9e2d0e9b3174ffe99fbedf57a9cc67d5e516974ed8501ec8"
    ),
    "gate-2026-08-03.sanitized.jsonl": (
        "187e1d48697a676db383068642d2f6d1dde44ed885b56f6c5ba75117cc6a8903"
    ),
    "golden-report-v2.6.json": (
        "0c221bc5af5f31732160645534f78de458d92d465db786a817e4c949bf2db619"
    ),
}
FROZEN_FIXTURE_PACK_SHA256 = {
    **FROZEN_FIXTURE_DATA_SHA256,
    "manifest.json": (
        "a9ea3c56a369fe502c262e09d7b1e21e3193991bbeb6f8dcfb9a84bcfe52fa07"
    ),
}

SOURCE_GATE = "S1"
SOURCE_HOST = "S2"
SOURCES = (SOURCE_GATE, SOURCE_HOST)
DISPOSITIONS = (
    "foreign_project",
    "conflict",
    "loss_signal",
    "skipped",
    "pilot",
    "confirmatory",
)
OUTCOMES = ("ACCEPTED", "OVERRIDDEN", "AMBIGUOUS", "UNRESOLVED", "CENSORED")
VERDICTS = ("PROCEED", "MODIFY", "DO_NOT_PROCEED", "NEEDS_HUMAN_JUDGMENT")
REQUIRED_ID_LIST_FIELDS = (
    "evidence_ids",
    "decision_chain",
    "abandoned_paths",
    "active_constraints",
    "current_direction",
    "seed_ids",
)
FRESHNESS_SECONDS = 1800 + 300
JOIN_TOLERANCE_SECONDS = 300
WINDOW_SECONDS = 1800
O3_MAX_AMBIGUOUS = 6
V1_ELIGIBLE_TARGET = 30


def _utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


ProjectProof = Mapping[str, str]


def make_project_proof(
    path: str | os.PathLike[str],
    vault_key: bytes,
    key_epoch: str,
    *,
    proof_version: str = PROJECT_PROOF_VERSION,
) -> dict[str, str]:
    """Compatibility wrapper over the shared vault-keyed proof context."""

    if proof_version != PROJECT_PROOF_VERSION:
        raise ValueError(f"unsupported project proof version: {proof_version}")
    return ProjectProofContext.from_vault_key(
        vault_key, key_epoch=key_epoch
    ).prove(path)


@dataclass(frozen=True)
class Observation:
    source: str
    file: str
    byte_offset: int
    nonce: str | None
    ts: datetime | None
    session_id: str | None = None
    adapter: str | None = None
    attestation: str | None = None
    measurement_protocol_version: str | None = None
    project_proof: ProjectProof | None = None
    # S2 call envelopes carry a trustworthy cwd-derived project scope that is
    # independent of the result's declared proof.  Keep it internal so a
    # rotated result proof remains a B9 loss signal while the invocation can
    # still bound an earlier window in the same host/session/project scope.
    host_scope_project_proof: ProjectProof | None = None
    key_epoch: str | None = None
    runtime_version: str | None = None
    verdict: str | None = None
    verdict_id_lists: Mapping[str, Sequence[int]] | None = None
    skipped: bool | None = None
    observable: bool | None = None
    evidence_available: bool | None = None
    progress_inserts: int = 0
    inserts: int = 0
    linked_cited_insert: bool = False
    cited_edge_activity: bool = False
    touches: int = 0
    embedded_conflict_reasons: tuple[str, ...] = ()
    legacy_project: bool = False
    hash_annotated: bool = False
    pre_nonce: bool = False
    raw_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"unknown source {self.source!r}")
        object.__setattr__(self, "ts", _utc(self.ts))
        if self.byte_offset < 0:
            raise ValueError("byte_offset must be non-negative")

    @property
    def obs_id(self) -> tuple[str, str, int]:
        return (self.source, self.file, self.byte_offset)


@dataclass(frozen=True)
class OutcomeEvidence:
    """Same-session KB/artifact evidence supplied by a pinned resolver."""

    nonce: str
    session_id: str
    observable: bool
    evidence_available: bool
    adapter: str | None = None
    project_fingerprint: str | None = None
    progress_inserts: int = 0
    inserts: int = 0
    linked_cited_insert: bool = False
    cited_edge_activity: bool = False
    touches: int = 0


@dataclass(frozen=True)
class LossMarker:
    reason: str
    source: str | None = None
    file: str | None = None
    byte_offset: int | None = None
    ts: datetime | None = None
    session_id: str | None = None
    adapter: str | None = None
    project_proof: ProjectProof | None = None
    host_scope_project_proof: ProjectProof | None = None
    nonce: str | None = None
    in_scope: bool = True
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", _utc(self.ts))


@dataclass(frozen=True)
class SourceHealth:
    source: str
    roots: tuple[str, ...] = ()
    files_seen: int = 0
    files_parsed: int = 0
    missing_files: int = 0
    unreadable_files: int = 0
    malformed_regions: int = 0
    unstable_files: int = 0

    @property
    def clean(self) -> bool:
        return not (
            self.missing_files
            or self.unreadable_files
            or self.malformed_regions
            or self.unstable_files
        )


@dataclass(frozen=True)
class SnapshotReceipt:
    source: str
    file: str
    first_sha256: str
    second_sha256: str
    snapshot_taken: datetime
    attempts: int = 1
    stable: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_taken", _utc(self.snapshot_taken))

    def qualifies(self, fresh_ts: datetime | None) -> bool:
        return bool(
            self.stable
            and fresh_ts is not None
            and self.first_sha256 == self.second_sha256
            and self.snapshot_taken >= fresh_ts + timedelta(seconds=FRESHNESS_SECONDS)
        )


@dataclass(frozen=True)
class CandidateCompletenessReceipt:
    source: str
    roots: tuple[str, ...]
    enumerated_files: tuple[str, ...]
    stable_file_hashes: tuple[tuple[str, str], ...]
    complete: bool


@dataclass(frozen=True)
class ReceiptLineage:
    nonce: str
    admitted: bool
    lineage_order_key: datetime
    measurement_protocol_version: str

    def __post_init__(self) -> None:
        order_key = _utc(self.lineage_order_key)
        if order_key is None:
            raise ValueError("lineage_order_key is required")
        object.__setattr__(self, "lineage_order_key", order_key)


@dataclass(frozen=True)
class GateRowCheck:
    obs_id: tuple[str, str, int]
    ts: datetime | None
    in_scope: bool
    id_lists_valid: bool
    missing_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", _utc(self.ts))


@dataclass(frozen=True)
class InvocationReceipt:
    nonce: str
    measurement_protocol_version: str
    observations: tuple[Observation, ...]
    disposition: str
    admitted: bool
    lineage_order_key: datetime
    fresh_ts: datetime | None
    session_id: str | None
    verdict: str | None
    outcome: str | None
    censored_reason: str | None = None
    loss_reasons: tuple[str, ...] = ()
    conflict_reasons: tuple[str, ...] = ()
    finalized: bool = False
    window_start: datetime | None = None
    window_end: datetime | None = None
    prefix_member: bool = False
    # Trusted prior coordinates used only for boundary reconstruction after a
    # protocol bump loses every current source. They are not current evidence
    # and never participate in disposition, outcome, freshness, or O1/O2.
    boundary_evidence: tuple[Observation, ...] = ()

    def __post_init__(self) -> None:
        if self.disposition not in DISPOSITIONS:
            raise ValueError(f"invalid disposition {self.disposition!r}")
        if self.outcome is not None and self.outcome not in OUTCOMES:
            raise ValueError(f"invalid outcome {self.outcome!r}")
        order_key = _utc(self.lineage_order_key)
        if order_key is None:
            raise ValueError("lineage_order_key is required")
        object.__setattr__(self, "lineage_order_key", order_key)
        object.__setattr__(self, "fresh_ts", _utc(self.fresh_ts))
        object.__setattr__(self, "window_start", _utc(self.window_start))
        object.__setattr__(self, "window_end", _utc(self.window_end))

    @property
    def identity(self) -> tuple[str, str]:
        return (self.nonce, self.measurement_protocol_version)

    @property
    def eligible(self) -> bool:
        return (
            self.finalized
            and self.disposition == "confirmatory"
            and self.outcome is not None
            and self.outcome != "CENSORED"
        )

    @property
    def in_d_min(self) -> bool:
        return self.finalized and self.disposition in {
            "confirmatory",
            "pilot",
            "loss_signal",
            "conflict",
        }

    @property
    def lineage(self) -> ReceiptLineage:
        return ReceiptLineage(
            nonce=self.nonce,
            admitted=self.admitted,
            lineage_order_key=self.lineage_order_key,
            measurement_protocol_version=self.measurement_protocol_version,
        )


@dataclass(frozen=True)
class CapturePin:
    node_id: int
    contract_sha256: str
    contract_version: str = CONTRACT_VERSION
    supersedes_node_id: int | None = SUPERSEDED_CAPTURE_NODE_ID


@dataclass(frozen=True)
class MeasurementManifest:
    contract_sha256: str
    ratification_node_ids: tuple[int, ...]
    implementation_commit: str
    measurement_protocol_version: str
    source_roots: Mapping[str, Sequence[str]]
    fixture_hashes: Mapping[str, str]
    composite_sha256: str

    def expected_composite(self) -> str:
        payload = {
            "contract_sha256": self.contract_sha256,
            "ratification_node_ids": list(self.ratification_node_ids),
            "implementation_commit": self.implementation_commit,
            "measurement_protocol_version": self.measurement_protocol_version,
            "source_roots": {
                key: sorted(os.fspath(v) for v in values)
                for key, values in sorted(self.source_roots.items())
            },
            "fixture_hashes": dict(sorted(self.fixture_hashes.items())),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return _sha256(encoded)


@dataclass(frozen=True)
class CanaryEvidence:
    host: str
    nonce: str
    tool_result_seen: bool
    gate_log_seen: bool
    host_record_seen: bool
    dual_source_joined: bool
    runtime_version: str
    measurement_protocol_version: str
    key_epoch: str
    project_proof: ProjectProof


@dataclass(frozen=True)
class MeasurementConfig:
    t0: datetime
    cap: datetime
    target_project_proof: ProjectProof
    key_epoch: str
    pinned_runtime_version: str
    measurement_protocol_version: str = MEASUREMENT_PROTOCOL_VERSION
    project_proof_version: str = PROJECT_PROOF_VERSION
    implementation_commit: str | None = None
    source_roots: Mapping[str, Sequence[str]] = field(default_factory=dict)
    require_fresh_snapshots: bool = True
    cap_reached: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "t0", _utc(self.t0))
        object.__setattr__(self, "cap", _utc(self.cap))
        if self.project_proof_version != PROJECT_PROOF_VERSION:
            raise ValueError(
                "measurement project proof version must match the frozen version"
            )
        if self.cap - self.t0 != timedelta(days=21):
            raise ValueError("measurement cap must be exactly 21 days after T0")


@dataclass(frozen=True)
class AuditState:
    config: MeasurementConfig
    receipts: tuple[InvocationReceipt, ...]
    loss_markers: tuple[LossMarker, ...]
    source_health: tuple[SourceHealth, ...]
    snapshots: tuple[SnapshotReceipt, ...]
    gate_rows: tuple[GateRowCheck, ...]
    candidate_completeness: tuple[CandidateCompletenessReceipt, ...] = ()
    hard_invalidations: tuple[str, ...] = ()
    measurement_taken_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "measurement_taken_at", _utc(self.measurement_taken_at)
        )

    @property
    def finalized_receipts(self) -> tuple[InvocationReceipt, ...]:
        return tuple(row for row in self.receipts if row.finalized)


@dataclass(frozen=True)
class OracleResult:
    invalidated: bool
    invalidation_reasons: tuple[str, ...]
    o1_pass: bool | None
    o2: str | None
    o2_reasons: tuple[str, ...]
    o3_pass: bool | None
    eligible_n: int
    d_min: int
    raw_label_counts: Mapping[str, int]
    clean_label_counts: Mapping[str, int]
    ambiguous_count: int
    ambiguous_rate: float | None
    disposition_counts: Mapping[str, int]
    marker_count: int
    source_health_clean: bool
    v1_green: bool
    verdict: str
    quality_summary: str


def _id_lists_valid(values: Mapping[str, Sequence[int]] | None) -> tuple[bool, tuple[str, ...]]:
    if not isinstance(values, Mapping):
        return False, REQUIRED_ID_LIST_FIELDS
    missing: list[str] = []
    for name in REQUIRED_ID_LIST_FIELDS:
        value = values.get(name)
        if not isinstance(value, (list, tuple)) or any(
            isinstance(item, bool) or not isinstance(item, int) for item in value
        ):
            missing.append(name)
    return not missing, tuple(missing)


def _in_window(ts: datetime | None, config: MeasurementConfig) -> bool:
    # Unplaceable evidence is conservatively in scope.
    return ts is None or config.t0 <= ts < config.cap


def _marker_is_proven_foreign(
    marker: LossMarker, config: MeasurementConfig
) -> bool:
    proof = marker.host_scope_project_proof or marker.project_proof
    return (
        compare_project_proofs(proof, config.target_project_proof)
        == PROJECT_FOREIGN
    )


def _observation_content_key(row: Observation) -> tuple[Any, ...]:
    """Return the coordinate-independent identity used for exact deduplication."""

    return (
        row.source,
        row.adapter,
        row.nonce,
        _iso(row.ts),
        row.session_id,
        row.attestation,
        row.measurement_protocol_version,
        tuple(sorted((row.project_proof or {}).items())),
        tuple(sorted((row.host_scope_project_proof or {}).items())),
        row.key_epoch,
        row.runtime_version,
        row.verdict,
        tuple(
            sorted(
                (name, tuple(value))
                for name, value in (row.verdict_id_lists or {}).items()
            )
        ),
        row.skipped,
        row.observable,
        row.evidence_available,
        row.progress_inserts,
        row.inserts,
        row.linked_cited_insert,
        row.cited_edge_activity,
        row.touches,
        row.embedded_conflict_reasons,
        row.legacy_project,
        row.hash_annotated,
        row.pre_nonce,
        row.raw_sha256,
    )


def _observation_evidence_join_key(row: Observation) -> tuple[Any, ...]:
    """Return source identity without coordinates or resolver-owned outputs."""

    return (
        row.source,
        row.adapter,
        row.nonce,
        _iso(row.ts),
        row.session_id,
        row.attestation,
        row.measurement_protocol_version,
        tuple(sorted((row.project_proof or {}).items())),
        tuple(sorted((row.host_scope_project_proof or {}).items())),
        row.key_epoch,
        row.runtime_version,
        row.verdict,
        tuple(
            sorted(
                (name, tuple(value))
                for name, value in (row.verdict_id_lists or {}).items()
            )
        ),
        row.skipped,
        row.embedded_conflict_reasons,
        row.legacy_project,
        row.hash_annotated,
        row.pre_nonce,
        row.raw_sha256,
    )


def _deduplicate_identical(observations: Sequence[Observation]) -> tuple[Observation, ...]:
    seen: set[tuple[Any, ...]] = set()
    result: list[Observation] = []
    for row in observations:
        key = _observation_content_key(row)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return tuple(result)


def _conflict_reasons(observations: Sequence[Observation]) -> tuple[str, ...]:
    reasons: set[str] = {
        reason
        for row in observations
        for reason in row.embedded_conflict_reasons
    }
    by_source: dict[str, list[Observation]] = defaultdict(list)
    for row in observations:
        by_source[row.source].append(row)
    for source, rows in by_source.items():
        if len(_deduplicate_identical(rows)) > 1:
            reasons.add(f"non_identical_duplicate:{source}")

    sessions = {row.session_id for row in observations if row.session_id}
    if len(sessions) > 1:
        reasons.add("session_mismatch")
    adapters = {row.adapter for row in observations if row.adapter}
    if len(adapters) > 1:
        reasons.add("host_adapter_mismatch")
    protocols = {
        row.measurement_protocol_version
        for row in observations
        if row.measurement_protocol_version
    }
    if len(protocols) > 1:
        reasons.add("protocol_mismatch")
    runtimes = {row.runtime_version for row in observations if row.runtime_version}
    if len(runtimes) > 1:
        reasons.add("runtime_version_mismatch")
    attestations = {row.attestation for row in observations if row.attestation}
    if len(attestations) > 1:
        reasons.add("attestation_mismatch")
    proofs = [row.project_proof for row in observations if row.project_proof is not None]
    proof_versions = {str(row.get("version") or "") for row in proofs}
    proof_epochs = {str(row.get("key_epoch") or "") for row in proofs}
    proof_key_ids = {str(row.get("key_id") or "") for row in proofs}
    proof_fingerprints = {str(row.get("fingerprint") or "") for row in proofs}
    # Rotation is a loss signal, never a conflict.  Two different fingerprints
    # under the *same* valid generation are genuinely conflicting project data.
    if (
        len(proof_versions) == 1
        and len(proof_epochs) == 1
        and len(proof_key_ids) == 1
        and "" not in proof_key_ids
        and len(proof_fingerprints) > 1
        and proof_versions == {PROJECT_PROOF_VERSION}
    ):
        reasons.add("project_proof_mismatch")
    verdicts = {row.verdict for row in observations if row.verdict is not None}
    if len(verdicts) > 1:
        reasons.add("verdict_mismatch")
    id_lists = {
        json.dumps(
            row.verdict_id_lists,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for row in observations
        if row.verdict_id_lists is not None
    }
    if len(id_lists) > 1:
        reasons.add("verdict_id_lists_mismatch")
    skipped_values = {row.skipped for row in observations if row.skipped is not None}
    if len(skipped_values) > 1:
        reasons.add("skipped_mismatch")
    observability = {
        row.observable for row in observations if row.observable is not None
    }
    if len(observability) > 1:
        reasons.add("observability_mismatch")
    timestamps = [row.ts for row in observations if row.ts is not None]
    if timestamps and max(timestamps) - min(timestamps) > timedelta(
        seconds=JOIN_TOLERANCE_SECONDS
    ):
        reasons.add("timestamp_mismatch")
    return tuple(sorted(reasons))


def _admission_conflict_reasons(
    observations: tuple[Observation, ...],
) -> tuple[str, ...]:
    """Cross-run admission-guard conflicts on evidence-only identity.

    Prior lineage rows carry the previous run's resolver outputs while current
    rows re-derive them, so the seven resolver-owned fields are neutralized
    before comparison: the guard joins on ``_observation_evidence_join_key``
    identity, and a rerun whose KB events sit differently relative to a
    legitimately different window is honest evolution, not tampering.
    ``measurement_protocol_version`` is neutralized for the same reason — a
    protocol bump is decision 4432's succession and never conflicts on its
    own (ruling 4562 item 2) — rather than partitioned on, because a
    partition would hand any relabelled row a private comparison group and
    reopen the 4546 false-green class item 3 closes. Tampering that hides
    behind a relabel still moves an evidence field this comparison keeps.
    """

    neutralized = tuple(
        replace(
            row,
            measurement_protocol_version=None,
            observable=True,
            evidence_available=True,
            progress_inserts=0,
            inserts=0,
            linked_cited_insert=False,
            cited_edge_activity=False,
            touches=0,
        )
        for row in observations
    )
    return _conflict_reasons(neutralized)


def _loss_reasons(
    observations: Sequence[Observation], config: MeasurementConfig
) -> tuple[str, ...]:
    reasons: set[str] = set()
    sources = {row.source for row in observations}
    if SOURCE_GATE not in sources:
        reasons.add("host_only")
    if SOURCE_HOST not in sources:
        reasons.add("gate_only")
    for row in observations:
        if row.attestation is None:
            reasons.add("attestation_missing")
        if row.project_proof is None and not row.legacy_project:
            reasons.add("project_proof_missing")
        if row.measurement_protocol_version is None:
            reasons.add("version_missing")
        if row.key_epoch is None:
            reasons.add("project_proof_missing")
        elif not hmac.compare_digest(row.key_epoch, config.key_epoch):
            reasons.add("key_epoch_mismatch")
        proof_status = compare_project_proofs(
            row.project_proof, config.target_project_proof
        )
        if proof_status == PROJECT_KEY_EPOCH_MISMATCH:
            reasons.add("key_epoch_mismatch")
        elif proof_status == PROJECT_PROOF_MISSING and not row.legacy_project:
            reasons.add("project_proof_missing")
        elif proof_status == PROJECT_PROOF_INVALID:
            reasons.add("project_proof_invalid")
    return tuple(sorted(reasons))


def _is_proven_foreign(
    observations: Sequence[Observation], config: MeasurementConfig
) -> bool:
    proofs = [
        proof
        for row in observations
        for proof in (
            row.project_proof,
            (
                row.host_scope_project_proof
                if row.source == SOURCE_HOST
                else None
            ),
        )
        if proof is not None
    ]
    if not proofs:
        return False
    return all(
        compare_project_proofs(proof, config.target_project_proof)
        == PROJECT_FOREIGN
        for proof in proofs
    )


def _has_proven_target(
    observations: Sequence[Observation], config: MeasurementConfig
) -> bool:
    return any(
        compare_project_proofs(proof, config.target_project_proof)
        == PROJECT_MATCH
        for row in observations
        for proof in (
            row.project_proof,
            (
                row.host_scope_project_proof
                if row.source == SOURCE_HOST
                else None
            ),
        )
        if proof is not None
    )


def _disposition(
    observations: Sequence[Observation], config: MeasurementConfig
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    # Frozen first-match order from contract v2.6.
    if _is_proven_foreign(observations, config):
        return "foreign_project", (), ()
    conflicts = _conflict_reasons(observations)
    if conflicts:
        return "conflict", (), conflicts
    losses = _loss_reasons(observations, config)
    if losses:
        return "loss_signal", losses, ()
    gate = next((row for row in observations if row.source == SOURCE_GATE), None)
    if gate is not None and gate.skipped is True:
        return "skipped", (), ()
    if any(
        row.pre_nonce
        or row.hash_annotated
        or row.legacy_project
        or row.runtime_version != config.pinned_runtime_version
        or row.measurement_protocol_version != config.measurement_protocol_version
        for row in observations
    ):
        return "pilot", (), ()
    return "confirmatory", (), ()


def _classify_outcome(observations: Sequence[Observation]) -> tuple[str, str | None]:
    gate = next((row for row in observations if row.source == SOURCE_GATE), None)
    if gate is None:
        return "CENSORED", "instrument_unavailable"
    if any(row.observable is not True for row in observations):
        return "CENSORED", "instrument_unavailable"
    if any(row.evidence_available is not True for row in observations):
        return "CENSORED", "evidence_unavailable"

    verdict = gate.verdict
    progress = max((row.progress_inserts for row in observations), default=0)
    inserts = max((row.inserts for row in observations), default=0)
    linked = any(row.linked_cited_insert for row in observations)
    cited_activity = any(row.cited_edge_activity for row in observations)
    touches = max((row.touches for row in observations), default=0)

    if verdict == "PROCEED":
        return ("ACCEPTED", None) if progress > 0 else ("AMBIGUOUS", None)
    if verdict == "MODIFY":
        if linked:
            return "ACCEPTED", None
        if inserts > 0:
            return "OVERRIDDEN", None
        return ("OVERRIDDEN", None) if touches > 0 else ("AMBIGUOUS", None)
    if verdict == "DO_NOT_PROCEED":
        return ("OVERRIDDEN", None) if progress > 0 else ("ACCEPTED", None)
    if verdict == "NEEDS_HUMAN_JUDGMENT":
        return (
            ("ACCEPTED", None)
            if inserts > 0 or cited_activity
            else ("UNRESOLVED", None)
        )
    return "AMBIGUOUS", None


def _snapshot_qualifies(
    observations: Sequence[Observation],
    fresh_ts: datetime | None,
    snapshots: Sequence[SnapshotReceipt],
    *,
    required: bool,
) -> bool:
    if not required:
        return True
    by_coordinate = {(row.source, row.file): row for row in snapshots}
    return bool(observations) and all(
        (row.source, row.file) in by_coordinate
        and by_coordinate[(row.source, row.file)].qualifies(fresh_ts)
        for row in observations
    )


def _candidate_inventory_qualifies(
    source_health: Sequence[SourceHealth],
    candidate_completeness: Sequence[CandidateCompletenessReceipt],
    *,
    loss_markers: Sequence[LossMarker] = (),
    cutoff: tuple[datetime, str] | None = None,
    prefix_nonces: frozenset[str] = frozenset(),
    known_nonces: frozenset[str] = frozenset(),
    source_cutoffs: Mapping[str, datetime] = {},
    config: MeasurementConfig | None = None,
) -> bool:
    """Require one complete, clean inventory receipt from each source.

    Per-observation hashes alone cannot prove that a competing candidate was
    absent before finalization. The full double-enumerated S1/S2 inventories
    must also be symmetric and clean before a receipt becomes immutable.
    """

    health = Counter(row.source for row in source_health)
    completeness = Counter(row.source for row in candidate_completeness)
    if not (
        all(health[source] == 1 for source in SOURCES)
        and set(health) == set(SOURCES)
        and all(completeness[source] == 1 for source in SOURCES)
        and set(completeness) == set(SOURCES)
    ):
        return False

    offsets: dict[str, Counter[str]] = defaultdict(Counter)
    if cutoff is not None:
        reason_field = {
            "schema_invalid": "malformed_regions",
            "missing_daily_file": "missing_files",
            "unreadable_file": "unreadable_files",
            "final_hash_unreadable": "unreadable_files",
            "snapshot_changed": "unstable_files",
            "final_hash_changed": "unstable_files",
        }
        for marker in loss_markers:
            field = reason_field.get(marker.reason)
            post_prefix = False
            if marker.ts is not None:
                if marker.nonce and marker.nonce in prefix_nonces:
                    post_prefix = False
                elif marker.nonce and marker.nonce in known_nonces:
                    post_prefix = (marker.ts, marker.nonce) > cutoff
                elif marker.source in source_cutoffs:
                    post_prefix = marker.ts > source_cutoffs[marker.source]
            if (
                field is None
                or marker.source not in SOURCES
                or not marker.in_scope
                or marker.ts is None
                or not post_prefix
                or (
                    config is not None
                    and _marker_is_proven_foreign(marker, config)
                )
            ):
                continue
            offsets[marker.source][field] += 1

    def clean(row: SourceHealth) -> bool:
        return not any(
            max(0, getattr(row, field) - offsets[row.source][field])
            for field in (
                "missing_files",
                "unreadable_files",
                "malformed_regions",
                "unstable_files",
            )
        )

    health_by_source = {row.source: row for row in source_health}
    return bool(
        all(clean(row) for row in source_health)
        and all(
            row.complete
            or (
                not health_by_source[row.source].clean
                and clean(health_by_source[row.source])
            )
            for row in candidate_completeness
        )
    )


def _with_prefix_membership(
    receipts: Sequence[InvocationReceipt],
    config: MeasurementConfig,
) -> tuple[list[InvocationReceipt], tuple[datetime, str], frozenset[str]]:
    admitted_order = sorted(
        (
            row
            for row in receipts
            if row.admitted
            and row.measurement_protocol_version
            == config.measurement_protocol_version
        ),
        key=lambda row: (row.lineage_order_key, row.nonce),
    )
    eligible_seen = 0
    t1_position: int | None = None
    cutoff = (config.cap, "")
    for position, row in enumerate(admitted_order):
        if row.eligible:
            eligible_seen += 1
            if eligible_seen == V1_ELIGIBLE_TARGET:
                t1_position = position
                cutoff = (row.lineage_order_key, row.nonce)
                break
    prefix_rows = (
        admitted_order
        if t1_position is None
        else admitted_order[: t1_position + 1]
    )
    prefix_nonces = frozenset(row.nonce for row in prefix_rows)
    return (
        [
            replace(
                row,
                prefix_member=row.admitted and row.nonce in prefix_nonces,
            )
            for row in receipts
        ],
        cutoff,
        prefix_nonces,
    )


def _prefix_source_cutoffs(
    receipts: Sequence[InvocationReceipt],
    prefix_nonces: frozenset[str],
    config: MeasurementConfig,
) -> dict[str, datetime]:
    cutoffs: dict[str, datetime] = {}
    for receipt in receipts:
        if (
            receipt.nonce not in prefix_nonces
            or receipt.measurement_protocol_version
            != config.measurement_protocol_version
            or receipt.disposition == "foreign_project"
        ):
            continue
        for observation in receipt.observations:
            if observation.source not in SOURCES or observation.ts is None:
                continue
            existing = cutoffs.get(observation.source)
            if existing is None or observation.ts > existing:
                cutoffs[observation.source] = observation.ts
    return cutoffs


def _lineage_by_nonce(
    prior_receipts: Iterable[InvocationReceipt | ReceiptLineage],
) -> dict[str, ReceiptLineage]:
    result: dict[str, ReceiptLineage] = {}
    for row in prior_receipts:
        lineage = row.lineage if isinstance(row, InvocationReceipt) else row
        existing = result.get(lineage.nonce)
        if existing is None:
            result[lineage.nonce] = lineage
        else:
            result[lineage.nonce] = ReceiptLineage(
                nonce=lineage.nonce,
                admitted=existing.admitted or lineage.admitted,
                lineage_order_key=min(
                    existing.lineage_order_key, lineage.lineage_order_key
                ),
                measurement_protocol_version=lineage.measurement_protocol_version,
            )
    return result


def _boundary_observations(
    receipt: InvocationReceipt,
) -> tuple[Observation, ...]:
    """Return trustworthy host coordinates, with explicit S1 as fallback.

    S1's classifier ``backend`` is not a host namespace. The parser leaves its
    adapter unset unless the row carries an explicit structural host_adapter.
    Whenever S2 exists it is authoritative and S1 cannot create cross-host
    overlap through a shared classifier backend.
    """

    evidence_rows = receipt.observations + receipt.boundary_evidence
    host_rows = tuple(
        row
        for row in evidence_rows
        if row.source == SOURCE_HOST
        and row.adapter
        and row.session_id
        and (row.host_scope_project_proof or row.project_proof) is not None
    )
    gate_rows = tuple(
        row
        for row in evidence_rows
        if row.source == SOURCE_GATE
        # Gate parser populates this only from explicit structural
        # ``host_adapter``. Classifier backends are not host namespaces.
        and row.adapter in {"codex", "claude", "cursor"}
        and row.session_id
        and row.project_proof is not None
    )
    # S2 is authoritative per exact identity, not per receipt. Retain an S1
    # coordinate when a conflicted nonce has S2 only for a different session.
    gate_only_coordinates = tuple(
        gate_row
        for gate_row in gate_rows
        if not any(
            host_row.adapter == gate_row.adapter
            and host_row.session_id == gate_row.session_id
            and compare_project_proofs(
                _boundary_project_proof(host_row), gate_row.project_proof,
            )
            == PROJECT_MATCH
            for host_row in host_rows
        )
    )
    return host_rows + gate_only_coordinates


def _boundary_project_proof(row: Observation) -> ProjectProof | None:
    return row.host_scope_project_proof or row.project_proof


def _matching_boundary_coordinate(
    left: InvocationReceipt, right: InvocationReceipt
) -> tuple[tuple[datetime, str, int, str], Observation] | None:
    """Return the next exact adapter/project/session coordinate in ``right``.

    A conflicted receipt can carry records from several sessions. Its
    receipt-wide earliest timestamp is therefore not a safe boundary for any
    one session. Compare the matching observation coordinates themselves and
    return only the earliest coordinate that is strictly after ``left`` in the
    same namespaced stream.
    """

    candidates: list[
        tuple[tuple[datetime, str, int, str], Observation]
    ] = []
    for left_row in _boundary_observations(left):
        if left_row.ts is None:
            continue
        left_key = (
            left_row.ts,
            left_row.file,
            left_row.byte_offset,
            left.nonce,
        )
        for right_row in _boundary_observations(right):
            if (
                right_row.ts is not None
                and right_row.adapter == left_row.adapter
                and right_row.session_id == left_row.session_id
                and compare_project_proofs(
                    _boundary_project_proof(right_row),
                    _boundary_project_proof(left_row),
                )
                == PROJECT_MATCH
            ):
                right_key = (
                    right_row.ts,
                    right_row.file,
                    right_row.byte_offset,
                    right.nonce,
                )
                if right_key > left_key:
                    candidates.append((right_key, right_row))
    return min(candidates, key=lambda item: item[0], default=None)


def _boundary_coordinate_is_gate_only(
    receipt: InvocationReceipt, coordinate: Observation
) -> bool:
    matching = tuple(
        row
        for row in receipt.observations + receipt.boundary_evidence
        if row.adapter == coordinate.adapter
        and row.session_id == coordinate.session_id
        and compare_project_proofs(
            _boundary_project_proof(row),
            _boundary_project_proof(coordinate),
        )
        == PROJECT_MATCH
    )
    return bool(
        any(row.source == SOURCE_GATE for row in matching)
        and not any(row.source == SOURCE_HOST for row in matching)
    )


def _marker_matches_boundary(
    receipt: InvocationReceipt, marker: LossMarker
) -> bool:
    marker_proof = marker.host_scope_project_proof or marker.project_proof
    if not (
        marker.adapter
        and marker.session_id
        and marker_proof is not None
    ):
        return False
    return any(
        row.adapter == marker.adapter
        and row.session_id == marker.session_id
        and compare_project_proofs(marker_proof, _boundary_project_proof(row))
        == PROJECT_MATCH
        for row in _boundary_observations(receipt)
    )


def _same_session_boundary(
    receipts: Sequence[InvocationReceipt], index: int
) -> tuple[InvocationReceipt, datetime, Observation] | None:
    current = receipts[index]
    if not current.session_id:
        return None
    candidates: list[
        tuple[tuple[datetime, str, int, str], InvocationReceipt]
    ] = []
    for other in receipts:
        if (
            other.identity == current.identity
            or other.disposition == "foreign_project"
        ):
            continue
        match = _matching_boundary_coordinate(current, other)
        if match is not None:
            coordinate, observation = match
            candidates.append((coordinate, other, observation))
    if not candidates:
        return None
    coordinate, receipt, observation = min(candidates, key=lambda item: item[0])
    return receipt, coordinate[0], observation


def _session_sequence_key(receipt: InvocationReceipt) -> tuple[datetime, str, int, str]:
    coordinates = [
        (row.ts, row.file, row.byte_offset, receipt.nonce)
        for row in receipt.observations
        if row.ts is not None
    ]
    if coordinates:
        return min(coordinates)
    return (
        receipt.fresh_ts or receipt.lineage_order_key,
        "",
        -1,
        receipt.nonce,
    )


def fold_observations(
    observations: Iterable[Observation],
    config: MeasurementConfig,
    *,
    prior_receipts: Iterable[InvocationReceipt | ReceiptLineage] = (),
    source_health: Iterable[SourceHealth] = (),
    snapshots: Iterable[SnapshotReceipt] = (),
    loss_markers: Iterable[LossMarker] = (),
    gate_rows: Iterable[GateRowCheck] = (),
    candidate_completeness: Iterable[CandidateCompletenessReceipt] = (),
    hard_invalidations: Iterable[str] = (),
) -> AuditState:
    """Fold observations into one immutable receipt per nonce/protocol.

    Nonce-less or timestamp-less observations become conservative
    ``identity_missing`` markers and never enter ``D_min``.  Previously admitted
    lineage is retained even when a source disappears.
    """

    rows = tuple(observations)
    prior_rows = tuple(prior_receipts)
    health_rows = tuple(source_health)
    completeness_rows = tuple(candidate_completeness)
    marker_rows = list(loss_markers)
    invalidation_rows = set(hard_invalidations)
    valid_rows: list[Observation] = []
    checks = list(gate_rows)
    for row in rows:
        if row.source == SOURCE_GATE and not any(
            check.obs_id == row.obs_id for check in checks
        ):
            valid, missing = _id_lists_valid(row.verdict_id_lists)
            checks.append(
                GateRowCheck(
                    obs_id=row.obs_id,
                    ts=row.ts,
                    in_scope=_observation_in_scope(row, config),
                    id_lists_valid=valid,
                    missing_fields=missing,
                )
            )
        if not row.nonce or row.ts is None:
            marker_rows.append(
                LossMarker(
                    reason="identity_missing",
                    source=row.source,
                    file=row.file,
                    byte_offset=row.byte_offset,
                    ts=row.ts,
                    session_id=row.session_id,
                    adapter=row.adapter,
                    project_proof=row.project_proof,
                    host_scope_project_proof=row.host_scope_project_proof,
                    nonce=row.nonce,
                    in_scope=_observation_in_scope(row, config),
                )
            )
            continue
        valid_rows.append(row)

    grouped: dict[str, dict[str | None, list[Observation]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in valid_rows:
        grouped[row.nonce][row.measurement_protocol_version].append(row)
    prior = _lineage_by_nonce(prior_rows)
    snapshot_rows = tuple(snapshots)
    all_finalized_rows = tuple(
        row
        for row in prior_rows
        if isinstance(row, InvocationReceipt)
        and row.finalized
    )
    all_finalized_groups: dict[
        tuple[str, str], list[InvocationReceipt]
    ] = defaultdict(list)
    for row in all_finalized_rows:
        all_finalized_groups[row.identity].append(row)
    for duplicate_rows in all_finalized_groups.values():
        if len(duplicate_rows) <= 1:
            continue
        invalidation_rows.add("duplicate_receipt_identity")
        if any(row != duplicate_rows[0] for row in duplicate_rows[1:]):
            invalidation_rows.add("non_identical_duplicate_receipt_identity")

    immutable_rows = tuple(
        row
        for row in all_finalized_rows
        if row.measurement_protocol_version == config.measurement_protocol_version
    )
    immutable_groups: dict[
        tuple[str, str], list[InvocationReceipt]
    ] = defaultdict(list)
    for row in immutable_rows:
        immutable_groups[row.identity].append(row)
    immutable = {
        identity: identity_rows[0]
        for identity, identity_rows in immutable_groups.items()
    }
    finalized_prior_by_nonce: dict[str, list[InvocationReceipt]] = defaultdict(list)
    for prior_receipt in all_finalized_rows:
        finalized_prior_by_nonce[prior_receipt.nonce].append(prior_receipt)
    full_prior_by_nonce: dict[str, list[InvocationReceipt]] = defaultdict(list)
    for prior_receipt in prior_rows:
        if isinstance(prior_receipt, InvocationReceipt):
            full_prior_by_nonce[prior_receipt.nonce].append(prior_receipt)

    # Admission is any-source monotone even before finalization. If only part
    # of a provisional receipt survives reprocessing, retain its lineage and
    # make each previously observed source deletion explicit. Total deletion
    # is handled by the synthesis loop below.
    for nonce, authority_rows in full_prior_by_nonce.items():
        provisional_rows = tuple(
            row
            for row in authority_rows
            if not row.finalized
            and row.admitted
            and row.disposition != "foreign_project"
        )
        if not provisional_rows or nonce not in grouped:
            continue
        prior_sources = {
            observation.source
            for row in provisional_rows
            for observation in row.observations + row.boundary_evidence
            if observation.source in SOURCES
        }
        current_sources = {
            observation.source
            for generation_rows in grouped[nonce].values()
            for observation in generation_rows
        }
        session_ids = {
            row.session_id for row in provisional_rows if row.session_id
        }
        session_id = next(iter(session_ids)) if len(session_ids) == 1 else None
        # Admission inheritance carries the same tamper detection as the
        # finalized path below. Preserving lineage must never let evidence that
        # changed content or moved outside [T0, cap) ride a prior admission into
        # a clean run: an honest re-read is evidence-identical and collapses, so
        # this fires only when the evidence backing the admission actually moved.
        # Prior coordinates live in ``boundary_evidence`` once synthesis has
        # rebuilt a total loss, so the guard reads both tuples exactly like
        # ``prior_sources`` above (fact 4546 / ruling 4562 item 3).
        provisional_candidates = tuple(
            observation
            for row in provisional_rows
            for observation in row.observations + row.boundary_evidence
        ) + tuple(
            observation
            for generation_rows in grouped[nonce].values()
            for observation in generation_rows
        )
        candidate_conflicts = _admission_conflict_reasons(provisional_candidates)
        if candidate_conflicts and not _is_proven_foreign(
            provisional_candidates, config,
        ):
            marker_rows.append(
                LossMarker(
                    reason="admitted_candidate_conflict",
                    session_id=session_id,
                    nonce=nonce,
                    ts=min(
                        (
                            observation.ts
                            for generation_rows in grouped[nonce].values()
                            for observation in generation_rows
                            if observation.ts is not None
                        ),
                        default=None,
                    ),
                    in_scope=True,
                    detail="|".join(candidate_conflicts),
                )
            )
        for missing_source in sorted(prior_sources - current_sources):
            marker_rows.append(
                LossMarker(
                    reason="admitted_source_deleted",
                    source=missing_source,
                    session_id=session_id,
                    nonce=nonce,
                    ts=prior[nonce].lineage_order_key,
                    in_scope=True,
                )
            )
    for (nonce, protocol), receipt in immutable.items():
        current_candidates = [
            row
            for generation_rows in grouped.get(nonce, {}).values()
            for row in generation_rows
        ]
        finalized_candidates = (
            tuple(receipt.observations)
            + tuple(receipt.boundary_evidence)
            + tuple(current_candidates)
        )
        candidate_conflicts = _admission_conflict_reasons(finalized_candidates)
        if candidate_conflicts and not _is_proven_foreign(
            finalized_candidates, config,
        ):
            marker_rows.append(
                LossMarker(
                    reason="finalized_candidate_conflict",
                    session_id=receipt.session_id,
                    nonce=nonce,
                    ts=min(
                        (
                            row.ts
                            for row in current_candidates
                            if row.ts is not None
                        ),
                        default=None,
                    ),
                    in_scope=True,
                    detail="|".join(candidate_conflicts),
                )
            )
        if receipt.disposition == "foreign_project":
            # B12: a lone proven-foreign receipt exits target-local deletion
            # accounting, but a newly visible target/mixed candidate above is
            # still a non-identical same-nonce loss and cannot be hidden by
            # immutability.
            continue
        present_sources = {
            row.source
            for row in grouped.get(nonce, {}).get(protocol, ())
        }
        for missing_source in sorted(set(SOURCES) - present_sources):
            marker_rows.append(
                LossMarker(
                    reason="finalized_source_deleted",
                    source=missing_source,
                    session_id=receipt.session_id,
                    nonce=nonce,
                    in_scope=True,
                )
            )
    # Preserve duplicates in the returned state so integrity checks and report
    # consumers cannot mistake dict collapse for canonical prior content.
    receipts: list[InvocationReceipt] = list(immutable_rows)
    for nonce, generations in grouped.items():
        generation = config.measurement_protocol_version
        if (nonce, generation) in immutable:
            # A finalized receipt never changes within one protocol generation.
            continue
        # Candidate membership is nonce-wide. Older/missing protocol rows can
        # conflict with the current observation set, while the immutable
        # receipt identity remains the pinned current generation.
        group = [
            row
            for protocol in sorted(
                generations,
                key=lambda item: (item is None, item or ""),
            )
            for row in generations[protocol]
        ]
        current_timestamps = [row.ts for row in group if row.ts is not None]
        fresh_ts = min(current_timestamps) if current_timestamps else None
        ancestor = prior.get(nonce)
        currently_admitted = any(_in_window(ts, config) for ts in current_timestamps)
        admitted = currently_admitted or bool(ancestor and ancestor.admitted)
        if ancestor is not None:
            order_key = min(ancestor.lineage_order_key, *current_timestamps)
        else:
            order_key = min(current_timestamps)
        disposition, losses, conflicts = _disposition(group, config)
        prior_authority = finalized_prior_by_nonce.get(nonce, ())
        prior_has_target_authority = any(
            row.disposition != "foreign_project" for row in prior_authority
        )
        prior_is_foreign_only = bool(prior_authority) and all(
            row.disposition == "foreign_project" for row in prior_authority
        )
        cross_generation_scope_conflict = (
            prior_has_target_authority and _is_proven_foreign(group, config)
        ) or (
            prior_is_foreign_only and _has_proven_target(group, config)
        )
        if cross_generation_scope_conflict:
            disposition = "conflict"
            losses = ()
            conflicts = tuple(
                sorted({*conflicts, "cross_generation_project_scope_mismatch"})
            )
        session_ids = {row.session_id for row in group if row.session_id}
        session_id = next(iter(session_ids)) if len(session_ids) == 1 else None
        gate = next((row for row in group if row.source == SOURCE_GATE), None)
        outcome, censored = _classify_outcome(group)
        if disposition in {"skipped", "foreign_project"}:
            outcome, censored = None, None
        finalized = _snapshot_qualifies(
            group,
            fresh_ts,
            snapshot_rows,
            required=config.require_fresh_snapshots,
        )
        receipts.append(
            InvocationReceipt(
                nonce=nonce,
                measurement_protocol_version=config.measurement_protocol_version,
                observations=_deduplicate_identical(group),
                disposition=disposition,
                admitted=admitted,
                lineage_order_key=order_key,
                fresh_ts=fresh_ts,
                session_id=session_id,
                verdict=gate.verdict if gate else None,
                outcome=outcome,
                censored_reason=censored,
                loss_reasons=losses,
                conflict_reasons=conflicts,
                finalized=finalized,
                window_start=fresh_ts,
                window_end=(
                    fresh_ts + timedelta(seconds=WINDOW_SECONDS)
                    if fresh_ts is not None
                    else None
                ),
            )
        )

    # M5: admitted lineage survives reprocessing even when every current source
    # observation has disappeared. A full prior receipt supplies structural
    # boundary authority, but a provisional prior never makes its old outcome
    # immutable: it becomes an unfinalized censored loss receipt that can be
    # re-evaluated when evidence returns.
    for nonce, ancestor in prior.items():
        if (
            not ancestor.admitted
            or nonce in grouped
            or (nonce, config.measurement_protocol_version) in immutable
        ):
            continue
        authority_rows = full_prior_by_nonce.get(nonce, [])
        if not authority_rows:
            # Bare ReceiptLineage proves no target-local disposition. Keep
            # deletion visible without inflating D_min or claiming foreignness.
            marker_rows.append(
                LossMarker(
                    reason="finalized_source_deleted",
                    nonce=nonce,
                    in_scope=True,
                )
            )
            invalidation_rows.add("admitted_lineage_authority_missing")
            continue
        by_protocol: dict[str, list[InvocationReceipt]] = defaultdict(list)
        for row in authority_rows:
            by_protocol[row.measurement_protocol_version].append(row)
        if any(len(rows) != 1 for rows in by_protocol.values()):
            # Never pick arbitrary authority when one generation has multiple
            # full receipts. Finalized duplicates were hard-invalidated above;
            # provisional duplicates fail closed here as well.
            invalidation_rows.add("ambiguous_prior_generation_authority")
            continue
        protocols = tuple(by_protocol)
        if len(protocols) == 1:
            authority = by_protocol[protocols[0]][0]
        else:
            parsed_protocols: dict[str, tuple[int, int, int]] = {}
            for protocol in protocols:
                match = re.fullmatch(
                    r"outcome-v(\d+)\.(\d+)\.(\d+)", protocol
                )
                if match is not None:
                    parsed_protocols[protocol] = tuple(
                        int(part) for part in match.groups()
                    )
            if len(parsed_protocols) != len(protocols):
                invalidation_rows.add("ambiguous_prior_generation_authority")
                continue
            latest_key = max(parsed_protocols.values())
            latest = [
                protocol
                for protocol, generation_key in parsed_protocols.items()
                if generation_key == latest_key
            ]
            if len(latest) != 1:
                invalidation_rows.add("ambiguous_prior_generation_authority")
                continue
            authority = by_protocol[latest[0]][0]
        non_target = authority.disposition in {"skipped", "foreign_project"}
        if authority.disposition != "foreign_project":
            deletion_reason = (
                "finalized_source_deleted"
                if authority.finalized
                else "admitted_source_deleted"
            )
            for source in SOURCES:
                marker_rows.append(
                    LossMarker(
                        reason=deletion_reason,
                        source=source,
                        session_id=authority.session_id,
                        nonce=nonce,
                        ts=authority.fresh_ts or ancestor.lineage_order_key,
                        in_scope=True,
                    )
                )
        receipts.append(
            InvocationReceipt(
                nonce=nonce,
                measurement_protocol_version=config.measurement_protocol_version,
                observations=(),
                disposition=(authority.disposition if non_target else "loss_signal"),
                admitted=True,
                lineage_order_key=ancestor.lineage_order_key,
                fresh_ts=None,
                session_id=authority.session_id,
                verdict=None,
                outcome=None if non_target else "CENSORED",
                censored_reason=(None if non_target else "instrument_unavailable"),
                loss_reasons=(() if non_target else ("gate_only", "host_only")),
                finalized=authority.finalized,
                window_start=None,
                window_end=None,
                boundary_evidence=(
                    authority.boundary_evidence or authority.observations
                ),
            )
        )

    receipts.sort(key=lambda row: (row.fresh_ts or row.lineage_order_key, row.nonce))
    bounded: list[InvocationReceipt] = []
    immutable_ids = set(immutable)
    for index, receipt in enumerate(receipts):
        if receipt.identity in immutable_ids:
            bounded.append(receipt)
            continue
        boundary_match = _same_session_boundary(receipts, index)
        end = receipt.window_end
        outcome = receipt.outcome
        reason = receipt.censored_reason
        if boundary_match is not None:
            boundary, boundary_ts, boundary_observation = boundary_match
            end = min(end, boundary_ts) if end is not None else boundary_ts
            if _boundary_coordinate_is_gate_only(
                boundary, boundary_observation,
            ):
                outcome = "CENSORED"
                reason = "boundary_uncertain"
        if receipt.session_id and receipt.window_start and end:
            if any(
                marker.in_scope
                and _marker_matches_boundary(receipt, marker)
                and (
                    marker.ts is None
                    or receipt.window_start <= marker.ts <= end
                )
                for marker in marker_rows
            ):
                outcome = "CENSORED"
                reason = "boundary_uncertain"
        bounded.append(
            replace(
                receipt,
                window_end=end,
                outcome=outcome,
                censored_reason=reason,
            )
        )

    # First compute a provisional T1 from snapshot-qualified outcomes. This is
    # needed to decide whether a placeable source defect lies strictly after
    # the closed measured prefix. Unplaceable or prefix-local defects still
    # prevent any new receipt from becoming immutable.
    bounded, provisional_cutoff, provisional_nonces = _with_prefix_membership(
        bounded, config,
    )
    inventory_globally_clean = _candidate_inventory_qualifies(
        health_rows, completeness_rows,
    )
    inventory_prefix_clean = _candidate_inventory_qualifies(
        health_rows,
        completeness_rows,
        loss_markers=tuple(marker_rows),
        cutoff=provisional_cutoff,
        prefix_nonces=provisional_nonces,
        known_nonces=frozenset(row.nonce for row in bounded),
        source_cutoffs=(
            _prefix_source_cutoffs(bounded, provisional_nonces, config)
            if provisional_cutoff != (config.cap, "")
            else {}
        ),
        config=config,
    )
    snapshot_candidate_ids = {
        (nonce, config.measurement_protocol_version)
        for nonce in grouped
        if (nonce, config.measurement_protocol_version) not in immutable
    }
    if not inventory_globally_clean:
        allowed = provisional_nonces if inventory_prefix_clean else frozenset()
        bounded = [
            replace(row, finalized=False)
            if row.identity in snapshot_candidate_ids and row.nonce not in allowed
            else row
            for row in bounded
        ]
        if not inventory_prefix_clean:
            bounded = [
                replace(row, finalized=False)
                if row.identity in snapshot_candidate_ids
                else row
                for row in bounded
            ]

    # Prefix membership is audit-derived from the complete current inventory,
    # not immutable receipt content. Recompute it after finalization and even
    # for immutable rows; all other immutable fields remain unchanged.
    bounded, _final_cutoff, _final_nonces = _with_prefix_membership(
        bounded, config,
    )

    return AuditState(
        config=config,
        receipts=tuple(bounded),
        loss_markers=tuple(marker_rows),
        source_health=health_rows,
        snapshots=snapshot_rows,
        gate_rows=tuple(checks),
        candidate_completeness=completeness_rows,
        hard_invalidations=tuple(sorted(invalidation_rows)),
    )


def verify_canary_evidence(
    canaries: Iterable[CanaryEvidence], config: MeasurementConfig
) -> tuple[str, ...]:
    """Validate recorded Codex + Claude Code canary evidence without live calls."""

    rows = tuple(canaries)
    errors: list[str] = []
    grouped: dict[str, list[CanaryEvidence]] = defaultdict(list)
    for row in rows:
        host = row.host.lower().replace("_", " ")
        canonical = "claude" if host in {"claude", "claude code"} else host
        grouped[canonical].append(row)
    for host, host_rows in grouped.items():
        if host not in {"codex", "claude"}:
            errors.append(f"unexpected_canary_host:{host}")
        if len(host_rows) != 1:
            errors.append(f"duplicate_canary:{host}")
    aliases = {
        "codex": grouped.get("codex", [None])[0],
        "claude": grouped.get("claude", [None])[0],
    }
    canary_nonces = {
        row.nonce
        for row in aliases.values()
        if row is not None and row.nonce
    }
    if all(row is not None for row in aliases.values()) and len(canary_nonces) != 2:
        errors.append("canary_nonce_not_host_unique")
    for host, row in aliases.items():
        if row is None:
            errors.append(f"missing_canary:{host}")
            continue
        if not row.nonce:
            errors.append(f"missing_nonce:{host}")
        if not all(
            (
                row.tool_result_seen,
                row.gate_log_seen,
                row.host_record_seen,
                row.dual_source_joined,
            )
        ):
            errors.append(f"incomplete_canary_path:{host}")
        if row.runtime_version != config.pinned_runtime_version:
            errors.append(f"runtime_mismatch:{host}")
        if row.measurement_protocol_version != config.measurement_protocol_version:
            errors.append(f"protocol_mismatch:{host}")
        if (
            compare_project_proofs(
                row.project_proof, config.target_project_proof
            )
            != PROJECT_MATCH
        ):
            errors.append(f"project_proof_mismatch:{host}")
        if not hmac.compare_digest(row.key_epoch, config.key_epoch):
            errors.append(f"key_epoch_mismatch:{host}")
    return tuple(sorted(set(errors)))


def verify_pins(
    *,
    contract_bytes: bytes,
    capture: CapturePin,
    manifest: MeasurementManifest,
    config: MeasurementConfig,
    prior_capture: CapturePin | None = None,
    fixture_bytes: Mapping[str, bytes] | None = None,
) -> tuple[str, ...]:
    """Return hard-invalidating B18 pin/tamper errors."""

    errors: list[str] = []
    actual_contract = _sha256(contract_bytes)
    if actual_contract != CONTRACT_SHA256:
        errors.append("contract_hash_mismatch")
    if capture.contract_sha256 != actual_contract:
        errors.append("capture_hash_mismatch")
    if capture.node_id != CAPTURE_NODE_ID:
        errors.append("capture_node_mismatch")
    if capture.contract_version != CONTRACT_VERSION:
        errors.append("capture_version_mismatch")
    if capture.supersedes_node_id is None:
        errors.append("capture_supersession_missing")
    elif capture.supersedes_node_id != SUPERSEDED_CAPTURE_NODE_ID:
        errors.append("capture_supersession_mismatch")
    if (
        prior_capture is not None
        and prior_capture.contract_sha256 != capture.contract_sha256
        and prior_capture.node_id == capture.node_id
    ):
        errors.append("supersession_without_new_node")

    if manifest.contract_sha256 != capture.contract_sha256:
        errors.append("manifest_contract_mismatch")
    if tuple(manifest.ratification_node_ids) != RATIFICATION_NODE_IDS:
        errors.append("manifest_ratification_mismatch")
    if manifest.measurement_protocol_version != config.measurement_protocol_version:
        errors.append("manifest_protocol_mismatch")
    if manifest.measurement_protocol_version != MEASUREMENT_PROTOCOL_VERSION:
        errors.append("manifest_protocol_not_frozen")
    if re.fullmatch(r"[0-9a-f]{40}", manifest.implementation_commit) is None:
        errors.append("manifest_commit_invalid")
    if config.implementation_commit is None:
        errors.append("implementation_commit_unpinned")
    elif re.fullmatch(r"[0-9a-f]{40}", config.implementation_commit) is None:
        errors.append("implementation_commit_invalid")
    elif not hmac.compare_digest(
        config.implementation_commit, manifest.implementation_commit
    ):
        errors.append("implementation_commit_mismatch")
    expected_roots = {
        source: sorted(os.fspath(item) for item in roots)
        for source, roots in sorted(config.source_roots.items())
    }
    manifest_roots = {
        source: sorted(os.fspath(item) for item in roots)
        for source, roots in sorted(manifest.source_roots.items())
    }
    if set(manifest_roots) != set(SOURCES) or any(
        not manifest_roots.get(source) for source in SOURCES
    ):
        errors.append("manifest_source_roots_incomplete")
    if manifest_roots != expected_roots:
        errors.append("manifest_source_roots_mismatch")
    if not manifest.fixture_hashes or any(
        re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for digest in manifest.fixture_hashes.values()
    ):
        errors.append("manifest_fixture_hashes_invalid")
    if dict(sorted(manifest.fixture_hashes.items())) != dict(
        sorted(FROZEN_FIXTURE_PACK_SHA256.items())
    ):
        errors.append("manifest_fixture_pack_mismatch")
    if manifest.expected_composite() != manifest.composite_sha256:
        errors.append("manifest_composite_mismatch")
    if fixture_bytes is not None:
        actual_fixtures = {
            name: _sha256(data) for name, data in sorted(fixture_bytes.items())
        }
        if actual_fixtures != dict(sorted(manifest.fixture_hashes.items())):
            errors.append("fixture_hash_mismatch")
        if set(fixture_bytes) != set(FROZEN_FIXTURE_PACK_SHA256):
            errors.append("fixture_pack_membership_mismatch")
        provenance_bytes = fixture_bytes.get("manifest.json")
        try:
            provenance = (
                json.loads(provenance_bytes.decode("utf-8"))
                if provenance_bytes is not None
                else None
            )
            provenance_rows = provenance.get("fixtures")
            if not isinstance(provenance_rows, Mapping):
                raise ValueError("fixtures mapping missing")
            provenance_hashes = {
                str(name): row.get("sha256")
                for name, row in provenance_rows.items()
                if isinstance(row, Mapping)
            }
            if provenance_hashes != FROZEN_FIXTURE_DATA_SHA256:
                raise ValueError("fixture provenance hashes differ")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, ValueError):
            errors.append("fixture_provenance_invalid")
    return tuple(sorted(set(errors)))


def _receipt_universe(state: AuditState) -> list[InvocationReceipt]:
    return sorted(
        (
            row
            for row in state.receipts
            if row.prefix_member
            and row.admitted
            and row.finalized
            and row.measurement_protocol_version
            == state.config.measurement_protocol_version
        ),
        key=lambda row: (row.lineage_order_key, row.nonce),
    )


def _receipt_prefix_boundary(state: AuditState) -> tuple[datetime, str]:
    admitted_order = sorted(
        (
        row
        for row in state.receipts
        if row.admitted
        and row.finalized
        and row.measurement_protocol_version
        == state.config.measurement_protocol_version
        ),
        key=lambda row: (row.lineage_order_key, row.nonce),
    )
    eligible_seen = 0
    cutoff = (state.config.cap, "")
    for row in admitted_order:
        if row.eligible:
            eligible_seen += 1
            if eligible_seen == V1_ELIGIBLE_TARGET:
                cutoff = (row.lineage_order_key, row.nonce)
                break
    return cutoff


def _marker_in_receipt_prefix(state: AuditState, marker: LossMarker) -> bool:
    """Apply target scope and T1 membership to loss accounting."""

    if not marker.in_scope:
        return False
    associated: list[InvocationReceipt] = []
    if marker.nonce:
        associated = [
            row
            for row in state.receipts
            if row.nonce == marker.nonce
            and row.measurement_protocol_version
            == state.config.measurement_protocol_version
        ]
        target_associated = any(
            row.disposition != "foreign_project" for row in associated
        )
        if _marker_is_proven_foreign(marker, state.config) and not target_associated:
            return False
        if associated and any(
            row.prefix_member and row.admitted and row.finalized
            for row in associated
        ):
            return True
    elif _marker_is_proven_foreign(marker, state.config):
        return False
    cutoff = _receipt_prefix_boundary(state)
    if marker.ts is not None:
        if cutoff == (state.config.cap, ""):
            return True
        if associated:
            return (marker.ts, marker.nonce or "") <= cutoff
        prefix_nonces = frozenset(
            row.nonce
            for row in state.receipts
            if row.prefix_member
            and row.admitted
            and row.finalized
            and row.measurement_protocol_version
            == state.config.measurement_protocol_version
        )
        source_cutoff = _prefix_source_cutoffs(
            state.receipts, prefix_nonces, state.config,
        ).get(marker.source or "")
        if source_cutoff is not None:
            return marker.ts <= source_cutoff
        # No source-qualified prefix coordinate can prove that this marker is
        # after T1. Keep it conservative.
        return True
    # Timestamp-less deletion/diagnostic markers may use a known receipt's
    # prefix membership. Unknown markers remain conservative.
    return not associated


def _state_integrity_errors(state: AuditState) -> tuple[str, ...]:
    """Detect impossible stored receipt combinations before oracle arithmetic."""

    errors: set[str] = set()
    if state.measurement_taken_at is None:
        errors.add("measurement_time_missing")
    close_ready = bool(
        state.measurement_taken_at is not None
        and state.measurement_taken_at
        >= state.config.cap + timedelta(seconds=FRESHNESS_SECONDS)
    )
    if state.config.cap_reached and not close_ready:
        errors.add("cap_reached_unverified")
    if not state.config.cap_reached and close_ready:
        errors.add("cap_reached_state_mismatch")
    for source in SOURCES:
        if not tuple(state.config.source_roots.get(source, ())):
            errors.add(f"source_roots_missing:{source}")
    by_identity: dict[tuple[str, str], list[InvocationReceipt]] = defaultdict(list)
    for row in state.receipts:
        by_identity[row.identity].append(row)
        if row.prefix_member and not row.admitted:
            errors.add("non_admitted_prefix_member")
        if row.disposition in {"skipped", "foreign_project"} and row.outcome is not None:
            errors.add("non_outcome_disposition_has_outcome")
        if (
            row.window_start is not None
            and row.window_end is not None
            and row.window_end < row.window_start
        ):
            errors.add("invalid_receipt_window_order")
        if row.disposition == "loss_signal" and not row.loss_reasons:
            errors.add("loss_disposition_without_reason")
        if row.disposition != "loss_signal" and row.loss_reasons:
            errors.add("loss_reason_disposition_mismatch")
        if row.disposition == "conflict" and not row.conflict_reasons:
            errors.add("conflict_disposition_without_reason")
        if row.disposition != "conflict" and row.conflict_reasons:
            errors.add("conflict_reason_disposition_mismatch")
    if any(len(rows) > 1 for rows in by_identity.values()):
        errors.add("duplicate_receipt_identity")
    if any(
        any(row != rows[0] for row in rows[1:])
        for rows in by_identity.values()
        if len(rows) > 1
    ):
        errors.add("non_identical_duplicate_receipt_identity")
    return tuple(sorted(errors))


def compute_oracles(state: AuditState) -> OracleResult:
    """Compute O1/O2/O3 after hard-invalidation checks."""

    invalidations = tuple(
        sorted(set(state.hard_invalidations) | set(_state_integrity_errors(state)))
    )
    if invalidations:
        return OracleResult(
            invalidated=True,
            invalidation_reasons=invalidations,
            o1_pass=None,
            o2=None,
            o2_reasons=(),
            o3_pass=None,
            eligible_n=0,
            d_min=0,
            raw_label_counts={},
            clean_label_counts={},
            ambiguous_count=0,
            ambiguous_rate=None,
            disposition_counts={},
            marker_count=sum(
                _marker_in_receipt_prefix(state, marker)
                for marker in state.loss_markers
            ),
            source_health_clean=False,
            v1_green=False,
            verdict="invalidated",
            quality_summary="Audit invalidated before oracle arithmetic.",
        )

    universe = _receipt_universe(state)
    eligible = [row for row in universe if row.eligible]
    d_min_rows = [row for row in universe if row.in_d_min]
    d_min = len(d_min_rows)
    eligible_n = len(eligible)
    markers = [
        row for row in state.loss_markers if _marker_in_receipt_prefix(state, row)
    ]
    post_prefix_malformed = Counter(
        row.source
        for row in state.loss_markers
        if row.source in SOURCES
        and row.reason == "schema_invalid"
        and row.in_scope
        and not _marker_is_proven_foreign(row, state.config)
        and not _marker_in_receipt_prefix(state, row)
    )
    post_prefix_missing_daily = Counter(
        row.source
        for row in state.loss_markers
        if row.source in SOURCES
        and row.reason == "missing_daily_file"
        and row.in_scope
        and not _marker_is_proven_foreign(row, state.config)
        and not _marker_in_receipt_prefix(state, row)
    )
    health_counts = Counter(row.source for row in state.source_health)
    completeness_counts = Counter(
        row.source for row in state.candidate_completeness
    )
    health_symmetric = all(health_counts[source] == 1 for source in SOURCES) and set(
        health_counts
    ) == set(SOURCES)
    completeness_symmetric = all(
        completeness_counts[source] == 1 for source in SOURCES
    ) and set(completeness_counts) == set(SOURCES)
    def effective_health_clean(row: SourceHealth) -> bool:
        return not (
            max(0, row.missing_files - post_prefix_missing_daily[row.source])
            or row.unreadable_files
            or max(
                0,
                row.malformed_regions - post_prefix_malformed[row.source],
            )
            or row.unstable_files
        )

    health_clean = health_symmetric and all(
        effective_health_clean(row) for row in state.source_health
    )
    health_by_source = {row.source: row for row in state.source_health}

    def effective_completeness(row: CandidateCompletenessReceipt) -> bool:
        if row.complete:
            return True
        health_row = health_by_source.get(row.source)
        if health_row is None:
            return False
        # Candidate completeness may be false solely because a parser marker
        # lies after the closed T1 prefix. Never mask inventory/read/hash loss.
        return bool(
            (
                health_row.malformed_regions
                or health_row.missing_files
            )
            and max(
                0,
                health_row.malformed_regions - post_prefix_malformed[row.source],
            )
            == 0
            and max(
                0,
                health_row.missing_files
                - post_prefix_missing_daily[row.source],
            )
            == 0
            and not health_row.unreadable_files
            and not health_row.unstable_files
        )

    all_candidates_complete = completeness_symmetric and all(
        effective_completeness(row) for row in state.candidate_completeness
    )

    # O1 deliberately spans the entire registered [T0, cap) window. Unlike O2
    # it is not closed at T1; skipped, pilot, foreign, and post-T1 parseable S1
    # rows remain subject to the verdict id-list requirement.
    in_scope_gate_rows = [
        row
        for row in state.gate_rows
        if row.in_scope
    ]
    o1_pass = all(row.id_lists_valid for row in in_scope_gate_rows)
    raw_counts = Counter(
        row.outcome for row in universe if row.outcome is not None
    )
    clean_counts = Counter(
        row.outcome for row in eligible if row.outcome is not None
    )
    disposition_counts = Counter(row.disposition for row in universe)

    o2_reasons: list[str] = []
    if any(health_counts[source] == 0 for source in SOURCES):
        o2_reasons.append("source_health_missing_source")
    elif not health_symmetric:
        o2_reasons.append("source_health_not_symmetric")
    if any(completeness_counts[source] == 0 for source in SOURCES):
        o2_reasons.append("candidate_completeness_missing_source")
    elif not completeness_symmetric:
        o2_reasons.append("candidate_completeness_not_symmetric")
    if 10 * eligible_n < 7 * d_min:
        o2 = "fail"
        o2_reasons.append("coverage_below_70_percent_of_d_min")
    else:
        if any(row.disposition == "loss_signal" for row in universe):
            o2_reasons.append("loss_signals_present")
        if markers:
            o2_reasons.append("loss_markers_present")
        if any(row.disposition == "conflict" for row in universe):
            o2_reasons.append("conflicts_present")
        if not health_clean:
            o2_reasons.append("source_health_not_clean")
        if not all_candidates_complete:
            o2_reasons.append("candidate_completeness_unproven")
        if any(
            row.admitted
            and row.prefix_member
            and not row.finalized
            and row.measurement_protocol_version
            == state.config.measurement_protocol_version
            and row.disposition != "foreign_project"
            for row in state.receipts
        ):
            o2_reasons.append("unfinalized_receipts")
        o2 = "indeterminate" if o2_reasons else "pass"

    first_thirty = eligible[:V1_ELIGIBLE_TARGET]
    ambiguous = sum(row.outcome == "AMBIGUOUS" for row in first_thirty)
    o3_pass = (
        ambiguous <= O3_MAX_AMBIGUOUS
        if len(first_thirty) == V1_ELIGIBLE_TARGET
        else None
    )
    ambiguous_rate = (
        round(100.0 * clean_counts.get("AMBIGUOUS", 0) / eligible_n, 6)
        if eligible_n
        else None
    )
    v1_green = bool(
        eligible_n == V1_ELIGIBLE_TARGET
        and o1_pass
        and o2 == "pass"
        and o3_pass is True
    )
    if v1_green:
        verdict = "pass"
    elif state.config.cap_reached and eligible_n < V1_ELIGIBLE_TARGET:
        verdict = "insufficient-n"
    elif o2 == "fail" or o1_pass is False or (o2 == "pass" and o3_pass is False):
        verdict = "fail"
    else:
        verdict = "indeterminate"
    quality_summary = (
        f"Clean eligible outcomes: N={eligible_n}; AMBIGUOUS={clean_counts.get('AMBIGUOUS', 0)}"
        + (
            f" ({ambiguous_rate:.6f}%)."
            if ambiguous_rate is not None
            else "; rate unavailable."
        )
    )
    return OracleResult(
        invalidated=False,
        invalidation_reasons=(),
        o1_pass=o1_pass,
        o2=o2,
        o2_reasons=tuple(o2_reasons),
        o3_pass=o3_pass,
        eligible_n=eligible_n,
        d_min=d_min,
        raw_label_counts=dict(sorted(raw_counts.items())),
        clean_label_counts=dict(sorted(clean_counts.items())),
        ambiguous_count=ambiguous,
        ambiguous_rate=ambiguous_rate,
        disposition_counts=dict(sorted(disposition_counts.items())),
        marker_count=len(markers),
        source_health_clean=health_clean,
        v1_green=v1_green,
        verdict=verdict,
        quality_summary=quality_summary,
    )


def audit(
    state: AuditState,
    *,
    contract_bytes: bytes | None = None,
    capture: CapturePin | None = None,
    manifest: MeasurementManifest | None = None,
    prior_capture: CapturePin | None = None,
    fixture_bytes: Mapping[str, bytes] | None = None,
    canaries: Iterable[CanaryEvidence] | None = None,
) -> OracleResult:
    """Verify the complete frozen envelope, then evaluate the state."""

    invalidations = list(state.hard_invalidations)
    supplied = (
        contract_bytes is not None,
        capture is not None,
        manifest is not None,
        fixture_bytes is not None,
    )
    if not all(supplied):
        invalidations.append("incomplete_audit_envelope")
        if fixture_bytes is None:
            invalidations.append("fixture_bytes_missing")
    else:
        invalidations.extend(
            verify_pins(
                contract_bytes=contract_bytes,
                capture=capture,
                manifest=manifest,
                config=state.config,
                prior_capture=prior_capture,
                fixture_bytes=fixture_bytes,
            )
        )
    if canaries is None:
        invalidations.append("canary_evidence_missing")
    else:
        invalidations.extend(verify_canary_evidence(canaries, state.config))
    return compute_oracles(
        replace(state, hard_invalidations=tuple(sorted(set(invalidations))))
    )


def _opaque_maps(
    state: AuditState,
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    files: dict[str, set[str]] = defaultdict(set)
    sessions: dict[str, set[str]] = defaultdict(set)
    for receipt in state.receipts:
        for observation in receipt.observations:
            files[observation.source].add(observation.file)
            if observation.session_id:
                sessions[observation.source].add(observation.session_id)
    for row in state.candidate_completeness:
        files[row.source].update(row.enumerated_files)
    for marker in state.loss_markers:
        source = marker.source or "GLOBAL"
        if marker.file:
            files[source].add(marker.file)
        if marker.session_id:
            sessions[source].add(marker.session_id)
    file_tokens = {
        (source, value): f"{source}-file-{index:04d}"
        for source, values in sorted(files.items())
        for index, value in enumerate(sorted(values), 1)
    }
    session_tokens = {
        (source, value): f"{source}-session-{index:04d}"
        for source, values in sorted(sessions.items())
        for index, value in enumerate(sorted(values), 1)
    }
    return file_tokens, session_tokens


def _receipt_report_row(
    row: InvocationReceipt,
    session_tokens: Mapping[tuple[str, str], str],
) -> dict[str, Any]:
    receipt_sessions: dict[str, str] = {}
    if row.session_id:
        sources = sorted({observation.source for observation in row.observations})
        for source in sources:
            token = session_tokens.get((source, row.session_id))
            if token:
                receipt_sessions[source] = token
    return {
        "identity": [row.nonce, row.measurement_protocol_version],
        "disposition": row.disposition,
        "admitted": row.admitted,
        "lineage_order_key": _iso(row.lineage_order_key),
        "fresh_ts": _iso(row.fresh_ts),
        "prefix_member": row.prefix_member,
        "session_tokens": receipt_sessions,
        "outcome": row.outcome,
        "censored_reason": row.censored_reason,
        "eligible": row.eligible,
        "loss_reasons": list(row.loss_reasons),
        "conflict_reasons": list(row.conflict_reasons),
        "finalized": row.finalized,
        "window": [_iso(row.window_start), _iso(row.window_end)],
    }


def render_report(state: AuditState, result: OracleResult | None = None) -> str:
    """Render a deterministic, byte-stable JSON report."""

    result = result or compute_oracles(state)
    file_tokens, session_tokens = _opaque_maps(state)
    payload = {
        "contract": {
            "capture_node_id": CAPTURE_NODE_ID,
            "sha256": CONTRACT_SHA256,
            "version": CONTRACT_VERSION,
            "measurement_protocol_version": state.config.measurement_protocol_version,
        },
        "window": {
            "t0": _iso(state.config.t0),
            "cap": _iso(state.config.cap),
            "cap_reached": state.config.cap_reached,
        },
        "oracles": {
            "invalidated": result.invalidated,
            "invalidation_reasons": list(result.invalidation_reasons),
            "o1": result.o1_pass,
            "o2": result.o2,
            "o2_reasons": list(result.o2_reasons),
            "o3": result.o3_pass,
            "eligible_n": result.eligible_n,
            "d_min": result.d_min,
            "ambiguous_count": result.ambiguous_count,
            "ambiguous_rate": result.ambiguous_rate,
            "v1_green": result.v1_green,
            "verdict": result.verdict,
        },
        "quality": {
            "raw": dict(result.raw_label_counts),
            "clean": dict(result.clean_label_counts),
            "summary": result.quality_summary,
        },
        "accounting": {
            "dispositions": dict(result.disposition_counts),
            "loss_markers": result.marker_count,
            "source_health_clean": result.source_health_clean,
            "source_health": [
                {
                    "source": row.source,
                    "root_count": len(row.roots),
                    "files_seen": row.files_seen,
                    "files_parsed": row.files_parsed,
                    "missing_files": row.missing_files,
                    "unreadable_files": row.unreadable_files,
                    "malformed_regions": row.malformed_regions,
                    "unstable_files": row.unstable_files,
                }
                for row in sorted(state.source_health, key=lambda x: x.source)
            ],
            "candidate_completeness": [
                {
                    "source": row.source,
                    "root_count": len(row.roots),
                    "enumerated_file_tokens": [
                        file_tokens[(row.source, file)]
                        for file in row.enumerated_files
                    ],
                    "stable_file_count": len(row.stable_file_hashes),
                    "complete": row.complete,
                }
                for row in sorted(state.candidate_completeness, key=lambda x: x.source)
            ],
        },
        "receipts": [
            _receipt_report_row(row, session_tokens)
            for row in sorted(
                state.receipts,
                key=lambda x: (
                    x.lineage_order_key,
                    x.nonce,
                    x.measurement_protocol_version,
                ),
            )
        ],
        "loss_markers": [
            {
                "reason": row.reason,
                "source": row.source,
                "file_token": (
                    file_tokens.get((row.source or "GLOBAL", row.file))
                    if row.file
                    else None
                ),
                "byte_offset": row.byte_offset,
                "ts": _iso(row.ts),
                "session_token": (
                    session_tokens.get(
                        (row.source or "GLOBAL", row.session_id)
                    )
                    if row.session_id
                    else None
                ),
                "nonce": row.nonce,
                "in_scope": row.in_scope,
                "target_local": not _marker_is_proven_foreign(row, state.config),
                # Marker detail can contain exception text or external-record
                # fragments. Keep it in AuditState for local diagnostics, but
                # expose only structural presence in the public report.
                "detail_present": row.detail is not None,
            }
            for row in sorted(
                state.loss_markers,
                key=lambda x: (
                    x.source or "",
                    x.file or "",
                    x.byte_offset if x.byte_offset is not None else -1,
                    x.reason,
                ),
            )
        ],
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n"


def audit_report(
    state: AuditState,
    *,
    contract_bytes: bytes,
    capture: CapturePin,
    manifest: MeasurementManifest,
    fixture_bytes: Mapping[str, bytes],
    prior_capture: CapturePin | None = None,
    canaries: Iterable[CanaryEvidence] | None = None,
) -> str:
    """Validate the complete B18 envelope before deterministic rendering."""

    result = audit(
        state,
        contract_bytes=contract_bytes,
        capture=capture,
        manifest=manifest,
        prior_capture=prior_capture,
        fixture_bytes=fixture_bytes,
        canaries=canaries,
    )
    return render_report(state, result)


def _project_proof_checked(
    value: Mapping[str, Any],
) -> tuple[ProjectProof | None, tuple[str, ...]]:
    """Distinguish an absent proof from an explicitly malformed one."""

    proof: Mapping[str, Any] | None = None
    if "project_proof" in value and value.get("project_proof") is not None:
        raw = value.get("project_proof")
        if not isinstance(raw, Mapping):
            return None, ("project_proof_invalid",)
        proof = raw
    elif (
        "project_fingerprint" in value
        and value.get("project_fingerprint") not in (None, "")
    ):
        proof = {
            "version": value.get("project_proof_version") or PROJECT_PROOF_VERSION,
            "key_epoch": value.get("key_epoch"),
            "fingerprint": value.get("project_fingerprint"),
        }
    if proof is None:
        return None, ()
    required = ("version", "key_epoch", "key_id", "fingerprint")
    if any(proof.get(name) in (None, "") for name in required):
        # A missing project/key-id component is the named required-field loss,
        # not an unparseable region.
        return None, ("project_proof_missing",)
    normalized = {
        "version": proof.get("version"),
        "key_epoch": proof.get("key_epoch"),
        "key_id": proof.get("key_id"),
        "fingerprint": proof.get("fingerprint"),
    }
    if (
        not all(isinstance(item, str) for item in normalized.values())
        or compare_project_proofs(normalized, normalized) != PROJECT_MATCH
    ):
        return None, ("project_proof_invalid",)
    return normalized, ()


def _project_proof_from_record(value: Mapping[str, Any]) -> ProjectProof | None:
    return _project_proof_checked(value)[0]


def _optional_string_field(
    value: Mapping[str, Any], name: str
) -> tuple[str | None, tuple[str, ...]]:
    """Read an optional identity/scope string without coercing scalars."""

    raw = value.get(name)
    if raw is None or raw == "":
        return None, ()
    if isinstance(raw, str):
        return raw, ()
    return None, (f"invalid_string:{name}",)


def _first_optional_string_field(
    value: Mapping[str, Any], names: Sequence[str]
) -> tuple[str | None, tuple[str, ...]]:
    """Read the first populated alias while validating every supplied alias."""

    selected: str | None = None
    errors: list[str] = []
    for name in names:
        parsed, field_errors = _optional_string_field(value, name)
        if selected is None and parsed is not None:
            selected = parsed
        errors.extend(field_errors)
    return selected, tuple(sorted(set(errors)))


def _first_optional_timestamp_field(
    value: Mapping[str, Any], names: Sequence[str]
) -> tuple[datetime | None, tuple[str, ...]]:
    selected: datetime | None = None
    parsed_values: list[datetime] = []
    errors: list[str] = []
    for name in names:
        if name not in value or value.get(name) in (None, ""):
            continue
        raw = value.get(name)
        if not isinstance(raw, (str, datetime)):
            errors.append(f"invalid_timestamp:{name}")
            continue
        parsed = _utc(raw)
        if parsed is None:
            errors.append(f"invalid_timestamp:{name}")
            continue
        if selected is None:
            selected = parsed
        parsed_values.append(parsed)
    if len(set(parsed_values)) > 1:
        errors.append("timestamp_alias_mismatch")
    return selected, tuple(sorted(set(errors)))


def _s1_verdict_id_lists(
    value: Mapping[str, Any],
) -> Mapping[str, list[int]] | None:
    """Read only the six fields physically present on an S1 gate-log row.

    Compact MCP-result reconstruction belongs to S2. O1 is an emitted-field
    presence check and must not be satisfied by alternate verbose structures.
    """

    result: dict[str, list[int]] = {}
    for name in REQUIRED_ID_LIST_FIELDS:
        raw = value.get(name)
        if isinstance(raw, (list, tuple)) and not any(
            isinstance(item, bool) or not isinstance(item, int)
            for item in raw
        ):
            result[name] = list(raw)
    return result or None


def _json_lines(data: bytes) -> Iterable[tuple[int, Mapping[str, Any] | None, str | None]]:
    offset = 0
    for raw in data.splitlines(keepends=True):
        body = raw.rstrip(b"\r\n")
        if not body:
            offset += len(raw)
            continue
        try:
            decoded = body.decode("utf-8")
            value = json.loads(decoded)
            if not isinstance(value, Mapping):
                yield offset, None, "schema_invalid"
            else:
                yield offset, value, None
        except (UnicodeDecodeError, json.JSONDecodeError):
            yield offset, None, "schema_invalid"
        offset += len(raw)


def parse_gate_log_bytes(
    data: bytes,
    *,
    file: str,
    config: MeasurementConfig,
) -> tuple[list[Observation], list[LossMarker], list[GateRowCheck]]:
    observations: list[Observation] = []
    markers: list[LossMarker] = []
    checks: list[GateRowCheck] = []
    records = tuple(_json_lines(data))
    for record_index, (offset, value, error) in enumerate(records):
        if error or value is None:
            markers.append(
                LossMarker(
                    reason="schema_invalid",
                    source=SOURCE_GATE,
                    file=file,
                    byte_offset=offset,
                    ts=_dated_source_marker_ts(SOURCE_GATE, file),
                    in_scope=_malformed_region_in_scope(
                        file, records, record_index, SOURCE_GATE, config
                    ),
                )
            )
            continue
        ts, ts_errors = _first_optional_timestamp_field(value, ("ts",))
        nonce, nonce_errors = _first_optional_string_field(
            value, ("gate_call_id", "nonce")
        )
        session_id, session_errors = _optional_string_field(value, "session_id")
        proof, proof_errors = _project_proof_checked(value)
        proof_schema_errors = tuple(
            error
            for error in proof_errors
            if error != "project_proof_missing"
        )
        ids = _s1_verdict_id_lists(value)
        typed, scalar_errors = _typed_observation_fields(value)
        attestation, runtime_version, embedded_conflicts, runtime_errors = (
            _shared_result_fields(value)
        )
        protocol, protocol_errors = _optional_string_field(
            value, "measurement_protocol_version"
        )
        key_epoch, key_epoch_errors = _optional_string_field(
            value, "key_epoch"
        )
        valid, missing = _id_lists_valid(ids)
        checks.append(
            GateRowCheck(
                obs_id=(SOURCE_GATE, file, offset),
                ts=ts,
                in_scope=_source_record_in_scope(
                    SOURCE_GATE, file, ts, config,
                ),
                id_lists_valid=valid,
                missing_fields=missing,
            )
        )
        host_adapter = value.get("host_adapter")
        adapter_errors = (
            ("invalid_host_adapter",)
            if host_adapter is not None
            and host_adapter not in {"claude", "codex", "cursor"}
            else ()
        )
        # Verdict ID-list validity is O1 evidence, not parseability.  Preserve
        # the observation and let GateRowCheck fail O1 for missing/wrongly
        # typed lists instead of manufacturing a schema-loss marker.
        schema_errors = tuple(
            sorted(
                set(
                    scalar_errors
                    + adapter_errors
                    + nonce_errors
                    + session_errors
                    + proof_schema_errors
                    + runtime_errors
                    + protocol_errors
                    + key_epoch_errors
                    + ts_errors
                )
            )
        )
        if schema_errors:
            markers.append(
                LossMarker(
                    reason="schema_invalid",
                    source=SOURCE_GATE,
                    file=file,
                    byte_offset=offset,
                    ts=ts or _dated_source_marker_ts(SOURCE_GATE, file),
                    session_id=session_id,
                    adapter=(
                        value.get("host_adapter")
                        if isinstance(value.get("host_adapter"), str)
                        else None
                    ),
                    project_proof=proof,
                    nonce=nonce,
                    in_scope=_source_record_in_scope(
                        SOURCE_GATE, file, ts, config,
                    ),
                    detail="|".join(schema_errors),
                )
            )
            continue
        observations.append(
            Observation(
                source=SOURCE_GATE,
                file=file,
                byte_offset=offset,
                nonce=nonce,
                ts=ts,
                session_id=session_id,
                adapter=(
                    str(value.get("host_adapter") or "")
                    or None
                ),
                attestation=attestation,
                measurement_protocol_version=protocol,
                project_proof=proof,
                key_epoch=key_epoch,
                runtime_version=runtime_version,
                verdict=(
                    value.get("recommendation")
                    if isinstance(value.get("recommendation"), str)
                    else None
                ),
                verdict_id_lists=ids,
                skipped=typed["skipped"],
                observable=typed["observable"],
                evidence_available=typed["evidence_available"],
                progress_inserts=typed["progress_inserts"],
                inserts=typed["inserts"],
                linked_cited_insert=typed["linked_cited_insert"],
                cited_edge_activity=typed["cited_edge_activity"],
                touches=typed["touches"],
                embedded_conflict_reasons=embedded_conflicts,
                legacy_project=(
                    proof is None
                    and not proof_errors
                    and bool(value.get("project"))
                ),
                hash_annotated=bool(value.get("query_hash")) and nonce is None,
                pre_nonce=nonce is None,
                raw_sha256=_sha256(
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ),
            )
        )
    return observations, markers, checks


def _walk_json(value: Any, *, depth: int = 0) -> Iterable[Any]:
    if depth > 8:
        return
    yield value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            wrapper = re.fullmatch(
                r"Wall time: [^\r\n]+\r?\n(?:Output|Final output):\r?\n([\s\S]*)",
                value,
            )
            if wrapper is None:
                return
            try:
                decoded = json.loads(wrapper.group(1))
            except (json.JSONDecodeError, TypeError):
                return
        yield from _walk_json(decoded, depth=depth + 1)
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _walk_json(child, depth=depth + 1)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for child in value:
            yield from _walk_json(child, depth=depth + 1)


def _gate_result_payload(value: Any) -> Mapping[str, Any] | None:
    fallback: Mapping[str, Any] | None = None
    for child in _walk_json(value):
        if isinstance(child, Mapping) and (
            child.get("gate_call_id") or child.get("nonce")
        ):
            return child
        if (
            fallback is None
            and isinstance(child, Mapping)
            and (
                "gate_status" in child
                or "recommendation" in child
                or isinstance(child.get("verdict"), Mapping)
            )
        ):
            fallback = child
    return fallback


def _gate_result_field(result: Mapping[str, Any] | None, name: str) -> Any:
    if result is None:
        return None
    if name in result:
        return result[name]
    verdict = result.get("verdict")
    if isinstance(verdict, Mapping) and name in verdict:
        return verdict[name]
    return None


def _shared_result_fields(
    result: Mapping[str, Any] | None,
) -> tuple[str | None, str | None, tuple[str, ...], tuple[str, ...]]:
    """Normalize runtime aliases while retaining any disagreement as conflict."""

    if result is None:
        return None, None, (), ()
    attestation, attestation_errors = _first_optional_string_field(
        result, ("attestation", "runtime_attestation")
    )
    runtime, runtime_errors = _optional_string_field(result, "runtime_version")
    aliases = [
        parsed
        for parsed in (
            *(
                _optional_string_field(result, name)[0]
                for name in ("attestation", "runtime_attestation")
            ),
            runtime,
        )
        if parsed is not None
    ]
    conflicts: set[str] = set()
    if len(set(aliases)) > 1:
        conflicts.add("runtime_attestation_alias_mismatch")
    verdict = result.get("verdict")
    if isinstance(verdict, Mapping):
        for name in ("recommendation", "skipped", *REQUIRED_ID_LIST_FIELDS):
            if name in result and name in verdict and result[name] != verdict[name]:
                conflicts.add(f"nested_shared_field_mismatch:{name}")
    return (
        attestation,
        runtime,
        tuple(sorted(conflicts)),
        tuple(sorted(set(attestation_errors + runtime_errors))),
    )


def _strict_id_list(value: Any, name: str) -> tuple[list[int] | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, (list, tuple)) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        return None, f"invalid_int_list:{name}"
    return list(value), None


def _shared_id_lists_checked(
    result: Mapping[str, Any] | None,
) -> tuple[Mapping[str, list[int]] | None, tuple[str, ...]]:
    """Recover all six structural id lists from full or compact MCP results."""

    if result is None:
        return None, ()
    values: dict[str, list[int]] = {}
    errors: list[str] = []
    for name in REQUIRED_ID_LIST_FIELDS:
        raw = _gate_result_field(result, name)
        if raw is None and name == "evidence_ids" and "evidence" in result:
            evidence = result.get("evidence")
            if not isinstance(evidence, list) or any(
                not isinstance(item, Mapping) for item in evidence
            ):
                errors.append("invalid_evidence_list")
                continue
            raw = [item.get("id") for item in evidence]
            parsed, error = _strict_id_list(raw, name)
            if parsed is not None:
                # S1 writes this field sorted; normalize compact evidence too.
                values[name] = sorted(parsed)
            if error:
                errors.append(error)
            continue
        if raw is None and name == "seed_ids":
            if "chain_summary" in result:
                summary = result.get("chain_summary")
                if not isinstance(summary, Mapping):
                    errors.append("invalid_chain_summary")
                else:
                    raw = summary.get("seed_ids")
            if raw is None and "chains" in result:
                chains = result.get("chains")
                if not isinstance(chains, Mapping):
                    errors.append("invalid_chains")
                else:
                    seeds = chains.get("seeds")
                    if not isinstance(seeds, (list, tuple)) or any(
                        not isinstance(seed, Mapping) for seed in seeds
                    ):
                        errors.append("invalid_chain_seeds")
                    else:
                        raw = [seed.get("id") for seed in seeds]
        parsed, error = _strict_id_list(raw, name)
        if parsed is not None:
            values[name] = parsed
        if error:
            errors.append(error)
    return (values or None), tuple(sorted(set(errors)))


def _shared_id_lists(result: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    return _shared_id_lists_checked(result)[0]


def _typed_observation_fields(
    result: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Read structural scalars without Python truthiness/string coercion."""

    fields: dict[str, Any] = {}
    errors: list[str] = []
    for name, default in (
        ("skipped", None),
        ("observable", None),
        ("evidence_available", None),
        ("linked_cited_insert", False),
        ("cited_edge_activity", False),
    ):
        value = _gate_result_field(result, name)
        if value is None:
            fields[name] = default
        elif isinstance(value, bool):
            fields[name] = value
        else:
            fields[name] = default
            errors.append(f"invalid_bool:{name}")
    for name in ("progress_inserts", "inserts", "touches"):
        value = _gate_result_field(result, name)
        if value is None:
            fields[name] = 0
        elif isinstance(value, int) and not isinstance(value, bool):
            fields[name] = value
        else:
            fields[name] = 0
            errors.append(f"invalid_int:{name}")
    return fields, tuple(sorted(set(errors)))


def _host_scope(
    cwd: str | None, project_proof_context: ProjectProofContext | None
) -> str:
    if not cwd:
        return "unknown"
    if project_proof_context is None:
        return f"legacy:{cwd}"
    return "proof:" + project_proof_context.prove(cwd)["fingerprint"]


def _contains_structural_latch_gate_call(script: str) -> bool:
    """Find an actual JS tool invocation while ignoring strings/comments."""

    token = re.compile(r"tools\.mcp__latch(?:__latch_gate|__kb_gate)\s*\(")
    index = 0
    while index < len(script):
        if token.match(script, index):
            return True
        char = script[index]
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            while index < len(script):
                if script[index] == "\\":
                    index += 2
                    continue
                if script[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if script.startswith("//", index):
            newline = script.find("\n", index + 2)
            index = len(script) if newline < 0 else newline + 1
            continue
        if script.startswith("/*", index):
            end = script.find("*/", index + 2)
            index = len(script) if end < 0 else end + 2
            continue
        index += 1
    return False


def _codex_gate_call(payload: Mapping[str, Any], gate_tool_names: set[str]) -> bool:
    name = payload.get("name")
    if isinstance(name, str) and name in gate_tool_names:
        return True
    if payload.get("type") != "custom_tool_call" or name != "exec":
        return False
    script = payload.get("input")
    if script is None:
        script = payload.get("arguments")
    if isinstance(script, Mapping):
        script = script.get("input") or script.get("code")
    return isinstance(script, str) and _contains_structural_latch_gate_call(script)


def parse_host_record_segments(
    segments: Sequence[tuple[str, bytes]],
    *,
    config: MeasurementConfig,
    project_proof_context: ProjectProofContext | None = None,
    vault_key: bytes | None = None,
) -> tuple[list[Observation], list[LossMarker]]:
    """Union complete S2 segments and join calls/results by scoped identity."""

    if vault_key is not None:
        raise ValueError(
            "raw vault-key proof derivation is disabled; pass ProjectProofContext"
        )
    if (
        project_proof_context is not None
        and project_proof_context.key_epoch != config.key_epoch
    ):
        raise ValueError("project proof context key epoch does not match config")

    observations: list[Observation] = []
    markers: list[LossMarker] = []
    gate_tool_names = {
        "latch_gate",
        "kb_gate",
        "mcp__latch__latch_gate",
        "mcp__latch__kb_gate",
        "mcp__claude-kb__kb_gate",
    }
    # (adapter, opaque/legacy project scope, session, call id)
    calls: dict[tuple[str, str, str, str], list[tuple[Any, ...]]] = defaultdict(list)
    results: dict[tuple[str, str, str, str], list[tuple[Any, ...]]] = defaultdict(list)
    unrelated_calls: set[tuple[str, str, str, str]] = set()

    def context_key(
        adapter: str,
        cwd: str | None,
        session_id: str | None,
        call_id: str,
    ) -> tuple[str, str, str, str]:
        return (
            adapter,
            _host_scope(cwd, project_proof_context),
            session_id or "",
            call_id,
        )

    for file, data in sorted(segments, key=lambda item: item[0]):
        session_id: str | None = None
        session_cwd: str | None = None
        codex_context_valid = True
        records = tuple(_json_lines(data))
        for record_index, (offset, value, error) in enumerate(records):
            if error or value is None:
                context_proof = (
                    project_proof_context.prove(session_cwd)
                    if session_cwd and project_proof_context is not None
                    else None
                )
                markers.append(
                    LossMarker(
                        reason="schema_invalid",
                        source=SOURCE_HOST,
                        file=file,
                        byte_offset=offset,
                        session_id=session_id,
                        adapter=(
                            "codex"
                            if session_id is not None or session_cwd is not None
                            else None
                        ),
                        project_proof=context_proof,
                        host_scope_project_proof=context_proof,
                        in_scope=_malformed_region_in_scope(
                            file, records, record_index, SOURCE_HOST, config
                        ),
                    )
                )
                continue
            raw_payload = value.get("payload")
            payload = raw_payload if isinstance(raw_payload, Mapping) else {}
            record_type = str(value.get("type") or "")
            payload_type = str(payload.get("type") or "")
            ts, record_ts_errors = _first_optional_timestamp_field(
                value, ("timestamp", "ts")
            )
            if record_ts_errors:
                markers.append(
                    LossMarker(
                        reason="schema_invalid",
                        source=SOURCE_HOST,
                        file=file,
                        byte_offset=offset,
                        ts=None,
                        session_id=session_id,
                        adapter=(
                            "codex"
                            if session_id is not None or session_cwd is not None
                            else None
                        ),
                        project_proof=(
                            project_proof_context.prove(session_cwd)
                            if session_cwd
                            and project_proof_context is not None
                            else None
                        ),
                        in_scope=_source_record_in_scope(
                            SOURCE_HOST, file, None, config
                        ),
                        detail="|".join(record_ts_errors),
                    )
                )
                continue

            if (
                record_type == "session_meta"
                and raw_payload is not None
                and not isinstance(raw_payload, Mapping)
            ):
                session_id = None
                session_cwd = None
                codex_context_valid = False
                markers.append(
                    LossMarker(
                        reason="schema_invalid",
                        source=SOURCE_HOST,
                        file=file,
                        byte_offset=offset,
                        ts=ts,
                        adapter="codex",
                        in_scope=_in_window(ts, config),
                        detail="invalid_object:payload",
                    )
                )
                continue

            if record_type == "session_meta" or payload_type == "session_meta":
                meta = payload if payload else value
                next_session_id, session_errors = _first_optional_string_field(
                    meta, ("id", "session_id")
                )
                next_session_cwd, cwd_errors = _optional_string_field(meta, "cwd")
                context_errors = tuple(
                    sorted(set(session_errors + cwd_errors))
                )
                session_id = next_session_id
                session_cwd = next_session_cwd
                codex_context_valid = not context_errors
                if context_errors:
                    markers.append(
                        LossMarker(
                            reason="schema_invalid",
                            source=SOURCE_HOST,
                            file=file,
                            byte_offset=offset,
                            ts=ts,
                            in_scope=_in_window(ts, config),
                            detail="|".join(context_errors),
                        )
                    )
                continue

            if value.get("event_type") == "gate_host_record":
                nonce, nonce_errors = _optional_string_field(value, "nonce")
                direct_session, session_errors = _optional_string_field(
                    value, "session_id"
                )
                direct_adapter, adapter_errors = _optional_string_field(
                    value, "adapter"
                )
                proof, proof_errors = _project_proof_checked(value)
                proof_schema_errors = tuple(
                    error
                    for error in proof_errors
                    if error != "project_proof_missing"
                )
                attestation, runtime, embedded, runtime_errors = (
                    _shared_result_fields(value)
                )
                protocol, protocol_errors = _optional_string_field(
                    value, "measurement_protocol_version"
                )
                key_epoch, key_epoch_errors = _optional_string_field(
                    value, "key_epoch"
                )
                ids, _id_errors = _shared_id_lists_checked(value)
                typed, scalar_errors = _typed_observation_fields(value)
                schema_errors = tuple(
                    sorted(
                        set(
                            scalar_errors
                            + nonce_errors
                            + session_errors
                            + adapter_errors
                            + proof_schema_errors
                            + runtime_errors
                            + protocol_errors
                            + key_epoch_errors
                        )
                    )
                )
                if schema_errors:
                    markers.append(
                        LossMarker(
                            reason="schema_invalid",
                            source=SOURCE_HOST,
                            file=file,
                            byte_offset=offset,
                            ts=ts,
                            session_id=direct_session,
                            adapter=direct_adapter,
                            project_proof=proof,
                            nonce=nonce,
                            in_scope=_in_window(ts, config),
                            detail="|".join(schema_errors),
                        )
                    )
                    continue
                observations.append(
                    Observation(
                        source=SOURCE_HOST,
                        file=file,
                        byte_offset=offset,
                        nonce=nonce,
                        ts=ts,
                        session_id=direct_session,
                        adapter=direct_adapter or "host",
                        attestation=attestation,
                        measurement_protocol_version=protocol,
                        project_proof=proof,
                        key_epoch=key_epoch,
                        runtime_version=runtime,
                        verdict=(
                            _gate_result_field(value, "recommendation")
                            if isinstance(
                                _gate_result_field(value, "recommendation"), str
                            )
                            else None
                        ),
                        verdict_id_lists=ids,
                        skipped=typed["skipped"],
                        observable=typed["observable"],
                        evidence_available=typed["evidence_available"],
                        progress_inserts=typed["progress_inserts"],
                        inserts=typed["inserts"],
                        linked_cited_insert=typed["linked_cited_insert"],
                        cited_edge_activity=typed["cited_edge_activity"],
                        touches=typed["touches"],
                        embedded_conflict_reasons=embedded,
                        legacy_project=(
                            proof is None
                            and not proof_errors
                            and bool(
                                value.get("project") or value.get("project_key")
                            )
                        ),
                        pre_nonce=nonce is None,
                        raw_sha256=_sha256(
                            json.dumps(
                                value,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ),
                    )
                )
                continue

            message = (
                value.get("message")
                if isinstance(value.get("message"), Mapping)
                else {}
            )
            content = (
                message.get("content")
                if isinstance(message.get("content"), list)
                else []
            )
            claude_session, claude_session_errors = _optional_string_field(
                value, "sessionId"
            )
            claude_cwd, claude_cwd_errors = _optional_string_field(value, "cwd")
            claude_context_errors = tuple(
                sorted(set(claude_session_errors + claude_cwd_errors))
            )
            if claude_context_errors:
                markers.append(
                    LossMarker(
                        reason="schema_invalid",
                        source=SOURCE_HOST,
                        file=file,
                        byte_offset=offset,
                        ts=ts,
                        adapter="claude",
                        in_scope=_in_window(ts, config),
                        detail="|".join(claude_context_errors),
                    )
                )
                continue
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") == "tool_use":
                    call_id, call_id_errors = _optional_string_field(block, "id")
                    if call_id_errors:
                        if str(block.get("name") or "") not in gate_tool_names:
                            # Malformed metadata on an unrelated tool is not
                            # loss in the gate measurement stream.
                            continue
                        markers.append(
                            LossMarker(
                                reason="schema_invalid",
                                source=SOURCE_HOST,
                                file=file,
                                byte_offset=offset,
                                ts=ts,
                                session_id=claude_session,
                                adapter="claude",
                                project_proof=(
                                    project_proof_context.prove(claude_cwd)
                                    if claude_cwd
                                    and project_proof_context is not None
                                    else None
                                ),
                                in_scope=_in_window(ts, config),
                                detail="|".join(call_id_errors),
                            )
                        )
                        continue
                    if call_id:
                        key = context_key(
                            "claude", claude_cwd, claude_session, call_id
                        )
                        if str(block.get("name") or "") in gate_tool_names:
                            calls[key].append(
                                (
                                    file,
                                    offset,
                                    ts,
                                    block,
                                    claude_session,
                                    claude_cwd,
                                    "claude",
                                )
                            )
                        else:
                            unrelated_calls.add(key)
                elif block.get("type") == "tool_result":
                    call_id, call_id_errors = _optional_string_field(
                        block, "tool_use_id"
                    )
                    if call_id_errors:
                        if _gate_result_payload(block.get("content")) is None:
                            # Without a joinable id, only a structurally
                            # identifiable gate result belongs to this stream.
                            continue
                        markers.append(
                            LossMarker(
                                reason="schema_invalid",
                                source=SOURCE_HOST,
                                file=file,
                                byte_offset=offset,
                                ts=ts,
                                session_id=claude_session,
                                adapter="claude",
                                project_proof=(
                                    project_proof_context.prove(claude_cwd)
                                    if claude_cwd
                                    and project_proof_context is not None
                                    else None
                                ),
                                in_scope=_in_window(ts, config),
                                detail="|".join(call_id_errors),
                            )
                        )
                        continue
                    if call_id:
                        key = context_key(
                            "claude", claude_cwd, claude_session, call_id
                        )
                        results[key].append(
                            (
                                file,
                                offset,
                                ts,
                                block.get("content"),
                                value,
                                "claude",
                                claude_cwd,
                            )
                        )

            if payload_type in {"function_call", "custom_tool_call"}:
                if not codex_context_valid:
                    continue
                call_id, call_id_errors = _first_optional_string_field(
                    payload, ("call_id", "id")
                )
                if call_id_errors:
                    if not _codex_gate_call(payload, gate_tool_names):
                        # Malformed metadata on an unrelated tool is not
                        # loss in the gate measurement stream.
                        continue
                    markers.append(
                        LossMarker(
                            reason="schema_invalid",
                            source=SOURCE_HOST,
                            file=file,
                            byte_offset=offset,
                            ts=ts,
                            session_id=session_id,
                            adapter="codex",
                            project_proof=(
                                project_proof_context.prove(session_cwd)
                                if session_cwd
                                and project_proof_context is not None
                                else None
                            ),
                            in_scope=_in_window(ts, config),
                            detail="|".join(call_id_errors),
                        )
                    )
                    continue
                if call_id:
                    key = context_key("codex", session_cwd, session_id, call_id)
                    if _codex_gate_call(payload, gate_tool_names):
                        calls[key].append(
                            (
                                file,
                                offset,
                                ts,
                                payload,
                                session_id,
                                session_cwd,
                                "codex",
                            )
                        )
                    else:
                        unrelated_calls.add(key)
            elif payload_type in {
                "function_call_output",
                "custom_tool_call_output",
            }:
                if not codex_context_valid:
                    continue
                call_id, call_id_errors = _optional_string_field(
                    payload, "call_id"
                )
                if call_id_errors:
                    if _gate_result_payload(payload.get("output")) is None:
                        # Without a joinable id, only a structurally
                        # identifiable gate result belongs to this stream.
                        continue
                    markers.append(
                        LossMarker(
                            reason="schema_invalid",
                            source=SOURCE_HOST,
                            file=file,
                            byte_offset=offset,
                            ts=ts,
                            session_id=session_id,
                            adapter="codex",
                            project_proof=(
                                project_proof_context.prove(session_cwd)
                                if session_cwd
                                and project_proof_context is not None
                                else None
                            ),
                            in_scope=_in_window(ts, config),
                            detail="|".join(call_id_errors),
                        )
                    )
                    continue
                if call_id:
                    key = context_key("codex", session_cwd, session_id, call_id)
                    results[key].append(
                        (
                            file,
                            offset,
                            ts,
                            payload.get("output"),
                            value,
                            "codex",
                            session_cwd,
                        )
                    )

    all_keys = set(calls) | (set(results) - unrelated_calls)
    for key in sorted(all_keys, key=lambda item: tuple(str(part) for part in item)):
        call_rows = calls.get(key, [])
        result_rows = results.get(key, [])
        if not call_rows:
            for (
                result_file,
                result_offset,
                result_ts,
                output,
                _raw,
                result_adapter,
                result_cwd,
            ) in result_rows:
                # A result with no discovered call is loss evidence only when
                # its output structurally identifies a gate result. Ordinary
                # tool outputs are outside this measurement stream.
                if _gate_result_payload(output) is None:
                    continue
                markers.append(
                    LossMarker(
                        reason="host_call_missing",
                        source=SOURCE_HOST,
                        file=result_file,
                        byte_offset=result_offset,
                        ts=result_ts,
                        session_id=key[2] or None,
                        adapter=result_adapter,
                        project_proof=(
                            project_proof_context.prove(result_cwd)
                            if result_cwd and project_proof_context is not None
                            else None
                        ),
                        in_scope=_in_window(result_ts, config),
                    )
                )
            continue
        if not result_rows:
            for (
                call_file,
                call_offset,
                call_ts,
                _payload,
                call_session,
                call_cwd,
                adapter,
            ) in call_rows:
                markers.append(
                    LossMarker(
                        reason="host_call_output_missing",
                        source=SOURCE_HOST,
                        file=call_file,
                        byte_offset=call_offset,
                        ts=call_ts,
                        session_id=call_session,
                        adapter=adapter,
                        project_proof=(
                            project_proof_context.prove(call_cwd)
                            if call_cwd and project_proof_context is not None
                            else None
                        ),
                        in_scope=_in_window(call_ts, config),
                    )
                )
            continue

        for call in call_rows:
            (
                call_file,
                call_offset,
                call_ts,
                call_payload,
                call_session,
                call_cwd,
                adapter,
            ) = call
            host_scope_project_proof = (
                project_proof_context.prove(call_cwd)
                if call_cwd and project_proof_context is not None
                else None
            )
            for (
                _result_file,
                _result_offset,
                _result_ts,
                output,
                raw_record,
                _result_adapter,
                _result_cwd,
            ) in result_rows:
                result = _gate_result_payload(output)
                nonce, nonce_errors = _first_optional_string_field(
                    result or {}, ("gate_call_id", "nonce")
                )
                declared_session, session_errors = _optional_string_field(
                    result or {}, "session_id"
                )
                proof, proof_errors = _project_proof_checked(result or {})
                if proof is None and not proof_errors:
                    proof = host_scope_project_proof
                proof_schema_errors = tuple(
                    error
                    for error in proof_errors
                    if error != "project_proof_missing"
                )
                attestation, runtime, embedded, runtime_errors = (
                    _shared_result_fields(result)
                )
                if key in unrelated_calls:
                    embedded = tuple(
                        sorted(
                            {
                                *embedded,
                                "call_identity_reused_by_unrelated_tool",
                            }
                        )
                    )
                protocol, protocol_errors = _optional_string_field(
                    result or {}, "measurement_protocol_version"
                )
                key_epoch, key_epoch_errors = _optional_string_field(
                    result or {}, "key_epoch"
                )
                ids, _id_errors = _shared_id_lists_checked(result)
                typed, scalar_errors = _typed_observation_fields(result)
                adapter_errors: tuple[str, ...] = ()
                declared_adapter = (result or {}).get("host_adapter")
                if declared_adapter is not None:
                    if declared_adapter not in {"claude", "codex", "cursor"}:
                        adapter_errors = ("invalid_host_adapter",)
                    elif declared_adapter != adapter:
                        embedded = tuple(
                            sorted({*embedded, "host_adapter_mismatch"})
                        )
                if proof is not None and host_scope_project_proof is not None:
                    scope_status = compare_project_proofs(
                        proof, host_scope_project_proof
                    )
                    if scope_status == PROJECT_FOREIGN:
                        embedded = tuple(
                            sorted({*embedded, "project_scope_mismatch"})
                        )
                if (
                    declared_session is not None
                    and call_session is not None
                    and declared_session != call_session
                ):
                    embedded = tuple(sorted({*embedded, "session_mismatch"}))
                schema_errors = tuple(
                    sorted(
                        set(
                            scalar_errors
                            + adapter_errors
                            + nonce_errors
                            + session_errors
                            + proof_schema_errors
                            + runtime_errors
                            + protocol_errors
                            + key_epoch_errors
                        )
                    )
                )
                if schema_errors:
                    markers.append(
                        LossMarker(
                            reason="schema_invalid",
                            source=SOURCE_HOST,
                            file=call_file,
                            byte_offset=call_offset,
                            ts=call_ts,
                            session_id=call_session,
                            adapter=adapter,
                            project_proof=proof,
                            host_scope_project_proof=host_scope_project_proof,
                            nonce=nonce,
                            in_scope=_in_window(call_ts, config),
                            detail="|".join(schema_errors),
                        )
                    )
                    continue
                recommendation = _gate_result_field(result, "recommendation")
                observations.append(
                    Observation(
                        source=SOURCE_HOST,
                        file=call_file,
                        byte_offset=call_offset,
                        nonce=nonce,
                        ts=call_ts,
                        session_id=call_session,
                        adapter=adapter,
                        attestation=attestation,
                        measurement_protocol_version=protocol,
                        project_proof=proof,
                        host_scope_project_proof=host_scope_project_proof,
                        key_epoch=key_epoch,
                        runtime_version=runtime,
                        verdict=(
                            recommendation
                            if isinstance(recommendation, str)
                            else None
                        ),
                        verdict_id_lists=ids,
                        skipped=typed["skipped"],
                        observable=typed["observable"],
                        evidence_available=typed["evidence_available"],
                        progress_inserts=typed["progress_inserts"],
                        inserts=typed["inserts"],
                        linked_cited_insert=typed["linked_cited_insert"],
                        cited_edge_activity=typed["cited_edge_activity"],
                        touches=typed["touches"],
                        embedded_conflict_reasons=embedded,
                        legacy_project=(
                            proof is None
                            and not proof_errors
                            and bool(
                                (result or {}).get("project")
                                or (result or {}).get("project_key")
                            )
                        ),
                        pre_nonce=nonce is None,
                        raw_sha256=_sha256(
                            json.dumps(
                                {
                                    "call": call_payload,
                                    "result": raw_record,
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ),
                    )
                )

    observations.sort(key=lambda row: (row.file, row.byte_offset, row.raw_sha256 or ""))
    return list(_deduplicate_identical(observations)), markers


def parse_host_record_bytes(
    data: bytes,
    *,
    file: str,
    config: MeasurementConfig,
    project_proof_context: ProjectProofContext | None = None,
    vault_key: bytes | None = None,
) -> tuple[list[Observation], list[LossMarker]]:
    """Parse one supplied host snapshot through the union parser."""

    return parse_host_record_segments(
        ((file, data),),
        config=config,
        project_proof_context=project_proof_context,
        vault_key=vault_key,
    )


def _source_path_matches(path: Path, source: str) -> bool:
    name = path.name
    if source == SOURCE_GATE:
        return name.startswith("gate-") and name.endswith(".log")
    if source == SOURCE_HOST:
        if name.startswith("rollout-") and name.endswith(".jsonl"):
            return True
        parts = set(path.parts)
        return (
            name.endswith(".jsonl")
            and ".claude" in parts
            and "projects" in parts
        )
    return False


def discover_source_files(
    roots: Sequence[str | os.PathLike[str]], source: str
) -> tuple[tuple[Path, ...], int]:
    """Discover only pinned source formats and count traversal failures."""

    files: set[Path] = set()
    errors = 0

    def onerror(_error: OSError) -> None:
        nonlocal errors
        errors += 1

    for raw_root in roots:
        root = Path(raw_root)
        try:
            if root.is_file():
                if _source_path_matches(root, source):
                    files.add(root)
                continue
            if not root.is_dir():
                continue
            for directory, _dirs, names in os.walk(root, onerror=onerror):
                for name in names:
                    path = Path(directory) / name
                    if _source_path_matches(path, source):
                        files.add(path)
        except OSError:
            errors += 1
    return tuple(sorted(files, key=lambda path: os.fspath(path))), errors


def enumerate_source_files(
    roots: Sequence[str | os.PathLike[str]], source: str
) -> tuple[Path, ...]:
    """Compatibility wrapper over source-specific discovery."""

    return discover_source_files(roots, source)[0]


def take_snapshot(
    path: Path,
    source: str,
    *,
    snapshot_taken: datetime,
    read_bytes: Callable[[Path], bytes] | None = None,
    retries: int = 3,
) -> tuple[bytes | None, SnapshotReceipt]:
    """Double-enumerate full bytes; retry changed files up to three times."""

    reader = read_bytes or (lambda item: item.read_bytes())
    last_first = last_second = ""
    for attempt in range(1, retries + 1):
        first = reader(path)
        second = reader(path)
        last_first, last_second = _sha256(first), _sha256(second)
        if last_first == last_second:
            return second, SnapshotReceipt(
                source=source,
                file=os.fspath(path),
                first_sha256=last_first,
                second_sha256=last_second,
                snapshot_taken=snapshot_taken,
                attempts=attempt,
                stable=True,
            )
    return None, SnapshotReceipt(
        source=source,
        file=os.fspath(path),
        first_sha256=last_first,
        second_sha256=last_second,
        snapshot_taken=snapshot_taken,
        attempts=retries,
        stable=False,
    )


def _file_date(path: Path) -> date | None:
    # S1 scope is encoded by the daily gate-log filename. Parent directories
    # may contain unrelated archival dates and must never override it.
    match = re.search(r"(20\d{2})[-_](\d{2})[-_](\d{2})", path.name)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _dated_source_marker_ts(source: str, file: str) -> datetime | None:
    """Return a conservative lower-bound coordinate for a dated S1 file."""

    if source != SOURCE_GATE:
        return None
    file_day = _file_date(Path(file))
    if file_day is None:
        return None
    return datetime.combine(file_day, datetime.min.time(), tzinfo=timezone.utc)


def _source_record_in_scope(
    source: str,
    file: str,
    ts: datetime | None,
    config: MeasurementConfig,
) -> bool:
    if ts is not None:
        return _in_window(ts, config)
    if source == SOURCE_GATE:
        day_start = _dated_source_marker_ts(source, file)
        if day_start is not None:
            return day_start < config.cap and day_start + timedelta(days=1) > config.t0
    return True


def _observation_in_scope(
    observation: Observation, config: MeasurementConfig
) -> bool:
    return _source_record_in_scope(
        observation.source,
        observation.file,
        observation.ts,
        config,
    )


def _malformed_region_in_scope(
    file: str,
    records: Sequence[tuple[int, Mapping[str, Any] | None, str | None]],
    index: int,
    source: str,
    config: MeasurementConfig,
) -> bool:
    """Scope malformed bytes only when their file/range proves placement.

    S1 is a daily stream, so a dated filename proves its UTC day. S2 files can
    be resumed long after their path date: only parseable timestamps on *both*
    sides bound a malformed region. A tail append, head region, or otherwise
    unbounded region remains conservatively in scope.
    """

    if source == SOURCE_GATE:
        file_day = _file_date(Path(file))
        if file_day is not None:
            day_start = datetime.combine(
                file_day, datetime.min.time(), tzinfo=timezone.utc
            )
            day_end = day_start + timedelta(days=1)
            return day_start < config.cap and day_end > config.t0

    def record_ts(
        record: tuple[int, Mapping[str, Any] | None, str | None]
    ) -> datetime | None:
        value = record[1]
        if value is None:
            return None
        return _utc(value.get("timestamp") or value.get("ts"))

    before = next(
        (ts for row in reversed(records[:index]) if (ts := record_ts(row))),
        None,
    )
    after = next(
        (ts for row in records[index + 1 :] if (ts := record_ts(row))),
        None,
    )
    if before is None or after is None:
        return True
    lower, upper = sorted((before, after))
    return lower < config.cap and upper >= config.t0


def _apply_outcome_evidence(
    observations: Sequence[Observation],
    preliminary_receipts: tuple[InvocationReceipt, ...],
    stable_source_bytes: Mapping[str, tuple[tuple[str, bytes], ...]],
    config: MeasurementConfig,
    resolver: Callable[
        [
            tuple[InvocationReceipt, ...],
            Mapping[str, tuple[tuple[str, bytes], ...]],
            MeasurementConfig,
        ],
        Iterable[OutcomeEvidence],
    ],
) -> tuple[list[Observation], list[LossMarker]]:
    """Attach exact receipt-identity evidence; resolver failure is fail-closed.

    S2 owns the authoritative host/session coordinate for a joined receipt.
    Once evidence resolves against that coordinate, propagate it to every
    observation folded into the receipt, including an S1 row whose optional
    session field was absent.  Per-row session lookup would incorrectly turn
    that otherwise confirmatory invocation into instrument-unavailable.
    """

    rows = tuple(observations)
    try:
        resolved = tuple(
            resolver(preliminary_receipts, stable_source_bytes, config)
        )
    except Exception as exc:  # Resolver errors are evidence loss, never outcomes.
        sessions = sorted({row.session_id for row in rows if row.session_id}) or [None]
        return (
            [
                replace(row, observable=None, evidence_available=False)
                for row in rows
            ],
            [
                LossMarker(
                    reason="evidence_resolver_failed",
                    session_id=session,
                    in_scope=True,
                    detail=type(exc).__name__,
                )
                for session in sessions
            ],
        )

    # A resolver can legitimately return an unavailable result for one
    # gate-only or otherwise incomplete receipt.  Keep that failure local:
    # evidence with no exact namespace cannot be attached to any observation,
    # while an unrelated exact result remains usable.  Duplicate exact
    # identities likewise invalidate only that identity rather than the whole
    # cohort.
    by_identity: dict[tuple[str, str, str, str], OutcomeEvidence] = {}
    invalid_identities: set[tuple[str, str, str, str]] = set()
    for evidence in resolved:
        try:
            identity = (
                evidence.nonce,
                evidence.adapter,
                evidence.project_fingerprint,
                evidence.session_id,
            )
            if any(
                not isinstance(value, str) or not value
                for value in identity
            ):
                continue
            key = identity
        except Exception:
            # Without an exact namespace, the malformed item cannot be tied
            # to (and therefore cannot censor) any particular receipt.
            continue
        try:
            scalar_fields_valid = not (
                not isinstance(evidence.observable, bool)
                or not isinstance(evidence.evidence_available, bool)
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    for value in (
                        evidence.progress_inserts,
                        evidence.inserts,
                        evidence.touches,
                    )
                )
                or not isinstance(evidence.linked_cited_insert, bool)
                or not isinstance(evidence.cited_edge_activity, bool)
            )
        except Exception:
            scalar_fields_valid = False
        if not scalar_fields_valid:
            # The item is matchable but unusable. Invalidate only its exact
            # identity, including any otherwise-valid duplicate result.
            by_identity.pop(key, None)
            invalid_identities.add(key)
            continue
        try:
            if key in invalid_identities:
                continue
            if key in by_identity:
                by_identity.pop(key, None)
                invalid_identities.add(key)
                continue
            by_identity[key] = evidence
        except Exception:
            # A malformed resolver item can censor only the receipt it fails
            # to match; it must not abort evidence application for its peers.
            continue

    # Preliminary receipts intentionally deduplicate identical observations
    # without considering file/offset. Reapply evidence over a matching
    # coordinate-independent source identity that also excludes resolver-owned
    # outputs: an immutable receipt contains the prior run's resolved values,
    # while newly parsed rows still contain their unresolved defaults.
    evidence_by_observation: dict[tuple[Any, ...], OutcomeEvidence] = {}
    invalid_observations: set[tuple[Any, ...]] = set()
    for receipt in preliminary_receipts:
        candidates = {
            (
                observation.adapter,
                observation.project_proof.get("fingerprint"),
            )
            for observation in _boundary_observations(receipt)
            if observation.adapter
            and isinstance(observation.project_proof, Mapping)
            and isinstance(
                observation.project_proof.get("fingerprint"), str
            )
        }
        if len(candidates) != 1 or not receipt.session_id:
            continue
        adapter, fingerprint = next(iter(candidates))
        evidence = by_identity.get(
            (
                receipt.nonce,
                adapter,
                fingerprint,
                receipt.session_id,
            )
        )
        if evidence is None:
            continue
        for observation in receipt.observations:
            observation_key = _observation_evidence_join_key(observation)
            if observation_key in invalid_observations:
                continue
            prior = evidence_by_observation.get(observation_key)
            if prior is not None and prior != evidence:
                evidence_by_observation.pop(observation_key, None)
                invalid_observations.add(observation_key)
                continue
            evidence_by_observation[observation_key] = evidence

    result: list[Observation] = []
    for row in rows:
        evidence = evidence_by_observation.get(
            _observation_evidence_join_key(row)
        )
        if evidence is None:
            result.append(replace(row, observable=None, evidence_available=False))
            continue
        result.append(
            replace(
                row,
                observable=evidence.observable,
                evidence_available=evidence.evidence_available,
                progress_inserts=evidence.progress_inserts,
                inserts=evidence.inserts,
                linked_cited_insert=evidence.linked_cited_insert,
                cited_edge_activity=evidence.cited_edge_activity,
                touches=evidence.touches,
            )
        )
    return result, []


def measure(
    source_roots: Mapping[str, Sequence[str | os.PathLike[str]]],
    config: MeasurementConfig,
    *,
    prior_receipts: Iterable[InvocationReceipt | ReceiptLineage] = (),
    vault_key: bytes | None = None,
    project_proof_context: ProjectProofContext | None = None,
    now: datetime | None = None,
    read_bytes: Callable[[Path], bytes] | None = None,
    enumerate_files: Callable[
        [Sequence[str | os.PathLike[str]]], tuple[Path, ...]
    ] | None = None,
    evidence_resolver: Callable[
        [
            tuple[InvocationReceipt, ...],
            Mapping[str, tuple[tuple[str, bytes], ...]],
            MeasurementConfig,
        ],
        Iterable[OutcomeEvidence],
    ] | None = None,
) -> AuditState:
    """Measure pinned roots, optionally resolve evidence, and fold a generation.

    Raw S1/S2 parsing is fail-closed: absent observability/evidence fields yield
    ``CENSORED``.  A resolver must return exact nonce+session evidence before
    the classifier can mint a clean outcome.
    """

    if vault_key is not None:
        raise ValueError(
            "raw vault-key proof derivation is disabled; pass ProjectProofContext"
        )

    now = _utc(now) or datetime.now(timezone.utc)
    config = replace(
        config,
        cap_reached=(
            now >= config.cap + timedelta(seconds=FRESHNESS_SECONDS)
        ),
    )
    prior_rows = tuple(prior_receipts)
    observations: list[Observation] = []
    markers: list[LossMarker] = []
    checks: list[GateRowCheck] = []
    snapshots: list[SnapshotReceipt] = []
    health: list[SourceHealth] = []
    completeness: list[CandidateCompletenessReceipt] = []
    invalidations: list[str] = []
    stable_source_bytes: dict[str, list[tuple[str, bytes]]] = {
        source: [] for source in SOURCES
    }

    actual_roots = {
        source: tuple(os.fspath(root) for root in roots)
        for source, roots in source_roots.items()
    }
    expected_roots = {
        source: tuple(os.fspath(root) for root in roots)
        for source, roots in config.source_roots.items()
    }
    if actual_roots != expected_roots:
        invalidations.append("source_roots_not_pinned")
    files_by_source: dict[str, tuple[Path, ...]] = {}
    dates_by_source: dict[str, set[date]] = defaultdict(set)
    for source in SOURCES:
        roots = tuple(actual_roots.get(source, ()))
        if not roots:
            invalidations.append(f"source_roots_missing:{source}")
        unavailable_roots = tuple(root for root in roots if not Path(root).exists())
        if unavailable_roots:
            invalidations.append(f"source_root_unavailable:{source}")

        def enumerate_pass() -> tuple[tuple[Path, ...], int]:
            if enumerate_files is None:
                return discover_source_files(roots, source)
            try:
                return tuple(enumerate_files(roots)), 0
            except OSError:
                return (), 1

        files, traversal_errors = enumerate_pass()
        inventory_stable = False
        for _attempt in range(3):
            second_inventory, pass_errors = enumerate_pass()
            traversal_errors = max(traversal_errors, pass_errors)
            if second_inventory == files:
                inventory_stable = True
                break
            files = second_inventory
        missing = len(unavailable_roots) + (1 if not roots else 0)
        unreadable = traversal_errors
        malformed = unstable = parsed_n = 0
        hashes: list[tuple[str, str]] = []
        host_segments: list[tuple[str, bytes]] = []
        data: bytes | None = None
        for path in files:
            token = os.fspath(path)
            file_in_scope = _source_record_in_scope(
                source, token, None, config,
            )
            try:
                data, snapshot = take_snapshot(
                    path,
                    source,
                    snapshot_taken=now,
                    read_bytes=read_bytes,
                )
            except OSError as exc:
                if file_in_scope:
                    unreadable += 1
                markers.append(
                    LossMarker(
                        reason="unreadable_file",
                        source=source,
                        file=token,
                        ts=_dated_source_marker_ts(source, token),
                        in_scope=file_in_scope,
                        detail=type(exc).__name__,
                    )
                )
                continue
            snapshots.append(snapshot)
            if not snapshot.stable or data is None:
                if file_in_scope:
                    unstable += 1
                markers.append(
                    LossMarker(
                        reason="snapshot_changed",
                        source=source,
                        file=token,
                        ts=_dated_source_marker_ts(source, token),
                        in_scope=file_in_scope,
                    )
                )
                continue
            hashes.append((token, snapshot.second_sha256))
            parsed_n += 1
            if source == SOURCE_GATE:
                parsed_rows, parse_markers, parse_checks = parse_gate_log_bytes(
                    data, file=token, config=config
                )
                observations.extend(parsed_rows)
                markers.extend(parse_markers)
                checks.extend(parse_checks)
                malformed += sum(
                    row.reason == "schema_invalid"
                    and row.in_scope
                    and not _marker_is_proven_foreign(row, config)
                    for row in parse_markers
                )
            else:
                host_segments.append((token, data))
                parsed_rows, parse_markers = [], []
            for row in parsed_rows:
                if (
                    row.ts is not None
                    and _in_window(row.ts, config)
                    and compare_project_proofs(
                        row.project_proof, config.target_project_proof
                    )
                    == PROJECT_MATCH
                ):
                    dates_by_source[source].add(row.ts.date())

        if source == SOURCE_HOST:
            parsed_rows, parse_markers = parse_host_record_segments(
                host_segments,
                config=config,
                project_proof_context=project_proof_context,
            )
            observations.extend(parsed_rows)
            markers.extend(parse_markers)
            malformed += sum(
                row.reason == "schema_invalid"
                and row.in_scope
                and not _marker_is_proven_foreign(row, config)
                for row in parse_markers
            )
            for row in parsed_rows:
                if (
                    row.ts is not None
                    and _in_window(row.ts, config)
                    and compare_project_proofs(
                        row.project_proof, config.target_project_proof
                    )
                    == PROJECT_MATCH
                ):
                    dates_by_source[source].add(row.ts.date())

        # Parsing is complete. Drop the initial snapshots before the terminal
        # full-hash pass reads the same corpus again; retaining S2 segments (or
        # the loop's final S1/S2 value) would briefly double corpus residency.
        host_segments.clear()
        data = None

        # Couple a terminal inventory to a final full-file hash pass. A stable
        # filename set alone is insufficient because files may be rewritten
        # after their initial double-read snapshot.
        post_inventory, post_errors = enumerate_pass()
        traversal_errors = max(traversal_errors, post_errors)
        if post_inventory != files:
            inventory_stable = False
        final_hash_clean = inventory_stable
        hash_by_file = dict(hashes)
        final_data: dict[str, bytes] = {}
        if inventory_stable:
            reader = read_bytes or (lambda item: item.read_bytes())
            for path in post_inventory:
                token = os.fspath(path)
                file_in_scope = _source_record_in_scope(
                    source, token, None, config,
                )
                try:
                    terminal_bytes = reader(path)
                except OSError as exc:
                    if file_in_scope:
                        unreadable += 1
                        final_hash_clean = False
                    markers.append(
                        LossMarker(
                            reason="final_hash_unreadable",
                            source=source,
                            file=token,
                            ts=_dated_source_marker_ts(source, token),
                            in_scope=file_in_scope,
                            detail=type(exc).__name__,
                        )
                    )
                    continue
                if _sha256(terminal_bytes) != hash_by_file.get(token):
                    if file_in_scope:
                        unstable += 1
                        final_hash_clean = False
                    markers.append(
                        LossMarker(
                            reason="final_hash_changed",
                            source=source,
                            file=token,
                            ts=_dated_source_marker_ts(source, token),
                            in_scope=file_in_scope,
                        )
                    )
                    continue
                final_data[token] = terminal_bytes
            terminal_inventory, terminal_errors = enumerate_pass()
            traversal_errors = max(traversal_errors, terminal_errors)
            if terminal_inventory != post_inventory:
                inventory_stable = False
                final_hash_clean = False
                post_inventory = terminal_inventory
        files = post_inventory
        if traversal_errors > unreadable:
            unreadable = traversal_errors
        if traversal_errors:
            markers.append(
                LossMarker(
                    reason="source_traversal_error",
                    source=source,
                    file=None,
                    in_scope=True,
                    detail=f"count={traversal_errors}",
                )
            )
        if inventory_stable and final_hash_clean:
            stable_source_bytes[source].extend(
                (token, final_data[token]) for token in sorted(final_data)
            )
        files_by_source[source] = files
        if not inventory_stable:
            unstable += 1
            markers.append(
                LossMarker(
                    reason="candidate_inventory_changed",
                    source=source,
                    file="|".join(roots),
                    in_scope=True,
                )
            )
        row_health = SourceHealth(
            source=source,
            roots=roots,
            files_seen=len(files),
            files_parsed=parsed_n,
            missing_files=missing,
            unreadable_files=unreadable,
            malformed_regions=malformed,
            unstable_files=unstable,
        )
        health.append(row_health)
        completeness.append(
            CandidateCompletenessReceipt(
                source=source,
                roots=roots,
                enumerated_files=tuple(os.fspath(path) for path in files),
                stable_file_hashes=tuple(sorted(hashes)),
                complete=inventory_stable
                and final_hash_clean
                and not (missing or unreadable or malformed or unstable),
            )
        )

    # Symmetric daily-file rule: only activity in the peer source makes silence
    # a marker. Dual silence remains clean.
    for source, peer in ((SOURCE_GATE, SOURCE_HOST), (SOURCE_HOST, SOURCE_GATE)):
        missing_days = dates_by_source[peer] - dates_by_source[source]
        if missing_days:
            markers.extend(
                LossMarker(
                    reason="missing_daily_file",
                    source=source,
                    file=day.isoformat(),
                    ts=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
                    in_scope=True,
                )
                for day in sorted(missing_days)
            )
            index = SOURCES.index(source)
            health[index] = replace(
                health[index],
                missing_files=health[index].missing_files + len(missing_days),
            )

    fold_kwargs = {
        "prior_receipts": prior_rows,
        "source_health": health,
        "snapshots": snapshots,
        "loss_markers": markers,
        "gate_rows": checks,
        "candidate_completeness": completeness,
        "hard_invalidations": invalidations,
    }
    preliminary = fold_observations(
        observations,
        config,
        **fold_kwargs,
    )
    if evidence_resolver is None:
        return replace(preliminary, measurement_taken_at=now)

    observations, resolver_markers = _apply_outcome_evidence(
        observations,
        preliminary.receipts,
        {
            source: tuple(rows)
            for source, rows in stable_source_bytes.items()
        },
        config,
        evidence_resolver,
    )
    final = fold_observations(
        observations,
        config,
        **{
            **fold_kwargs,
            "loss_markers": tuple(markers) + tuple(resolver_markers),
        },
    )
    return replace(final, measurement_taken_at=now)


def _coerce_receipt(row: InvocationReceipt | Mapping[str, Any], config: MeasurementConfig) -> InvocationReceipt:
    if isinstance(row, InvocationReceipt):
        return row
    nonce = str(row.get("nonce") or row.get("gate_call_id") or row.get("invocation") or "")
    if not nonce:
        raise ValueError("receipt-like row is missing nonce/invocation identity")
    disposition = str(row.get("disposition") or "confirmatory")
    outcome = row.get("outcome") or row.get("outcome_category")
    order_key = _utc(row.get("lineage_order_key") or row.get("ts") or row.get("gate_ts"))
    if order_key is None:
        raise ValueError("receipt-like row is missing lineage_order_key/ts")
    protocol = str(
        row.get("measurement_protocol_version")
        or row.get("protocol_version")
        or config.measurement_protocol_version
    )
    return InvocationReceipt(
        nonce=nonce,
        measurement_protocol_version=protocol,
        observations=(),
        disposition=disposition,
        admitted=bool(row.get("admitted", False)),
        lineage_order_key=order_key,
        fresh_ts=_utc(row.get("fresh_ts") or order_key),
        session_id=str(row.get("session_id") or "") or None,
        verdict=str(row.get("verdict") or row.get("recommendation") or "") or None,
        outcome=str(outcome) if outcome is not None else None,
        censored_reason=(
            str(row.get("censored_reason") or row.get("censor_reason") or "") or None
        ),
        loss_reasons=tuple(row.get("loss_reasons") or ()),
        conflict_reasons=tuple(row.get("conflict_reasons") or ()),
        finalized=bool(row.get("finalized", False)),
        window_start=_utc(row.get("window_start") or order_key),
        window_end=_utc(row.get("window_end")),
        prefix_member=bool(row.get("prefix_member", False)),
    )


def audit_rows(
    receipts: Iterable[InvocationReceipt | Mapping[str, Any]],
    config: MeasurementConfig,
    *,
    gate_rows: Iterable[GateRowCheck | Mapping[str, Any]] = (),
    loss_markers: Iterable[LossMarker | Mapping[str, Any]] = (),
    source_health: Iterable[SourceHealth | Mapping[str, Any]] = (),
    candidate_completeness: Iterable[
        CandidateCompletenessReceipt | Mapping[str, Any]
    ] = (),
    hard_invalidations: Iterable[str] = (),
    measurement_taken_at: datetime | None = None,
) -> OracleResult:
    """Report adapter accepting finalized receipt-like dictionaries."""

    coerced_receipts = tuple(_coerce_receipt(row, config) for row in receipts)
    coerced_checks = tuple(
        row
        if isinstance(row, GateRowCheck)
        else GateRowCheck(
            obs_id=tuple(row.get("obs_id") or (SOURCE_GATE, "report", index)),
            ts=_utc(row.get("ts")),
            in_scope=bool(row.get("in_scope", True)),
            id_lists_valid=bool(row.get("id_lists_valid", False)),
            missing_fields=tuple(row.get("missing_fields") or ()),
        )
        for index, row in enumerate(gate_rows)
    )
    coerced_markers = tuple(
        row if isinstance(row, LossMarker) else LossMarker(**row)
        for row in loss_markers
    )
    coerced_health = tuple(
        row if isinstance(row, SourceHealth) else SourceHealth(**row)
        for row in source_health
    )
    coerced_completeness = tuple(
        row
        if isinstance(row, CandidateCompletenessReceipt)
        else CandidateCompletenessReceipt(**row)
        for row in candidate_completeness
    )
    state = AuditState(
        config=config,
        receipts=coerced_receipts,
        loss_markers=coerced_markers,
        source_health=coerced_health,
        snapshots=(),
        gate_rows=coerced_checks,
        candidate_completeness=coerced_completeness,
        hard_invalidations=tuple(hard_invalidations),
        measurement_taken_at=measurement_taken_at,
    )
    return compute_oracles(state)
