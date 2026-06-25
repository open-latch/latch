"""Tests for the heal cross-scope guard (Artifact Evidence Contract).

Artifacts are EVIDENCE, not law. Heal may caution against a destructive
*deterministic* supersede across disjoint repo scopes (defer to the LLM, which
sees the evidence and may still supersede/reconcile), but it must NEVER
hard-partition: candidate discovery stays broad, and same-scope / overlapping /
either-scopeless pairs behave exactly as before.

Retrieval boost-not-wall and graph/traversal/gate behavior are unchanged BY
CONSTRUCTION — this slice touches only heal arbitration + an artifacts read
helper; no edits to search.py, gate, edge creation, or traversal. (Retrieval
boost is covered by tests/test_artifacts.py.)

Runnable standalone: `python tests/test_heal_scope_guard.py`.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402

import artifacts  # noqa: E402
import db  # noqa: E402
import embeddings  # noqa: E402
import heal  # noqa: E402
import paths  # noqa: E402

FS = frozenset


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _node(nid, *, kind="fact", ref=0, ts="2026-01-01 00:00:00"):
    """A node dict where recency is NEUTRAL (equal ts) so ref_count is the only
    deterministic lever — keeps the guard tests about scope, not timestamps."""
    return {"id": nid, "kind": kind, "title": f"n{nid}", "body": "x",
            "ref_count": ref, "status": "staging", "created_at": ts, "updated_at": ts}


def _isolated_conn():
    """A fully-migrated, isolated KB. Force LEGACY (per-cwd) mode so
    db.connect(tmp) yields a unique DB and the real pinned KB is never touched.
    Using None (not a tmp pin) avoids leaking a stale pin into later pytest
    tests; conftest.py already forces legacy for the whole session, this keeps
    standalone runs hermetic too."""
    tmp = tempfile.mkdtemp(prefix="kb_scope_test_")
    paths._PINNED_DIR = None
    return db.connect(tmp)


def _zero_emb():
    return embeddings.to_blob(np.zeros(embeddings.VEC_DIM if hasattr(embeddings, "VEC_DIM") else 384,
                                       dtype=np.float32))


# --------------------------------------------------------------------------- #
# Pure scope relation
# --------------------------------------------------------------------------- #
def test_scope_relation():
    _assert(artifacts.scope_relation(FS({"R"}), FS({"R"})) == "overlap", "same repo => overlap")
    _assert(artifacts.scope_relation(FS({"A"}), FS({"B"})) == "disjoint", "no shared => disjoint")
    _assert(artifacts.scope_relation(FS({"A", "B"}), FS({"B", "C"})) == "overlap", "shared B => overlap")
    _assert(artifacts.scope_relation(FS(), FS({"A"})) == "either_empty", "left empty => either_empty")
    _assert(artifacts.scope_relation(FS({"A"}), FS()) == "either_empty", "right empty => either_empty")
    print("PASS scope_relation")


# --------------------------------------------------------------------------- #
# three_pass_arbitrate: the deterministic guard matrix
# --------------------------------------------------------------------------- #
def test_same_scope_deterministic_unchanged():
    a, b = _node(1, ref=10), _node(2, ref=1)  # ref_count dominance => deterministic supersede
    v = heal.three_pass_arbitrate(a, b, similarity=0.9, use_llm=False, tier="high",
                                  a_repos=FS({"R"}), b_repos=FS({"R"}))
    _assert(v["decision"] == "supersede" and v["path"] == "ref_count",
            f"same-scope deterministic supersede must STILL fire: {v}")
    _assert(v["winner_id"] == 1, f"higher ref_count (node 1) should win: {v}")
    print("PASS same_scope_deterministic_unchanged")


def test_either_scopeless_deterministic_unchanged():
    a, b = _node(1, ref=10), _node(2, ref=1)
    v = heal.three_pass_arbitrate(a, b, similarity=0.9, use_llm=False, tier="high",
                                  a_repos=FS(), b_repos=FS({"R"}))
    _assert(v["decision"] == "supersede" and v["path"] == "ref_count",
            f"either-scopeless deterministic supersede must STILL fire: {v}")
    print("PASS either_scopeless_deterministic_unchanged")


def test_overlap_deterministic_unchanged():
    a, b = _node(1, ref=10), _node(2, ref=1)
    v = heal.three_pass_arbitrate(a, b, similarity=0.9, use_llm=False, tier="high",
                                  a_repos=FS({"A", "B"}), b_repos=FS({"B"}))
    _assert(v["decision"] == "supersede" and v["path"] == "ref_count",
            f"overlapping-scope deterministic supersede must STILL fire: {v}")
    print("PASS overlap_deterministic_unchanged")


def test_disjoint_defers_deterministic_to_skip():
    a, b = _node(1, ref=10), _node(2, ref=1)  # WOULD ref_count-supersede if not guarded
    v = heal.three_pass_arbitrate(a, b, similarity=0.9, use_llm=False, tier="high",
                                  a_repos=FS({"A"}), b_repos=FS({"B"}))
    _assert(v["decision"] == "keep_both" and v["path"] == "skip",
            f"disjoint scope must NOT deterministically supersede (defer to LLM): {v}")
    print("PASS disjoint_defers_deterministic_to_skip")


def test_disjoint_llm_supersede_allowed():
    """The global-directive case: across disjoint scopes the LLM (seeing the
    evidence + framing) may still supersede/reconcile — the guard only removes
    the SILENT deterministic supersede, not the LLM's authority."""
    a, b = _node(1, ref=10), _node(2, ref=1)
    captured = {}

    def fake_nightly(older, newer, similarity, *, a_repos=FS(), b_repos=FS()):
        captured["a_repos"], captured["b_repos"] = a_repos, b_repos
        return {"decision": "supersede_a", "reason": "global directive supersedes cross-repo"}

    orig = heal._arbitrate_nightly
    heal._arbitrate_nightly = fake_nightly
    try:
        v = heal.three_pass_arbitrate(a, b, similarity=0.9, use_llm=True, tier="high",
                                      a_repos=FS({"A"}), b_repos=FS({"B"}))
    finally:
        heal._arbitrate_nightly = orig
    _assert(v["decision"] == "supersede" and v["path"] == "llm",
            f"LLM must be allowed to supersede across disjoint scopes: {v}")
    _assert(captured.get("a_repos") and captured.get("b_repos"),
            "repo evidence must be threaded into the nightly LLM prompt")
    print("PASS disjoint_llm_supersede_allowed (global-directive case)")


def test_disjoint_llm_disabled_keep_both():
    a, b = _node(1, ref=10), _node(2, ref=1)
    v = heal.three_pass_arbitrate(a, b, similarity=0.9, use_llm=False, tier="high",
                                  a_repos=FS({"A"}), b_repos=FS({"B"}))
    _assert(v["decision"] == "keep_both",
            f"disjoint + no LLM available must be keep_both, never destructive: {v}")
    print("PASS disjoint_llm_disabled_keep_both")


# --------------------------------------------------------------------------- #
# Inline path: new-node repo evidence
# --------------------------------------------------------------------------- #
def test_new_node_repo_scope():
    s = heal._new_node_repo_scope([{"repo": "C:/x", "path": "a.py"}], "C:/proj")
    _assert(s == FS({artifacts.canonicalize_repo("C:/x")}), f"explicit artifact repo wins: {s}")
    s = heal._new_node_repo_scope(None, "C:/proj")
    _assert(s == FS({artifacts.canonicalize_repo("C:/proj")}), f"falls back to project_path: {s}")
    _assert(heal._new_node_repo_scope(None, None) == FS(), "no evidence -> empty scope")
    print("PASS new_node_repo_scope")


def test_inline_arbitrate_prompt_evidence():
    """Inline arbitrate adds an ARTIFACT EVIDENCE block + framing when scoped,
    and leaves the prompt byte-identical when both nodes are scopeless."""
    seen = {}

    def fake_invoke(prompt, **kw):
        seen["payload"] = prompt
        return heal.model_backends.ModelCallResult(
            '{"decision":"keep_both","reason":"ok"}',
            None,
            False,
            "claude",
        )

    orig_invoke = heal.model_backends.invoke_prompt
    orig_dis, orig_inc = heal.paths.is_disabled, heal.paths.is_in_compact
    heal.model_backends.invoke_prompt = fake_invoke
    heal.paths.is_disabled = lambda: False
    heal.paths.is_in_compact = lambda: False
    heal._consecutive_arbitrate_timeouts = 0
    new = {"kind": "fact", "title": "t", "body": "b"}
    old = {"id": 2, "kind": "fact", "title": "o", "body": "b2", "created_at": "x", "updated_at": "y"}
    try:
        heal.arbitrate(new, old, 0.9, new_repos=FS({"A"}), old_repos=FS({"B"}))
        _assert("ARTIFACT EVIDENCE" in seen["payload"], "evidence block present when scoped")
        _assert("PROVENANCE EVIDENCE" in seen["payload"], "framing present when scoped")
        heal._consecutive_arbitrate_timeouts = 0
        heal.arbitrate(new, old, 0.9)  # scopeless
        _assert("ARTIFACT EVIDENCE" not in seen["payload"],
                "NO evidence block when scopeless (prompt unchanged for the 95% majority)")
    finally:
        heal.model_backends.invoke_prompt = orig_invoke
        heal.paths.is_disabled = orig_dis
        heal.paths.is_in_compact = orig_inc
    print("PASS inline_arbitrate_prompt_evidence")


def test_inline_insert_passes_evidence_before_arbitration():
    """insert_with_heal must give arbitrate the new+matched repo scope (derived
    from the intended artifacts/project_path) BEFORE the LLM call."""
    conn = _isolated_conn()
    captured = {}

    def fake_arb(new, old, sim, *, new_repos=FS(), old_repos=FS()):
        captured["new_repos"], captured["old_repos"] = new_repos, old_repos
        return {"decision": "keep_both", "reason": "x"}

    orig_embed, orig_find, orig_arb = heal.embeddings.embed, heal.find_near_duplicates, heal.arbitrate
    heal.embeddings.embed = lambda text: np.zeros(384, dtype=np.float32)
    heal.arbitrate = fake_arb
    try:
        old_id = db.insert_node(conn, kind="fact", title="old", body="b", embedding=_zero_emb())
        artifacts.link_node_artifacts(conn, old_id, [{"repo": "C:/OLD"}])
        heal.find_near_duplicates = lambda c, vec, **kw: [{
            "id": old_id, "kind": "fact", "title": "old", "body": "b",
            "status": "staging", "similarity": 0.95, "created_at": "x", "updated_at": "y",
        }]
        heal.insert_with_heal(
            conn, kind="fact", title="new", body="b2",
            use_llm=True, project_path=None,          # None -> skip budget gate
            artifacts=[{"repo": "C:/NEW"}],
        )
    finally:
        heal.embeddings.embed, heal.find_near_duplicates, heal.arbitrate = orig_embed, orig_find, orig_arb

    _assert(captured.get("new_repos") == FS({artifacts.canonicalize_repo("C:/NEW")}),
            f"new-node repo evidence must reach arbitrate: {captured.get('new_repos')}")
    _assert(captured.get("old_repos") == FS({artifacts.canonicalize_repo("C:/OLD")}),
            f"matched-node repo evidence must reach arbitrate: {captured.get('old_repos')}")
    print("PASS inline_insert_passes_evidence_before_arbitration")


# --------------------------------------------------------------------------- #
# Scope helpers over a real DB
# --------------------------------------------------------------------------- #
def test_node_repo_scope_and_disjoint_db():
    conn = _isolated_conn()
    n1 = db.insert_node(conn, kind="fact", title="a", body="x", embedding=_zero_emb())
    n2 = db.insert_node(conn, kind="fact", title="b", body="y", embedding=_zero_emb())
    n3 = db.insert_node(conn, kind="fact", title="c", body="z", embedding=_zero_emb())  # scopeless
    artifacts.link_node_artifacts(conn, n1, [{"repo": "C:/A", "path": "f.py"}])
    artifacts.link_node_artifacts(conn, n2, [{"repo": "C:/B"}])
    _assert(artifacts.node_repo_scope(conn, n1) == FS({artifacts.canonicalize_repo("C:/A")}),
            "n1 scope = {C:/A}")
    _assert(artifacts.node_repo_scope(conn, n3) == FS(), "n3 scopeless")
    _assert(artifacts.is_cross_scope_disjoint(conn, n1, n2) is True, "A vs B disjoint")
    _assert(artifacts.is_cross_scope_disjoint(conn, n1, n3) is False, "scopeless -> NOT disjoint")
    cache: dict = {}
    artifacts.node_repo_scope(conn, n1, cache=cache)
    _assert(n1 in cache, "cache memoizes the repo set")
    print("PASS node_repo_scope_and_disjoint_db")


if __name__ == "__main__":
    test_scope_relation()
    test_same_scope_deterministic_unchanged()
    test_either_scopeless_deterministic_unchanged()
    test_overlap_deterministic_unchanged()
    test_disjoint_defers_deterministic_to_skip()
    test_disjoint_llm_supersede_allowed()
    test_disjoint_llm_disabled_keep_both()
    test_new_node_repo_scope()
    test_inline_arbitrate_prompt_evidence()
    test_inline_insert_passes_evidence_before_arbitration()
    test_node_repo_scope_and_disjoint_db()
    print("\nAll heal scope-guard tests pass.")
