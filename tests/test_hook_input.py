"""Regression coverage for shared hook stdin decoding."""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "hooks"))

import _common as hook_common  # noqa: E402
import cursor_pre_tool_use  # noqa: E402
import cursor_post_tool_use  # noqa: E402


def _windows_stdin(payload: bytes) -> io.TextIOWrapper:
    """Model Windows Cursor's UTF-8 pipe behind a cp1252 text wrapper."""
    return io.TextIOWrapper(
        io.BytesIO(payload),
        encoding="cp1252",
        errors="surrogateescape",
    )


def test_read_hook_input_decodes_windows_utf8_bom(monkeypatch):
    payload = {
        "conversation_id": "conversation",
        "tool_name": "MCP:latch_recent",
        "tool_input": {"limit": 5},
    }
    encoded = b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8") + b"\n"
    monkeypatch.setattr(sys, "stdin", _windows_stdin(encoded))

    assert hook_common.read_hook_input() == payload


def test_read_hook_input_decodes_bom_free_utf8_not_console_locale(monkeypatch):
    payload = {"prompt": "naive caf\u00e9 \u2713"}
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", _windows_stdin(encoded))

    assert hook_common.read_hook_input() == payload


def test_read_hook_input_accepts_unicode_bom_in_text_only_stream(monkeypatch):
    payload = {"session_id": "session"}
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO("\ufeff" + json.dumps(payload)),
    )

    assert hook_common.read_hook_input() == payload


def test_read_hook_input_fails_closed_on_invalid_utf8(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _windows_stdin(b"\xff\xfe{"))

    assert hook_common.read_hook_input() == {}


def test_read_hook_input_fails_closed_on_malformed_json(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _windows_stdin(b"\xef\xbb\xbfnot-json"))

    assert hook_common.read_hook_input() == {}


def test_cursor_post_tool_use_arms_gate_receipt_from_bom_payload(monkeypatch):
    """Regression: postToolUse must use the BOM-safe reader. Cursor's BOM-
    prefixed latch_gate result previously failed json.loads at char 0, so the
    gate receipt never armed and post-gate Write/Shell stayed denied."""
    prompt = "add cusip helper"
    gate_inner = json.dumps(
        {"request": prompt, "gate_status": "OK", "verdict": {"recommendation": "PROCEED"}}
    )
    tool_output = json.dumps({"content": [{"type": "text", "text": gate_inner}], "isError": False})
    payload = {
        "conversation_id": "conversation",
        "session_id": "conversation",
        "tool_name": "MCP:latch_gate",
        "tool_input": {"request": prompt},
        "tool_output": tool_output,
        "workspace_roots": ["/C:/Users/test/latch-project"],
        "cursor_version": "3.11.19",
    }
    encoded = b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8") + b"\n"
    monkeypatch.setattr(sys, "stdin", _windows_stdin(encoded))

    captured: dict = {}

    def fake_record_gate(cwd, sid, *, request, gate_status, recommendation):
        captured.update(request=request, gate_status=gate_status, recommendation=recommendation)
        return True, recommendation

    monkeypatch.setattr(cursor_post_tool_use.cursor_gate_state, "record_gate", fake_record_gate)

    with redirect_stdout(io.StringIO()):
        assert cursor_post_tool_use.main() == 0

    assert captured == {"request": prompt, "gate_status": "OK", "recommendation": "PROCEED"}


def test_cursor_pre_tool_use_allows_bom_prefixed_latch_read(monkeypatch):
    payload = {
        "workspace_roots": ["/C:/Users/test/latch-project"],
        "conversation_id": "conversation",
        "session_id": "conversation",
        "tool_name": "MCP:latch_recent",
        "tool_input": {"limit": 5},
        "cursor_version": "3.11.19",
    }
    encoded = b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8") + b"\n"
    monkeypatch.setattr(sys, "stdin", _windows_stdin(encoded))
    monkeypatch.setattr(cursor_pre_tool_use, "is_disabled", lambda: False)
    monkeypatch.setattr(cursor_pre_tool_use, "is_in_compact", lambda: False)
    monkeypatch.setattr(cursor_pre_tool_use, "is_unlatched_mode", lambda: False)

    output = io.StringIO()
    with redirect_stdout(output):
        assert cursor_pre_tool_use.main() == 0

    assert json.loads(output.getvalue()) == {}
