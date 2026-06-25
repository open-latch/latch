"""End-to-end latency check for the UserPromptSubmit hook against a warm daemon.

Spawns the embed listener in this process, pre-warms the model, then fires
the user_prompt_submit.py hook as a fresh subprocess (the way Claude Code
invokes it) and measures wall-clock. Validates the Phase 1 fix took the hook
from ~19.5s (fact 346) to under HARD_BUDGET_MS = 250ms.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import db  # noqa: E402
import embeddings  # noqa: E402
import log_utils  # noqa: E402
import mcp_server  # noqa: E402
import paths  # noqa: E402


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="kb_latency_")
    print(f"project: {tmp}")

    # Seed a small KB so retrieval has something to work with.
    conn = db.connect(tmp)
    try:
        for i in range(5):
            v = embeddings.embed(f"seed body {i}")
            db.insert_node(
                conn, kind="fact",
                title=f"seed fact {i}", body=f"some body about thing {i}",
                status="canonical", embedding=embeddings.to_blob(v),
            )
    finally:
        conn.close()

    mcp_server._start_embed_listener(tmp)

    # Synchronous warm-up on the main thread (matches the new __main__
    # flow in mcp_server.py — see notes there about the daemon-thread
    # deadlock that motivated this change).
    t_warm = time.perf_counter()
    embeddings.embed("latch embed pre-warm")
    print(f"warm-up: {(time.perf_counter() - t_warm) * 1000:.0f} ms")

    if embeddings.embed_remote("ready?", project_cwd=tmp, timeout=5.0) is None:
        print("FAIL: daemon never came up", file=sys.stderr)
        return 1

    hook_path = _SRC / "hooks" / "user_prompt_submit.py"
    py = sys.executable
    payloads = [
        {"session_id": "perf-1", "cwd": tmp, "prompt": "what do we know about thing 2"},
        {"session_id": "perf-1", "cwd": tmp, "prompt": "tell me more about that"},
        {"session_id": "perf-1", "cwd": tmp, "prompt": "and what was thing 4 about"},
    ]
    elapsed = []
    for p in payloads:
        t0 = time.perf_counter()
        proc = subprocess.run(
            [py, str(hook_path)],
            input=json.dumps(p).encode("utf-8"),
            capture_output=True,
            timeout=30,
            env={**os.environ, "PYTHONPATH": str(_SRC)},
        )
        dt_ms = (time.perf_counter() - t0) * 1000
        elapsed.append(dt_ms)
        print(f"hook elapsed: {dt_ms:.1f} ms  rc={proc.returncode}")
        if proc.stderr:
            sys.stderr.write(proc.stderr.decode("utf-8", errors="replace"))

    log = log_utils.today_log_path("retrieve", tmp)
    if log.exists():
        print(f"\nretrieve log ({log.name}):")
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                print(f"  inner elapsed_ms={d.get('elapsed_ms')} path={d.get('path')} "
                      f"injected={len(d.get('injected', []) or [])} skip={d.get('skip')}")
            except ValueError:
                print(f"  (bad line) {line}")

    budget_ms = 250
    over = [e for e in elapsed if e > budget_ms]
    print(f"\nbudget: {budget_ms} ms")
    print(f"elapsed (wall): min={min(elapsed):.1f}  max={max(elapsed):.1f}  median={sorted(elapsed)[len(elapsed)//2]:.1f}")
    print(f"over budget: {len(over)} / {len(elapsed)}")
    return 0 if not over else 1


if __name__ == "__main__":
    sys.exit(main())
