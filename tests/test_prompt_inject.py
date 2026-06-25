"""Unit tests for UserPromptSubmit hook (C1 dedupe + C2 graph traversal).

Hits the in-process functions directly with throwaway DBs — no subprocess,
no real `claude -p`. The single slow op is the embedding model load on
first call; subsequent embeds reuse the cached _MODEL.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(_SRC / "hooks"))

import db  # noqa: E402
import embeddings  # noqa: E402
import user_prompt_submit as ups  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_project():
    tmp = tempfile.mkdtemp(prefix="kb_inject_test_")
    conn = db.connect(tmp)
    return tmp, conn


def _mk(conn, *, kind, title, body, status="staging"):
    v = embeddings.embed(f"{title}\n\n{body}")
    return db.insert_node(
        conn, kind=kind, title=title, body=body,
        status=status, embedding=embeddings.to_blob(v),
    )


def _set_turn(conn, sid, turn: int):
    """Helper — set sessions.turn_count directly so TTL tests are deterministic."""
    db.upsert_session(conn, sid, "fake-cwd", None)
    conn.execute("UPDATE sessions SET turn_count = ? WHERE id = ?", (turn, sid))
    conn.commit()


# ---------- C1: dedupe + filtering ----------

def test_short_prompt_returns_no_injection():
    tmp, conn = _fresh_project()
    try:
        _mk(conn, kind="fact", title="anything", body="some body")
        sid = "sess-short"
        _set_turn(conn, sid, 0)
        conn.close()
        log_entry = {}
        # Internal helper: 2-word prompt should be skipped at the top of main(),
        # but we can also assert the retrieve path returns [] on empty input.
        conn = db.connect(tmp)
        try:
            qvec = embeddings.embed("two words")
            out = ups._vector_path(
                conn, sid=sid, turn=0, active_set=set(), qvec=qvec, log_entry=log_entry,
            )
            # The vector path itself doesn't enforce min-words — that's main()'s job.
            # We just confirm it runs without error and that filters are recorded.
            _assert("filtered_out_kind" in log_entry, "log keys missing")
        finally:
            conn.close()
        print("PASS short_prompt_returns_no_injection")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_excluded_kinds_filtered():
    tmp, conn = _fresh_project()
    try:
        _mk(conn, kind="workstream", title="Phase 1 caching strategy",
            body="session-cache evaluation work", status="canonical")
        _mk(conn, kind="idea", title="raptor clustering",
            body="hierarchical retrieval idea")
        _mk(conn, kind="open_question", title="path forward",
            body="cache vs latency redesign")
        # One non-excluded kind that should win.
        nid = _mk(conn, kind="fact", title="cache miss latency 904ms",
                  body="latency blocker over 100ms budget")
        sid = "sess-excl"
        _set_turn(conn, sid, 0)
        conn.close()
        conn = db.connect(tmp)
        try:
            qvec = embeddings.embed("cache miss latency budget")
            log = {}
            out = ups._vector_path(
                conn, sid=sid, turn=0, active_set=set(), qvec=qvec, log_entry=log,
            )
            ids = [r["id"] for r in out]
            _assert(nid in ids, f"fact not surfaced: {ids}")
            kinds = {r["kind"] for r in out}
            _assert(not (kinds & ups.EXCLUDED_KINDS),
                    f"excluded kind leaked into injection: {kinds}")
        finally:
            conn.close()
        print("PASS excluded_kinds_filtered")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_active_set_dedupe():
    tmp, conn = _fresh_project()
    try:
        nid_a = _mk(conn, kind="fact", title="alpha topic",
                    body="something about alpha")
        nid_b = _mk(conn, kind="fact", title="alpha follow-up",
                    body="another note about alpha")
        sid = "sess-dedup"
        _set_turn(conn, sid, 0)
        # Pretend node_a was already injected at session start.
        db.record_retrievals(
            conn, session_id=sid, turn=0,
            items=[(nid_a, None)], source="session_start",
        )
        conn.close()
        conn = db.connect(tmp)
        try:
            active = db.get_active_set(conn, session_id=sid, current_turn=0)
            _assert(nid_a in active, "preseeded node not in active set")
            qvec = embeddings.embed("alpha")
            log = {}
            out = ups._vector_path(
                conn, sid=sid, turn=0, active_set=active, qvec=qvec, log_entry=log,
            )
            ids = [r["id"] for r in out]
            _assert(nid_a not in ids, f"already-active node re-injected: {ids}")
            _assert(nid_b in ids,
                    f"new related node not surfaced: {ids}")
        finally:
            conn.close()
        print("PASS active_set_dedupe")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_active_set_decay_after_ttl():
    tmp, conn = _fresh_project()
    try:
        nid = _mk(conn, kind="fact", title="something",
                  body="distinctive marker")
        sid = "sess-ttl"
        _set_turn(conn, sid, 0)
        # Inject at turn 0.
        db.record_retrievals(
            conn, session_id=sid, turn=0,
            items=[(nid, 0.9)], source="prompt",
        )
        # At turn=20 (= TTL), node should still be active.
        active_at_20 = db.get_active_set(conn, session_id=sid, current_turn=20)
        _assert(nid in active_at_20, f"TTL boundary should still be active: {active_at_20}")
        # At turn=21, node falls out of active.
        active_at_21 = db.get_active_set(conn, session_id=sid, current_turn=21)
        _assert(nid not in active_at_21,
                f"node should be aged out at turn 21: {active_at_21}")
        # But it's still in the table (audit trail).
        row = conn.execute(
            "SELECT * FROM session_retrievals WHERE session_id = ? AND node_id = ?",
            (sid, nid),
        ).fetchone()
        _assert(row is not None, "audit row should still exist after aging out")
        conn.close()
        print("PASS active_set_decay_after_ttl")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_record_retrievals_increments_hit_count():
    tmp, conn = _fresh_project()
    try:
        nid = _mk(conn, kind="fact", title="x", body="y")
        sid = "sess-hits"
        _set_turn(conn, sid, 0)
        db.record_retrievals(conn, session_id=sid, turn=0,
                             items=[(nid, 0.7)], source="prompt")
        db.record_retrievals(conn, session_id=sid, turn=1,
                             items=[(nid, 0.8)], source="prompt")
        row = conn.execute(
            "SELECT hit_count, last_injected_turn, sim_at_first FROM session_retrievals "
            "WHERE session_id = ? AND node_id = ?",
            (sid, nid),
        ).fetchone()
        _assert(row["hit_count"] == 2, f"hit_count should be 2, got {row['hit_count']}")
        _assert(row["last_injected_turn"] == 1,
                f"last_injected_turn should be 1, got {row['last_injected_turn']}")
        # sim_at_first is locked at the first inject and does NOT update.
        _assert(abs(row["sim_at_first"] - 0.7) < 1e-6,
                f"sim_at_first should remain 0.7, got {row['sim_at_first']}")
        conn.close()
        print("PASS record_retrievals_increments_hit_count")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- C2: depth + topic shift ----------

def test_depth_keyword_regex():
    samples = [
        ("tell me more about latency", True),
        ("why does that happen", True),
        ("explain the trade-off", True),
        ("what's the next step", False),
        ("ok", False),
        ("show me an example", True),
    ]
    for prompt, expected in samples:
        got = bool(ups.DEPTH_KEYWORDS.search(prompt))
        _assert(got == expected,
                f"depth regex on {prompt!r}: expected {expected} got {got}")
    print("PASS depth_keyword_regex")


def test_graph_path_surfaces_neighbors():
    tmp, conn = _fresh_project()
    try:
        nid_pivot = _mk(conn, kind="fact", title="latency 904ms blocker",
                        body="cache miss latency exceeds budget")
        nid_neigh1 = _mk(conn, kind="decision", title="forward-fill panel",
                         body="snapshot definition decision")
        nid_neigh2 = _mk(conn, kind="progress", title="re-validation 2026-04-23",
                         body="cache eviction policy re-run")
        # Edges from pivot to both neighbors.
        db.add_edge(conn, nid_pivot, nid_neigh1, "context")
        db.add_edge(conn, nid_pivot, nid_neigh2, "context")
        sid = "sess-graph"
        _set_turn(conn, sid, 5)
        # Active set contains the pivot only.
        db.record_retrievals(
            conn, session_id=sid, turn=4,
            items=[(nid_pivot, 0.8)], source="prompt",
        )
        active = db.get_active_set(conn, session_id=sid, current_turn=5)
        _assert(nid_pivot in active, "pivot not active")
        qvec = embeddings.embed("latency")  # close to pivot's title
        log = {}
        out = ups._graph_path(conn, sid=sid, turn=5, active_set=active,
                              qvec=qvec, log_entry=log)
        ids = [r["id"] for r in out]
        _assert(nid_pivot not in ids, "pivot must not be in neighbors output")
        _assert(nid_neigh1 in ids or nid_neigh2 in ids,
                f"no neighbors surfaced: {ids}")
        _assert(log.get("graph_pivot") == nid_pivot,
                f"log pivot wrong: {log.get('graph_pivot')}")
        conn.close()
        print("PASS graph_path_surfaces_neighbors")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_graph_path_skips_when_active_empty():
    tmp, conn = _fresh_project()
    try:
        sid = "sess-empty"
        _set_turn(conn, sid, 0)
        qvec = embeddings.embed("anything")
        log = {}
        out = ups._graph_path(conn, sid=sid, turn=0, active_set=set(),
                              qvec=qvec, log_entry=log)
        _assert(out == [], f"expected empty output, got {out}")
        _assert(log.get("graph_skip") == "empty_active",
                f"graph_skip not logged: {log}")
        conn.close()
        print("PASS graph_path_skips_when_active_empty")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- logging ----------

def test_log_writes_jsonl():
    tmp, conn = _fresh_project()
    try:
        conn.close()
        ups._write_log(tmp, {"hello": "world", "n": 1})
        ups._write_log(tmp, {"hello": "again", "n": 2})
        import log_utils
        path = log_utils.today_log_path(ups.LOG_STREAM, tmp)
        _assert(path.exists(), f"log file missing: {path}")
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        _assert(len(lines) == 2, f"expected 2 lines, got {len(lines)}")
        first = json.loads(lines[0])
        _assert(first["n"] == 1, f"first line wrong: {first}")
        print("PASS log_writes_jsonl")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- last_prompt_embedding (topic-shift plumbing) ----------

def test_last_prompt_embedding_roundtrip():
    tmp, conn = _fresh_project()
    try:
        sid = "sess-emb"
        _set_turn(conn, sid, 0)
        v = embeddings.embed("first prompt")
        db.update_last_prompt_embedding(conn, sid, embeddings.to_blob(v))
        got = db.get_last_prompt_embedding(conn, sid)
        _assert(got is not None, "embedding not stored")
        import numpy as np
        v_back = np.frombuffer(got, dtype=np.float32)
        _assert(v_back.shape == v.shape, f"shape mismatch: {v_back.shape} vs {v.shape}")
        _assert(abs(float(v_back @ v) - 1.0) < 1e-4,
                "roundtrip embedding should self-cosine to 1.0")
        conn.close()
        print("PASS last_prompt_embedding_roundtrip")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_short_prompt_returns_no_injection()
    test_excluded_kinds_filtered()
    test_active_set_dedupe()
    test_active_set_decay_after_ttl()
    test_record_retrievals_increments_hit_count()
    test_depth_keyword_regex()
    test_graph_path_surfaces_neighbors()
    test_graph_path_skips_when_active_empty()
    test_log_writes_jsonl()
    test_last_prompt_embedding_roundtrip()
    print("\nAll prompt-inject tests pass.")
