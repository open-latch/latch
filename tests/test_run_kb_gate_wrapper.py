"""Regression tests for the /kb-gate shell wrapper.

The first-wow seed report prints a shell fallback using bin/run_kb_gate.sh.
That path must use latch's configured interpreter instead of a random `python`
on PATH, or a fresh demo can fail before the gate emits a receipt.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

KB_HOME = Path(__file__).resolve().parent.parent
SCRIPT = KB_HOME / "bin" / "run_kb_gate.sh"


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_run_kb_gate_honors_configured_python():
    env = dict(os.environ)
    env.pop("LATCH_PYTHON", None)
    env["CLAUDE_KB_PYTHON"] = "echo"
    r = subprocess.run(
        ["bash", str(SCRIPT), "Revive this rejected path"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(KB_HOME),
    )
    _assert(r.returncode == 0, f"exit {r.returncode}: {r.stderr}")
    _assert(str(KB_HOME / "src" / "kb_gate_cli.py") in r.stdout,
            f"wrapper should exec kb_gate_cli.py, got: {r.stdout}")
    _assert(str(KB_HOME) in r.stdout,
            f"wrapper should pass the current project dir, got: {r.stdout}")
    _assert("Revive this rejected path" in r.stdout,
            f"wrapper should preserve the request args, got: {r.stdout}")
    print("PASS run_kb_gate_honors_configured_python")


if __name__ == "__main__":
    test_run_kb_gate_honors_configured_python()
    print("\nAll run_kb_gate wrapper tests pass.")
