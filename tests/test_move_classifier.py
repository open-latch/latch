"""Deterministic epistemic-move classifier — src/move_classifier.py."""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import move_classifier  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _mt(prompt):
    return move_classifier.classify_move(prompt)["move_type"]


def test_diagnosis_detected():
    for p in (
        "why is the vol fit so flat at the wings?",
        "what's causing the skew to blow up?",
        "I think the issue is the clamp — explain why the fit is off",
        "what is the root cause of the PBE-111 flatline?",
    ):
        _assert(_mt(p) == "diagnosis", f"expected diagnosis: {p!r} -> {_mt(p)}")
    print("PASS diagnosis_detected")


def test_hypothesis_detected():
    for p in (
        "maybe the calendar arbitrage filter is too aggressive",
        "could it be the shared-rho constraint?",
        "my guess is the data feed is stale",
    ):
        _assert(_mt(p) == "hypothesis", f"expected hypothesis: {p!r} -> {_mt(p)}")
    print("PASS hypothesis_detected")


def test_investigation_detected():
    for p in (
        "look into why the fitter is dropping the 30y expiry",
        "debug the SSVI calibration step",
        "check whether the config has Phase 2d enabled",
    ):
        _assert(_mt(p) == "investigation", f"expected investigation: {p!r} -> {_mt(p)}")
    print("PASS investigation_detected")


def test_implementation_detected():
    for p in (
        "add a wing-clamp parameter to the fitter",
        "refactor the Skew20 curve fitter",
    ):
        _assert(_mt(p) == "implementation", f"expected implementation: {p!r} -> {_mt(p)}")
    print("PASS implementation_detected")


def test_other_for_neutral_prompt():
    _assert(_mt("show me the latest checkpoint summary") == "other",
            "neutral prompt should be 'other'")
    print("PASS other_for_neutral_prompt")


def test_diagnosis_wins_over_implementation():
    # A prompt with both a "why" and a "fix" verb classifies as the higher-risk
    # diagnosis move.
    _assert(_mt("why is the fit bad, and can you fix it?") == "diagnosis",
            "diagnosis should win priority over implementation")
    print("PASS diagnosis_wins_over_implementation")


def test_matched_substring_returned():
    res = move_classifier.classify_move("why is the fit off?")
    _assert(res["matched"] is not None, "diagnosis match should carry a substring")
    _assert(move_classifier.classify_move("hello there friend")["matched"] is None,
            "'other' should have matched=None")
    print("PASS matched_substring_returned")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} MOVE-CLASSIFIER TESTS PASSED")
