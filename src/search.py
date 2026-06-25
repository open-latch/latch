"""Hybrid retrieval: FTS5 keyword + cosine vector, fused via reciprocal rank."""
from __future__ import annotations

import sqlite3

import numpy as np

import db
import embeddings
import artifacts


# Multiplicative lift applied to the retrieval score of nodes carrying an
# artifact coordinate in the active repo. Boost, NOT a wall: cross-repo hits keep
# their score and stay in the result set (relevance-gated reach-through, id=1508
# pt 3; cross-project reachability, id=1497). Tunable.
SCOPE_BOOST = 0.5


def _nodes_in_repo(conn: sqlite3.Connection, node_ids: list[int], repo: str) -> set[int]:
    """Subset of `node_ids` whose node carries an artifact coordinate in `repo`
    (any path). One indexed lookup (idx_artifact_repo + node_artifact PK), so the
    scope boost adds only negligible latency (id=1329)."""
    if not node_ids or not repo:
        return set()
    placeholders = ",".join("?" for _ in node_ids)
    rows = conn.execute(
        f"SELECT DISTINCT na.node_id FROM node_artifact na "
        f"JOIN artifact a ON a.id = na.artifact_id "
        f"WHERE a.repo = ? AND na.node_id IN ({placeholders})",
        (repo, *node_ids),
    ).fetchall()
    return {r["node_id"] for r in rows}


def apply_scope_boost(
    conn: sqlite3.Connection, rows: list[dict], scope_repo: str | None,
    *, boost: float = SCOPE_BOOST,
) -> list[dict]:
    """Lift the `score` of result rows whose node carries an artifact in
    `scope_repo`, then re-sort descending. Boost-not-wall: cross-repo rows keep
    their score and remain in the list (reach-through). No-op when `scope_repo`
    is falsy — callers passing None are byte-identical. Mutates row scores in
    place and returns the re-sorted list."""
    if not scope_repo or not rows:
        return rows
    in_scope = _nodes_in_repo(
        conn, [r["id"] for r in rows], artifacts.canonicalize_repo(scope_repo),
    )
    if not in_scope:
        return rows
    for r in rows:
        if r["id"] in in_scope:
            r["score"] = r.get("score", 0.0) * (1.0 + boost)
    rows.sort(key=lambda r: -r.get("score", 0.0))
    return rows


def hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    kind: str | None = None,
    limit: int = 10,
    fts_pool: int = 50,
    vec_pool: int = 50,
    rrf_k: int = 60,
    include_stale: bool = False,
    track_access: bool = True,
    scope_repo: str | None = None,
) -> list[dict]:
    """Return top `limit` nodes by reciprocal rank fusion of FTS + cosine.

    `rrf_k` is the standard RRF damping constant; 60 is a common default.
    Stale nodes are filtered out unless `include_stale=True`. When
    `track_access=True`, returned nodes' ref_count is bumped (this is the
    signal the promotion policy + nightly healer key off of).

    `scope_repo` (optional): boost nodes carrying an artifact coordinate in this
    repo so same-repo context ranks first, while cross-repo hits stay reachable
    (relevance-gated reach-through). None = no scoping (byte-identical).
    """
    fts_rows = db.fts_search(conn, query, limit=fts_pool, include_stale=include_stale)
    vec_rows = _vector_search(conn, query, limit=vec_pool, include_stale=include_stale)

    if kind is not None:
        fts_rows = [r for r in fts_rows if r["kind"] == kind]
        vec_rows = [r for r in vec_rows if r["kind"] == kind]

    scores: dict[int, float] = {}
    rows_by_id: dict[int, dict] = {}
    # FTS rows carry a `_fts_snippet` field with the matched span; preserve it
    # across RRF so kb_search compact-mode can surface "what matched" rather
    # than a prefix excerpt for FTS-hit rows. Vector-only hits fall back to
    # prefix in compact_row.
    fts_snippets: dict[int, str] = {}
    for rank, r in enumerate(fts_rows):
        scores[r["id"]] = scores.get(r["id"], 0.0) + 1.0 / (rrf_k + rank + 1)
        rows_by_id[r["id"]] = r
        snip = r.get("_fts_snippet")
        if snip:
            fts_snippets[r["id"]] = snip
    for rank, r in enumerate(vec_rows):
        scores[r["id"]] = scores.get(r["id"], 0.0) + 1.0 / (rrf_k + rank + 1)
        rows_by_id.setdefault(r["id"], r)

    if scope_repo:
        in_scope = _nodes_in_repo(
            conn, list(scores.keys()), artifacts.canonicalize_repo(scope_repo),
        )
        for nid in in_scope:
            scores[nid] *= (1.0 + SCOPE_BOOST)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
    out = []
    for nid, s in ranked:
        row = rows_by_id[nid].copy()
        row["score"] = s
        row.pop("embedding", None)  # don't ship blobs over MCP
        if nid in fts_snippets:
            row["_fts_snippet"] = fts_snippets[nid]
        out.append(row)
    if track_access and out:
        db.bump_ref_count(conn, [r["id"] for r in out])
    return out


def vector_search(
    conn: sqlite3.Connection,
    query: str | None = None,
    *,
    qvec=None,
    limit: int = 10,
    include_stale: bool = False,
    scope_repo: str | None = None,
) -> list[dict]:
    """Pure-vector search (no FTS, no ref_count bump). Public callable used by
    the UserPromptSubmit hook so retrieval ordering is purely semantic and
    doesn't pollute the promotion signal.

    Pass either `query` (string, will embed here) OR a pre-computed `qvec`
    (numpy float32 array) to reuse the prompt embedding from the caller."""
    if qvec is None:
        if not query:
            return []
        qvec = embeddings.embed(query)
    out = None
    if db.vec_loaded(conn):
        try:
            out = _vec_nodes_search(conn, qvec, limit, include_stale=include_stale)
        except sqlite3.OperationalError:
            out = None
    if out is None:
        out = _brute_force_vector_search(conn, qvec, limit, include_stale=include_stale)
    return apply_scope_boost(conn, out, scope_repo)


def _vector_search(
    conn: sqlite3.Connection, query: str, limit: int, *, include_stale: bool = False,
) -> list[dict]:
    qvec = embeddings.embed(query)
    if db.vec_loaded(conn):
        try:
            return _vec_nodes_search(conn, qvec, limit, include_stale=include_stale)
        except sqlite3.OperationalError:
            # Fall through to brute force if the virtual table is missing/broken.
            pass
    return _brute_force_vector_search(conn, qvec, limit, include_stale=include_stale)


def _vec_nodes_search(
    conn: sqlite3.Connection, qvec, limit: int, *, include_stale: bool = False,
) -> list[dict]:
    qblob = embeddings.to_blob(qvec)
    # Over-fetch so we can still return `limit` results after dropping stale.
    k = limit * 2 if not include_stale else limit
    rows = conn.execute(
        "SELECT rowid, distance FROM vec_nodes "
        "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (qblob, k),
    ).fetchall()
    if not rows:
        return []
    ids = [r["rowid"] for r in rows]
    placeholders = ",".join("?" for _ in ids)
    node_rows = conn.execute(
        f"SELECT id, kind, title, body, status, session_id, created_at, updated_at "
        f"FROM nodes WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    nodes_by_id = {r["id"]: dict(r) for r in node_rows}
    out = []
    for r in rows:
        node = nodes_by_id.get(r["rowid"])
        if node is None:
            continue
        if not include_stale and node["status"] == "stale":
            continue
        # Cosine distance in [0, 2]; map to similarity in (0, 1] for RRF-friendly score.
        node["score"] = 1.0 / (1.0 + float(r["distance"]))
        out.append(node)
        if len(out) >= limit:
            break
    return out


def _brute_force_vector_search(
    conn: sqlite3.Connection, qvec, limit: int, *, include_stale: bool = False,
) -> list[dict]:
    where = "WHERE embedding IS NOT NULL"
    if not include_stale:
        where += " AND status != 'stale'"
    rows = conn.execute(
        f"SELECT id, kind, title, body, status, session_id, created_at, updated_at, embedding "
        f"FROM nodes {where}"
    ).fetchall()
    if not rows:
        return []
    mat = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    idx, scores = embeddings.cosine_topk(qvec, mat, k=limit)
    out = []
    for i, s in zip(idx, scores):
        d = dict(rows[i])
        d["score"] = float(s)
        d.pop("embedding", None)
        out.append(d)
    return out
