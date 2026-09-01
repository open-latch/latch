"""Production evidence resolver for canonical outcome-measurement receipts.

Boundaries, admission, and finalization remain core-owned. This module consumes
only those immutable receipt windows, exact already-snapshotted S2 bytes, and a
read-only database connection. Any unavailable input fails closed for that
receipt instead of manufacturing clean zero evidence.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from latch.store import artifacts
from latch.evals import outcome_measurement
from latch.proof import project_proof


StableSourceBytes = Mapping[str, Sequence[tuple[str, bytes]]]


@dataclass(frozen=True)
class _StableS2Index:
    snapshots: Mapping[str, bytes]
    parse_segments: tuple[tuple[str, bytes], ...]
    invalid_files: frozenset[str]
    conflicting_files: frozenset[str]


def _db_ts(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    format_string = (
        "%Y-%m-%d %H:%M:%S.%f"
        if value.microsecond
        else "%Y-%m-%d %H:%M:%S"
    )
    return value.strftime(format_string)


def _instrument_unavailable(
    receipt: outcome_measurement.InvocationReceipt,
) -> outcome_measurement.OutcomeEvidence:
    adapter, fingerprint = _receipt_namespace(receipt)
    return outcome_measurement.OutcomeEvidence(
        nonce=receipt.nonce,
        session_id=receipt.session_id or "",
        observable=False,
        evidence_available=False,
        adapter=adapter,
        project_fingerprint=fingerprint,
    )


def _evidence_unavailable(
    receipt: outcome_measurement.InvocationReceipt,
    *,
    touches: int = 0,
) -> outcome_measurement.OutcomeEvidence:
    adapter, fingerprint = _receipt_namespace(receipt)
    return outcome_measurement.OutcomeEvidence(
        nonce=receipt.nonce,
        session_id=receipt.session_id or "",
        observable=True,
        evidence_available=False,
        adapter=adapter,
        project_fingerprint=fingerprint,
        touches=touches,
    )


def _receipt_namespace(
    receipt: outcome_measurement.InvocationReceipt,
) -> tuple[str | None, str | None]:
    identities = {
        (row.adapter, row.project_proof.get("fingerprint"))
        for row in receipt.observations
        if row.source == outcome_measurement.SOURCE_HOST
        and row.nonce == receipt.nonce
        and row.adapter
        and isinstance(row.project_proof, Mapping)
        and isinstance(row.project_proof.get("fingerprint"), str)
    }
    if len(identities) != 1:
        return None, None
    return next(iter(identities))


def _exact_evidence_ids(
    receipt: outcome_measurement.InvocationReceipt,
) -> tuple[int, ...]:
    rows = [
        row
        for row in receipt.observations
        if row.source == outcome_measurement.SOURCE_GATE
        and row.nonce == receipt.nonce
    ]
    if not rows:
        raise ValueError("receipt has no exact S1 observation")
    values: list[tuple[int, ...]] = []
    for row in rows:
        ids = (row.verdict_id_lists or {}).get("evidence_ids")
        if not isinstance(ids, Sequence) or isinstance(ids, (str, bytes, bytearray)):
            raise ValueError("S1 evidence_ids are unavailable")
        if any(isinstance(item, bool) or not isinstance(item, int) for item in ids):
            raise ValueError("S1 evidence_ids are malformed")
        values.append(tuple(ids))
    if len(set(values)) != 1:
        raise ValueError("S1 evidence_ids conflict")
    return values[0]


def _stable_s2_index(
    stable_source_bytes: StableSourceBytes,
) -> _StableS2Index:
    segments = stable_source_bytes.get(outcome_measurement.SOURCE_HOST)
    if not isinstance(segments, Sequence) or isinstance(
        segments, (str, bytes, bytearray)
    ):
        raise ValueError("stable S2 bytes are unavailable")
    grouped: dict[str, list[bytes]] = {}
    invalid_files: set[str] = set()
    for item in segments:
        if (
            not isinstance(item, Sequence)
            or isinstance(item, (str, bytes, bytearray))
            or len(item) != 2
        ):
            # With no identifiable file/session this segment cannot belong to
            # a receipt. Ignore it so unrelated unknown evidence cannot poison
            # an otherwise exact-session resolution.
            continue
        file_token, data = item
        if not isinstance(file_token, str) or not file_token:
            continue
        if not isinstance(data, bytes):
            invalid_files.add(file_token)
            continue
        grouped.setdefault(file_token, []).append(data)
    snapshots: dict[str, bytes] = {}
    parse_segments: list[tuple[str, bytes]] = []
    conflicting_files: set[str] = set()
    for file_token, copies in grouped.items():
        distinct = tuple(dict.fromkeys(copies))
        parse_segments.extend((file_token, data) for data in distinct)
        if len(distinct) == 1:
            snapshots[file_token] = distinct[0]
        else:
            conflicting_files.add(file_token)
    return _StableS2Index(
        snapshots=snapshots,
        parse_segments=tuple(parse_segments),
        invalid_files=frozenset(invalid_files),
        conflicting_files=frozenset(conflicting_files),
    )


def _same_session_counts(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    start: datetime,
    end: datetime,
    evidence_ids: tuple[int, ...],
) -> tuple[int, int, bool]:
    start_text = _db_ts(start)
    end_text = _db_ts(end)
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN kind = 'progress' THEN 1 ELSE 0 END) AS progress "
        "FROM nodes WHERE session_id = ? AND created_at BETWEEN ? AND ?",
        (session_id, start_text, end_text),
    ).fetchone()
    if row is None:
        raise sqlite3.DatabaseError("same-session insert query returned no row")
    total = int(row[0] or 0)
    progress = int(row[1] or 0)
    linked = False
    if evidence_ids:
        placeholders = ",".join("?" for _ in evidence_ids)
        linked_row = conn.execute(
            "SELECT COUNT(DISTINCT e.id) "
            "FROM edges e JOIN nodes n ON n.id = e.src "
            "WHERE n.session_id = ? "
            "AND n.created_at BETWEEN ? AND ? "
            "AND e.created_at BETWEEN ? AND ? "
            "AND e.status = 'active' "
            f"AND e.dst IN ({placeholders})",
            (
                session_id,
                start_text,
                end_text,
                start_text,
                end_text,
                *evidence_ids,
            ),
        ).fetchone()
        if linked_row is None:
            raise sqlite3.DatabaseError("same-session edge query returned no row")
        linked = int(linked_row[0] or 0) > 0
    return total, progress, linked


def _project_proof_context(
    conn: sqlite3.Connection,
    config: outcome_measurement.MeasurementConfig,
    supplied: project_proof.ProjectProofContext | None,
) -> project_proof.ProjectProofContext:
    context = supplied
    if context is None:
        identity = getattr(conn, "_kb_vault_identity", None)
        context = project_proof.ProjectProofContext.from_vault_identity(
            identity,
            key_epoch=config.key_epoch,
        )
    if context.key_epoch != config.key_epoch:
        raise ValueError("project proof context key epoch does not match config")
    return context


def _target_identity_resolver(
    config: outcome_measurement.MeasurementConfig,
    context: project_proof.ProjectProofContext,
) -> artifacts.ArtifactIdentityResolver:
    target = config.target_project_proof
    target_fingerprint = target.get("fingerprint")
    if not isinstance(target_fingerprint, str) or not target_fingerprint:
        raise ValueError("configured target project proof is malformed")

    def resolve(
        adapter: str,
        session_id: str,
        cwd: str | None,
        explicit_proof: Mapping[str, Any] | None,
    ) -> artifacts.ArtifactIdentity | None:
        if not adapter or not session_id:
            return None
        candidate: Mapping[str, Any] | None = explicit_proof
        if candidate is None and cwd:
            candidate = context.prove(cwd)
        if project_proof.compare_project_proofs(
            candidate, candidate,
        ) != project_proof.PROJECT_MATCH:
            return None
        fingerprint = candidate.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            return None
        return (adapter, fingerprint, session_id)

    return resolve


def _receipt_artifact_identity(
    receipt: outcome_measurement.InvocationReceipt,
    config: outcome_measurement.MeasurementConfig,
) -> tuple[artifacts.ArtifactIdentity, tuple[outcome_measurement.Observation, ...]]:
    host_rows = tuple(
        row
        for row in receipt.observations
        if row.source == outcome_measurement.SOURCE_HOST
        and row.nonce == receipt.nonce
    )
    if not host_rows:
        raise ValueError("receipt has no exact S2 observation")
    target_fingerprint = config.target_project_proof.get("fingerprint")
    if not isinstance(target_fingerprint, str) or not target_fingerprint:
        raise ValueError("configured target project proof is malformed")
    identities: set[artifacts.ArtifactIdentity] = set()
    for row in host_rows:
        if row.session_id != receipt.session_id or not row.adapter:
            raise ValueError("S2 observation identity does not match receipt")
        if (
            project_proof.compare_project_proofs(
                row.project_proof, config.target_project_proof,
            )
            != project_proof.PROJECT_MATCH
        ):
            raise ValueError("S2 observation project proof does not match target")
        identities.add((row.adapter, target_fingerprint, row.session_id))
    if len(identities) != 1:
        raise ValueError("receipt has conflicting S2 adapter/project/session identity")
    return next(iter(identities)), host_rows


def _touches_from_exact_s2(
    receipt: outcome_measurement.InvocationReceipt,
    stable_s2: _StableS2Index,
    artifact_index: artifacts.ArtifactEvidenceIndex,
    config: outcome_measurement.MeasurementConfig,
    *,
    project_path: str,
) -> int:
    session_id = receipt.session_id
    start = receipt.window_start
    end = receipt.window_end
    if not session_id or start is None or end is None:
        raise ValueError("receipt has no exact session/window")
    identity, host_rows = _receipt_artifact_identity(receipt, config)
    exact_files = {row.file for row in host_rows if row.file}
    if len(exact_files) != len({row.file for row in host_rows}):
        raise ValueError("S2 observation has no file token")

    for row in host_rows:
        file_token = row.file
        if not file_token or file_token in artifact_index.file_errors:
            raise ValueError("exact S2 snapshot bytes are unavailable")
        data = stable_s2.snapshots.get(file_token)
        if data is None:
            raise ValueError("exact S2 snapshot bytes are unavailable")
        if identity not in artifact_index.identities_by_file.get(file_token, ()):
            raise ValueError(
                "exact S2 snapshot does not carry the receipt identity"
            )
        if (
            isinstance(row.byte_offset, bool)
            or not isinstance(row.byte_offset, int)
            or row.byte_offset < 0
            or row.byte_offset >= len(data)
        ):
            raise ValueError("S2 observation offset is outside its exact snapshot")

    observed = artifacts.observe_indexed_session_artifacts(
        artifact_index,
        identity,
        project_path,
        start,
        end,
    )
    touches: set[tuple[str, str]] = set()
    for item in observed:
        repo = item.get("repo")
        path = item.get("path")
        if not isinstance(repo, str) or not isinstance(path, str):
            raise ValueError("artifact helper returned a malformed coordinate")
        touches.add((repo, path))
    return len(touches)


def resolve_receipt_evidence(
    receipts: Iterable[outcome_measurement.InvocationReceipt],
    stable_source_bytes: StableSourceBytes,
    config: outcome_measurement.MeasurementConfig | None = None,
    *,
    conn: sqlite3.Connection,
    project_path: str,
    project_proof_context: project_proof.ProjectProofContext | None = None,
) -> tuple[outcome_measurement.OutcomeEvidence, ...]:
    """Resolve same-session DB/artifact evidence for canonical receipt windows.

    ``config`` supplies only the pinned project-proof identity. This module
    deliberately computes no boundaries or admission state from it.
    """
    receipt_rows = tuple(receipts)
    try:
        if config is None:
            raise ValueError("measurement config is unavailable")
        if not isinstance(project_path, str) or not project_path.strip():
            raise ValueError("project path is unavailable")
        stable_s2 = _stable_s2_index(stable_source_bytes)
        proof_context = _project_proof_context(
            conn, config, project_proof_context,
        )
        if (
            project_proof.compare_project_proofs(
                proof_context.prove(project_path),
                config.target_project_proof,
            )
            != project_proof.PROJECT_MATCH
        ):
            raise ValueError("configured target project proof does not match project")
        artifact_index = artifacts.build_artifact_evidence_index(
            stable_s2.parse_segments,
            resolve_identity=_target_identity_resolver(config, proof_context),
            invalid_files=stable_s2.invalid_files,
            conflicting_files=stable_s2.conflicting_files,
        )
    except (AttributeError, TypeError, ValueError):
        return tuple(_instrument_unavailable(receipt) for receipt in receipt_rows)

    results: list[outcome_measurement.OutcomeEvidence] = []
    for receipt in receipt_rows:
        try:
            if (
                not receipt.nonce
                or not receipt.session_id
                or receipt.window_start is None
                or receipt.window_end is None
                or receipt.window_end < receipt.window_start
            ):
                raise ValueError("receipt evidence identity/window is unavailable")
            touches = _touches_from_exact_s2(
                receipt,
                stable_s2,
                artifact_index,
                config,
                project_path=project_path,
            )
            identity, _host_rows = _receipt_artifact_identity(
                receipt, config,
            )
        except (
            artifacts.ArtifactEvidenceError,
            TypeError,
            ValueError,
        ):
            results.append(_instrument_unavailable(receipt))
            continue

        try:
            raw_session_identities = {
                candidate
                for identities in artifact_index.identities_by_file.values()
                for candidate in identities
                if candidate[2] == receipt.session_id
            }
            if raw_session_identities != {identity}:
                raise ValueError(
                    "raw session id is ambiguous across adapter/project namespaces"
                )
            evidence_ids = _exact_evidence_ids(receipt)
            inserts, progress, linked = _same_session_counts(
                conn,
                session_id=receipt.session_id,
                start=receipt.window_start,
                end=receipt.window_end,
                evidence_ids=evidence_ids,
            )
        except Exception:
            # Exact S2 bytes parsed successfully, so the instrument is
            # observable. Database/S1 evidence loss has a distinct contract
            # censor reason and must not be collapsed into instrument loss.
            results.append(_evidence_unavailable(receipt, touches=touches))
            continue

        results.append(outcome_measurement.OutcomeEvidence(
            nonce=receipt.nonce,
            session_id=receipt.session_id,
            observable=True,
            evidence_available=True,
            adapter=identity[0],
            project_fingerprint=identity[1],
            progress_inserts=progress,
            inserts=inserts,
            linked_cited_insert=linked,
            cited_edge_activity=linked,
            touches=touches,
        ))
    return tuple(results)


def make_receipt_evidence_resolver(
    conn: sqlite3.Connection,
    project_path: str,
    *,
    project_proof_context: project_proof.ProjectProofContext | None = None,
) -> Callable[
    [
        tuple[outcome_measurement.InvocationReceipt, ...],
        StableSourceBytes,
        outcome_measurement.MeasurementConfig,
    ],
    Iterable[outcome_measurement.OutcomeEvidence],
]:
    """Bind the read-only DB/project context to the core three-argument hook."""

    def resolver(
        receipts: tuple[outcome_measurement.InvocationReceipt, ...],
        stable_source_bytes: StableSourceBytes,
        config: outcome_measurement.MeasurementConfig,
    ) -> Iterable[outcome_measurement.OutcomeEvidence]:
        return resolve_receipt_evidence(
            receipts,
            stable_source_bytes,
            config,
            conn=conn,
            project_path=project_path,
            project_proof_context=project_proof_context,
        )

    return resolver
