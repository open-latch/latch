"""Unit tests for Step 8 — RAPTOR-style tree build with landmarks.

Deterministic only: build_tree is called with `use_llm=False` so no claude -p
subprocess spawns. In that mode, clusters are identified but summaries are not
written; we verify the clustering + landmark + rebuild mechanics without the
LLM cost. Summary-output parsing is covered separately by pure-text tests.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402

import db  # noqa: E402
import embeddings  # noqa: E402
import tree  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_tree_test_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


def _mk(conn, *, kind="fact", title="t", body="b", status="staging", ref_count=0):
    v = embeddings.embed(f"{title}\n\n{body}")
    nid = db.insert_node(conn, kind=kind, title=title, body=body,
                         status=status, embedding=embeddings.to_blob(v))
    if ref_count:
        conn.execute("UPDATE nodes SET ref_count = ? WHERE id = ?", (ref_count, nid))
        conn.commit()
    return nid


# ---------- clustering primitive ----------

def test_cluster_by_threshold_groups_similar():
    """Three near-identical vectors and one unrelated — should get 2 clusters."""
    a = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    a = a / np.linalg.norm(a)
    b = np.array([0.99, 0.01, 0.0, 0.0], dtype=np.float32)
    b = b / np.linalg.norm(b)
    c = np.array([0.98, 0.0, 0.02, 0.0], dtype=np.float32)
    c = c / np.linalg.norm(c)
    d = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
    vecs = np.stack([a, b, c, d])
    clusters = tree._cluster_by_threshold(vecs, threshold=0.9)
    _assert(len(clusters) == 2, f"expected 2 clusters, got {clusters}")
    big = max(clusters, key=len)
    _assert(sorted(big) == [0, 1, 2], f"first 3 should cluster: {clusters}")
    _assert([3] in clusters, f"4th should be singleton: {clusters}")
    print("PASS cluster_by_threshold_groups_similar")


def test_cluster_by_threshold_empty():
    clusters = tree._cluster_by_threshold(np.empty((0, 4), dtype=np.float32))
    _assert(clusters == [], f"empty input should give empty clusters: {clusters}")
    print("PASS cluster_by_threshold_empty")


def test_cluster_by_threshold_all_singletons():
    """No pairs above threshold -> every node is its own cluster."""
    n = 4
    vecs = np.eye(n, dtype=np.float32)  # pairwise similarity = 0
    clusters = tree._cluster_by_threshold(vecs, threshold=0.5)
    _assert(len(clusters) == n, f"expected {n} singleton clusters, got {clusters}")
    print("PASS cluster_by_threshold_all_singletons")


# ---------- summary output parsing ----------

def test_parse_summary_clean_json():
    raw = '{"title": "deploy pipeline overview", "body": "covers github actions setup"}'
    out = tree._parse_summary_output(raw)
    _assert(out is not None, out)
    _assert(out["title"] == "deploy pipeline overview", out)
    print("PASS parse_summary_clean_json")


def test_parse_summary_envelope():
    inner = {"title": "x", "body": "y"}
    env = json.dumps({"type": "result", "result": json.dumps(inner)})
    out = tree._parse_summary_output(env)
    _assert(out == inner, out)
    print("PASS parse_summary_envelope")


def test_parse_summary_fenced():
    raw = '```json\n{"title":"t","body":"b"}\n```'
    out = tree._parse_summary_output(raw)
    _assert(out is not None and out["title"] == "t", out)
    print("PASS parse_summary_fenced")


def test_parse_summary_missing_fields_returns_none():
    _assert(tree._parse_summary_output('{"title":"only"}') is None, "body missing should fail")
    _assert(tree._parse_summary_output('{"body":"only"}') is None, "title missing should fail")
    _assert(tree._parse_summary_output("") is None, "empty should fail")
    _assert(tree._parse_summary_output("not even json") is None, "garbage should fail")
    print("PASS parse_summary_missing_fields_returns_none")


# ---------- build_tree end-to-end (use_llm=False) ----------

def test_build_tree_empty_kb_is_noop():
    tmp, conn = _fresh_db()
    try:
        out = tree.build_tree(conn, project_path=tmp, use_llm=False)
        _assert(out["ok"] is True and out["leaves"] == 0, out)
        print("PASS build_tree_empty_kb_is_noop")
    finally:
        _cleanup(tmp, conn)


def test_build_tree_identifies_landmarks():
    """A high-ref_count node bypasses clustering."""
    tmp, conn = _fresh_db()
    try:
        lm = _mk(conn, title="landmark node", body="heavily referenced anchor", ref_count=10)
        _mk(conn, title="filler one", body="random content alpha")
        _mk(conn, title="filler two", body="random content beta")
        out = tree.build_tree(conn, project_path=tmp, use_llm=False,
                              landmark_ref_count=5)
        _assert(out["landmarks"] == 1, f"expected 1 landmark: {out}")
        _assert(db.get_node(conn, lm)["parent_id"] is None,
                "landmark parent_id should be NULL")
        _assert(db.get_node(conn, lm)["depth"] == 0,
                "landmark stays at depth=0")
        print(f"PASS build_tree_identifies_landmarks ({out})")
    finally:
        _cleanup(tmp, conn)


def test_build_tree_tiny_kb_no_summaries():
    """With <min_cluster_size similar leaves, no summary should be generated."""
    tmp, conn = _fresh_db()
    try:
        _mk(conn, title="python deploy", body="github actions builds the wheel")
        _mk(conn, title="python builds", body="CI produces a wheel artifact")
        out = tree.build_tree(conn, project_path=tmp, use_llm=False,
                              min_cluster_size=3)
        _assert(out["summaries_generated"] == 0, f"no summaries for <3 members: {out}")
        _assert(out["singletons"] >= 1, out)
        print(f"PASS build_tree_tiny_kb_no_summaries ({out})")
    finally:
        _cleanup(tmp, conn)


def test_build_tree_use_llm_false_generates_no_summary():
    """Even with a qualifying cluster, use_llm=False should skip summary gen."""
    tmp, conn = _fresh_db()
    try:
        # 4 near-identical nodes -> one qualifying cluster.
        _mk(conn, title="docker compose deploy",
            body="we run docker compose up for production deploys")
        _mk(conn, title="deploy via docker compose",
            body="production deployment uses docker compose up -d")
        _mk(conn, title="docker compose production",
            body="compose up handles our production deploy flow")
        _mk(conn, title="production docker compose",
            body="deploy pipeline ends in docker compose up")
        out = tree.build_tree(conn, project_path=tmp, use_llm=False,
                              min_cluster_size=3)
        _assert(out["clusters"] >= 1, out)
        _assert(out["summaries_generated"] == 0,
                f"use_llm=False should never write summaries: {out}")
        depth1 = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE depth > 0 AND status != 'stale'"
        ).fetchone()[0]
        _assert(depth1 == 0, f"no depth>0 nodes should exist: {depth1}")
        print(f"PASS build_tree_use_llm_false_generates_no_summary ({out})")
    finally:
        _cleanup(tmp, conn)


def test_rebuild_stales_prior_summaries():
    """A second build_tree should stale any depth>0 nodes from a prior run."""
    tmp, conn = _fresh_db()
    try:
        # Inject a prior summary node directly.
        v = embeddings.embed("old summary\n\nold summary body")
        prior = db.insert_node(conn, kind="summary", title="old summary",
                               body="old summary body", status="canonical",
                               embedding=embeddings.to_blob(v))
        conn.execute("UPDATE nodes SET depth = 1 WHERE id = ?", (prior,))
        conn.commit()

        # Also add a leaf pointing to it.
        leaf = _mk(conn, title="some leaf", body="some leaf body")
        conn.execute("UPDATE nodes SET parent_id = ? WHERE id = ?", (prior, leaf))
        conn.commit()

        out = tree.build_tree(conn, project_path=tmp, use_llm=False)
        _assert(out["prior_summaries_staled"] == 1, out)
        _assert(db.get_node(conn, prior)["status"] == "stale",
                "prior summary should be stale")
        _assert(db.get_node(conn, leaf)["parent_id"] is None,
                "leaf parent_id should be cleared when parent goes stale")
        print("PASS rebuild_stales_prior_summaries")
    finally:
        _cleanup(tmp, conn)


def test_build_tree_ignores_stale_leaves():
    """Stale nodes must not be counted or clustered."""
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="fresh", body="fresh content")
        b = _mk(conn, title="also fresh", body="another node")
        c = _mk(conn, title="will be stale", body="tombstone")
        db.update_node(conn, c, status="stale")
        out = tree.build_tree(conn, project_path=tmp, use_llm=False)
        _assert(out["leaves"] == 2, f"stale leaf should not count: {out}")
        print("PASS build_tree_ignores_stale_leaves")
    finally:
        _cleanup(tmp, conn)


def test_build_tree_disabled_flag():
    tmp, conn = _fresh_db()
    try:
        import os
        os.environ["CLAUDE_KB_DISABLE"] = "1"
        try:
            out = tree.build_tree(conn, project_path=tmp, use_llm=False)
            _assert(out.get("ok") is False and out.get("reason") == "disabled", out)
            print("PASS build_tree_disabled_flag")
        finally:
            del os.environ["CLAUDE_KB_DISABLE"]
    finally:
        _cleanup(tmp, conn)


# ---------- hash-based skip ----------

def test_cluster_content_hash_stable_for_same_input():
    """Same members + prompt + model -> same hash. Member order irrelevant."""
    m1 = [
        {"id": 1, "kind": "fact", "title": "a", "body": "alpha"},
        {"id": 2, "kind": "fact", "title": "b", "body": "beta"},
    ]
    m2 = list(reversed(m1))
    _assert(tree._cluster_content_hash(m1) == tree._cluster_content_hash(m2),
            "reorder should not change hash")
    print("PASS cluster_content_hash_stable_for_same_input")


def test_cluster_content_hash_changes_with_member_body():
    """Edit a member's body -> different hash (catches content drift)."""
    m1 = [{"id": 1, "kind": "fact", "title": "a", "body": "alpha"}]
    m2 = [{"id": 1, "kind": "fact", "title": "a", "body": "ALPHA-EDITED"}]
    _assert(tree._cluster_content_hash(m1) != tree._cluster_content_hash(m2),
            "body edit should change hash")
    print("PASS cluster_content_hash_changes_with_member_body")


def test_cluster_content_hash_changes_with_prompt():
    """Prompt edit invalidates the cache (the gate risk-callout case)."""
    members = [{"id": 1, "kind": "fact", "title": "a", "body": "alpha"}]
    h1 = tree._cluster_content_hash(members, prompt_template="prompt v1")
    h2 = tree._cluster_content_hash(members, prompt_template="prompt v2")
    _assert(h1 != h2, "different prompt should produce different hash")
    print("PASS cluster_content_hash_changes_with_prompt")


def test_cluster_content_hash_changes_with_model():
    """Model swap invalidates the cache."""
    members = [{"id": 1, "kind": "fact", "title": "a", "body": "alpha"}]
    h1 = tree._cluster_content_hash(members, model_tag="claude-A")
    h2 = tree._cluster_content_hash(members, model_tag="claude-B")
    _assert(h1 != h2, "different model tag should produce different hash")
    print("PASS cluster_content_hash_changes_with_model")


def test_build_tree_reuses_cached_summary_on_hash_match():
    """If a prior summary's content_hash matches the new cluster's hash,
    the summary is reused (no LLM, no insert, not staled)."""
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="docker compose deploy",
                body="we run docker compose up for production deploys")
        b = _mk(conn, title="deploy via docker compose",
                body="production deployment uses docker compose up -d")
        c = _mk(conn, title="docker compose production",
                body="compose up handles our production deploy flow")

        members = [
            dict(conn.execute(
                "SELECT id, kind, title, body FROM nodes WHERE id = ?", (nid,)
            ).fetchone())
            for nid in (a, b, c)
        ]
        cluster_hash = tree._cluster_content_hash(members)

        v = embeddings.embed("cached summary\n\nbody")
        prior = db.insert_node(conn, kind="summary", title="cached summary",
                               body="body", status="canonical",
                               embedding=embeddings.to_blob(v))
        conn.execute(
            "UPDATE nodes SET depth = 1, content_hash = ? WHERE id = ?",
            (cluster_hash, prior),
        )
        conn.commit()

        out = tree.build_tree(conn, project_path=tmp, use_llm=False,
                              min_cluster_size=3)

        _assert(out["summaries_reused"] == 1, f"expected 1 reuse: {out}")
        _assert(out["summaries_generated"] == 0, f"no new summary: {out}")
        _assert(out["prior_summaries_staled"] == 0,
                f"cached summary should not be staled: {out}")
        _assert(db.get_node(conn, prior)["status"] == "canonical",
                "cached summary should remain canonical")
        for nid in (a, b, c):
            _assert(db.get_node(conn, nid)["parent_id"] == prior,
                    f"leaf {nid} should point at cached summary")
        print(f"PASS build_tree_reuses_cached_summary_on_hash_match ({out})")
    finally:
        _cleanup(tmp, conn)


def test_build_tree_backfills_missing_hash():
    """A pre-existing summary with NULL content_hash gets backfilled on the
    next build_tree run from its current children; if the leaves still form
    the same cluster, the now-hashed summary is reused (one-shot migration)."""
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="alpha node", body="alpha shared topic content")
        b = _mk(conn, title="beta node",  body="beta shared topic content")
        c = _mk(conn, title="gamma node", body="gamma shared topic content")

        v = embeddings.embed("legacy summary\n\nlegacy body")
        prior = db.insert_node(conn, kind="summary", title="legacy summary",
                               body="legacy body", status="canonical",
                               embedding=embeddings.to_blob(v))
        conn.execute("UPDATE nodes SET depth = 1 WHERE id = ?", (prior,))
        for nid in (a, b, c):
            conn.execute("UPDATE nodes SET parent_id = ? WHERE id = ?",
                         (prior, nid))
        conn.commit()

        _assert(db.get_node(conn, prior).get("content_hash") is None,
                "pre-run hash should be NULL")

        out = tree.build_tree(conn, project_path=tmp, use_llm=False,
                              min_cluster_size=3)

        post_hash = db.get_node(conn, prior).get("content_hash")
        _assert(post_hash is not None and len(post_hash) == 64,
                f"hash should be backfilled to a sha256: {post_hash}")
        _assert(out["summaries_backfilled"] == 1, out)
        _assert(out["summaries_reused"] == 1, f"backfilled hash should match: {out}")
        _assert(out["prior_summaries_staled"] == 0, out)
        print(f"PASS build_tree_backfills_missing_hash ({out})")
    finally:
        _cleanup(tmp, conn)


def test_build_tree_stales_summary_on_hash_mismatch():
    """If a cached summary's hash doesn't match the new cluster, the summary
    is staled (cache miss). With use_llm=False there's no regen — the cluster
    just ends up with no parent this run."""
    tmp, conn = _fresh_db()
    try:
        _mk(conn, title="docker compose deploy",
            body="we run docker compose up for production deploys")
        _mk(conn, title="deploy via docker compose",
            body="production deployment uses docker compose up -d")
        _mk(conn, title="docker compose production",
            body="compose up handles our production deploy flow")

        v = embeddings.embed("stale-hash summary\n\nbody")
        prior = db.insert_node(conn, kind="summary", title="stale-hash summary",
                               body="body", status="canonical",
                               embedding=embeddings.to_blob(v))
        conn.execute(
            "UPDATE nodes SET depth = 1, content_hash = ? WHERE id = ?",
            ("a" * 64, prior),  # bogus hash, won't match any real cluster
        )
        conn.commit()

        out = tree.build_tree(conn, project_path=tmp, use_llm=False,
                              min_cluster_size=3)

        _assert(out["summaries_reused"] == 0, f"hash mismatch -> miss: {out}")
        _assert(out["prior_summaries_staled"] == 1,
                f"orphaned summary should be staled: {out}")
        _assert(db.get_node(conn, prior)["status"] == "stale",
                "cached summary should be stale")
        print(f"PASS build_tree_stales_summary_on_hash_mismatch ({out})")
    finally:
        _cleanup(tmp, conn)


def test_build_tree_oversized_cluster_is_skipped():
    """Fail-safe collapse guard: a cluster larger than max_cluster_members is
    refused a summary (counted in oversized_skipped) and never persisted, rather
    than written as a giant canonical summary. (Step 1 of the single-link fix.)"""
    tmp, conn = _fresh_db()
    try:
        # 6 near-identical nodes -> one cluster of 6 at the default threshold.
        for i in range(6):
            _mk(conn, title=f"docker compose deploy {i}",
                body="we run docker compose up for production deploys nightly")
        # Pin single-link: the oversized fail-safe is a single-link backstop;
        # bounded average-linkage enforces the cap during merging, so an oversized
        # cluster never reaches the skip path under the default linkage.
        out = tree.build_tree(conn, project_path=tmp, use_llm=False,
                              min_cluster_size=3, max_cluster_members=5,
                              linkage="single")
        _assert(out["oversized_skipped"] == 1,
                f"oversized cluster (6 > 5) should be skipped once: {out}")
        _assert(out["largest_cluster"] >= 6, f"largest_cluster telemetry: {out}")
        depth1 = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE depth > 0 AND status != 'stale'"
        ).fetchone()[0]
        _assert(depth1 == 0, f"oversized cluster must not be persisted: {depth1}")
        print(f"PASS build_tree_oversized_cluster_is_skipped ({out})")
    finally:
        _cleanup(tmp, conn)


def test_average_linkage_breaks_bridge_chain():
    """Single-link chains a weak A~B~C~D bridge into one cluster; bounded
    average-linkage refuses the end-to-end merge and splits it. (id=1560 Step 2.)"""
    ang = np.deg2rad(np.array([0.0, 45.0, 90.0, 135.0], dtype=np.float64))
    V = np.stack([np.cos(ang), np.sin(ang)], axis=1).astype(np.float32)
    thr = 0.5  # adjacent cos45=.707 >= thr; endpoints A..D cos135=-.707 < thr
    single = tree._cluster_by_threshold(V, threshold=thr)
    avg = tree._cluster_average_linkage(V, threshold=thr, max_cluster_members=40)
    _assert(any(len(c) == 4 for c in single),
            f"single-link should chain all 4: {[sorted(c) for c in single]}")
    _assert(max(len(c) for c in avg) < 4,
            f"average-link should NOT chain all 4: {[sorted(c) for c in avg]}")
    print(f"PASS average_linkage_breaks_bridge_chain "
          f"(single={sorted(len(c) for c in single)}, avg={sorted(len(c) for c in avg)})")


def test_average_linkage_respects_size_cap():
    """Bounded average-linkage never emits a cluster larger than the cap, even
    for many near-identical vectors (the size gate is enforced during merging)."""
    rng = np.random.RandomState(0)
    V = np.tile(np.array([1.0, 0.0, 0.0]), (10, 1)) + 0.001 * rng.randn(10, 3)
    V = (V / np.linalg.norm(V, axis=1, keepdims=True)).astype(np.float32)
    clusters = tree._cluster_average_linkage(V, threshold=0.5, max_cluster_members=4)
    _assert(all(len(c) <= 4 for c in clusters),
            f"no cluster may exceed cap=4: {[len(c) for c in clusters]}")
    _assert(sum(len(c) for c in clusters) == 10, "all 10 points accounted for")
    print(f"PASS average_linkage_respects_size_cap ({sorted(len(c) for c in clusters)})")


def test_build_tree_rejects_unknown_linkage():
    """A typo'd linkage must fail fast, not silently fall back to single-link
    (the failure-prone path). Review finding P3. The check runs before any DB
    mutation, so a fresh empty KB is fine here."""
    tmp, conn = _fresh_db()
    try:
        raised = False
        try:
            tree.build_tree(conn, project_path=tmp, use_llm=False, linkage="averge")
        except ValueError:
            raised = True
        _assert(raised, "unknown linkage should raise ValueError")
        print("PASS build_tree_rejects_unknown_linkage")
    finally:
        _cleanup(tmp, conn)


if __name__ == "__main__":
    test_cluster_by_threshold_groups_similar()
    test_cluster_by_threshold_empty()
    test_cluster_by_threshold_all_singletons()
    test_parse_summary_clean_json()
    test_parse_summary_envelope()
    test_parse_summary_fenced()
    test_parse_summary_missing_fields_returns_none()
    test_build_tree_empty_kb_is_noop()
    test_build_tree_identifies_landmarks()
    test_build_tree_tiny_kb_no_summaries()
    test_build_tree_use_llm_false_generates_no_summary()
    test_rebuild_stales_prior_summaries()
    test_build_tree_ignores_stale_leaves()
    test_build_tree_disabled_flag()
    test_cluster_content_hash_stable_for_same_input()
    test_cluster_content_hash_changes_with_member_body()
    test_cluster_content_hash_changes_with_prompt()
    test_cluster_content_hash_changes_with_model()
    test_build_tree_reuses_cached_summary_on_hash_match()
    test_build_tree_backfills_missing_hash()
    test_build_tree_stales_summary_on_hash_mismatch()
    test_build_tree_oversized_cluster_is_skipped()
    test_average_linkage_breaks_bridge_chain()
    test_average_linkage_respects_size_cap()
    test_build_tree_rejects_unknown_linkage()
    print("\nAll tree tests pass.")
