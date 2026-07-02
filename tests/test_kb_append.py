"""Tests for Slice 2a — kb_append + the rolling-region transform (spec id=1519).

Covers the pure transform (rolling.py) and the decoupled tool core
(_kb_append_impl) against a throwaway temp DB. Run directly:
    python tests/test_kb_append.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import db          # noqa: E402
import embeddings  # noqa: E402
import mcp_server  # noqa: E402
import rolling     # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_append_test_")
    return tmp, db.connect(tmp)


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


# ---------- rolling.apply (pure transform) ----------

def test_apply_creates_region_on_top():
    base = "## Workstream X\n\nStable base content that should be preserved."
    out = rolling.apply(base, "shipped the thing", date="2026-06-10")
    _assert(out.startswith(rolling.START), f"region must be at top:\n{out}")
    _assert(rolling.HEADER in out, "header missing")
    _assert("- 2026-06-10: shipped the thing" in out, "entry missing")
    _assert("Stable base content that should be preserved." in out, "base body lost")
    print("PASS test_apply_creates_region_on_top")


def test_apply_newest_first_and_caps_at_3():
    body = "base"
    for i, txt in enumerate(["one", "two", "three", "four"]):
        body = rolling.apply(body, txt, date=f"2026-06-1{i}")
    entries, rest = rolling._split(body)
    _assert(len(entries) == 3, f"cap=3 expected, got {len(entries)}: {entries}")
    _assert("four" in entries[0], f"newest must be first: {entries}")
    _assert(all("one" not in e for e in entries), f"oldest must be evicted: {entries}")
    _assert(rest == "base", f"base body must survive: {rest!r}")
    print("PASS test_apply_newest_first_and_caps_at_3")


def test_apply_collapses_multiline_text():
    out = rolling.apply("base", "line one\nline two\n  line three", date="2026-06-10")
    entries, _ = rolling._split(out)
    _assert("\n" not in entries[0], "entry must be a single line")
    _assert("line one line two line three" in entries[0], f"got {entries[0]!r}")
    print("PASS test_apply_collapses_multiline_text")


def test_strip_markers_removes_only_comment_lines():
    out = rolling.apply("base body", "x", date="2026-06-10")
    stripped = rolling.strip_markers(out)
    _assert(rolling.START not in stripped and rolling.END not in stripped, "markers remain")
    _assert(rolling.HEADER in stripped and "base body" in stripped, "content lost in strip")
    print("PASS test_strip_markers_removes_only_comment_lines")


# ---------- _kb_append_impl (tool core, temp DB) ----------

def test_append_updates_body_without_reembed():
    tmp, conn = _fresh_db()
    try:
        vec = embeddings.embed("workstream anchor")
        blob0 = embeddings.to_blob(vec)
        ws = db.insert_node(conn, kind="workstream", title="WS",
                            body="## WS\n\nbase", status="canonical", embedding=blob0)
        res = mcp_server._kb_append_impl(conn, ws, "shipped slice 2a",
                                         reembed=False, date="2026-06-10")
        _assert(res.get("ok") is True, res)
        node = db.get_node(conn, ws)
        _assert(node["body"].startswith(rolling.START), "region not at top of stored body")
        _assert("shipped slice 2a" in node["body"], "entry not stored")
        _assert("base" in node["body"], "base body lost")
        _assert(node["embedding"] == blob0, "embedding changed despite reembed=False")
        print("PASS test_append_updates_body_without_reembed")
    finally:
        _cleanup(tmp, conn)


def test_append_reembed_true_changes_embedding():
    tmp, conn = _fresh_db()
    try:
        blob0 = embeddings.to_blob(embeddings.embed("anchor"))
        ws = db.insert_node(conn, kind="workstream", title="WS",
                            body="base", status="canonical", embedding=blob0)
        mcp_server._kb_append_impl(conn, ws, "new state", reembed=True, date="2026-06-10")
        node = db.get_node(conn, ws)
        _assert(node["embedding"] != blob0, "embedding should change when reembed=True")
        print("PASS test_append_reembed_true_changes_embedding")
    finally:
        _cleanup(tmp, conn)


def test_append_rejects_claim_bearing_kinds():
    tmp, conn = _fresh_db()
    try:
        fact = db.insert_node(conn, kind="fact", title="F", body="a claim",
                              status="canonical",
                              embedding=embeddings.to_blob(embeddings.embed("a claim")))
        res = mcp_server._kb_append_impl(conn, fact, "x", reembed=False, date="2026-06-10")
        _assert(res.get("ok") is False, f"fact must be rejected: {res}")
        _assert("latch_correct" in res.get("error", ""),
                f"error should point to latch_correct: {res}")
        node = db.get_node(conn, fact)
        _assert(node["body"] == "a claim", "rejected append must not mutate the body")
        print("PASS test_append_rejects_claim_bearing_kinds")
    finally:
        _cleanup(tmp, conn)


def test_append_orphan_hint_empty_for_workstream_even_with_id_mention():
    # The gate's guardrail (id=1194/id=1172): orphan_hint is kind-scoped and must
    # stay empty for living-summary kinds — the filter is NOT bypassed.
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="base",
                            status="canonical",
                            embedding=embeddings.to_blob(embeddings.embed("WS")))
        res = mcp_server._kb_append_impl(conn, ws, "shipped; see id=999",
                                         reembed=False, date="2026-06-10")
        _assert(res["ok"] is True, res)
        _assert(res["orphan_hint"] == [], f"workstream orphan_hint must be []: {res['orphan_hint']}")
        print("PASS test_append_orphan_hint_empty_for_workstream_even_with_id_mention")
    finally:
        _cleanup(tmp, conn)


def test_append_empty_text_rejected():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="base",
                            status="canonical",
                            embedding=embeddings.to_blob(embeddings.embed("WS")))
        res = mcp_server._kb_append_impl(conn, ws, "   ", reembed=False, date="2026-06-10")
        _assert(res.get("ok") is False, f"empty text must be rejected: {res}")
        print("PASS test_append_empty_text_rejected")
    finally:
        _cleanup(tmp, conn)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
