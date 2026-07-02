"""Unit tests for the PostToolUse deterministic activity surface.

The hook's job: pull a displayable `kb_activity` / gate `findings` block out of
whatever shape Claude Code hands us in `tool_response`, and render a single
`systemMessage` line — but ONLY when `must_display_to_user` is set. These tests
pin the parsing tolerance (string-wrapped JSON, `{"result": ...}` wrappers,
kb_search's row-0 placement) and the must-display gating.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "hooks"))

import post_tool_use as ptu  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_kb_search_list_shape_string_wrapped():
    # kb_search returns a list; kb_activity rides on row 0; Claude Code hands
    # the whole thing back as a JSON string wrapped under "result".
    activity = {"must_display_to_user": True, "label": "Latch KB activity",
                "summary": "Read 15 KB search result(s)."}
    tr = json.dumps({"result": [{"id": 1, "kb_activity": activity}, {"id": 2}]})
    msg = ptu.surface_message({"tool_response": tr})
    _assert(msg == "Latch KB activity: Read 15 KB search result(s).",
            f"expected rendered search summary, got {msg!r}")


def test_kb_get_dict_shape_already_parsed():
    # kb_get returns a dict with kb_activity at top level; sometimes already parsed.
    activity = {"must_display_to_user": True, "label": "Latch KB activity",
                "summary": "Read KB fact node id=552."}
    msg = ptu.surface_message({"tool_response": {"kb_activity": activity}})
    _assert(msg == "Latch KB activity: Read KB fact node id=552.",
            f"expected rendered get summary, got {msg!r}")


def test_gate_findings_includes_recommendation():
    findings = {"must_display_to_user": True, "label": "Latch gate findings",
                "recommendation": "MODIFY", "summary": "Proceed with three adjustments."}
    tr = json.dumps({"result": {"findings": findings}})
    msg = ptu.surface_message({"tool_response": tr})
    _assert(msg == "Latch gate findings [MODIFY]: Proceed with three adjustments.",
            f"gate line should carry the verdict, got {msg!r}")


def test_gate_findings_appends_latch_receipt():
    findings = {
        "must_display_to_user": True,
        "label": "Latch gate findings",
        "recommendation": "DO_NOT_PROCEED",
        "summary": "This revives a rejected path.",
        "why_it_matters": "Latch ran the gate on this request using cited KB nodes.",
    }
    tr = json.dumps({"result": {"findings": findings}})
    msg = ptu.surface_message({"tool_response": tr})
    _assert(
        msg == (
            "Latch gate findings [DO_NOT_PROCEED]: This revives a rejected path. "
            "— Latch ran the gate on this request using cited KB nodes."
        ),
        f"gate receipt should be appended, got {msg!r}",
    )


def test_why_it_matters_appended_when_distinct():
    activity = {"must_display_to_user": True, "label": "Latch KB activity",
                "summary": "Tracked decision id=9.", "why_it_matters": "Keeps the wedge honest."}
    msg = ptu.surface_message({"tool_response": {"kb_activity": activity}})
    _assert(msg.endswith("— Keeps the wedge honest."),
            f"why_it_matters should be appended, got {msg!r}")


def test_must_display_false_suppresses():
    activity = {"must_display_to_user": False, "label": "x", "summary": "hidden"}
    msg = ptu.surface_message({"tool_response": {"kb_activity": activity}})
    _assert(msg is None, f"must_display_to_user=False must not surface, got {msg!r}")


def test_missing_summary_suppresses():
    activity = {"must_display_to_user": True, "label": "x"}
    msg = ptu.surface_message({"tool_response": {"kb_activity": activity}})
    _assert(msg is None, f"a block with no summary must not surface, got {msg!r}")


def test_no_activity_block_suppresses():
    tr = json.dumps({"result": [{"id": 1}, {"id": 2}]})
    msg = ptu.surface_message({"tool_response": tr})
    _assert(msg is None, f"a result with no kb_activity must not surface, got {msg!r}")


def test_garbage_and_empty_are_safe():
    _assert(ptu.surface_message({"tool_response": "not json {"}) is None,
            "unparseable tool_response must not raise or surface")
    _assert(ptu.surface_message({}) is None, "empty payload must not surface")
    _assert(ptu.surface_message(None) is None, "non-dict payload must not raise")


def test_long_summary_is_truncated():
    activity = {"must_display_to_user": True, "label": "L", "summary": "x" * 2000}
    msg = ptu.surface_message({"tool_response": {"kb_activity": activity}})
    _assert(len(msg) <= ptu._MAX_LEN, f"message should be capped at {ptu._MAX_LEN}, got {len(msg)}")
    _assert(msg.endswith("…"), "truncated message should end with an ellipsis")


if __name__ == "__main__":
    test_kb_search_list_shape_string_wrapped()
    test_kb_get_dict_shape_already_parsed()
    test_gate_findings_includes_recommendation()
    test_gate_findings_appends_latch_receipt()
    test_why_it_matters_appended_when_distinct()
    test_must_display_false_suppresses()
    test_missing_summary_suppresses()
    test_no_activity_block_suppresses()
    test_garbage_and_empty_are_safe()
    test_long_summary_is_truncated()
    print("\nAll post_tool_use tests pass.")
