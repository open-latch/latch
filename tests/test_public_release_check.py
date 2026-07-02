"""Regression tests for the public release hygiene scanner."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import public_release_check as prc  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_path_policy_blocks_strategy_docs():
    findings = prc.check_path_policy([
        "README.md",
        "docs/first_run_mission.md",
        "docs/launch_strategy.md",
    ])
    _assert(any(f.path == "docs/launch_strategy.md" for f in findings), findings)
    print("PASS path_policy_blocks_strategy_docs")


def test_sensitive_terms_are_blocked():
    findings = prc.scan_text("README.md", "Use /Users/nicomey/project for the paid billing demo.")
    rules = {f.rule for f in findings}
    _assert("personal account or filesystem reference" in rules, findings)
    _assert("paid or billing language" in rules, findings)
    print("PASS sensitive_terms_are_blocked")


def test_code_meta_identifier_is_allowed():
    findings = prc.scan_text("src/example.py", "session_meta = {'id': 'abc'}")
    _assert(findings == [], findings)
    print("PASS code_meta_identifier_is_allowed")


if __name__ == "__main__":
    test_path_policy_blocks_strategy_docs()
    test_sensitive_terms_are_blocked()
    test_code_meta_identifier_is_allowed()
    print("\nAll public release check tests pass.")
