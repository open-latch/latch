"""Deterministic workstream lifecycle mutations.

Every mutation is one ``BEGIN IMMEDIATE`` transaction under the shared writer
lock.  The immutable ``workstream_ops`` row is committed with the graph/node
changes it describes, making retries safe and exact reversal possible.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import sys
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import embeddings  # noqa: E402
import feeders  # noqa: E402
import lifecycle_receipts  # noqa: E402
import lockfile  # noqa: E402
import log_utils  # noqa: E402
import priorities  # noqa: E402
import rolling  # noqa: E402


OPEN_SIMILARITY_ATTACH = 0.85
OPEN_SIMILARITY_PROPOSE = 0.70
AUTO_MIN_RECURRENCE_SESSIONS = 2
RECENT_ACTIVE_LANE_DAYS = 30
RECENT_ACTIVE_LANE_CAP = 12
ACTIVE_STATUSES = frozenset({"staging", "canonical"})
CLOSE_OUTCOMES = frozenset({"completed", "abandoned", "superseded_by"})
CLOSE_ACTIONS = frozenset({"moot", "release", "keep-open", "repoint"})


class WorkstreamLifecycleError(RuntimeError):
    """Base lifecycle error; no partial mutation has been committed."""


class WorkstreamValidationError(WorkstreamLifecycleError, ValueError):
    pass


class WorkstreamConflictError(WorkstreamLifecycleError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _snapshot_token(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _clean_text(name: str, value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise WorkstreamValidationError(f"{name} is required")
    return text


def _normalize_title(value: str) -> str:
    return " ".join(value.casefold().split())


def _date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _project_path(project_path: str | None) -> str:
    return str(project_path or os.getcwd())


def _backup_before_mutation(
    conn: sqlite3.Connection, *, op: str, op_key: str,
) -> str | None:
    """Create a consistent SQLite backup immediately before a lifecycle write."""
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None:
        return None
    source = str(row["file"] if isinstance(row, sqlite3.Row) else row[2])
    if not source or source == ":memory:":
        return None
    source_path = Path(source)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    key_hash = hashlib.sha256(op_key.encode("utf-8")).hexdigest()[:12]
    backup = source_path.with_name(
        f"{source_path.name}.bak.lifecycle-{op.lower()}-{key_hash}.{stamp}"
    )
    dest = sqlite3.connect(str(backup))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return str(backup)


def _failure_point(_label: str) -> None:
    """Test seam for proving rollback after intermediate lifecycle writes."""
    return None


def _require_clean_connection(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise WorkstreamLifecycleError(
            "lifecycle operations require a connection with no open transaction"
        )


def _row(conn: sqlite3.Connection, node_id: int) -> dict | None:
    raw = conn.execute("SELECT * FROM nodes WHERE id = ?", (int(node_id),)).fetchone()
    return dict(raw) if raw is not None else None


def resolve_active(
    conn: sqlite3.Connection,
    workstream_id: int,
    *,
    max_hops: int = 16,
) -> dict:
    """Resolve one lane identity through an active ``merged_into`` chain.

    Closed lanes are valid historical identities and return ``active_id=None``.
    Malformed/ambiguous/cyclic chains fail closed for every read caller.
    """
    try:
        current = int(workstream_id)
    except (TypeError, ValueError):
        return {
            "requested_id": workstream_id, "state": "not_found",
            "active_id": None, "node": None, "path": [],
        }
    requested = current
    path: list[int] = []
    visited: set[int] = set()
    started_merged = False
    for _ in range(max(1, int(max_hops))):
        if current in visited:
            path.append(current)
            return {
                "requested_id": requested, "state": "cycle", "active_id": None,
                "node": None, "path": path,
            }
        visited.add(current)
        path.append(current)
        node = _row(conn, current)
        if node is None:
            return {
                "requested_id": requested, "state": "not_found", "active_id": None,
                "node": None, "path": path,
            }
        if node["kind"] != "workstream":
            return {
                "requested_id": requested, "state": "not_workstream",
                "active_id": None, "node": node, "path": path,
            }
        if node["status"] != "stale":
            return {
                "requested_id": requested,
                "state": "merged" if started_merged else "active",
                "active_id": int(node["id"]), "node": node, "path": path,
            }
        targets = conn.execute(
            "SELECT dst FROM edges WHERE src = ? AND relation = 'merged_into' "
            "AND status = 'active' ORDER BY id",
            (current,),
        ).fetchall()
        unique = sorted({int(target["dst"]) for target in targets})
        if not unique:
            return {
                "requested_id": requested, "state": "closed", "active_id": None,
                "node": node, "path": path,
            }
        if len(unique) != 1:
            return {
                "requested_id": requested, "state": "ambiguous", "active_id": None,
                "node": node, "path": path,
            }
        started_merged = True
        current = unique[0]
    return {
        "requested_id": requested, "state": "cycle", "active_id": None,
        "node": None, "path": path,
    }


def merge_receipts_for_path(
    conn: sqlite3.Connection, path: Sequence[int],
) -> list[dict]:
    """Return immutable MERGE evidence for the active redirect hops in ``path``.

    Identity edges predate the lifecycle ledger, so a hop may legitimately have
    no receipt.  When ledger rows exist, prefer the row whose recorded edge is
    currently active; this avoids attributing an older, subsequently unmerged
    operation to a later redirect between the same pair of lanes.
    """
    normalized = [int(node_id) for node_id in path]
    evidence: list[dict] = []
    for source_id, absorber_id in zip(normalized, normalized[1:]):
        edge_rows = conn.execute(
            "SELECT id FROM edges WHERE src = ? AND dst = ? "
            "AND relation = 'merged_into' AND status = 'active' ORDER BY id",
            (source_id, absorber_id),
        ).fetchall()
        active_edge_ids = {int(row["id"]) for row in edge_rows}
        if not active_edge_ids:
            continue
        rows = conn.execute(
            "SELECT * FROM workstream_ops WHERE op = 'MERGE' "
            "AND state = 'applied' AND src_workstream_id = ? "
            "AND dst_workstream_id = ? ORDER BY id DESC",
            (source_id, absorber_id),
        ).fetchall()
        chosen: tuple[dict, dict] | None = None
        legacy_candidate: tuple[dict, dict] | None = None
        for raw in rows:
            row = dict(raw)
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            edge_id = payload.get("merge_edge_id")
            try:
                edge_matches = edge_id is not None and int(edge_id) in active_edge_ids
            except (TypeError, ValueError):
                edge_matches = False
            if edge_matches:
                chosen = (row, payload)
                break
            # Early lifecycle ledgers may not have recorded the edge id.  A
            # single matching applied row is still stronger evidence than a
            # synthesized redirect message.
            if edge_id is None and legacy_candidate is None:
                legacy_candidate = (row, payload)
        if chosen is None and len(rows) == 1:
            chosen = legacy_candidate
        if chosen is None:
            continue
        row, payload = chosen
        evidence.append({
            "source_workstream_id": source_id,
            "absorber_workstream_id": absorber_id,
            "operation_id": int(row["id"]),
            "op_key": str(row["op_key"]),
            "receipt": (
                str(payload["receipt"])
                if payload.get("receipt") is not None else None
            ),
        })
    return evidence


def resolve_membership_target(
    conn: sqlite3.Connection, workstream_id: int,
) -> dict:
    """Resolve a write target, redirecting merges and rejecting history."""
    resolution = resolve_active(conn, workstream_id)
    active_id = resolution.get("active_id")
    if active_id is None:
        state = resolution["state"]
        if state == "not_found":
            error = f"workstream {workstream_id} not found"
        elif state == "not_workstream":
            error = f"node {workstream_id} is not a workstream"
        elif state == "closed":
            error = f"workstream {workstream_id} is closed (stale)"
        else:
            error = f"workstream {workstream_id} identity is {state}"
        return {
            "ok": False,
            "requested_workstream_id": workstream_id,
            "resolved_workstream_id": None,
            "state": state,
            "error": error,
            "resolution": resolution,
        }
    redirected = int(active_id) != int(workstream_id)
    merge_evidence = (
        merge_receipts_for_path(conn, resolution.get("path") or [])
        if redirected else []
    )
    durable_receipt = next(
        (item["receipt"] for item in merge_evidence if item.get("receipt")),
        None,
    )
    return {
        "ok": True,
        "requested_workstream_id": int(workstream_id),
        "resolved_workstream_id": int(active_id),
        "state": resolution["state"],
        "redirected": redirected,
        "receipt": (
            durable_receipt
            or f"latch redirected merged workstream {int(workstream_id)} to active "
               f"workstream {int(active_id)}."
            if redirected else None
        ),
        "merge_evidence": merge_evidence,
        "resolution": resolution,
    }


def _existing_result(
    conn: sqlite3.Connection,
    *,
    op_key: str,
    request: Mapping[str, Any],
) -> dict | None:
    existing = db.get_workstream_op(conn, op_key)
    if existing is None:
        return None
    payload = existing.get("payload") or {}
    if _canonical(payload.get("request")) != _canonical(dict(request)):
        raise WorkstreamConflictError(
            f"operation key {op_key!r} is already bound to a different request"
        )
    workstream_id = (
        existing.get("src_workstream_id")
        if existing.get("op") == "CLOSE"
        else existing.get("dst_workstream_id") or existing.get("src_workstream_id")
    )
    return {
        "ok": existing["state"] == "applied",
        "state": existing["state"],
        "error_code": existing.get("error_code"),
        "op": existing["op"],
        "op_key": existing["op_key"],
        "operation_id": int(existing["id"]),
        "candidate_key": existing.get("candidate_key"),
        "workstream_id": workstream_id,
        "receipt": payload.get("receipt"),
        "payload": payload,
        "idempotent": True,
        "forced": bool(existing.get("forced")),
    }


def _applied_result(row: dict) -> dict:
    payload = row.get("payload") or {}
    workstream_id = (
        row.get("src_workstream_id")
        if row.get("op") == "CLOSE"
        else row.get("dst_workstream_id") or row.get("src_workstream_id")
    )
    return {
        "ok": True,
        "state": "applied",
        "op": row["op"],
        "op_key": row["op_key"],
        "operation_id": int(row["id"]),
        "candidate_key": row.get("candidate_key"),
        "workstream_id": workstream_id,
        "receipt": payload.get("receipt"),
        "payload": payload,
        "idempotent": False,
        "forced": bool(row.get("forced")),
    }


def _finish_failed_nc(
    conn: sqlite3.Connection,
    *,
    op_key: str,
    op: str,
    origin: str,
    request: Mapping[str, Any],
    error_code: str,
    session_id: str | None = None,
    src_workstream_id: int | None = None,
    dst_workstream_id: int | None = None,
    forced: bool = False,
    preflight_token: str | None = None,
    candidate_key: str | None = None,
    failure_payload: Mapping[str, Any] | None = None,
) -> dict:
    payload = {"request": dict(request)}
    if failure_payload:
        payload.update(dict(failure_payload))
    row = db.begin_workstream_op_nc(
        conn,
        op_key=op_key,
        op=op,
        origin=origin,
        payload=payload,
        candidate_key=candidate_key or f"{op.lower()}:{op_key}",
        session_id=session_id,
        src_workstream_id=src_workstream_id,
        dst_workstream_id=dst_workstream_id,
        forced=forced,
        preflight_token=preflight_token,
    )
    row = db.finish_workstream_op_nc(
        conn, op_key, state="failed", error_code=error_code,
    )
    result = _existing_result(conn, op_key=op_key, request=request)
    assert result is not None
    return result


def _add_edge_id(
    conn: sqlite3.Connection, src: int, dst: int, relation: str, *, created_by: str,
) -> int:
    edge_id = db.add_edge_nc(conn, src, dst, relation, created_by=created_by)
    if edge_id is not None:
        return int(edge_id)
    canonical = db.canonicalize_relation(relation)
    row = conn.execute(
        "SELECT id FROM edges WHERE src = ? AND dst = ? AND relation = ?",
        (src, dst, canonical),
    ).fetchone()
    if row is None:  # pragma: no cover - defensive adapter for older db.py
        raise WorkstreamLifecycleError("edge insert did not yield a durable identity")
    return int(row["id"])


def _tombstone_edge_id(conn: sqlite3.Connection, edge: Mapping[str, Any]) -> int:
    helper = getattr(db, "tombstone_edge_id_nc", None)
    if helper is not None:
        return int(helper(conn, int(edge["id"])))
    return int(db.tombstone_edge_nc(
        conn, int(edge["src"]), int(edge["dst"]), str(edge["relation"]),
    ))


def _similar_workstreams(
    conn: sqlite3.Connection,
    text: str,
    *,
    embedding: bytes | None = None,
) -> tuple[bytes | None, list[dict]]:
    rows = conn.execute(
        "SELECT id, title, embedding FROM nodes WHERE kind = 'workstream' "
        "AND status != 'stale' AND embedding IS NOT NULL ORDER BY id",
    ).fetchall()
    if embedding is None:
        try:
            embedding = embeddings.to_blob(embeddings.embed(text))
        except Exception:
            embedding = None
    if embedding is None or not rows:
        return embedding, []
    query = embeddings.from_blob(embedding)
    assert query is not None
    matches = []
    for row in rows:
        other = embeddings.from_blob(row["embedding"])
        if other is None or other.shape != query.shape:
            continue
        score = float(other @ query)
        matches.append({"id": int(row["id"]), "title": row["title"], "similarity": score})
    matches.sort(key=lambda item: (-item["similarity"], item["id"]))
    return embedding, matches


def _normalize_recurrence(
    recurrence: Mapping[str, Any] | None,
    *,
    session_id: str | None,
) -> dict:
    source = dict(recurrence or {})
    raw_ids = source.get("session_ids") or ([] if session_id is None else [session_id])
    session_ids = sorted({str(value) for value in raw_ids if str(value).strip()})
    if "session_count" in source:
        count = int(source["session_count"])
    elif "sessions" in source:
        count = int(source["sessions"])
    else:
        count = len(session_ids) if session_ids else 1
    if count < 0:
        raise WorkstreamValidationError("recurrence session_count cannot be negative")
    raw_targets = source.get("shared_target_ids") or []
    if not isinstance(raw_targets, (list, tuple, set)):
        raise WorkstreamValidationError("recurrence shared_target_ids must be a list")
    try:
        shared_target_ids = sorted({int(value) for value in raw_targets})
    except (TypeError, ValueError) as exc:
        raise WorkstreamValidationError(
            "recurrence shared_target_ids must contain integer ids"
        ) from exc
    return {
        "session_count": count,
        "session_ids": session_ids,
        "since": str(source.get("since") or _date()),
        "shared_target_ids": shared_target_ids,
        "shared_target_validated": source.get("shared_target_validated") is True,
    }


def _verified_shared_targets(
    conn: sqlite3.Connection,
    *,
    member_ids: Sequence[int],
    target_ids: Sequence[int],
) -> bool:
    """Verify the structural Tier-1 analog accepted by governed OPEN."""
    members = sorted({int(value) for value in member_ids})
    targets = sorted({int(value) for value in target_ids})
    if len(members) < 2 or not targets:
        return False
    member_marks = ",".join("?" for _ in members)
    target_marks = ",".join("?" for _ in targets)
    rows = conn.execute(
        f"SELECT e.dst, COUNT(DISTINCT e.src) AS source_count FROM edges e "
        f"JOIN nodes member ON member.id=e.src "
        f"JOIN nodes target ON target.id=e.dst "
        f"WHERE e.src IN ({member_marks}) AND e.dst IN ({target_marks}) "
        "AND e.relation IN ('advances','motivates','depends_on') "
        "AND e.status='active' AND member.status!='stale' AND target.status!='stale' "
        "AND target.kind IN ('decision','workstream') GROUP BY e.dst "
        "HAVING COUNT(DISTINCT e.src)>=2",
        [*members, *targets],
    ).fetchall()
    return {int(row["dst"]) for row in rows} == set(targets)


def _auto_plan_is_current(
    conn: sqlite3.Connection,
    *,
    origin: str,
    candidate_key: str | None,
    op_key: str,
    op: str,
    operation_request: Mapping[str, Any],
) -> bool:
    """Recompute governed auto authority inside the mutation transaction."""
    if origin != "auto":
        return True
    candidate = str(candidate_key or "").strip()
    if not candidate:
        return False
    try:
        import workstream_automation

        return bool(workstream_automation.auto_plan_is_current(
            conn,
            candidate_key=candidate,
            op_key=op_key,
            op=op,
            operation_request=operation_request,
        ))
    except Exception:
        return False


def _charter(
    objective: Any, done_when: Any, scope_boundary: Any, next_step: Any,
) -> tuple[dict, str]:
    fields = {
        "objective": _clean_text("objective", objective),
        "done_when": _clean_text("done_when", done_when),
        "scope_boundary": _clean_text("scope_boundary", scope_boundary),
        "next_step": _clean_text("next_step", next_step),
    }
    body = (
        f"Objective: {fields['objective']}\n"
        f"Done when: {fields['done_when']}\n"
        f"Scope boundary: {fields['scope_boundary']}\n"
        f"Next step: {fields['next_step']}"
    )
    return fields, body


def _recent_active_lane_count(conn: sqlite3.Connection) -> int:
    """Detector-parity 30-day active-lane count, recomputed under write lock."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_ACTIVE_LANE_DAYS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM nodes w
        WHERE w.kind = 'workstream' AND w.status != 'stale'
          AND MAX(
                COALESCE((
                    SELECT MAX(n.updated_at) FROM nodes n
                    WHERE n.status != 'stale' AND n.workstream_id = w.id
                ), ''),
                COALESCE((
                    SELECT MAX(r.ts) FROM retrieval_events r
                    WHERE r.workstream_id_at_event = w.id
                ), '')
              ) >= ?
        """,
        (cutoff,),
    ).fetchone()
    return int(row["n"] if row is not None else 0)


def open_workstream(
    conn: sqlite3.Connection,
    *,
    title: str,
    objective: str,
    done_when: str,
    scope_boundary: str,
    next_step: str,
    op_key: str,
    member_ids: Sequence[int] = (),
    origin: str = "manual",
    recurrence: Mapping[str, Any] | None = None,
    probation: Mapping[str, Any] | None = None,
    session_id: str | None = None,
    branched_from: int | None = None,
    project_path: str | None = None,
    embedding: bytes | None = None,
    similarity_override: bool = False,
    candidate_key: str | None = None,
    force: bool = False,
) -> dict:
    """Open a staging lane with an exact charter and enumerated membership."""
    clean_title = _clean_text("title", title)
    key = _clean_text("op_key", op_key)
    clean_origin = _clean_text("origin", origin).lower()
    charter, body = _charter(objective, done_when, scope_boundary, next_step)
    members = sorted({int(node_id) for node_id in member_ids})
    recur = _normalize_recurrence(recurrence, session_id=session_id)
    # Automatic probation is machine-owned state.  Caller-provided values are
    # retained only in the authorization envelope; they can neither disable
    # probation nor forge its start/target in the applied receipt.
    requested_probation = {} if clean_origin == "auto" else dict(probation or {})
    request = {
        "title": clean_title,
        **charter,
        "member_ids": members,
        "origin": clean_origin,
        "recurrence": recur,
        "probation": requested_probation,
        "branched_from": int(branched_from) if branched_from is not None else None,
        "similarity_override": bool(similarity_override),
        "force": bool(force),
    }
    _require_clean_connection(conn)
    prior = _existing_result(conn, op_key=key, request=request)
    if prior is not None:
        return prior
    if clean_origin == "auto":
        if force:
            raise WorkstreamValidationError("automatic OPEN cannot force past safety rails")
        distinct = len(recur["session_ids"])
        session_recurrence = bool(
            recur["session_count"] >= AUTO_MIN_RECURRENCE_SESSIONS and distinct >= 2
        )
        shared_target_recurrence = bool(
            recur["shared_target_validated"]
            and recur["shared_target_ids"]
            and str(candidate_key or "").strip()
            and _verified_shared_targets(
                conn, member_ids=members, target_ids=recur["shared_target_ids"],
            )
        )
        if not session_recurrence and not shared_target_recurrence:
            raise WorkstreamValidationError(
                "automatic OPEN requires two-session recurrence or a verified shared target"
            )

    lock_path = _project_path(project_path)
    result: dict
    with lockfile.writer_lock(lock_path):
        backup_path = _backup_before_mutation(conn, op="OPEN", op_key=key)
        conn.execute("BEGIN IMMEDIATE")
        try:
            prior = _existing_result(conn, op_key=key, request=request)
            if prior is not None:
                conn.commit()
                return prior
            if not _auto_plan_is_current(
                conn,
                origin=clean_origin,
                candidate_key=candidate_key,
                op_key=key,
                op="OPEN",
                operation_request={
                    "title": title,
                    "objective": objective,
                    "done_when": done_when,
                    "scope_boundary": scope_boundary,
                    "next_step": next_step,
                    "op_key": op_key,
                    "member_ids": member_ids,
                    "origin": origin,
                    "recurrence": recurrence,
                    "probation": probation,
                    "session_id": session_id,
                    "branched_from": branched_from,
                    "embedding": embedding,
                    "similarity_override": similarity_override,
                    "candidate_key": candidate_key,
                    "force": force,
                },
            ):
                failed = _finish_failed_nc(
                    conn,
                    op_key=key,
                    op="OPEN",
                    origin=clean_origin,
                    request=request,
                    error_code="preflight_stale",
                    session_id=session_id,
                    src_workstream_id=(
                        int(branched_from) if branched_from is not None else None
                    ),
                    forced=False,
                    candidate_key=candidate_key,
                    failure_payload={"governor": "auto_plan_stale"},
                )
                conn.commit()
                return failed
            recent_lane_count = _recent_active_lane_count(conn)
            if recent_lane_count >= RECENT_ACTIVE_LANE_CAP and not force:
                failed = _finish_failed_nc(
                    conn,
                    op_key=key,
                    op="OPEN",
                    origin=clean_origin,
                    request=request,
                    error_code="blocked",
                    session_id=session_id,
                    src_workstream_id=(int(branched_from) if branched_from is not None else None),
                    forced=False,
                    candidate_key=candidate_key,
                    failure_payload={
                        "governor": "recent_active_lane_cap",
                        "recent_active_lane_count": recent_lane_count,
                        "recent_active_lane_cap": RECENT_ACTIVE_LANE_CAP,
                    },
                )
                conn.commit()
                return failed
            collisions = conn.execute(
                "SELECT id, title, status FROM nodes WHERE kind = 'workstream' ORDER BY id"
            ).fetchall()
            exact = [dict(row) for row in collisions if _normalize_title(row["title"]) == _normalize_title(clean_title)]
            if exact:
                raise WorkstreamConflictError(
                    f"workstream title collides with existing id={exact[0]['id']}"
                )
            member_rows: dict[int, dict] = {}
            for node_id in members:
                node = _row(conn, node_id)
                if (
                    node is None
                    or node["kind"] in {"workstream", "priority"}
                    or node["status"] == "stale"
                ):
                    raise WorkstreamValidationError(f"invalid OPEN member id={node_id}")
                owner_id = node.get("workstream_id")
                if owner_id is not None:
                    owner = resolve_active(conn, int(owner_id))
                    if owner["active_id"] is not None:
                        raise WorkstreamConflictError(
                            f"OPEN member id={node_id} already belongs to active "
                            f"workstream {owner['active_id']}; explicit repoint is required"
                        )
                    if owner["state"] not in {"closed"}:
                        raise WorkstreamValidationError(
                            f"OPEN member id={node_id} has invalid workstream ownership"
                        )
                member_rows[node_id] = node
            if (
                clean_origin == "auto"
                and recur["shared_target_validated"]
                and recur["shared_target_ids"]
                and not _verified_shared_targets(
                    conn,
                    member_ids=members,
                    target_ids=recur["shared_target_ids"],
                )
            ):
                failed = _finish_failed_nc(
                    conn,
                    op_key=key,
                    op="OPEN",
                    origin=clean_origin,
                    request=request,
                    error_code="preflight_stale",
                    session_id=session_id,
                    forced=False,
                    candidate_key=candidate_key,
                    failure_payload={"governor": "shared_target_stale"},
                )
                conn.commit()
                return failed
            parent = None
            if branched_from is not None:
                parent = resolve_active(conn, int(branched_from))
                if parent["active_id"] is None:
                    raise WorkstreamValidationError("branched_from must resolve to an active workstream")
            active_existing = [
                dict(row) for row in conn.execute(
                    "SELECT id, embedding FROM nodes WHERE kind='workstream' "
                    "AND status!='stale' ORDER BY id"
                ).fetchall()
            ]
            embedding, similar = _similar_workstreams(
                conn, f"{clean_title}\n{body}", embedding=embedding,
            )
            if clean_origin == "auto" and active_existing:
                expected_ids = {int(row["id"]) for row in active_existing}
                compared_ids = {int(item["id"]) for item in similar}
                comparison_complete = bool(
                    embedding is not None
                    and all(row.get("embedding") is not None for row in active_existing)
                    and compared_ids == expected_ids
                )
                if not comparison_complete:
                    failed = _finish_failed_nc(
                        conn,
                        op_key=key,
                        op="OPEN",
                        origin=clean_origin,
                        request=request,
                        error_code="blocked",
                        session_id=session_id,
                        forced=False,
                        candidate_key=candidate_key,
                        failure_payload={"governor": "similarity_comparison_incomplete"},
                    )
                    conn.commit()
                    return failed
            if similar and similar[0]["similarity"] >= OPEN_SIMILARITY_ATTACH:
                raise WorkstreamConflictError(
                    f"similar active workstream id={similar[0]['id']} score={similar[0]['similarity']:.3f}; adopt instead"
                )
            if (
                similar and similar[0]["similarity"] >= OPEN_SIMILARITY_PROPOSE
                and (not similarity_override or clean_origin == "auto")
            ):
                raise WorkstreamConflictError(
                    f"possible duplicate id={similar[0]['id']} score={similar[0]['similarity']:.3f}; explicit override required"
                )

            body = rolling.apply(body, "Workstream opened.", date=_date())
            if clean_origin == "auto":
                probation_payload = {
                    "active": True,
                    "opened_at": db._now(),
                    "eligible_session_target": 10,
                    "eligible_session_count": 0,
                    "recurrence": recur,
                }
            else:
                probation_payload = dict(requested_probation)
                probation_payload.setdefault("opened_at", db._now())
                probation_payload.setdefault("recurrence", recur)
                probation_payload.setdefault("eligible_session_target", 10)
                probation_payload.setdefault("eligible_session_count", recur["session_count"])
                probation_payload.setdefault("active", False)
            workstream_id = db.insert_node_nc(
                conn,
                kind="workstream",
                title=clean_title,
                body=body,
                status="staging",
                session_id=session_id,
                embedding=embedding,
            )
            prior_memberships = {
                str(node_id): member_rows[node_id].get("workstream_id") for node_id in members
            }
            db.set_node_workstream_nc(conn, members, workstream_id)
            branch_edge_id = None
            if parent is not None:
                branch_edge_id = _add_edge_id(
                    conn, workstream_id, int(parent["active_id"]), "branched_from",
                    created_by="lifecycle:open",
                )
            focus = db.get_focus(conn, limit=0)
            if len(focus) >= 3:
                score = math.nextafter(float(focus[2]["effective_score"]), -math.inf)
            else:
                score = 0.01
            db.set_focus_row_nc(
                conn, workstream_id, score=score, set_at=db._now(),
                set_by="lifecycle", pinned=False, rank=0,
            )
            db.recompute_focus_ranks_nc(conn)
            receipt = lifecycle_receipts.opened(
                clean_title, recur["session_count"], recur["since"], charter["done_when"],
            )
            watch_neighbor = None
            if similar:
                nearest_score = float(similar[0]["similarity"])
                if (
                    OPEN_SIMILARITY_PROPOSE <= nearest_score < OPEN_SIMILARITY_ATTACH
                    and clean_origin != "auto" and similarity_override
                ) or (
                    clean_origin == "auto" and nearest_score < OPEN_SIMILARITY_PROPOSE
                ):
                    watch_neighbor = similar[0]
            payload = {
                "request": request,
                "title": clean_title,
                "receipt": receipt,
                "assigned_member_ids": members,
                "prior_memberships": prior_memberships,
                "watch_pair": (
                    [workstream_id, int(watch_neighbor["id"])]
                    if watch_neighbor is not None else None
                ),
                "watch_similarity": (
                    float(watch_neighbor["similarity"])
                    if watch_neighbor is not None else None
                ),
                "probation": probation_payload,
                "branch_edge_id": branch_edge_id,
                "focus": db.get_focus_row(conn, workstream_id),
                "similarity_candidates": similar[:3],
                "recent_active_lane_count": recent_lane_count,
                "recent_active_lane_cap": RECENT_ACTIVE_LANE_CAP,
                "governor_forced": bool(
                    force and recent_lane_count >= RECENT_ACTIVE_LANE_CAP
                ),
                "backup_path": backup_path,
            }
            row = db.begin_workstream_op_nc(
                conn,
                op_key=key,
                op="OPEN",
                origin=clean_origin,
                payload=payload,
                candidate_key=candidate_key or f"open:{_normalize_title(clean_title)}",
                session_id=session_id,
                src_workstream_id=int(parent["active_id"]) if parent else None,
                dst_workstream_id=workstream_id,
                forced=force,
            )
            row = db.finish_workstream_op_nc(conn, key, state="applied")
            conn.commit()
            result = _applied_result(row)
        except Exception:
            conn.rollback()
            raise
    lifecycle_receipts.emit_applied(result, project_path=lock_path, session_id=session_id)
    return result


def _auto_open_probation_state(
    conn: sqlite3.Connection,
    workstream_id: int,
    *,
    now: datetime | None = None,
) -> dict:
    """Recompute the automatic OPEN probation window from durable events.

    The immutable receipt records the target, not proof that the target was
    reached without lane contact.  Release becomes eligible only after the
    target number of post-open eligible sessions, remains fail-closed once a
    lane contact contaminates the window, and expires with detector retention.
    """
    row = conn.execute(
        "SELECT op_key, applied_at, payload_json FROM workstream_ops WHERE op = 'OPEN' "
        "AND state = 'applied' AND origin = 'auto' "
        "AND dst_workstream_id = ? ORDER BY id DESC LIMIT 1",
        (int(workstream_id),),
    ).fetchone()
    base = {
        "present": False,
        "active": False,
        "release_ready": False,
        "graduated": False,
        "reason": "missing_auto_open",
        "eligible_session_count": 0,
        "eligible_session_target": 10,
        "contact_session_count": 0,
        "payload": None,
    }
    if row is None:
        return base
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        return {**base, "present": True, "reason": "invalid_open_payload"}
    if not isinstance(payload, dict):
        return {**base, "present": True, "reason": "invalid_open_payload"}
    probation = payload.get("probation")
    if not isinstance(probation, Mapping) or probation.get("active") is not True:
        return {
            **base,
            "present": True,
            "payload": payload,
            "reason": "not_probationary",
        }
    try:
        import workstream_detector

        anchor = now or datetime.now(timezone.utc)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        else:
            anchor = anchor.astimezone(timezone.utc)
        opened_at = workstream_detector._parse_ts(probation.get("opened_at"))
        target = max(1, int(
            probation.get("eligible_session_target")
            or probation.get("eligible_sessions_target")
            or 10
        ))
        if opened_at is None:
            return {
                **base,
                "present": True,
                "payload": payload,
                "eligible_session_target": target,
                "reason": "missing_opened_at",
            }
        retention_days = int(workstream_detector.ELIGIBLE_WINDOW_CAP_DAYS)
        retention_start = anchor - timedelta(days=retention_days)
        if opened_at < retention_start or opened_at > anchor:
            return {
                **base,
                "present": True,
                "payload": payload,
                "opening_op_key": str(row["op_key"]),
                "opened_at": workstream_detector._stamp(opened_at),
                "eligible_session_target": target,
                "reason": "probation_evidence_expired",
            }
        cap_stamp = workstream_detector._stamp(retention_start)
        anchor_stamp = workstream_detector._stamp(anchor)
        event_rows = [
            dict(item) for item in conn.execute(
                "SELECT * FROM retrieval_events WHERE ts >= ? AND ts <= ? "
                "ORDER BY ts, id",
                (cap_stamp, anchor_stamp),
            ).fetchall()
        ]
        session_started = {
            str(item["id"]): item["started_at"]
            for item in conn.execute("SELECT id, started_at FROM sessions").fetchall()
        }
        eligible = workstream_detector._select_eligible_sessions(
            event_rows, session_started, now=anchor,
        )
        after_sessions = {
            str(item["session_id"])
            for item in eligible
            if (
                workstream_detector._parse_ts(item.get("started_at"))
                or opened_at
            ) > opened_at
        }
        contact_sessions = {
            str(item["session_id"])
            for item in event_rows
            if workstream_detector._is_contact_event(item)
            and item.get("workstream_id_at_event") is not None
            and int(item["workstream_id_at_event"]) == int(workstream_id)
            and (
                workstream_detector._parse_ts(item.get("ts"))
                or opened_at
            ) > opened_at
        }
    except Exception:
        return {
            **base,
            "present": True,
            "payload": payload,
            "reason": "probation_evidence_unavailable",
        }
    observed = len(after_sessions)
    contacts = len(contact_sessions)
    graduated = observed >= target and contacts > 0
    ready = observed >= target and contacts == 0
    active = observed < target
    return {
        "present": True,
        "active": active,
        "release_ready": ready,
        "graduated": graduated,
        "reason": (
            "contact_observed" if graduated
            else "qualified" if ready
            else "contact_before_target" if contacts
            else "awaiting_target_sessions"
        ),
        "opening_op_key": str(row["op_key"]),
        "opened_at": workstream_detector._stamp(opened_at),
        "eligible_session_count": observed,
        "eligible_session_target": target,
        "contact_session_count": contacts,
        "eligible_sessions_after_open": sorted(after_sessions),
        "contact_sessions_after_open": sorted(contact_sessions),
        "payload": payload,
    }


def _active_auto_open_payload(
    conn: sqlite3.Connection, workstream_id: int,
) -> dict | None:
    """Compatibility adapter: payload only while zero-contact rollback is ready."""
    state = _auto_open_probation_state(conn, workstream_id)
    return state["payload"] if state.get("release_ready") else None


def _probation_release_members(
    conn: sqlite3.Connection, workstream_id: int,
) -> list[dict]:
    """Currently-owned members assigned by the active automatic OPEN receipt."""
    payload = _active_auto_open_payload(conn, workstream_id)
    if payload is None:
        return []
    assigned: list[int] = []
    for raw_id in payload.get("assigned_member_ids") or []:
        try:
            assigned.append(int(raw_id))
        except (TypeError, ValueError):
            continue
    assigned = sorted(set(assigned))
    if not assigned:
        return []
    placeholders = ",".join("?" for _ in assigned)
    rows = conn.execute(
        f"SELECT id, kind, title, status, workstream_id, updated_at FROM nodes "
        f"WHERE id IN ({placeholders}) AND workstream_id = ? "
        "AND kind NOT IN ('workstream','priority') ORDER BY id",
        (*assigned, int(workstream_id)),
    ).fetchall()
    return [dict(row) for row in rows]


def _close_snapshot(
    conn: sqlite3.Connection,
    workstream_id: int,
    *,
    include_probation_releases: bool = False,
) -> tuple[list[dict], str]:
    snapshot = feeders.open_feeder_snapshot(conn, workstream_id)
    # CLOSE also mutates scoped priorities, so their exact state belongs to the
    # preflight even though priorities are not feeders.
    priority_snapshot = _priority_snapshot(conn, workstream_id)
    if include_probation_releases:
        by_id = {int(item["id"]): item for item in snapshot}
        for row in _probation_release_members(conn, workstream_id):
            node_id = int(row["id"])
            item = by_id.get(node_id)
            if item is None:
                item = {
                    **row,
                    "is_member": True,
                    "intent_edges": [],
                }
                snapshot.append(item)
                by_id[node_id] = item
            item["release_only"] = True
            item["probation_assigned"] = True
        snapshot.sort(key=lambda item: int(item["id"]))
    return snapshot, _snapshot_token({"feeders": snapshot, "priorities": priority_snapshot})


def close_preflight(
    conn: sqlite3.Connection,
    workstream_id: int,
    *,
    origin: str = "manual",
    outcome: str | None = None,
) -> dict:
    """Read-only CLOSE preflight suitable for presenting dispositions."""
    resolved = resolve_active(conn, workstream_id)
    if resolved["active_id"] is None:
        return {"resolution": resolved, "feeders": [], "token": None}
    probation_abandonment = (
        str(origin).strip().lower() == "auto"
        and str(outcome or "").strip().lower() == "abandoned"
        and _active_auto_open_payload(conn, int(resolved["active_id"])) is not None
    )
    rows, token = _close_snapshot(
        conn,
        int(resolved["active_id"]),
        include_probation_releases=probation_abandonment,
    )
    return {"resolution": resolved, "feeders": rows, "token": token}


def _normalize_dispositions(dispositions: Mapping[int | str, Any] | None) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for raw_id, raw in (dispositions or {}).items():
        node_id = int(raw_id)
        if isinstance(raw, str):
            item = {"action": raw}
        elif isinstance(raw, Mapping):
            item = dict(raw)
        else:
            raise WorkstreamValidationError(f"invalid disposition for feeder {node_id}")
        action = str(item.get("action") or "").strip().lower()
        if action not in CLOSE_ACTIONS:
            raise WorkstreamValidationError(f"invalid disposition action {action!r}")
        normalized = {"action": action}
        if action == "repoint":
            target = item.get("target_workstream_id", item.get("target"))
            if target is None:
                raise WorkstreamValidationError("repoint requires target_workstream_id")
            normalized["target_workstream_id"] = int(target)
        out[node_id] = normalized
    return out


def _within_open_probation(conn: sqlite3.Connection, workstream_id: int) -> bool:
    return _active_auto_open_payload(conn, workstream_id) is not None


def close_workstream(
    conn: sqlite3.Connection,
    workstream_id: int,
    *,
    outcome: str,
    reason: str,
    op_key: str,
    dispositions: Mapping[int | str, Any] | None = None,
    origin: str = "manual",
    force: bool = False,
    superseded_by: int | None = None,
    preflight_token: str | None = None,
    session_id: str | None = None,
    project_path: str | None = None,
    candidate_key: str | None = None,
) -> dict:
    """Close a lane after exact, edge-ID-stable feeder dispositions."""
    requested_id = int(workstream_id)
    clean_outcome = _clean_text("outcome", outcome).lower()
    if clean_outcome not in CLOSE_OUTCOMES:
        raise WorkstreamValidationError(f"invalid CLOSE outcome {clean_outcome!r}")
    clean_reason = _clean_text("reason", reason)
    key = _clean_text("op_key", op_key)
    clean_origin = _clean_text("origin", origin).lower()
    normalized_dispositions = _normalize_dispositions(dispositions)
    if clean_origin == "auto" and force:
        raise WorkstreamValidationError("automatic CLOSE cannot force past safety rails")
    if clean_outcome == "superseded_by" and superseded_by is None:
        raise WorkstreamValidationError("superseded_by outcome requires a target")
    request = {
        "requested_workstream_id": requested_id,
        "outcome": clean_outcome,
        "reason": clean_reason,
        "dispositions": {str(k): v for k, v in sorted(normalized_dispositions.items())},
        "origin": clean_origin,
        "force": bool(force),
        "superseded_by": int(superseded_by) if superseded_by is not None else None,
    }
    _require_clean_connection(conn)
    prior = _existing_result(conn, op_key=key, request=request)
    if prior is not None:
        return prior
    initial_resolution = resolve_active(conn, requested_id)
    if initial_resolution["active_id"] is None:
        return {
            "ok": initial_resolution["state"] == "closed", "state": initial_resolution["state"],
            "workstream_id": None, "resolution": initial_resolution, "already": True,
        }
    active_id = int(initial_resolution["active_id"])
    expected_token = preflight_token
    lock_path = _project_path(project_path)
    result: dict
    with lockfile.writer_lock(lock_path):
        backup_path = _backup_before_mutation(conn, op="CLOSE", op_key=key)
        conn.execute("BEGIN IMMEDIATE")
        try:
            prior = _existing_result(conn, op_key=key, request=request)
            if prior is not None:
                conn.commit()
                return prior
            locked_probation_abandonment = (
                clean_origin == "auto"
                and clean_outcome == "abandoned"
                and _within_open_probation(conn, active_id)
            )
            snapshot, locked_token = _close_snapshot(
                conn,
                active_id,
                include_probation_releases=locked_probation_abandonment,
            )
            if expected_token is None:
                expected_token = locked_token
            if not _auto_plan_is_current(
                conn,
                origin=clean_origin,
                candidate_key=candidate_key,
                op_key=key,
                op="CLOSE",
                operation_request={
                    "workstream_id": workstream_id,
                    "outcome": outcome,
                    "reason": reason,
                    "op_key": op_key,
                    "dispositions": dispositions,
                    "origin": origin,
                    "force": force,
                    "superseded_by": superseded_by,
                    "preflight_token": preflight_token,
                    "session_id": session_id,
                    "candidate_key": candidate_key,
                },
            ):
                failed = _finish_failed_nc(
                    conn,
                    op_key=key,
                    op="CLOSE",
                    origin=clean_origin,
                    request=request,
                    error_code="preflight_stale",
                    session_id=session_id,
                    src_workstream_id=active_id,
                    dst_workstream_id=(
                        int(superseded_by) if superseded_by is not None else None
                    ),
                    forced=force,
                    preflight_token=expected_token,
                    candidate_key=candidate_key,
                    failure_payload={"governor": "auto_plan_stale"},
                )
                conn.commit()
                return failed
            resolution = resolve_active(conn, requested_id)
            if resolution["active_id"] is None or int(resolution["active_id"]) != active_id:
                failed = _finish_failed_nc(
                    conn, op_key=key, op="CLOSE", origin=clean_origin,
                    request=request, error_code="preflight_stale", session_id=session_id,
                    src_workstream_id=active_id, forced=force,
                    preflight_token=expected_token,
                    candidate_key=candidate_key,
                )
                conn.commit()
                return failed
            if locked_token != expected_token:
                failed = _finish_failed_nc(
                    conn, op_key=key, op="CLOSE", origin=clean_origin,
                    request=request, error_code="preflight_stale", session_id=session_id,
                    src_workstream_id=active_id, forced=force,
                    preflight_token=expected_token,
                    candidate_key=candidate_key,
                )
                conn.commit()
                return failed
            feeder_ids = {int(item["id"]) for item in snapshot}
            disposition_ids = set(normalized_dispositions)
            if disposition_ids - feeder_ids:
                raise WorkstreamValidationError(
                    f"dispositions name non-open feeders {sorted(disposition_ids - feeder_ids)}"
                )
            missing = sorted(feeder_ids - disposition_ids)
            keep_open = sorted(
                node_id for node_id, item in normalized_dispositions.items()
                if item["action"] == "keep-open"
            )
            if (missing or keep_open) and not force:
                failed = _finish_failed_nc(
                    conn, op_key=key, op="CLOSE", origin=clean_origin,
                    request=request, error_code="open_feeders", session_id=session_id,
                    src_workstream_id=active_id, forced=force,
                    preflight_token=expected_token,
                    candidate_key=candidate_key,
                )
                conn.commit()
                return failed

            ws = _row(conn, active_id)
            assert ws is not None
            prior_status = str(ws["status"])
            prior_focus = db.get_focus_row(conn, active_id)
            changed_edge_ids: list[int] = []
            created_edge_ids: list[int] = []
            tombstoned_edge_ids: list[int] = []
            repointed_member_ids: list[int] = []
            released_member_ids: list[int] = []
            disposition_records: list[dict] = []
            by_id = {int(item["id"]): item for item in snapshot}
            for feeder_id in sorted(feeder_ids & disposition_ids):
                item = by_id[feeder_id]
                disposition = normalized_dispositions[feeder_id]
                action = disposition["action"]
                if item.get("release_only") and action != "release":
                    raise WorkstreamValidationError(
                        f"probation-assigned member {feeder_id} permits only release"
                    )
                if action == "keep-open":
                    continue
                record: dict[str, Any] = {"feeder_id": feeder_id, "action": action, "edge_ids": []}
                if action == "moot":
                    edge_id = _add_edge_id(
                        conn, active_id, feeder_id, "resolves", created_by="lifecycle:close",
                    )
                    record["edge_ids"].append(edge_id)
                    created_edge_ids.append(edge_id)
                    changed_edge_ids.append(edge_id)
                elif action == "release":
                    if not (
                        clean_origin == "auto" and clean_outcome == "abandoned"
                        and _within_open_probation(conn, active_id)
                    ):
                        raise WorkstreamValidationError(
                            "release is limited to automatic probation abandonment"
                        )
                    if item["is_member"]:
                        db.set_node_workstream_nc(conn, [feeder_id], None)
                        released_member_ids.append(feeder_id)
                elif action == "repoint":
                    target_resolution = resolve_active(conn, disposition["target_workstream_id"])
                    target_id = target_resolution["active_id"]
                    if target_id is None or int(target_id) == active_id:
                        raise WorkstreamValidationError("repoint target must be another active workstream")
                    target_id = int(target_id)
                    record["target_workstream_id"] = target_id
                    if item["is_member"]:
                        db.set_node_workstream_nc(conn, [feeder_id], target_id)
                        repointed_member_ids.append(feeder_id)
                    for edge in item["intent_edges"]:
                        new_edge_id = _add_edge_id(
                            conn, feeder_id, target_id, edge["relation"],
                            created_by="lifecycle:close",
                        )
                        _tombstone_edge_id(conn, edge)
                        created_edge_ids.append(new_edge_id)
                        tombstoned_edge_ids.append(int(edge["id"]))
                        changed_edge_ids.extend([new_edge_id, int(edge["id"])])
                        record["edge_ids"].extend([new_edge_id, int(edge["id"])])
                disposition_records.append(record)

            successor_edge_id = None
            if clean_outcome == "superseded_by":
                successor = resolve_active(conn, int(superseded_by))
                if successor["active_id"] is None or int(successor["active_id"]) == active_id:
                    raise WorkstreamValidationError("superseded_by target must be another active workstream")
                successor_edge_id = _add_edge_id(
                    conn, active_id, int(successor["active_id"]), "closed_in_favor_of",
                    created_by="lifecycle:close",
                )
                created_edge_ids.append(successor_edge_id)
                changed_edge_ids.append(successor_edge_id)

            priority_snapshots = _priority_snapshot(conn, active_id)
            retired_priority_ids: list[int] = []
            priority_rows = conn.execute(
                "SELECT id FROM nodes WHERE kind = 'priority' AND status = 'canonical' "
                "AND workstream_id = ? ORDER BY id",
                (active_id,),
            ).fetchall()
            for priority in priority_rows:
                retired = priorities.retire_priority_nc(conn, int(priority["id"]))
                if retired.get("retired"):
                    retired_priority_ids.append(int(priority["id"]))

            receipt = lifecycle_receipts.closed(ws["title"], clean_outcome, clean_reason)
            closed_body = rolling.append_epilogue(
                ws["body"], f"Closed ({clean_outcome}): {clean_reason}.",
                date=_date(), op_key=key,
            )
            db.update_node_nc(conn, active_id, body=closed_body, status="stale")
            db.set_focus_row_nc(
                conn, active_id, score=0.0, set_at=db._now(), set_by="lifecycle",
                pinned=False, rank=int(prior_focus["rank"]) if prior_focus else 0,
            )
            db.recompute_focus_ranks_nc(conn)
            unhandled = sorted(set(missing) | set(keep_open))
            payload = {
                "request": request,
                "title": ws["title"],
                "receipt": receipt,
                "prior_status": prior_status,
                "feeder_snapshot": snapshot,
                "feeder_dispositions": disposition_records,
                "feeder_disposition_edge_ids": sorted(set(changed_edge_ids)),
                "created_edge_ids": sorted(set(created_edge_ids)),
                "tombstoned_edge_ids": sorted(set(tombstoned_edge_ids)),
                "repointed_member_ids": repointed_member_ids,
                "released_member_ids": released_member_ids,
                "unhandled_feeder_ids": unhandled,
                "focus": prior_focus,
                "retired_priority_ids": retired_priority_ids,
                "priority_snapshots": priority_snapshots,
                "successor_edge_id": successor_edge_id,
                "backup_path": backup_path,
            }
            row = db.begin_workstream_op_nc(
                conn,
                op_key=key,
                op="CLOSE",
                origin=clean_origin,
                payload=payload,
                candidate_key=candidate_key or f"close:{active_id}",
                session_id=session_id,
                src_workstream_id=active_id,
                dst_workstream_id=(int(superseded_by) if superseded_by is not None else None),
                forced=force,
                preflight_token=expected_token,
            )
            row = db.finish_workstream_op_nc(conn, key, state="applied")
            conn.commit()
            result = _applied_result(row)
        except Exception:
            conn.rollback()
            raise
    lifecycle_receipts.emit_applied(result, project_path=lock_path, session_id=session_id)
    return result


def reopen_workstream(
    conn: sqlite3.Connection,
    workstream_id: int,
    *,
    reason: str,
    op_key: str,
    origin: str = "manual",
    session_id: str | None = None,
    project_path: str | None = None,
) -> dict:
    """Reopen a closed identity, or redirect a merged-away identity."""
    requested_id = int(workstream_id)
    clean_reason = _clean_text("reason", reason)
    key = _clean_text("op_key", op_key)
    clean_origin = _clean_text("origin", origin).lower()
    request = {
        "requested_workstream_id": requested_id,
        "reason": clean_reason,
        "origin": clean_origin,
    }
    _require_clean_connection(conn)
    prior = _existing_result(conn, op_key=key, request=request)
    if prior is not None:
        return prior
    resolution = resolve_active(conn, requested_id)
    if resolution["state"] in {"active", "merged"}:
        return {
            "ok": True, "state": "active", "already": True,
            "requested_workstream_id": requested_id,
            "workstream_id": resolution["active_id"],
            "redirected": resolution["state"] == "merged",
            "resolution": resolution,
        }
    if resolution["state"] != "closed":
        raise WorkstreamValidationError(f"cannot REOPEN identity in state {resolution['state']}")

    lock_path = _project_path(project_path)
    result: dict
    with lockfile.writer_lock(lock_path):
        backup_path = _backup_before_mutation(conn, op="REOPEN", op_key=key)
        conn.execute("BEGIN IMMEDIATE")
        try:
            prior = _existing_result(conn, op_key=key, request=request)
            if prior is not None:
                conn.commit()
                return prior
            resolution = resolve_active(conn, requested_id)
            if resolution["state"] != "closed":
                failed = _finish_failed_nc(
                    conn, op_key=key, op="REOPEN", origin=clean_origin,
                    request=request, error_code="preflight_stale", session_id=session_id,
                    src_workstream_id=requested_id,
                )
                conn.commit()
                return failed
            ws = resolution["node"]
            close_row = conn.execute(
                "SELECT payload_json FROM workstream_ops WHERE op = 'CLOSE' "
                "AND state = 'applied' AND src_workstream_id = ? ORDER BY id DESC LIMIT 1",
                (requested_id,),
            ).fetchone()
            restored_status = "staging"
            if close_row is not None:
                try:
                    close_payload = json.loads(close_row["payload_json"])
                    candidate = close_payload.get("prior_status")
                    if candidate in ACTIVE_STATUSES:
                        restored_status = candidate
                except (TypeError, json.JSONDecodeError):
                    pass
            reopened_body = rolling.apply(
                ws["body"], f"Reopened: {clean_reason}.", date=_date(),
            )
            db.update_node_nc(conn, requested_id, body=reopened_body, status=restored_status)
            db.bump_focus_nc(conn, requested_id, delta=db.FOCUS_DEFAULT_DELTA, set_by="lifecycle")
            receipt = lifecycle_receipts.reopened(ws["title"], clean_reason)
            payload = {
                "request": request,
                "title": ws["title"],
                "receipt": receipt,
                "prior_status": "stale",
                "restored_status": restored_status,
                "backup_path": backup_path,
            }
            row = db.begin_workstream_op_nc(
                conn,
                op_key=key,
                op="REOPEN",
                origin=clean_origin,
                payload=payload,
                candidate_key=f"reopen:{requested_id}",
                session_id=session_id,
                src_workstream_id=requested_id,
                dst_workstream_id=requested_id,
            )
            row = db.finish_workstream_op_nc(conn, key, state="applied")
            conn.commit()
            result = _applied_result(row)
        except Exception:
            conn.rollback()
            raise
    lifecycle_receipts.emit_applied(result, project_path=lock_path, session_id=session_id)
    return result


def _normalize_relations(relations: Mapping[int | str, str] | None) -> dict[int, str]:
    out: dict[int, str] = {}
    for raw_id, raw_relation in (relations or {}).items():
        node_id = int(raw_id)
        relation = db.canonicalize_relation(str(raw_relation).strip())
        if relation not in feeders.FEEDER_RELATIONS:
            raise WorkstreamValidationError(f"invalid feeder relation {raw_relation!r}")
        out[node_id] = relation
    return out


def adopt_nodes(
    conn: sqlite3.Connection,
    workstream_id: int,
    node_ids: Sequence[int],
    *,
    op_key: str,
    relations: Mapping[int | str, str] | None = None,
    evidence: Mapping[str, Any] | None = None,
    origin: str = "manual",
    allow_auto_apply: bool = False,
    session_id: str | None = None,
    project_path: str | None = None,
    candidate_key: str | None = None,
) -> dict:
    """Batch-assign nodes and optional forward intent edges to an active lane."""
    requested_id = int(workstream_id)
    members = sorted({int(node_id) for node_id in node_ids})
    if not members:
        raise WorkstreamValidationError("node_ids must not be empty")
    key = _clean_text("op_key", op_key)
    clean_origin = _clean_text("origin", origin).lower()
    normalized_relations = _normalize_relations(relations)
    if set(normalized_relations) - set(members):
        raise WorkstreamValidationError("relations may only name adopted node_ids")
    evidence_payload = dict(evidence or {})
    trigger = str(evidence_payload.get("trigger") or "").lower()
    forward = bool(evidence_payload.get("forward_looking"))
    if clean_origin == "auto" and not allow_auto_apply:
        raise WorkstreamValidationError("standalone automatic ADOPT is candidate-only")
    if clean_origin == "auto" and (not forward or trigger in {"cluster", "cluster_similarity", "cosine"}):
        raise WorkstreamValidationError("automatic ADOPT requires non-cosine forward-looking evidence")
    request = {
        "requested_workstream_id": requested_id,
        "node_ids": members,
        "relations": {str(k): v for k, v in sorted(normalized_relations.items())},
        "evidence": evidence_payload,
        "origin": clean_origin,
        "allow_auto_apply": bool(allow_auto_apply),
    }
    _require_clean_connection(conn)
    prior = _existing_result(conn, op_key=key, request=request)
    if prior is not None:
        return prior
    initial_resolution = resolve_active(conn, requested_id)
    if initial_resolution["active_id"] is None:
        raise WorkstreamValidationError("ADOPT target must resolve to an active workstream")
    active_id = int(initial_resolution["active_id"])

    lock_path = _project_path(project_path)
    result: dict
    with lockfile.writer_lock(lock_path):
        backup_path = _backup_before_mutation(conn, op="ADOPT", op_key=key)
        conn.execute("BEGIN IMMEDIATE")
        try:
            prior = _existing_result(conn, op_key=key, request=request)
            if prior is not None:
                conn.commit()
                return prior
            locked_snapshot = []
            nodes: dict[int, dict] = {}
            for node_id in members:
                node = _row(conn, node_id)
                locked_snapshot.append({
                    "id": node_id,
                    "kind": node.get("kind") if node else None,
                    "status": node.get("status") if node else None,
                    "workstream_id": node.get("workstream_id") if node else None,
                    "updated_at": node.get("updated_at") if node else None,
                })
                if node is not None:
                    nodes[node_id] = node
            token = _snapshot_token(locked_snapshot)
            if not _auto_plan_is_current(
                conn,
                origin=clean_origin,
                candidate_key=candidate_key,
                op_key=key,
                op="ADOPT",
                operation_request={
                    "workstream_id": workstream_id,
                    "node_ids": node_ids,
                    "op_key": op_key,
                    "relations": relations,
                    "evidence": evidence,
                    "origin": origin,
                    "allow_auto_apply": allow_auto_apply,
                    "session_id": session_id,
                    "candidate_key": candidate_key,
                },
            ):
                failed = _finish_failed_nc(
                    conn,
                    op_key=key,
                    op="ADOPT",
                    origin=clean_origin,
                    request=request,
                    error_code="preflight_stale",
                    session_id=session_id,
                    dst_workstream_id=active_id,
                    preflight_token=token,
                    candidate_key=candidate_key,
                    failure_payload={"governor": "auto_plan_stale"},
                )
                conn.commit()
                return failed
            resolution = resolve_active(conn, requested_id)
            if resolution["active_id"] is None or int(resolution["active_id"]) != active_id:
                failed = _finish_failed_nc(
                    conn, op_key=key, op="ADOPT", origin=clean_origin,
                    request=request, error_code="preflight_stale", session_id=session_id,
                    dst_workstream_id=active_id, preflight_token=token,
                    candidate_key=candidate_key,
                )
                conn.commit()
                return failed
            for node_id in members:
                node = nodes.get(node_id)
                if (
                    node is None
                    or node["kind"] in {"workstream", "priority"}
                    or node["status"] == "stale"
                ):
                    raise WorkstreamValidationError(f"invalid ADOPT node id={node_id}")
            prior_memberships = {str(node_id): nodes[node_id]["workstream_id"] for node_id in members}
            db.set_node_workstream_nc(conn, members, active_id)
            edge_ids: list[int] = []
            for node_id, relation in sorted(normalized_relations.items()):
                edge_ids.append(_add_edge_id(
                    conn, node_id, active_id, relation, created_by="lifecycle:adopt",
                ))
            ws = _row(conn, active_id)
            assert ws is not None
            receipt = lifecycle_receipts.adopted(len(members), ws["title"])
            payload = {
                "request": request,
                "title": ws["title"],
                "receipt": receipt,
                "assigned_member_ids": members,
                "prior_memberships": prior_memberships,
                "created_edge_ids": edge_ids,
                "evidence": evidence_payload,
                "backup_path": backup_path,
            }
            row = db.begin_workstream_op_nc(
                conn,
                op_key=key,
                op="ADOPT",
                origin=clean_origin,
                payload=payload,
                candidate_key=(
                    candidate_key
                    or f"adopt:{active_id}:{_snapshot_token(members)[:16]}"
                ),
                session_id=session_id,
                src_workstream_id=None,
                dst_workstream_id=active_id,
                preflight_token=token,
            )
            row = db.finish_workstream_op_nc(conn, key, state="applied")
            conn.commit()
            result = _applied_result(row)
        except Exception:
            conn.rollback()
            raise
    lifecycle_receipts.emit_applied(result, project_path=lock_path, session_id=session_id)
    return result


MERGE_UNKNOWN_ACTIONS = frozenset({"rehome", "preserve", "tombstone"})


def _focus_snapshot(conn: sqlite3.Connection, workstream_id: int) -> dict | None:
    row = db.get_focus_row(conn, workstream_id)
    return dict(row) if row is not None else None


def _priority_snapshot(conn: sqlite3.Connection, workstream_id: int) -> list[dict]:
    # Legacy generic node writes could create a lane-scoped priority without
    # its ordering side-table row. list_priorities() cannot see such a node, so
    # lifecycle operations must stop before producing an incomplete receipt.
    malformed = conn.execute(
        "SELECT n.id FROM nodes n "
        "LEFT JOIN priority_order po ON po.node_id = n.id "
        "WHERE n.kind = 'priority' AND n.status != 'stale' AND n.workstream_id = ? "
        "AND po.node_id IS NULL ORDER BY n.id",
        (workstream_id,),
    ).fetchall()
    if malformed:
        ids = [int(row["id"]) for row in malformed]
        raise WorkstreamValidationError(
            f"workstream {workstream_id} has priority nodes missing "
            f"priority_order metadata: {ids}"
        )
    rows = priorities.list_priorities(conn, workstream_id=workstream_id)
    out: list[dict] = []
    for item in rows:
        order = conn.execute(
            "SELECT rank, retired_at FROM priority_order WHERE node_id = ?",
            (item["id"],),
        ).fetchone()
        out.append({
            "id": int(item["id"]),
            "title": item["title"],
            "body": item["body"],
            "status": item["status"],
            "session_id": item.get("session_id"),
            "workstream_id": item.get("workstream_id"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "updated_by": item.get("updated_by"),
            "rank": order["rank"] if order is not None else None,
            "retired_at": order["retired_at"] if order is not None else None,
            "effective_rank": item.get("effective_rank"),
        })
    return out


def _merge_snapshot(
    conn: sqlite3.Connection, source_id: int, absorber_id: int,
) -> dict:
    source = _row(conn, source_id)
    absorber = _row(conn, absorber_id)
    members = [dict(row) for row in conn.execute(
        "SELECT id, kind, status, workstream_id, updated_at, updated_by FROM nodes "
        "WHERE workstream_id = ? AND kind NOT IN ('workstream', 'priority') "
        "ORDER BY id",
        (source_id,),
    ).fetchall()]
    inbound = [dict(row) for row in conn.execute(
        """
        SELECT e.id, e.src, e.dst, e.relation, e.status, e.created_by,
               n.kind AS src_kind, n.status AS src_status
        FROM edges e
        JOIN nodes n ON n.id = e.src
        WHERE e.dst = ? AND e.status = 'active' AND n.kind != 'workstream'
        ORDER BY e.id
        """,
        (source_id,),
    ).fetchall()]
    compact_source = None if source is None else {
        "id": source["id"], "kind": source["kind"], "status": source["status"],
        "title": source["title"], "body_hash": _snapshot_token(source["body"]),
        "updated_at": source["updated_at"],
    }
    compact_absorber = None if absorber is None else {
        "id": absorber["id"], "kind": absorber["kind"], "status": absorber["status"],
        "title": absorber["title"], "body_hash": _snapshot_token(absorber["body"]),
        "updated_at": absorber["updated_at"],
    }
    source_priorities = _priority_snapshot(conn, source_id)
    absorber_priorities = _priority_snapshot(conn, absorber_id)
    return {
        "source": compact_source,
        "absorber": compact_absorber,
        "members": members,
        "inbound_edges": inbound,
        "src_focus": _focus_snapshot(conn, source_id),
        "dst_focus": _focus_snapshot(conn, absorber_id),
        "source_priorities": source_priorities,
        "absorber_priority_ids": [
            int(item["id"])
            for item in absorber_priorities
        ],
    }


def merge_preflight(
    conn: sqlite3.Connection,
    source_workstream_id: int,
    absorber_workstream_id: int,
) -> dict:
    """Read-only merge snapshot, including relations that need disposition."""
    source_id = int(source_workstream_id)
    absorber_id = int(absorber_workstream_id)
    source_resolution = resolve_active(conn, source_id)
    absorber_resolution = resolve_active(conn, absorber_id)
    snapshot = _merge_snapshot(conn, source_id, absorber_id)
    unknown = [
        edge for edge in snapshot["inbound_edges"]
        if edge["relation"] not in feeders.FEEDER_RELATIONS
    ]
    return {
        "source_resolution": source_resolution,
        "absorber_resolution": absorber_resolution,
        "snapshot": snapshot,
        "unknown_inbound_edges": unknown,
        "token": _snapshot_token(snapshot),
        "acyclic": _merge_edge_is_safe(conn, source_id, absorber_id),
    }


def _merge_edge_is_safe(
    conn: sqlite3.Connection, source_id: int, absorber_id: int, *, max_hops: int = 32,
) -> bool:
    """Fail closed if the proposed identity edge enters a malformed chain."""
    source_targets = conn.execute(
        "SELECT dst FROM edges WHERE src = ? AND relation = 'merged_into' AND status = 'active'",
        (source_id,),
    ).fetchall()
    if source_targets:
        return False
    current = absorber_id
    visited: set[int] = set()
    for _ in range(max_hops):
        if current == source_id or current in visited:
            return False
        visited.add(current)
        targets = conn.execute(
            "SELECT DISTINCT dst FROM edges WHERE src = ? AND relation = 'merged_into' "
            "AND status = 'active' ORDER BY dst",
            (current,),
        ).fetchall()
        if not targets:
            return True
        if len(targets) != 1:
            return False
        current = int(targets[0]["dst"])
    return False


def _normalize_merge_dispositions(
    dispositions: Mapping[int | str, Any] | None,
) -> dict[int, str]:
    out: dict[int, str] = {}
    for raw_id, raw_value in (dispositions or {}).items():
        edge_id = int(raw_id)
        if isinstance(raw_value, Mapping):
            action = str(raw_value.get("action") or "").strip().lower()
        else:
            action = str(raw_value).strip().lower()
        if action == "keep":
            action = "preserve"
        if action not in MERGE_UNKNOWN_ACTIONS:
            raise WorkstreamValidationError(
                f"invalid MERGE edge disposition {action!r} for edge {edge_id}"
            )
        out[edge_id] = action
    return out


def _edge_target_state(
    conn: sqlite3.Connection, src: int, dst: int, relation: str,
) -> dict | None:
    row = conn.execute(
        "SELECT id, status, created_by FROM edges WHERE src = ? AND dst = ? AND relation = ?",
        (src, dst, db.canonicalize_relation(relation)),
    ).fetchone()
    return dict(row) if row is not None else None


def _next_workstream_op_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'workstream_ops'"
    ).fetchone()
    return int(row["seq"] if row is not None else 0) + 1


def _copy_merge_priorities_nc(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    absorber_id: int,
    source_priorities: list[dict],
) -> dict:
    absorber_active = priorities.list_priorities(conn, workstream_id=absorber_id)
    capacity = max(0, int(priorities.MAX_ACTIVE) - len(absorber_active))
    accepted = source_priorities[:capacity]
    overflow = source_priorities[capacity:]
    retired_ids: list[int] = []
    readded_ids: list[int] = []
    priority_map: list[dict] = []

    existing_times = [db._parse_ts(item.get("created_at")) for item in absorber_active]
    existing_times = [stamp for stamp in existing_times if stamp is not None]
    base = min(existing_times) if existing_times else datetime.now(timezone.utc)
    from datetime import timedelta

    for item in source_priorities:
        retired = priorities.retire_priority_nc(conn, int(item["id"]))
        if retired.get("retired"):
            retired_ids.append(int(item["id"]))
    for index, item in enumerate(accepted, start=1):
        new_id = db.insert_node_nc(
            conn,
            kind=priorities.PRIORITY_KIND,
            title=item["title"],
            body=item["body"],
            status=priorities.ACTIVE_STATUS,
            session_id=item.get("session_id"),
            embedding=None,
            workstream_id=absorber_id,
        )
        # Floating priority ordering is recency based.  Place copies just older
        # than every existing floating row so they form an unlocked tail while
        # retaining the source lane's effective order.
        tail_stamp = (base - timedelta(seconds=index)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE nodes SET created_at = ?, updated_at = ? WHERE id = ?",
            (tail_stamp, tail_stamp, new_id),
        )
        conn.execute(
            "INSERT INTO priority_order(node_id, rank, retired_at) VALUES (?, NULL, NULL)",
            (new_id,),
        )
        readded_ids.append(new_id)
        priority_map.append({"source_priority_id": int(item["id"]), "copy_priority_id": new_id})
    return {
        "retired_priority_ids": retired_ids,
        "readded_priority_ids": readded_ids,
        "overflow_retired_priority_ids": [int(item["id"]) for item in overflow],
        "priority_map": priority_map,
    }


_PRIORITY_COPY_FIELDS = (
    "id", "kind", "title", "body", "status", "session_id", "created_at",
    "updated_at", "ref_count", "last_referenced_at", "retention_tier",
    "parent_id", "depth", "created_by", "updated_by", "workstream_id",
    "content_hash",
)


def _priority_copy_snapshot(conn: sqlite3.Connection, node_id: int) -> dict | None:
    node = _row(conn, node_id)
    if node is None:
        return None
    order = conn.execute(
        "SELECT rank, retired_at FROM priority_order WHERE node_id = ?",
        (int(node_id),),
    ).fetchone()
    return {
        **{field: node.get(field) for field in _PRIORITY_COPY_FIELDS},
        "embedding_is_null": node.get("embedding") is None,
        "priority_rank": order["rank"] if order is not None else None,
        "priority_retired_at": order["retired_at"] if order is not None else None,
        "priority_order_present": order is not None,
    }


def _priority_copy_reference_kinds(
    conn: sqlite3.Connection, node_id: int,
) -> list[str]:
    """Return post-MERGE references that a hard delete would destroy/orphan."""
    checks = {
        "edge": (
            "SELECT 1 FROM edges WHERE src = ? OR dst = ? LIMIT 1",
            (node_id, node_id),
        ),
        "node_child": (
            "SELECT 1 FROM nodes WHERE parent_id = ? OR workstream_id = ? LIMIT 1",
            (node_id, node_id),
        ),
        "session_retrieval": (
            "SELECT 1 FROM session_retrievals WHERE node_id = ? LIMIT 1", (node_id,),
        ),
        "retrieval_event": (
            "SELECT 1 FROM retrieval_events WHERE node_id = ? LIMIT 1", (node_id,),
        ),
        "artifact": (
            "SELECT 1 FROM node_artifact WHERE node_id = ? LIMIT 1", (node_id,),
        ),
        "seed_import": (
            "SELECT 1 FROM seed_import WHERE node_id = ? OR workstream_id = ? LIMIT 1",
            (node_id, node_id),
        ),
        "profile_config": (
            "SELECT 1 FROM profile_config WHERE profile_node_id = ? LIMIT 1", (node_id,),
        ),
        "profile_binding": (
            "SELECT 1 FROM profile_binding WHERE profile_node_id = ? LIMIT 1", (node_id,),
        ),
    }
    return [
        kind for kind, (sql, params) in checks.items()
        if conn.execute(sql, params).fetchone() is not None
    ]


def merge_workstreams(
    conn: sqlite3.Connection,
    source_workstream_id: int,
    absorber_workstream_id: int,
    *,
    op_key: str,
    dispositions: Mapping[int | str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    origin: str = "manual",
    force: bool = False,
    preflight_token: str | None = None,
    session_id: str | None = None,
    project_path: str | None = None,
    candidate_key: str | None = None,
) -> dict:
    """Merge one active lane into another with a complete reversal payload."""
    source_id = int(source_workstream_id)
    absorber_id = int(absorber_workstream_id)
    if source_id == absorber_id:
        raise WorkstreamValidationError("MERGE source and absorber must differ")
    key = _clean_text("op_key", op_key)
    clean_origin = _clean_text("origin", origin).lower()
    if clean_origin == "auto" and force:
        raise WorkstreamValidationError("automatic MERGE cannot force past safety rails")
    normalized_dispositions = _normalize_merge_dispositions(dispositions)
    evidence_payload = dict(evidence or {})
    request = {
        "source_workstream_id": source_id,
        "absorber_workstream_id": absorber_id,
        "dispositions": {str(k): v for k, v in sorted(normalized_dispositions.items())},
        "evidence": evidence_payload,
        "origin": clean_origin,
        "force": bool(force),
    }
    _require_clean_connection(conn)
    prior = _existing_result(conn, op_key=key, request=request)
    if prior is not None:
        return prior
    expected_token = preflight_token

    lock_path = _project_path(project_path)
    result: dict
    with lockfile.writer_lock(lock_path):
        backup_path = _backup_before_mutation(conn, op="MERGE", op_key=key)
        conn.execute("BEGIN IMMEDIATE")
        try:
            prior = _existing_result(conn, op_key=key, request=request)
            if prior is not None:
                conn.commit()
                return prior
            locked = merge_preflight(conn, source_id, absorber_id)
            if locked["source_resolution"]["state"] != "active":
                raise WorkstreamValidationError("MERGE source must be an active identity")
            if locked["absorber_resolution"]["state"] != "active":
                raise WorkstreamValidationError("MERGE absorber must be an active identity")
            if not locked["acyclic"]:
                raise WorkstreamValidationError(
                    "MERGE would enter a cyclic or malformed identity chain"
                )
            if expected_token is None:
                expected_token = locked["token"]
            if not _auto_plan_is_current(
                conn,
                origin=clean_origin,
                candidate_key=candidate_key,
                op_key=key,
                op="MERGE",
                operation_request={
                    "source_workstream_id": source_workstream_id,
                    "absorber_workstream_id": absorber_workstream_id,
                    "op_key": op_key,
                    "dispositions": dispositions,
                    "evidence": evidence,
                    "origin": origin,
                    "force": force,
                    "preflight_token": preflight_token,
                    "session_id": session_id,
                    "candidate_key": candidate_key,
                },
            ):
                failed = _finish_failed_nc(
                    conn,
                    op_key=key,
                    op="MERGE",
                    origin=clean_origin,
                    request=request,
                    error_code="preflight_stale",
                    session_id=session_id,
                    src_workstream_id=source_id,
                    dst_workstream_id=absorber_id,
                    forced=force,
                    preflight_token=expected_token,
                    candidate_key=candidate_key,
                    failure_payload={"governor": "auto_plan_stale"},
                )
                conn.commit()
                return failed
            if (
                locked["token"] != expected_token
                or locked["source_resolution"]["state"] != "active"
                or locked["absorber_resolution"]["state"] != "active"
                or not locked["acyclic"]
            ):
                failed = _finish_failed_nc(
                    conn, op_key=key, op="MERGE", origin=clean_origin,
                    request=request, error_code="preflight_stale", session_id=session_id,
                    src_workstream_id=source_id, dst_workstream_id=absorber_id,
                    forced=force, preflight_token=expected_token,
                    candidate_key=candidate_key,
                )
                conn.commit()
                return failed
            snapshot = locked["snapshot"]
            unknown_ids = {int(edge["id"]) for edge in locked["unknown_inbound_edges"]}
            disposition_ids = set(normalized_dispositions)
            if disposition_ids - unknown_ids:
                raise WorkstreamValidationError(
                    f"MERGE dispositions name non-unknown edges {sorted(disposition_ids - unknown_ids)}"
                )
            missing = sorted(unknown_ids - disposition_ids)
            if missing and not force:
                failed = _finish_failed_nc(
                    conn, op_key=key, op="MERGE", origin=clean_origin,
                    request=request, error_code="blocked", session_id=session_id,
                    src_workstream_id=source_id, dst_workstream_id=absorber_id,
                    forced=force, preflight_token=expected_token,
                    candidate_key=candidate_key,
                )
                conn.commit()
                return failed

            source = _row(conn, source_id)
            absorber = _row(conn, absorber_id)
            assert source is not None and absorber is not None
            source_body_hash = _snapshot_token(source["body"])
            members = [int(item["id"]) for item in snapshot["members"]]
            prior_memberships = {str(item["id"]): item["workstream_id"] for item in snapshot["members"]}
            prior_member_metadata = {
                str(item["id"]): {
                    "updated_at": item.get("updated_at"),
                    "updated_by": item.get("updated_by"),
                }
                for item in snapshot["members"]
            }
            db.set_node_workstream_nc(conn, members, absorber_id)
            post_member_metadata = {
                str(node_id): {
                    "updated_at": node.get("updated_at") if node else None,
                    "updated_by": node.get("updated_by") if node else None,
                }
                for node_id in members
                for node in (_row(conn, node_id),)
            }
            _failure_point("merge_after_members")

            rehome_records: list[dict] = []
            rehomed_edge_ids: list[int] = []
            tombstoned_edge_ids: list[int] = []
            preserved_unknown_edge_ids: list[int] = []
            unhandled_unknown_edge_ids = missing if force else []
            for edge in snapshot["inbound_edges"]:
                old_id = int(edge["id"])
                relation = str(edge["relation"])
                action = "rehome" if relation in feeders.FEEDER_RELATIONS else normalized_dispositions.get(old_id)
                if action is None:  # forced unknown relation: leave it visibly attached to stale source
                    continue
                if action == "preserve":
                    preserved_unknown_edge_ids.append(old_id)
                    continue
                target_before = None
                new_id = None
                if action == "rehome":
                    target_before = _edge_target_state(
                        conn, int(edge["src"]), absorber_id, relation,
                    )
                    new_id = _add_edge_id(
                        conn, int(edge["src"]), absorber_id, relation,
                        created_by="lifecycle:merge",
                    )
                    rehomed_edge_ids.append(new_id)
                _tombstone_edge_id(conn, edge)
                tombstoned_edge_ids.append(old_id)
                rehome_records.append({
                    "old_edge": {
                        "id": old_id, "src": int(edge["src"]), "dst": int(edge["dst"]),
                        "relation": relation, "created_by": edge.get("created_by"),
                    },
                    "action": action,
                    "new_edge_id": new_id,
                    "target_before": target_before,
                })
            _failure_point("merge_after_edges")

            priority_changes = _copy_merge_priorities_nc(
                conn,
                source_id=source_id,
                absorber_id=absorber_id,
                source_priorities=snapshot["source_priorities"],
            )
            priority_changes["created_priority_snapshots"] = [
                _priority_copy_snapshot(conn, int(node_id))
                for node_id in priority_changes["readded_priority_ids"]
            ]

            src_focus = snapshot["src_focus"]
            dst_focus = snapshot["dst_focus"]
            focus_rows = [item for item in (src_focus, dst_focus) if item is not None]
            db.delete_focus_row_nc(conn, source_id)
            if focus_rows:
                score = max(db._decay_score(item["score"], item["set_at"]) for item in focus_rows)
                pinned = any(bool(item["pinned"]) for item in focus_rows)
                db.set_focus_row_nc(
                    conn, absorber_id, score=score, set_at=db._now(),
                    set_by="lifecycle", pinned=pinned, rank=0,
                )
            db.recompute_focus_ranks_nc(conn)
            post_focus = _focus_snapshot(conn, absorber_id)

            rolling_text = f'Merged workstream "{source["title"]}" into this workstream.'
            absorber_body_before = absorber["body"]
            absorber_body_after, rolling_line = rolling.apply_keyed(
                absorber_body_before, rolling_text, date=_date(), op_key=key,
            )
            db.update_node_nc(conn, absorber_id, body=absorber_body_after)
            db.update_node_nc(conn, source_id, status="stale")
            source_after_merge = _row(conn, source_id)
            absorber_after_merge = _row(conn, absorber_id)
            assert source_after_merge is not None and absorber_after_merge is not None
            merge_edge_id = _add_edge_id(
                conn, source_id, absorber_id, "merged_into", created_by="lifecycle:merge",
            )
            _failure_point("merge_before_ledger")

            predicted_receipt_id = _next_workstream_op_id(conn)
            coactive = int(evidence_payload.get("coactive_sessions") or 0)
            window = int(evidence_payload.get("window_sessions") or 0)
            receipt = lifecycle_receipts.merged(
                source["title"], absorber["title"], coactive, window, predicted_receipt_id,
            )
            payload = {
                "request": request,
                "title": absorber["title"],
                "source_title": source["title"],
                "receipt": receipt,
                "repointed_member_ids": members,
                "prior_memberships": prior_memberships,
                "prior_member_metadata": prior_member_metadata,
                "post_member_metadata": post_member_metadata,
                "rehomed_edge_ids": sorted(set(rehomed_edge_ids)),
                "tombstoned_edge_ids": sorted(set(tombstoned_edge_ids)),
                "edge_rehomes": rehome_records,
                "preserved_unknown_edge_ids": preserved_unknown_edge_ids,
                "unhandled_unknown_edge_ids": unhandled_unknown_edge_ids,
                **priority_changes,
                "priority_snapshots": snapshot["source_priorities"],
                "src_focus": src_focus,
                "dst_focus": dst_focus,
                "post_focus": post_focus,
                "rolling_line": rolling_line,
                "rolling_op_key": key,
                "absorber_body_before": absorber_body_before,
                "absorber_body_before_hash": _snapshot_token(absorber_body_before),
                "absorber_body_after_hash": _snapshot_token(absorber_body_after),
                "source_body_hash": source_body_hash,
                "source_prior_status": source["status"],
                "source_prior_updated_at": source.get("updated_at"),
                "source_prior_updated_by": source.get("updated_by"),
                "source_post_updated_at": source_after_merge.get("updated_at"),
                "source_post_updated_by": source_after_merge.get("updated_by"),
                "absorber_prior_updated_at": absorber.get("updated_at"),
                "absorber_prior_updated_by": absorber.get("updated_by"),
                "absorber_post_updated_at": absorber_after_merge.get("updated_at"),
                "absorber_post_updated_by": absorber_after_merge.get("updated_by"),
                "merge_edge_id": merge_edge_id,
                "backup_path": backup_path,
            }
            row = db.begin_workstream_op_nc(
                conn,
                op_key=key,
                op="MERGE",
                origin=clean_origin,
                payload=payload,
                candidate_key=candidate_key or f"merge:{source_id}:{absorber_id}",
                session_id=session_id,
                src_workstream_id=source_id,
                dst_workstream_id=absorber_id,
                forced=force,
                preflight_token=expected_token,
            )
            if int(row["id"]) != predicted_receipt_id:
                raise WorkstreamLifecycleError("MERGE receipt sequence changed during transaction")
            row = db.finish_workstream_op_nc(conn, key, state="applied")
            conn.commit()
            result = _applied_result(row)
        except Exception:
            conn.rollback()
            raise
    lifecycle_receipts.emit_applied(result, project_path=lock_path, session_id=session_id)
    return result


def _restore_focus_nc(
    conn: sqlite3.Connection, workstream_id: int, snapshot: Mapping[str, Any] | None,
) -> None:
    if snapshot is None:
        db.delete_focus_row_nc(conn, workstream_id)
    else:
        db.restore_focus_row_nc(conn, snapshot)


def _focus_reversal_state(snapshot: Mapping[str, Any] | None) -> dict | None:
    """Focus fields whose drift can make MERGE reversal unsafe.

    ``rank`` is a derived display position and ``set_by`` is attribution, not
    focus state.  Either may change without changing the score/timestamp/pin
    tuple that MERGE actually combined.
    """
    if snapshot is None:
        return None
    return {
        "score": snapshot.get("score"),
        "set_at": snapshot.get("set_at"),
        "pinned": snapshot.get("pinned"),
    }


def _merge_unmerge_drift(
    conn: sqlite3.Connection,
    merge_row: Mapping[str, Any],
) -> list[str]:
    payload = merge_row.get("payload") or {}
    source_id = int(merge_row["src_workstream_id"])
    absorber_id = int(merge_row["dst_workstream_id"])
    drift: list[str] = []
    source = _row(conn, source_id)
    absorber = _row(conn, absorber_id)
    if source is None or source["status"] != "stale":
        drift.append("source_status")
    elif _snapshot_token(source["body"]) != payload.get("source_body_hash"):
        drift.append("source_body")
    elif source["title"] != payload.get("source_title"):
        drift.append("source_title")
    if absorber is None or absorber["status"] == "stale":
        drift.append("absorber_status")
    merge_edge = conn.execute(
        "SELECT status FROM edges WHERE id = ?", (payload.get("merge_edge_id"),),
    ).fetchone()
    if merge_edge is None or merge_edge["status"] != "active":
        drift.append("merge_edge")
    resolved = resolve_active(conn, source_id)
    if resolved["active_id"] != absorber_id:
        drift.append("identity_chain")
    for node_id in payload.get("repointed_member_ids") or []:
        node = _row(conn, int(node_id))
        if node is None or node.get("workstream_id") != absorber_id:
            drift.append(f"member:{node_id}")
            continue
        expected_metadata = (payload.get("post_member_metadata") or {}).get(
            str(node_id),
        )
        if expected_metadata is not None and (
            node.get("updated_at") != expected_metadata.get("updated_at")
            or node.get("updated_by") != expected_metadata.get("updated_by")
        ):
            drift.append(f"member_metadata:{node_id}")
    for record in payload.get("edge_rehomes") or []:
        old = conn.execute(
            "SELECT status FROM edges WHERE id = ?", (record["old_edge"]["id"],),
        ).fetchone()
        if old is None or old["status"] != "tombstoned":
            drift.append(f"old_edge:{record['old_edge']['id']}")
        new_id = record.get("new_edge_id")
        if new_id is not None:
            new = conn.execute("SELECT status FROM edges WHERE id = ?", (new_id,)).fetchone()
            if new is None or new["status"] != "active":
                drift.append(f"new_edge:{new_id}")
    if db.get_focus_row(conn, source_id) is not None:
        drift.append("source_focus")
    if _focus_reversal_state(_focus_snapshot(conn, absorber_id)) != _focus_reversal_state(
        payload.get("post_focus")
    ):
        drift.append("absorber_focus")
    for item in payload.get("priority_snapshots") or []:
        node = _row(conn, int(item["id"]))
        if (
            node is None or node["status"] != "stale"
            or node.get("workstream_id") != source_id
            or node.get("title") != item.get("title")
            or node.get("body") != item.get("body")
        ):
            drift.append(f"source_priority:{item['id']}")
    copy_ids = [int(value) for value in payload.get("readded_priority_ids") or []]
    raw_copy_snapshots = payload.get("created_priority_snapshots")
    if not isinstance(raw_copy_snapshots, list):
        raw_copy_snapshots = []
    copy_snapshots = {
        int(item["id"]): dict(item)
        for item in raw_copy_snapshots
        if isinstance(item, Mapping) and item.get("id") is not None
    }
    if set(copy_snapshots) != set(copy_ids):
        drift.append("copied_priority_snapshots")
    for node_id in copy_ids:
        expected = copy_snapshots.get(node_id)
        current = _priority_copy_snapshot(conn, node_id)
        if expected is None or current != expected:
            drift.append(f"copied_priority:{node_id}")
        for reference_kind in _priority_copy_reference_kinds(conn, node_id):
            drift.append(f"copied_priority_reference:{node_id}:{reference_kind}")
    return drift


def unmerge_workstreams(
    conn: sqlite3.Connection,
    merge_op_key: str,
    *,
    op_key: str,
    reason: str = "explicit reversal",
    origin: str = "manual",
    session_id: str | None = None,
    project_path: str | None = None,
) -> dict:
    """Reverse one applied MERGE exactly, failing closed on post-merge drift."""
    merge_key = _clean_text("merge_op_key", merge_op_key)
    key = _clean_text("op_key", op_key)
    clean_reason = _clean_text("reason", reason)
    clean_origin = _clean_text("origin", origin).lower()
    request = {
        "merge_op_key": merge_key,
        "reason": clean_reason,
        "origin": clean_origin,
    }
    _require_clean_connection(conn)
    prior = _existing_result(conn, op_key=key, request=request)
    if prior is not None:
        return prior
    merge_row = db.get_workstream_op(conn, merge_key)
    if merge_row is None or merge_row["op"] != "MERGE" or merge_row["state"] != "applied":
        raise WorkstreamValidationError("UNMERGE requires an applied MERGE receipt")
    for raw in conn.execute(
        "SELECT * FROM workstream_ops WHERE op = 'UNMERGE' AND state = 'applied' ORDER BY id"
    ).fetchall():
        existing = dict(raw)
        try:
            existing_payload = json.loads(existing["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if existing_payload.get("merge_op_key") == merge_key:
            return {
                "ok": True, "state": "applied", "already": True,
                "op": "UNMERGE", "op_key": existing["op_key"],
                "operation_id": int(existing["id"]),
                "workstream_id": merge_row["src_workstream_id"],
            }
    source_id = int(merge_row["src_workstream_id"])
    absorber_id = int(merge_row["dst_workstream_id"])

    lock_path = _project_path(project_path)
    result: dict
    with lockfile.writer_lock(lock_path):
        backup_path = _backup_before_mutation(conn, op="UNMERGE", op_key=key)
        conn.execute("BEGIN IMMEDIATE")
        try:
            prior = _existing_result(conn, op_key=key, request=request)
            if prior is not None:
                conn.commit()
                return prior
            merge_row = db.get_workstream_op(conn, merge_key)
            if merge_row is None:
                raise WorkstreamValidationError("MERGE receipt disappeared")
            drift = _merge_unmerge_drift(conn, merge_row)
            if drift:
                failed = _finish_failed_nc(
                    conn, op_key=key, op="UNMERGE", origin=clean_origin,
                    request=request, error_code="preflight_stale", session_id=session_id,
                    src_workstream_id=absorber_id, dst_workstream_id=source_id,
                    preflight_token=_snapshot_token(drift),
                )
                conn.commit()
                failed["drift"] = drift
                return failed
            payload = merge_row["payload"]
            prior_member_metadata = payload.get("prior_member_metadata") or {}
            for raw_id, prior_owner in (payload.get("prior_memberships") or {}).items():
                node_id = int(raw_id)
                metadata = prior_member_metadata.get(str(raw_id))
                if isinstance(metadata, Mapping):
                    cur = conn.execute(
                        "UPDATE nodes SET workstream_id = ?, updated_at = ?, updated_by = ? "
                        "WHERE id = ?",
                        (
                            prior_owner,
                            metadata.get("updated_at"),
                            metadata.get("updated_by"),
                            node_id,
                        ),
                    )
                    if cur.rowcount != 1:
                        raise WorkstreamLifecycleError(
                            f"MERGE member {node_id} could not be restored"
                        )
                else:
                    # Backward compatibility for MERGE receipts written before
                    # exact member update metadata was captured.
                    db.set_node_workstream_nc(conn, [node_id], prior_owner)
            _failure_point("unmerge_after_members")

            for record in payload.get("edge_rehomes") or []:
                old = record["old_edge"]
                _add_edge_id(
                    conn, int(old["src"]), int(old["dst"]), old["relation"],
                    created_by=old.get("created_by") or "lifecycle:unmerge",
                )
                new_id = record.get("new_edge_id")
                if new_id is not None:
                    target_before = record.get("target_before")
                    if target_before is None or target_before.get("status") == "tombstoned":
                        helper = getattr(db, "tombstone_edge_id_nc")
                        helper(conn, int(new_id))
            merge_edge = conn.execute(
                "SELECT id, src, dst, relation FROM edges WHERE id = ?",
                (payload["merge_edge_id"],),
            ).fetchone()
            if merge_edge is None:
                raise WorkstreamLifecycleError("MERGE identity edge disappeared")
            db.tombstone_edge_id_nc(conn, int(merge_edge["id"]))
            _failure_point("unmerge_after_edges")

            # These rows were created solely by this exact MERGE receipt.  The
            # drift preflight above proved their identity/scope/order state;
            # exact replay removes them (and priority_order via FK cascade)
            # before restoring the original source-scoped identities.
            for node_id in payload.get("readded_priority_ids") or []:
                cur = conn.execute(
                    "DELETE FROM nodes WHERE id = ? AND kind = 'priority' "
                    "AND workstream_id = ?",
                    (int(node_id), absorber_id),
                )
                if cur.rowcount != 1:
                    raise WorkstreamLifecycleError(
                        f"MERGE-created priority {node_id} could not be removed"
                    )
            for item in payload.get("priority_snapshots") or []:
                conn.execute(
                    "UPDATE nodes SET status = ?, workstream_id = ?, updated_at = ?, "
                    "updated_by = ? WHERE id = ?",
                    (
                        item["status"], item["workstream_id"], item["updated_at"],
                        item["updated_by"], int(item["id"]),
                    ),
                )
                conn.execute(
                    "INSERT INTO priority_order(node_id, rank, retired_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(node_id) DO UPDATE SET rank=excluded.rank, "
                    "retired_at=excluded.retired_at",
                    (int(item["id"]), item["rank"], item["retired_at"]),
                )

            _restore_focus_nc(conn, source_id, payload.get("src_focus"))
            _restore_focus_nc(conn, absorber_id, payload.get("dst_focus"))
            db.recompute_focus_ranks_nc(conn)

            source = _row(conn, source_id)
            absorber = _row(conn, absorber_id)
            assert source is not None and absorber is not None
            source_metadata_is_merge_owned = (
                "source_post_updated_at" in payload
                and source.get("updated_at") == payload.get("source_post_updated_at")
                and source.get("updated_by") == payload.get("source_post_updated_by")
            )
            db.update_node_nc(conn, source_id, status=payload.get("source_prior_status") or "staging")
            if "source_prior_updated_at" in payload:
                source_updated_at = (
                    payload.get("source_prior_updated_at")
                    if source_metadata_is_merge_owned
                    else source.get("updated_at")
                )
                source_updated_by = (
                    payload.get("source_prior_updated_by")
                    if source_metadata_is_merge_owned
                    else source.get("updated_by")
                )
                conn.execute(
                    "UPDATE nodes SET updated_at = ?, updated_by = ? WHERE id = ?",
                    (
                        source_updated_at,
                        source_updated_by,
                        source_id,
                    ),
                )
            restored_body, removed = rolling.remove_keyed(
                absorber["body"], payload.get("rolling_op_key") or merge_key,
            )
            correction_line = None
            exact_body_restore = (
                removed
                and _snapshot_token(absorber["body"])
                == payload.get("absorber_body_after_hash")
            )
            if exact_body_restore:
                restored_body = payload.get("absorber_body_before", restored_body)
            elif not removed:
                restored_body, correction_line = rolling.apply_keyed(
                    absorber["body"],
                    f'Correction: merge of workstream "{source["title"]}" was reversed.',
                    date=_date(), op_key=key,
                )
            db.update_node_nc(conn, absorber_id, body=restored_body)
            absorber_metadata_is_merge_owned = (
                "absorber_post_updated_at" in payload
                and absorber.get("updated_at") == payload.get("absorber_post_updated_at")
                and absorber.get("updated_by") == payload.get("absorber_post_updated_by")
            )
            if (
                exact_body_restore
                and absorber_metadata_is_merge_owned
                and "absorber_prior_updated_at" in payload
            ):
                conn.execute(
                    "UPDATE nodes SET updated_at = ?, updated_by = ? WHERE id = ?",
                    (
                        payload.get("absorber_prior_updated_at"),
                        payload.get("absorber_prior_updated_by"),
                        absorber_id,
                    ),
                )
            _failure_point("unmerge_before_ledger")

            receipt = (
                f'latch reversed merge receipt #{int(merge_row["id"])} — '
                f'restored workstream "{source["title"]}".'
            )
            unmerge_payload = {
                "request": request,
                "merge_op_key": merge_key,
                "title": source["title"],
                "receipt": receipt,
                "rolling_line_removed": bool(removed),
                "correction_line": correction_line,
                "restored_member_ids": payload.get("repointed_member_ids") or [],
                "restored_edge_ids": payload.get("tombstoned_edge_ids") or [],
                "restored_priority_ids": payload.get("retired_priority_ids") or [],
                "backup_path": backup_path,
            }
            row = db.begin_workstream_op_nc(
                conn,
                op_key=key,
                op="UNMERGE",
                origin=clean_origin,
                payload=unmerge_payload,
                candidate_key=f"unmerge:{merge_key}",
                session_id=session_id,
                src_workstream_id=absorber_id,
                dst_workstream_id=source_id,
            )
            row = db.finish_workstream_op_nc(conn, key, state="applied")
            conn.commit()
            result = _applied_result(row)
        except Exception:
            conn.rollback()
            raise
    lifecycle_receipts.emit_applied(result, project_path=lock_path, session_id=session_id)
    return result


def _reconcile_lifecycle_integrity_in_transaction(
    conn: sqlite3.Connection,
    *,
    project_path: str | None = None,
    apply_repairs: bool = True,
    backup_path: str | None = None,
) -> dict:
    """Project applied lifecycle receipts onto lane/identity state.

    Only operation-owned state is repaired.  Legacy workstreams with no applied
    lifecycle receipt are reported as unmanaged and left byte-for-byte alone;
    no synthetic operations or user-facing receipts are created.
    """
    raw_rows = conn.execute(
        "SELECT * FROM workstream_ops WHERE state = 'applied' ORDER BY id"
    ).fetchall()
    rows: list[dict] = []
    by_key: dict[str, dict] = {}
    ambiguous: list[dict] = []
    blocked_ids: set[int] = set()
    managed_ids: set[int] = set()
    expected_status: dict[int, dict] = {}
    expected_edges: dict[int, dict] = {}

    def mark_ambiguous(code: str, row: Mapping[str, Any], ids: Sequence[int | None]) -> None:
        affected = sorted({int(value) for value in ids if value is not None})
        blocked_ids.update(affected)
        ambiguous.append({
            "code": code,
            "op_key": row.get("op_key"),
            "op": row.get("op"),
            "workstream_ids": affected,
        })

    for raw in raw_rows:
        row = dict(raw)
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            payload = None
        row["payload"] = payload
        rows.append(row)
        by_key[str(row["op_key"])] = row

    for row in rows:
        op = str(row["op"])
        src = row.get("src_workstream_id")
        dst = row.get("dst_workstream_id")
        for value in (src, dst):
            if value is not None:
                managed_ids.add(int(value))
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            mark_ambiguous("missing_payload", row, (src, dst))
            continue
        if op == "OPEN":
            if dst is None:
                mark_ambiguous("missing_open_target", row, (src, dst))
                continue
            expected_status[int(dst)] = {
                "mode": "active", "fallback": "staging", "op_key": row["op_key"],
            }
        elif op == "CLOSE":
            if src is None:
                mark_ambiguous("missing_close_target", row, (src, dst))
                continue
            expected_status[int(src)] = {
                "mode": "stale", "fallback": "stale", "op_key": row["op_key"],
            }
        elif op == "REOPEN":
            target = dst if dst is not None else src
            if target is None:
                mark_ambiguous("missing_reopen_target", row, (src, dst))
                continue
            fallback = payload.get("restored_status")
            if fallback not in ACTIVE_STATUSES:
                fallback = "staging"
            expected_status[int(target)] = {
                "mode": "active", "fallback": fallback, "op_key": row["op_key"],
            }
        elif op == "MERGE":
            edge_id = payload.get("merge_edge_id")
            if src is None or dst is None or edge_id is None:
                mark_ambiguous("missing_merge_payload", row, (src, dst))
                continue
            source_id, absorber_id = int(src), int(dst)
            expected_status[source_id] = {
                "mode": "stale", "fallback": "stale", "op_key": row["op_key"],
            }
            expected_edges[int(edge_id)] = {
                "src": source_id, "dst": absorber_id, "status": "active",
                "op_key": row["op_key"],
            }
        elif op == "UNMERGE":
            merge_key = payload.get("merge_op_key")
            merge_row = by_key.get(str(merge_key)) if merge_key else None
            if (
                merge_row is None or merge_row.get("op") != "MERGE"
                or int(merge_row["id"]) >= int(row["id"])
                or not isinstance(merge_row.get("payload"), Mapping)
            ):
                mark_ambiguous("missing_unmerge_source_receipt", row, (src, dst))
                continue
            merge_payload = merge_row["payload"]
            source_id = merge_row.get("src_workstream_id")
            absorber_id = merge_row.get("dst_workstream_id")
            edge_id = merge_payload.get("merge_edge_id")
            if source_id is None or absorber_id is None or edge_id is None:
                mark_ambiguous(
                    "missing_unmerge_reversal_payload", row,
                    (source_id, absorber_id, src, dst),
                )
                continue
            source_id, absorber_id = int(source_id), int(absorber_id)
            managed_ids.update({source_id, absorber_id})
            fallback = merge_payload.get("source_prior_status")
            if fallback not in ACTIVE_STATUSES:
                fallback = "staging"
            expected_status[source_id] = {
                "mode": "active", "fallback": fallback, "op_key": row["op_key"],
            }
            expected_edges[int(edge_id)] = {
                "src": source_id, "dst": absorber_id, "status": "tombstoned",
                "op_key": row["op_key"],
            }

    # Validate all projected identities/edge identities before repairing any
    # state for that source lane.
    for workstream_id in sorted(expected_status):
        node = _row(conn, workstream_id)
        if node is None:
            mark_ambiguous(
                "missing_workstream", {"op_key": expected_status[workstream_id]["op_key"], "op": None},
                (workstream_id,),
            )
        elif node["kind"] != "workstream":
            mark_ambiguous(
                "target_not_workstream", {"op_key": expected_status[workstream_id]["op_key"], "op": None},
                (workstream_id,),
            )
    for edge_id, expected in sorted(expected_edges.items()):
        edge = conn.execute(
            "SELECT id, src, dst, relation, status FROM edges WHERE id = ?", (edge_id,),
        ).fetchone()
        if (
            edge is None or int(edge["src"]) != expected["src"]
            or int(edge["dst"]) != expected["dst"]
            or edge["relation"] != "merged_into"
        ):
            mark_ambiguous(
                "missing_or_mismatched_merge_edge",
                {"op_key": expected["op_key"], "op": "MERGE"},
                (expected["src"], expected["dst"]),
            )
    edge_sources = {item["src"] for item in expected_edges.values()}
    for source_id in sorted(edge_sources):
        active = conn.execute(
            "SELECT id, dst FROM edges WHERE src = ? AND relation = 'merged_into' "
            "AND status = 'active' ORDER BY id",
            (source_id,),
        ).fetchall()
        known_edges = [
            (edge_id, item["dst"])
            for edge_id, item in expected_edges.items()
            if item["src"] == source_id
        ]
        actual_active = [(int(edge["id"]), int(edge["dst"])) for edge in active]
        if any(pair not in known_edges for pair in actual_active):
            mark_ambiguous(
                "ambiguous_active_merge_edges",
                {"op_key": expected_status.get(source_id, {}).get("op_key"), "op": "MERGE"},
                (source_id, *[dst for _, dst in actual_active]),
            )

    status_repairs: list[tuple[int, str]] = []
    edge_repairs: list[tuple[int, str]] = []
    for workstream_id, expected in sorted(expected_status.items()):
        if workstream_id in blocked_ids:
            continue
        node = _row(conn, workstream_id)
        assert node is not None
        if expected["mode"] == "stale":
            desired = "stale"
            needs_repair = node["status"] != desired
        else:
            desired = expected["fallback"]
            needs_repair = node["status"] not in ACTIVE_STATUSES
        if needs_repair:
            status_repairs.append((workstream_id, desired))
    for edge_id, expected in sorted(expected_edges.items()):
        if expected["src"] in blocked_ids or expected["dst"] in blocked_ids:
            continue
        edge = conn.execute("SELECT status FROM edges WHERE id = ?", (edge_id,)).fetchone()
        if edge is not None and edge["status"] != expected["status"]:
            edge_repairs.append((edge_id, expected["status"]))

    repaired_status_ids: list[int] = []
    repaired_edge_ids: list[int] = []
    repair_key = (
        _snapshot_token({"status": status_repairs, "edges": edge_repairs})
        if status_repairs or edge_repairs
        else None
    )
    if apply_repairs and (status_repairs or edge_repairs):
        if backup_path is None:
            raise WorkstreamLifecycleError(
                "integrity repair requires a pre-transaction backup"
            )
        for workstream_id, desired in status_repairs:
            db.update_node_nc(conn, workstream_id, status=desired)
            repaired_status_ids.append(workstream_id)
        for edge_id, desired in edge_repairs:
            conn.execute("UPDATE edges SET status = ? WHERE id = ?", (desired, edge_id))
            repaired_edge_ids.append(edge_id)

    all_workstreams = {
        int(row["id"]) for row in conn.execute(
            "SELECT id FROM nodes WHERE kind = 'workstream' ORDER BY id"
        ).fetchall()
    }
    legacy_ids = sorted(all_workstreams - managed_ids)
    report = {
        "ok": not ambiguous,
        "applied_operations": len(rows),
        "managed_workstream_ids": sorted(managed_ids),
        "repaired_status_ids": repaired_status_ids,
        "repaired_edge_ids": repaired_edge_ids,
        "repair_count": len(repaired_status_ids) + len(repaired_edge_ids),
        "backup_path": backup_path,
        "ambiguous": ambiguous,
        "legacy_unmanaged_ids": legacy_ids,
        "legacy_unmanaged_count": len(legacy_ids),
        "synthetic_receipts_created": 0,
        "_planned_repair_count": len(status_repairs) + len(edge_repairs),
        "_repair_key": repair_key,
    }
    return report


def reconcile_lifecycle_integrity(
    conn: sqlite3.Connection,
    *,
    project_path: str | None = None,
    emit_log: bool = True,
    already_locked: bool = False,
) -> dict:
    """Atomically project applied lifecycle receipts onto identity state.

    The full predicate projection is recomputed after ``BEGIN IMMEDIATE`` while
    the shared project writer lock is held. ``already_locked`` remains an
    explicit fast path for maintenance callers, although same-thread ownership
    is also safely recognized by :func:`lockfile.writer_lock`.
    """
    _require_clean_connection(conn)
    lock_context = (
        nullcontext()
        if already_locked
        else lockfile.writer_lock(_project_path(project_path))
    )
    with lock_context:
        # SQLite's backup API can block when invoked from the same connection
        # after a write transaction has started.  Project the possible repair
        # while holding the shared writer lock, take the backup before BEGIN,
        # then recompute every predicate under BEGIN IMMEDIATE before writing.
        preflight = _reconcile_lifecycle_integrity_in_transaction(
            conn,
            project_path=project_path,
            apply_repairs=False,
        )
        backup_path = None
        if preflight["_planned_repair_count"]:
            backup_path = _backup_before_mutation(
                conn,
                op="INTEGRITY",
                op_key=str(preflight["_repair_key"]),
            )
        conn.execute("BEGIN IMMEDIATE")
        try:
            report = _reconcile_lifecycle_integrity_in_transaction(
                conn,
                project_path=project_path,
                backup_path=backup_path,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    report.pop("_planned_repair_count", None)
    report.pop("_repair_key", None)
    if emit_log:
        log_utils.emit_event(
            "workstream_integrity", report, project_path=project_path, session_id=None,
        )
    return report


# Compact aliases for internal callers and future MCP adapters.
open = open_workstream
close = close_workstream
reopen = reopen_workstream
adopt = adopt_nodes
merge = merge_workstreams
unmerge = unmerge_workstreams
merge_workstream = merge_workstreams
unmerge_workstream = unmerge_workstreams
