"""Regression tests for the /latch-gate and legacy /kb-gate shell wrappers.

The first-wow seed report prints a shell fallback using bin/run_latch_gate.sh.
Both paths must use latch's configured interpreter instead of a random `python`
on PATH, or a fresh demo can fail before the gate emits a receipt.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

KB_HOME = Path(__file__).resolve().parent.parent
SCRIPTS = [
    KB_HOME / "bin" / "run_latch_gate.sh",
    KB_HOME / "bin" / "run_kb_gate.sh",
]


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_gate_wrappers_honor_configured_python():
    env = dict(os.environ)
    env.pop("LATCH_PYTHON", None)
    env["CLAUDE_KB_PYTHON"] = "echo"
    for script in SCRIPTS:
        r = subprocess.run(
            ["bash", str(script), "Revive this rejected path"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(KB_HOME),
        )
        _assert(r.returncode == 0, f"{script.name} exit {r.returncode}: {r.stderr}")
        _assert(str(KB_HOME / "src" / "kb_gate_cli.py") in r.stdout,
                f"{script.name} should exec kb_gate_cli.py, got: {r.stdout}")
        _assert(str(KB_HOME) in r.stdout,
                f"{script.name} should pass the current project dir, got: {r.stdout}")
        _assert("Revive this rejected path" in r.stdout,
                f"{script.name} should preserve the request args, got: {r.stdout}")
    print("PASS gate_wrappers_honor_configured_python")


def test_legacy_wrapper_does_not_require_latch_wrapper_executable_bit():
    target = KB_HOME / "bin" / "run_latch_gate.sh"
    legacy = KB_HOME / "bin" / "run_kb_gate.sh"
    original_mode = target.stat().st_mode
    env = dict(os.environ)
    env.pop("LATCH_PYTHON", None)
    env["CLAUDE_KB_PYTHON"] = "echo"
    try:
        target.chmod(original_mode & ~0o111)
        r = subprocess.run(
            ["bash", str(legacy), "Revive this rejected path"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(KB_HOME),
        )
    finally:
        target.chmod(original_mode)
    _assert(r.returncode == 0, f"{legacy.name} exit {r.returncode}: {r.stderr}")
    _assert(str(KB_HOME / "src" / "kb_gate_cli.py") in r.stdout,
            f"{legacy.name} should still delegate through bash: {r.stdout}")
    print("PASS legacy_wrapper_does_not_require_latch_wrapper_executable_bit")


if __name__ == "__main__":
    test_gate_wrappers_honor_configured_python()
    test_legacy_wrapper_does_not_require_latch_wrapper_executable_bit()
    print("\nAll gate wrapper tests pass.")
