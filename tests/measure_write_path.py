"""Measurement harness for the KB write-path latency work — decision id=1482, step 1.

This is the *measurement pass* that id=1310 and id=1482 mandate BEFORE any
surgical-update / kb_append code is written. It does NOT change the engine; it
quantifies where the cost of a "freshen a plan/workstream body" operation
actually goes, so the step-2 fix targets the real lever instead of an assumed one.

Design notes
------------
* **Standalone, zero standing overhead.** The harness is a manual benchmark, not
  in-handler instrumentation. Nothing here ships to a live install's hot path, so
  there is no per-op tax to gate (priority id=1329 / the gate's P3 ask, satisfied
  by elimination rather than a flag). It calls the same `db` / `heal` /
  `embeddings` functions the MCP tools call.
* **Never touches the live KB.** Runs against a fresh `tempfile` project DB.
* **Cross-platform** (priority id=1330): `perf_counter` + `tempfile` + `pathlib`
  only; no OS-specific paths or calls.

What it splits (per id=1482's required output)
-----------------------------------------------
  (A) intrinsic server-CPU per-op latency — embed, heal scan, hint SQL, DB write,
      lock wait, connect. Measured with perf_counter (median of R reps).
  (B) the round-trip / token shape — bytes fetched + resent for a full-body
      rewrite vs a surgical append. This is the cost perf_counter CANNOT see: it
      lands in the agent's context window as input tokens (the "~16x overhead"
      the lean-progress rule cited). Computed from body sizes, not timed.

Headline question for the checkpoint (do NOT pre-commit the fix — gate id=1482):
  is the kb_update full-body re-embed (id=1491) the dominant lever, or is it the
  token round-trip? Note up front: `embeddings.embed` truncates at
  MAX_SEQ_LEN=256 tokens (embeddings.py), so the re-embed is BOUNDED regardless
  of body size. This harness measures whether that makes the embed a non-lever.

Usage:  python tests/measure_write_path.py [--nodes N] [--reps R]
"""
from __future__ import annotations

import argparse
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import db          # noqa: E402
import embeddings  # noqa: E402
import heal        # noqa: E402
import lockfile    # noqa: E402

# A realistic large workstream body (~9.5 KB, matching id=338 per id=1310's trace).
_LARGE_BODY = (
    "## claude_kb v2 — KB tool build\n\n"
    + ("Latest: shipped X; see id=1234, id=1235; open question id=1300; "
       "search hints: foo, bar, baz. Recent ship reports below. " * 165)
)
# The actual change a "lean Latest: touch" makes — a few hundred chars.
_DELTA_LINE = (
    "Latest (2026-06-10): measurement harness for write-path cost landed; "
    "see decision id=1482 and fact id=1491. Next: step-2 design checkpoint."
)

_CHARS_PER_TOKEN = 4  # standard rough heuristic for English text


def _ms(fn, reps: int) -> tuple[float, float, float]:
    """Run fn() reps times; return (median, min, max) in milliseconds."""
    samples = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(samples), min(samples), max(samples)


def _row(label: str, med: float, lo: float, hi: float) -> str:
    return f"  {label:<34} {med:8.2f} ms   (min {lo:6.2f}  max {hi:6.2f})"


def _seed(conn, n: int) -> None:
    """Seed n embedded fact nodes so find_near_duplicates has a realistic scan
    population. Uses embed_batch for speed."""
    bodies = [
        f"seed node {i} about topic {i % 23}; filler so the body is a "
        f"plausible length and the vector is not degenerate."
        for i in range(n)
    ]
    vecs = embeddings.embed_batch(bodies)
    for i, b in enumerate(bodies):
        db.insert_node(
            conn, kind="fact", title=f"seed {i}", body=b,
            status="canonical", embedding=embeddings.to_blob(vecs[i]),
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=300,
                    help="seed population for the heal scan (live KB is ~1500)")
    ap.add_argument("--reps", type=int, default=9)
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="kb_writepath_")
    print(f"project (throwaway): {tmp}")
    print(f"large body: {len(_LARGE_BODY):,} chars   delta: {len(_DELTA_LINE):,} chars\n")

    conn = db.connect(tmp)
    try:
        vec_on = db.vec_loaded(conn)
        print(f"sqlite-vec loaded: {vec_on}  (False => brute-force cosine scan)")

        # Warm up the ONNX model so cold-load (~hundreds of ms) is excluded.
        t_warm = time.perf_counter()
        embeddings.embed("warm up the embedder")
        print(f"embedder warm-up (excluded): {(time.perf_counter() - t_warm) * 1000:.0f} ms")

        _seed(conn, args.nodes)
        print(f"seeded {args.nodes} fact nodes\n")

        # The workstream node we will freshen, + a lean progress node linked to it
        # via `advances` (so the hint computations have a realistic graph to walk).
        ws_vec = embeddings.embed(_LARGE_BODY[:2000])
        ws_id = db.insert_node(
            conn, kind="workstream", title="claude_kb v2 — KB tool build",
            body=_LARGE_BODY, status="canonical",
            embedding=embeddings.to_blob(ws_vec),
        )
        prog_body = "Shipped the write-path measurement harness. Linked to the workstream."
        prog_vec = embeddings.embed(prog_body)
        prog_id = db.insert_node(
            conn, kind="progress", title="measurement harness shipped",
            body=prog_body, embedding=embeddings.to_blob(prog_vec),
        )
        db.add_edge(conn, src=prog_id, dst=ws_id, relation="advances",
                    project_path=tmp, session_id="bench")

        reps = args.reps
        print("=" * 74)
        print("(A) INTRINSIC SERVER-CPU PER-OP LATENCY  (median of %d reps)" % reps)
        print("=" * 74)

        # --- embed: does cost scale with body size, or is it bounded by truncation?
        print(_row("embed(delta ~%d ch)" % len(_DELTA_LINE),
                   *_ms(lambda: embeddings.embed(_DELTA_LINE), reps)))
        print(_row("embed(large body ~%d ch)" % len(_LARGE_BODY),
                   *_ms(lambda: embeddings.embed(_LARGE_BODY), reps)))
        print("    ^ if these two are ~equal, the re-embed is BOUNDED by the 256-tok")
        print("      truncation and does NOT scale with body size (refines id=1491).\n")

        # --- heal similarity scan (find_near_duplicates)
        scan_vec = embeddings.embed(prog_body)
        print(_row("find_near_duplicates (scan)",
                   *_ms(lambda: heal.find_near_duplicates(
                       conn, scan_vec, kind="progress",
                       threshold=heal.SIMILARITY_THRESHOLD), reps)))

        # --- the three write-path hints (pure SQL edge-walks per id=832)
        print(_row("compute_plan_freshness_hint",
                   *_ms(lambda: heal.compute_plan_freshness_hint(conn, prog_id, "progress"), reps)))
        print(_row("compute_orphan_hint",
                   *_ms(lambda: heal.compute_orphan_hint(conn, prog_id, prog_body, "progress"), reps)))
        print(_row("compute_ship_edge_hint",
                   *_ms(lambda: heal.compute_ship_edge_hint(conn, prog_id, "progress"), reps)))

        # --- the DB write of the full ~9.5 KB row (UPDATE nodes + vec re-insert)
        ws_blob = embeddings.to_blob(ws_vec)
        print(_row("db.update_node (full ~9.5KB row)",
                   *_ms(lambda: db.update_node(conn, ws_id, body=_LARGE_BODY, embedding=ws_blob), reps)))

        # --- lock wait (no contention => ~0; real cost only under concurrent compaction)
        print(_row("lockfile.wait_for_compaction",
                   *_ms(lambda: lockfile.wait_for_compaction(tmp), reps)))

        print()
        # --- end-to-end: a full kb_update freshen = re-embed + DB write + orphan_hint
        def _full_freshen():
            v = embeddings.embed(f"{ '' }\n\n{_LARGE_BODY}")
            db.update_node(conn, ws_id, body=_LARGE_BODY, embedding=embeddings.to_blob(v))
            heal.compute_orphan_hint(conn, ws_id, _LARGE_BODY, "workstream")
        fm, flo, fhi = _ms(_full_freshen, reps)
        print(_row("FULL kb_update freshen (server)", fm, flo, fhi))

        # --- end-to-end: a lean kb_insert (progress node, no LLM arbitration)
        def _lean_insert():
            heal.insert_with_heal(
                conn, kind="progress", title="t", body="a lean progress note",
                status="staging", session_id="bench", use_llm=False,
                project_path=None,
            )
        print(_row("FULL kb_insert lean (server)", *_ms(_lean_insert, reps)))

        print("\n" + "=" * 74)
        print("(B) ROUND-TRIP / TOKEN SHAPE  (the cost perf_counter cannot see)")
        print("=" * 74)
        big = len(_LARGE_BODY)
        delta = len(_DELTA_LINE)
        # Full-body rewrite: agent must kb_get the body into context, then resend
        # the entire rewritten body in the kb_update call args.
        rewrite_fetch = big
        rewrite_resend = big
        rewrite_total = rewrite_fetch + rewrite_resend
        # Surgical append: no fetch needed; resend only the delta line.
        append_resend = delta
        print(f"  full-body rewrite : fetch {rewrite_fetch:,} + resend {rewrite_resend:,} "
              f"= {rewrite_total:,} ch  (~{rewrite_total // _CHARS_PER_TOKEN:,} tok)")
        print(f"  surgical append   : resend {append_resend:,} ch  "
              f"(~{max(1, append_resend // _CHARS_PER_TOKEN):,} tok)")
        print(f"  token overhead ratio (rewrite / append): "
              f"~{rewrite_total / max(1, append_resend):.0f}x")
        print(f"  round-trip COUNT  : rewrite = 2 calls (kb_get + kb_update) "
              f"+ any hint follow-ups;  append = 1 call")

        print("\n" + "=" * 74)
        print("READ THE SPLIT (bring these to the step-2 checkpoint; do NOT pre-pick a fix)")
        print("=" * 74)
        print(f"  server CPU per full freshen : ~{fm:.1f} ms")
        print(f"  token cost per full freshen : ~{rewrite_total // _CHARS_PER_TOKEN:,} input tokens")
        print("  Different currencies: (A) is wall-clock CPU, (B) is LLM context tokens.")
        print("  The lean-progress rule (auto-memory) was about (B); id=1491 framed it as")
        print("  re-embed, which lives in (A). If embed(delta) ~= embed(large), then within")
        print("  (A) the embed is bounded and not the lever; the lever is (B) the fetch+resend.")
        return 0
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
