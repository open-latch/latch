from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from latch.proof import no_history_demo as demo  # noqa: E402


KB_HOME = Path(__file__).resolve().parent.parent
SCRIPT = KB_HOME / "bin" / "latch_demo_no_history.sh"


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_no_history_fixture_seeds_rule_and_gate_context_without_history():
    root = Path(tempfile.mkdtemp(prefix="latch-no-history-test-"))
    fixture = None
    try:
        fixture = demo.create_fixture(root)
        _assert((fixture.project / "GOVERNANCE.md").exists(),
                "fixture should create a concrete governance file")
        _assert(fixture.kb_dir.exists(), "fixture should use a throwaway KB dir")
        out = demo.run_gate(
            fixture,
            request=demo.DEMO_REQUEST,
            use_llm=False,
            max_chains=3,
            backend=None,
        )
        receipt = demo.render_receipt(
            fixture,
            out,
            request=demo.DEMO_REQUEST,
            keep=True,
            use_llm=False,
        )
        _assert("used no personal Claude/Codex history" in receipt, receipt)
        _assert(f"id={fixture.decision_id}" in receipt, receipt)
        _assert("Offline mode: --no-llm skipped classifier judgment" in receipt, receipt)
        _assert(demo._chain_contains_decision(out, fixture.decision_id),
                f"gate assembly should retrieve seeded decision: {out}")
    finally:
        if fixture is not None:
            demo.cleanup_fixture(fixture)
        else:
            shutil.rmtree(root, ignore_errors=True)
    print("PASS no_history_fixture_seeds_rule_and_gate_context_without_history")


def test_no_history_demo_wrapper_uses_configured_python():
    env = dict(os.environ)
    env["LATCH_PYTHON"] = "echo"
    env.pop("CLAUDE_KB_PYTHON", None)
    r = subprocess.run(
        ["bash", str(SCRIPT), "--no-llm"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(KB_HOME),
    )
    _assert(r.returncode == 0, f"exit {r.returncode}: {r.stderr}")
    _assert(str(KB_HOME / "src" / "latch" / "proof" / "no_history_demo.py") in r.stdout,
            f"wrapper should exec no_history_demo.py, got: {r.stdout}")
    _assert("--no-llm" in r.stdout,
            f"wrapper should preserve args, got: {r.stdout}")
    print("PASS no_history_demo_wrapper_uses_configured_python")


if __name__ == "__main__":
    test_no_history_fixture_seeds_rule_and_gate_context_without_history()
    test_no_history_demo_wrapper_uses_configured_python()
    print("\nAll no-history demo tests pass.")
