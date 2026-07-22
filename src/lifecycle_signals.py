"""Deterministic identity and contamination-free lifecycle signal helpers.

This module intentionally contains no detector policy. It defines the stable
substrate consumed by later detector/automation slices: candidate identities,
operation identities, contact projections, and explicit write/gate events.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

import db


SUBSTRATE_VERSION = "workstream-lifecycle-s1-v1"
_CANDIDATE_OPS = frozenset({"OPEN", "MERGE", "CLOSE", "ADOPT"})


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


def list_session_workstream_contacts(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Project contamination-free events into session/workstream contacts.

    Brief/session-start events, focus-derived gate events, and project-only
    events never qualify. Prompt/graph/tool/gate contacts require turn > 0;
    explicit write contacts are eligible whenever they have a real session.
    """
    where = [
        "session_id IS NOT NULL",
        "workstream_id_at_event IS NOT NULL",
        "(source = 'write' OR "
        " (turn > 0 AND source IN ('prompt', 'graph', 'tool', 'gate')))",
    ]
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
