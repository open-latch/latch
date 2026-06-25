"""Closed-set validation-guard tests for the kb_capture_decision MCP tool.

Slice 1 of the decision-capture pipeline (KB id=1784 / id=1279 / id=1350). The
three label guards (human_action / confidence_tier / provenance) return BEFORE
any DB write or decision.log emit, so these tests need no KB fixture and have no
side effects. The end-to-end happy path (insert + cited links + decision.log
row) is exercised against an isolated temp KB / on the live MCP, not here.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import mcp_server  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _rejected(result, label):
    """True iff result is a validation rejection naming `label`."""
    return (
        isinstance(result, dict)
        and result.get("ok") is False
        and isinstance(result.get("error"), str)
        and label in result["error"]
    )


def test_rejects_bad_human_action():
    r = mcp_server.kb_capture_decision(
        title="t", body="b", gate_request="req", human_action="bogus")
    _assert(_rejected(r, "human_action"),
            f"bad human_action should be rejected before any write, got {r}")
    print("PASS rejects_bad_human_action")


def test_rejects_bad_confidence_tier():
    r = mcp_server.kb_capture_decision(
        title="t", body="b", gate_request="req",
        human_action="approve", confidence_tier="nope")
    _assert(_rejected(r, "confidence_tier"),
            f"bad confidence_tier should be rejected, got {r}")
    print("PASS rejects_bad_confidence_tier")


def test_rejects_bad_provenance():
    r = mcp_server.kb_capture_decision(
        title="t", body="b", gate_request="req",
        human_action="override", provenance="nope")
    _assert(_rejected(r, "provenance"),
            f"bad provenance should be rejected, got {r}")
    print("PASS rejects_bad_provenance")


def test_guard_label_sets_match_capture_streams():
    # The tool validates against the capture_streams closed sets — pin that the
    # error messages enumerate exactly those, so the guard can't drift from the
    # stream schema (which the correlator depends on).
    cs = mcp_server.capture_streams
    r = mcp_server.kb_capture_decision(
        title="t", body="b", gate_request="req", human_action="bogus")
    for action in cs.HUMAN_ACTIONS:
        _assert(action in r["error"],
                f"guard error should list valid action {action!r}: {r['error']}")
    print("PASS guard_label_sets_match_capture_streams")


if __name__ == "__main__":
    test_rejects_bad_human_action()
    test_rejects_bad_confidence_tier()
    test_rejects_bad_provenance()
    test_guard_label_sets_match_capture_streams()
    print("\nAll kb_capture_decision validation tests pass.")
