"""Pinned, read-only production runner for outcome-measurement v2.6.

This module has no scheduler, MCP registration, installer hook, or import-time
execution.  It is the integration seam for the explicit offline CLI, which
must already have the frozen manifest and both recorded live-canary receipts.
Calling it does not merge/install a runtime, execute a canary, or start T0.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Iterable, Mapping, Sequence
import uuid

import db
import outcome_evidence
import outcome_measurement
import project_proof


LINEAGE_CHECKPOINT_SCHEMA = "latch-outcome-lineage-v2"


def lineage_checkpoint_coordinate(
    config: outcome_measurement.MeasurementConfig,
    manifest: outcome_measurement.MeasurementManifest,
) -> str:
    """Bind persisted authority to one opaque project/window/manifest coordinate."""

    payload = {
        "target_project_proof": dict(sorted(config.target_project_proof.items())),
        "project_proof_version": config.project_proof_version,
        "key_epoch": config.key_epoch,
        "t0": config.t0.isoformat(),
        "cap": config.cap.isoformat(),
        "pinned_runtime_version": config.pinned_runtime_version,
        "measurement_protocol_version": config.measurement_protocol_version,
        "implementation_commit": config.implementation_commit,
        "source_roots": {
            source: sorted(os.fspath(root) for root in roots)
            for source, roots in sorted(config.source_roots.items())
        },
        "manifest_composite_sha256": manifest.composite_sha256,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PinnedAuditRun:
    """One canonical state and its deterministic, envelope-verified report."""

    state: outcome_measurement.AuditState
    report: str


def load_lineage_checkpoint(
    path: str | os.PathLike[str],
    *,
    coordinate_sha256: str,
    contract_sha256: str = outcome_measurement.CONTRACT_SHA256,
    allow_missing: bool = False,
) -> tuple[outcome_measurement.InvocationReceipt, ...]:
    """Load private structural receipt authority carried between audit runs.

    A missing checkpoint is allowed only for an explicit first-run bootstrap.
    Once a window has state, deletion must fail closed instead of silently
    resetting admission history.  The checkpoint contains normalized receipt
    metadata only, never source bytes, prompts, results, or database content.
    """

    if re.fullmatch(r"[0-9a-f]{64}", coordinate_sha256) is None:
        raise ValueError("lineage checkpoint coordinate is invalid")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        if allow_missing:
            return ()
        raise ValueError("lineage checkpoint is missing") from None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("lineage checkpoint is unavailable or invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != LINEAGE_CHECKPOINT_SCHEMA
        or payload.get("contract_sha256") != contract_sha256
        or payload.get("coordinate_sha256") != coordinate_sha256
    ):
        raise ValueError("lineage checkpoint schema or coordinate is invalid")
    if set(payload) != {
        "schema", "contract_sha256", "coordinate_sha256", "receipts",
    }:
        raise ValueError("lineage checkpoint fields are invalid")
    rows = payload.get("receipts")
    if not isinstance(rows, list):
        raise ValueError("lineage checkpoint receipts must be a list")
    try:
        receipts = [_receipt_from_checkpoint(row) for row in rows]
    except (TypeError, ValueError) as exc:
        raise ValueError("lineage checkpoint row is invalid") from exc
    identities = {
        row.identity for row in receipts
    }
    if len(identities) != len(receipts):
        raise ValueError("lineage checkpoint contains a duplicate identity")
    return tuple(receipts)


def _timestamp(value: object, *, required: bool = False) -> datetime | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("checkpoint timestamp is invalid")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("checkpoint timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("checkpoint timestamp has no timezone")
    return parsed


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("checkpoint string field is invalid")
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("checkpoint string list is invalid")
    return tuple(value)


def _proof(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise ValueError("checkpoint project proof is invalid")
    return dict(value)


def _id_lists(value: object) -> dict[str, list[int]] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("checkpoint verdict id lists are invalid")
    result: dict[str, list[int]] = {}
    for key, items in value.items():
        if (
            not isinstance(key, str)
            or not isinstance(items, list)
            or any(isinstance(item, bool) or not isinstance(item, int) for item in items)
        ):
            raise ValueError("checkpoint verdict id lists are invalid")
        result[key] = list(items)
    return result


def _observation_from_checkpoint(value: object) -> outcome_measurement.Observation:
    if not isinstance(value, dict):
        raise ValueError("checkpoint observation is invalid")
    expected = {
        "source", "file", "byte_offset", "nonce", "ts", "session_id",
        "adapter", "attestation", "measurement_protocol_version",
        "project_proof", "host_scope_project_proof", "key_epoch",
        "runtime_version", "verdict", "verdict_id_lists", "skipped",
        "observable", "evidence_available", "progress_inserts", "inserts",
        "linked_cited_insert", "cited_edge_activity", "touches",
        "embedded_conflict_reasons", "legacy_project", "hash_annotated",
        "pre_nonce", "raw_sha256",
    }
    if set(value) != expected:
        raise ValueError("checkpoint observation fields are invalid")
    for name in ("source", "file"):
        if not isinstance(value[name], str) or not value[name]:
            raise ValueError("checkpoint observation identity is invalid")
    for name in ("byte_offset", "progress_inserts", "inserts", "touches"):
        item = value[name]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError("checkpoint observation integer is invalid")
    for name in (
        "linked_cited_insert", "cited_edge_activity", "legacy_project",
        "hash_annotated", "pre_nonce",
    ):
        if not isinstance(value[name], bool):
            raise ValueError("checkpoint observation boolean is invalid")
    for name in ("skipped", "observable", "evidence_available"):
        if value[name] is not None and not isinstance(value[name], bool):
            raise ValueError("checkpoint optional boolean is invalid")
    return outcome_measurement.Observation(
        source=value["source"],
        file=value["file"],
        byte_offset=value["byte_offset"],
        nonce=_optional_string(value["nonce"]),
        ts=_timestamp(value["ts"]),
        session_id=_optional_string(value["session_id"]),
        adapter=_optional_string(value["adapter"]),
        attestation=_optional_string(value["attestation"]),
        measurement_protocol_version=_optional_string(
            value["measurement_protocol_version"]
        ),
        project_proof=_proof(value["project_proof"]),
        host_scope_project_proof=_proof(value["host_scope_project_proof"]),
        key_epoch=_optional_string(value["key_epoch"]),
        runtime_version=_optional_string(value["runtime_version"]),
        verdict=_optional_string(value["verdict"]),
        verdict_id_lists=_id_lists(value["verdict_id_lists"]),
        skipped=value["skipped"],
        observable=value["observable"],
        evidence_available=value["evidence_available"],
        progress_inserts=value["progress_inserts"],
        inserts=value["inserts"],
        linked_cited_insert=value["linked_cited_insert"],
        cited_edge_activity=value["cited_edge_activity"],
        touches=value["touches"],
        embedded_conflict_reasons=_string_tuple(value["embedded_conflict_reasons"]),
        legacy_project=value["legacy_project"],
        hash_annotated=value["hash_annotated"],
        pre_nonce=value["pre_nonce"],
        raw_sha256=_optional_string(value["raw_sha256"]),
    )


def _receipt_from_checkpoint(value: object) -> outcome_measurement.InvocationReceipt:
    if not isinstance(value, dict):
        raise ValueError("checkpoint receipt is invalid")
    expected = {
        "nonce", "measurement_protocol_version", "observations",
        "disposition", "admitted", "lineage_order_key", "fresh_ts",
        "session_id", "verdict", "outcome", "censored_reason",
        "loss_reasons", "conflict_reasons", "finalized", "window_start",
        "window_end", "prefix_member", "boundary_evidence",
    }
    if set(value) != expected:
        raise ValueError("checkpoint receipt fields are invalid")
    for name in ("nonce", "measurement_protocol_version", "disposition"):
        if not isinstance(value[name], str) or not value[name]:
            raise ValueError("checkpoint receipt identity is invalid")
    for name in ("admitted", "finalized", "prefix_member"):
        if not isinstance(value[name], bool):
            raise ValueError("checkpoint receipt boolean is invalid")
    for name in ("observations", "boundary_evidence"):
        if not isinstance(value[name], list):
            raise ValueError("checkpoint receipt observations are invalid")
    observations = tuple(
        _observation_from_checkpoint(row) for row in value["observations"]
    )
    boundary = tuple(
        _observation_from_checkpoint(row) for row in value["boundary_evidence"]
    )
    if any(
        row.nonce is not None and row.nonce != value["nonce"]
        for row in observations + boundary
    ):
        raise ValueError("checkpoint observation nonce does not match receipt")
    return outcome_measurement.InvocationReceipt(
        nonce=value["nonce"],
        measurement_protocol_version=value["measurement_protocol_version"],
        observations=observations,
        disposition=value["disposition"],
        admitted=value["admitted"],
        lineage_order_key=_timestamp(value["lineage_order_key"], required=True),
        fresh_ts=_timestamp(value["fresh_ts"]),
        session_id=_optional_string(value["session_id"]),
        verdict=_optional_string(value["verdict"]),
        outcome=_optional_string(value["outcome"]),
        censored_reason=_optional_string(value["censored_reason"]),
        loss_reasons=_string_tuple(value["loss_reasons"]),
        conflict_reasons=_string_tuple(value["conflict_reasons"]),
        finalized=value["finalized"],
        window_start=_timestamp(value["window_start"]),
        window_end=_timestamp(value["window_end"]),
        prefix_member=value["prefix_member"],
        boundary_evidence=boundary,
    )


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _observation_checkpoint_row(row: outcome_measurement.Observation) -> dict[str, object]:
    return {
        "source": row.source,
        "file": row.file,
        "byte_offset": row.byte_offset,
        "nonce": row.nonce,
        "ts": _iso(row.ts),
        "session_id": row.session_id,
        "adapter": row.adapter,
        "attestation": row.attestation,
        "measurement_protocol_version": row.measurement_protocol_version,
        "project_proof": (
            dict(row.project_proof) if row.project_proof is not None else None
        ),
        "host_scope_project_proof": (
            dict(row.host_scope_project_proof)
            if row.host_scope_project_proof is not None else None
        ),
        "key_epoch": row.key_epoch,
        "runtime_version": row.runtime_version,
        "verdict": row.verdict,
        "verdict_id_lists": (
            {name: list(items) for name, items in row.verdict_id_lists.items()}
            if row.verdict_id_lists is not None else None
        ),
        "skipped": row.skipped,
        "observable": row.observable,
        "evidence_available": row.evidence_available,
        "progress_inserts": row.progress_inserts,
        "inserts": row.inserts,
        "linked_cited_insert": row.linked_cited_insert,
        "cited_edge_activity": row.cited_edge_activity,
        "touches": row.touches,
        "embedded_conflict_reasons": list(row.embedded_conflict_reasons),
        "legacy_project": row.legacy_project,
        "hash_annotated": row.hash_annotated,
        "pre_nonce": row.pre_nonce,
        "raw_sha256": row.raw_sha256,
    }


def _receipt_checkpoint_row(
    row: outcome_measurement.InvocationReceipt,
) -> dict[str, object]:
    return {
        "nonce": row.nonce,
        "measurement_protocol_version": row.measurement_protocol_version,
        "observations": [
            _observation_checkpoint_row(item) for item in row.observations
        ],
        "disposition": row.disposition,
        "admitted": row.admitted,
        "lineage_order_key": _iso(row.lineage_order_key),
        "fresh_ts": _iso(row.fresh_ts),
        "session_id": row.session_id,
        "verdict": row.verdict,
        "outcome": row.outcome,
        "censored_reason": row.censored_reason,
        "loss_reasons": list(row.loss_reasons),
        "conflict_reasons": list(row.conflict_reasons),
        "finalized": row.finalized,
        "window_start": _iso(row.window_start),
        "window_end": _iso(row.window_end),
        "prefix_member": row.prefix_member,
        "boundary_evidence": [
            _observation_checkpoint_row(item) for item in row.boundary_evidence
        ],
    }


def _atomic_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            pass
        else:
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_lineage_checkpoint(
    path: str | os.PathLike[str],
    state: outcome_measurement.AuditState,
    *,
    coordinate_sha256: str,
    contract_sha256: str = outcome_measurement.CONTRACT_SHA256,
) -> None:
    """Atomically persist admitted structural receipt authority.

    Normalized observations contain only the coordinates and classifier/evidence
    fields already present in ``InvocationReceipt``. Raw source bytes, prompts,
    tool outputs, database rows, and report payloads are never checkpointed.
    """

    if re.fullmatch(r"[0-9a-f]{64}", coordinate_sha256) is None:
        raise ValueError("lineage checkpoint coordinate is invalid")
    by_identity: dict[
        tuple[str, str], outcome_measurement.InvocationReceipt
    ] = {}
    for receipt in state.receipts:
        if not receipt.admitted:
            continue
        existing = by_identity.get(receipt.identity)
        if existing is not None and existing != receipt:
            raise ValueError("audit state contains conflicting receipt authority")
        by_identity[receipt.identity] = receipt
    rows = [
        _receipt_checkpoint_row(row)
        for row in sorted(
            by_identity.values(),
            key=lambda item: (
                item.lineage_order_key,
                item.nonce,
                item.measurement_protocol_version,
            ),
        )
    ]
    payload = {
        "schema": LINEAGE_CHECKPOINT_SCHEMA,
        "contract_sha256": contract_sha256,
        "coordinate_sha256": coordinate_sha256,
        "receipts": rows,
    }
    _atomic_private_text(
        Path(path),
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
    )


def write_canonical_report(
    path: str | os.PathLike[str], report: str
) -> None:
    """Atomically write a private canonical report."""

    _atomic_private_text(Path(path), report)


def _deployed_implementation_commit() -> str | None:
    """Return HEAD only for an exact, clean source checkout.

    The manifest commit must be independently tied to the running source, not
    merely repeated by caller-controlled objects. Unpacked runtimes without
    exact Git metadata deliberately fail closed.
    """

    root = Path(__file__).resolve().parent.parent
    try:
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status.stdout.strip():
            return None
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None


def run_pinned_audit(
    *,
    project_path: str | os.PathLike[str],
    source_roots: Mapping[str, Sequence[str | os.PathLike[str]]],
    config: outcome_measurement.MeasurementConfig,
    contract_bytes: bytes,
    capture: outcome_measurement.CapturePin,
    manifest: outcome_measurement.MeasurementManifest,
    fixture_bytes: Mapping[str, bytes],
    canaries: Iterable[outcome_measurement.CanaryEvidence],
    prior_capture: outcome_measurement.CapturePin | None = None,
    prior_receipts: Iterable[
        outcome_measurement.InvocationReceipt | outcome_measurement.ReceiptLineage
    ] = (),
) -> PinnedAuditRun:
    """Measure stable pinned roots and render only through the full audit gate.

    The vault identity is read from the selected project's existing database;
    raw key material and recoverable project paths never enter a receipt.  All
    evidence queries share one read-only SQLite snapshot.  A mismatch between
    that derived project proof and the configured target hard-invalidates the
    report rather than selecting another project or silently continuing.
    """

    project = os.fspath(project_path)
    if not project.strip():
        raise ValueError("an explicit project path is required")
    if config.require_fresh_snapshots is not True:
        raise ValueError("canonical audits require fresh post-drain snapshots")
    canary_rows = tuple(canaries)
    prior_rows = tuple(prior_receipts)
    deployed_commit = _deployed_implementation_commit()

    conn = db.connect_readonly(project)
    try:
        conn.execute("BEGIN")
        proof_context = project_proof.ProjectProofContext.from_vault_identity(
            conn._kb_vault_identity,
            key_epoch=config.key_epoch,
        )
        derived_target = proof_context.prove(project)
        target_status = project_proof.compare_project_proofs(
            derived_target,
            config.target_project_proof,
        )
        resolver = outcome_evidence.make_receipt_evidence_resolver(conn, project)
        state = outcome_measurement.measure(
            source_roots,
            config,
            prior_receipts=prior_rows,
            project_proof_context=proof_context,
            evidence_resolver=resolver,
        )
        runner_invalidations: set[str] = set()
        if target_status != project_proof.PROJECT_MATCH:
            runner_invalidations.add(
                f"configured_target_project_proof:{target_status}"
            )
        if deployed_commit is None:
            runner_invalidations.add("deployed_implementation_commit_unavailable")
        elif config.implementation_commit != deployed_commit:
            runner_invalidations.add("deployed_implementation_commit_mismatch")
        if runner_invalidations:
            state = replace(
                state,
                hard_invalidations=tuple(sorted({
                    *state.hard_invalidations,
                    *runner_invalidations,
                })),
            )
        report = outcome_measurement.audit_report(
            state,
            contract_bytes=contract_bytes,
            capture=capture,
            manifest=manifest,
            fixture_bytes=fixture_bytes,
            prior_capture=prior_capture,
            canaries=canary_rows,
        )
        return PinnedAuditRun(state=state, report=report)
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()
