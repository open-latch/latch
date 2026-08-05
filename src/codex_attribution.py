"""Recover session attribution for gate calls from Codex's own transcripts.

Codex-hosted gate calls carry no ``session_id``: the host does not expose a
per-request conversation identity to a reused MCP process, and the SessionStart
marker that would supply one is not written on every build (KB id=3152, id=4018).
Without attribution those rows are permanently unlabelable by the correlator.

The v2.6 recovery path is deliberately nonce-only.  A rollout records the
``latch_gate`` tool result (including ``gate_call_id``) inside the thread whose
id is in the transcript.  Historical hash-recovered rows are a frozen pilot
corpus; production code never re-joins them.  A repeated nonce, incomplete
candidate inventory, malformed candidate region, or mixed project proof fails
closed instead of manufacturing confident attribution.

Read-only. Nothing here writes to the KB or to Codex's files.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import codex_transcript  # noqa: E402
import outcome_measurement  # noqa: E402
import project_proof     # noqa: E402


GATE_TOOL_NAMES = frozenset({"latch_gate", "kb_gate"})

# A date-bounded filename walk is not a candidate-complete index: Codex keeps
# resumed threads in their original start-day directory, even when a gate call
# is appended days later.  Discovery therefore enumerates the full rollout
# root and proves that the inventory stayed stable while it was scanned.
CANDIDATE_DISCOVERY_VERSION = "codex-rollout-full-v2"

def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _enumerate_rollout_paths(root: Path) -> tuple[list[Path], int]:
    """Enumerate the entire rollout root and count traversal failures.

    ``Path.rglob`` does not expose every directory-read failure.  ``os.walk``
    with an error callback lets the candidate-completeness receipt fail closed
    instead of silently presenting a partial candidate set as complete.
    """
    errors = 0

    def _onerror(_error: OSError) -> None:
        nonlocal errors
        errors += 1

    found: list[Path] = []
    try:
        for directory, dirnames, filenames in os.walk(
            root, topdown=True, onerror=_onerror, followlinks=False,
        ):
            dirnames.sort()
            for name in sorted(filenames):
                if name.startswith("rollout-") and name.endswith(".jsonl"):
                    found.append(Path(directory) / name)
    except OSError:
        errors += 1
    return found, errors


def _rollout_paths(
    home: Path | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[Path]:
    """Return every rollout path, irrespective of its start-day directory.

    ``start_date`` and ``end_date`` remain accepted for API compatibility but
    deliberately do not restrict discovery.  Resumed threads append later
    calls to the original file; a date-directory filter missed 98/126 observed
    calls and could manufacture false uniqueness from a partial index.
    """
    del start_date, end_date
    root = (home or codex_transcript.codex_home()) / "sessions"
    if not root.is_dir():
        return []
    return _enumerate_rollout_paths(root)[0]


def _parser_config(
    proof_context: project_proof.ProjectProofContext | None,
    target_project_path: str | Path | None,
) -> outcome_measurement.MeasurementConfig:
    """Minimal config for the shared, bytes-only S2 parser.

    Parsing does not use the runtime pin for classification.  The placeholder
    values merely satisfy the parser's typed boundary; the actual metadata is
    retained on each observation and compared by the correlator.
    """
    target = (
        proof_context.prove(target_project_path)
        if proof_context is not None and target_project_path is not None
        else {
            "version": project_proof.PROJECT_PROOF_VERSION,
            "key_epoch": "attribution-unscoped",
            "fingerprint": "0" * 64,
        }
    )
    epoch = proof_context.key_epoch if proof_context is not None else "attribution-unscoped"
    t0 = datetime(2000, 1, 1, tzinfo=timezone.utc)
    return outcome_measurement.MeasurementConfig(
        t0=t0,
        cap=t0 + timedelta(days=21),
        target_project_proof=target,
        key_epoch=epoch,
        pinned_runtime_version="attribution-parser",
        require_fresh_snapshots=False,
    )


def _read_snapshot(path: Path) -> tuple[bytes | None, str | None, bool]:
    """Read one full-file snapshot and detect concurrent replacement/growth."""
    try:
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
    except OSError:
        return None, None, False
    unstable = (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or getattr(before, "st_ino", None) != getattr(after, "st_ino", None)
    )
    return data, hashlib.sha256(data).hexdigest(), unstable


def _transcript_project_proof_from_bytes(
    data: bytes,
    proof_context: project_proof.ProjectProofContext | None,
) -> dict[str, str] | None:
    """Derive the opaque cwd proof from the same bytes used for call parsing."""
    if proof_context is None:
        return None
    for raw in data.splitlines():
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        cwd = payload.get("cwd") or obj.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            return proof_context.prove(cwd.strip())
    return None


def _observation_metadata(row: outcome_measurement.Observation) -> dict:
    return {
        "nonce": row.nonce,
        "ts": row.ts,
        "session_id": row.session_id,
        "adapter": row.adapter,
        "attestation": row.attestation,
        "measurement_protocol_version": row.measurement_protocol_version,
        "project_proof": row.project_proof,
        "host_scope_project_proof": row.host_scope_project_proof,
        "key_epoch": row.key_epoch,
        "runtime_version": row.runtime_version,
        "verdict": row.verdict,
        "verdict_id_lists": row.verdict_id_lists,
        "skipped": row.skipped,
        "observable": row.observable,
        "evidence_available": row.evidence_available,
        "progress_inserts": row.progress_inserts,
        "inserts": row.inserts,
        "linked_cited_insert": row.linked_cited_insert,
        "cited_edge_activity": row.cited_edge_activity,
        "touches": row.touches,
        "embedded_conflict_reasons": row.embedded_conflict_reasons,
        "legacy_project": row.legacy_project,
        "hash_annotated": row.hash_annotated,
        "pre_nonce": row.pre_nonce,
        "stream_coordinate": (row.file, row.byte_offset),
    }


def _scan_gate_calls_in_snapshot(
    data: bytes,
    *,
    file: str,
    config: outcome_measurement.MeasurementConfig,
) -> tuple[list[dict], dict[str, int | bool]]:
    """Parse structural gate calls and fold shared S2 result metadata.

    The shared parser owns all host wrapping semantics.  This scanner only
    records prompt-free call coordinates and validates candidate-bearing call
    arguments so malformed/missing requests make completeness fail closed.
    """
    observations, markers = outcome_measurement.parse_host_record_bytes(
        data, file=file, config=config, vault_key=None,
    )
    by_offset: dict[int, list[outcome_measurement.Observation]] = {}
    for row in observations:
        by_offset.setdefault(row.byte_offset, []).append(row)
    out: list[dict] = []
    # The shared parser is authoritative for JSON/schema validity.  Candidate
    # completeness must include every one of its schema-invalid regions, even
    # when the malformed bytes do not happen to contain a gate tool-name token.
    malformed = sum(marker.reason == "schema_invalid" for marker in markers)
    offset = 0
    for line_index, raw_with_end in enumerate(data.splitlines(keepends=True)):
        raw = raw_with_end.rstrip(b"\r\n")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            offset += len(raw_with_end)
            continue
        if not isinstance(obj, dict):
            offset += len(raw_with_end)
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            offset += len(raw_with_end)
            continue
        shared_rows = [
            row for row in by_offset.get(offset, ()) if row.adapter == "codex"
        ]
        direct_gate_call = (
            payload.get("type") in ("function_call", "custom_tool_call")
            and payload.get("name") in GATE_TOOL_NAMES
        )
        # Current Codex wraps MCP calls in an outer ``exec`` custom tool call.
        # The shared parser owns structural recognition of that JavaScript
        # envelope, so this adapter consumes its codex observation at the same
        # pinned byte coordinate instead of searching arbitrary script text.
        shared_codex_gate_call = bool(shared_rows)
        if not direct_gate_call and not shared_codex_gate_call:
            offset += len(raw_with_end)
            continue

        call_id = payload.get("call_id") or payload.get("id")
        if not isinstance(call_id, str) or not call_id:
            malformed += 1
        if direct_gate_call:
            raw_args = payload.get("arguments")
            if not isinstance(raw_args, str):
                raw_args = payload.get("input")
            decoded = None
            if isinstance(raw_args, str) and raw_args:
                try:
                    decoded = json.loads(raw_args)
                except json.JSONDecodeError:
                    decoded = None
            request = decoded.get("request") if isinstance(decoded, dict) else None
            if not isinstance(request, str) or not request.strip():
                malformed += 1

        # One call can have multiple nonidentical results. The shared parser
        # already coalesces byte-identical observations; preserve every row it
        # retains so attribution cannot manufacture nonce uniqueness by
        # collapsing a same-offset conflict.
        for shared in shared_rows or (None,):
            metadata = (
                _observation_metadata(shared) if shared is not None else None
            )
            out.append({
                "ts": _parse_ts(obj.get("timestamp")),
                "gate_call_id": shared.nonce if shared is not None else None,
                "skipped": shared.skipped if shared is not None else None,
                "line_index": line_index,
                "byte_offset": offset,
                "host_observation": metadata,
            })
        offset += len(raw_with_end)

    missing_results = sum(
        marker.reason == "host_call_output_missing" for marker in markers
    )
    return out, {
        "unreadable": False,
        "unstable": False,
        "malformed_candidate_regions": malformed,
        "missing_tool_results": missing_results,
    }


def _scan_gate_calls_in_transcript(
    path: Path,
) -> tuple[list[dict], dict[str, int | bool]]:
    """Compatibility wrapper; production indexing supplies one pinned snapshot."""
    data, _digest, unstable = _read_snapshot(path)
    if data is None:
        return [], {
            "unreadable": True,
            "unstable": False,
            "malformed_candidate_regions": 0,
            "missing_tool_results": 0,
        }
    calls, health = _scan_gate_calls_in_snapshot(
        data,
        file=str(path),
        config=_parser_config(None, None),
    )
    health["unstable"] = unstable
    return calls, health


def _gate_calls_in_transcript(path: Path) -> list[dict]:
    return _scan_gate_calls_in_transcript(path)[0]


def build_index(
    home: Path | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    *,
    proof_context: project_proof.ProjectProofContext | None = None,
    target_project_path: str | Path | None = None,
) -> dict:
    """Build a candidate-complete index of gate calls in every Codex rollout.

    ``start_date`` and ``end_date`` describe the caller's measurement window,
    not the rollout-directory search scope.  The latter is always the complete
    source root because a resumed thread remains in its start-day directory.

    ``candidate_completeness`` is count-only and contains no prompts or paths.
    Attribution refuses an index whose inventory or any full-file digest changed,
    could not be traversed/read, contained malformed candidate arguments, or
    lost a gate tool result.  Calls, cwd proof, and S2 metadata are all parsed
    from the same byte snapshot; the final digest pass prevents a middle rewrite
    from combining different file generations.

    Only ``by_nonce`` exists.  Historical hash recovery is frozen pilot data and
    is never recomputed. ``session_calls`` remains the exact, prompt-free stream
    for boundaries, including skipped and unmatched calls plus opaque project
    proof so foreign/mixed segments can be excluded.
    """
    del start_date, end_date
    by_nonce: dict[str, list[dict]] = {}
    session_calls: dict[str, list[dict]] = {}
    root = (home or codex_transcript.codex_home()) / "sessions"
    target_proof = (
        proof_context.prove(target_project_path)
        if proof_context is not None and target_project_path is not None
        else None
    )
    receipt: dict[str, object] = {
        "version": CANDIDATE_DISCOVERY_VERSION,
        "scope": "all_rollouts",
        "root_present": root.is_dir(),
        "enumerated_files": 0,
        "scanned_files": 0,
        "unreadable_files": 0,
        "unstable_files": 0,
        "content_changed_files": 0,
        "malformed_candidate_regions": 0,
        "missing_tool_results": 0,
        "session_identity_conflicts": 0,
        "unidentified_gate_files": 0,
        "traversal_errors": 0,
        "inventory_changed": False,
        "complete": False,
    }
    if not root.is_dir():
        return {
            "by_nonce": by_nonce,
            "session_calls": session_calls,
            "target_project_proof": target_proof,
            "candidate_completeness": receipt,
        }

    initial_paths, initial_errors = _enumerate_rollout_paths(root)
    receipt["enumerated_files"] = len(initial_paths)
    receipt["traversal_errors"] = initial_errors
    snapshot_hashes: dict[Path, str] = {}
    parser_config = _parser_config(proof_context, target_project_path)
    for path_order, path in enumerate(initial_paths):
        receipt["scanned_files"] = int(receipt["scanned_files"]) + 1
        data, digest, unstable = _read_snapshot(path)
        if data is None or digest is None:
            receipt["unreadable_files"] = int(receipt["unreadable_files"]) + 1
            continue
        snapshot_hashes[path] = digest
        calls, health = _scan_gate_calls_in_snapshot(
            data, file=str(path), config=parser_config,
        )
        if unstable:
            receipt["unstable_files"] = int(receipt["unstable_files"]) + 1
        receipt["malformed_candidate_regions"] = (
            int(receipt["malformed_candidate_regions"])
            + int(health["malformed_candidate_regions"])
        )
        receipt["missing_tool_results"] = (
            int(receipt["missing_tool_results"])
            + int(health["missing_tool_results"])
        )
        session_id = codex_transcript.transcript_session_id_bytes(data)
        if not session_id:
            if calls:
                receipt["unidentified_gate_files"] = (
                    int(receipt["unidentified_gate_files"]) + 1
                )
            continue
        candidate_project_proof = _transcript_project_proof_from_bytes(
            data, proof_context,
        )
        for call in calls:
            host_observation = call.get("host_observation")
            observed_session = (
                host_observation.get("session_id")
                if isinstance(host_observation, dict) else None
            )
            if observed_session and observed_session != session_id:
                receipt["session_identity_conflicts"] = (
                    int(receipt["session_identity_conflicts"]) + 1
                )
            candidate = {
                "session_id": session_id,
                "transcript_path": str(path),
                "ts": call["ts"],
                "gate_call_id": call["gate_call_id"],
                "project_proof": candidate_project_proof,
                "host_observation": host_observation,
                "source_order": (path_order, call["byte_offset"]),
            }
            nonce = call["gate_call_id"]
            if nonce:
                by_nonce.setdefault(nonce, []).append(candidate)
            session_calls.setdefault(session_id, []).append({
                "ts": call["ts"],
                "gate_call_id": call["gate_call_id"],
                "skipped": call["skipped"],
                "adapter": "codex",
                "project_proof": candidate_project_proof,
                "host_observation": host_observation,
                # Numeric source order preserves (segment_path, byte offset)
                # order without copying prompt text into the structural stream.
                "source_order": (path_order, call["byte_offset"]),
            })

    final_paths, final_errors = _enumerate_rollout_paths(root)
    receipt["traversal_errors"] = int(receipt["traversal_errors"]) + final_errors
    receipt["inventory_changed"] = initial_paths != final_paths
    if not receipt["inventory_changed"]:
        for path in final_paths:
            _data, digest, unstable = _read_snapshot(path)
            if digest is None:
                receipt["unreadable_files"] = int(receipt["unreadable_files"]) + 1
                continue
            if unstable:
                receipt["unstable_files"] = int(receipt["unstable_files"]) + 1
            if snapshot_hashes.get(path) != digest:
                receipt["content_changed_files"] = (
                    int(receipt["content_changed_files"]) + 1
                )
    receipt["complete"] = not any((
        int(receipt["traversal_errors"]),
        int(receipt["unreadable_files"]),
        int(receipt["unstable_files"]),
        int(receipt["content_changed_files"]),
        int(receipt["malformed_candidate_regions"]),
        int(receipt["missing_tool_results"]),
        int(receipt["session_identity_conflicts"]),
        int(receipt["unidentified_gate_files"]),
        bool(receipt["inventory_changed"]),
    ))
    for calls in session_calls.values():
        calls.sort(key=lambda call: (
            call["ts"] is None,
            call["ts"].timestamp() if call["ts"] is not None else float("inf"),
            call["source_order"],
        ))
    return {
        "by_nonce": by_nonce,
        "session_calls": session_calls,
        "target_project_proof": target_proof,
        "candidate_completeness": receipt,
    }


def _partition_by_project(
    candidates: list[dict],
    target_project_proof: dict | None,
    *,
    allow_legacy_unscoped: bool,
) -> tuple[list[dict], bool]:
    """Return proven matches and whether unresolved proof blocks attribution.

    Project partitioning can establish that at least one candidate belongs to
    the target, but it must never establish nonce uniqueness.  Same-nonce
    conflict detection runs over the complete candidate set first.  Missing,
    invalid, or rotated proof could still be the target, so absent a structural
    conflict it blocks attribution instead of making another candidate unique.
    """
    if target_project_proof is None and allow_legacy_unscoped:
        return list(candidates), False
    matched: list[dict] = []
    unresolved = False
    for candidate in candidates:
        status = project_proof.compare_project_proofs(
            candidate.get("project_proof"), target_project_proof,
        )
        if status == project_proof.PROJECT_MATCH:
            matched.append(candidate)
        elif status != project_proof.PROJECT_FOREIGN:
            unresolved = True
    return matched, unresolved


def _freeze_shared_value(value):
    if isinstance(value, dict):
        return tuple(
            sorted((str(key), _freeze_shared_value(item)) for key, item in value.items())
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_shared_value(item) for item in value)
    return value


def _candidate_shared_fields(candidate: dict) -> tuple:
    host = candidate.get("host_observation")
    host = host if isinstance(host, dict) else {}
    semantic_fields = (
        "nonce",
        "session_id",
        "adapter",
        "attestation",
        "measurement_protocol_version",
        "project_proof",
        "host_scope_project_proof",
        "key_epoch",
        "runtime_version",
        "verdict",
        "verdict_id_lists",
        "skipped",
        "observable",
        "evidence_available",
        "progress_inserts",
        "inserts",
        "linked_cited_insert",
        "cited_edge_activity",
        "touches",
        "embedded_conflict_reasons",
        "legacy_project",
        "hash_annotated",
        "pre_nonce",
    )
    return (
        candidate.get("session_id"),
        _freeze_shared_value(candidate.get("project_proof")),
        tuple(
            (name, _freeze_shared_value(host.get(name)))
            for name in semantic_fields
        ),
    )


def _candidate_set_conflicts(candidates: list[dict]) -> tuple[str, ...]:
    """Return closed structural conflict reasons for one exact nonce."""
    reasons: set[str] = set()
    if len({row.get("session_id") for row in candidates}) > 1:
        reasons.add("nonce_in_multiple_sessions")
    if len({_candidate_shared_fields(row) for row in candidates}) > 1:
        reasons.add("nonidentical_nonce_candidate")
    timestamps = [row.get("ts") for row in candidates if row.get("ts") is not None]
    if timestamps and (max(timestamps) - min(timestamps)).total_seconds() > 300:
        reasons.add("nonce_timestamp_conflict")
    return tuple(sorted(reasons))


def attribute(
    gate_row: dict,
    index: dict,
    project: str | None = None,
    *,
    target_project_proof: dict | None = None,
) -> dict | None:
    """Attribute one session-less gate row by exact nonce, or return None.

    There is intentionally no hash fallback. Historical hash-recovered rows are
    frozen pilots and are consumed as pinned data rather than rejoined live.
    Identical duplicate S2 records coalesce; a nonce in two sessions or any
    non-identical duplicate returns an explicit conflict instead of silently
    selecting the first candidate.

    ``project`` is retained only as a legacy API signal.  Its lossy sanitized
    value is never compared.  Production callers supply an opaque proof (or put
    one in the index via ``target_project_path``); if they pass only the legacy
    string, attribution fails closed.  Calls with neither are the pre-contract
    unscoped pilot path.

    On success returns the exact session, transcript coordinate, and folded S2
    host-result metadata. A conflict can carry no session when multiple sessions
    are implicated, but remains identity-proven for audit accounting.
    """
    completeness = index.get("candidate_completeness")
    if not isinstance(completeness, dict) or completeness.get("complete") is not True:
        return None
    if target_project_proof is None:
        row_proof = gate_row.get("project_proof")
        target_project_proof = (
            row_proof if isinstance(row_proof, dict)
            else index.get("target_project_proof")
        )
    allow_legacy_unscoped = target_project_proof is None and project is None

    nonce = gate_row.get("gate_call_id")
    if not isinstance(nonce, str) or not nonce:
        return None
    all_nonce_hits = list((index.get("by_nonce") or {}).get(nonce) or [])
    conflicts = _candidate_set_conflicts(all_nonce_hits)
    nonce_hits, project_unresolved = _partition_by_project(
        all_nonce_hits,
        target_project_proof,
        allow_legacy_unscoped=allow_legacy_unscoped,
    )
    if not nonce_hits:
        return None
    if conflicts:
        sessions = {row.get("session_id") for row in all_nonce_hits}
        result = {
            "session_id": next(iter(sessions)) if len(sessions) == 1 else None,
            "transcript_path": (
                all_nonce_hits[0].get("transcript_path")
                if len(sessions) == 1 else None
            ),
            "source": "codex_transcript_nonce",
            "conflict": True,
            "conflict_reasons": conflicts,
        }
        if target_project_proof is not None:
            result["project_check"] = project_proof.PROJECT_MATCH
        return result
    if project_unresolved:
        return None
    hit = min(nonce_hits, key=lambda row: row.get("source_order") or (0, 0))
    result = {
        "session_id": hit["session_id"],
        "transcript_path": hit["transcript_path"],
        "source": "codex_transcript_nonce",
        "host_observation": hit.get("host_observation"),
        "source_order": hit.get("source_order"),
        "project_proof": hit.get("project_proof"),
        "conflict": False,
    }
    if target_project_proof is not None:
        result["project_check"] = project_proof.PROJECT_MATCH
    return result
