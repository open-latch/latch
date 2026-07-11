"""Tests for fail-closed Cursor gate state and mutation classification."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import cursor_gate_state as cgs  # noqa: E402
import paths  # noqa: E402


def _tmp():
    root = tempfile.mkdtemp(prefix="cursor-gate-state-")
    return root, paths.project_dir(root)


def test_prompt_state_requires_exact_successful_gate_and_resets_each_turn():
    root, project_dir = _tmp()
    try:
        prompt = "Implement the Cursor gate"
        state = cgs.begin_prompt(root, "conversation-1", prompt)
        assert state["prompt_hash"]
        assert prompt not in json.dumps(state)
        assert cgs.mutation_authorized(root, "conversation-1")[0] is False

        armed, detail = cgs.record_gate(
            root, "conversation-1", request=prompt,
            gate_status="OK", recommendation="PROCEED",
        )
        assert armed and detail == "PROCEED"
        assert cgs.mutation_authorized(root, "conversation-1") == (True, "PROCEED")

        next_state = cgs.begin_prompt(root, "conversation-1", "Now update the docs")
        assert next_state["turn"] == 2
        assert next_state["gate_receipt"] is None
        assert cgs.mutation_authorized(root, "conversation-1")[0] is False
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def test_gate_rejects_rephrased_skipped_and_cross_session_receipts():
    root, project_dir = _tmp()
    try:
        cgs.begin_prompt(root, "conversation-1", "Fix the bug verbatim")
        ok, detail = cgs.record_gate(
            root, "conversation-1", request="Fix that bug",
            gate_status="OK", recommendation="PROCEED",
        )
        assert not ok and "verbatim" in detail
        ok, detail = cgs.record_gate(
            root, "conversation-1", request="Fix the bug verbatim",
            gate_status="SKIPPED", recommendation=None,
        )
        assert not ok and "usable verdict" in detail
        ok, detail = cgs.record_gate(
            root, "conversation-2", request="Fix the bug verbatim",
            gate_status="OK", recommendation="PROCEED",
        )
        assert not ok and "session mismatch" in detail
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def test_cursor_payload_fields_and_user_query_normalization():
    payload = {
        "workspace_roots": [{"uri": "file:///tmp/project"}],
        "conversation_id": " conversation ",
        "prompt": "<user_query>\nFix this exactly\n</user_query>",
    }
    assert cgs.project_cwd(payload) == "/tmp/project"
    assert cgs.session_id(payload, "/tmp/project") == "conversation"
    assert cgs.prompt_text(payload) == "Fix this exactly"


def test_mutation_classifier_is_conservative_and_keeps_gate_tools_available():
    mutation_cases = [
        {"tool_name": "Write", "tool_input": {"path": "x"}},
        {"tool_name": "StrReplace", "tool_input": {}},
        {"tool_name": "Shell", "tool_input": {"command": "rm -rf build"}},
        {"tool_name": "Shell", "tool_input": {"command": "git status && rm x"}},
        {"tool_name": "Task", "tool_input": {"readonly": False}},
        {"tool_name": "mcp__filesystem__write_file", "tool_input": {}},
        {"tool_name": "MCP", "tool_input": {"server": "filesystem", "tool": "read_file"}},
        {"tool_name": "NewUnknownTool", "tool_input": {}},
        {"tool_name": "latch_insert", "tool_input": {}},
        {"tool_name": "kb_update", "tool_input": {}},
        {"tool_name": "mcp__latch__latch_append", "tool_input": {}},
        {"tool_name": "mcp__claude-kb__kb_correct_apply", "tool_input": {}},
        {"tool_name": "MCP", "tool_input": {
            "server": "latch", "tool": "latch_link",
        }},
        {"tool_name": "MCP", "tool_input": {
            "serverName": "claude-kb", "toolName": "kb_unlink",
        }},
        {"tool_name": "latch_capture_decision", "tool_input": {}},
        {"tool_name": "latch_priority_add", "tool_input": {}},
        {"tool_name": "latch_priority_reorder", "tool_input": {}},
        {"tool_name": "latch_priority_retire", "tool_input": {}},
        {"tool_name": "latch_future_unknown", "tool_input": {}},
        {},
    ]
    for payload in mutation_cases:
        assert cgs.mutation_capability(payload)[0] is True, payload

    read_cases = [
        {"tool_name": "Read", "tool_input": {"path": "x"}},
        {"tool_name": "Shell", "tool_input": {"command": "git status --short"}},
        {"tool_name": "Shell", "tool_input": {"command": "gh pr view 17 --json state"}},
        {"tool_name": "Shell", "tool_input": {"command": "Get-Content README.md"}},
        {"tool_name": "Task", "tool_input": {"readonly": True}},
        {"tool_name": "mcp__latch__latch_gate", "tool_input": {}},
        {"tool_name": "MCP", "tool_input": {"server": "latch", "tool": "latch_gate"}},
        {"tool_name": "mcp__claude-kb__kb_search", "tool_input": {}},
        {"tool_name": "MCP", "tool_input": {
            "serverName": "claude-kb", "toolName": "kb_correct_plan",
        }},
        {"tool_name": "latch_priority_list", "tool_input": {}},
    ]
    for payload in read_cases:
        assert cgs.mutation_capability(payload)[0] is False, payload


def test_read_only_shell_allowlist_rejects_write_variants():
    assert cgs.read_only_shell_command("git -C /tmp/repo diff --stat")
    assert cgs.read_only_shell_command("git branch --show-current")
    assert cgs.read_only_shell_command("sed -n 1,20p README.md")
    assert not cgs.read_only_shell_command("sed -i s/a/b/ README.md")
    assert not cgs.read_only_shell_command("git branch new-branch")
    assert not cgs.read_only_shell_command("python -m pytest")
    assert not cgs.read_only_shell_command("rg needle . | head")
