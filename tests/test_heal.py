"""Unit tests for Step 3 — on-insert heal.

Exercises find_near_duplicates (vec_nodes + brute-force paths), apply_supersede,
apply_keep_both, insert_with_heal (use_llm=False path only — LLM is mocked),
arbitrator output parsing, and stale-exclusion in search/recent.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from latch.store import db  # noqa: E402
from latch.retrieval import embeddings  # noqa: E402
from latch.pipeline import heal  # noqa: E402
from latch.common import log_utils  # noqa: E402
from latch.store import paths  # noqa: E402
from latch.retrieval import search as searchmod  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_heal_test_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


# ---------- find_near_duplicates ----------

def test_find_near_duplicates_hits_similar():
    tmp, conn = _fresh_db()
    try:
        v1 = embeddings.embed("The Red Sox won the game in extra innings")
        db.insert_node(conn, kind="fact", title="Red Sox walkoff win",
                       body="The Red Sox beat the Yankees 5-4 in the 11th inning.",
                       embedding=embeddings.to_blob(v1))
        q = embeddings.embed("Red Sox beat Yankees 5-4 in extras")
        cands = heal.find_near_duplicates(conn, q, threshold=0.5)
        _assert(len(cands) >= 1, f"expected a hit, got {cands}")
        _assert(cands[0]["similarity"] >= 0.5, cands[0])
        print(f"PASS find_near_duplicates_hits_similar (sim={cands[0]['similarity']:.3f})")
    finally:
        _cleanup(tmp, conn)


def test_find_near_duplicates_misses_unrelated():
    tmp, conn = _fresh_db()
    try:
        v1 = embeddings.embed("A recipe for chocolate chip cookies")
        db.insert_node(conn, kind="fact", title="cookie recipe",
                       body="flour, butter, sugar, chocolate chips",
                       embedding=embeddings.to_blob(v1))
        q = embeddings.embed("The weather forecast predicts heavy snow")
        cands = heal.find_near_duplicates(conn, q, threshold=0.85)
        _assert(cands == [], f"expected no hits, got {cands}")
        print("PASS find_near_duplicates_misses_unrelated")
    finally:
        _cleanup(tmp, conn)


def test_find_near_duplicates_excludes_stale():
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("Postgres connection pool settings for prod")
        nid = db.insert_node(conn, kind="fact", title="pg pool",
                             body="max_connections=200", embedding=embeddings.to_blob(v))
        db.update_node(conn, nid, status="stale")
        q = embeddings.embed("Postgres pool config for production")
        cands = heal.find_near_duplicates(conn, q, threshold=0.5)
        _assert(cands == [], f"stale leaked through: {cands}")
        print("PASS find_near_duplicates_excludes_stale")
    finally:
        _cleanup(tmp, conn)


def test_find_near_duplicates_excludes_workstreams():
    """Lifecycle lanes are structural state, never generic heal candidates."""
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("release automation migration workstream")
        wid = db.insert_node(
            conn, kind="workstream", title="release automation",
            body="migrate the release pipeline", embedding=embeddings.to_blob(v),
        )
        cands = heal.find_near_duplicates(conn, v, threshold=0.5)
        _assert(wid not in {c["id"] for c in cands},
                f"workstream leaked into heal arbitration: {cands}")
        print("PASS find_near_duplicates_excludes_workstreams")
    finally:
        _cleanup(tmp, conn)


def test_find_near_duplicates_excludes_self():
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("build deployment uses docker compose")
        nid = db.insert_node(conn, kind="fact", title="deploy",
                             body="docker compose up -d", embedding=embeddings.to_blob(v))
        cands = heal.find_near_duplicates(conn, v, exclude_id=nid, threshold=0.5)
        _assert(cands == [], f"self leaked through: {cands}")
        print("PASS find_near_duplicates_excludes_self")
    finally:
        _cleanup(tmp, conn)


def test_find_near_duplicates_brute_force_path():
    """Force vec off and verify brute-force path returns the same shape."""
    tmp, conn = _fresh_db()
    try:
        v1 = embeddings.embed("Nightly batch job runs at 2am UTC")
        db.insert_node(conn, kind="fact", title="batch schedule",
                       body="2am UTC nightly", embedding=embeddings.to_blob(v1))
        conn._kb_vec_loaded = False  # force brute
        q = embeddings.embed("Batch runs every night at 2 UTC")
        cands = heal.find_near_duplicates(conn, q, threshold=0.5)
        _assert(len(cands) >= 1 and "similarity" in cands[0], cands)
        print(f"PASS find_near_duplicates_brute_force_path (sim={cands[0]['similarity']:.3f})")
    finally:
        _cleanup(tmp, conn)


def test_public_vector_search_preserves_workstream_id():
    """The brute-force/vector handoff must retain event-time lane attribution."""
    tmp, conn = _fresh_db()
    try:
        q = embeddings.embed("release automation migration")
        wid = db.insert_node(
            conn, kind="workstream", title="release automation",
            body="migrate the release pipeline",
        )
        nid = db.insert_node(
            conn, kind="decision", title="release migration decision",
            body="use the automated release pipeline",
            embedding=embeddings.to_blob(q), workstream_id=wid,
        )
        conn._kb_vec_loaded = False
        rows = searchmod.vector_search(conn, qvec=q, limit=5)
        hit = next((r for r in rows if r["id"] == nid), None)
        _assert(hit is not None, f"expected vector hit for node {nid}: {rows}")
        _assert(hit["workstream_id"] == wid,
                f"vector result lost workstream_id={wid}: {hit}")
        print("PASS public_vector_search_preserves_workstream_id")
    finally:
        _cleanup(tmp, conn)


# ---------- apply_supersede / apply_keep_both ----------

def test_apply_supersede_marks_stale_and_edges():
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("foo")
        old = db.insert_node(conn, kind="fact", title="old", body="old body",
                             embedding=embeddings.to_blob(v))
        new = db.insert_node(conn, kind="fact", title="new", body="new body",
                             embedding=embeddings.to_blob(v))
        heal.apply_supersede(conn, new_id=new, old_id=old)
        old_node = db.get_node(conn, old)
        new_node = db.get_node(conn, new)
        _assert(old_node["status"] == "stale", f"old not stale: {old_node['status']}")
        _assert(new_node["status"] == "staging", new_node["status"])
        edges = db.neighbors(conn, new)
        rels = [(e["relation"], e["src"], e["dst"]) for e in edges]
        _assert(("supersedes", new, old) in rels, f"missing supersedes edge: {rels}")
        print("PASS apply_supersede_marks_stale_and_edges")
    finally:
        _cleanup(tmp, conn)


def test_apply_keep_both_adds_related_edge():
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("bar")
        a = db.insert_node(conn, kind="fact", title="a", body="aa",
                           embedding=embeddings.to_blob(v))
        b = db.insert_node(conn, kind="fact", title="b", body="bb",
                           embedding=embeddings.to_blob(v))
        heal.apply_keep_both(conn, new_id=b, old_id=a)
        _assert(db.get_node(conn, a)["status"] == "staging", "a status changed")
        _assert(db.get_node(conn, b)["status"] == "staging", "b status changed")
        edges = db.neighbors(conn, b)
        rels = [(e["relation"], e["src"], e["dst"]) for e in edges]
        _assert(("related_to", b, a) in rels, f"missing related_to edge: {rels}")
        print("PASS apply_keep_both_adds_related_edge")
    finally:
        _cleanup(tmp, conn)


def test_prepare_edge_is_transaction_neutral():
    """_prepare_edge writes via db.add_edge_nc only: nothing is visible on an
    independent connection until the caller commits, nothing is emitted, and
    the prepared envelope carries no elapsed_ms (the post-commit emitter
    stamps it at emission time)."""
    tmp, conn = _fresh_db()
    other = db.connect(tmp)
    emitted = []
    original_emit = log_utils.emit_event
    try:
        a = db.insert_node(conn, kind="fact", title="winner", body="w")
        b = db.insert_node(conn, kind="fact", title="loser", body="l")
        log_utils.emit_event = lambda *args, **kwargs: emitted.append(args)
        try:
            events = heal._prepare_edge(conn, a, b, "supersedes")
            _assert(heal._prepare_edge(conn, b, a, "related_to") == [],
                    "related_to must prepare no reconciliation envelope")
        finally:
            log_utils.emit_event = original_emit
        _assert(emitted == [],
                f"prepare must emit nothing — emission belongs to the "
                f"post-commit emitter, got {emitted}")
        _assert(conn.in_transaction,
                "prepare must leave the caller's transaction open, not commit it")
        row = other.execute(
            "SELECT 1 FROM edges WHERE src = ? AND dst = ?", (a, b)
        ).fetchone()
        _assert(row is None,
                "prepared edge must not be visible before the caller commits")
        _assert(len(events) == 1,
                f"supersedes must prepare exactly one reconciliation envelope: {events}")
        env = events[0]
        _assert(env["stream"] == "reconciliation", env)
        _assert("elapsed_ms" not in env["payload"],
                f"envelope must not carry elapsed_ms pre-emission: {env['payload']}")
        _assert(env["payload"]["src_status_before"] == "staging", env["payload"])
        conn.rollback()
        _assert(not heal.edge_exists_between(conn, a, b),
                "rollback must discard the prepared edge")
        print("PASS prepare_edge_is_transaction_neutral")
    finally:
        other.close()
        _cleanup(tmp, conn)


# ---------- insert_with_heal end-to-end ----------

def test_insert_with_heal_no_match():
    tmp, conn = _fresh_db()
    try:
        out = heal.insert_with_heal(
            conn, kind="fact", title="unique thing",
            body="Nothing like this has been seen before.", use_llm=False,
        )
        _assert(out["heal"] == "none", out)
        _assert(out["matched_id"] is None, out)
        _assert(db.get_node(conn, out["id"]) is not None, "node not inserted")
        print("PASS insert_with_heal_no_match")
    finally:
        _cleanup(tmp, conn)


def test_insert_with_heal_no_llm_keeps_both_on_match():
    tmp, conn = _fresh_db()
    try:
        heal.insert_with_heal(
            conn, kind="fact", title="deploy uses docker compose",
            body="The deploy pipeline is docker compose up -d.",
            use_llm=False,
        )
        out = heal.insert_with_heal(
            conn, kind="fact", title="deployment docker compose",
            body="Our deploy uses docker compose up.",
            use_llm=False, threshold=0.5,  # sentence-transformers sim on rephrase ~0.7-0.9
        )
        _assert(out["heal"] == "keep_both", out)
        _assert(out["matched_id"] is not None, out)
        _assert(out["arbitrator"] is None, "arbitrator should not run when use_llm=False")
        # Both nodes should still be non-stale, with a related_to edge.
        matched = db.get_node(conn, out["matched_id"])
        _assert(matched["status"] != "stale", matched["status"])
        edges = db.neighbors(conn, out["id"])
        rels = [e["relation"] for e in edges]
        _assert("related_to" in rels, f"expected related_to edge, got {rels}")
        print(f"PASS insert_with_heal_no_llm_keeps_both_on_match (sim={out['similarity']:.3f})")
    finally:
        _cleanup(tmp, conn)


def test_cross_lane_duplicate_signal_stamps_substrate_version(monkeypatch):
    tmp, conn = _fresh_db()
    events = []
    try:
        left = db.insert_node(
            conn,
            kind="workstream",
            title="Left lane",
            body="Objective: left.",
            status="canonical",
        )
        right = db.insert_node(
            conn,
            kind="workstream",
            title="Right lane",
            body="Objective: right.",
            status="canonical",
        )
        heal.insert_with_heal(
            conn,
            kind="fact",
            title="deploy uses docker compose",
            body="The deploy pipeline is docker compose up -d.",
            workstream_id=left,
            use_llm=False,
        )
        monkeypatch.setattr(
            heal.log_utils,
            "emit_event",
            lambda channel, payload, **kwargs: events.append((channel, payload)),
        )
        heal.insert_with_heal(
            conn,
            kind="fact",
            title="deployment uses docker compose",
            body="The deploy pipeline runs docker compose up.",
            workstream_id=right,
            threshold=0.0,
            use_llm=False,
        )
        lifecycle = [payload for channel, payload in events if channel == "lifecycle"]
        _assert(len(lifecycle) == 1, lifecycle)
        _assert(
            lifecycle[0]["substrate_version"]
            == heal.lifecycle_signals.SUBSTRATE_VERSION,
            lifecycle[0],
        )
    finally:
        _cleanup(tmp, conn)


def test_insert_with_heal_respects_kind_filter():
    """A fact and a preference with identical text should not collide — different kinds."""
    tmp, conn = _fresh_db()
    try:
        heal.insert_with_heal(
            conn, kind="fact", title="python is used",
            body="Project uses Python 3.11.", use_llm=False,
        )
        out = heal.insert_with_heal(
            conn, kind="preference", title="python is used",
            body="Project uses Python 3.11.", use_llm=False, threshold=0.5,
        )
        _assert(out["heal"] == "none",
                f"cross-kind match should not heal: {out}")
        print("PASS insert_with_heal_respects_kind_filter")
    finally:
        _cleanup(tmp, conn)


def test_arbitration_runs_before_any_write():
    """DECIDE before PREPARE (5648 item 3): the LLM arbitration — whose model
    subprocess can run up to ARBITRATE_TIMEOUT_S — must complete before
    insert_with_heal's first DB write. With the wrapper holding one
    transaction, arbitration below the first write would pin a RESERVED lock
    30x past the 5000ms busy timeout."""
    tmp, conn = _fresh_db()
    other = db.connect(tmp)
    observed = {}
    original_arbitrate = heal.arbitrate

    def _spy(new, old, sim, **kw):
        observed["in_transaction"] = conn.in_transaction
        observed["local_rows"] = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE title = ?", ("arbitrated twin",)
        ).fetchone()[0]
        observed["visible_rows"] = other.execute(
            "SELECT COUNT(*) FROM nodes WHERE title = ?", ("arbitrated twin",)
        ).fetchone()[0]
        return {"decision": "keep_both", "reason": "spy"}

    heal.arbitrate = _spy
    try:
        heal.insert_with_heal(
            conn, kind="fact", title="seed for arbitration",
            body="docker compose up -d", use_llm=False,
        )
        out = heal.insert_with_heal(
            conn, kind="fact", title="arbitrated twin",
            body="docker compose up", use_llm=True, threshold=0.5,
        )
        _assert(out["heal"] == "keep_both", out)
        _assert(observed, "arbitrator spy was never invoked")
        _assert(observed["local_rows"] == 0 and observed["visible_rows"] == 0,
                f"arbitration must run before the node insert, got {observed}")
        _assert(observed["in_transaction"] is False,
                f"the DECIDE phase must hold no transaction, got {observed}")
    finally:
        heal.arbitrate = original_arbitrate
        other.close()
        _cleanup(tmp, conn)
    print("PASS arbitration_runs_before_any_write")


def test_insert_with_heal_commits_for_non_transactional_callers():
    """Pin for the committing-wrapper contract (5648 item 3, gate amendment
    2483): driven exactly as compactor._apply_compaction and seed drive it —
    use_llm=False, no caller transaction, no caller commit — the node must be
    visible on an independent connection the moment the call returns, with no
    transaction left open. This is the pin that catches the silent-data-loss
    failure mode of a transaction-neutral insert_with_heal: the compactor
    never commits at its own level (_run_compaction_locked ends
    `finally: conn.close()`)."""
    tmp, conn = _fresh_db()
    other = db.connect(tmp)
    try:
        out = heal.insert_with_heal(
            conn, kind="fact", title="compactor-shaped insert",
            body="extracted by an offline maintenance pass", status="staging",
            session_id="sess-compactor", use_llm=False,
        )
        _assert(not conn.in_transaction,
                "insert_with_heal must commit before returning")
        row = other.execute(
            "SELECT status FROM nodes WHERE id = ?", (out["id"],)
        ).fetchone()
        _assert(row is not None and row["status"] == "staging",
                f"independent connection must see the committed node: {row}")
        print("PASS insert_with_heal_commits_for_non_transactional_callers")
    finally:
        other.close()
        _cleanup(tmp, conn)


# ---------- plan_freshness_hint ----------

def test_plan_freshness_hint_fires_on_progress_with_plan_link():
    """progress node + implements/advances/depends_on edge to a plan-shaped
    node → hint surfaces the linked plan."""
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("plan body")
        plan_id = db.insert_node(
            conn, kind="progress", title="5-step sequence plan",
            body="Steps 1-5 locked, implementation not yet started.",
            embedding=embeddings.to_blob(v),
        )
        out = heal.insert_with_heal(
            conn, kind="progress", title="Step 1 shipped",
            body="Step 1 of the sequence shipped on commit abc123.",
            use_llm=False,
            links=[{"dst": plan_id, "relation": "implements"}],
        )
        hint = out.get("plan_freshness_hint")
        _assert(hint is not None, f"hint missing from return: {out}")
        _assert(len(hint) == 1, f"expected 1 hint, got {hint}")
        _assert(hint[0]["linked_id"] == plan_id, hint[0])
        _assert(hint[0]["relation"] == "implements", hint[0])
        _assert(hint[0]["kind"] == "progress", hint[0])
        _assert("plan" in hint[0]["title"].lower(), hint[0])
        print("PASS plan_freshness_hint_fires_on_progress_with_plan_link")
    finally:
        _cleanup(tmp, conn)


def test_plan_freshness_hint_empty_for_related_to_relation():
    """related_to is not a plan-link relation — no hint."""
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("plan body")
        plan_id = db.insert_node(
            conn, kind="progress", title="some plan",
            body="b", embedding=embeddings.to_blob(v),
        )
        out = heal.insert_with_heal(
            conn, kind="progress", title="some ship",
            body="b2", use_llm=False,
            links=[{"dst": plan_id, "relation": "related_to"}],
        )
        _assert(out["plan_freshness_hint"] == [],
                f"unexpected hint: {out['plan_freshness_hint']}")
        print("PASS plan_freshness_hint_empty_for_related_to_relation")
    finally:
        _cleanup(tmp, conn)


def test_plan_freshness_hint_empty_for_non_progress_source():
    """fact node linking via implements should NOT trigger — mandate is
    scoped to ship-progress inserts."""
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("plan body")
        plan_id = db.insert_node(
            conn, kind="progress", title="some plan",
            body="b", embedding=embeddings.to_blob(v),
        )
        out = heal.insert_with_heal(
            conn, kind="fact", title="a finding",
            body="some observation",
            use_llm=False,
            links=[{"dst": plan_id, "relation": "implements"}],
        )
        _assert(out["plan_freshness_hint"] == [],
                f"unexpected hint from fact: {out['plan_freshness_hint']}")
        print("PASS plan_freshness_hint_empty_for_non_progress_source")
    finally:
        _cleanup(tmp, conn)


def test_plan_freshness_hint_skips_stale_targets():
    """Already-stale plan nodes don't need freshening — no hint."""
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("plan body")
        plan_id = db.insert_node(
            conn, kind="progress", title="stale plan",
            body="b", embedding=embeddings.to_blob(v),
        )
        db.update_node(conn, plan_id, status="stale")
        out = heal.insert_with_heal(
            conn, kind="progress", title="a ship",
            body="ship body", use_llm=False,
            links=[{"dst": plan_id, "relation": "implements"}],
        )
        _assert(out["plan_freshness_hint"] == [],
                f"stale plan leaked into hint: {out['plan_freshness_hint']}")
        print("PASS plan_freshness_hint_skips_stale_targets")
    finally:
        _cleanup(tmp, conn)


def test_plan_freshness_hint_skips_non_plan_targets():
    """Linking to a fact via implements doesn't trigger — only plan-shaped
    kinds (progress, decision, workstream) count."""
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("body")
        fact_id = db.insert_node(
            conn, kind="fact", title="some fact",
            body="b", embedding=embeddings.to_blob(v),
        )
        out = heal.insert_with_heal(
            conn, kind="progress", title="a ship",
            body="b2", use_llm=False,
            links=[{"dst": fact_id, "relation": "implements"}],
        )
        _assert(out["plan_freshness_hint"] == [],
                f"fact target leaked into hint: {out['plan_freshness_hint']}")
        print("PASS plan_freshness_hint_skips_non_plan_targets")
    finally:
        _cleanup(tmp, conn)


def test_plan_freshness_hint_multi_links_and_all_relations():
    """Decision + workstream targets across implements/advances/depends_on all
    surface in the hint."""
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("body")
        dec_id = db.insert_node(
            conn, kind="decision", title="some decision",
            body="b", embedding=embeddings.to_blob(v),
        )
        ws_id = db.insert_node(
            conn, kind="workstream", title="some workstream",
            body="b", embedding=embeddings.to_blob(v),
        )
        prog_id = db.insert_node(
            conn, kind="progress", title="some plan",
            body="b", embedding=embeddings.to_blob(v),
        )
        out = heal.insert_with_heal(
            conn, kind="progress", title="multi ship",
            body="b2", use_llm=False,
            links=[
                {"dst": dec_id, "relation": "advances"},
                {"dst": ws_id, "relation": "depends_on"},
                {"dst": prog_id, "relation": "implements"},
            ],
        )
        hint = out["plan_freshness_hint"]
        _assert(len(hint) == 3, f"expected 3 hints, got {hint}")
        kinds = {h["kind"] for h in hint}
        _assert(kinds == {"decision", "workstream", "progress"}, kinds)
        relations = {h["relation"] for h in hint}
        _assert(relations == {"advances", "depends_on", "implements"}, relations)
        print("PASS plan_freshness_hint_multi_links_and_all_relations")
    finally:
        _cleanup(tmp, conn)


# ---------- reconciliation_banner ----------

def test_reconciliation_banner_empty_when_no_edges():
    """Plain canonical node with no reconciled_by edges → empty banner."""
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("isolated fact")
        nid = db.insert_node(
            conn, kind="fact", title="some isolated fact",
            body="x", embedding=embeddings.to_blob(v),
        )
        banner = db.reconciliation_banner(conn, nid)
        _assert(banner == [], f"expected empty banner, got {banner}")
        print("PASS reconciliation_banner_empty_when_no_edges")
    finally:
        _cleanup(tmp, conn)


def test_reconciliation_banner_surfaces_outgoing_reconciled_by():
    """Outgoing reconciled_by from old → new puts new in banner of old."""
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("body")
        old = db.insert_node(
            conn, kind="fact", title="old framing fact",
            body="rolling 20-day window of historical fits",
            embedding=embeddings.to_blob(v),
        )
        new = db.insert_node(
            conn, kind="fact", title="HFT time scales fact",
            body="cross-product covariance: minutes-to-hours",
            embedding=embeddings.to_blob(v),
        )
        db.add_edge(conn, src=old, dst=new, relation="reconciled_by")
        banner = db.reconciliation_banner(conn, old)
        _assert(len(banner) == 1, f"expected 1 banner entry, got {banner}")
        _assert(banner[0]["linked_id"] == new, banner[0])
        _assert("HFT" in banner[0]["title"], banner[0])
        # Reverse direction: kb_get on the NEW node should NOT surface the
        # banner (the new node is the reconciler, not the reconciled).
        banner_new = db.reconciliation_banner(conn, new)
        _assert(banner_new == [], f"new shouldn't have banner: {banner_new}")
        print("PASS reconciliation_banner_surfaces_outgoing_reconciled_by")
    finally:
        _cleanup(tmp, conn)


def test_reconciliation_banner_excludes_stale_reconciler():
    """If the reconciling node has been marked stale, drop it from the
    banner — the reconciler itself has been replaced or invalidated."""
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("body")
        old = db.insert_node(
            conn, kind="fact", title="old framing",
            body="x", embedding=embeddings.to_blob(v),
        )
        new = db.insert_node(
            conn, kind="fact", title="stale reconciler",
            body="x", embedding=embeddings.to_blob(v),
        )
        db.add_edge(conn, src=old, dst=new, relation="reconciled_by")
        db.update_node(conn, new, status="stale")
        banner = db.reconciliation_banner(conn, old)
        _assert(banner == [], f"stale reconciler leaked: {banner}")
        print("PASS reconciliation_banner_excludes_stale_reconciler")
    finally:
        _cleanup(tmp, conn)


def test_reconciliation_banner_multiple_reconcilers():
    """A node can be reconciled by multiple newer nodes — all surface."""
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("body")
        old = db.insert_node(
            conn, kind="fact", title="contested framing",
            body="x", embedding=embeddings.to_blob(v),
        )
        new1 = db.insert_node(
            conn, kind="fact", title="reconciler one",
            body="x", embedding=embeddings.to_blob(v),
        )
        new2 = db.insert_node(
            conn, kind="decision", title="reconciler two",
            body="x", embedding=embeddings.to_blob(v),
        )
        db.add_edge(conn, src=old, dst=new1, relation="reconciled_by")
        db.add_edge(conn, src=old, dst=new2, relation="reconciled_by")
        banner = db.reconciliation_banner(conn, old)
        _assert(len(banner) == 2, f"expected 2, got {banner}")
        ids = {entry["linked_id"] for entry in banner}
        _assert(ids == {new1, new2}, ids)
        print("PASS reconciliation_banner_multiple_reconcilers")
    finally:
        _cleanup(tmp, conn)


def test_reconciliation_banner_ignores_other_relations():
    """Only reconciled_by triggers the banner — supersedes, related_to,
    constrains, depends_on must not appear."""
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("body")
        old = db.insert_node(
            conn, kind="fact", title="some node",
            body="x", embedding=embeddings.to_blob(v),
        )
        other = db.insert_node(
            conn, kind="fact", title="other node",
            body="x", embedding=embeddings.to_blob(v),
        )
        for rel in ("supersedes", "related_to", "constrains", "depends_on"):
            db.add_edge(conn, src=old, dst=other, relation=rel)
        banner = db.reconciliation_banner(conn, old)
        _assert(banner == [], f"non-reconciled_by leaked: {banner}")
        print("PASS reconciliation_banner_ignores_other_relations")
    finally:
        _cleanup(tmp, conn)


# ---------- arbitrator short-circuit + output parsing ----------

def test_arbitrate_short_circuits_in_compact(monkeypatch=None):
    """When running inside a compactor-spawned session, arbitrate must not
    spawn a model subprocess — it returns keep_both immediately."""
    import os
    os.environ["CLAUDE_KB_IN_COMPACT"] = "1"
    try:
        out = heal.arbitrate(
            {"kind": "fact", "title": "x", "body": "y"},
            {"id": 1, "kind": "fact", "title": "x2", "body": "y2",
             "created_at": "t", "updated_at": "t"},
            similarity=0.9,
        )
        _assert(out["decision"] == "keep_both", out)
        _assert("disabled/in-compact" in out["reason"], out)
        print("PASS arbitrate_short_circuits_in_compact")
    finally:
        del os.environ["CLAUDE_KB_IN_COMPACT"]


def test_parse_arbitrate_clean_json():
    raw = '{"decision": "supersede", "reason": "new is strictly better"}'
    out = heal._parse_arbitrate_output(raw)
    _assert(out["decision"] == "supersede", out)
    _assert("strictly better" in out["reason"], out)
    print("PASS parse_arbitrate_clean_json")


def test_parse_arbitrate_envelope():
    inner = {"decision": "keep_both", "reason": "different angles"}
    env = json.dumps({"type": "result", "result": json.dumps(inner)})
    out = heal._parse_arbitrate_output(env)
    _assert(out["decision"] == "keep_both", out)
    print("PASS parse_arbitrate_envelope")


def test_parse_arbitrate_fenced():
    raw = '```json\n{"decision":"supersede","reason":"dupe"}\n```'
    out = heal._parse_arbitrate_output(raw)
    _assert(out["decision"] == "supersede", out)
    print("PASS parse_arbitrate_fenced")


def test_parse_arbitrate_unknown_decision_defaults_to_keep_both():
    raw = '{"decision": "nuke_it", "reason": "lol"}'
    out = heal._parse_arbitrate_output(raw)
    _assert(out["decision"] == "keep_both", out)
    _assert("unknown decision" in out["reason"], out)
    print("PASS parse_arbitrate_unknown_decision_defaults_to_keep_both")


def test_parse_arbitrate_garbage_defaults_to_keep_both():
    out = heal._parse_arbitrate_output("lol what json")
    _assert(out["decision"] == "keep_both", out)
    print("PASS parse_arbitrate_garbage_defaults_to_keep_both")


def test_parse_arbitrate_empty_defaults_to_keep_both():
    out = heal._parse_arbitrate_output("")
    _assert(out["decision"] == "keep_both", out)
    _assert("empty" in out["reason"], out)
    print("PASS parse_arbitrate_empty_defaults_to_keep_both")


# ---------- nightly arbitrator (symmetric A/B, four-verb) ----------

def test_parse_arbitrate_nightly_supersede_a():
    raw = '{"decision": "supersede_a", "reason": "A is strictly better"}'
    out = heal._parse_arbitrate_nightly_output(raw)
    _assert(out["decision"] == "supersede_a", out)
    _assert("strictly" in out["reason"], out)
    print("PASS parse_arbitrate_nightly_supersede_a")


def test_parse_arbitrate_nightly_supersede_b():
    raw = '{"decision": "supersede_b", "reason": "B replaces A entirely"}'
    out = heal._parse_arbitrate_nightly_output(raw)
    _assert(out["decision"] == "supersede_b", out)
    print("PASS parse_arbitrate_nightly_supersede_b")


def test_parse_arbitrate_nightly_keep_both():
    raw = '{"decision": "keep_both", "reason": "distinct angles"}'
    out = heal._parse_arbitrate_nightly_output(raw)
    _assert(out["decision"] == "keep_both", out)
    print("PASS parse_arbitrate_nightly_keep_both")


def test_parse_arbitrate_nightly_reconciled_by():
    raw = '{"decision": "reconciled_by", "reason": "B constrains A scope"}'
    out = heal._parse_arbitrate_nightly_output(raw)
    _assert(out["decision"] == "reconciled_by", out)
    _assert("constrain" in out["reason"], out)
    print("PASS parse_arbitrate_nightly_reconciled_by")


def test_parse_arbitrate_nightly_legacy_supersede_maps_to_b():
    """Legacy on-insert prompt returned bare 'supersede' meaning new wins.
    Nightly parser maps it to supersede_b so transitional outputs survive."""
    raw = '{"decision": "supersede", "reason": "new wins"}'
    out = heal._parse_arbitrate_nightly_output(raw)
    _assert(out["decision"] == "supersede_b", out)
    print("PASS parse_arbitrate_nightly_legacy_supersede_maps_to_b")


def test_parse_arbitrate_nightly_unknown_defaults_to_keep_both():
    raw = '{"decision": "nuke_it", "reason": "lol"}'
    out = heal._parse_arbitrate_nightly_output(raw)
    _assert(out["decision"] == "keep_both", out)
    _assert("unknown decision" in out["reason"], out)
    print("PASS parse_arbitrate_nightly_unknown_defaults_to_keep_both")


def test_parse_arbitrate_nightly_envelope_and_fenced():
    """Envelope unwrap + ```json fence stripping both work on the four-verb parser."""
    inner = {"decision": "reconciled_by", "reason": "older constrained"}
    env = json.dumps({"type": "result", "result": json.dumps(inner)})
    out = heal._parse_arbitrate_nightly_output(env)
    _assert(out["decision"] == "reconciled_by", out)

    fenced = '```json\n{"decision":"supersede_a","reason":"older wins"}\n```'
    out2 = heal._parse_arbitrate_nightly_output(fenced)
    _assert(out2["decision"] == "supersede_a", out2)
    print("PASS parse_arbitrate_nightly_envelope_and_fenced")


def test_arbitrate_nightly_short_circuits_in_compact():
    """When CLAUDE_KB_IN_COMPACT is set, the nightly arbitrator must not spawn
    a model subprocess — returns keep_both immediately, mirroring on-insert."""
    import os
    os.environ["CLAUDE_KB_IN_COMPACT"] = "1"
    try:
        out = heal._arbitrate_nightly(
            {"id": 1, "kind": "fact", "title": "older", "body": "x",
             "created_at": "t1", "updated_at": "t1"},
            {"id": 2, "kind": "fact", "title": "newer", "body": "y",
             "created_at": "t2", "updated_at": "t2"},
            similarity=0.6,
        )
        _assert(out["decision"] == "keep_both", out)
        _assert("disabled/in-compact" in out["reason"], out)
        print("PASS arbitrate_nightly_short_circuits_in_compact")
    finally:
        del os.environ["CLAUDE_KB_IN_COMPACT"]


# ---------- heal.log emission (KB id=1095) ----------

def _heal_log_path(tmp):
    return log_utils.today_log_path("heal", tmp)


def _read_heal_log_rows(tmp):
    path = _heal_log_path(tmp)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _cleanup_emission_artifacts(tmp):
    """The pytest capability root owns resolved vault teardown."""
    del tmp


def test_heal_log_emit_keep_both_via_no_llm():
    """use_llm=False on a match → one heal.log row with decision=keep_both,
    matched_status_before captured at point-in-time."""
    tmp, conn = _fresh_db()
    try:
        # Seed: first insert finds no candidates; second matches.
        heal.insert_with_heal(
            conn, kind="fact", title="deploy uses docker compose",
            body="The deploy pipeline is docker compose up -d.",
            use_llm=False, project_path=tmp,
        )
        out = heal.insert_with_heal(
            conn, kind="fact", title="deployment docker compose",
            body="Our deploy uses docker compose up.",
            use_llm=False, threshold=0.5, project_path=tmp,
            session_id="sess-test-1",
        )
        rows = _read_heal_log_rows(tmp)
        # Only the second insert emits (first had no candidates).
        _assert(len(rows) == 1, f"expected 1 row, got {len(rows)}: {rows}")
        row = rows[0]
        _assert(row["arbitrator_decision"] == "keep_both", row)
        _assert(row["inserted_node_id"] == out["id"], row)
        _assert(row["matched_id"] == out["matched_id"], row)
        _assert(row["matched_kind"] == "fact", row)
        _assert(row["matched_status_before"] == "staging",
                f"point-in-time status should be pre-supersede: {row}")
        _assert(row["inserted_kind"] == "fact", row)
        _assert(isinstance(row["similarity"], float), row)
        _assert(row["session_id"] == "sess-test-1", row)
        _assert(isinstance(row["elapsed_ms"], int) and row["elapsed_ms"] >= 0, row)
        print("PASS heal_log_emit_keep_both_via_no_llm")
    finally:
        _cleanup_emission_artifacts(tmp)
        _cleanup(tmp, conn)


def test_heal_log_emit_supersede_captures_pre_mutation_status():
    """Forced supersede via patched arbitrator → matched_status_before MUST
    reflect the pre-supersede status, not the post-mutation 'stale'.

    This is the regression guard for KB id=1095's capture-before-mutation rule.
    """
    tmp, conn = _fresh_db()
    original_arbitrate = heal.arbitrate
    # Accept the artifact-evidence kwargs (new_repos/old_repos) the on-insert
    # heal now passes to the arbitrator (evidence contract).
    heal.arbitrate = lambda new, old, sim, **kw: {
        "decision": "supersede", "reason": "forced by test",
    }
    try:
        heal.insert_with_heal(
            conn, kind="fact", title="seed node",
            body="some body that will be superseded",
            use_llm=False, project_path=tmp,
        )
        # Verify seed was inserted at 'staging' status (the default).
        # Second insert triggers the LLM path which our patch returns supersede on.
        out = heal.insert_with_heal(
            conn, kind="fact", title="replacement node",
            body="some body that replaces the first one",
            use_llm=True, threshold=0.5, project_path=tmp,
            session_id="sess-test-2",
        )
        _assert(out["heal"] == "supersede", f"forced supersede expected: {out}")
        # Confirm the matched node IS now stale post-supersede.
        matched = db.get_node(conn, out["matched_id"])
        _assert(matched["status"] == "stale", f"matched should be stale now: {matched}")

        rows = _read_heal_log_rows(tmp)
        _assert(len(rows) == 1, f"expected 1 row, got {len(rows)}: {rows}")
        row = rows[0]
        _assert(row["arbitrator_decision"] == "supersede", row)
        _assert(row["matched_status_before"] == "staging",
                f"matched_status_before MUST be pre-supersede 'staging', "
                f"got {row['matched_status_before']!r} (regression)")
        print("PASS heal_log_emit_supersede_captures_pre_mutation_status")
    finally:
        heal.arbitrate = original_arbitrate
        _cleanup_emission_artifacts(tmp)
        _cleanup(tmp, conn)


def test_heal_log_no_emit_when_no_candidates():
    """First insert into a fresh DB → no match → no heal.log row written."""
    tmp, conn = _fresh_db()
    try:
        out = heal.insert_with_heal(
            conn, kind="fact", title="totally unique node",
            body="No prior content remotely like this.",
            use_llm=False, project_path=tmp,
        )
        _assert(out["heal"] == "none", out)
        rows = _read_heal_log_rows(tmp)
        _assert(rows == [], f"unexpected rows on no-match: {rows}")
        print("PASS heal_log_no_emit_when_no_candidates")
    finally:
        _cleanup_emission_artifacts(tmp)
        _cleanup(tmp, conn)


def test_heal_log_common_header_fields_present():
    """Every emitted row has ts, project, session_id, event_type per id=1091 §2."""
    tmp, conn = _fresh_db()
    try:
        heal.insert_with_heal(
            conn, kind="fact", title="deploy thing",
            body="docker compose up -d", use_llm=False, project_path=tmp,
        )
        heal.insert_with_heal(
            conn, kind="fact", title="deploy similar",
            body="docker compose up", use_llm=False, threshold=0.5,
            project_path=tmp, session_id="sess-test-3",
        )
        rows = _read_heal_log_rows(tmp)
        _assert(len(rows) == 1, f"expected 1 row: {rows}")
        for key in ("ts", "project", "session_id", "event_type"):
            _assert(key in rows[0], f"missing header {key!r}: {rows[0]}")
        _assert(rows[0]["event_type"] == "heal", rows[0])
        print("PASS heal_log_common_header_fields_present")
    finally:
        _cleanup_emission_artifacts(tmp)
        _cleanup(tmp, conn)


def test_heal_log_no_forbidden_fields():
    """Structural-only invariant per id=1091 §3: no titles, bodies, or
    free-form arbitrator reasoning strings end up in heal.log."""
    tmp, conn = _fresh_db()
    try:
        heal.insert_with_heal(
            conn, kind="fact", title="some seed",
            body="seed body content", use_llm=False, project_path=tmp,
        )
        heal.insert_with_heal(
            conn, kind="fact", title="something similar",
            body="similar body content", use_llm=False, threshold=0.5,
            project_path=tmp,
        )
        rows = _read_heal_log_rows(tmp)
        forbidden = {"title", "body", "matched_title", "matched_body",
                     "arbitrator_reason", "reason", "inserted_title",
                     "inserted_body"}
        for row in rows:
            leaked = set(row.keys()) & forbidden
            _assert(leaked == set(),
                    f"forbidden fields leaked: {leaked} in row {row}")
        print("PASS heal_log_no_forbidden_fields")
    finally:
        _cleanup_emission_artifacts(tmp)
        _cleanup(tmp, conn)


def test_heal_log_failure_isolation():
    """Mock Path.open to raise on heal.log write — kb_insert MUST still
    succeed and the node MUST still be persisted to the DB."""
    tmp, conn = _fresh_db()
    heal.insert_with_heal(
        conn, kind="fact", title="seed thing",
        body="docker compose up -d", use_llm=False, project_path=tmp,
    )
    original_open = Path.open
    captured_log_path = log_utils.today_log_path("heal", tmp)

    def _selective_raise(self, *a, **kw):
        if self == captured_log_path:
            raise IOError("simulated disk failure")
        return original_open(self, *a, **kw)

    Path.open = _selective_raise
    try:
        # Must NOT raise even though emission target throws.
        out = heal.insert_with_heal(
            conn, kind="fact", title="another similar",
            body="docker compose up", use_llm=False, threshold=0.5,
            project_path=tmp,
        )
        _assert(out["heal"] == "keep_both", out)
        # The node was still inserted.
        _assert(db.get_node(conn, out["id"]) is not None,
                "node not persisted under log failure")
    finally:
        Path.open = original_open
        _cleanup_emission_artifacts(tmp)
        _cleanup(tmp, conn)
    print("PASS heal_log_failure_isolation")


# ---------- stale exclusion in search / recent ----------

def test_search_excludes_stale_by_default():
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("the quick brown fox jumps over the lazy dog")
        alive = db.insert_node(conn, kind="fact", title="alive",
                               body="the quick brown fox jumps",
                               embedding=embeddings.to_blob(v))
        stale = db.insert_node(conn, kind="fact", title="stale",
                               body="the quick brown fox leaps",
                               embedding=embeddings.to_blob(v))
        db.update_node(conn, stale, status="stale")
        results = searchmod.hybrid_search(conn, "quick brown fox", limit=10)
        ids = [r["id"] for r in results]
        _assert(alive in ids, f"alive missing: {ids}")
        _assert(stale not in ids, f"stale should be filtered out: {ids}")
        # include_stale=True surfaces both
        results_all = searchmod.hybrid_search(conn, "quick brown fox", limit=10, include_stale=True)
        ids_all = [r["id"] for r in results_all]
        _assert(stale in ids_all, f"stale not surfaced by include_stale: {ids_all}")
        print("PASS search_excludes_stale_by_default")
    finally:
        _cleanup(tmp, conn)


def test_recent_excludes_stale_by_default():
    tmp, conn = _fresh_db()
    try:
        v = embeddings.embed("x")
        n1 = db.insert_node(conn, kind="fact", title="n1", body="n1",
                            embedding=embeddings.to_blob(v))
        n2 = db.insert_node(conn, kind="fact", title="n2", body="n2",
                            embedding=embeddings.to_blob(v))
        db.update_node(conn, n2, status="stale")
        default = [r["id"] for r in db.recent_nodes(conn)]
        _assert(n1 in default and n2 not in default,
                f"default should exclude stale: {default}")
        explicit = [r["id"] for r in db.recent_nodes(conn, status="stale")]
        _assert(explicit == [n2], f"explicit status=stale should return only stale: {explicit}")
        audit = [r["id"] for r in db.recent_nodes(conn, include_stale=True)]
        _assert(n1 in audit and n2 in audit,
                f"include_stale should surface both: {audit}")
        print("PASS recent_excludes_stale_by_default")
    finally:
        _cleanup(tmp, conn)


if __name__ == "__main__":
    test_find_near_duplicates_hits_similar()
    test_find_near_duplicates_misses_unrelated()
    test_find_near_duplicates_excludes_stale()
    test_find_near_duplicates_excludes_workstreams()
    test_find_near_duplicates_excludes_self()
    test_find_near_duplicates_brute_force_path()
    test_public_vector_search_preserves_workstream_id()
    test_apply_supersede_marks_stale_and_edges()
    test_apply_keep_both_adds_related_edge()
    test_prepare_edge_is_transaction_neutral()
    test_insert_with_heal_no_match()
    test_insert_with_heal_no_llm_keeps_both_on_match()
    test_insert_with_heal_respects_kind_filter()
    test_arbitration_runs_before_any_write()
    test_insert_with_heal_commits_for_non_transactional_callers()
    test_plan_freshness_hint_fires_on_progress_with_plan_link()
    test_plan_freshness_hint_empty_for_related_to_relation()
    test_plan_freshness_hint_empty_for_non_progress_source()
    test_plan_freshness_hint_skips_stale_targets()
    test_plan_freshness_hint_skips_non_plan_targets()
    test_plan_freshness_hint_multi_links_and_all_relations()
    test_reconciliation_banner_empty_when_no_edges()
    test_reconciliation_banner_surfaces_outgoing_reconciled_by()
    test_reconciliation_banner_excludes_stale_reconciler()
    test_reconciliation_banner_multiple_reconcilers()
    test_reconciliation_banner_ignores_other_relations()
    test_arbitrate_short_circuits_in_compact()
    test_parse_arbitrate_clean_json()
    test_parse_arbitrate_envelope()
    test_parse_arbitrate_fenced()
    test_parse_arbitrate_unknown_decision_defaults_to_keep_both()
    test_parse_arbitrate_garbage_defaults_to_keep_both()
    test_parse_arbitrate_empty_defaults_to_keep_both()
    test_parse_arbitrate_nightly_supersede_a()
    test_parse_arbitrate_nightly_supersede_b()
    test_parse_arbitrate_nightly_keep_both()
    test_parse_arbitrate_nightly_reconciled_by()
    test_parse_arbitrate_nightly_legacy_supersede_maps_to_b()
    test_parse_arbitrate_nightly_unknown_defaults_to_keep_both()
    test_parse_arbitrate_nightly_envelope_and_fenced()
    test_arbitrate_nightly_short_circuits_in_compact()
    test_search_excludes_stale_by_default()
    test_recent_excludes_stale_by_default()
    test_heal_log_emit_keep_both_via_no_llm()
    test_heal_log_emit_supersede_captures_pre_mutation_status()
    test_heal_log_no_emit_when_no_candidates()
    test_heal_log_common_header_fields_present()
    test_heal_log_no_forbidden_fields()
    test_heal_log_failure_isolation()
    print("\nAll heal tests pass.")
