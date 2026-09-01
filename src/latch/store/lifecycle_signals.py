"""Deterministic identity and contamination-free lifecycle signal helpers.

This module intentionally contains no detector policy. It defines the stable
substrate consumed by later detector/automation slices: candidate identities,
operation identities, contact projections, and explicit write/gate events.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from latch.store import db


SUBSTRATE_VERSION = "workstream-lifecycle-s1-v1"
CANDIDATE_PAYLOAD_BINDING_VERSION = "workstream-candidate-payload-v1"
_CANDIDATE_OPS = frozenset({"OPEN", "MERGE", "CLOSE", "ADOPT"})
_CONTACT_SOURCES = frozenset({"prompt", "graph", "tool", "gate"})


def _canonical_hash(prefix: str, value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"


def make_candidate_key(
    op: str,
    lane_ids: Iterable[int],
    member_unit_hashes: Iterable[str] = (),
) -> str:
    """Stable identity across derivations, independent of input ordering."""
    normalized_op = str(op).upper()
    if normalized_op not in _CANDIDATE_OPS:
        raise ValueError("candidate op must be OPEN, MERGE, CLOSE, or ADOPT")
    lanes = sorted({int(lane_id) for lane_id in lane_ids})
    units = sorted({str(value).strip() for value in member_unit_hashes if str(value).strip()})
    if normalized_op == "OPEN" and not units:
        raise ValueError("OPEN candidate identity requires member-unit hashes")
    if normalized_op != "OPEN" and units:
        raise ValueError("member-unit hashes are valid only for OPEN candidates")
    if normalized_op != "OPEN" and not lanes:
        raise ValueError("candidate identity requires a target lane")
    return _canonical_hash(
        "wsc1", {"op": normalized_op, "lane_ids": lanes, "member_units": units},
    )


def make_auto_op_key(op: str, candidate_key: str, window: Any) -> str:
    """Derivation-window-scoped idempotency key for an automatic operation."""
    normalized_op = str(op).upper()
    if normalized_op not in db.WORKSTREAM_OPS:
        raise ValueError("unknown workstream operation")
    candidate = str(candidate_key).strip()
    if not candidate:
        raise ValueError("candidate_key must be non-empty")
    return _canonical_hash(
        "wso1", {"op": normalized_op, "candidate_key": candidate, "window": window},
    )


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_ints(value: Any) -> list[int] | None:
    if not isinstance(value, (list, tuple)):
        return None
    parsed = [_positive_int(item) for item in value]
    if any(item is None for item in parsed):
        return None
    values = [int(item) for item in parsed if item is not None]
    return sorted(set(values)) if len(values) == len(set(values)) else None


def _merge_payload(signal: Mapping[str, Any], request: Mapping[str, Any]) -> dict | None:
    source = request.get(
        "source_workstream_id",
        request.get("src_workstream_id", request.get("src")),
    )
    absorber = request.get(
        "absorber_workstream_id",
        request.get("dst_workstream_id", request.get("dst")),
    )
    source_id, absorber_id = _positive_int(source), _positive_int(absorber)
    if source_id is None or absorber_id is None or source_id == absorber_id:
        return None
    dispositions = request.get("dispositions")
    if not isinstance(dispositions, Mapping):
        return None
    normalized: dict[str, str] = {}
    for raw_id, raw_value in dispositions.items():
        edge_id = _positive_int(raw_id)
        if edge_id is None:
            return None
        action = (
            raw_value.get("action")
            if isinstance(raw_value, Mapping)
            else raw_value
        )
        clean_action = str(action or "").strip().lower()
        if clean_action == "keep":
            clean_action = "preserve"
        if clean_action not in {"rehome", "preserve", "tombstone"}:
            return None
        normalized[str(edge_id)] = clean_action
    pair = _positive_ints([signal.get("left"), signal.get("right")])
    if pair is not None and set(pair) != {source_id, absorber_id}:
        return None
    return {
        "lane_pair": sorted({source_id, absorber_id}),
        "source_workstream_id": source_id,
        "absorber_workstream_id": absorber_id,
        "dispositions": {
            key: normalized[key]
            for key in sorted(normalized, key=int)
        },
    }


def _adopt_payload(signal: Mapping[str, Any], request: Mapping[str, Any]) -> dict | None:
    workstream_id = _positive_int(
        request.get(
            "workstream_id",
            request.get("requested_workstream_id", signal.get("workstream_id")),
        )
    )
    members = _positive_ints(request.get("node_ids"))
    relations = request.get("relations")
    evidence = request.get("evidence")
    if (
        workstream_id is None
        or not members
        or not isinstance(relations, Mapping)
        or not isinstance(evidence, Mapping)
    ):
        return None
    normalized_relations: dict[str, str] = {}
    for raw_id, raw_relation in relations.items():
        node_id = _positive_int(raw_id)
        if node_id is None:
            return None
        normalized_relations[str(node_id)] = str(raw_relation or "").strip().lower()
    if set(map(int, normalized_relations)) != set(members):
        return None
    signal_members = _positive_ints(signal.get("member_ids"))
    if signal_members is not None and signal_members != members:
        return None
    return {
        "workstream_id": workstream_id,
        "member_ids": members,
        "relations": {
            key: normalized_relations[key]
            for key in sorted(normalized_relations, key=int)
        },
        "allow_auto_apply": request.get("allow_auto_apply") is True,
    }


def make_candidate_payload_binding(
    op: str,
    signal: Mapping[str, Any],
    *,
    request: Mapping[str, Any] | None = None,
) -> dict | None:
    """Bind reusable candidate evidence to its exact actionable payload.

    The base candidate key deliberately remains stable for suggestion identity.
    Attestations and calibration use this versioned fingerprint as the second
    half of their identity so changed MERGE direction/dispositions or ADOPT
    target/membership/relations cannot inherit earlier judgments. Volatile
    corroboration stays outside the identity and is revalidated at apply time.
    """
    normalized_op = str(op).upper()
    operation_request = request
    if not isinstance(operation_request, Mapping):
        operation_request = signal.get("apply_request")
    if not isinstance(operation_request, Mapping):
        return None
    if normalized_op == "MERGE":
        payload = _merge_payload(signal, operation_request)
    elif normalized_op == "ADOPT":
        payload = _adopt_payload(signal, operation_request)
    else:
        return None
    if payload is None:
        return None
    try:
        fingerprint = _canonical_hash(
            "wsp1",
            {
                "version": CANDIDATE_PAYLOAD_BINDING_VERSION,
                "op": normalized_op,
                "payload": payload,
            },
        )
    except (TypeError, ValueError):
        return None
    return {
        "version": CANDIDATE_PAYLOAD_BINDING_VERSION,
        "fingerprint": fingerprint,
    }


def candidate_evidence_key(candidate_key: str, binding: Mapping[str, Any]) -> str:
    """Return the composite identity used by attestation/calibration evidence."""
    version = str(binding.get("version") or "").strip()
    fingerprint = str(binding.get("fingerprint") or "").strip()
    if version != CANDIDATE_PAYLOAD_BINDING_VERSION or not fingerprint:
        raise ValueError("candidate payload binding is incomplete")
    return _canonical_hash(
        "wce1",
        {
            "candidate_key": str(candidate_key).strip(),
            "binding_version": version,
            "payload_fingerprint": fingerprint,
        },
    )


def is_eligible_contact_event(event: Mapping[str, Any]) -> bool:
    """Canonical contact predicate shared by detector and revalidation."""
    if event.get("session_id") is None:
        return False
    source = str(event.get("source") or "")
    if source == "write":
        return True
    try:
        turn = int(event.get("turn"))
    except (TypeError, ValueError):
        return False
    return source in _CONTACT_SOURCES and turn > 0


def eligible_contact_sql(alias: str = "") -> str:
    """SQL equivalent of :func:`is_eligible_contact_event`."""
    clean_alias = str(alias).strip()
    if clean_alias and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", clean_alias) is None:
        raise ValueError("SQL alias must be an identifier")
    prefix = f"{clean_alias}." if clean_alias else ""
    return (
        f"{prefix}session_id IS NOT NULL AND "
        f"({prefix}source='write' OR ({prefix}turn>0 AND "
        f"{prefix}source IN ('prompt','graph','tool','gate')))"
    )


def list_session_workstream_contacts(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Project contamination-free events into session/workstream contacts.

    Legacy session-start injection events, focus-derived gate events, and
    project-only events never qualify. Prompt/graph/tool/gate contacts require
    turn > 0; explicit write contacts are eligible whenever they have a real
    session.
    """
    where = [eligible_contact_sql(), "workstream_id_at_event IS NOT NULL"]
    params: list[Any] = []
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    if until is not None:
        where.append("ts < ?")
        params.append(until)
    rows = conn.execute(
        "SELECT session_id, workstream_id_at_event AS workstream_id, "
        "MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS event_count, "
        "GROUP_CONCAT(DISTINCT source) AS sources "
        "FROM retrieval_events WHERE " + " AND ".join(where) +
        " GROUP BY session_id, workstream_id_at_event "
        "ORDER BY first_ts, session_id, workstream_id_at_event",
        params,
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        item["sources"] = sorted((item["sources"] or "").split(","))
        out.append(item)
    return out


def contact_sessions_by_workstream(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
) -> dict[int, set[str]]:
    result: dict[int, set[str]] = {}
    for row in list_session_workstream_contacts(conn, since=since, until=until):
        result.setdefault(int(row["workstream_id"]), set()).add(row["session_id"])
    return result


def record_write_contact(
    conn: sqlite3.Connection,
    *,
    session_id: str | None,
    node_id: int,
    turn: int | None = None,
) -> int:
    return db.record_retrieval_events(
        conn,
        source="write",
        items=[(node_id, None)],
        session_id=session_id,
        turn=turn,
    )


def record_gate_contacts(
    conn: sqlite3.Connection,
    *,
    session_id: str | None,
    turn: int | None,
    chain_assembly: Mapping[str, Any],
) -> int:
    """Record only request-derived (hybrid-rooted) gate evidence contacts."""
    chains = {
        int(chain.get("seed_id")): chain
        for chain in (chain_assembly.get("chains") or [])
        if isinstance(chain, Mapping) and chain.get("seed_id") is not None
    }
    total = 0
    for seed in chain_assembly.get("seeds") or []:
        if not isinstance(seed, Mapping) or seed.get("source") != "hybrid":
            continue
        seed_id = int(seed["id"])
        items: list[tuple[int, float | None]] = [(seed_id, seed.get("score"))]
        details: dict[int, dict[str, Any]] = {
            seed_id: {
                "seed_node_id": seed_id,
                "reached_node_id": seed_id,
                "workstream_id_at_event": seed.get("lane_group_id"),
            }
        }
        seen_reached = {seed_id}
        chain = chains.get(seed_id) or {}
        for evidence in chain.get("evidence") or []:
            if not isinstance(evidence, Mapping) or evidence.get("id") is None:
                continue
            reached_id = int(evidence["id"])
            if reached_id in seen_reached:
                continue
            seen_reached.add(reached_id)
            items.append((reached_id, evidence.get("score")))
            details[reached_id] = {
                "seed_node_id": seed_id,
                "reached_node_id": reached_id,
                "workstream_id_at_event": evidence.get(
                    "resolved_workstream_id"
                ),
            }
        # Record one batch per root so a reached node shared by two chains keeps
        # both seed->reached traversal pairs in the append-only event table.
        total += db.record_retrieval_events(
            conn,
            source="gate",
            items=items,
            session_id=session_id,
            turn=turn,
            event_details=details,
        )
    return total
