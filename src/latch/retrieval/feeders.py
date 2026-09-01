"""Open-feeder resolution for workstream-centered surfacing.

A *feeder* is a forward-looking node that exists to serve a workstream's end
state: an unresolved open_question or idea that belongs to the workstream, or
any non-stale node pointing at the workstream through a dependency-shaped edge
(advances / motivates / depends_on). Feeders are the read side of
lifecycle-aware capture (KB 2299, sanctioned by 2330): the active goal pulls
its declared building blocks into view instead of waiting for text similarity
to resurface them.

Deterministic by design — SQL over nodes/edges, no model calls — so it is safe
inside project-direction and compactor context assembly.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from latch.store import db  # noqa: E402

# Edge relations that declare "X exists to serve Y". Synonyms are canonicalized
# at insert time (db.canonicalize_relation), so raw equality is enough here.
FEEDER_RELATIONS = ("advances", "motivates", "depends_on")
# Incoming relations that close a feeder. Closure-duty writes these edges
# without touching node status (the node may stay `staging` forever), so the
# edge — not status — is the authoritative "this was resolved" signal.
# Tombstoning the resolution edge re-opens the feeder: the audit-stable
# inverse, never a delete.
RESOLUTION_RELATIONS = ("resolves", "supersedes", "replaces")
DEFAULT_LIMIT = 5
CONTEXT_ROW_CAP = 10


# Shared per-row predicate: no active incoming resolution edge. Interpolated
# into queries where the candidate row is aliased `n`; binds RESOLUTION_RELATIONS.
_NOT_RESOLVED_SQL = """NOT EXISTS (
              SELECT 1 FROM edges res
              WHERE res.dst = n.id
                AND res.status = 'active'
                AND res.relation IN (?, ?, ?)
          )"""


def _active_workstream(conn: sqlite3.Connection, workstream_id: int) -> bool:
    """True only for a live workstream identity.

    Lifecycle reads deliberately fail closed.  A stale (closed or merged-away)
    identity must not keep contributing feeders after its lane has ended.
    Callers that want merge redirection resolve the identity before calling.
    """
    row = conn.execute(
        "SELECT kind, status FROM nodes WHERE id = ?", (workstream_id,),
    ).fetchone()
    return bool(
        row is not None
        and row["kind"] == "workstream"
        and row["status"] != "stale"
    )


def open_feeder_snapshot(
    conn: sqlite3.Connection,
    workstream_id: int,
    *,
    require_active: bool = True,
) -> list[dict]:
    """Return every unresolved feeder with durable intent-edge identities.

    Unlike :func:`open_feeders`, this is an unbounded mutation preflight.  It
    keeps *all* active feeder relations for each node so CLOSE can deterministically
    repoint/tombstone exactly the edges it inspected and can persist those IDs in
    its reversal receipt.
    """
    wid = int(workstream_id)
    if require_active and not _active_workstream(conn, wid):
        return []
    rows = conn.execute(
        f"""
        SELECT n.id, n.kind, n.title, n.status, n.workstream_id, n.updated_at,
               CASE WHEN n.workstream_id = ? THEN 1 ELSE 0 END AS is_member,
               e.id AS edge_id, e.src AS edge_src, e.dst AS edge_dst,
               e.relation AS edge_relation, e.status AS edge_status,
               e.created_by AS edge_created_by
        FROM nodes n
        LEFT JOIN edges e
          ON e.src = n.id
         AND e.dst = ?
         AND e.status = 'active'
         AND e.relation IN (?, ?, ?)
        WHERE n.status != 'stale'
          AND n.kind != 'workstream'
          AND NOT (n.kind = 'open_question' AND n.status = 'canonical')
          AND (
                (n.workstream_id = ? AND n.kind IN ('idea', 'open_question'))
                OR e.id IS NOT NULL
              )
          AND {_NOT_RESOLVED_SQL}
        ORDER BY n.updated_at DESC, n.id DESC, e.id ASC
        """,
        (
            wid, wid, *FEEDER_RELATIONS, wid, *RESOLUTION_RELATIONS,
        ),
    ).fetchall()
    by_id: dict[int, dict] = {}
    for row in rows:
        node_id = int(row["id"])
        item = by_id.setdefault(node_id, {
            "id": node_id,
            "kind": row["kind"],
            "title": row["title"],
            "status": row["status"],
            "workstream_id": row["workstream_id"],
            "updated_at": row["updated_at"],
            "is_member": bool(row["is_member"]),
            "intent_edges": [],
        })
        if row["edge_id"] is not None:
            item["intent_edges"].append({
                "id": int(row["edge_id"]),
                "src": int(row["edge_src"]),
                "dst": int(row["edge_dst"]),
                "relation": row["edge_relation"],
                "status": row["edge_status"],
                "created_by": row["edge_created_by"],
            })
    return list(by_id.values())


def open_feeders(
    conn: sqlite3.Connection, workstream_id: int, *, limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """Return the workstream's unresolved building blocks, newest first.

    Two sources, deduped with the edge relation winning over plain membership
    (the relation carries the declared intent):
      * members with a forward-looking kind — `idea` (any non-stale status;
        canonical means ratified, not finished) and `open_question` (canonical
        means resolved, so only unresolved ones count);
      * nodes with an active advances/motivates/depends_on edge into the
        workstream node — any kind except workstream, same resolved/stale
        exclusions.

    A feeder with an active incoming resolves/supersedes/replaces edge is
    closed and excluded regardless of its own status — closure-duty edges
    land while the node is still `staging`.
    """
    ranked = []
    for item in open_feeder_snapshot(conn, workstream_id):
        projected = {
            name: item[name]
            for name in ("id", "kind", "title", "status", "workstream_id", "updated_at")
        }
        projected["via"] = (
            item["intent_edges"][0]["relation"]
            if item["intent_edges"] else "member"
        )
        ranked.append(projected)
    return ranked[:limit] if limit and limit > 0 else ranked


def focus_feeders(
    conn: sqlite3.Connection, *, per_workstream: int = 3, focus_limit: int = 3,
) -> dict[int, list[dict]]:
    """`open_feeders` for each current focus workstream, keyed by workstream id."""
    out: dict[int, list[dict]] = {}
    for ws in db.get_focus(conn, limit=focus_limit):
        wid = int(ws.get("workstream_id") or ws["id"])
        if not _active_workstream(conn, wid):
            continue
        rows = open_feeders(conn, wid, limit=per_workstream)
        if rows:
            out[wid] = rows
    return out


def merge_feeder_rows(
    conn: sqlite3.Connection, related: list[dict], *, cap: int = CONTEXT_ROW_CAP,
) -> list[dict]:
    """Append focus-workstream feeders to a compactor related-nodes sample.

    Appended rows carry role="open_feeder" plus their workstream id so the
    summarizer can honor the closure duty and target forward-looking links.
    Existing rows are never modified; duplicates by id are skipped.
    """
    seen = {int(r["id"]) for r in related if r.get("id") is not None}
    merged = list(related)
    added = 0
    for wid, rows in focus_feeders(conn).items():
        for feeder in rows:
            if added >= cap:
                return merged
            nid = int(feeder["id"])
            if nid in seen:
                continue
            seen.add(nid)
            merged.append({
                "id": nid,
                "kind": feeder["kind"],
                "title": feeder["title"],
                "role": "open_feeder",
                "workstream_id": wid,
                "via": feeder["via"],
            })
            added += 1
    return merged
