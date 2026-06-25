"""Unit tests for Step 7 — nightly heal (integrity + three-pass arbitration).

Deterministic tests only: recency + ref_count paths don't call `claude -p`.
The LLM fallthrough path is exercised with use_llm=False, which skips to
keep_both — that verifies the branch reaches the terminal state without
actually spawning a subprocess.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import db  # noqa: E402
import embeddings  # noqa: E402
import heal  # noqa: E402


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
    """Summary schema includes new keys: reconciled count + by_tier breakdown."""
    tmp, conn = _fresh_db()
    try:
        result = heal.nightly_heal(conn, project_path=tmp, use_llm=False,
                                   contradictions=False)
        _assert("reconciled" in result, f"missing 'reconciled' key: {result}")
        _assert("by_tier" in result, f"missing 'by_tier' key: {result}")
        _assert("high" in result["by_tier"] and "low" in result["by_tier"], result["by_tier"])
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


def test_nightly_heal_budget_blocked_falls_back_to_keep_both():
    """When budget.check_and_record denies (cap hit), pair must fall back to
    keep_both (no supersede, no reconciled_by) and budget_blocked counter bumps."""
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

        # Pre-fill the heal budget so the next check_and_record returns False.
        import budget
        cap = budget.DEFAULT_HEAL_DAILY_CAP
        for _ in range(cap):
            budget.check_and_record(tmp, category="heal", cap=cap)

        result = heal.nightly_heal(
            conn, project_path=tmp, use_llm=True,
            low_threshold=0.30, high_threshold=0.95,  # force low-tier (always LLM)
        )

        _assert(result["budget_blocked"] >= 1,
                f"expected budget_blocked >= 1: {result}")
        _assert(result["superseded"] == 0, f"no supersede on budget block: {result}")
        _assert(result["reconciled"] == 0, f"no reconcile on budget block: {result}")
        # Both nodes still alive.
        _assert(db.get_node(conn, a)["status"] != "stale", "a stale")
        _assert(db.get_node(conn, b)["status"] != "stale", "b stale")
        print(f"PASS nightly_heal_budget_blocked_falls_back_to_keep_both "
              f"(budget_blocked={result['budget_blocked']})")
    finally:
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
        import budget
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
    import log_utils
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
    import correlator
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
    test_nightly_heal_budget_blocked_falls_back_to_keep_both()
    test_nightly_heal_high_tier_arbitrated_before_low_tier_under_budget_pressure()
    test_nightly_heal_runs_log_retention()
    test_nightly_heal_log_retention_failure_isolated()
    test_nightly_heal_runs_correlator()
    test_nightly_heal_correlator_failure_isolated()
    test_nightly_heal_excludes_summary_nodes_from_contradiction_sweep()
    print("\nAll nightly-heal tests pass.")
