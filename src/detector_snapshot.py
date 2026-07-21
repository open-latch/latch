"""Small deterministic authority snapshots for the local dev detector.

The functions accept an existing SQLite connection so latency-sensitive hooks
can freeze node state without opening another database or running migrations.
No titles or bodies are returned; they are used only to compute a versioned
content digest.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterable


HASH_ALGORITHM = "sha256"
HASH_VERSION = 1


def snapshot_node(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    score: float | None = None,
    snapshot_at: str | None = None,
) -> dict:
    """Freeze status, authority, and content identity for one node."""
    row = conn.execute(
        "SELECT id, kind, title, body, status, created_at, updated_at "
        "FROM nodes WHERE id = ?",
        (int(node_id),),
    ).fetchone()
    captured_at = snapshot_at or _now_iso()
    if row is None:
        return {
            "id": int(node_id),
            "status": None,
            "authority": "NOT_FOUND",
            "superseded_by": [],
            "reconciled_by": [],
            "content_hash": None,
            "score": score,
            "snapshot_at": captured_at,
        }

    item = dict(row)
    superseded_by = [
        int(r["src"])
        for r in conn.execute(
            "SELECT src FROM edges WHERE dst = ? AND status = 'active' "
            "AND relation IN ('supersedes', 'replaces') ORDER BY src",
            (int(node_id),),
        ).fetchall()
    ]
    reconciled_by = [
        int(r["dst"])
        for r in conn.execute(
            "SELECT e.dst FROM edges e JOIN nodes n ON n.id = e.dst "
            "WHERE e.src = ? AND e.status = 'active' "
            "AND e.relation = 'reconciled_by' "
            "AND COALESCE(n.status, '') != 'stale' ORDER BY e.dst",
            (int(node_id),),
        ).fetchall()
    ]
    if item["status"] == "stale" or superseded_by:
        authority = "STALE"
    elif reconciled_by:
        authority = "RECONCILED"
    else:
        authority = "OK"

    canonical_content = json.dumps(
        {
            "kind": item["kind"],
            "title": item["title"],
            "body": item["body"],
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(
        canonical_content.encode("utf-8", errors="replace")
    ).hexdigest()
    return {
        "id": int(item["id"]),
        "kind": item["kind"],
        "status": item["status"],
        "authority": authority,
        "superseded_by": superseded_by,
        "reconciled_by": reconciled_by,
        "content_hash": {
            "algorithm": HASH_ALGORITHM,
            "version": HASH_VERSION,
            "value": digest,
        },
        "score": score,
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
        "snapshot_at": captured_at,
    }


def snapshot_nodes(
    conn: sqlite3.Connection,
    node_ids: Iterable[int],
    *,
    scores: dict[int, float] | None = None,
    limit: int = 32,
    snapshot_at: str | None = None,
) -> list[dict]:
    """Freeze a bounded, stable set while preserving caller priority order."""
    ids: list[int] = []
    seen: set[int] = set()
    for raw_id in node_ids:
        node_id = int(raw_id)
        if node_id not in seen:
            seen.add(node_id)
            ids.append(node_id)
        if len(ids) >= max(0, int(limit)):
            break
    captured_at = snapshot_at or _now_iso()
    conn.execute("SAVEPOINT detector_snapshot_batch")
    try:
        snapshots = [
            snapshot_node(
                conn,
                node_id,
                score=(scores or {}).get(node_id),
                snapshot_at=captured_at,
            )
            for node_id in ids
        ]
        conn.execute("RELEASE SAVEPOINT detector_snapshot_batch")
        return snapshots
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT detector_snapshot_batch")
        conn.execute("RELEASE SAVEPOINT detector_snapshot_batch")
        raise


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
