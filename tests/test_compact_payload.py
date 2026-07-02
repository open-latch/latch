"""Unit tests for MCP payload size guardrails (compact-by-default rule).

Covers:
- db.compact_row: prefix excerpt, snippet override, embedding/body strip,
  body_chars correctness, empty-body edge case.
- db.fts_search: returns _fts_snippet column on FTS5 matches.
- search.hybrid_search: preserves _fts_snippet across RRF when available.
- mcp_server._compact_search_rows: snippet vs prefix vs mixed strategy.
- mcp_server._compact_recent_rows: prefix-only.
- mcp_server._apply_safety_net: triggers above threshold, no-ops below,
  marks first row.
- mcp_server._log_compact: writes JSONL line with the documented schema.
- kb_search and kb_recent tool entry points: compact-by-default, verbose=True
  returns full body.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import db  # noqa: E402
import search  # noqa: E402
import mcp_server  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_compact_test_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


# ---------- db.compact_row ----------

def test_compact_row_prefix_excerpt_truncates_long_body():
    body = "x" * 5000
    row = {"id": 1, "kind": "fact", "title": "t", "body": body, "status": "canonical"}
    out = db.compact_row(row)
    _assert("body" not in out, "full body must be removed")
    _assert(out["body_chars"] == 5000, f"body_chars mismatch: {out['body_chars']}")
    _assert(out["body_excerpt"].endswith("…"), "long-body excerpt should end with ellipsis")
    _assert(len(out["body_excerpt"]) <= db.COMPACT_BODY_CHARS + 5,
            f"excerpt too long: {len(out['body_excerpt'])}")
    print("PASS compact_row_prefix_excerpt_truncates_long_body")


def test_compact_row_short_body_passes_through_no_ellipsis():
    body = "short body"
    row = {"id": 1, "kind": "fact", "title": "t", "body": body, "status": "canonical"}
    out = db.compact_row(row)
    _assert(out["body_excerpt"] == "short body", "short body should pass through")
    _assert(out["body_chars"] == len(body), "body_chars should equal full length")
    _assert(not out["body_excerpt"].endswith("…"),
            "short-body excerpt should NOT have ellipsis")
    print("PASS compact_row_short_body_passes_through_no_ellipsis")


def test_compact_row_snippet_text_overrides_prefix():
    body = "the full body has many things in it but only the matched span matters"
    row = {"id": 1, "kind": "fact", "title": "t", "body": body, "status": "canonical"}
    out = db.compact_row(row, snippet_text="…matched span…")
    _assert(out["body_excerpt"] == "…matched span…", "snippet should override prefix")
    _assert(out["body_chars"] == len(body), "body_chars still reflects true body length")
    print("PASS compact_row_snippet_text_overrides_prefix")


def test_compact_row_strips_embedding_blob():
    row = {"id": 1, "kind": "fact", "title": "t", "body": "x",
           "status": "canonical", "embedding": b"\x00\x01\x02"}
    out = db.compact_row(row)
    _assert("embedding" not in out, "embedding blob must be stripped")
    print("PASS compact_row_strips_embedding_blob")


def test_compact_row_handles_empty_body():
    row = {"id": 1, "kind": "fact", "title": "t", "body": "", "status": "canonical"}
    out = db.compact_row(row)
    _assert(out["body_excerpt"] == "", "empty body → empty excerpt")
    _assert(out["body_chars"] == 0, "empty body → 0 chars")
    print("PASS compact_row_handles_empty_body")


def test_compact_row_handles_missing_body():
    row = {"id": 1, "kind": "fact", "title": "t", "status": "canonical"}
    out = db.compact_row(row)
    _assert(out["body_excerpt"] == "", "missing body → empty excerpt")
    _assert(out["body_chars"] == 0, "missing body → 0 chars")
    print("PASS compact_row_handles_missing_body")


# ---------- db.fts_search snippet column ----------

def test_fts_search_returns_snippet_column():
    tmp, conn = _fresh_db()
    try:
        body = "the Redis session cache beats the in-process LRU on p99 latency"
        nid = db.insert_node(conn, kind="fact", title="Redis beats LRU", body=body)
        rows = db.fts_search(conn, "Redis")
        _assert(len(rows) >= 1, "FTS should match the seeded row")
        target = next((r for r in rows if r["id"] == nid), None)
        _assert(target is not None, "seeded row not found in FTS results")
        _assert("_fts_snippet" in target, "_fts_snippet column missing")
        _assert(target["_fts_snippet"], "_fts_snippet should be non-empty")
        _assert("redis" in target["_fts_snippet"].lower(),
                f"snippet should contain match: {target['_fts_snippet']!r}")
        print("PASS fts_search_returns_snippet_column")
    finally:
        _cleanup(tmp, conn)


# ---------- search.hybrid_search snippet propagation ----------

def test_hybrid_search_preserves_fts_snippet():
    tmp, conn = _fresh_db()
    try:
        body = "Kalman filter forecast horizon is 30 seconds for HFT cadence"
        nid = db.insert_node(
            conn, kind="fact", title="Kalman 30s horizon", body=body,
            embedding=db.np.zeros(384, dtype=db.np.float32).tobytes()
            if hasattr(db, "np") else None,
        )
        results = search.hybrid_search(conn, "Kalman forecast", limit=5,
                                       track_access=False)
        target = next((r for r in results if r["id"] == nid), None)
        _assert(target is not None, "hybrid_search did not return seeded row")
        _assert(target.get("_fts_snippet"),
                "hybrid_search should preserve _fts_snippet from FTS leg")
        print("PASS hybrid_search_preserves_fts_snippet")
    finally:
        _cleanup(tmp, conn)


# ---------- mcp_server._compact_search_rows ----------

def test_compact_search_rows_snippet_strategy_when_all_have_snippet():
    rows = [
        {"id": 1, "kind": "fact", "title": "a", "body": "x" * 3000,
         "status": "canonical", "_fts_snippet": "…matched a…"},
        {"id": 2, "kind": "fact", "title": "b", "body": "y" * 3000,
         "status": "canonical", "_fts_snippet": "…matched b…"},
    ]
    out, strategy = mcp_server._compact_search_rows(rows)
    _assert(strategy == "snippet", f"expected snippet, got {strategy}")
    _assert(out[0]["body_excerpt"] == "…matched a…", "row 0 should use snippet")
    _assert(out[1]["body_excerpt"] == "…matched b…", "row 1 should use snippet")
    _assert("_fts_snippet" not in out[0],
            "_fts_snippet implementation detail should be stripped from output")
    print("PASS compact_search_rows_snippet_strategy_when_all_have_snippet")


def test_compact_search_rows_prefix_strategy_when_no_snippet():
    rows = [
        {"id": 1, "kind": "fact", "title": "a", "body": "x" * 3000,
         "status": "canonical"},
        {"id": 2, "kind": "fact", "title": "b", "body": "y" * 3000,
         "status": "canonical"},
    ]
    out, strategy = mcp_server._compact_search_rows(rows)
    _assert(strategy == "prefix", f"expected prefix, got {strategy}")
    _assert(out[0]["body_excerpt"].startswith("xxx"), "row 0 should be prefix excerpt")
    _assert(out[0]["body_excerpt"].endswith("…"), "long-body prefix should have ellipsis")
    print("PASS compact_search_rows_prefix_strategy_when_no_snippet")


def test_compact_search_rows_mixed_strategy_when_some_have_snippet():
    rows = [
        {"id": 1, "kind": "fact", "title": "a", "body": "x" * 3000,
         "status": "canonical", "_fts_snippet": "…matched a…"},
        {"id": 2, "kind": "fact", "title": "b", "body": "y" * 3000,
         "status": "canonical"},
    ]
    out, strategy = mcp_server._compact_search_rows(rows)
    _assert(strategy == "mixed", f"expected mixed, got {strategy}")
    _assert(out[0]["body_excerpt"] == "…matched a…", "row 0 uses snippet")
    _assert(out[1]["body_excerpt"].startswith("yyy"), "row 1 falls back to prefix")
    print("PASS compact_search_rows_mixed_strategy_when_some_have_snippet")


def test_compact_search_rows_empty_returns_none_strategy():
    out, strategy = mcp_server._compact_search_rows([])
    _assert(out == [], "empty in → empty out")
    _assert(strategy == "none", f"empty should report none strategy, got {strategy}")
    print("PASS compact_search_rows_empty_returns_none_strategy")


# ---------- mcp_server._compact_recent_rows ----------

def test_compact_recent_rows_uses_prefix_only():
    rows = [
        {"id": 1, "kind": "fact", "title": "a", "body": "x" * 3000,
         "status": "canonical"},
        {"id": 2, "kind": "fact", "title": "b", "body": "yy",
         "status": "canonical"},
    ]
    out = mcp_server._compact_recent_rows(rows)
    _assert(out[0]["body_excerpt"].endswith("…"), "long row → ellipsis")
    _assert(out[1]["body_excerpt"] == "yy", "short row → full body in excerpt")
    _assert(out[0]["body_chars"] == 3000, "true length preserved")
    _assert(out[1]["body_chars"] == 2, "true length preserved (short)")
    print("PASS compact_recent_rows_uses_prefix_only")


# ---------- mcp_server KB activity hints ----------

def test_kb_activity_contract_is_foreground_safe():
    activity = mcp_server._kb_activity(
        action="write",
        tool="latch_insert",
        summary="Tracked KB decision node id=12: Use Redis.",
        nodes=[{"id": 12, "kind": "decision", "title": "Use Redis"}],
        hints=["plan_freshness_hint"],
    )
    _assert(activity["label"] == "Latch KB activity", activity)
    _assert(activity["must_display_to_user"] is True, activity)
    _assert(activity["action"] == "write", activity)
    _assert(activity["nodes"][0]["id"] == 12, activity)
    _assert(activity["hints"] == ["plan_freshness_hint"], activity)
    print("PASS kb_activity_contract_is_foreground_safe")


def test_kb_get_returns_activity_hint():
    tmp, conn = _fresh_db()
    try:
        nid = db.insert_node(conn, kind="decision", title="Use Redis", body="because")
        original_cwd = mcp_server.PROJECT_CWD
        original_conn = mcp_server._conn
        mcp_server.PROJECT_CWD = tmp
        mcp_server._conn = lambda: db.connect(tmp)
        try:
            result = mcp_server.kb_get(nid, include_neighbors=False)
        finally:
            mcp_server._conn = original_conn
            mcp_server.PROJECT_CWD = original_cwd
        activity = result.get("kb_activity")
        _assert(activity, f"kb_get should return kb_activity: {result}")
        _assert(activity["action"] == "read", activity)
        _assert(activity["tool"] == "latch_get", activity)
        _assert(activity["nodes"][0]["id"] == nid, activity)
        print("PASS kb_get_returns_activity_hint")
    finally:
        _cleanup(tmp, conn)


# ---------- mcp_server._apply_safety_net ----------

def test_safety_net_does_not_trigger_under_threshold():
    rows = [{"id": 1, "body_excerpt": "x" * 100, "body_chars": 100}]
    out, triggered = mcp_server._apply_safety_net(rows)
    _assert(not triggered, "small payload should not trigger safety net")
    _assert(out[0]["body_excerpt"] == "x" * 100, "excerpt should be untouched")
    print("PASS safety_net_does_not_trigger_under_threshold")


def test_safety_net_triggers_above_threshold_and_truncates():
    # Force payload over 80KB by stuffing a few rows with huge excerpts.
    huge = "z" * 30_000
    rows = [
        {"id": i, "body_excerpt": huge, "body_chars": len(huge), "title": "t"}
        for i in range(4)
    ]
    out, triggered = mcp_server._apply_safety_net(rows)
    _assert(triggered, "oversize payload should trigger safety net")
    for r in out:
        _assert(len(r["body_excerpt"]) <= mcp_server.SAFETY_NET_FALLBACK_CHARS + 5,
                f"excerpt not force-truncated: {len(r['body_excerpt'])}")
    _assert(out[0].get("safety_net_triggered") is True,
            "first row should be stamped with safety_net_triggered")
    print("PASS safety_net_triggers_above_threshold_and_truncates")


def test_safety_net_handles_empty_rows():
    out, triggered = mcp_server._apply_safety_net([])
    _assert(out == [], "empty in → empty out")
    _assert(not triggered, "empty payload should not trigger safety net")
    print("PASS safety_net_handles_empty_rows")


# ---------- mcp_server._log_compact ----------

def test_log_compact_writes_jsonl_with_documented_schema():
    tmp = tempfile.mkdtemp(prefix="kb_log_test_")
    try:
        # _log_compact uses module-level PROJECT_CWD; redirect it temporarily.
        original = mcp_server.PROJECT_CWD
        mcp_server.PROJECT_CWD = tmp
        try:
            mcp_server._log_compact(
                tool="latch_search", row_count=3, total_bytes=1234,
                verbose_requested=False, safety_net_triggered=False,
                excerpt_strategy="snippet",
            )
        finally:
            mcp_server.PROJECT_CWD = original

        # Locate the log file under projects/<sanitized-cwd>/compact_excerpt.log.
        import paths as paths_mod
        log_path = paths_mod.project_dir(tmp) / mcp_server.COMPACT_LOG_FILE_NAME
        _assert(log_path.exists(), f"log file not written at {log_path}")

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        _assert(len(lines) == 1, f"expected 1 log line, got {len(lines)}")
        entry = json.loads(lines[0])
        for field in ("ts", "project", "tool", "row_count", "total_bytes",
                      "verbose_requested", "safety_net_triggered", "excerpt_strategy"):
            _assert(field in entry, f"log entry missing field {field!r}: {entry}")
        _assert(entry["tool"] == "latch_search", f"tool mismatch: {entry['tool']}")
        _assert(entry["row_count"] == 3, f"row_count mismatch: {entry['row_count']}")
        _assert(entry["excerpt_strategy"] == "snippet",
                f"strategy mismatch: {entry['excerpt_strategy']}")
        print("PASS log_compact_writes_jsonl_with_documented_schema")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- end-to-end: kb_search and kb_recent tools ----------

def test_kb_search_compact_default_returns_excerpts():
    tmp, conn = _fresh_db()
    try:
        body = "the cache warm-up routine pre-populates session keys from the last 30 minutes of activity to avoid cold-start latency on instance flap " * 30
        db.insert_node(
            conn, kind="fact", title="cache warm-up routine", body=body,
        )
        original_cwd = mcp_server.PROJECT_CWD
        mcp_server.PROJECT_CWD = tmp
        # Re-route the default _conn() to the fresh DB.
        original_conn = mcp_server._conn
        mcp_server._conn = lambda: db.connect(tmp)
        try:
            results = mcp_server.kb_search("cache warm-up")
        finally:
            mcp_server._conn = original_conn
            mcp_server.PROJECT_CWD = original_cwd
        _assert(results, "kb_search returned no results")
        r = results[0]
        _assert("body" not in r, "compact mode must not include full body")
        _assert("body_excerpt" in r, "compact mode must include body_excerpt")
        _assert("body_chars" in r, "compact mode must include body_chars")
        _assert(r["body_chars"] == len(body), "body_chars must equal full body length")
        _assert(r["kb_activity"]["tool"] == "latch_search",
                f"first search row should carry kb_activity: {r}")
        _assert(r["kb_activity"]["must_display_to_user"] is True,
                f"search activity should be foreground: {r['kb_activity']}")
        print("PASS kb_search_compact_default_returns_excerpts")
    finally:
        _cleanup(tmp, conn)


def test_kb_search_verbose_returns_full_body():
    tmp, conn = _fresh_db()
    try:
        body = "the cache warm-up routine pre-populates session keys from the last 30 minutes " * 30
        db.insert_node(conn, kind="fact", title="cache warm-up", body=body)
        original_cwd = mcp_server.PROJECT_CWD
        original_conn = mcp_server._conn
        mcp_server.PROJECT_CWD = tmp
        mcp_server._conn = lambda: db.connect(tmp)
        try:
            results = mcp_server.kb_search("cache warm-up", verbose=True)
        finally:
            mcp_server._conn = original_conn
            mcp_server.PROJECT_CWD = original_cwd
        _assert(results, "kb_search verbose returned no results")
        r = results[0]
        _assert("body" in r, "verbose mode must include full body")
        _assert(r["body"] == body, "verbose body must equal seeded body")
        _assert(r["kb_activity"]["tool"] == "latch_search",
                f"verbose search row should carry kb_activity: {r}")
        _assert("_fts_snippet" not in r,
                "_fts_snippet implementation detail should be stripped even in verbose")
        print("PASS kb_search_verbose_returns_full_body")
    finally:
        _cleanup(tmp, conn)


def test_kb_recent_compact_default_returns_excerpts():
    tmp, conn = _fresh_db()
    try:
        body = "x" * 4000
        db.insert_node(conn, kind="fact", title="long fact", body=body)
        original_cwd = mcp_server.PROJECT_CWD
        original_conn = mcp_server._conn
        mcp_server.PROJECT_CWD = tmp
        mcp_server._conn = lambda: db.connect(tmp)
        try:
            results = mcp_server.kb_recent(limit=5)
        finally:
            mcp_server._conn = original_conn
            mcp_server.PROJECT_CWD = original_cwd
        _assert(results, "kb_recent returned no results")
        r = results[0]
        _assert("body" not in r, "compact mode must not include full body")
        _assert("body_excerpt" in r, "compact mode must include body_excerpt")
        _assert(r["body_chars"] == 4000, "body_chars must reflect true length")
        _assert(r["body_excerpt"].endswith("…"), "long body excerpt should be ellipsized")
        _assert(r["kb_activity"]["tool"] == "latch_recent",
                f"first recent row should carry kb_activity: {r}")
        print("PASS kb_recent_compact_default_returns_excerpts")
    finally:
        _cleanup(tmp, conn)


def test_kb_recent_verbose_returns_full_body():
    tmp, conn = _fresh_db()
    try:
        body = "x" * 4000
        db.insert_node(conn, kind="fact", title="long fact", body=body)
        original_cwd = mcp_server.PROJECT_CWD
        original_conn = mcp_server._conn
        mcp_server.PROJECT_CWD = tmp
        mcp_server._conn = lambda: db.connect(tmp)
        try:
            results = mcp_server.kb_recent(limit=5, verbose=True)
        finally:
            mcp_server._conn = original_conn
            mcp_server.PROJECT_CWD = original_cwd
        r = results[0]
        _assert(r["body"] == body, "verbose body must equal seeded body")
        _assert(r["kb_activity"]["tool"] == "latch_recent",
                f"verbose recent row should carry kb_activity: {r}")
        print("PASS kb_recent_verbose_returns_full_body")
    finally:
        _cleanup(tmp, conn)


if __name__ == "__main__":
    # db.compact_row
    test_compact_row_prefix_excerpt_truncates_long_body()
    test_compact_row_short_body_passes_through_no_ellipsis()
    test_compact_row_snippet_text_overrides_prefix()
    test_compact_row_strips_embedding_blob()
    test_compact_row_handles_empty_body()
    test_compact_row_handles_missing_body()
    # FTS5 snippet column
    test_fts_search_returns_snippet_column()
    test_hybrid_search_preserves_fts_snippet()
    # mcp_server compaction helpers
    test_compact_search_rows_snippet_strategy_when_all_have_snippet()
    test_compact_search_rows_prefix_strategy_when_no_snippet()
    test_compact_search_rows_mixed_strategy_when_some_have_snippet()
    test_compact_search_rows_empty_returns_none_strategy()
    test_compact_recent_rows_uses_prefix_only()
    # KB activity
    test_kb_activity_contract_is_foreground_safe()
    test_kb_get_returns_activity_hint()
    # safety net
    test_safety_net_does_not_trigger_under_threshold()
    test_safety_net_triggers_above_threshold_and_truncates()
    test_safety_net_handles_empty_rows()
    # logging
    test_log_compact_writes_jsonl_with_documented_schema()
    # tool entry points
    test_kb_search_compact_default_returns_excerpts()
    test_kb_search_verbose_returns_full_body()
    test_kb_recent_compact_default_returns_excerpts()
    test_kb_recent_verbose_returns_full_body()
    print("\nAll compact-payload tests pass.")
