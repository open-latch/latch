"""Open-feeder resolution for workstream-centered surfacing.

A *feeder* is a forward-looking node that exists to serve a workstream's end
state: an unresolved open_question or idea that belongs to the workstream, or
any non-stale node pointing at the workstream through a dependency-shaped edge
(advances / motivates / depends_on). Feeders are the read side of
lifecycle-aware capture (KB 2299, sanctioned by 2330): the active goal pulls
its declared building blocks into view instead of waiting for text similarity
to resurface them.

Deterministic by design — SQL over nodes/edges, no model calls — so it is safe
on the SessionStart hot path and inside the compactor's context assembly.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402

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
    members = conn.execute(
        f"""
        SELECT n.id, n.kind, n.title, n.status, n.workstream_id, n.updated_at
        FROM nodes n
        WHERE n.workstream_id = ?
          AND n.status != 'stale'
          AND (n.kind = 'idea'
               OR (n.kind = 'open_question' AND n.status != 'canonical'))
          AND {_NOT_RESOLVED_SQL}
        """,
        (workstream_id, *RESOLUTION_RELATIONS),
    ).fetchall()
    edge_rows = conn.execute(
        f"""
        SELECT n.id, n.kind, n.title, n.status, n.workstream_id, n.updated_at,
               e.relation
        FROM edges e
        JOIN nodes n ON n.id = e.src
        WHERE e.dst = ?
          AND e.status = 'active'
          AND e.relation IN (?, ?, ?)
          AND n.status != 'stale'
          AND n.kind != 'workstream'
          AND NOT (n.kind = 'open_question' AND n.status = 'canonical')
          AND {_NOT_RESOLVED_SQL}
        """,
        (workstream_id, *FEEDER_RELATIONS, *RESOLUTION_RELATIONS),
    ).fetchall()

    by_id: dict[int, dict] = {}
    for row in members:
        d = dict(row)
        d["via"] = "member"
        by_id[int(d["id"])] = d
    for row in edge_rows:
        d = dict(row)
        d["via"] = d.pop("relation")
        by_id[int(d["id"])] = d
    ranked = sorted(
        by_id.values(), key=lambda d: str(d["updated_at"]), reverse=True,
    )
    return ranked[:limit] if limit and limit > 0 else ranked


def focus_feeders(
    conn: sqlite3.Connection, *, per_workstream: int = 3, focus_limit: int = 3,
) -> dict[int, list[dict]]:
    """`open_feeders` for each current focus workstream, keyed by workstream id."""
    out: dict[int, list[dict]] = {}
    for ws in db.get_focus(conn, limit=focus_limit):
        wid = int(ws.get("workstream_id") or ws["id"])
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
