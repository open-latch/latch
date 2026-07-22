"""Fail-closed workstream trust-ladder planning and governed execution.

This module owns policy, not mutation mechanics. ``plan_actions`` is read-only;
``run_governed`` lazily dispatches only eligible plans to the atomic operations
in :mod:`workstreams`. Missing evidence always degrades to a suggestion.
"""
from __future__ import annotations

import importlib
import inspect
import json
import os
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from types import ModuleType
from typing import Any

import db
import lifecycle_receipts
import lifecycle_signals
import lockfile
import workstream_detector


AGREE_QUORUM = 2
CALIBRATION_WINDOWS = 2
CALIBRATION_PERSISTENCE = 0.70
QUIESCENCE_MINUTES = 30
TRAILING_REGRET_WINDOW = 20
TRAILING_REGRET_MAX_RATE = 0.20
TRAILING_REGRET_REVERSAL_SCAN_LIMIT = 100

_GOVERNANCE_ONLY_KEYS = frozenset({
    "priority_policy_complete",
    "proposal_validated",
    "proposal_source",
    "apply_method",
})


def _parse_ts(value: datetime | str | None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value is None:
        parsed = datetime.now(timezone.utc)
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_workstreams() -> ModuleType:
    return importlib.import_module("workstreams")


def _operation_callable(module: ModuleType, op: str):
    names = {
        "OPEN": ("open_workstream", "open"),
        "MERGE": ("merge_workstreams", "merge"),
        "CLOSE": ("close_workstream", "close"),
        "ADOPT": ("adopt_nodes", "adopt"),
        "UNMERGE": ("unmerge_workstreams", "unmerge_workstream", "unmerge"),
    }.get(op, ())
    for name in names:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return candidate
    return None


def _attestation_state(
    conn: sqlite3.Connection,
    *,
    derivation_id: int,
    candidate_key: str,
) -> dict:
    # Candidate identity is stable across daily derivations.  Events are
    # accepted only when their derivation really contained that candidate, but
    # they continue to count while the same key persists in the latest run.
    rows = conn.execute(
        "SELECT e.verdict, e.session_id, e.derivation_id "
        "FROM workstream_op_events e "
        "JOIN workstream_derivation_candidates c "
        "ON c.derivation_id=e.derivation_id AND c.candidate_key=e.candidate_key "
        "WHERE e.candidate_key=? AND e.event_type='attestation' "
        "AND e.verdict IS NOT NULL AND e.derivation_id<=? ORDER BY e.id",
        (candidate_key, derivation_id),
    ).fetchall()
    agrees = {
        str(row["session_id"])
        for row in rows
        if row["verdict"] == "agree" and row["session_id"] is not None
    }
    disagrees = sum(1 for row in rows if row["verdict"] == "disagree")
    unsure = sum(1 for row in rows if row["verdict"] == "unsure")
    return {
        "agree_sessions": sorted(agrees),
        "agree_session_count": len(agrees),
        "disagree_count": disagrees,
        "unsure_count": unsure,
        "derivation_ids": sorted({int(row["derivation_id"]) for row in rows}),
        "quorum": len(agrees) >= AGREE_QUORUM and disagrees == 0,
    }


def _calibration_state(conn: sqlite3.Connection, candidate_key: str) -> dict:
    state = workstream_detector.load_prequential_state(
        conn, candidate_key, lookback=CALIBRATION_WINDOWS,
    )
    state["graduated"] = workstream_detector.graduation_eligible(
        state,
        min_consecutive=CALIBRATION_WINDOWS,
        min_persistence=CALIBRATION_PERSISTENCE,
    )
    return state


def _json_object(value: Any) -> dict | None:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return dict(decoded) if isinstance(decoded, Mapping) else None


def _strict_int_list(value: Any) -> list[int] | None:
    if not isinstance(value, (list, tuple)):
        return None
    result: list[int] = []
    for raw in value:
        if isinstance(raw, bool):
            return None
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            return None
        if parsed <= 0:
            return None
        result.append(parsed)
    return sorted(set(result)) if len(set(result)) == len(result) else None


def _verified_shared_targets(
    conn: sqlite3.Connection,
    *,
    member_ids: list[int],
    target_ids: list[int],
) -> bool:
    if len(member_ids) < 2 or not target_ids:
        return False
    member_marks = ",".join("?" for _ in member_ids)
    target_marks = ",".join("?" for _ in target_ids)
    rows = conn.execute(
        f"SELECT e.dst,COUNT(DISTINCT e.src) AS source_count FROM edges e "
        f"JOIN nodes member ON member.id=e.src "
        f"JOIN nodes target ON target.id=e.dst "
        f"WHERE e.src IN ({member_marks}) AND e.dst IN ({target_marks}) "
        f"AND e.relation IN ('advances','motivates','depends_on') "
        f"AND e.status='active' AND member.status!='stale' "
        f"AND target.status!='stale' "
        f"AND target.kind IN ('decision','workstream') GROUP BY e.dst "
        f"HAVING COUNT(DISTINCT e.src)>=2",
        [*member_ids, *target_ids],
    ).fetchall()
    return {int(row["dst"]) for row in rows} == set(target_ids)


def _verified_recurrence_sessions(
    conn: sqlite3.Connection,
    *,
    member_ids: list[int],
    session_ids: list[str],
) -> bool:
    """Recompute OPEN recurrence from contamination-free orphan contacts."""
    members = sorted({int(value) for value in member_ids})
    sessions = sorted({str(value) for value in session_ids if str(value).strip()})
    if not members or len(sessions) < 2:
        return False
    member_marks = ",".join("?" for _ in members)
    session_marks = ",".join("?" for _ in sessions)
    rows = conn.execute(
        f"SELECT DISTINCT r.session_id FROM retrieval_events r "
        f"JOIN nodes member ON member.id=r.node_id "
        f"WHERE r.node_id IN ({member_marks}) "
        f"AND r.session_id IN ({session_marks}) "
        "AND r.workstream_id_at_event IS NULL "
        "AND member.status!='stale' "
        "AND (r.source='write' OR "
        "(r.turn>0 AND r.source IN ('prompt','graph','tool','gate')))",
        [*members, *sessions],
    ).fetchall()
    return {str(row["session_id"]) for row in rows} == set(sessions)


def _workstream_open_origin(
    conn: sqlite3.Connection,
    workstream_id: int,
) -> str | None:
    """Return an unambiguous applied OPEN origin, otherwise fail closed."""
    rows = conn.execute(
        "SELECT origin FROM workstream_ops WHERE op='OPEN' AND state='applied' "
        "AND dst_workstream_id=? ORDER BY id",
        (int(workstream_id),),
    ).fetchall()
    if len(rows) != 1:
        return None
    origin = str(rows[0]["origin"] or "").strip().lower()
    return origin or None


def _accepted_open_proposal(
    conn: sqlite3.Connection,
    *,
    candidate_key: str,
    signal: Mapping[str, Any],
) -> dict:
    """Load the newest valid accepted proposal for a currently-live key.

    The join proves that each event was attached to a derivation containing the
    candidate.  The caller itself is iterating the latest derivation, so an
    event for a candidate that disappeared cannot become actionable.
    """
    events = conn.execute(
        "SELECT e.* FROM workstream_op_events e "
        "JOIN workstream_derivation_candidates c "
        "ON c.derivation_id=e.derivation_id AND c.candidate_key=e.candidate_key "
        "WHERE e.candidate_key=? AND e.event_type IN "
        "('proposal_accepted','proposal_rejected') ORDER BY e.id DESC",
        (candidate_key,),
    ).fetchall()
    # Proposal judgments are ordered updates for one stable candidate.  A
    # newer rejection supersedes an older accepted charter; reviving it would
    # otherwise bypass the compactor's latest validation result.
    accepted = (
        events[0]
        if events and events[0]["event_type"] == "proposal_accepted"
        else None
    )
    state: dict[str, Any] = {
        "event_present": bool(events),
        "accepted": False,
        "request": {},
        "evidence": {
            "proposal_event_present": bool(events),
            "proposal_event_status": (
                str(events[0]["event_type"]) if events else None
            ),
        },
    }
    if accepted is None:
        return state
    payload = _json_object(accepted["payload_json"])
    if payload is None or "force" in payload:
        state["evidence"]["proposal_event_status"] = "malformed"
        return state
    required = ("title", "objective", "done_when", "scope_boundary", "next_step")
    if any(not isinstance(payload.get(key), str) or not payload[key].strip() for key in required):
        state["evidence"]["proposal_event_status"] = "malformed"
        return state
    if (
        payload.get("candidate_key") != candidate_key
        or payload.get("proposal_validated") is not True
        or str(payload.get("proposal_source") or "").lower() != "compactor"
    ):
        state["evidence"]["proposal_event_status"] = "malformed"
        return state
    members = _strict_int_list(payload.get("member_ids"))
    recurrence = payload.get("recurrence")
    if members is None or not isinstance(recurrence, Mapping):
        state["evidence"]["proposal_event_status"] = "malformed"
        return state
    session_values = recurrence.get("session_ids")
    if not isinstance(session_values, (list, tuple)):
        state["evidence"]["proposal_event_status"] = "malformed"
        return state
    session_ids = sorted({str(value) for value in session_values if str(value).strip()})
    try:
        session_count = int(recurrence.get("session_count") or len(session_ids))
    except (TypeError, ValueError):
        state["evidence"]["proposal_event_status"] = "malformed"
        return state
    shared_target_ids = _strict_int_list(recurrence.get("shared_target_ids", []))
    if shared_target_ids is None:
        state["evidence"]["proposal_event_status"] = "malformed"
        return state
    candidate_targets = _strict_int_list(signal.get("shared_target_ids"))
    shared_target_validated = bool(
        shared_target_ids
        and candidate_targets is not None
        and set(shared_target_ids).issubset(candidate_targets)
        and _verified_shared_targets(
            conn, member_ids=members, target_ids=shared_target_ids,
        )
    )
    candidate_members = _strict_int_list(signal.get("member_ids"))
    if candidate_members is not None and not set(members).issubset(candidate_members):
        state["evidence"]["proposal_event_status"] = "member_mismatch"
        return state
    session_recurrence_validated = bool(
        session_count >= 2
        and len(session_ids) >= 2
        and _verified_recurrence_sessions(
            conn, member_ids=members, session_ids=session_ids,
        )
    )
    session_recurrence = session_recurrence_validated
    if not session_recurrence and not shared_target_validated:
        state["evidence"]["proposal_event_status"] = "recurrence_incomplete"
        return state
    state.update({
        "accepted": True,
        "request": {
            **{key: payload[key].strip() for key in required},
            "member_ids": members,
            "recurrence": {
                **dict(recurrence),
                "session_ids": session_ids,
                "session_count": session_count,
                "session_recurrence_validated": session_recurrence_validated,
                "shared_target_ids": shared_target_ids,
                "shared_target_validated": shared_target_validated,
            },
            "proposal_validated": True,
            "proposal_source": "compactor",
        },
        "evidence": {
            "proposal_event_present": True,
            "proposal_event_status": "proposal_accepted",
            "proposal_event_id": int(accepted["id"]),
            "proposal_event_derivation_id": int(accepted["derivation_id"]),
            "proposal_event_session_id": accepted["session_id"],
            "proposal_key": payload.get("proposal_key"),
            "session_recurrence_validated": session_recurrence_validated,
            "shared_target_recurrence_validated": shared_target_validated,
        },
    })
    return state


def _probation_active(probation: Mapping[str, Any], *, now: datetime) -> bool:
    active = probation.get("active") is True
    opened_at = probation.get("opened_at")
    if not active or opened_at is None:
        return False
    try:
        opened = _parse_ts(str(opened_at))
    except (TypeError, ValueError):
        return False
    if opened > now or now - opened > timedelta(
        days=workstream_detector.ELIGIBLE_WINDOW_CAP_DAYS,
    ):
        return False
    until = probation.get("until")
    if until is None:
        return True
    try:
        not_expired = _parse_ts(str(until)) >= now
    except (TypeError, ValueError):
        return False
    return bool(not_expired)


def _probation_merge_back(
    conn: sqlite3.Connection,
    signal: Mapping[str, Any],
    *,
    now: datetime,
) -> dict:
    try:
        pair = {int(signal["left"]), int(signal["right"])}
        co_contacts = int(signal.get("co_contact_sessions") or 0)
        jaccard = float(signal.get("jaccard") or 0.0)
    except (KeyError, TypeError, ValueError):
        return {"eligible": False, "reason": "signal_incomplete"}
    high_confidence = bool(
        signal.get("qualified")
        and co_contacts >= workstream_detector.MERGE_MIN_CO_CONTACT
        and jaccard >= workstream_detector.MERGE_MIN_JACCARD
        and signal.get("tier2_inputs")
    )
    if len(pair) != 2 or not high_confidence:
        return {"eligible": False, "reason": "confidence_incomplete"}
    rows = conn.execute(
        "SELECT id,op_key,dst_workstream_id,payload_json FROM workstream_ops "
        "WHERE op='OPEN' AND state='applied' AND origin='auto' "
        "AND dst_workstream_id IN (?,?) ORDER BY id DESC LIMIT 3",
        tuple(sorted(pair)),
    ).fetchall()
    matches: list[dict] = []
    for row in rows:
        payload = _json_object(row["payload_json"])
        if payload is None:
            continue
        watch_pair = _strict_int_list(payload.get("watch_pair"))
        probation = payload.get("probation")
        new_lane = int(row["dst_workstream_id"])
        if (
            watch_pair is None
            or set(watch_pair) != pair
            or new_lane not in pair
            or not isinstance(probation, Mapping)
            or not _probation_active(probation, now=now)
        ):
            continue
        try:
            dynamic = _load_workstreams()._auto_open_probation_state(
                conn, new_lane, now=now,
            )
        except Exception:
            continue
        if not isinstance(dynamic, Mapping) or dynamic.get("active") is not True:
            continue
        watched = next(iter(pair - {new_lane}))
        # OPEN stores [new lane, watched lane].  A sorted-equivalent pair is
        # insufficient because it would make the reversal direction ambiguous.
        raw_pair = payload.get("watch_pair")
        if not isinstance(raw_pair, (list, tuple)) or [int(v) for v in raw_pair] != [new_lane, watched]:
            continue
        absorber_origin = _workstream_open_origin(conn, watched)
        matches.append({
            "open_operation_id": int(row["id"]),
            "open_op_key": row["op_key"],
            "source_workstream_id": new_lane,
            "absorber_workstream_id": watched,
            "absorber_origin": absorber_origin,
            "attestation_carveout_eligible": absorber_origin == "auto",
            "eligible_session_count": dynamic.get("eligible_session_count"),
            "eligible_session_target": dynamic.get("eligible_session_target"),
            "contact_session_count": dynamic.get("contact_session_count"),
        })
    if len(matches) != 1:
        return {
            "eligible": False,
            "reason": "not_found" if not matches else "ambiguous",
        }
    return {"eligible": True, "reason": "watch_pair", **matches[0]}


def _trailing_regret_state(conn: sqlite3.Connection) -> dict:
    """Pessimistic bounded regret rate over recent automatic OPEN/MERGE ops."""
    sampled = conn.execute(
        "SELECT * FROM workstream_ops WHERE state='applied' AND origin='auto' "
        "AND op IN ('OPEN','MERGE') ORDER BY id DESC LIMIT ?",
        (TRAILING_REGRET_WINDOW,),
    ).fetchall()
    if not sampled:
        return {
            "window": TRAILING_REGRET_WINDOW,
            "threshold": TRAILING_REGRET_MAX_RATE,
            "numerator": 0,
            "denominator": 0,
            "reversal_count": 0,
            "ambiguous_count": 0,
            "rate": 0.0,
            "complete": True,
            "exceeds_threshold": False,
        }
    minimum_id = min(int(row["id"]) for row in sampled)
    reversals = conn.execute(
        "SELECT * FROM workstream_ops WHERE state='applied' AND id>? "
        "AND op IN ('UNMERGE','CLOSE','MERGE') ORDER BY id DESC LIMIT ?",
        (minimum_id, TRAILING_REGRET_REVERSAL_SCAN_LIMIT + 1),
    ).fetchall()
    truncated = len(reversals) > TRAILING_REGRET_REVERSAL_SCAN_LIMIT
    reversals = reversals[:TRAILING_REGRET_REVERSAL_SCAN_LIMIT]
    parsed_reversals = [(row, _json_object(row["payload_json"])) for row in reversals]
    reversal_count = 0
    ambiguous_count = int(truncated)
    denominator = 0
    for operation in sampled:
        operation_id = int(operation["id"])
        if operation["op"] == "MERGE":
            denominator += 1
            linked: list[sqlite3.Row] = []
            malformed = False
            for row, payload in parsed_reversals:
                if int(row["id"]) <= operation_id or row["op"] != "UNMERGE":
                    continue
                if payload is None:
                    if str(row["candidate_key"] or "") == f"unmerge:{operation['op_key']}":
                        malformed = True
                    continue
                if (
                    str(row["candidate_key"] or "") == f"unmerge:{operation['op_key']}"
                    and payload.get("merge_op_key") != operation["op_key"]
                ):
                    malformed = True
                if payload.get("merge_op_key") == operation["op_key"]:
                    linked.append(row)
            if malformed or len(linked) > 1:
                ambiguous_count += 1
            elif len(linked) == 1:
                reversal_count += 1
            continue

        payload = _json_object(operation["payload_json"])
        probation = payload.get("probation") if payload is not None else None
        if not isinstance(probation, Mapping) or probation.get("active") is not True:
            continue
        denominator += 1
        lane_id = operation["dst_workstream_id"]
        watch_pair = _strict_int_list(payload.get("watch_pair")) if payload else None
        if lane_id is None or (payload is not None and payload.get("watch_pair") is not None and watch_pair is None):
            ambiguous_count += 1
            continue
        lane_id = int(lane_id)
        watched = None
        if watch_pair is not None:
            if len(watch_pair) != 2 or lane_id not in watch_pair:
                ambiguous_count += 1
                continue
            watched = next(iter(set(watch_pair) - {lane_id}), None)
        linked: list[sqlite3.Row] = []
        for row, _reversal_payload in parsed_reversals:
            if int(row["id"]) <= operation_id:
                continue
            if row["op"] == "CLOSE" and row["src_workstream_id"] == lane_id:
                linked.append(row)
            elif (
                watched is not None
                and row["op"] == "MERGE"
                and row["src_workstream_id"] == lane_id
                and row["dst_workstream_id"] == watched
            ):
                linked.append(row)
        if len(linked) > 1:
            ambiguous_count += 1
        elif len(linked) == 1:
            reversal_count += 1
    numerator = reversal_count + ambiguous_count
    rate = numerator / denominator if denominator else 0.0
    return {
        "window": TRAILING_REGRET_WINDOW,
        "threshold": TRAILING_REGRET_MAX_RATE,
        "numerator": numerator,
        "denominator": denominator,
        "reversal_count": reversal_count,
        "ambiguous_count": ambiguous_count,
        "rate": round(rate, 6),
        "complete": ambiguous_count == 0,
        "exceeds_threshold": bool(denominator and rate > TRAILING_REGRET_MAX_RATE),
    }


def _target_ids(op: str, signal: Mapping[str, Any], request: Mapping[str, Any]) -> set[int]:
    values: list[Any] = []
    if op == "MERGE":
        values.extend([
            request.get(
                "source_workstream_id",
                request.get("src_workstream_id", request.get("src")),
            ),
            request.get(
                "absorber_workstream_id",
                request.get("dst_workstream_id", request.get("dst")),
            ),
            signal.get("left"), signal.get("right"),
        ])
    elif op == "CLOSE":
        values.extend([
            request.get("workstream_id", request.get("requested_workstream_id")),
            signal.get("workstream_id"),
        ])
    elif op == "ADOPT":
        values.extend([
            request.get("workstream_id", request.get("requested_workstream_id")),
            signal.get("workstream_id"),
        ])
    elif op == "OPEN" and request.get("branched_from") is not None:
        # OPEN does not mutate the parent, so a pinned parent does not veto it.
        values = []
    result: set[int] = set()
    for value in values:
        if value is None:
            continue
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _pinned_targets(conn: sqlite3.Connection, target_ids: set[int]) -> list[int]:
    if not target_ids:
        return []
    placeholders = ",".join("?" for _ in target_ids)
    rows = conn.execute(
        f"SELECT workstream_id FROM focus WHERE pinned=1 "
        f"AND workstream_id IN ({placeholders}) ORDER BY workstream_id",
        sorted(target_ids),
    ).fetchall()
    return [int(row["workstream_id"]) for row in rows]


def _recent_activity(
    conn: sqlite3.Connection,
    *,
    target_ids: set[int],
    member_ids: set[int],
    now: datetime,
    quiescence_minutes: int,
) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if target_ids:
        placeholders = ",".join("?" for _ in target_ids)
        clauses.append(f"workstream_id_at_event IN ({placeholders})")
        params.extend(sorted(target_ids))
    if member_ids:
        placeholders = ",".join("?" for _ in member_ids)
        clauses.append(f"node_id IN ({placeholders})")
        params.extend(sorted(member_ids))
    if not clauses:
        return []
    cutoff = (now - timedelta(minutes=max(0, quiescence_minutes))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = conn.execute(
        "SELECT id, session_id, ts, node_id, workstream_id_at_event "
        "FROM retrieval_events WHERE ts >= ? AND (" + " OR ".join(clauses) + ") "
        "ORDER BY ts, id",
        [cutoff, *params],
    ).fetchall()
    return [dict(row) for row in rows]


def _required_request_fields(op: str, request: Mapping[str, Any]) -> list[str]:
    if op == "OPEN":
        required = ("title", "objective", "done_when", "scope_boundary", "next_step")
    elif op == "MERGE":
        if "source_workstream_id" in request or "absorber_workstream_id" in request:
            required = ("source_workstream_id", "absorber_workstream_id", "dispositions")
        elif "src_workstream_id" in request or "dst_workstream_id" in request:
            required = ("src_workstream_id", "dst_workstream_id", "reason")
        else:
            required = ("src", "dst", "reason")
    elif op == "CLOSE":
        required = ("workstream_id", "outcome", "reason", "dispositions")
    elif op == "ADOPT":
        if "workstream_id" in request:
            required = ("workstream_id", "node_ids", "relations", "evidence")
        else:
            required = ("requested_workstream_id", "node_ids", "relations", "evidence")
    else:
        return ["unsupported_op"]
    missing = [
        name for name in required
        if name not in request
        or request[name] is None
        or (isinstance(request[name], str) and not request[name].strip())
    ]
    if "dispositions" in required and not isinstance(request.get("dispositions"), Mapping):
        missing.append("dispositions")
    if op == "ADOPT":
        if not _strict_int_list(request.get("node_ids")):
            missing.append("node_ids")
        if not isinstance(request.get("relations"), Mapping):
            missing.append("relations")
        if not isinstance(request.get("evidence"), Mapping):
            missing.append("evidence")
    return sorted(set(missing))


def _merge_preflight_request(
    conn: sqlite3.Connection,
    signal: Mapping[str, Any],
    request: Mapping[str, Any],
    module: ModuleType,
    *,
    now: datetime,
) -> tuple[dict, dict, list[str]]:
    prepared = dict(request)
    reasons: list[str] = []
    evidence: dict[str, Any] = {}
    probation = _probation_merge_back(conn, signal, now=now)
    evidence["probation_merge_back"] = probation

    source = prepared.get(
        "source_workstream_id",
        prepared.get("src_workstream_id", prepared.get("src")),
    )
    absorber = prepared.get(
        "absorber_workstream_id",
        prepared.get("dst_workstream_id", prepared.get("dst")),
    )
    basis = "apply_request" if source is not None and absorber is not None else None
    if probation.get("eligible"):
        source = probation["source_workstream_id"]
        absorber = probation["absorber_workstream_id"]
        basis = "probation_watch_pair"
    elif source is None or absorber is None:
        direction = signal.get("direction")
        if isinstance(direction, Mapping) and direction.get("unambiguous") is True:
            source = direction.get("source_workstream_id")
            absorber = direction.get("absorber_workstream_id")
            basis = str(direction.get("basis") or "detector_direction")
    try:
        source_id, absorber_id = int(source), int(absorber)
    except (TypeError, ValueError):
        reasons.append("merge_direction_missing")
        return {}, evidence, reasons
    if source_id == absorber_id:
        reasons.append("merge_direction_invalid")
        return {}, evidence, reasons
    try:
        candidate_pair = {int(signal["left"]), int(signal["right"])}
    except (KeyError, TypeError, ValueError):
        candidate_pair = set()
    if candidate_pair and candidate_pair != {source_id, absorber_id}:
        reasons.append("merge_direction_candidate_mismatch")
        return {}, evidence, reasons
    evidence["direction"] = {
        "source_workstream_id": source_id,
        "absorber_workstream_id": absorber_id,
        "basis": basis,
    }

    preflight_fn = getattr(module, "merge_preflight", None)
    if not callable(preflight_fn):
        reasons.append("merge_preflight_unavailable")
        return {}, evidence, reasons
    try:
        preflight = preflight_fn(conn, source_id, absorber_id)
    except Exception:
        reasons.append("merge_preflight_failed")
        return {}, evidence, reasons
    if not isinstance(preflight, Mapping):
        reasons.append("merge_preflight_invalid")
        return {}, evidence, reasons
    source_resolution = preflight.get("source_resolution")
    absorber_resolution = preflight.get("absorber_resolution")
    if (
        not isinstance(source_resolution, Mapping)
        or source_resolution.get("state") != "active"
        or int(source_resolution.get("active_id") or -1) != source_id
        or not isinstance(absorber_resolution, Mapping)
        or absorber_resolution.get("state") != "active"
        or int(absorber_resolution.get("active_id") or -1) != absorber_id
    ):
        reasons.append("merge_preflight_inactive_identity")
    if preflight.get("acyclic") is not True:
        reasons.append("merge_preflight_cycle")
    token = preflight.get("token")
    if not isinstance(token, str) or not token.strip():
        reasons.append("merge_preflight_token_missing")

    unknown = preflight.get("unknown_inbound_edges")
    if not isinstance(unknown, (list, tuple)):
        reasons.append("merge_preflight_unknown_edges_missing")
        unknown = []
    unknown_ids: set[int] = set()
    try:
        unknown_ids = {int(item["id"]) for item in unknown if isinstance(item, Mapping)}
    except (TypeError, ValueError):
        reasons.append("merge_preflight_unknown_edges_invalid")
    dispositions = prepared.get("dispositions", {})
    if not isinstance(dispositions, Mapping):
        reasons.append("merge_dispositions_incomplete")
        dispositions = {}
    normalized_dispositions: dict[str, str] = {}
    try:
        for raw_id, raw in dispositions.items():
            action = raw.get("action") if isinstance(raw, Mapping) else raw
            action = str(action or "").strip().lower()
            if action == "keep":
                action = "preserve"
            if action not in {"rehome", "preserve", "tombstone"}:
                raise ValueError
            normalized_dispositions[str(int(raw_id))] = action
    except (TypeError, ValueError):
        reasons.append("merge_dispositions_incomplete")
        normalized_dispositions = {}
    if set(map(int, normalized_dispositions)) != unknown_ids:
        reasons.append("merge_unknown_edge_dispositions_required")

    snapshot = preflight.get("snapshot")
    priority_complete = True
    priority_projection: dict[str, Any] = {}
    if not isinstance(snapshot, Mapping):
        priority_complete = False
    else:
        source_priorities = snapshot.get("source_priorities")
        absorber_priority_ids = snapshot.get("absorber_priority_ids")
        if not isinstance(source_priorities, (list, tuple)) or not isinstance(
            absorber_priority_ids, (list, tuple),
        ):
            priority_complete = False
        else:
            cap = int(getattr(getattr(module, "priorities", None), "MAX_ACTIVE", 3))
            available = max(0, cap - len(absorber_priority_ids))
            priority_projection = {
                "cap": cap,
                "source_priority_count": len(source_priorities),
                "absorber_priority_count": len(absorber_priority_ids),
                "copied_priority_count": min(len(source_priorities), available),
                "overflow_retired_count": max(0, len(source_priorities) - available),
                "policy": "absorber_kept_source_tail_copied_overflow_retired",
            }
    if "priority_policy_complete" in prepared and prepared.get("priority_policy_complete") is not True:
        priority_complete = False
    if not priority_complete:
        reasons.append("merge_priority_policy_incomplete")
    evidence["preflight"] = {
        "token": token,
        "unknown_inbound_edge_ids": sorted(unknown_ids),
        "priority_projection": priority_projection,
    }

    detector_evidence = prepared.get("evidence")
    if not isinstance(detector_evidence, Mapping):
        detector_evidence = {}
    prepared = {
        "source_workstream_id": source_id,
        "absorber_workstream_id": absorber_id,
        "dispositions": normalized_dispositions,
        "evidence": {
            **dict(detector_evidence),
            "coactive_sessions": int(signal.get("co_contact_sessions") or 0),
            "window_sessions": int(
                signal.get("union_sessions")
                or detector_evidence.get("eligible_session_count")
                or 0
            ),
            "direction_basis": basis,
        },
        "priority_policy_complete": priority_complete,
        "preflight_token": token,
    }
    return prepared, evidence, sorted(set(reasons))


def _completion_evidence_state(
    conn: sqlite3.Connection,
    signal: Mapping[str, Any],
    *,
    workstream_id: int,
) -> tuple[bool, dict]:
    completion = signal.get("completion_evidence")
    state: dict[str, Any] = {"structural_completion": False}
    if not isinstance(completion, Mapping):
        return False, state
    done_when = str(completion.get("done_when") or "").strip()
    forward = _strict_int_list(completion.get("forward_member_ids"))
    resolved = _strict_int_list(completion.get("resolved_forward_member_ids"))
    edge_ids = _strict_int_list(completion.get("resolution_edge_ids"))
    try:
        density = float(completion.get("resolution_density") or 0.0)
    except (TypeError, ValueError):
        density = 0.0
    state.update({
        "done_when_present": bool(done_when),
        "forward_member_count": len(forward or []),
        "resolved_forward_member_count": len(resolved or []),
        "resolution_edge_count": len(edge_ids or []),
        "resolution_density": density,
    })
    if (
        not done_when
        or not forward
        or resolved is None
        or set(forward) - set(resolved)
        or not edge_ids
        or density < 1.0
    ):
        return False, state
    lane = conn.execute(
        "SELECT body,status FROM nodes WHERE id=? AND kind='workstream'",
        (workstream_id,),
    ).fetchone()
    if lane is None or lane["status"] == "stale":
        return False, state
    charter_match = re.search(r"(?im)^Done when:\s*(.+?)\s*$", str(lane["body"] or ""))
    if charter_match is None or charter_match.group(1).strip() != done_when:
        state["charter_match"] = False
        return False, state
    state["charter_match"] = True
    placeholders = ",".join("?" for _ in edge_ids)
    rows = conn.execute(
        f"SELECT id,src,dst,relation,status,created_by FROM edges "
        f"WHERE id IN ({placeholders}) ORDER BY id",
        edge_ids,
    ).fetchall()
    if len(rows) != len(edge_ids):
        return False, state
    covered: set[int] = set()
    for row in rows:
        if (
            row["status"] != "active"
            or row["relation"] not in {"resolves", "supersedes", "replaces"}
            or row["created_by"] is None
        ):
            return False, state
        if int(row["dst"]) in set(forward):
            covered.add(int(row["dst"]))
    if covered != set(forward):
        return False, state
    state["structural_completion"] = True
    return True, state


def _close_preflight_request(
    conn: sqlite3.Connection,
    signal: Mapping[str, Any],
    request: Mapping[str, Any],
    module: ModuleType,
) -> tuple[dict, dict, list[str]]:
    prepared = dict(request)
    reasons: list[str] = []
    evidence: dict[str, Any] = {}
    outcome = str(prepared.get("outcome") or signal.get("outcome") or "").lower()
    try:
        workstream_id = int(
            prepared.get("workstream_id", signal.get("workstream_id"))
        )
    except (TypeError, ValueError):
        return prepared, evidence, ["close_target_missing"]
    if outcome == "completed":
        completion_ok, completion_state = _completion_evidence_state(
            conn, signal, workstream_id=workstream_id,
        )
        evidence["completion"] = completion_state
        if not completion_ok:
            reasons.append("completion_signal_missing")
        prepared.setdefault("workstream_id", workstream_id)
        prepared.setdefault("outcome", "completed")
        prepared.setdefault(
            "reason", "Done-when satisfied with complete resolution coverage",
        )
        prepared.setdefault("dispositions", {})
    elif outcome == "abandoned":
        # A silent human lane is only a candidate.  Automatic abandonment is
        # separately proven by the probation/release-only rail below.
        if not prepared:
            reasons.append("completion_signal_missing")
            return {}, evidence, reasons
    else:
        reasons.append("completion_signal_missing")
        return prepared, evidence, reasons

    preflight_fn = getattr(module, "close_preflight", None)
    if not callable(preflight_fn):
        reasons.append("close_preflight_unavailable")
        return prepared, evidence, reasons
    try:
        signature = inspect.signature(preflight_fn)
        supports_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        preflight_kwargs: dict[str, Any] = {}
        if "origin" in signature.parameters or supports_kwargs:
            preflight_kwargs["origin"] = "auto"
        if "outcome" in signature.parameters or supports_kwargs:
            preflight_kwargs["outcome"] = outcome
        preflight = preflight_fn(conn, workstream_id, **preflight_kwargs)
    except Exception:
        reasons.append("close_preflight_failed")
        return prepared, evidence, reasons
    if not isinstance(preflight, Mapping):
        reasons.append("close_preflight_invalid")
        return prepared, evidence, reasons
    resolution = preflight.get("resolution")
    if (
        not isinstance(resolution, Mapping)
        or resolution.get("state") != "active"
        or int(resolution.get("active_id") or -1) != workstream_id
    ):
        reasons.append("close_preflight_inactive_identity")
    feeders = preflight.get("feeders")
    if not isinstance(feeders, (list, tuple)):
        reasons.append("close_preflight_feeders_missing")
        feeders = []
    feeder_ids = {
        int(item["id"]) for item in feeders if isinstance(item, Mapping) and item.get("id") is not None
    }
    dispositions = prepared.get("dispositions")
    if not isinstance(dispositions, Mapping):
        reasons.append("close_dispositions_incomplete")
        dispositions = {}
    try:
        disposition_ids = {int(value) for value in dispositions}
    except (TypeError, ValueError):
        disposition_ids = set()
        reasons.append("close_dispositions_incomplete")
    if disposition_ids != feeder_ids:
        reasons.append("close_dispositions_do_not_match_preflight")
    if outcome == "completed" and feeder_ids:
        reasons.append("close_preflight_open_feeders")
    active_priorities = int(conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE kind='priority' AND status!='stale' "
        "AND workstream_id=?", (workstream_id,),
    ).fetchone()[0])
    if active_priorities:
        reasons.append("close_active_priorities")
    token = preflight.get("token")
    if not isinstance(token, str) or not token.strip():
        reasons.append("close_preflight_token_missing")
    prepared["workstream_id"] = workstream_id
    prepared["preflight_token"] = token
    evidence["preflight"] = {
        "token": token,
        "feeder_ids": sorted(feeder_ids),
        "active_priority_count": active_priorities,
    }
    return prepared, evidence, sorted(set(reasons))


def _adopt_evidence(
    signal: Mapping[str, Any],
    request: Mapping[str, Any],
    attestation: Mapping[str, Any],
) -> tuple[bool, dict]:
    evidence = request.get("evidence")
    if not isinstance(evidence, Mapping):
        evidence = {}
    trigger = str(evidence.get("trigger") or "").strip().lower()
    forbidden = trigger in {"cluster", "cluster_similarity", "cosine", "similarity"}
    session_values = signal.get("tier1_session_ids", evidence.get("session_ids", []))
    sessions = (
        {str(value) for value in session_values if str(value).strip()}
        if isinstance(session_values, (list, tuple)) else set()
    )
    tier1 = bool(
        signal.get("tier1_present")
        or (signal.get("tier1") and len(sessions) >= 2)
    )
    tier2_inputs = signal.get("tier2_inputs")
    tier2 = bool(
        isinstance(tier2_inputs, (list, tuple))
        and tier2_inputs
        and (signal.get("shared_target_ids") or evidence.get("shared_target_ids"))
    )
    forward = evidence.get("forward_looking") is True
    members = _strict_int_list(request.get("node_ids"))
    signal_members = _strict_int_list(signal.get("member_ids"))
    relations = request.get("relations")
    relation_complete = False
    if members is not None and isinstance(relations, Mapping):
        try:
            normalized_relations = {
                int(raw_id): str(relation).strip().lower()
                for raw_id, relation in relations.items()
            }
            relation_complete = bool(
                set(normalized_relations) == set(members)
                and set(normalized_relations.values()).issubset({
                    "advances", "motivates", "depends_on",
                })
            )
        except (TypeError, ValueError):
            relation_complete = False
    member_match = bool(
        members
        and signal_members is not None
        and set(members) == set(signal_members)
    )
    corroborated = bool(tier1 or tier2 or attestation.get("quorum"))
    complete = bool(
        request.get("allow_auto_apply") is True
        and forward
        and not forbidden
        and relation_complete
        and member_match
        and signal.get("tree_stale") is not True
        and corroborated
    )
    return complete, {
        "trigger": trigger or None,
        "forward_looking": forward,
        "cluster_or_cosine_only": forbidden,
        "tier1_present": tier1,
        "tier1_distinct_sessions": len(sessions),
        "tier2_present": tier2,
        "member_match": member_match,
        "relation_policy_complete": relation_complete,
        "tree_stale": signal.get("tree_stale") is True,
        "attestation_quorum": bool(attestation.get("quorum")),
    }


def _open_evidence(signal: Mapping[str, Any], request: Mapping[str, Any]) -> tuple[bool, dict]:
    proposal = signal.get("proposal")
    if not isinstance(proposal, Mapping):
        proposal = {}
    validated = bool(
        signal.get("proposal_validated")
        or proposal.get("validated")
        or request.get("proposal_validated")
    )
    source = str(
        signal.get("proposal_source")
        or proposal.get("source")
        or request.get("proposal_source")
        or ""
    ).lower()
    recurrence = request.get("recurrence")
    if not isinstance(recurrence, Mapping):
        recurrence = signal.get("recurrence")
    if not isinstance(recurrence, Mapping):
        recurrence = {}
    raw_sessions = recurrence.get("session_ids", [])
    session_ids = {
        str(value) for value in raw_sessions if str(value).strip()
    } if isinstance(raw_sessions, (list, tuple)) else set()
    try:
        session_count = int(
            recurrence.get("session_count")
            or recurrence.get("sessions")
            or len(session_ids)
        )
    except (TypeError, ValueError):
        session_count = 0
    tier1 = signal.get("tier1_present") is True
    shared_targets = _strict_int_list(recurrence.get("shared_target_ids", []))
    shared_target_validated = bool(
        recurrence.get("shared_target_validated") is True and shared_targets
    )
    session_recurrence = bool(
        session_count >= 2 and len(session_ids) >= 2 and tier1
    )
    complete = bool(
        validated
        and source == "compactor"
        and (session_recurrence or shared_target_validated)
    )
    return complete, {
        "proposal_validated": validated,
        "proposal_source": source or None,
        "recurrence_session_count": session_count,
        "recurrence_distinct_sessions": len(session_ids),
        "tier1_present": tier1,
        "shared_target_count": len(shared_targets or []),
        "shared_target_recurrence_validated": shared_target_validated,
    }


def _auto_open_probation(conn: sqlite3.Connection, workstream_id: int) -> dict | None:
    row = conn.execute(
        "SELECT op_key,payload_json FROM workstream_ops WHERE op='OPEN' AND state='applied' "
        "AND origin='auto' AND dst_workstream_id=? ORDER BY id DESC LIMIT 1",
        (workstream_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    probation = payload.get("probation")
    if not isinstance(probation, Mapping):
        return None
    return {**dict(probation), "_opening_op_key": row["op_key"]}


def _probation_abandonment(
    conn: sqlite3.Connection,
    signal: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    now: datetime,
) -> bool:
    try:
        workstream_id = int(
            request.get("workstream_id", signal.get("workstream_id"))
        )
    except (TypeError, ValueError):
        return False
    if str(request.get("outcome") or signal.get("outcome") or "").lower() != "abandoned":
        return False
    probation = _auto_open_probation(conn, workstream_id)
    if probation is None:
        return False
    if not _probation_active(probation, now=now):
        return False
    if (
        signal.get("probation_ready") is not True
        or signal.get("auto_apply_eligible") is not True
        or str(signal.get("reason") or "") != "auto_open_probation_no_contacts"
        or signal.get("opening_op_key") != probation.get("_opening_op_key")
    ):
        return False
    try:
        observed = int(signal.get("eligible_session_count") or 0)
        target = int(signal.get("eligible_session_target") or 0)
    except (TypeError, ValueError):
        return False
    if target <= 0 or observed < target:
        return False
    opened_at = str(probation["opened_at"])
    contacted = conn.execute(
        "SELECT 1 FROM retrieval_events WHERE workstream_id_at_event=? AND ts>? "
        "AND session_id IS NOT NULL AND (source='write' OR "
        "(turn>0 AND source IN ('prompt','graph','tool','gate'))) LIMIT 1",
        (workstream_id, opened_at),
    ).fetchone()
    if contacted is not None:
        return False
    dispositions = request.get("dispositions")
    if not isinstance(dispositions, Mapping) or not dispositions:
        return False
    actions = []
    for disposition in dispositions.values():
        if isinstance(disposition, Mapping):
            actions.append(str(disposition.get("action") or "").lower())
        else:
            actions.append(str(disposition).lower())
    return bool(actions and all(action == "release" for action in actions))


def _merge_rails(
    conn: sqlite3.Connection,
    signal: Mapping[str, Any],
    request: Mapping[str, Any],
    module: ModuleType,
) -> tuple[bool, dict]:
    merge_present = _operation_callable(module, "MERGE") is not None
    unmerge_present = _operation_callable(module, "UNMERGE") is not None
    exercised = conn.execute(
        "SELECT 1 FROM workstream_ops WHERE op='UNMERGE' AND state='applied' LIMIT 1"
    ).fetchone() is not None
    policy_complete = bool(
        request.get("priority_policy_complete")
        or signal.get("priority_policy_complete")
    )
    return bool(merge_present and unmerge_present and exercised and policy_complete), {
        "merge_present": merge_present,
        "unmerge_present": unmerge_present,
        "unmerge_exercised": exercised,
        "priority_policy_complete": policy_complete,
    }


def plan_actions(
    conn: sqlite3.Connection,
    *,
    now: datetime | str | None = None,
    receipts_live: bool | None = None,
    quiescence_minutes: int = QUIESCENCE_MINUTES,
    workstreams_module: ModuleType | None = None,
) -> list[dict]:
    """Read the latest derivation and return fail-closed trust-ladder plans."""
    latest = conn.execute(
        "SELECT * FROM workstream_derivations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if latest is None:
        return []
    derivation = dict(latest)
    module = workstreams_module or _load_workstreams()
    receipt_channel = (
        lifecycle_receipts.RECEIPTS_CHANNEL_LIVE
        if receipts_live is None else bool(receipts_live)
    )
    anchor = _parse_ts(now)
    candidates = conn.execute(
        "SELECT * FROM workstream_derivation_candidates WHERE derivation_id=? "
        "ORDER BY rank,candidate_key", (derivation["id"],),
    ).fetchall()
    plans: list[dict] = []
    trailing_regret = _trailing_regret_state(conn)
    for raw in candidates:
        candidate = dict(raw)
        try:
            signal = json.loads(candidate["signal_json"])
        except (TypeError, json.JSONDecodeError):
            signal = {}
        if not isinstance(signal, Mapping):
            signal = {}
        request = signal.get("apply_request")
        if not isinstance(request, Mapping):
            request = {}
        request = dict(request)
        raw_force = "force" in request
        op = str(candidate["op"]).upper()
        candidate_key = str(candidate["candidate_key"])
        stage = "shadow" if not bool(signal.get("qualified")) else "candidate"
        reasons: list[str] = []
        preparation_evidence: dict[str, Any] = {}
        if not bool(signal.get("qualified")):
            reasons.append("candidate_not_qualified")
        if signal.get("cap_pressure") is True:
            reasons.append("cap_pressure")
        if raw_force:
            reasons.append("force_forbidden")
        if not receipt_channel:
            reasons.append("receipt_channel_unavailable")
        if derivation.get("substrate_version") != workstream_detector.SUBSTRATE_VERSION:
            reasons.append("substrate_version_mismatch")

        attestation = _attestation_state(
            conn, derivation_id=int(derivation["id"]), candidate_key=candidate_key,
        )
        if op == "OPEN":
            proposal = _accepted_open_proposal(
                conn, candidate_key=candidate_key, signal=signal,
            )
            preparation_evidence.update(proposal["evidence"])
            if proposal["accepted"]:
                request = dict(proposal["request"])
            elif proposal["event_present"]:
                # A rejected or malformed event cannot borrow a legacy inline
                # request and accidentally become an accepted proposal.
                request = {}
                reasons.append("open_proposal_event_not_accepted")
        elif op == "MERGE":
            request, prepared_evidence, prepared_reasons = _merge_preflight_request(
                conn, signal, request, module, now=anchor,
            )
            preparation_evidence.update(prepared_evidence)
            reasons.extend(prepared_reasons)
        elif op == "CLOSE":
            request, prepared_evidence, prepared_reasons = _close_preflight_request(
                conn, signal, request, module,
            )
            preparation_evidence.update(prepared_evidence)
            reasons.extend(prepared_reasons)

        missing = _required_request_fields(op, request)
        if missing:
            reasons.append("missing_apply_request")
        calibration = _calibration_state(conn, candidate_key)
        if not calibration["graduated"]:
            reasons.append("calibration_not_graduated")
        if not trailing_regret["complete"]:
            reasons.append("trailing_regret_linkage_ambiguous")
        if trailing_regret["exceeds_threshold"]:
            reasons.append("trailing_regret_rate_exceeded")
        target_ids = _target_ids(op, signal, request)
        pinned = _pinned_targets(conn, target_ids)
        if pinned:
            reasons.append("pinned_target")
        member_ids: set[int] = set()
        raw_members = request.get(
            "member_ids",
            request.get("node_ids", signal.get("member_ids", [])),
        )
        for value in raw_members or []:
            try:
                member_ids.add(int(value))
            except (TypeError, ValueError):
                continue
        activity = _recent_activity(
            conn,
            target_ids=target_ids,
            member_ids=member_ids,
            now=anchor,
            quiescence_minutes=quiescence_minutes,
        )
        if activity:
            reasons.append("session_not_quiescent")

        evidence: dict[str, Any] = dict(preparation_evidence)
        if op == "OPEN":
            open_ok, open_evidence = _open_evidence(signal, request)
            evidence.update(open_evidence)
            if open_ok:
                stage = "attested"
            else:
                reasons.append("open_proposal_or_recurrence_incomplete")
        elif op == "MERGE":
            probation_self_revert = bool(
                isinstance(evidence.get("probation_merge_back"), Mapping)
                and evidence["probation_merge_back"].get("eligible") is True
                and evidence["probation_merge_back"].get(
                    "attestation_carveout_eligible"
                ) is True
            )
            if attestation["quorum"] or probation_self_revert:
                stage = "attested"
            else:
                reasons.append("attestation_quorum_missing")
            evidence["probation_self_revert"] = probation_self_revert
            rails_ok, rail_evidence = _merge_rails(conn, signal, request, module)
            evidence.update(rail_evidence)
            if not rails_ok:
                reasons.append("merge_reversibility_rails_incomplete")
        elif op == "CLOSE":
            outcome = str(request.get("outcome") or signal.get("outcome") or "").lower()
            probation = _probation_abandonment(conn, signal, request, now=anchor)
            structural_completion = bool(
                isinstance(evidence.get("completion"), Mapping)
                and evidence["completion"].get("structural_completion")
            )
            completed_attested = bool(
                outcome == "completed"
                and structural_completion
                and attestation["quorum"]
            )
            evidence.update({
                "outcome": outcome,
                "completed_attested": completed_attested,
                "probation_abandonment": probation,
            })
            if completed_attested or probation:
                stage = "attested"
            else:
                reasons.append("close_policy_not_satisfied")
        elif op == "ADOPT":
            adopt_ok, adopt_evidence = _adopt_evidence(signal, request, attestation)
            evidence.update(adopt_evidence)
            if _operation_callable(module, "ADOPT") is None:
                reasons.append("adopt_operation_unavailable")
            if adopt_ok:
                stage = "attested"
            else:
                reasons.append("adopt_corroboration_incomplete")
        else:
            reasons.append("unsupported_auto_operation")
        evidence["trailing_regret"] = dict(trailing_regret)

        existing_key = lifecycle_signals.make_auto_op_key(
            op,
            candidate_key,
            {
                "derivation_key": derivation["derivation_key"],
                "window_start": derivation.get("window_start"),
                "window_end": derivation.get("window_end"),
            },
        )
        existing = conn.execute(
            "SELECT state FROM workstream_ops WHERE op_key=?", (existing_key,),
        ).fetchone()
        if existing is not None:
            reasons.append(f"operation_{existing['state']}")
        eligible = bool(stage == "attested" and not reasons)
        if eligible:
            stage = "eligible"
        plans.append({
            "candidate_key": candidate_key,
            "op": op,
            "rank": int(candidate["rank"]),
            "stage": stage,
            "eligible": eligible,
            "suggestion": not eligible,
            "reason_codes": sorted(set(reasons)),
            "op_key": existing_key,
            "apply_request": request,
            "target_workstream_ids": sorted(target_ids),
            "pinned_workstream_ids": pinned,
            "recent_activity": activity,
            "attestation": attestation,
            "calibration": calibration,
            "policy_evidence": evidence,
            "derivation_id": int(derivation["id"]),
            "derivation_key": derivation["derivation_key"],
        })
    return plans


def auto_plan_is_current(
    conn: sqlite3.Connection,
    *,
    candidate_key: str,
    op_key: str,
    op: str,
    operation_request: Mapping[str, Any] | None = None,
    now: datetime | str | None = None,
    receipts_live: bool | None = None,
    quiescence_minutes: int = QUIESCENCE_MINUTES,
    workstreams_module: ModuleType | None = None,
) -> bool:
    """Fail-closed read guard for use inside a mutation's write transaction.

    Production operations call this after ``BEGIN IMMEDIATE`` and before their
    first state change.  It deliberately recomputes every planner rail instead
    of trusting a pre-dispatch plan that may have raced with a pin, contact,
    attestation, proposal, receipt, derivation, or regret update.
    """
    try:
        normalized_op = str(op).upper()
        matches = [
            plan for plan in plan_actions(
                conn,
                now=now,
                receipts_live=receipts_live,
                quiescence_minutes=quiescence_minutes,
                workstreams_module=workstreams_module,
            )
            if plan["candidate_key"] == str(candidate_key)
            and plan["op"] == normalized_op
            and plan["op_key"] == str(op_key)
        ]
        if len(matches) != 1 or not matches[0]["eligible"]:
            return False
        if not isinstance(operation_request, Mapping):
            return False
        module = workstreams_module or _load_workstreams()
        operation, expected = _prepare_operation_request(
            matches[0], module=module, project_path=None,
        )
        return _canonical_operation_request(
            operation, expected,
        ) == _canonical_operation_request(
            operation, operation_request,
        )
    except Exception:
        return False


def _prepare_operation_request(
    plan: Mapping[str, Any],
    *,
    module: ModuleType,
    project_path: str | None,
) -> tuple[Any, dict[str, Any]]:
    """Translate a governed plan into the exact service-call envelope."""
    op = str(plan["op"])
    operation = _operation_callable(module, op)
    if operation is None:
        raise RuntimeError(f"workstream {op} operation is unavailable")
    request = {
        key: value for key, value in dict(plan["apply_request"]).items()
        if key not in _GOVERNANCE_ONLY_KEYS and key != "force"
    }
    if op == "MERGE":
        if "source_workstream_id" not in request:
            source = request.pop("src_workstream_id", request.pop("src", None))
            if source is not None:
                request["source_workstream_id"] = source
        if "absorber_workstream_id" not in request:
            absorber = request.pop("dst_workstream_id", request.pop("dst", None))
            if absorber is not None:
                request["absorber_workstream_id"] = absorber
        if "reason" in request:
            reason = request.pop("reason")
            evidence = request.get("evidence")
            if not isinstance(evidence, Mapping):
                evidence = {}
            request["evidence"] = {**dict(evidence), "reason": reason}
    elif op == "ADOPT":
        if "workstream_id" not in request:
            target = request.pop("requested_workstream_id", None)
            if target is not None:
                request["workstream_id"] = target
        request["allow_auto_apply"] = True
    request["op_key"] = plan["op_key"]
    request["origin"] = "auto"
    signature = inspect.signature(operation)
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if "force" in signature.parameters:
        request["force"] = False
    if "candidate_key" in signature.parameters or accepts_kwargs:
        request["candidate_key"] = plan["candidate_key"]
    if project_path is not None:
        request["project_path"] = project_path
    if not accepts_kwargs:
        request = {
            key: value for key, value in request.items()
            if key in signature.parameters
        }
    return operation, request


def _canonical_request_value(value: Any) -> Any:
    """Convert operation arguments to deterministic, JSON-comparable data."""
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_request_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_request_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_canonical_request_value(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ),
        )
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes_hex__": bytes(value).hex()}
    if value is None or isinstance(value, (str, int, float, bool)):
        # ``allow_nan=False`` below turns non-finite float inputs into a
        # fail-closed comparison error.
        return value
    raise TypeError(f"unsupported operation request value {type(value).__name__}")


def _canonical_operation_request(
    operation: Any,
    request: Mapping[str, Any],
) -> str:
    """Bind every semantic service argument, including its default value."""
    signature = inspect.signature(operation)
    normalized: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if name in {"conn", "project_path"}:
            continue
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        if name in request:
            normalized[name] = _canonical_request_value(request[name])
        elif parameter.default is not inspect.Parameter.empty:
            normalized[name] = _canonical_request_value(parameter.default)
        else:
            raise ValueError(f"missing required operation argument {name}")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _dispatch(
    conn: sqlite3.Connection,
    plan: Mapping[str, Any],
    *,
    module: ModuleType,
    project_path: str | None,
) -> dict:
    operation, request = _prepare_operation_request(
        plan, module=module, project_path=project_path,
    )
    result = operation(conn, **request)
    return dict(result) if isinstance(result, Mapping) else {"result": result}


def _dispatch_error_code(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    if "conflict" in name:
        return "conflict"
    if "validation" in name or isinstance(exc, (TypeError, ValueError)):
        return "invalid_payload"
    return "mutation_failed"


def _record_raised_failure(
    conn: sqlite3.Connection,
    plan: Mapping[str, Any],
    *,
    exc: Exception,
    project_path: str | None = None,
) -> dict | None:
    """Atomically make a pre-ledger exception terminal under the writer lock."""
    op_key = str(plan["op_key"])
    conn.rollback()
    error_code = _dispatch_error_code(exc)
    lock_project = project_path
    if lock_project is None:
        row = conn.execute("PRAGMA database_list").fetchone()
        database_file = str(row["file"] if row is not None else "")
        lock_project = os.path.dirname(database_file) if database_file else os.getcwd()
    with lockfile.writer_lock(str(lock_project)):
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = db.get_workstream_op(conn, op_key)
            if existing is None:
                request = dict(plan.get("apply_request") or {})
                op = str(plan["op"])
                target_ids = list(plan.get("target_workstream_ids") or [])
                src_id = dst_id = None
                if op == "MERGE":
                    src_id = request.get("source_workstream_id")
                    dst_id = request.get("absorber_workstream_id")
                elif op == "CLOSE":
                    src_id = request.get("workstream_id")
                elif op == "ADOPT":
                    dst_id = request.get(
                        "workstream_id", request.get("requested_workstream_id")
                    )
                elif op == "OPEN" and target_ids:
                    dst_id = target_ids[0]
                db.begin_workstream_op_nc(
                    conn,
                    op_key=op_key,
                    op=op,
                    origin="auto",
                    payload={
                        "request": request,
                        "failure": {"error_class": type(exc).__name__},
                    },
                    candidate_key=str(plan["candidate_key"]),
                    src_workstream_id=(int(src_id) if src_id is not None else None),
                    dst_workstream_id=(int(dst_id) if dst_id is not None else None),
                    forced=False,
                    preflight_token=request.get("preflight_token"),
                )
                existing = db.get_workstream_op(conn, op_key)
            if existing is not None and existing["state"] == "pending":
                existing = db.finish_workstream_op_nc(
                    conn, op_key, state="failed", error_code=error_code,
                )
            conn.commit()
            return existing
        except Exception:
            conn.rollback()
            raise


def run_governed(
    conn: sqlite3.Connection,
    *,
    now: datetime | str | None = None,
    receipts_live: bool | None = None,
    quiescence_minutes: int = QUIESCENCE_MINUTES,
    project_path: str | None = None,
    workstreams_module: ModuleType | None = None,
) -> dict:
    """Execute eligible plans in deterministic rank order; suggestions do nothing."""
    module = workstreams_module or _load_workstreams()
    plans = plan_actions(
        conn,
        now=now,
        receipts_live=receipts_live,
        quiescence_minutes=quiescence_minutes,
        workstreams_module=module,
    )
    applied: list[dict] = []
    failed: list[dict] = []
    for plan in plans:
        if not plan["eligible"]:
            continue
        try:
            result = _dispatch(
                conn, plan, module=module, project_path=project_path,
            )
            if result.get("state") == "applied" and result.get("ok") is True:
                applied.append({
                    "candidate_key": plan["candidate_key"],
                    "op_key": plan["op_key"],
                    "op": plan["op"],
                    "result": result,
                })
            else:
                failed.append({
                    "candidate_key": plan["candidate_key"],
                    "op_key": plan["op_key"],
                    "op": plan["op"],
                    "error": str(result.get("error_code") or "operation_not_applied"),
                    "result": result,
                })
        except Exception as exc:
            # Atomic operation services own rollback. Keep raw exception text
            # out of durable ledgers; persist only a closed error class/code.
            terminal = None
            try:
                terminal = _record_raised_failure(
                    conn, plan, exc=exc, project_path=project_path,
                )
            except Exception:
                conn.rollback()
            failed.append({
                "candidate_key": plan["candidate_key"],
                "op_key": plan["op_key"],
                "op": plan["op"],
                "error": type(exc).__name__,
                "error_code": (
                    terminal.get("error_code")
                    if isinstance(terminal, Mapping) else _dispatch_error_code(exc)
                ),
            })
    return {
        "mode": "governed",
        "plans": plans,
        "applied": applied,
        "failed": failed,
        "suggestion_count": sum(1 for plan in plans if plan["suggestion"]),
    }
