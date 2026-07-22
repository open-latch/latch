"""Approved user-facing receipts for deterministic workstream lifecycle ops.

The operation ledger is authoritative.  ``workstream_op_events`` records the
one-way "surfaced" flip, which keeps an applied receipt visible on the next
SessionStart even if the process died between commit and display.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import log_utils  # noqa: E402


# The founder sign-off recorded with the chunk-2 unblock enables the approved
# strings below on all SessionStart hosts, including Quiet (bounded to one).
RECEIPTS_CHANNEL_LIVE = True


def _candidate_suggestion(conn: sqlite3.Connection, op: str, signal: dict) -> str:
    """Render one bounded, statement-only trust-ladder suggestion."""
    normalized = str(op).upper()
    lane_ids: list[int] = []
    for key in ("left", "right", "workstream_id", "target_workstream_id"):
        value = signal.get(key)
        if value is None:
            continue
        try:
            lane_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    titles: dict[int, str] = {}
    if lane_ids:
        placeholders = ",".join("?" for _ in lane_ids)
        rows = conn.execute(
            f"SELECT id, title FROM nodes WHERE id IN ({placeholders})",
            lane_ids,
        ).fetchall()
        titles = {int(row["id"]): str(row["title"]) for row in rows}
    if normalized == "MERGE" and len(lane_ids) >= 2:
        left = titles.get(lane_ids[0], f"#{lane_ids[0]}")
        right = titles.get(lane_ids[1], f"#{lane_ids[1]}")
        return (
            f'latch sees recurring overlap between workstreams "{left}" and '
            f'"{right}"; it is gathering more evidence before acting.'
        )
    if normalized == "CLOSE" and lane_ids:
        title = titles.get(lane_ids[0], f"#{lane_ids[0]}")
        return (
            f'latch sees workstream "{title}" may be ready to close; it is '
            "gathering more evidence before acting."
        )
    if normalized == "ADOPT" and lane_ids:
        title = titles.get(lane_ids[-1], f"#{lane_ids[-1]}")
        return (
            f'latch sees recurring work that may belong in workstream "{title}"; '
            "it is gathering more evidence before acting."
        )
    return (
        "latch sees recurring work that may merit a new workstream; it is "
        "waiting for a validated charter before acting."
    )


def opened(title: str, recurrences: int, since: str, done_when: str) -> str:
    return (
        f'latch opened workstream "{title}" — recurred across {int(recurrences)} '
        f"sessions since {since}; Done when: {done_when}."
    )


def merged(src_title: str, dst_title: str, coactive: int, window: int, receipt_id: int) -> str:
    return (
        f'latch merged workstream "{src_title}" into workstream "{dst_title}" — '
        f"co-active in {int(coactive)} of last {int(window)} sessions "
        f"(reversible; receipt #{int(receipt_id)})."
    )


def closed(title: str, outcome: str, reason: str) -> str:
    return f'latch closed workstream "{title}" ({outcome}): {reason}.'


def reopened(title: str, reason: str) -> str:
    return f'latch reopened workstream "{title}": {reason}.'


def adopted(count: int, title: str) -> str:
    return f'latch moved {int(count)} nodes into workstream "{title}".'


def receipt_for_op(row: dict) -> str | None:
    """Render the immutable approved receipt from an operation ledger row."""
    payload = row.get("payload") or {}
    # Migration baselines describe state that existed before the lifecycle
    # ledger.  They are deliberately silent and must never be reconstructed as
    # synthetic OPEN receipts by a later durable-report read.
    if payload.get("baseline") or row.get("origin") == "legacy":
        return None
    if payload.get("receipt"):
        return str(payload["receipt"])
    request = payload.get("request") or {}
    op = str(row.get("op") or "").upper()
    title = str(payload.get("title") or request.get("title") or "")
    if op == "OPEN":
        recurrence = request.get("recurrence") or payload.get("probation") or {}
        return opened(
            title,
            recurrence.get("session_count", recurrence.get("sessions", 0)),
            recurrence.get("since", "unknown"),
            request.get("done_when", ""),
        )
    if op == "CLOSE":
        return closed(title, request.get("outcome", "completed"), request.get("reason", ""))
    if op == "REOPEN":
        return reopened(title, request.get("reason", ""))
    if op == "ADOPT":
        return adopted(len(payload.get("assigned_member_ids") or []), title)
    return None


def pending_receipts(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    """Applied operations whose user-facing receipt has not yet surfaced."""
    rows = conn.execute(
        """
        SELECT o.*
        FROM workstream_ops o
        WHERE o.state = 'applied'
          AND NOT EXISTS (
              SELECT 1 FROM workstream_op_events e
              WHERE e.op_key = o.op_key AND e.event_type = 'receipt_surfaced'
          )
        ORDER BY o.applied_at ASC, o.id ASC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    out = []
    for raw in rows:
        row = dict(raw)
        try:
            row["payload"] = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            row["payload"] = {}
        text = receipt_for_op(row)
        if text:
            out.append({
                "op_key": row["op_key"],
                "op": row["op"],
                "origin": row.get("origin"),
                "workstream_id": (
                    row.get("dst_workstream_id") or row.get("src_workstream_id")
                ),
                "receipt": text,
                "operation_id": int(row["id"]),
            })
    return out


def recent_receipts(conn: sqlite3.Connection, *, limit: int = 10) -> list[dict]:
    """Recent applied lifecycle receipts for durable report surfaces."""
    rows = conn.execute(
        "SELECT * FROM workstream_ops WHERE state = 'applied' "
        "AND origin != 'legacy' "
        "ORDER BY applied_at DESC, id DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    out: list[dict] = []
    for raw in rows:
        row = dict(raw)
        try:
            row["payload"] = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            row["payload"] = {}
        rendered = receipt_for_op(row)
        if rendered:
            out.append({
                "operation_id": int(row["id"]),
                "op_key": row["op_key"],
                "op": row["op"],
                "origin": row.get("origin"),
                "workstream_id": (
                    row.get("dst_workstream_id") or row.get("src_workstream_id")
                ),
                "receipt": rendered,
                "applied_at": row.get("applied_at"),
            })
    return out


def surface_pending_suggestions(
    conn: sqlite3.Connection,
    *,
    session_id: str | None = None,
    limit: int = 1,
) -> list[str]:
    """Surface each stable live candidate at most once, including on Quiet.

    Suggestions are statements, never approval prompts.  They do not create an
    operation row or mutate any workstream; the event only proves that the
    trust ladder has a viable user-facing channel.
    """
    latest = db.latest_workstream_derivation(conn)
    if latest is None:
        return []
    rows = conn.execute(
        "SELECT c.candidate_key, c.op, c.signal_json "
        "FROM workstream_derivation_candidates c "
        "WHERE c.derivation_id=? "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM workstream_op_events e "
        "  WHERE e.candidate_key=c.candidate_key "
        "    AND e.event_type='suggestion_surfaced'"
        ") "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM workstream_ops o "
        "  WHERE o.candidate_key=c.candidate_key AND o.state='applied'"
        ") ORDER BY c.rank, c.candidate_key LIMIT ?",
        (int(latest["id"]), max(20, int(limit) * 4)),
    ).fetchall()
    suggestions: list[tuple[str, str]] = []
    try:
        for row in rows:
            try:
                signal = json.loads(row["signal_json"])
            except (TypeError, json.JSONDecodeError):
                signal = {}
            if not isinstance(signal, dict):
                signal = {}
            if not bool(signal.get("qualified")):
                continue
            candidate_key = str(row["candidate_key"])
            suggestion = _candidate_suggestion(conn, str(row["op"]), signal)
            db.append_workstream_op_event_nc(
                conn,
                event_key=f"suggestion:{candidate_key}",
                candidate_key=candidate_key,
                event_type="suggestion_surfaced",
                payload={"op": str(row["op"]).upper()},
                derivation_key=str(latest["derivation_key"]),
                session_id=session_id,
                require_latest_candidate=True,
            )
            suggestions.append((candidate_key, suggestion))
            if len(suggestions) >= max(1, int(limit)):
                break
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return [suggestion for _key, suggestion in suggestions]


def reconcile_orphaned_restore_ops(
    conn: sqlite3.Connection,
    *,
    project_path: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Recover terminal op keys that survived only in structural JSONL.

    A database restore can rewind ``workstream_ops`` while the daily receipt
    log remains newer.  We cannot reconstruct a lost mutation payload from a
    structural log, so these keys are recorded as ``orphaned_by_restore`` and
    never replayed.  The detector run that follows this reconciliation derives
    the current candidate set from the restored graph.
    """
    anchor = now or datetime.now(timezone.utc)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    start = (anchor - timedelta(days=log_utils.COLD_RETENTION_DAYS)).date()
    end = anchor.date()
    missing: dict[str, dict] = {}
    for row in log_utils.read_log_range(
        "workstream_lifecycle", start, end, project_path=project_path,
    ):
        op_key = str(row.get("op_key") or "").strip()
        op = str(row.get("op") or "").upper()
        if (
            not op_key
            or op not in db.WORKSTREAM_OPS
            or str(row.get("state") or "applied") != "applied"
            or db.get_workstream_op(conn, op_key) is not None
        ):
            continue
        # Last structural row wins only for metadata fields; all qualifying
        # rows describe the same immutable applied key.
        missing[op_key] = {
            "op": op,
            "workstream_id": row.get("workstream_id"),
            "candidate_key": row.get("candidate_key"),
            "session_id": row.get("session_id"),
            "logged_at": row.get("ts"),
        }
    created: list[str] = []
    try:
        for op_key, item in sorted(missing.items()):
            workstream_id = item.get("workstream_id")
            try:
                workstream_id = int(workstream_id) if workstream_id is not None else None
            except (TypeError, ValueError):
                workstream_id = None
            op = str(item["op"])
            src = workstream_id if op == "CLOSE" else None
            dst = workstream_id if op != "CLOSE" else None
            db.begin_workstream_op_nc(
                conn,
                op_key=op_key,
                op=op,
                origin="restore_reconciliation",
                candidate_key=(
                    str(item["candidate_key"])
                    if item.get("candidate_key") else f"restore:{op_key}"
                ),
                session_id=item.get("session_id"),
                src_workstream_id=src,
                dst_workstream_id=dst,
                payload={
                    "request": {},
                    "restore_log": {
                        "logged_at": item.get("logged_at"),
                        "workstream_id": workstream_id,
                    },
                },
            )
            db.finish_workstream_op_nc(
                conn,
                op_key,
                state="orphaned_by_restore",
                error_code="orphaned_by_restore",
            )
            created.append(op_key)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "orphaned_by_restore_count": len(created),
        "op_keys": created,
    }


def reconcile_legacy_workstream_baselines(conn: sqlite3.Connection) -> dict:
    """Give pre-lifecycle active lanes an explicit non-auto origin baseline.

    Baselines describe already-existing state; they do not replay OPEN or emit
    a user-facing receipt.  The paired ``receipt_surfaced`` event prevents a
    migration baseline from masquerading as a newly applied operation.
    """
    rows = conn.execute(
        "SELECT n.id, n.title FROM nodes n "
        "WHERE n.kind='workstream' AND n.status!='stale' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM workstream_ops o WHERE o.state='applied' "
        "  AND o.op='OPEN' AND o.dst_workstream_id=n.id"
        ") ORDER BY n.id"
    ).fetchall()
    created: list[int] = []
    try:
        for row in rows:
            workstream_id = int(row["id"])
            member_ids = [
                int(item["id"])
                for item in conn.execute(
                    "SELECT id FROM nodes WHERE workstream_id=? "
                    "AND kind!='workstream' ORDER BY id",
                    (workstream_id,),
                ).fetchall()
            ]
            op_key = f"baseline:workstream:{workstream_id}"
            candidate_key = f"baseline:{workstream_id}"
            db.begin_workstream_op_nc(
                conn,
                op_key=op_key,
                op="OPEN",
                origin="legacy",
                candidate_key=candidate_key,
                dst_workstream_id=workstream_id,
                payload={
                    "request": {
                        "title": str(row["title"]),
                        "member_ids": member_ids,
                        "origin": "legacy",
                        "recurrence": {
                            "session_count": 0,
                            "session_ids": [],
                            "since": "baseline",
                        },
                    },
                    "title": str(row["title"]),
                    "receipt": None,
                    "assigned_member_ids": member_ids,
                    "watch_pair": None,
                    "probation": {"active": False, "baseline": True},
                    "baseline": True,
                },
            )
            db.finish_workstream_op_nc(conn, op_key, state="applied")
            mark_surfaced_nc(conn, op_key, session_id=None)
            created.append(workstream_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "baseline_count": len(created),
        "workstream_ids": created,
        "origin": "legacy",
    }


def mark_surfaced_nc(
    conn: sqlite3.Connection,
    op_key: str,
    *,
    session_id: str | None = None,
) -> dict:
    row = db.get_workstream_op(conn, op_key)
    if row is None or row["state"] != "applied":
        raise KeyError(f"no applied workstream operation {op_key!r}")
    candidate = row.get("candidate_key") or f"op:{op_key}"
    return db.append_workstream_op_event_nc(
        conn,
        event_key=f"receipt:{op_key}",
        candidate_key=candidate,
        event_type="receipt_surfaced",
        payload={"operation_id": int(row["id"])},
        op_key=op_key,
        session_id=session_id,
    )


def mark_surfaced(
    conn: sqlite3.Connection,
    op_key: str,
    *,
    session_id: str | None = None,
) -> dict:
    try:
        result = mark_surfaced_nc(conn, op_key, session_id=session_id)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def surface_pending(
    conn: sqlite3.Connection,
    *,
    session_id: str | None = None,
    limit: int = 20,
) -> list[str]:
    """Atomically claim pending receipts and return their display strings."""
    items = pending_receipts(conn, limit=limit)
    if not items:
        return []
    try:
        for item in items:
            mark_surfaced_nc(conn, item["op_key"], session_id=session_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return [item["receipt"] for item in items]


def surface_pending_items(
    conn: sqlite3.Connection,
    *,
    session_id: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Claim pending receipts while retaining operation metadata for UI policy."""
    items = pending_receipts(conn, limit=limit)
    if not items:
        return []
    try:
        for item in items:
            mark_surfaced_nc(conn, item["op_key"], session_id=session_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return items


def emit_applied(
    result: dict,
    *,
    project_path: str | None = None,
    session_id: str | None = None,
) -> None:
    """Best-effort structural JSONL telemetry after an applied commit."""
    log_utils.emit_event(
        "workstream_lifecycle",
        {
            "op_key": result.get("op_key"),
            "op": result.get("op"),
            "candidate_key": result.get("candidate_key"),
            "workstream_id": result.get("workstream_id"),
            "state": result.get("state", "applied"),
            "forced": bool(result.get("forced", False)),
        },
        project_path=project_path,
        session_id=session_id,
    )
