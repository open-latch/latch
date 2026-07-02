"""Step 9 / step 4a — gate chain assembly.

Exercises src/gate.py against throwaway KBs:

- hybrid-search seeding picks up matches and includes stale by default
  (opposite of kb_search), so abandoned-path nodes surface
- focus seeding adds workstreams (deduped against hybrid hits)
- 1- and 2-hop traversal walks both in and out edges over canonical
  traversal relations + related_to, ignoring other free-form relations
- direction tagging matches the edge's seed-perspective (out = seed is src)
- max_hops bounds depth; cycles don't loop
- evidence is deduped (a node reached via multiple paths shows once)
- seeds are excluded from each chain's evidence (no self-citation)
- body_excerpt is truncated to the configured limit
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import db          # noqa: E402
import embeddings  # noqa: E402
import gate        # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_gate_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    try:
        conn.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def _ins(conn, kind, title, body, *, status="staging", workstream_id=None):
    """Insert with embedding so hybrid_search can find it."""
    vec = embeddings.embed(f"{title}\n\n{body}")
    return db.insert_node(
        conn, kind=kind, title=title, body=body, status=status,
        embedding=embeddings.to_blob(vec), workstream_id=workstream_id,
    )


# ---------- hybrid seeding ----------

def test_hybrid_seeding_picks_up_query_matches():
    tmp, conn = _fresh_db()
    try:
        match = _ins(conn, "decision", "Redis wins cache bake-off",
                     "Redis beat Memcached and in-process LRU across latency and consistency criteria")
        _ins(conn, "fact", "unrelated topic", "talking about something else entirely")

        out = gate.assemble_gate(conn, "Redis bake-off")
        seed_ids = {s["id"] for s in out["seeds"]}
        _assert(match in seed_ids, f"match node missing from seeds: {seed_ids}")
        # The "unrelated topic" node may also rank above floor on a small DB,
        # so we don't assert its absence — only that the match is present.
        print("PASS hybrid_seeding_picks_up_query_matches")
    finally:
        _cleanup(tmp, conn)


def test_hybrid_seeding_includes_stale_by_default():
    """The whole point of the gate is finding abandoned paths — stale must
    surface in seeds, opposite of kb_search's default."""
    tmp, conn = _fresh_db()
    try:
        stale_id = _ins(conn, "decision", "in-process queue prototype",
                        "Tried in-process queue; throughput capped at 1/4 of target", status="stale")
        out = gate.assemble_gate(conn, "in-process queue")
        seed_ids = {s["id"] for s in out["seeds"]}
        _assert(stale_id in seed_ids,
                f"stale node should seed by default: {seed_ids}")
        print("PASS hybrid_seeding_includes_stale_by_default")
    finally:
        _cleanup(tmp, conn)


def test_hybrid_seeding_can_exclude_stale():
    tmp, conn = _fresh_db()
    try:
        stale_id = _ins(conn, "decision", "abandoned-thing only match",
                        "abandoned-thing only match body", status="stale")
        out = gate.assemble_gate(
            conn, "abandoned thing", include_stale=False,
        )
        seed_ids = {s["id"] for s in out["seeds"]}
        _assert(stale_id not in seed_ids,
                f"include_stale=False should exclude stale, got {seed_ids}")
        print("PASS hybrid_seeding_can_exclude_stale")
    finally:
        _cleanup(tmp, conn)


# ---------- focus seeding ----------

def test_focus_seeding_adds_workstreams():
    tmp, conn = _fresh_db()
    try:
        ws = _ins(conn, "workstream", "active workstream",
                  "the body of an active workstream")
        db.bump_focus(conn, ws)
        # Pass empty query so hybrid is skipped — isolates the focus-seeding
        # contribution. With only 1-2 nodes a non-empty query would also
        # match via vec_topk regardless of content.
        out = gate.assemble_gate(conn, "")
        seed_ids = {s["id"] for s in out["seeds"]}
        _assert(ws in seed_ids, f"focus workstream missing from seeds: {seed_ids}")
        focus_seeds = [s for s in out["seeds"] if s["source"] == "focus"]
        _assert(any(s["id"] == ws for s in focus_seeds),
                f"workstream not marked source=focus: {out['seeds']}")
        print("PASS focus_seeding_adds_workstreams")
    finally:
        _cleanup(tmp, conn)


def test_focus_seeding_dedupes_against_hybrid():
    tmp, conn = _fresh_db()
    try:
        ws = _ins(conn, "workstream", "cache bake-off workstream",
                  "the cache bake-off workstream body")
        db.bump_focus(conn, ws)
        out = gate.assemble_gate(conn, "cache bake-off workstream")
        ids = [s["id"] for s in out["seeds"]]
        _assert(ids.count(ws) == 1, f"workstream should appear once, got {ids}")
        # First occurrence should be from hybrid (hybrid is collected first).
        for s in out["seeds"]:
            if s["id"] == ws:
                _assert(s["source"] == "hybrid",
                        f"dedupe should keep hybrid source, got {s['source']}")
                break
        print("PASS focus_seeding_dedupes_against_hybrid")
    finally:
        _cleanup(tmp, conn)


def test_focus_seeding_disabled_when_flag_off():
    tmp, conn = _fresh_db()
    try:
        ws = _ins(conn, "workstream", "active workstream",
                  "the body of an active workstream")
        db.bump_focus(conn, ws)
        # Empty query skips hybrid, isolating the focus path.
        out = gate.assemble_gate(
            conn, "", focus_seed=False,
        )
        seed_ids = {s["id"] for s in out["seeds"]}
        _assert(ws not in seed_ids,
                f"focus_seed=False should drop focus seeds: {seed_ids}")
        print("PASS focus_seeding_disabled_when_flag_off")
    finally:
        _cleanup(tmp, conn)


# ---------- traversal ----------

def _seed_only(out, seed_id):
    """Helper: drop chains rooted at non-seed-of-interest. Useful when a
    small test DB makes hybrid pull all nodes as seeds — we only care about
    the chain rooted at the actual seed under test."""
    return next(c for c in out["chains"] if c["seed_id"] == seed_id)


def test_traversal_walks_canonical_relations_one_hop():
    """Use disjoint vocab on seed vs target so the query matches only seed."""
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        target = _ins(conn, "decision", "in-process LRU hypothesis",
                      "in-process LRU hypothesis body")
        db.add_edge(conn, src=seed, dst=target, relation="supersedes")

        out = gate.assemble_gate(conn, "Redis session cache", seed_top_k=1,
                                           focus_seed=False)
        # seed_top_k=1 keeps only the strongest hybrid match.
        seed_ids = {s["id"] for s in out["seeds"]}
        _assert(seed_ids == {seed},
                f"only seed should be a seed: {seed_ids}")
        chain = _seed_only(out, seed)
        evidence_ids = [e["id"] for e in chain["evidence"]]
        _assert(target in evidence_ids,
                f"1-hop supersedes target missing: {evidence_ids}")
        ev = next(e for e in chain["evidence"] if e["id"] == target)
        _assert(ev["via_relation"] == "supersedes", f"relation: {ev}")
        _assert(ev["direction"] == "out", f"out: seed is src; got {ev}")
        _assert(ev["hop"] == 1, f"hop: {ev}")
        _assert(ev["path"] == [target], f"path: {ev}")
        print("PASS traversal_walks_canonical_relations_one_hop")
    finally:
        _cleanup(tmp, conn)


def test_traversal_walks_incoming_edges():
    """Direction='in' when seed is the dst."""
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        constraint = _ins(conn, "fact", "in-process LRU hypothesis",
                          "in-process LRU hypothesis body")
        db.add_edge(conn, src=constraint, dst=seed, relation="constrains")
        out = gate.assemble_gate(conn, "Redis session cache",
                                           seed_top_k=1, focus_seed=False)
        chain = _seed_only(out, seed)
        ev = next(e for e in chain["evidence"] if e["id"] == constraint)
        _assert(ev["direction"] == "in", f"in: seed is dst; got {ev}")
        print("PASS traversal_walks_incoming_edges")
    finally:
        _cleanup(tmp, conn)


def test_traversal_includes_related_to():
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        relate = _ins(conn, "fact", "in-process LRU hypothesis",
                      "in-process LRU hypothesis body")
        db.add_edge(conn, src=seed, dst=relate, relation="related_to")
        out = gate.assemble_gate(conn, "Redis session cache",
                                           seed_top_k=1, focus_seed=False)
        chain = _seed_only(out, seed)
        ids = {e["id"] for e in chain["evidence"]}
        _assert(relate in ids, f"related_to should be traversed: {ids}")
        print("PASS traversal_includes_related_to")
    finally:
        _cleanup(tmp, conn)


def test_traversal_skips_unknown_relation():
    """Free-form relations outside the traversal set must not be walked."""
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        noise = _ins(conn, "fact", "in-process LRU hypothesis",
                     "in-process LRU hypothesis body")
        conn.execute(
            "INSERT INTO edges (src, dst, relation) VALUES (?, ?, 'implements')",
            (seed, noise),
        )
        conn.commit()
        out = gate.assemble_gate(conn, "Redis session cache",
                                           seed_top_k=1, focus_seed=False)
        chain = _seed_only(out, seed)
        ids = {e["id"] for e in chain["evidence"]}
        _assert(noise not in ids,
                f"implements should not be traversed; got {ids}")
        print("PASS traversal_skips_unknown_relation")
    finally:
        _cleanup(tmp, conn)


def test_traversal_canonicalizes_synonyms_before_filtering():
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        prereq = _ins(conn, "fact", "in-process LRU hypothesis",
                      "in-process LRU hypothesis body")
        db.add_edge(conn, src=seed, dst=prereq, relation="requires")
        out = gate.assemble_gate(conn, "Redis session cache",
                                           seed_top_k=1, focus_seed=False)
        chain = _seed_only(out, seed)
        ev = next(e for e in chain["evidence"] if e["id"] == prereq)
        _assert(ev["via_relation"] == "depends_on",
                f"requires should canonicalize to depends_on: {ev}")
        print("PASS traversal_canonicalizes_synonyms_before_filtering")
    finally:
        _cleanup(tmp, conn)


def test_traversal_two_hops():
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        mid = _ins(conn, "decision", "in-process LRU hypothesis",
                   "in-process LRU hypothesis body")
        far = _ins(conn, "fact", "Postgres baseline reference",
                   "Postgres baseline reference body")
        db.add_edge(conn, src=seed, dst=mid, relation="supersedes")
        # hop-2 leg uses a canonical relation: related_to is pruned past hop-1
        # (id=1415 #3), canonical relations still traverse to full depth.
        db.add_edge(conn, src=mid, dst=far, relation="depends_on")

        out = gate.assemble_gate(conn, "Redis session cache",
                                           seed_top_k=1, focus_seed=False, max_hops=2)
        chain = _seed_only(out, seed)
        far_ev = next(e for e in chain["evidence"] if e["id"] == far)
        _assert(far_ev["hop"] == 2, f"far should be hop 2: {far_ev}")
        _assert(far_ev["path"] == [mid, far],
                f"path should include intermediate: {far_ev}")
        print("PASS traversal_two_hops")
    finally:
        _cleanup(tmp, conn)


def test_traversal_max_hops_bounds_depth():
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        mid = _ins(conn, "decision", "in-process LRU hypothesis",
                   "in-process LRU hypothesis body")
        far = _ins(conn, "fact", "Postgres baseline reference",
                   "Postgres baseline reference body")
        db.add_edge(conn, src=seed, dst=mid, relation="supersedes")
        db.add_edge(conn, src=mid, dst=far, relation="related_to")

        out = gate.assemble_gate(conn, "Redis session cache",
                                           seed_top_k=1, focus_seed=False, max_hops=1)
        chain = _seed_only(out, seed)
        ids = {e["id"] for e in chain["evidence"]}
        _assert(mid in ids and far not in ids,
                f"max_hops=1 should stop at mid: {ids}")
        print("PASS traversal_max_hops_bounds_depth")
    finally:
        _cleanup(tmp, conn)


def test_traversal_no_cycles():
    tmp, conn = _fresh_db()
    try:
        a = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        b = _ins(conn, "decision", "in-process LRU hypothesis",
                 "in-process LRU hypothesis body")
        db.add_edge(conn, src=a, dst=b, relation="related_to")
        db.add_edge(conn, src=b, dst=a, relation="related_to")
        out = gate.assemble_gate(conn, "Redis session cache",
                                           seed_top_k=1, focus_seed=False, max_hops=2)
        chain = _seed_only(out, a)
        ids = [e["id"] for e in chain["evidence"]]
        _assert(ids.count(b) == 1, f"b should appear once even with cycle: {ids}")
        print("PASS traversal_no_cycles")
    finally:
        _cleanup(tmp, conn)


def test_traversal_dedupes_same_target_via_multiple_paths():
    """Diamond: seed -> A and seed -> B, both A and B point to common C."""
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        a = _ins(conn, "fact", "alpha branch", "alpha branch body")
        b = _ins(conn, "fact", "beta branch", "beta branch body")
        c = _ins(conn, "fact", "shared target gamma", "shared target gamma body")
        db.add_edge(conn, src=seed, dst=a, relation="related_to")
        db.add_edge(conn, src=seed, dst=b, relation="related_to")
        # hop-2 legs use a canonical relation so the diamond still reaches the
        # shared target — related_to is pruned past hop-1 (id=1415 #3).
        db.add_edge(conn, src=a, dst=c, relation="depends_on")
        db.add_edge(conn, src=b, dst=c, relation="depends_on")

        out = gate.assemble_gate(conn, "Redis session cache",
                                           seed_top_k=1, focus_seed=False, max_hops=2)
        chain = _seed_only(out, seed)
        ids = [e["id"] for e in chain["evidence"]]
        _assert(ids.count(c) == 1, f"C should be deduped: {ids}")
        print("PASS traversal_dedupes_same_target_via_multiple_paths")
    finally:
        _cleanup(tmp, conn)


# ---------- hop-prune + reconciled_by (KB id=1415 #3/#4) ----------

def test_traversal_prunes_related_to_at_hop_2():
    """#3: related_to (the dense ~72% relation) is walked at hop 1 but pruned
    at hop 2, where its fan-out is low-signal and explosive."""
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        mid = _ins(conn, "fact", "mid topical node", "mid topical node body")
        far = _ins(conn, "fact", "far topical node", "far topical node body")
        db.add_edge(conn, src=seed, dst=mid, relation="related_to")
        db.add_edge(conn, src=mid, dst=far, relation="related_to")
        out = gate.assemble_gate(conn, "Redis session cache",
                                 seed_top_k=1, focus_seed=False, max_hops=2)
        chain = _seed_only(out, seed)
        ids = {e["id"] for e in chain["evidence"]}
        _assert(mid in ids, f"hop-1 related_to should still be walked: {ids}")
        _assert(far not in ids, f"hop-2 related_to should be pruned: {ids}")
        print("PASS traversal_prunes_related_to_at_hop_2")
    finally:
        _cleanup(tmp, conn)


def test_traversal_keeps_canonical_after_related_to_hop_1():
    """Only related_to is depth-capped, not the nodes downstream of it: a
    canonical relation at hop 2 is still reached even when hop 1 was related_to."""
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        mid = _ins(conn, "fact", "mid topical node", "mid topical node body")
        far = _ins(conn, "decision", "superseded design", "superseded design body")
        db.add_edge(conn, src=seed, dst=mid, relation="related_to")
        db.add_edge(conn, src=mid, dst=far, relation="supersedes")
        out = gate.assemble_gate(conn, "Redis session cache",
                                 seed_top_k=1, focus_seed=False, max_hops=2)
        chain = _seed_only(out, seed)
        ev = next((e for e in chain["evidence"] if e["id"] == far), None)
        _assert(ev is not None and ev["hop"] == 2,
                f"canonical hop-2 target should be reached: {chain['evidence']}")
        print("PASS traversal_keeps_canonical_after_related_to_hop_1")
    finally:
        _cleanup(tmp, conn)


def test_traversal_walks_reconciled_by():
    """#4: the gate must walk reconciled_by so a seed whose framing was
    reconciled surfaces the reconciling node (latch's staleness mechanism)."""
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        newer = _ins(conn, "decision", "newer scope decision", "newer scope decision body")
        db.add_edge(conn, src=seed, dst=newer, relation="reconciled_by")
        out = gate.assemble_gate(conn, "Redis session cache",
                                 seed_top_k=1, focus_seed=False)
        chain = _seed_only(out, seed)
        ev = next((e for e in chain["evidence"] if e["id"] == newer), None)
        _assert(ev is not None, f"reconciled_by target missing: {chain['evidence']}")
        _assert(ev["via_relation"] == "reconciled_by", f"relation: {ev}")
        print("PASS traversal_walks_reconciled_by")
    finally:
        _cleanup(tmp, conn)


def test_traversal_reconciled_by_survives_hop_2_prune():
    """reconciled_by is decision-bearing, so unlike related_to it is NOT
    depth-capped — it still reaches at hop 2."""
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        mid = _ins(conn, "fact", "mid topical node", "mid topical node body")
        newer = _ins(conn, "decision", "newer scope decision", "newer scope decision body")
        db.add_edge(conn, src=seed, dst=mid, relation="related_to")
        db.add_edge(conn, src=mid, dst=newer, relation="reconciled_by")
        out = gate.assemble_gate(conn, "Redis session cache",
                                 seed_top_k=1, focus_seed=False, max_hops=2)
        chain = _seed_only(out, seed)
        ev = next((e for e in chain["evidence"] if e["id"] == newer), None)
        _assert(ev is not None and ev["hop"] == 2,
                f"reconciled_by should still reach at hop 2: {chain['evidence']}")
        print("PASS traversal_reconciled_by_survives_hop_2_prune")
    finally:
        _cleanup(tmp, conn)


def test_seeds_excluded_from_evidence():
    """If two hybrid hits link to each other, neither one should appear in
    the other's evidence list (they're seeds, rendered separately)."""
    tmp, conn = _fresh_db()
    try:
        seed_a = _ins(conn, "decision", "seed alpha", "cache alpha decision")
        seed_b = _ins(conn, "decision", "seed beta", "cache beta decision")
        db.add_edge(conn, src=seed_a, dst=seed_b, relation="related_to")

        out = gate.assemble_gate(conn, "cache decision")
        seed_ids = {s["id"] for s in out["seeds"]}
        _assert({seed_a, seed_b}.issubset(seed_ids),
                f"both should be seeds: {seed_ids}")
        for chain in out["chains"]:
            ev_ids = {e["id"] for e in chain["evidence"]}
            _assert(not (ev_ids & seed_ids),
                    f"chain {chain['seed_id']} evidence overlaps seeds: "
                    f"{ev_ids & seed_ids}")
        print("PASS seeds_excluded_from_evidence")
    finally:
        _cleanup(tmp, conn)


def test_evidence_includes_stale_targets():
    """Stale traversal targets are kept (the abandoned-path signal)."""
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        old = _ins(conn, "decision", "in-process LRU hypothesis",
                   "in-process LRU hypothesis body", status="stale")
        db.add_edge(conn, src=seed, dst=old, relation="supersedes")
        out = gate.assemble_gate(conn, "Redis session cache",
                                           seed_top_k=1, focus_seed=False)
        chain = _seed_only(out, seed)
        ev = next(e for e in chain["evidence"] if e["id"] == old)
        _assert(ev["status"] == "stale", f"stale should be tagged: {ev}")
        print("PASS evidence_includes_stale_targets")
    finally:
        _cleanup(tmp, conn)


# ---------- output schema ----------

def test_output_schema_top_level_shape():
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "shape test", "...")
        out = gate.assemble_gate(conn, "shape test")
        for k in ("query", "seeds", "chains", "evidence_node_ids"):
            _assert(k in out, f"missing top-level key {k!r}: {out.keys()}")
        _assert(out["query"] == "shape test", "query should round-trip")
        _assert(isinstance(out["seeds"], list), "seeds is a list")
        _assert(isinstance(out["chains"], list), "chains is a list")
        _assert(isinstance(out["evidence_node_ids"], list), "evidence_node_ids is a list")
        print("PASS output_schema_top_level_shape")
    finally:
        _cleanup(tmp, conn)


def test_gate_findings_surface_cited_evidence_for_proceed():
    verdict = {
        "recommendation": "PROCEED",
        "summary": "Use the existing decision path (id=10).",
        "decision_chain": [10],
        "abandoned_paths": [9],
        "active_constraints": [11],
        "current_direction": [10],
        "risk_if_proceed": "Watch for drift.",
        "better_next_action": "",
        "evidence_nodes": [10, 11],
        "load_bearing_claims": [
            {
                "claim": "decision path is current",
                "evidence_type": "kb_node",
                "evidence_ref": 10,
                "gap_type": None,
            }
        ],
        "uncovered_claims": [],
    }
    evidence = [
        {"id": 10, "kind": "decision", "title": "Current path", "status": "canonical"},
        {"id": 11, "kind": "fact", "title": "Boundary", "status": "canonical"},
    ]

    out = gate.format_gate_findings(verdict, evidence, gate_status="OK")

    _assert(out["label"] == "Latch gate findings", out)
    _assert(out["must_display_to_user"] is True, out)
    _assert(out["recommendation"] == "PROCEED", out)
    _assert(out["gate_status"] == "OK", out)
    _assert(out["source"] == "latch_gate", out)
    _assert("Latch ran the gate" in out["receipt"]["summary"], out)
    _assert("current authority" in out["receipt"]["summary"], out)
    _assert(out["receipt"]["used"]["evidence_nodes"] == 2, out)
    _assert(out["receipt"]["used"]["decision_chain"] == 1, out)
    _assert(out["why_it_matters"] == out["receipt"]["summary"], out)
    _assert("status/current authority" in out["display_guidance"], out)
    _assert(out["evidence_nodes"][0]["id"] == 10, out)
    _assert(out["evidence_nodes"][0]["title"] == "Current path", out)
    _assert(out["load_bearing_claims"][0]["evidence_ref"] == 10, out)
    print("PASS gate_findings_surface_cited_evidence_for_proceed")


def test_gate_findings_surface_agent_mistake_redirect():
    verdict = {
        "recommendation": "MODIFY",
        "summary": "Prior agent mistake conflicts with the local-only cache decision.",
        "decision_chain": [12],
        "abandoned_paths": [13],
        "active_constraints": [12],
        "current_direction": [12],
        "risk_if_proceed": "The agent would repeat a prior cache mistake.",
        "better_next_action": "Keep the cache in-process for the local demo.",
        "evidence_nodes": [12, 13],
        "load_bearing_claims": [
            {
                "claim": "local demo caching should stay in-process",
                "evidence_type": "kb_node",
                "evidence_ref": 12,
                "gap_type": None,
            },
        ],
        "uncovered_claims": [],
    }
    evidence = [
        {
            "id": 12,
            "kind": "decision",
            "title": "Keep local demo caching in-process",
            "status": "canonical",
        },
        {
            "id": 13,
            "kind": "fact",
            "title": "Agent rewired cache after the decision",
            "status": "canonical",
        },
    ]

    out = gate.format_gate_findings(verdict, evidence, gate_status="OK")

    _assert(out["label"] == "Latch gate findings", out)
    _assert(out["recommendation"] == "MODIFY", out)
    _assert("Latch ran the gate" in out["receipt"]["summary"], out)
    _assert("current authority" in out["receipt"]["summary"], out)
    _assert(out["evidence_nodes"][0]["status"] == "canonical", out)
    _assert(out["better_next_action"].startswith("Keep the cache"), out)
    print("PASS gate_findings_surface_agent_mistake_redirect")


def test_evidence_node_ids_is_unique_and_sorted():
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        e1 = _ins(conn, "fact", "alpha branch", "alpha branch body")
        e2 = _ins(conn, "fact", "beta branch", "beta branch body")
        db.add_edge(conn, src=seed, dst=e1, relation="related_to")
        db.add_edge(conn, src=seed, dst=e2, relation="related_to")
        out = gate.assemble_gate(conn, "Redis session cache",
                                           seed_top_k=1, focus_seed=False)
        ids = out["evidence_node_ids"]
        _assert(ids == sorted(set(ids)), f"should be sorted unique: {ids}")
        _assert({e1, e2}.issubset(set(ids)), f"both targets present: {ids}")
        print("PASS evidence_node_ids_is_unique_and_sorted")
    finally:
        _cleanup(tmp, conn)


def test_body_excerpt_truncates_long_bodies():
    tmp, conn = _fresh_db()
    try:
        long_body = "x" * 800
        seed = _ins(conn, "fact", "long-body fact", long_body)
        out = gate.assemble_gate(
            conn, "long-body fact", body_excerpt_chars=100,
        )
        s = next(s for s in out["seeds"] if s["id"] == seed)
        _assert(len(s["body_excerpt"]) <= 100,
                f"excerpt too long: {len(s['body_excerpt'])}")
        _assert(s["body_excerpt"].endswith("…"),
                f"truncation should append ellipsis: {s['body_excerpt'][-5:]!r}")
        print("PASS body_excerpt_truncates_long_bodies")
    finally:
        _cleanup(tmp, conn)


def test_body_excerpt_handles_none_body():
    tmp, conn = _fresh_db()
    try:
        # Bypass _ins so we can write a NULL-ish body — embed.embed needs
        # something so use a minimal placeholder, but excerpt should still
        # cope with empty/whitespace.
        seed = _ins(conn, "fact", "tiny", "")
        out = gate.assemble_gate(conn, "tiny")
        s = next(s for s in out["seeds"] if s["id"] == seed)
        _assert(s["body_excerpt"] == "",
                f"empty body should produce empty excerpt: {s['body_excerpt']!r}")
        print("PASS body_excerpt_handles_none_body")
    finally:
        _cleanup(tmp, conn)


def test_empty_db_returns_empty_chains():
    tmp, conn = _fresh_db()
    try:
        out = gate.assemble_gate(conn, "anything")
        _assert(out["seeds"] == [], f"no seeds expected: {out['seeds']}")
        _assert(out["chains"] == [], f"no chains expected: {out['chains']}")
        _assert(out["evidence_node_ids"] == [], "empty evidence_node_ids")
        print("PASS empty_db_returns_empty_chains")
    finally:
        _cleanup(tmp, conn)


# ---------- render-layer prompt caps (KB id=1415) ----------
# These exercise the rendering layer directly with synthetic chain assemblies —
# no DB needed. assemble_gate/traversal semantics are unchanged; the prompt is
# bounded only where it's serialized for the classifier/adversary.

def _ev(eid, *, hop=1, via="related_to", status="staging",
        updated_at="2026-01-01 00:00:00", kind="fact"):
    return {
        "id": eid, "kind": kind, "title": f"node {eid}",
        "body_excerpt": f"body {eid}", "status": status, "workstream_id": None,
        "updated_at": updated_at, "via_relation": via, "direction": "out",
        "hop": hop, "path": [eid],
    }


def _seed(sid, *, kind="decision", status="canonical", source="hybrid"):
    return {
        "id": sid, "kind": kind, "title": f"seed {sid}",
        "body_excerpt": f"seed body {sid}", "status": status,
        "workstream_id": None, "source": source, "score": 1.0,
    }


def _assembly(seeds_and_evidence):
    """seeds_and_evidence: list of (seed_dict, [evidence_dict, ...])."""
    seeds = [s for s, _ in seeds_and_evidence]
    chains = [{"seed_id": s["id"], "evidence": ev} for s, ev in seeds_and_evidence]
    all_ids = sorted({e["id"] for _, ev in seeds_and_evidence for e in ev})
    return {"query": "q", "seeds": seeds, "chains": chains,
            "evidence_node_ids": all_ids}


def _count_rendered_evidence(rendered):
    """Count evidence lines (4-space-indented [id=...]); seed headers are
    'seed [id=' and the omission note is '    … +', so neither is counted."""
    return sum(1 for ln in rendered.splitlines() if ln.startswith("    [id="))


def test_render_caps_evidence_per_chain():
    asm = _assembly([(_seed(1), [_ev(100 + i) for i in range(30)])])
    rendered = gate._render_chain_for_prompt(
        asm, max_evidence_per_chain=12, max_total_evidence=1000, stale_budget=0)
    n = _count_rendered_evidence(rendered)
    _assert(n == 12, f"per-chain cap should render exactly 12, got {n}")
    _assert("+18 lower-signal evidence node(s) omitted" in rendered,
            "omission note missing")
    print("PASS render_caps_evidence_per_chain")


def test_render_ranking_prefers_canonical_over_related_to():
    related = [_ev(100 + i, via="related_to", hop=1) for i in range(12)]
    canonical = [_ev(900 + i, via="supersedes", hop=1) for i in range(3)]
    asm = _assembly([(_seed(1), related + canonical)])
    rendered = gate._render_chain_for_prompt(
        asm, max_evidence_per_chain=12, max_total_evidence=1000, stale_budget=0)
    for cid in (900, 901, 902):
        _assert(f"[id={cid}," in rendered,
                f"canonical evidence {cid} should outrank related_to")
    _assert(_count_rendered_evidence(rendered) == 12, "cap respected")
    print("PASS render_ranking_prefers_canonical_over_related_to")


def test_render_ranking_prefers_reconciled_by():
    related = [_ev(100 + i, via="related_to", hop=1) for i in range(12)]
    recon = [_ev(950, via="reconciled_by", hop=1)]
    asm = _assembly([(_seed(1), related + recon)])
    rendered = gate._render_chain_for_prompt(
        asm, max_evidence_per_chain=12, max_total_evidence=1000, stale_budget=0)
    _assert("[id=950," in rendered,
            "reconciled_by should rank in the high-signal tier and survive the cap")
    print("PASS render_ranking_prefers_reconciled_by")


def test_render_ranking_recency_tilt():
    old = [_ev(100 + i, updated_at="2025-01-01 00:00:00") for i in range(5)]
    new = [_ev(200 + i, updated_at="2026-06-01 00:00:00") for i in range(2)]
    asm = _assembly([(_seed(1), old + new)])
    rendered = gate._render_chain_for_prompt(
        asm, max_evidence_per_chain=2, max_total_evidence=1000, stale_budget=0)
    _assert("[id=200," in rendered and "[id=201," in rendered,
            "newest nodes should survive the recency tilt")
    print("PASS render_ranking_recency_tilt")


def test_render_preserves_stale_budget():
    active = [_ev(100 + i, status="canonical", via="supersedes") for i in range(12)]
    stale = [_ev(800 + i, status="stale", via="supersedes") for i in range(4)]
    asm = _assembly([(_seed(1), active + stale)])
    # Control: active outranks stale, so with no budget all 12 slots go active.
    no_budget = gate._render_chain_for_prompt(
        asm, max_evidence_per_chain=12, max_total_evidence=1000, stale_budget=0)
    _assert(no_budget.count("status=stale") == 0,
            "no-budget control should drop all stale")
    with_budget = gate._render_chain_for_prompt(
        asm, max_evidence_per_chain=12, max_total_evidence=1000, stale_budget=3)
    _assert(with_budget.count("status=stale") >= 3,
            "stale budget should preserve >=3 stale nodes")
    _assert(_count_rendered_evidence(with_budget) == 12,
            "the stale-for-active swap must not exceed the cap")
    print("PASS render_preserves_stale_budget")


def test_render_global_total_cap():
    chains = [(_seed(i), [_ev(i * 1000 + j) for j in range(30)]) for i in (1, 2, 3)]
    asm = _assembly(chains)
    rendered = gate._render_chain_for_prompt(
        asm, max_chains=5, max_evidence_per_chain=12, max_total_evidence=20,
        stale_budget=0)
    n = _count_rendered_evidence(rendered)
    _assert(n == 20, f"global cap should bound total evidence to 20, got {n}")
    print("PASS render_global_total_cap")


def test_select_chain_evidence_never_exceeds_cap():
    evidence = ([_ev(i, status="canonical") for i in range(10)]
                + [_ev(100 + i, status="stale") for i in range(10)])
    out = gate._select_chain_evidence(evidence, max_evidence=5, stale_budget=3)
    _assert(len(out) == 5, f"selection must respect the cap: {len(out)}")
    _assert(sum(1 for e in out if e["status"] == "stale") >= 3,
            "stale budget honored")
    # Degenerate: cap smaller than the budget, pool mostly stale — still bounded.
    tiny = gate._select_chain_evidence(
        [_ev(1, status="canonical")] + [_ev(10 + i, status="stale") for i in range(5)],
        max_evidence=2, stale_budget=3)
    _assert(len(tiny) <= 2, f"tiny cap must stay bounded: {len(tiny)}")
    print("PASS select_chain_evidence_never_exceeds_cap")


if __name__ == "__main__":
    test_hybrid_seeding_picks_up_query_matches()
    test_hybrid_seeding_includes_stale_by_default()
    test_hybrid_seeding_can_exclude_stale()
    test_focus_seeding_adds_workstreams()
    test_focus_seeding_dedupes_against_hybrid()
    test_focus_seeding_disabled_when_flag_off()
    test_traversal_walks_canonical_relations_one_hop()
    test_traversal_walks_incoming_edges()
    test_traversal_includes_related_to()
    test_traversal_skips_unknown_relation()
    test_traversal_canonicalizes_synonyms_before_filtering()
    test_traversal_two_hops()
    test_traversal_max_hops_bounds_depth()
    test_traversal_no_cycles()
    test_traversal_dedupes_same_target_via_multiple_paths()
    test_traversal_prunes_related_to_at_hop_2()
    test_traversal_keeps_canonical_after_related_to_hop_1()
    test_traversal_walks_reconciled_by()
    test_traversal_reconciled_by_survives_hop_2_prune()
    test_seeds_excluded_from_evidence()
    test_evidence_includes_stale_targets()
    test_output_schema_top_level_shape()
    test_gate_findings_surface_cited_evidence_for_proceed()
    test_gate_findings_surface_agent_mistake_redirect()
    test_evidence_node_ids_is_unique_and_sorted()
    test_body_excerpt_truncates_long_bodies()
    test_body_excerpt_handles_none_body()
    test_empty_db_returns_empty_chains()
    test_render_caps_evidence_per_chain()
    test_render_ranking_prefers_canonical_over_related_to()
    test_render_ranking_prefers_reconciled_by()
    test_render_ranking_recency_tilt()
    test_render_preserves_stale_budget()
    test_render_global_total_cap()
    test_select_chain_evidence_never_exceeds_cap()
    print("\nAll gate tests pass.")
