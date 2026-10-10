"""Unit tests for Step 7 — nightly heal (integrity + three-pass arbitration).

Deterministic tests only: recency + ref_count paths don't call `claude -p`.
The LLM fallthrough path is exercised with use_llm=False, which skips to
keep_both — that verifies the branch reaches the terminal state without
actually spawning a subprocess.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from latch.store import db  # noqa: E402
from latch.retrieval import embeddings  # noqa: E402
from latch.pipeline import heal  # noqa: E402
from latch.common import log_utils  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_nightly_test_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


def _mk(conn, *, kind="fact", title="t", body="b", status="staging"):
    v = embeddings.embed(f"{title}\n\n{body}")
    return db.insert_node(conn, kind=kind, title=title, body=body,
                          status=status, embedding=embeddings.to_blob(v))


def _set_ts(conn, node_id, *, updated_at=None, created_at=None):
    """Force a timestamp for recency tests. Bypasses update_node which would
    re-stamp updated_at to now()."""
    if updated_at:
        conn.execute("UPDATE nodes SET updated_at = ? WHERE id = ?",
                     (updated_at, node_id))
    if created_at:
        conn.execute("UPDATE nodes SET created_at = ? WHERE id = ?",
                     (created_at, node_id))
    conn.commit()


def _set_ref(conn, node_id, n):
    conn.execute("UPDATE nodes SET ref_count = ? WHERE id = ?", (n, node_id))
    conn.commit()


def _days_ago(d: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=d)).strftime("%Y-%m-%d %H:%M:%S")


# ---------- three_pass_arbitrate ----------

def test_recency_pass_picks_newer_when_diff_large_and_newer_fresh():
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="a")
        b = _mk(conn, title="b")
        _set_ts(conn, a, updated_at=_days_ago(2))     # fresh
        _set_ts(conn, b, updated_at=_days_ago(90))    # stale
        na = db.get_node(conn, a)
        nb = db.get_node(conn, b)
        v = heal.three_pass_arbitrate(na, nb, similarity=0.8, use_llm=False)
        _assert(v["decision"] == "supersede", v)
        _assert(v["path"] == "recency", v)
        _assert(v["winner_id"] == a, v)
        _assert(v["loser_id"] == b, v)
        print("PASS recency_pass_picks_newer_when_diff_large_and_newer_fresh")
    finally:
        _cleanup(tmp, conn)


def test_recency_pass_skips_when_both_stale():
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="a")
        b = _mk(conn, title="b")
        _set_ts(conn, a, updated_at=_days_ago(60))   # both stale
        _set_ts(conn, b, updated_at=_days_ago(120))
        na = db.get_node(conn, a)
        nb = db.get_node(conn, b)
        v = heal.three_pass_arbitrate(na, nb, similarity=0.8, use_llm=False)
        _assert(v["path"] != "recency", f"recency should not fire when both stale: {v}")
        print("PASS recency_pass_skips_when_both_stale")
    finally:
        _cleanup(tmp, conn)


def test_recency_pass_skips_small_age_diff():
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="a")
        b = _mk(conn, title="b")
        _set_ts(conn, a, updated_at=_days_ago(1))
        _set_ts(conn, b, updated_at=_days_ago(10))
        na = db.get_node(conn, a)
        nb = db.get_node(conn, b)
        v = heal.three_pass_arbitrate(na, nb, similarity=0.8, use_llm=False)
        _assert(v["path"] != "recency", v)
        print("PASS recency_pass_skips_small_age_diff")
    finally:
        _cleanup(tmp, conn)


def test_ref_count_pass_picks_dominant():
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="a")
        b = _mk(conn, title="b")
        _set_ts(conn, a, updated_at=_days_ago(5))
        _set_ts(conn, b, updated_at=_days_ago(5))
        _set_ref(conn, a, 9)
        _set_ref(conn, b, 1)
        na = db.get_node(conn, a)
        nb = db.get_node(conn, b)
        v = heal.three_pass_arbitrate(na, nb, similarity=0.8, use_llm=False)
        _assert(v["decision"] == "supersede", v)
        _assert(v["path"] == "ref_count", v)
        _assert(v["winner_id"] == a, v)
        print("PASS ref_count_pass_picks_dominant")
    finally:
        _cleanup(tmp, conn)


def test_ref_count_pass_skips_cold_start():
    """Loser must have been referenced at least once — 3 vs 0 is cold-start, not dominance."""
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="a")
        b = _mk(conn, title="b")
        _set_ts(conn, a, updated_at=_days_ago(5))
        _set_ts(conn, b, updated_at=_days_ago(5))
        _set_ref(conn, a, 9)
        _set_ref(conn, b, 0)
        na = db.get_node(conn, a)
        nb = db.get_node(conn, b)
        v = heal.three_pass_arbitrate(na, nb, similarity=0.8, use_llm=False)
        _assert(v["path"] != "ref_count",
                f"ref_count should skip when loser has 0 refs: {v}")
        print("PASS ref_count_pass_skips_cold_start")
    finally:
        _cleanup(tmp, conn)


def test_ref_count_pass_skips_below_ratio():
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="a")
        b = _mk(conn, title="b")
        _set_ts(conn, a, updated_at=_days_ago(5))
        _set_ts(conn, b, updated_at=_days_ago(5))
        _set_ref(conn, a, 5)
        _set_ref(conn, b, 2)  # ratio 2.5, below 3
        na = db.get_node(conn, a)
        nb = db.get_node(conn, b)
        v = heal.three_pass_arbitrate(na, nb, similarity=0.8, use_llm=False)
        _assert(v["path"] != "ref_count", v)
        print("PASS ref_count_pass_skips_below_ratio")
    finally:
        _cleanup(tmp, conn)


def test_ref_count_pass_skips_cross_kind():
    """Cross-kind pairs (entity vs fact, decision vs progress, etc.) are usually
    complementary facets, not duplicates — ref_count cascade must defer to LLM
    instead of silently superseding the lower-ref side. Regression: 2026-04-29
    nightly run killed a narrow `fact` because a high-ref `entity` umbrella
    crossed the 0.70 sim threshold."""
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, kind="entity", title="vision")
        b = _mk(conn, kind="fact", title="narrow finding")
        _set_ts(conn, a, updated_at=_days_ago(5))
        _set_ts(conn, b, updated_at=_days_ago(5))
        _set_ref(conn, a, 8)
        _set_ref(conn, b, 2)  # ratio 4 — would dominate if same-kind
        na = db.get_node(conn, a)
        nb = db.get_node(conn, b)
        v = heal.three_pass_arbitrate(na, nb, similarity=0.72, use_llm=False)
        _assert(v["path"] != "ref_count",
                f"cross-kind ref_count cascade must skip: {v}")
        print("PASS ref_count_pass_skips_cross_kind")
    finally:
        _cleanup(tmp, conn)


def test_llm_pass_skip_when_use_llm_false():
    """Inconclusive recency + ref_count + use_llm=False -> skip path, keep_both decision."""
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="a")
        b = _mk(conn, title="b")
        _set_ts(conn, a, updated_at=_days_ago(5))
        _set_ts(conn, b, updated_at=_days_ago(5))
        _set_ref(conn, a, 2)
        _set_ref(conn, b, 2)
        na = db.get_node(conn, a)
        nb = db.get_node(conn, b)
        v = heal.three_pass_arbitrate(na, nb, similarity=0.8, use_llm=False)
        _assert(v["decision"] == "keep_both", v)
        _assert(v["path"] == "skip", v)
        print("PASS llm_pass_skip_when_use_llm_false")
    finally:
        _cleanup(tmp, conn)


# ---------- edge_exists_between ----------

def test_edge_exists_between_detects_either_direction():
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="a")
        b = _mk(conn, title="b")
        _assert(not heal.edge_exists_between(conn, a, b), "no edge yet")
        db.add_edge(conn, src=a, dst=b, relation="related_to")
        _assert(heal.edge_exists_between(conn, a, b), "a->b should register")
        _assert(heal.edge_exists_between(conn, b, a), "reverse direction should also register")
        print("PASS edge_exists_between_detects_either_direction")
    finally:
        _cleanup(tmp, conn)


# ---------- apply_nightly_supersede ----------

def test_apply_nightly_supersede_marks_stale_and_links():
    tmp, conn = _fresh_db()
    try:
        w = _mk(conn, title="winner")
        l = _mk(conn, title="loser")
        heal.apply_nightly_supersede(conn, w, l)
        _assert(db.get_node(conn, l)["status"] == "stale", "loser should be stale")
        _assert(db.get_node(conn, w)["status"] == "staging", "winner untouched")
        _assert(heal.edge_exists_between(conn, w, l), "supersedes edge should exist")
        print("PASS apply_nightly_supersede_marks_stale_and_links")
    finally:
        _cleanup(tmp, conn)


# ---------- _order_by_age ----------

def test_nightly_supersede_commit_boundary_unchanged():
    """apply_nightly_supersede is a self-committing integrity path (priority
    2483: internal integrity/maintenance paths are out of the simplification
    surface): after it returns, an INDEPENDENT connection must already see the
    staled loser and the inherited edges — the nightly pass never commits at a
    higher level."""
    tmp, conn = _fresh_db()
    other = db.connect(tmp)
    try:
        w = _mk(conn, title="winner", body="w")
        l = _mk(conn, title="loser", body="l")
        a = _mk(conn, title="anchor", body="a")
        db.add_edge(conn, src=l, dst=a, relation="implements")

        heal.apply_nightly_supersede(conn, w, l)

        _assert(not conn.in_transaction,
                "nightly supersede must not leave a transaction open")
        row = other.execute(
            "SELECT status FROM nodes WHERE id = ?", (l,)
        ).fetchone()
        _assert(row["status"] == "stale",
                f"independent connection must see the staled loser, got {row['status']!r}")
        inherited = other.execute(
            "SELECT status FROM edges WHERE src = ? AND dst = ? "
            "AND relation = 'implements'",
            (w, a),
        ).fetchone()
        _assert(inherited is not None and inherited["status"] == "active",
                f"independent connection must see the inherited edge: {inherited}")
        audit = other.execute(
            "SELECT status FROM edges WHERE src = ? AND dst = ? "
            "AND relation = 'supersedes'",
            (w, l),
        ).fetchone()
        _assert(audit is not None and audit["status"] == "active",
                f"independent connection must see the supersedes audit edge: {audit}")
        print("PASS nightly_supersede_commit_boundary_unchanged")
    finally:
        other.close()
        _cleanup(tmp, conn)


def test_order_by_age_uses_updated_at():
    """Older = smaller updated_at."""
    older = {"id": 10, "updated_at": "2026-01-01 00:00:00",
             "created_at": "2026-01-01 00:00:00"}
    newer = {"id": 20, "updated_at": "2026-05-01 00:00:00",
             "created_at": "2026-05-01 00:00:00"}
    # Pass in either order — function normalizes.
    a, b = heal._order_by_age(older, newer)
    _assert(a is older and b is newer, (a, b))
    a, b = heal._order_by_age(newer, older)
    _assert(a is older and b is newer, (a, b))
    print("PASS order_by_age_uses_updated_at")


def test_order_by_age_falls_back_to_id():
    """When timestamps are missing or equal, smaller id = older."""
    a = {"id": 5}
    b = {"id": 99}
    older, newer = heal._order_by_age(a, b)
    _assert(older is a and newer is b, (older, newer))
    # Same timestamp, different id — id breaks the tie.
    a2 = {"id": 5, "updated_at": "2026-01-01 00:00:00"}
    b2 = {"id": 99, "updated_at": "2026-01-01 00:00:00"}
    older, newer = heal._order_by_age(a2, b2)
    _assert(older is a2 and newer is b2, (older, newer))
    print("PASS order_by_age_falls_back_to_id")


# ---------- apply_nightly_reconciled_by ----------

def test_apply_nightly_reconciled_by_adds_edge_and_keeps_canonical():
    """Edge older -> newer with reconciled_by; neither node marked stale.
    Both stay canonical — distinct from supersede semantics."""
    tmp, conn = _fresh_db()
    try:
        older = _mk(conn, title="older framing", body="rolling 20-day window")
        newer = _mk(conn, title="newer constraint",
                    body="5-10 days max, minutes-to-hours scale")
        # Pre-condition: both staging, no edges.
        _assert(db.get_node(conn, older)["status"] == "staging", "older pre")
        _assert(db.get_node(conn, newer)["status"] == "staging", "newer pre")

        heal.apply_nightly_reconciled_by(conn, older, newer)

        # Both still non-stale.
        _assert(db.get_node(conn, older)["status"] != "stale", "older must NOT be stale")
        _assert(db.get_node(conn, newer)["status"] != "stale", "newer must NOT be stale")
        # Edge: older -> newer with relation 'reconciled_by'.
        banner = db.reconciliation_banner(conn, older)
        _assert(len(banner) == 1, banner)
        _assert(banner[0]["linked_id"] == newer, banner[0])
        # Reverse direction (kb_get of newer) must NOT surface the banner.
        banner_rev = db.reconciliation_banner(conn, newer)
        _assert(banner_rev == [], banner_rev)
        print("PASS apply_nightly_reconciled_by_adds_edge_and_keeps_canonical")
    finally:
        _cleanup(tmp, conn)


# ---------- tier dispatch ----------

def test_three_pass_low_tier_skips_recency_and_ref_count():
    """Low tier must bypass deterministic passes — recency/ref_count signals
    don't apply to reconciliation candidates. With use_llm=False, returns
    keep_both via the `skip` path immediately."""
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="a")
        b = _mk(conn, title="b")
        # Set up conditions that WOULD trigger recency (90d diff, newer fresh)
        # and ref_count (9:1 ratio) — but tier=low must ignore them.
        _set_ts(conn, a, updated_at=_days_ago(2))
        _set_ts(conn, b, updated_at=_days_ago(90))
        _set_ref(conn, a, 9)
        _set_ref(conn, b, 1)
        na = db.get_node(conn, a)
        nb = db.get_node(conn, b)
        v = heal.three_pass_arbitrate(na, nb, similarity=0.55,
                                      use_llm=False, tier="low")
        _assert(v["decision"] == "keep_both", v)
        _assert(v["path"] == "skip", v)
        _assert(v["tier"] == "low", v)
        # Verify high tier with the same inputs DOES fire recency (regression).
        v_high = heal.three_pass_arbitrate(na, nb, similarity=0.75,
                                           use_llm=False, tier="high")
        _assert(v_high["path"] == "recency", v_high)
        print("PASS three_pass_low_tier_skips_recency_and_ref_count")
    finally:
        _cleanup(tmp, conn)


def test_three_pass_high_tier_default_preserves_behavior():
    """Regression: default tier='high' + same args as old tests returns the
    same shape (decision, path, winner_id, loser_id) — older tests don't
    break."""
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="a")
        b = _mk(conn, title="b")
        _set_ts(conn, a, updated_at=_days_ago(2))
        _set_ts(conn, b, updated_at=_days_ago(90))
        na = db.get_node(conn, a)
        nb = db.get_node(conn, b)
        # No tier kwarg — should default to high.
        v = heal.three_pass_arbitrate(na, nb, similarity=0.8, use_llm=False)
        _assert(v["decision"] == "supersede", v)
        _assert(v["path"] == "recency", v)
        _assert(v["tier"] == "high", v)
        _assert(v["winner_id"] == a, v)
        _assert(v["loser_id"] == b, v)
        print("PASS three_pass_high_tier_default_preserves_behavior")
    finally:
        _cleanup(tmp, conn)


# ---------- nightly_heal two-tier summary + dispatch ----------

def test_nightly_heal_summary_has_reconciled_and_by_tier_keys():
    """Summary schema exposes reconciliation, tier, and deferral counts."""
    tmp, conn = _fresh_db()
    try:
        result = heal.nightly_heal(conn, project_path=tmp, use_llm=False,
                                   contradictions=False)
        _assert("reconciled" in result, f"missing 'reconciled' key: {result}")
        _assert("by_tier" in result, f"missing 'by_tier' key: {result}")
        _assert("high" in result["by_tier"] and "low" in result["by_tier"], result["by_tier"])
        _assert(result["deferred"] == 0, f"unexpected deferral: {result}")
        _assert(result["deferred_by_tier"] == {"high": 0, "low": 0}, result)
        _assert(result["by_path"]["deferred"] == 0, result)
        print("PASS nightly_heal_summary_has_reconciled_and_by_tier_keys")
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_low_tier_keeps_both_when_use_llm_false():
    """Low-tier pair + use_llm=False → must end as keep_both via skip path
    (LLM disabled). Both nodes stay non-stale; related_to edge added."""
    tmp, conn = _fresh_db()
    try:
        # Two nodes with moderate similarity. Use distinct enough text to
        # land in the 0.45-0.70 band; if the embedder puts them higher
        # the test still proves "not stale" but the by_tier assertion would
        # land in 'high'. The decision (keep_both) is what we really check.
        a = _mk(conn, kind="fact", title="cookie recipe",
                body="flour, butter, sugar, chocolate chips, baking soda")
        b = _mk(conn, kind="fact", title="brownie recipe",
                body="flour, butter, sugar, cocoa powder, eggs")
        _set_ts(conn, a, updated_at=_days_ago(5))
        _set_ts(conn, b, updated_at=_days_ago(5))
        result = heal.nightly_heal(
            conn, project_path=tmp, use_llm=False,
            low_threshold=0.30, high_threshold=0.95,  # force low-tier bucket
        )
        # Both nodes must still be alive — keep_both never marks stale.
        _assert(db.get_node(conn, a)["status"] != "stale", "a stale")
        _assert(db.get_node(conn, b)["status"] != "stale", "b stale")
        _assert(result["superseded"] == 0,
                f"no supersedes expected at low tier with use_llm=False: {result}")
        _assert(result["reconciled"] == 0,
                f"no reconciles expected with use_llm=False: {result}")
        print(f"PASS nightly_heal_low_tier_keeps_both_when_use_llm_false "
              f"(by_tier={result['by_tier']})")
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_applies_reconciled_by_when_llm_returns_it():
    """End-to-end: when the (mocked) nightly arbitrator returns reconciled_by
    on a low-tier pair, the apply path adds a reconciled_by edge older->newer
    and both stay canonical."""
    tmp, conn = _fresh_db()
    try:
        older = _mk(conn, kind="fact", title="rolling window framing",
                    body="primary store uses a rolling 20-day retention window")
        newer = _mk(conn, kind="decision", title="hot-path narrows retention window",
                    body="5-10 days max for hot reads; older falls to archive")
        _set_ts(conn, older, updated_at=_days_ago(40))
        _set_ts(conn, newer, updated_at=_days_ago(2))

        # Stub _arbitrate_nightly to return reconciled_by deterministically.
        original = heal._arbitrate_nightly

        def stub(_o, _n, _sim, **kw):  # **kw: tolerate a_repos/b_repos evidence kwargs
            return {"decision": "reconciled_by", "reason": "newer constrains older scope"}

        heal._arbitrate_nightly = stub
        try:
            result = heal.nightly_heal(
                conn, project_path=tmp, use_llm=True,
                low_threshold=0.30, high_threshold=0.95,  # force low-tier path
            )
        finally:
            heal._arbitrate_nightly = original

        _assert(result["reconciled"] >= 1,
                f"expected at least 1 reconciled: {result}")
        _assert(db.get_node(conn, older)["status"] != "stale",
                "older must stay canonical after reconciled_by")
        _assert(db.get_node(conn, newer)["status"] != "stale",
                "newer must stay canonical after reconciled_by")
        banner = db.reconciliation_banner(conn, older)
        _assert(any(b["linked_id"] == newer for b in banner),
                f"reconciled_by edge older->newer missing: {banner}")
        print(f"PASS nightly_heal_applies_reconciled_by_when_llm_returns_it "
              f"(reconciled={result['reconciled']}, by_tier={result['by_tier']})")
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_budget_blocked_defers_without_edge_and_retries():
    """A budget-blocked pair remains edge-free and is retried once approved."""
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, kind="fact", title="similar a",
                body="this is some content about deploys")
        b = _mk(conn, kind="fact", title="similar b",
                body="this is some content about deployments")
        _set_ts(conn, a, updated_at=_days_ago(5))
        _set_ts(conn, b, updated_at=_days_ago(5))
        _set_ref(conn, a, 2)
        _set_ref(conn, b, 2)

        # Pre-fill the heal budget so the sweep's next reservation grants zero.
        from latch.gate import budget
        cap = budget.DEFAULT_HEAL_DAILY_CAP
        for _ in range(cap):
            budget.check_and_record(tmp, category="heal", cap=cap)

        result = heal.nightly_heal(
            conn, project_path=tmp, use_llm=True,
            low_threshold=0.30, high_threshold=0.95,  # force low-tier (always LLM)
        )

        _assert(result["budget_blocked"] >= 1,
                f"expected budget_blocked >= 1: {result}")
        _assert(result["deferred"] == result["budget_blocked"],
                f"every budget block must be an explicit deferral: {result}")
        _assert(result["deferred_by_tier"]["low"] >= 1,
                f"expected a low-tier deferred pair: {result}")
        _assert(result["by_path"]["deferred"] >= 1,
                f"deferred path counter missing: {result}")
        _assert(result["superseded"] == 0, f"no supersede on budget block: {result}")
        _assert(result["reconciled"] == 0, f"no reconcile on budget block: {result}")
        _assert(result["kept_both"] == 0,
                f"budget exhaustion must not adjudicate keep_both: {result}")
        # Both nodes still alive.
        _assert(db.get_node(conn, a)["status"] != "stale", "a stale")
        _assert(db.get_node(conn, b)["status"] != "stale", "b stale")
        _assert(not heal.edge_exists_between(conn, a, b),
                "budget exhaustion must leave the pair edge-free")

        today = datetime.now(timezone.utc).date()
        deferred_rows = list(log_utils.read_log_range(
            "heal_deferred", today, today, tmp,
        ))
        expected_pair = tuple(sorted((a, b)))
        matching = [
            row for row in deferred_rows
            if (row.get("node_a_id"), row.get("node_b_id")) == expected_pair
        ]
        _assert(matching, f"missing structural heal_deferred row: {deferred_rows}")
        _assert(matching[-1]["tier"] == "low"
                and matching[-1]["reason"] == "budget_cap"
                and matching[-1]["retry_eligible"] is True,
                f"incomplete deferred-pair provenance: {matching[-1]}")

        # Approval makes budget available. With no edge from the first run, the
        # exact pair must be rediscovered and receive a real arbitration verdict.
        budget.approve_today(tmp)
        calls: list[tuple[int, int]] = []
        original_arb = heal._arbitrate_nightly

        def stub_arb(a_node, b_node, _sim, **kw):
            calls.append(tuple(sorted((a_node["id"], b_node["id"]))))
            return {"decision": "keep_both", "reason": "retry adjudicated"}

        heal._arbitrate_nightly = stub_arb
        try:
            retried = heal.nightly_heal(
                conn, project_path=tmp, use_llm=True,
                low_threshold=0.30, high_threshold=0.95,
            )
        finally:
            heal._arbitrate_nightly = original_arb

        _assert(expected_pair in calls,
                f"deferred pair was not retried after budget approval: {calls}")
        _assert(retried["llm_invocations"] >= 1 and retried["kept_both"] >= 1,
                f"retry did not receive a real verdict: {retried}")
        _assert(heal.edge_exists_between(conn, a, b),
                "real retry verdict should persist its relationship")
        print(f"PASS nightly_heal_budget_blocked_defers_without_edge_and_retries "
              f"(budget_blocked={result['budget_blocked']}, "
              f"retried_calls={retried['llm_invocations']})")
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_budget_status_error_defers_and_runs_maintenance():
    """A budget-state I/O failure must fail closed for LLM work while allowing
    deterministic arbitration and every trailing maintenance stage."""
    from latch.gate import budget

    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="budget error a", body="shared budget error claim")
        b = _mk(conn, title="budget error b", body="shared budget error claim")
        for node_id in (a, b):
            _set_ts(conn, node_id, updated_at=_days_ago(5))
            _set_ref(conn, node_id, 2)

        original_find = heal.find_near_duplicates
        original_status = budget.status
        original_check = budget.check_and_record
        original_reserve = budget.reserve_available
        status_calls = 0
        check_calls = 0
        reserve_calls = 0

        def fake_find(_conn, _vec, *, exclude_id=None, **_):
            if exclude_id == a:
                return [{"id": b, "similarity": 0.60, "kind": "fact",
                         "status": "staging"}]
            return []

        def status_error(*_args, **_kwargs):
            nonlocal status_calls
            status_calls += 1
            raise OSError("budget state locked")

        def check_error(*_args, **_kwargs):
            nonlocal check_calls
            check_calls += 1
            raise OSError("budget state locked")

        def reserve_error(*_args, **_kwargs):
            nonlocal reserve_calls
            reserve_calls += 1
            raise OSError("budget state locked")

        try:
            heal.find_near_duplicates = fake_find
            budget.status = status_error
            budget.check_and_record = check_error
            budget.reserve_available = reserve_error
            result = heal.nightly_heal(
                conn, project_path=tmp, use_llm=True,
                low_threshold=0.50, high_threshold=0.70,
            )
        finally:
            heal.find_near_duplicates = original_find
            budget.status = original_status
            budget.check_and_record = original_check
            budget.reserve_available = original_reserve

        _assert(result["ok"] is True and result["llm_invocations"] == 0,
                f"budget status failure must not abort the sweep: {result}")
        _assert(result["deferred"] == 1 and result["budget_blocked"] == 1,
                f"LLM pair should be safely deferred: {result}")
        _assert(status_calls == 0,
                f"heal must not depend on an advisory status read: {status_calls}")
        _assert(check_calls == 0,
                f"heal should use atomic batch admission: {check_calls}")
        _assert(reserve_calls == 1,
                f"atomic budget reservation should be attempted once: {reserve_calls}")
        _assert(not heal.edge_exists_between(conn, a, b),
                "budget-state failure must leave the pair retryable")
        _assert(db.get_node(conn, a)["status"] != "stale"
                and db.get_node(conn, b)["status"] != "stale",
                "budget-state failure must leave both nodes live")
        for key in ("log_retention", "request_text_retention", "correlator", "drift"):
            _assert(key in result, f"trailing maintenance did not reach {key}: {result}")

        today = datetime.now(timezone.utc).date()
        rows = list(log_utils.read_log_range("heal_deferred", today, today, tmp))
        matching = [row for row in rows
                    if (row.get("node_a_id"), row.get("node_b_id"))
                    == tuple(sorted((a, b)))]
        _assert(matching and matching[-1]["reason"] == "budget_state_error",
                f"budget I/O deferral needs an accurate reason: {matching}")
        print("PASS nightly_heal_budget_status_error_defers_and_runs_maintenance")
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_releases_unused_batch_after_apply_failure():
    """An unexpected early failure consumes only the attempt that started;
    every later slot from the atomic batch must be returned for the next run."""
    from latch.gate import budget

    tmp, conn = _fresh_db()
    original_find = heal.find_near_duplicates
    original_arb = heal._arbitrate_nightly
    original_apply = heal._apply_verdict
    try:
        pairs = []
        for label in ("first", "second", "third"):
            a = _mk(conn, title=f"{label} a", body=f"{label} failure pair")
            b = _mk(conn, title=f"{label} b", body=f"{label} failure pair")
            pairs.append((a, b))
            for node_id in (a, b):
                _set_ref(conn, node_id, 1)
                _set_ts(conn, node_id, updated_at=_days_ago(5))
        by_seed = {
            pairs[0][0]: (pairs[0][1], 0.99),
            pairs[1][0]: (pairs[1][1], 0.95),
            pairs[2][0]: (pairs[2][1], 0.90),
        }
        calls: list[tuple[int, int]] = []

        def fake_find(_conn, _vec, *, exclude_id=None, **_):
            if exclude_id not in by_seed:
                return []
            other, similarity = by_seed[exclude_id]
            return [{"id": other, "similarity": similarity, "kind": "fact",
                     "status": "staging"}]

        def stub_arb(older, newer, _sim, **_):
            calls.append(tuple(sorted((older["id"], newer["id"]))))
            return {"decision": "keep_both", "reason": "test"}

        def fail_apply(*_args, **_kwargs):
            raise RuntimeError("apply failed")

        budget.approve_today(tmp)
        heal.find_near_duplicates = fake_find
        heal._arbitrate_nightly = stub_arb
        heal._apply_verdict = fail_apply
        raised = False
        try:
            heal.nightly_heal(
                conn, project_path=tmp, use_llm=True,
                low_threshold=0.50, high_threshold=0.70,
            )
        except RuntimeError:
            raised = True

        _assert(raised and calls == [tuple(sorted(pairs[0]))],
                f"fixture must fail after one attempted arbitration: {calls}")
        state = budget.status(tmp)
        _assert(state["heal"]["count"] == 1,
                f"unused batch slots leaked after failure: {state}")
        print("PASS nightly_heal_releases_unused_batch_after_apply_failure")
    finally:
        heal.find_near_duplicates = original_find
        heal._arbitrate_nightly = original_arb
        heal._apply_verdict = original_apply
        _cleanup(tmp, conn)


def test_nightly_heal_replans_batch_across_utc_rollover():
    """A pre-charged slot from yesterday cannot authorize today's call; the
    live pair must be atomically reserved again without a false deferral."""
    from latch.gate import budget

    tmp, conn = _fresh_db()
    original_find = heal.find_near_duplicates
    original_arb = heal._arbitrate_nightly
    original_reserve = budget.reserve_available
    original_today = budget._today_iso
    try:
        a = _mk(conn, title="rollover a", body="rollover reservation pair")
        b = _mk(conn, title="rollover b", body="rollover reservation pair")
        for node_id in (a, b):
            _set_ref(conn, node_id, 1)
            _set_ts(conn, node_id, updated_at=_days_ago(5))

        day = {"value": "2026-08-25"}
        reserve_calls = 0
        calls: list[tuple[int, int]] = []

        def fake_find(_conn, _vec, *, exclude_id=None, **_):
            if exclude_id == a:
                return [{"id": b, "similarity": 0.90, "kind": "fact",
                         "status": "staging"}]
            return []

        def rollover_reserve(*args, **kwargs):
            nonlocal reserve_calls
            result = original_reserve(*args, **kwargs)
            reserve_calls += 1
            if reserve_calls == 1:
                day["value"] = "2026-08-26"
            return result

        def stub_arb(older, newer, _sim, **_):
            calls.append(tuple(sorted((older["id"], newer["id"]))))
            return {"decision": "keep_both", "reason": "test"}

        budget._today_iso = lambda: day["value"]
        budget.reserve_available = rollover_reserve
        heal.find_near_duplicates = fake_find
        heal._arbitrate_nightly = stub_arb
        result = heal.nightly_heal(
            conn, project_path=tmp, use_llm=True,
            low_threshold=0.50, high_threshold=0.70,
        )

        _assert(reserve_calls == 2,
                f"rollover must discard and reacquire the old slot: {reserve_calls}")
        _assert(calls == [tuple(sorted((a, b)))],
                f"pair should be invoked exactly once after replanning: {calls}")
        _assert(result["llm_invocations"] == 1
                and result["deferred"] == 0
                and result["budget_blocked"] == 0,
                f"rollover caused a false budget deferral: {result}")
        state = budget.status(tmp)
        _assert(state["date"] == day["value"] and state["heal"]["count"] == 1,
                f"invocation must be charged to the new UTC day: {state}")
        print("PASS nightly_heal_replans_batch_across_utc_rollover")
    finally:
        budget._today_iso = original_today
        budget.reserve_available = original_reserve
        heal.find_near_duplicates = original_find
        heal._arbitrate_nightly = original_arb
        _cleanup(tmp, conn)


def test_nightly_heal_never_merges_reservations_across_utc_days():
    """If midnight falls inside an expansion reservation, old generic slots
    must not widen the new day's priority window."""
    from latch.gate import budget

    tmp, conn = _fresh_db()
    original_find = heal.find_near_duplicates
    original_arb = heal._arbitrate_nightly
    original_reserve = budget.reserve_available
    original_today = budget._today_iso
    original_cap = budget.DEFAULT_HEAL_DAILY_CAP
    try:
        a = _mk(conn, title="epoch a", body="epoch chain claim")
        b = _mk(conn, title="epoch b", body="epoch chain claim")
        e = _mk(conn, title="epoch hot", body="epoch chain claim")
        c = _mk(conn, title="epoch cold c", body="epoch cold claim")
        d = _mk(conn, title="epoch cold d", body="epoch cold claim")
        for node_id, refs in {a: 20, b: 20, e: 20, c: 1, d: 1}.items():
            _set_ref(conn, node_id, refs)
            _set_ts(conn, node_id, updated_at=_days_ago(5))
        hot_priority = heal._pair_priority(db.get_node(conn, a), db.get_node(conn, e))
        cold_priority = heal._pair_priority(db.get_node(conn, c), db.get_node(conn, d))
        _assert(hot_priority > cold_priority,
                f"fixture must rank the downstream pair first: {(hot_priority, cold_priority)}")

        day = {"value": "2026-08-25"}
        reserve_calls = 0
        calls: list[tuple[int, int]] = []

        def fake_find(_conn, _vec, *, exclude_id=None, **_):
            if exclude_id == a:
                return [
                    {"id": b, "similarity": 0.99, "kind": "fact",
                     "status": "staging"},
                    {"id": e, "similarity": 0.80, "kind": "fact",
                     "status": "staging"},
                ]
            if exclude_id == c:
                return [{"id": d, "similarity": 0.90, "kind": "fact",
                         "status": "staging"}]
            return []

        def rollover_on_expansion(*args, **kwargs):
            nonlocal reserve_calls
            reserve_calls += 1
            if reserve_calls == 2:
                day["value"] = "2026-08-26"
            return original_reserve(*args, **kwargs)

        def stub_arb(older, newer, _sim, **_):
            calls.append(tuple(sorted((older["id"], newer["id"]))))
            return {"decision": "keep_both", "reason": "test"}

        budget._today_iso = lambda: day["value"]
        budget.approve_today(tmp)  # Day one is unlimited; day two has cap=1.
        budget.DEFAULT_HEAL_DAILY_CAP = 1
        budget.reserve_available = rollover_on_expansion
        heal.find_near_duplicates = fake_find
        heal._arbitrate_nightly = stub_arb
        result = heal.nightly_heal(
            conn, project_path=tmp, use_llm=True,
            low_threshold=0.50, high_threshold=0.70,
        )

        expected = [tuple(sorted((a, b))), tuple(sorted((a, e)))]
        _assert(calls == expected,
                f"old-day width leaked into the new priority window: {calls}")
        _assert(result["llm_invocations"] == 2
                and result["deferred"] == 1
                and result["budget_blocked"] == 1,
                f"new-day cap was not enforced exactly: {result}")
        state = budget.status(tmp, heal_cap=1)
        _assert(state["date"] == day["value"]
                and state["heal"]["count"] == 1
                and state["heal"]["remaining"] == 0,
                f"new-day invocation accounting drifted: {state}")
        today = datetime.now(timezone.utc).date()
        rows = list(log_utils.read_log_range("heal_deferred", today, today, tmp))
        capped_pairs = {
            (row.get("node_a_id"), row.get("node_b_id"))
            for row in rows if row.get("reason") == "budget_cap"
        }
        _assert(capped_pairs == {tuple(sorted((c, d)))},
                f"only the lower-priority new-day pair may defer: {rows}")
        print("PASS nightly_heal_never_merges_reservations_across_utc_days")
    finally:
        budget._today_iso = original_today
        budget.DEFAULT_HEAL_DAILY_CAP = original_cap
        budget.reserve_available = original_reserve
        heal.find_near_duplicates = original_find
        heal._arbitrate_nightly = original_arb
        _cleanup(tmp, conn)


def test_nightly_heal_transfers_reserved_slot_after_live_invalidation():
    """A pair invalidated while another model call runs must not consume its
    reserved token; the token transfers to the highest-priority live fallback."""
    from latch.gate import budget

    tmp, conn = _fresh_db()
    original_find = heal.find_near_duplicates
    original_arb = heal._arbitrate_nightly
    original_cap = budget.DEFAULT_HEAL_DAILY_CAP
    try:
        first = (
            _mk(conn, title="first a", body="first transfer pair"),
            _mk(conn, title="first b", body="first transfer pair"),
        )
        invalidated = (
            _mk(conn, title="invalidated a", body="invalidated transfer pair"),
            _mk(conn, title="invalidated b", body="invalidated transfer pair"),
        )
        fallback = (
            _mk(conn, title="fallback a", body="fallback transfer pair"),
            _mk(conn, title="fallback b", body="fallback transfer pair"),
        )
        for pair, refs in ((first, 20), (invalidated, 10), (fallback, 1)):
            for node_id in pair:
                _set_ref(conn, node_id, refs)
                _set_ts(conn, node_id, updated_at=_days_ago(5))
        priorities = [
            heal._pair_priority(db.get_node(conn, *pair[:1]), db.get_node(conn, pair[1]))
            for pair in (first, invalidated, fallback)
        ]
        _assert(priorities[0] > priorities[1] > priorities[2],
                f"fixture priority order changed: {priorities}")

        by_seed = {
            first[0]: (first[1], 0.99),
            invalidated[0]: (invalidated[1], 0.95),
            fallback[0]: (fallback[1], 0.90),
        }
        calls: list[tuple[int, int]] = []

        def fake_find(_conn, _vec, *, exclude_id=None, **_):
            if exclude_id not in by_seed:
                return []
            other, similarity = by_seed[exclude_id]
            return [{"id": other, "similarity": similarity, "kind": "fact",
                     "status": "staging"}]

        def stub_arb(older, newer, _sim, **_):
            pair = tuple(sorted((older["id"], newer["id"])))
            calls.append(pair)
            if pair == tuple(sorted(first)):
                conn.execute(
                    "UPDATE nodes SET status = 'stale' WHERE id = ?",
                    (invalidated[0],),
                )
                conn.commit()
            return {"decision": "keep_both", "reason": "test"}

        budget.DEFAULT_HEAL_DAILY_CAP = 2
        heal.find_near_duplicates = fake_find
        heal._arbitrate_nightly = stub_arb
        result = heal.nightly_heal(
            conn, project_path=tmp, use_llm=True,
            low_threshold=0.50, high_threshold=0.70,
        )

        _assert(calls == [tuple(sorted(first)), tuple(sorted(fallback))],
                f"reserved slot did not transfer to the live fallback: {calls}")
        _assert(not heal.edge_exists_between(conn, *invalidated),
                "invalidated pair must remain unmutated")
        _assert(heal.edge_exists_between(conn, *fallback),
                "transferred fallback verdict was not persisted")
        _assert(result["llm_invocations"] == 2
                and result["deferred"] == 0
                and result["budget_blocked"] == 0,
                f"invalidated reservation was falsely deferred: {result}")
        _assert(budget.status(tmp, heal_cap=2)["heal"]["remaining"] == 0,
                "both authoritative reservations should be consumed")
        print("PASS nightly_heal_transfers_reserved_slot_after_live_invalidation")
    finally:
        budget.DEFAULT_HEAL_DAILY_CAP = original_cap
        heal.find_near_duplicates = original_find
        heal._arbitrate_nightly = original_arb
        _cleanup(tmp, conn)


def test_nightly_heal_revalidates_after_budget_reservation():
    """A candidate invalidated while budget I/O is in flight cannot consume
    the returned slot; the highest-priority live fallback receives it."""
    from latch.gate import budget

    tmp, conn = _fresh_db()
    original_find = heal.find_near_duplicates
    original_arb = heal._arbitrate_nightly
    original_reserve = budget.reserve_available
    original_cap = budget.DEFAULT_HEAL_DAILY_CAP
    try:
        selected = (
            _mk(conn, title="selected a", body="reservation race pair"),
            _mk(conn, title="selected b", body="reservation race pair"),
        )
        fallback = (
            _mk(conn, title="fallback a", body="reservation fallback pair"),
            _mk(conn, title="fallback b", body="reservation fallback pair"),
        )
        for pair, refs in ((selected, 10), (fallback, 1)):
            for node_id in pair:
                _set_ref(conn, node_id, refs)
                _set_ts(conn, node_id, updated_at=_days_ago(5))
        selected_priority = heal._pair_priority(
            db.get_node(conn, selected[0]), db.get_node(conn, selected[1]),
        )
        fallback_priority = heal._pair_priority(
            db.get_node(conn, fallback[0]), db.get_node(conn, fallback[1]),
        )
        _assert(selected_priority > fallback_priority,
                f"fixture must select the invalidated pair first: "
                f"{(selected_priority, fallback_priority)}")

        by_seed = {
            selected[0]: (selected[1], 0.99),
            fallback[0]: (fallback[1], 0.90),
        }
        reserve_calls = 0
        calls: list[tuple[int, int]] = []

        def fake_find(_conn, _vec, *, exclude_id=None, **_):
            if exclude_id not in by_seed:
                return []
            other, similarity = by_seed[exclude_id]
            return [{"id": other, "similarity": similarity, "kind": "fact",
                     "status": "staging"}]

        def invalidating_reserve(*args, **kwargs):
            nonlocal reserve_calls
            result = original_reserve(*args, **kwargs)
            reserve_calls += 1
            if reserve_calls == 1:
                conn.execute(
                    "UPDATE nodes SET status = 'stale' WHERE id = ?",
                    (selected[0],),
                )
                conn.commit()
            return result

        def stub_arb(older, newer, _sim, **_):
            calls.append(tuple(sorted((older["id"], newer["id"]))))
            return {"decision": "keep_both", "reason": "test"}

        budget.DEFAULT_HEAL_DAILY_CAP = 1
        budget.reserve_available = invalidating_reserve
        heal.find_near_duplicates = fake_find
        heal._arbitrate_nightly = stub_arb
        result = heal.nightly_heal(
            conn, project_path=tmp, use_llm=True,
            low_threshold=0.50, high_threshold=0.70,
        )

        _assert(calls == [tuple(sorted(fallback))],
                f"stale selected pair consumed the generic slot: {calls}")
        _assert(not heal.edge_exists_between(conn, *selected),
                "invalidated selected pair must remain unmutated")
        _assert(heal.edge_exists_between(conn, *fallback),
                "fallback did not receive the transferred slot")
        _assert(result["llm_invocations"] == 1
                and result["deferred"] == 0
                and result["budget_blocked"] == 0,
                f"reservation race caused a false deferral: {result}")
        _assert(budget.status(tmp, heal_cap=1)["heal"]["remaining"] == 0,
                "the transferred slot should be consumed exactly once")
        print("PASS nightly_heal_revalidates_after_budget_reservation")
    finally:
        budget.DEFAULT_HEAL_DAILY_CAP = original_cap
        budget.reserve_available = original_reserve
        heal.find_near_duplicates = original_find
        heal._arbitrate_nightly = original_arb
        _cleanup(tmp, conn)


def test_nightly_heal_keeps_generic_slots_for_downstream_frontiers():
    """Invalidated frontiers do not prove their generic slots are surplus: a
    surviving component can expose more live LLM work after its next verdict."""
    from latch.gate import budget

    tmp, conn = _fresh_db()
    original_find = heal.find_near_duplicates
    original_arb = heal._arbitrate_nightly
    original_cap = budget.DEFAULT_HEAL_DAILY_CAP
    try:
        a = _mk(conn, title="chain a", body="shared downstream chain")
        b = _mk(conn, title="chain b", body="shared downstream chain")
        c = _mk(conn, title="chain c", body="shared downstream chain")
        h = _mk(conn, title="chain h", body="shared downstream chain")
        d = _mk(conn, title="invalid d", body="invalidated independent pair")
        e = _mk(conn, title="invalid e", body="invalidated independent pair")
        f = _mk(conn, title="invalid f", body="other invalidated pair")
        g = _mk(conn, title="invalid g", body="other invalidated pair")
        i = _mk(conn, title="invalid i", body="third invalidated pair")
        j = _mk(conn, title="invalid j", body="third invalidated pair")
        for node_id in (a, b, c, h, d, e, f, g, i, j):
            _set_ref(conn, node_id, 1)
            _set_ts(conn, node_id, updated_at=_days_ago(5))

        by_seed = {
            a: [(b, 0.99), (c, 0.85), (h, 0.80)],
            d: [(e, 0.95)],
            f: [(g, 0.90)],
            i: [(j, 0.88)],
        }
        calls: list[tuple[int, int]] = []

        def fake_find(_conn, _vec, *, exclude_id=None, **_):
            return [
                {"id": other, "similarity": similarity, "kind": "fact",
                 "status": "staging"}
                for other, similarity in by_seed.get(exclude_id, [])
            ]

        def stub_arb(older, newer, _sim, **_):
            pair = tuple(sorted((older["id"], newer["id"])))
            calls.append(pair)
            if pair == tuple(sorted((a, b))):
                conn.execute(
                    "UPDATE nodes SET status = 'stale' WHERE id IN (?, ?, ?)",
                    (d, f, i),
                )
                conn.commit()
            return {"decision": "keep_both", "reason": "test"}

        budget.DEFAULT_HEAL_DAILY_CAP = 3
        heal.find_near_duplicates = fake_find
        heal._arbitrate_nightly = stub_arb
        result = heal.nightly_heal(
            conn, project_path=tmp, use_llm=True,
            low_threshold=0.50, high_threshold=0.70,
        )

        expected = [
            tuple(sorted((a, b))),
            tuple(sorted((a, c))),
            tuple(sorted((a, h))),
        ]
        _assert(calls == expected,
                f"generic slots did not reach downstream frontiers: {calls}")
        _assert(result["llm_invocations"] == 3
                and result["deferred"] == 0
                and result["budget_blocked"] == 0,
                f"downstream live work was falsely capped: {result}")
        _assert(budget.status(tmp, heal_cap=3)["heal"]["remaining"] == 0,
                "all three authoritative slots should be consumed")
        today = datetime.now(timezone.utc).date()
        deferred = list(log_utils.read_log_range("heal_deferred", today, today, tmp))
        _assert(not [row for row in deferred if row.get("reason") == "budget_cap"],
                f"no live downstream pair should be budget-deferred: {deferred}")
        print("PASS nightly_heal_keeps_generic_slots_for_downstream_frontiers")
    finally:
        budget.DEFAULT_HEAL_DAILY_CAP = original_cap
        heal.find_near_duplicates = original_find
        heal._arbitrate_nightly = original_arb
        _cleanup(tmp, conn)


def test_nightly_heal_high_tier_arbitrated_before_low_tier_under_budget_pressure():
    """Two-pass dispatch (id=950): with budget for only ONE LLM call, that
    call must be spent on the high-tier pair, not the low-tier pair, even
    when the low-tier pair was discovered first in node-id iteration order."""
    tmp, conn = _fresh_db()
    try:
        # Low-tier pair (lower node ids — would be encountered first in the
        # old single-pass iteration order).
        low_a = _mk(conn, kind="fact", title="low pair a",
                    body="some moderately overlapping content")
        low_b = _mk(conn, kind="fact", title="low pair b",
                    body="some moderately overlapping content")
        # High-tier pair (higher node ids — would be encountered last under
        # the old order; the bug we're fixing).
        high_a = _mk(conn, kind="fact", title="high pair a",
                     body="this is some near duplicate content")
        high_b = _mk(conn, kind="fact", title="high pair b",
                     body="this is some near duplicate content")
        for n in (low_a, low_b, high_a, high_b):
            _set_ts(conn, n, updated_at=_days_ago(5))

        # Stub find_near_duplicates so we control which sim each pair gets.
        original_find = heal.find_near_duplicates

        def fake_find(_conn, _vec, *, exclude_id=None, threshold=0.0, top_k=5, **_):
            if exclude_id == low_a:
                return [{"id": low_b, "similarity": 0.60,
                         "kind": "fact", "status": "staging"}]
            if exclude_id == high_a:
                return [{"id": high_b, "similarity": 0.95,
                         "kind": "fact", "status": "staging"}]
            return []

        heal.find_near_duplicates = fake_find

        # Track which pair the LLM arbitrator was invoked for.
        calls: list[tuple[int, int]] = []
        original_arb = heal._arbitrate_nightly

        def stub_arb(a_node, b_node, _sim, **kw):  # **kw: tolerate a_repos/b_repos evidence
            calls.append((a_node["id"], b_node["id"]))
            return {"decision": "keep_both", "reason": "test"}

        heal._arbitrate_nightly = stub_arb

        # Pre-fill heal budget leaving only 1 LLM call available.
        from latch.gate import budget
        cap = budget.DEFAULT_HEAL_DAILY_CAP
        for _ in range(cap - 1):
            budget.check_and_record(tmp, category="heal", cap=cap)

        try:
            result = heal.nightly_heal(
                conn, project_path=tmp, use_llm=True,
                low_threshold=0.50, high_threshold=0.70,
            )
        finally:
            heal.find_near_duplicates = original_find
            heal._arbitrate_nightly = original_arb

        _assert(result["by_tier"]["high"] == 1 and result["by_tier"]["low"] == 1,
                f"expected one pair per tier in by_tier: {result['by_tier']}")
        _assert(result["llm_invocations"] == 1,
                f"expected exactly 1 LLM call (budget allowed only 1): {result}")
        _assert(len(calls) == 1, f"arbitrator should have been called once: {calls}")
        # The one LLM call must be the high-tier pair.
        called_pair = tuple(sorted(calls[0]))
        expected_high = tuple(sorted((high_a, high_b)))
        _assert(called_pair == expected_high,
                f"LLM call should target high-tier pair {expected_high}, "
                f"got {called_pair} (calls={calls})")
        # Low-tier pair must have been budget-blocked.
        _assert(result["budget_blocked"] == 1,
                f"expected 1 budget_blocked: {result}")
        _assert(result["budget_blocked_by_tier"]["low"] == 1
                and result["budget_blocked_by_tier"]["high"] == 0,
                f"budget block should be on low tier only: "
                f"{result['budget_blocked_by_tier']}")
        _assert(result["deferred_by_tier"]["low"] == 1
                and result["deferred_by_tier"]["high"] == 0,
                f"deferral should be on low tier only: "
                f"{result['deferred_by_tier']}")
        _assert(not heal.edge_exists_between(conn, low_a, low_b),
                "budget-blocked low-tier pair must remain retryable")
        _assert(heal.edge_exists_between(conn, high_a, high_b),
                "the one adjudicated high-tier pair should persist its verdict")
        print(f"PASS nightly_heal_high_tier_arbitrated_before_low_tier_under_budget_pressure "
              f"(by_tier={result['by_tier']}, "
              f"budget_blocked_by_tier={result['budget_blocked_by_tier']})")
    finally:
        _cleanup(tmp, conn)


# ---------- integrity pass ----------

def test_integrity_removes_orphan_edges():
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="a")
        # FKs normally prevent this — temporarily disable so we can simulate
        # the bitrot state the integrity pass is meant to catch (e.g. edges
        # left over from a pre-FK-enforcement era).
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("INSERT INTO edges (src, dst, relation, created_at) "
                     "VALUES (?, ?, ?, datetime('now'))",
                     (a, 99999, "bogus"))
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        result = heal.run_integrity_pass(conn)
        _assert(result["orphan_edges_deleted"] >= 1,
                f"expected at least 1 orphan edge cleaned: {result}")
        count = conn.execute("SELECT COUNT(*) FROM edges WHERE dst = 99999").fetchone()[0]
        _assert(count == 0, "orphan edge should be gone")
        print("PASS integrity_removes_orphan_edges")
    finally:
        _cleanup(tmp, conn)


def test_integrity_backfills_missing_vec_rows():
    """If vec is loaded and a node has an embedding but no vec_nodes row, backfill it."""
    tmp, conn = _fresh_db()
    try:
        if not db.vec_loaded(conn):
            print("SKIP integrity_backfills_missing_vec_rows (sqlite-vec not loaded)")
            return
        a = _mk(conn, title="fill me", body="needs a vec row")
        conn.execute("DELETE FROM vec_nodes WHERE rowid = ?", (a,))
        conn.commit()
        missing_before = conn.execute(
            "SELECT COUNT(*) FROM vec_nodes WHERE rowid = ?", (a,)
        ).fetchone()[0]
        _assert(missing_before == 0, "precondition: vec row removed")
        result = heal.run_integrity_pass(conn)
        _assert(result["vec_backfilled"] >= 1, result)
        present_after = conn.execute(
            "SELECT COUNT(*) FROM vec_nodes WHERE rowid = ?", (a,)
        ).fetchone()[0]
        _assert(present_after == 1, "vec row should be backfilled")
        print("PASS integrity_backfills_missing_vec_rows")
    finally:
        _cleanup(tmp, conn)


# ---------- nightly_heal end-to-end ----------

def test_nightly_heal_resolves_recency_collision():
    tmp, conn = _fresh_db()
    try:
        # Two semantically similar nodes, one clearly older.
        a = _mk(conn, title="deploy uses docker compose",
                body="production deploy runs docker compose up -d")
        b = _mk(conn, title="deployment docker compose",
                body="our deploy uses docker compose up")
        _set_ts(conn, a, updated_at=_days_ago(90))
        _set_ts(conn, b, updated_at=_days_ago(2))
        result = heal.nightly_heal(conn, project_path=tmp, use_llm=False,
                                   low_threshold=0.5)
        _assert(result["superseded"] >= 1, f"expected a supersede: {result}")
        _assert(result["by_path"]["recency"] >= 1,
                f"expected recency path: {result}")
        # Older should be stale now; newer should still be non-stale.
        _assert(db.get_node(conn, a)["status"] == "stale", "older should be stale")
        _assert(db.get_node(conn, b)["status"] != "stale", "newer should be alive")
        print(f"PASS nightly_heal_resolves_recency_collision ({result['by_path']})")
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_is_idempotent():
    """Second run should be a no-op — edges from the first run short-circuit."""
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="python deploy pipeline",
                body="the python deploy pipeline uses github actions")
        b = _mk(conn, title="deploy pipeline",
                body="python deploy pipeline is github actions based")
        _set_ts(conn, a, updated_at=_days_ago(60))
        _set_ts(conn, b, updated_at=_days_ago(1))
        first = heal.nightly_heal(conn, project_path=tmp, use_llm=False,
                                  low_threshold=0.5)
        _assert(first["collisions"] >= 1, first)
        second = heal.nightly_heal(conn, project_path=tmp, use_llm=False,
                                   low_threshold=0.5)
        _assert(second["superseded"] == 0 and second["kept_both"] == 0,
                f"second run should not apply new actions: {second}")
        print(f"PASS nightly_heal_is_idempotent "
              f"(1st: super={first['superseded']} kept={first['kept_both']}; "
              f"2nd: super={second['superseded']} kept={second['kept_both']})")
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_skips_when_edge_exists():
    """Manually-linked pairs should not be re-arbitrated."""
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="deploy uses docker",
                body="deployment pipeline uses docker compose")
        b = _mk(conn, title="deployment docker compose",
                body="deploy uses docker compose up")
        _set_ts(conn, a, updated_at=_days_ago(90))
        _set_ts(conn, b, updated_at=_days_ago(1))
        # User has already linked them deliberately.
        db.add_edge(conn, src=a, dst=b, relation="relates_to")
        result = heal.nightly_heal(conn, project_path=tmp, use_llm=False,
                                   low_threshold=0.5)
        _assert(result["skipped_edge_exists"] >= 1,
                f"expected edge-exists skip: {result}")
        _assert(db.get_node(conn, a)["status"] != "stale", "a should not be marked stale")
        _assert(db.get_node(conn, b)["status"] != "stale", "b should not be marked stale")
        print("PASS nightly_heal_skips_when_edge_exists")
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_disabled_flag():
    tmp, conn = _fresh_db()
    try:
        import os
        os.environ["CLAUDE_KB_DISABLE"] = "1"
        try:
            result = heal.nightly_heal(conn, project_path=tmp, use_llm=False)
            _assert(result.get("ok") is False and result.get("reason") == "disabled", result)
            print("PASS nightly_heal_disabled_flag")
        finally:
            del os.environ["CLAUDE_KB_DISABLE"]
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_runs_log_retention():
    tmp, conn = _fresh_db()
    try:
        result = heal.nightly_heal(conn, project_path=tmp, use_llm=False)
        _assert("log_retention" in result,
                f"expected log_retention key in summary: {result}")
        retention = result["log_retention"]
        _assert(isinstance(retention, dict), f"expected dict, got {type(retention)}")
        for k in ("gzipped", "deleted", "skipped"):
            _assert(k in retention,
                    f"expected {k} in retention result: {retention}")
        print("PASS nightly_heal_runs_log_retention")
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_log_retention_failure_isolated():
    from latch.common import log_utils
    tmp, conn = _fresh_db()
    saved = log_utils.maintain_log_retention
    try:
        def boom(*a, **kw):
            raise RuntimeError("retention exploded")
        log_utils.maintain_log_retention = boom
        result = heal.nightly_heal(conn, project_path=tmp, use_llm=False)
        _assert(result.get("ok") is True,
                f"nightly_heal should still succeed: {result}")
        _assert("error" in result.get("log_retention", {}),
                f"expected error key in retention result: {result.get('log_retention')}")
        print("PASS nightly_heal_log_retention_failure_isolated")
    finally:
        log_utils.maintain_log_retention = saved
        _cleanup(tmp, conn)


def test_nightly_heal_runs_correlator():
    tmp, conn = _fresh_db()
    try:
        result = heal.nightly_heal(conn, project_path=tmp, use_llm=False)
        _assert("correlator" in result,
                f"expected correlator key in summary: {result}")
        counts = result["correlator"]
        _assert(isinstance(counts, dict), f"expected dict, got {type(counts)}")
        for k in ("rows_emitted", "rows_skipped_no_session_id",
                  "rows_skipped_dedup", "rows_skipped_skipped_verdict"):
            _assert(k in counts,
                    f"expected {k} in correlator result: {counts}")
        print("PASS nightly_heal_runs_correlator")
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_correlator_failure_isolated():
    from latch.proof import correlator
    tmp, conn = _fresh_db()
    saved = correlator.correlate
    try:
        def boom(*a, **kw):
            raise RuntimeError("correlator exploded")
        correlator.correlate = boom
        result = heal.nightly_heal(conn, project_path=tmp, use_llm=False)
        _assert(result.get("ok") is True,
                f"nightly_heal should still succeed: {result}")
        _assert("error" in result.get("correlator", {}),
                f"expected error key in correlator result: {result.get('correlator')}")
        print("PASS nightly_heal_correlator_failure_isolated")
    finally:
        correlator.correlate = saved
        _cleanup(tmp, conn)


def test_nightly_heal_excludes_summary_nodes_from_contradiction_sweep():
    """Regression (id=1699 / id=1797): a tree summary is a near-duplicate of its
    own members by construction, so it collides at the sweep threshold and the
    recency pass would let a FRESH summary supersede (stale) an OLDER source
    node. Summaries must be excluded on BOTH sides — the seed query (so a summary
    is never an `a`) and the post-refetch guard (so a summary returned as `b` by
    find_near_duplicates(kind=None) is skipped). With an old canonical content
    node and a fresh same-vector summary, the content must stay non-stale and NO
    edge (supersedes / reconciled_by / related_to) may be created between them.

    This test FAILS on the pre-fix code: identical vectors + the recency setup
    drive a deterministic supersede of the content node, no LLM required."""
    tmp, conn = _fresh_db()
    try:
        # Identical title+body => identical embedding => sim ~1.0 (high tier).
        t, b = "shared cluster topic", "identical body text so the vectors match exactly"
        content = _mk(conn, kind="decision", title=t, body=b, status="canonical")
        summ = _mk(conn, kind="summary", title=t, body=b, status="canonical")
        _set_ts(conn, content, updated_at=_days_ago(90))  # old source node
        _set_ts(conn, summ, updated_at=_days_ago(1))       # fresh -> recency would pick it
        result = heal.nightly_heal(conn, project_path=tmp, use_llm=False,
                                   low_threshold=0.5)
        # Neither node staled.
        _assert(db.get_node(conn, content)["status"] != "stale",
                f"content must NOT be staled by a summary: {result}")
        _assert(db.get_node(conn, summ)["status"] != "stale",
                f"summary must NOT be staled either: {result}")
        # No edge of any kind linking them (covers supersedes/reconciled_by/related_to).
        _assert(not heal.edge_exists_between(conn, content, summ),
                "no edge may link content<->summary after the sweep")
        _assert(result["superseded"] == 0, f"no supersede expected: {result}")
        # Seed-exclusion side: the summary was NOT examined as a candidate `a`.
        _assert(result["examined"] == 1,
                f"summary must be excluded from candidate seeds (examined!=1): {result}")
        # Guard side: the summary returned as `b` was skipped by the rail.
        _assert(result["skipped_summary"] >= 1,
                f"summary-exclusion guard should have fired: {result}")
        print(f"PASS nightly_heal_excludes_summary_nodes_from_contradiction_sweep "
              f"(examined={result['examined']}, skipped_summary={result['skipped_summary']})")
    finally:
        _cleanup(tmp, conn)



def _read_run_rows(tmp):
    path = log_utils.today_log_path("heal_run", tmp)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def test_nightly_heal_emits_run_heartbeat():
    """Every sweep leaves a heal_run row. Per-arbitration diagnostics do not
    prove that an entire pass completed, so the positive heartbeat is required."""
    tmp, conn = _fresh_db()
    try:
        heal.nightly_heal(conn, project_path=tmp, use_llm=False)
        rows = _read_run_rows(tmp)
        _assert(len(rows) == 1, f"expected 1 heal_run row, got {len(rows)}")
        for key in ("examined", "collisions", "superseded", "kept_both",
                    "reconciled", "deferred", "budget_blocked", "by_path",
                    "integrity", "backend", "model"):
            _assert(key in rows[0], f"heal_run row missing {key}")
        _assert(rows[0]["backend"] == "claude", rows[0])
        _assert(rows[0]["model"] == "sonnet", rows[0])
        print("PASS nightly_heal_emits_run_heartbeat")
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_heartbeat_on_integrity_only_run():
    """contradictions=False returns early; that path must still heartbeat."""
    tmp, conn = _fresh_db()
    try:
        heal.nightly_heal(conn, project_path=tmp, use_llm=False,
                          contradictions=False)
        rows = _read_run_rows(tmp)
        _assert(len(rows) == 1,
                f"integrity-only run emitted {len(rows)} heal_run rows")
        print("PASS nightly_heal_heartbeat_on_integrity_only_run")
    finally:
        _cleanup(tmp, conn)


def _set_ws(conn, node_id, ws):
    conn.execute("UPDATE nodes SET workstream_id = ? WHERE id = ?", (ws, node_id))
    conn.commit()


def _priority_probe(conn, tmp, pairs, *, refs, ws=None):
    """Run a sweep with budget for exactly one LLM call over `pairs`.

    Returns the (sorted) pair the arbitrator actually spent it on.
    `pairs` is [(a, b, sim), ...]; `refs`/`ws` map node id -> value.
    """
    from latch.gate import budget
    for node_id, n in refs.items():
        _set_ref(conn, node_id, n)
    for node_id, w in (ws or {}).items():
        _set_ws(conn, node_id, w)
    for a, b, _ in pairs:
        _set_ts(conn, a, updated_at=_days_ago(5))
        _set_ts(conn, b, updated_at=_days_ago(5))

    by_a = {a: (b, sim) for a, b, sim in pairs}
    original_find, original_arb = heal.find_near_duplicates, heal._arbitrate_nightly
    calls: list[tuple[int, int]] = []

    def fake_find(_conn, _vec, *, exclude_id=None, threshold=0.0, top_k=5, **_):
        if exclude_id in by_a:
            b, sim = by_a[exclude_id]
            return [{"id": b, "similarity": sim, "kind": "fact",
                     "status": "staging"}]
        return []

    def stub_arb(a_node, b_node, _sim, **kw):
        calls.append((a_node["id"], b_node["id"]))
        return {"decision": "keep_both", "reason": "test"}

    # Patch INSIDE the guard: the budget pre-fill below can raise, and a leaked
    # module global would corrupt every later test in the session.
    try:
        heal.find_near_duplicates, heal._arbitrate_nightly = fake_find, stub_arb
        cap = budget.DEFAULT_HEAL_DAILY_CAP
        for _ in range(cap - 1):
            budget.check_and_record(tmp, category="heal", cap=cap)
        result = heal.nightly_heal(conn, project_path=tmp, use_llm=True,
                                   low_threshold=0.50, high_threshold=0.70)
    finally:
        heal.find_near_duplicates, heal._arbitrate_nightly = original_find, original_arb
    _assert(len(calls) == 1, f"expected exactly 1 LLM call, got {calls}")
    return tuple(sorted(calls[0])), result


def test_nightly_heal_prioritizes_retrieved_pairs_over_more_similar_ones():
    """Within a tier, the one available LLM call goes to the pair that is
    actually being retrieved -- even though the cold pair is MORE similar.
    Similarity measures likeness; ref_count measures what the contradiction
    costs while it stays unresolved."""
    tmp, conn = _fresh_db()
    try:
        hot_a = _mk(conn, kind="fact", title="hot a", body="hot pair content")
        hot_b = _mk(conn, kind="fact", title="hot b", body="hot pair content")
        cold_a = _mk(conn, kind="fact", title="cold a", body="cold pair content")
        cold_b = _mk(conn, kind="fact", title="cold b", body="cold pair content")
        called, result = _priority_probe(
            conn, tmp,
            # cold pair is the MORE similar one -- old sort would pick it.
            [(hot_a, hot_b, 0.75), (cold_a, cold_b, 0.95)],
            refs={hot_a: 10, hot_b: 10, cold_a: 0, cold_b: 0},
        )
        _assert(called == tuple(sorted((hot_a, hot_b))),
                f"budget should go to the retrieved pair, got {called}")
        _assert(result["priority_llm"] > result["priority_deferred"],
                f"priority mass should favour what was arbitrated: {result}")
        print("PASS nightly_heal_prioritizes_retrieved_pairs_over_more_similar_ones")
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_cross_lane_outranks_same_lane_at_equal_refs():
    """Equal retrieval pressure, and the same-lane pair is MORE similar: the
    cross-workstream pair still wins, because no single lane's context
    resolves it. Similarity alone would have picked the same-lane pair."""
    tmp, conn = _fresh_db()
    try:
        # workstream_id is a FK to a kind='workstream' node; the sweep's seed
        # query excludes that kind, so these never become candidates.
        ws1 = _mk(conn, kind="workstream", title="lane one", body="lane one")
        ws2 = _mk(conn, kind="workstream", title="lane two", body="lane two")
        x_a = _mk(conn, kind="fact", title="x a", body="cross lane content")
        x_b = _mk(conn, kind="fact", title="x b", body="cross lane content")
        s_a = _mk(conn, kind="fact", title="s a", body="same lane content")
        s_b = _mk(conn, kind="fact", title="s b", body="same lane content")
        called, _ = _priority_probe(
            conn, tmp,
            [(x_a, x_b, 0.80), (s_a, s_b, 0.95)],
            refs={x_a: 3, x_b: 3, s_a: 3, s_b: 3},
            ws={x_a: ws1, x_b: ws2, s_a: ws1, s_b: ws1},
        )
        _assert(called == tuple(sorted((x_a, x_b))),
                f"cross-lane pair should win the tiebreak, got {called}")
        print("PASS nightly_heal_cross_lane_outranks_same_lane_at_equal_refs")
    finally:
        _cleanup(tmp, conn)


def test_priority_preserves_similarity_order_when_budget_covers_queue():
    """Priority is a scarce-budget selector, not a general invocation reorder.
    With room for the full queue, LLM calls retain similarity order so the
    process-global timeout breaker cannot change persisted topology."""
    from latch.gate import budget

    tmp, conn = _fresh_db()
    original_find = heal.find_near_duplicates
    original_invoke = heal.model_backends.invoke_prompt
    original_timeouts = heal._consecutive_arbitrate_timeouts
    try:
        cold_a = _mk(conn, title="cold a", body="cold full-budget pair")
        cold_b = _mk(conn, title="cold b", body="cold full-budget pair")
        warm_a = _mk(conn, title="warm a", body="warm full-budget pair")
        warm_b = _mk(conn, title="warm b", body="warm full-budget pair")
        hot_a = _mk(conn, title="hot a", body="hot full-budget pair")
        hot_b = _mk(conn, title="hot b", body="hot full-budget pair")
        for node_id, refs in {
            cold_a: 0, cold_b: 0,
            warm_a: 5, warm_b: 5,
            hot_a: 20, hot_b: 20,
        }.items():
            _set_ref(conn, node_id, refs)
            _set_ts(conn, node_id, updated_at=_days_ago(5))
        priorities = [
            heal._pair_priority(db.get_node(conn, hot_a), db.get_node(conn, hot_b)),
            heal._pair_priority(db.get_node(conn, warm_a), db.get_node(conn, warm_b)),
            heal._pair_priority(db.get_node(conn, cold_a), db.get_node(conn, cold_b)),
        ]
        _assert(priorities[0] > priorities[1] > priorities[2],
                f"fixture must oppose priority and similarity order: {priorities}")
        budget.approve_today(tmp)

        calls: list[str] = []

        def fake_find(_conn, _vec, *, exclude_id=None, **_):
            pairs = {
                cold_a: (cold_b, 0.99),
                warm_a: (warm_b, 0.90),
                hot_a: (hot_b, 0.80),
            }
            if exclude_id not in pairs:
                return []
            other, similarity = pairs[exclude_id]
            return [{"id": other, "similarity": similarity, "kind": "fact",
                     "status": "staging"}]

        def fake_invoke(prompt, **_):
            if "cold full-budget pair" in prompt:
                calls.append("cold")
                return heal.model_backends.ModelCallResult(
                    '{"decision":"supersede_b","reason":"cold succeeds"}',
                    None, False, "test",
                )
            if "warm full-budget pair" in prompt:
                calls.append("warm")
            elif "hot full-budget pair" in prompt:
                calls.append("hot")
            else:
                raise AssertionError("unexpected arbitration prompt")
            return heal.model_backends.ModelCallResult(
                None, "test timeout", True, "test",
            )

        try:
            heal.find_near_duplicates = fake_find
            heal.model_backends.invoke_prompt = fake_invoke
            heal._consecutive_arbitrate_timeouts = 0
            result = heal.nightly_heal(
                conn, project_path=tmp, use_llm=True,
                low_threshold=0.50, high_threshold=0.70,
            )
        finally:
            heal.find_near_duplicates = original_find
            heal.model_backends.invoke_prompt = original_invoke
            heal._consecutive_arbitrate_timeouts = original_timeouts

        _assert(calls == ["cold", "warm", "hot"],
                f"full-budget calls must retain similarity order: {calls}")
        supersedes = [(row["src"], row["dst"]) for row in conn.execute(
            "SELECT src, dst FROM edges WHERE relation = 'supersedes'"
        )]
        _assert(supersedes == [(cold_b, cold_a)],
                f"timeout breaker changed the cold-pair topology: {supersedes}")
        _assert(result["llm_invocations"] == 3
                and result["budget_blocked"] == 0,
                f"full queue should be arbitrated: {result}")
        print("PASS priority_preserves_similarity_order_when_budget_covers_queue")
    finally:
        heal.find_near_duplicates = original_find
        heal.model_backends.invoke_prompt = original_invoke
        heal._consecutive_arbitrate_timeouts = original_timeouts
        _cleanup(tmp, conn)


def test_heal_deferred_row_carries_priority_fields():
    """The deferred backlog must be measurable from its own log stream."""
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, kind="fact", title="d a", body="deferred pair content")
        b = _mk(conn, kind="fact", title="d b", body="deferred pair content")
        c = _mk(conn, kind="fact", title="d c", body="other pair content")
        d = _mk(conn, kind="fact", title="d d", body="other pair content")
        _priority_probe(conn, tmp, [(a, b, 0.95), (c, d, 0.90)],
                        refs={a: 9, b: 9, c: 1, d: 1})
        path = log_utils.today_log_path("heal_deferred", tmp)
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
                if l.strip()]
        _assert(rows, "expected at least one heal_deferred row")
        for key in ("priority", "ref_count_max", "cross_lane"):
            _assert(key in rows[0], f"heal_deferred row missing {key}")
        print("PASS heal_deferred_row_carries_priority_fields")
    finally:
        _cleanup(tmp, conn)


def test_priority_does_not_change_deterministic_topology():
    """Priority must allocate the LLM budget WITHOUT changing which supersedes
    execute. Two pairs share node A: the more-similar pair (A,C) and the
    higher-priority pair (A,B). With the LLM disabled, every verdict here is
    deterministic, so the persisted supersedes edge must be the one similarity
    order produces -- priority must not reach it."""
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, kind="fact", title="shared", body="the shared claim")
        b = _mk(conn, kind="fact", title="hot", body="the shared claim hot")
        c = _mk(conn, kind="fact", title="cold", body="the shared claim cold")
        _set_ref(conn, a, 1); _set_ref(conn, b, 10); _set_ref(conn, c, 5)
        for n in (a, b, c):
            _set_ts(conn, n, updated_at=_days_ago(5))

        original_find = heal.find_near_duplicates

        def fake_find(_conn, _vec, *, exclude_id=None, threshold=0.0, top_k=5, **_):
            if exclude_id == a:
                return [{"id": c, "similarity": 0.95, "kind": "fact",
                         "status": "staging"},
                        {"id": b, "similarity": 0.75, "kind": "fact",
                         "status": "staging"}]
            return []

        try:
            heal.find_near_duplicates = fake_find
            heal.nightly_heal(conn, project_path=tmp, use_llm=False,
                              low_threshold=0.50, high_threshold=0.70)
        finally:
            heal.find_near_duplicates = original_find

        winners = [r["src"] for r in conn.execute(
            "SELECT src FROM edges WHERE relation = 'supersedes'")]
        _assert(winners == [c], f"expected the more-similar pair to resolve "
                               f"first (winner {c}), got {winners}")
        print("PASS priority_does_not_change_deterministic_topology")
    finally:
        _cleanup(tmp, conn)


def test_priority_planning_preserves_mixed_shared_node_precedence():
    """A higher-similarity LLM pair must mutate before a lower-similarity
    deterministic pair that shares its node. Priority selects the LLM slot;
    it must not let the deterministic pair jump ahead and stale that node."""
    from latch.gate import budget

    tmp, conn = _fresh_db()
    try:
        ws1 = _mk(conn, kind="workstream", title="lane one", body="lane one")
        ws2 = _mk(conn, kind="workstream", title="lane two", body="lane two")
        a = _mk(conn, title="shared a", body="the shared claim")
        b = _mk(conn, title="llm b", body="the shared claim llm")
        c = _mk(conn, title="deterministic c", body="the shared claim newer")
        _set_ws(conn, a, ws1); _set_ws(conn, b, ws2); _set_ws(conn, c, ws1)
        for node_id in (a, b, c):
            _set_ref(conn, node_id, 1)
        _set_ts(conn, a, updated_at=_days_ago(60))
        _set_ts(conn, b, updated_at=_days_ago(60))
        _set_ts(conn, c, updated_at=_days_ago(5))

        original_find = heal.find_near_duplicates
        original_arb = heal._arbitrate_nightly
        calls: list[tuple[int, int]] = []

        def fake_find(_conn, _vec, *, exclude_id=None, threshold=0.0,
                      top_k=5, **_):
            if exclude_id == a:
                return [
                    {"id": b, "similarity": 0.95, "kind": "fact",
                     "status": "staging"},
                    {"id": c, "similarity": 0.75, "kind": "fact",
                     "status": "staging"},
                ]
            return []

        def stub_arb(older, newer, _sim, **_):
            calls.append((older["id"], newer["id"]))
            return {"decision": "supersede_b", "reason": "test"}

        try:
            heal.find_near_duplicates = fake_find
            heal._arbitrate_nightly = stub_arb
            cap = budget.DEFAULT_HEAL_DAILY_CAP
            for _ in range(cap - 1):
                budget.check_and_record(tmp, category="heal", cap=cap)
            result = heal.nightly_heal(
                conn, project_path=tmp, use_llm=True,
                low_threshold=0.50, high_threshold=0.70,
            )
        finally:
            heal.find_near_duplicates = original_find
            heal._arbitrate_nightly = original_arb

        _assert(calls == [(a, b)], f"expected A/B LLM arbitration, got {calls}")
        edges = [(row["src"], row["dst"]) for row in conn.execute(
            "SELECT src, dst FROM edges WHERE relation = 'supersedes'"
        )]
        _assert(edges == [(b, a)],
                f"expected B supersedes A before A/C could mutate, got {edges}")
        _assert(result["llm_invocations"] == 1,
                f"expected one selected LLM call: {result}")
        print("PASS priority_planning_preserves_mixed_shared_node_precedence")
    finally:
        _cleanup(tmp, conn)


def test_priority_reclaims_invalidated_slot_for_later_fallback():
    """A deterministic cascade can invalidate the initially highest-priority
    LLM pair before its turn. The sole live fallback must consume that slot,
    even though it appears later in similarity order."""
    from latch.gate import budget

    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="shared a", body="later fallback shared claim")
        b = _mk(conn, title="selected b", body="later fallback shared claim")
        c = _mk(conn, title="newer c", body="later fallback shared claim")
        d = _mk(conn, title="fallback d", body="later live fallback claim")
        e = _mk(conn, title="fallback e", body="later live fallback claim")
        for node_id, refs in {a: 10, b: 10, c: 1, d: 1, e: 1}.items():
            _set_ref(conn, node_id, refs)
        _set_ts(conn, a, updated_at=_days_ago(60))
        _set_ts(conn, b, updated_at=_days_ago(60))
        _set_ts(conn, c, updated_at=_days_ago(5))
        _set_ts(conn, d, updated_at=_days_ago(60))
        _set_ts(conn, e, updated_at=_days_ago(60))
        selected_priority = heal._pair_priority(
            db.get_node(conn, a), db.get_node(conn, b),
        )
        fallback_priority = heal._pair_priority(
            db.get_node(conn, d), db.get_node(conn, e),
        )
        _assert(selected_priority > fallback_priority,
                f"fixture no longer selects A/B first: "
                f"{(selected_priority, fallback_priority)}")

        original_find = heal.find_near_duplicates
        original_arb = heal._arbitrate_nightly
        calls: list[tuple[int, int]] = []

        def fake_find(_conn, _vec, *, exclude_id=None, **_):
            if exclude_id == a:
                return [
                    {"id": c, "similarity": 0.99, "kind": "fact",
                     "status": "staging"},
                    {"id": b, "similarity": 0.95, "kind": "fact",
                     "status": "staging"},
                ]
            if exclude_id == d:
                return [{"id": e, "similarity": 0.90, "kind": "fact",
                         "status": "staging"}]
            return []

        def stub_arb(older, newer, _sim, **_):
            calls.append(tuple(sorted((older["id"], newer["id"]))))
            return {"decision": "keep_both", "reason": "test"}

        try:
            heal.find_near_duplicates = fake_find
            heal._arbitrate_nightly = stub_arb
            cap = budget.DEFAULT_HEAL_DAILY_CAP
            for _ in range(cap - 1):
                budget.check_and_record(tmp, category="heal", cap=cap)
            result = heal.nightly_heal(
                conn, project_path=tmp, use_llm=True,
                low_threshold=0.50, high_threshold=0.70,
            )
        finally:
            heal.find_near_duplicates = original_find
            heal._arbitrate_nightly = original_arb

        fallback = tuple(sorted((d, e)))
        _assert(calls == [fallback],
                f"released slot should reach the sole live fallback: {calls}")
        supersedes = [(row["src"], row["dst"]) for row in conn.execute(
            "SELECT src, dst FROM edges WHERE relation = 'supersedes'"
        )]
        _assert(supersedes == [(c, a)],
                f"deterministic topology changed: {supersedes}")
        _assert(heal.edge_exists_between(conn, d, e),
                "fallback verdict was not persisted")
        _assert(result["llm_invocations"] == 1
                and result["deferred"] == 0
                and result["budget_blocked"] == 0,
                f"live fallback was falsely deferred: {result}")
        _assert(budget.status(tmp)["heal"]["remaining"] == 0,
                "released slot was not consumed")

        today = datetime.now(timezone.utc).date()
        rows = list(log_utils.read_log_range("heal_deferred", today, today, tmp))
        _assert(not [row for row in rows if row.get("reason") == "budget_cap"],
                f"no live pair should be logged budget_cap: {rows}")
        print("PASS priority_reclaims_invalidated_slot_for_later_fallback")
    finally:
        _cleanup(tmp, conn)


def test_priority_reclaims_invalidated_slot_for_highest_ranked_fallback():
    """If a deterministic cascade invalidates the selected LLM pair, its
    unspent slot goes to the highest-priority surviving fallback -- not merely
    the next fallback encountered in similarity order. The winning fallback is
    deliberately earlier than the invalidating mutation in global order."""
    from latch.gate import budget

    tmp, conn = _fresh_db()
    try:
        ws1 = _mk(conn, kind="workstream", title="lane one", body="lane one")
        ws2 = _mk(conn, kind="workstream", title="lane two", body="lane two")
        a = _mk(conn, title="shared a", body="shared invalidation claim")
        b = _mk(conn, title="selected b", body="shared invalidation claim")
        c = _mk(conn, title="newer c", body="shared invalidation claim")
        d = _mk(conn, title="near fallback d", body="near fallback claim")
        e = _mk(conn, title="near fallback e", body="near fallback claim")
        f = _mk(conn, title="priority fallback f", body="priority fallback claim")
        g = _mk(conn, title="priority fallback g", body="priority fallback claim")

        for node_id, ws in {
            a: ws1, b: ws2, c: ws1, d: ws1, e: ws1, f: ws1, g: ws2,
        }.items():
            _set_ws(conn, node_id, ws)
        for node_id, refs in {
            a: 10, b: 10, c: 10, d: 2, e: 2, f: 7, g: 7,
        }.items():
            _set_ref(conn, node_id, refs)
        _set_ts(conn, a, updated_at=_days_ago(60))
        _set_ts(conn, b, updated_at=_days_ago(60))
        _set_ts(conn, c, updated_at=_days_ago(5))
        for node_id in (d, e, f, g):
            _set_ts(conn, node_id, updated_at=_days_ago(5))
        priorities = {
            "selected": heal._pair_priority(db.get_node(conn, a), db.get_node(conn, b)),
            "earlier": heal._pair_priority(db.get_node(conn, f), db.get_node(conn, g)),
            "later": heal._pair_priority(db.get_node(conn, d), db.get_node(conn, e)),
        }
        _assert(priorities["selected"] > priorities["earlier"] > priorities["later"],
                f"fixture no longer exercises ranked fallback: {priorities}")

        original_find = heal.find_near_duplicates
        original_arb = heal._arbitrate_nightly
        calls: list[tuple[int, int]] = []

        def fake_find(_conn, _vec, *, exclude_id=None, **_):
            if exclude_id == a:
                return [
                    {"id": c, "similarity": 0.99, "kind": "fact",
                     "status": "staging"},
                    {"id": b, "similarity": 0.95, "kind": "fact",
                     "status": "staging"},
                ]
            if exclude_id == d:
                return [{"id": e, "similarity": 0.90, "kind": "fact",
                         "status": "staging"}]
            if exclude_id == f:
                return [{"id": g, "similarity": 0.995, "kind": "fact",
                         "status": "staging"}]
            return []

        def stub_arb(older, newer, _sim, **_):
            calls.append(tuple(sorted((older["id"], newer["id"]))))
            return {"decision": "keep_both", "reason": "test"}

        try:
            heal.find_near_duplicates = fake_find
            heal._arbitrate_nightly = stub_arb
            cap = budget.DEFAULT_HEAL_DAILY_CAP
            for _ in range(cap - 1):
                budget.check_and_record(tmp, category="heal", cap=cap)
            result = heal.nightly_heal(
                conn, project_path=tmp, use_llm=True,
                low_threshold=0.50, high_threshold=0.70,
            )
        finally:
            heal.find_near_duplicates = original_find
            heal._arbitrate_nightly = original_arb

        expected_priority_fallback = tuple(sorted((f, g)))
        _assert(calls == [expected_priority_fallback],
                f"slot should go to ranked surviving fallback, got {calls}")
        supersedes = [(row["src"], row["dst"]) for row in conn.execute(
            "SELECT src, dst FROM edges WHERE relation = 'supersedes'"
        )]
        _assert(supersedes == [(c, a)],
                f"similarity-ordered deterministic topology changed: {supersedes}")
        _assert(heal.edge_exists_between(conn, f, g),
                "selected fallback verdict was not persisted")
        _assert(not heal.edge_exists_between(conn, d, e),
                "lower-priority fallback should remain retryable")
        _assert(result["llm_invocations"] == 1
                and result["budget_blocked"] == 1
                and result["deferred"] == 1,
                f"one fallback should spend and one should defer: {result}")
        _assert(budget.status(tmp)["heal"]["remaining"] == 0,
                "reclaimed slot was not consumed")

        today = datetime.now(timezone.utc).date()
        rows = list(log_utils.read_log_range("heal_deferred", today, today, tmp))
        deferred = [row for row in rows if row.get("reason") == "budget_cap"]
        deferred_pairs = {
            (row.get("node_a_id"), row.get("node_b_id")) for row in deferred
        }
        _assert(len(deferred) == 1
                and deferred_pairs == {tuple(sorted((d, e)))},
                f"only the lower-priority live fallback may defer: {rows}")
        print("PASS priority_reclaims_invalidated_slot_for_highest_ranked_fallback")
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_heartbeat_on_failure():
    """A crashing sweep must still leave a heal_run row, marked not-ok."""
    tmp, conn = _fresh_db()
    original = heal.run_integrity_pass
    try:
        def boom(*_a, **_k):
            raise RuntimeError("integrity exploded")

        heal.run_integrity_pass = boom
        raised = False
        try:
            heal.nightly_heal(conn, project_path=tmp, use_llm=False)
        except RuntimeError:
            raised = True
        finally:
            heal.run_integrity_pass = original
        _assert(raised, "the exception must still propagate to the caller")

        rows = _read_run_rows(tmp)
        _assert(len(rows) == 1, f"a failed sweep must emit one heal_run row, "
                               f"got {len(rows)}")
        _assert(rows[0]["ok"] is False, f"row must be marked not-ok: {rows[0]}")
        _assert(rows[0]["error"] == "RuntimeError",
                f"row must carry the structural error type: {rows[0]}")
        print("PASS nightly_heal_heartbeat_on_failure")
    finally:
        heal.run_integrity_pass = original
        _cleanup(tmp, conn)

if __name__ == "__main__":
    test_recency_pass_picks_newer_when_diff_large_and_newer_fresh()
    test_recency_pass_skips_when_both_stale()
    test_recency_pass_skips_small_age_diff()
    test_ref_count_pass_picks_dominant()
    test_ref_count_pass_skips_cold_start()
    test_ref_count_pass_skips_below_ratio()
    test_ref_count_pass_skips_cross_kind()
    test_llm_pass_skip_when_use_llm_false()
    test_edge_exists_between_detects_either_direction()
    test_apply_nightly_supersede_marks_stale_and_links()
    test_nightly_supersede_commit_boundary_unchanged()
    test_order_by_age_uses_updated_at()
    test_order_by_age_falls_back_to_id()
    test_apply_nightly_reconciled_by_adds_edge_and_keeps_canonical()
    test_three_pass_low_tier_skips_recency_and_ref_count()
    test_three_pass_high_tier_default_preserves_behavior()
    test_integrity_removes_orphan_edges()
    test_integrity_backfills_missing_vec_rows()
    test_nightly_heal_resolves_recency_collision()
    test_nightly_heal_is_idempotent()
    test_nightly_heal_skips_when_edge_exists()
    test_nightly_heal_disabled_flag()
    test_nightly_heal_summary_has_reconciled_and_by_tier_keys()
    test_nightly_heal_low_tier_keeps_both_when_use_llm_false()
    test_nightly_heal_applies_reconciled_by_when_llm_returns_it()
    test_nightly_heal_budget_blocked_defers_without_edge_and_retries()
    test_nightly_heal_budget_status_error_defers_and_runs_maintenance()
    test_nightly_heal_releases_unused_batch_after_apply_failure()
    test_nightly_heal_replans_batch_across_utc_rollover()
    test_nightly_heal_never_merges_reservations_across_utc_days()
    test_nightly_heal_transfers_reserved_slot_after_live_invalidation()
    test_nightly_heal_revalidates_after_budget_reservation()
    test_nightly_heal_keeps_generic_slots_for_downstream_frontiers()
    test_nightly_heal_high_tier_arbitrated_before_low_tier_under_budget_pressure()
    test_nightly_heal_runs_log_retention()
    test_nightly_heal_log_retention_failure_isolated()
    test_nightly_heal_runs_correlator()
    test_nightly_heal_correlator_failure_isolated()
    test_nightly_heal_excludes_summary_nodes_from_contradiction_sweep()
    test_nightly_heal_emits_run_heartbeat()
    test_nightly_heal_heartbeat_on_integrity_only_run()
    test_nightly_heal_prioritizes_retrieved_pairs_over_more_similar_ones()
    test_nightly_heal_cross_lane_outranks_same_lane_at_equal_refs()
    test_priority_preserves_similarity_order_when_budget_covers_queue()
    test_heal_deferred_row_carries_priority_fields()
    test_priority_does_not_change_deterministic_topology()
    test_priority_planning_preserves_mixed_shared_node_precedence()
    test_priority_reclaims_invalidated_slot_for_later_fallback()
    test_priority_reclaims_invalidated_slot_for_highest_ranked_fallback()
    test_nightly_heal_heartbeat_on_failure()
    print("\nAll nightly-heal tests pass.")
