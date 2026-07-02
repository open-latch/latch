"""Deterministic cite-presence detector — src/cite_detector.py (Slice 3-B).

Covers: code-class claim + no cite -> flagged; claim + in-window cite -> clear;
a cite in a DIFFERENT window does not excuse an uncited claim; fenced code
blocks are stripped; bare filename (no line) does NOT count as a cite; neutral
prose isn't flagged; n_claims vs n_flagged counts; empty input.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import cite_detector  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _scan(text):
    return cite_detector.scan_message(text)


def test_uncited_claim_flagged():
    for t in (
        "The EnableBidAskClamp flag is set to false in the deployed config.",
        "The config currently defaults to svi_ssvi_v1 with Phase 2d off.",
        "The loader reads the floating key and silently ignores it.",
        "It's currently hard-coded to 33.",
    ):
        r = _scan(t)
        _assert(r["n_flagged"] == 1, f"expected 1 flagged: {t!r} -> {r}")
        _assert(r["has_uncited_claim"], f"should flag: {t!r}")
    print("PASS uncited_claim_flagged")


def test_in_window_cite_clears():
    for t in (
        "The flag is set to false in `config.toml:42`.",
        "The loader reads the floating key (loader.py:118) and ignores it.",
        "The config defaults to svi_ssvi_v1 — see [config.toml:7](x#L7).",
        "It is hard-coded to 33 at heal.py:205.",
    ):
        r = _scan(t)
        _assert(r["n_claims"] == 1, f"claim should still be detected: {t!r} -> {r}")
        _assert(r["n_flagged"] == 0, f"in-window cite should clear: {t!r} -> {r}")
    print("PASS in_window_cite_clears")


def test_cite_in_other_window_does_not_excuse():
    text = (
        "The EnableBidAskClamp flag is set to false in the deployed config.\n\n"
        "Separately, the fitter reads gate.py:91 for the seed kinds."
    )
    r = _scan(text)
    # Para 1: uncited claim -> flagged. Para 2: claim WITH a cite -> clear.
    _assert(r["n_claims"] == 2, f"two claims expected: {r}")
    _assert(r["n_flagged"] == 1, f"only the uncited para should flag: {r}")
    print("PASS cite_in_other_window_does_not_excuse")


def test_bullets_are_separate_windows():
    text = (
        "Findings:\n"
        "- The clamp flag is set to false.\n"
        "- The model_tag is svi_ssvi_v1 per config.toml:7.\n"
    )
    r = _scan(text)
    _assert(r["n_claims"] == 2, f"two bullet claims: {r}")
    _assert(r["n_flagged"] == 1, f"first bullet uncited, second cited: {r}")
    print("PASS bullets_are_separate_windows")


def test_fenced_code_block_stripped():
    text = (
        "Here is the relevant snippet:\n\n"
        "```python\n"
        "flag = False  # the setting is disabled here\n"
        "model_tag = 'svi_ssvi_v1'\n"
        "```\n\n"
        "That's the whole change."
    )
    r = _scan(text)
    _assert(r["n_claims"] == 0, f"code inside a fence is not a conclusion: {r}")
    _assert(r["n_flagged"] == 0, f"no flag from fenced code: {r}")
    print("PASS fenced_code_block_stripped")


def test_bare_filename_is_not_a_cite():
    # The contract asks for file:line specifically — a vague filename mention a
    # non-technical user can't verify must NOT clear the flag.
    r = _scan("The flag is set to false in config.toml.")
    _assert(r["n_flagged"] == 1, f"bare filename should not count as a cite: {r}")
    print("PASS bare_filename_is_not_a_cite")


def test_neutral_prose_not_flagged():
    for t in (
        "Let me read the deployed config before I say anything about the flag.",
        "I'll check config.toml:42 and report back what I find.",
        "Thanks — that makes sense. I'll start on the fix next.",
        "The plan has three slices; I built the first two last session.",
    ):
        r = _scan(t)
        _assert(r["n_flagged"] == 0, f"should not flag neutral prose: {t!r} -> {r}")
    print("PASS neutral_prose_not_flagged")


def test_empty_and_none():
    for t in ("", "   \n\n  ", None):
        r = _scan(t)
        _assert(r["n_claims"] == 0 and r["n_flagged"] == 0, f"empty -> zeros: {r}")
    print("PASS empty_and_none")


def test_flagged_substrings_returned_for_debug():
    r = _scan("The flag is set to false.")
    _assert(r["flagged"] and isinstance(r["flagged"][0], str),
            f"flagged substrings should be returned for tests/debug: {r}")
    print("PASS flagged_substrings_returned_for_debug")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} CITE-DETECTOR TESTS PASSED")
