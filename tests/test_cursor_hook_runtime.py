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
import cursor_pre_tool_use as cpre  # noqa: E402
import cursor_session_start as css  # noqa: E402
import cursor_gate_state as cgs  # noqa: E402
import paths  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402


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


def test_post_tool_use_arms_only_matching_gate_receipt():
    root = tempfile.mkdtemp(prefix="cursor-hook-gate-")
    project_dir = paths.project_dir(root)
    try:
        prompt = "Implement the exact request"
        cgs.begin_prompt(root, "conversation", prompt)
        payload = {
            "workspaceRoot": root,
            "conversation_id": "conversation",
            "tool_output": {
                "request": prompt,
                "gate_status": "OK",
                "verdict": {"recommendation": "PROCEED"},
                "findings": {
                    "must_display_to_user": True,
                    "summary": "Safe to proceed",
                    "recommendation": "PROCEED",
                },
            },
        }
        assert cptu.record_gate_receipt(payload) == (True, "PROCEED")
        assert cgs.mutation_authorized(root, "conversation")[0] is True
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def test_pre_tool_use_denies_before_gate_and_preserves_cursor_permissions_after():
    root = tempfile.mkdtemp(prefix="cursor-hook-gate-")
    project_dir = paths.project_dir(root)
    try:
        prompt = "Implement the exact request"
        cgs.begin_prompt(root, "conversation", prompt)
        write_payload = {
            "workspaceRoot": root,
            "conversation_id": "conversation",
            "tool_name": "Write",
            "tool_input": {"path": "src/x.py", "content": "x"},
        }
        denied = cpre.decision(write_payload)
        assert denied["permission"] == "deny"
        assert "current Cursor prompt" in denied["user_message"]
        assert cpre.decision({**write_payload, "tool_name": "Read"}) == {}

        assert cgs.record_gate(
            root, "conversation", request=prompt,
            gate_status="OK", recommendation="MODIFY",
        )[0]
        assert cpre.decision(write_payload) == {}
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
