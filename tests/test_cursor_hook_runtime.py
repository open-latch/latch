"""Focused tests for Cursor-native hook payload handling."""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "hooks"))

import cursor_post_tool_use as cptu  # noqa: E402
import cursor_session_start as css  # noqa: E402


def test_cursor_session_fields_and_output_shape(monkeypatch):
    assert css.cursor_project_cwd({"workspace_roots": ["/workspace"]}) == "/workspace"
    assert css.cursor_project_cwd({"workspaceRoot": "/root"}) == "/root"
    assert css.cursor_session_id({"conversation_id": "conversation"}) == "conversation"
    out = io.StringIO()
    with redirect_stdout(out):
        css.emit_cursor_context("brief")
    assert json.loads(out.getvalue()) == {"additional_context": "brief"}


def test_cursor_post_tool_use_reads_cursor_output_shapes():
    response = {
        "result": [{
            "kb_activity": {
                "must_display_to_user": True,
                "summary": "Read decision id=7",
                "label": "Latch read",
            }
        }]
    }
    assert cptu.cursor_surface_message({"tool_output": response}) == \
        "Latch read: Read decision id=7"
    assert cptu.cursor_surface_message({"result_json": json.dumps(response)}) == \
        "Latch read: Read decision id=7"
    assert cptu.cursor_surface_message({"tool_output": {"ok": True}}) is None
