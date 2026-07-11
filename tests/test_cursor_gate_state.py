"""Tests for fail-closed Cursor gate state and mutation classification."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "hooks"))

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
        assert not ok and "no current Cursor prompt state" in detail
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


def test_cursor_session_identity_never_falls_back_to_project_marker():
    root, project_dir = _tmp()
    try:
        marker = project_dir / "cursor_session.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"session_id": "other-conversation"}), encoding="utf-8")
        assert cgs.session_id({"workspaceRoot": root}, root) is None
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def test_interleaved_cursor_sessions_fail_closed():
    root, project_dir = _tmp()
    try:
        prompt_a = "Implement request A"
        prompt_b = "Implement request B"
        cgs.begin_prompt(root, "conversation-a", prompt_a)
        assert cgs.record_gate(
            root, "conversation-a", request=prompt_a,
            gate_status="OK", recommendation="PROCEED",
        )[0]

        cgs.begin_prompt(root, "conversation-b", prompt_b)
        assert cgs.mutation_authorized(root, "conversation-a")[0]
        assert not cgs.mutation_authorized(root, "conversation-b")[0]
        assert cgs.record_gate(
            root, "conversation-b", request=prompt_b,
            gate_status="OK", recommendation="PROCEED",
        )[0]
        assert cgs.mutation_authorized(root, "conversation-b")[0]
        assert not cgs.record_gate(
            root, None, request=prompt_b,
            gate_status="OK", recommendation="PROCEED",
        )[0]
        assert not cgs.mutation_authorized(root, None)[0]
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def test_gate_state_atomic_writes_survive_concurrent_hooks():
    root, project_dir = _tmp()
    errors: list[Exception] = []
    start = threading.Barrier(9)

    def writer(index: int) -> None:
        try:
            start.wait()
            for turn in range(50):
                cgs.begin_prompt(root, f"conversation-{index}", f"prompt {index}-{turn}")
        except Exception as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    try:
        threads = [threading.Thread(target=writer, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join()
        assert not errors, errors
        for index in range(8):
            state = cgs.read_state(root, f"conversation-{index}")
            assert isinstance(state, dict)
            assert state.get("session_id") == f"conversation-{index}"
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def test_mutation_classifier_is_conservative_and_keeps_gate_tools_available():
    mutation_cases = [
        {"tool_name": "Write", "tool_input": {"path": "x"}},
        {"tool_name": "StrReplace", "tool_input": {}},
        {"tool_name": "Shell", "tool_input": {"command": "rm -rf build"}},
        {"tool_name": "Shell", "tool_input": {"command": "git status && rm x"}},
        {"tool_name": "Shell", "tool_input": {
            "command": "sed -n '1w /tmp/latch-bypass' README.md",
        }},
        {"tool_name": "Shell", "tool_input": {
            "command": "git diff --output=/tmp/latch-bypass",
        }},
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
        {"tool_name": "SpreadsheetUpdate", "tool_input": {}},
        {},
    ]
    for payload in mutation_cases:
        assert cgs.mutation_capability(payload)[0] is True, payload

    read_cases = [
        {"tool_name": "Read", "tool_input": {"path": "x"}},
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
    assert not cgs.read_only_shell_command("git -C /tmp/repo diff --stat")
    assert not cgs.read_only_shell_command("git branch --show-current")
    assert not cgs.read_only_shell_command("sed -n 1,20p README.md")
    assert not cgs.read_only_shell_command("sed -i s/a/b/ README.md")
    assert not cgs.read_only_shell_command("sed -n '1w /tmp/out' README.md")
    assert not cgs.read_only_shell_command("git diff --output=/tmp/out")
    assert not cgs.read_only_shell_command("git branch new-branch")
    assert not cgs.read_only_shell_command("python -m pytest")
    assert not cgs.read_only_shell_command("rg needle . | head")


def _shell(command: str, root: str, sid: str) -> dict:
    return {
        "workspaceRoot": root,
        "conversation_id": sid,
        "tool_name": "Shell",
        "tool_input": {"command": command},
    }


def test_managed_operation_receipts_are_exact_and_single_use():
    import cursor_pre_tool_use as cpre

    root, project_dir = _tmp()
    sid = "operation-session"
    compact = paths.KB_ROOT / "bin" / "run_cursor_compact_now.sh"
    try:
        cgs.begin_prompt(root, sid, "/latch-compact")
        payload = _shell(f"bash {compact} {sid}", root, sid)
        assert cpre.decision(payload) == {}
        assert cpre.decision(payload)["permission"] == "deny"

        cgs.begin_prompt(root, sid, "/latch-compact")
        wrong_args = _shell(f"bash {compact} {sid} --final", root, sid)
        assert cpre.decision(wrong_args)["permission"] == "deny"
        outside = _shell("bash /tmp/run_cursor_compact_now.sh", root, sid)
        assert cpre.decision(outside)["permission"] == "deny"
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def test_seed_operation_requires_preview_then_explicit_apply():
    import cursor_pre_tool_use as cpre

    root, project_dir = _tmp()
    sid = "seed-session"
    seed = paths.KB_ROOT / "bin" / "latch_seed.sh"
    try:
        cgs.begin_prompt(root, sid, "/latch-seed apply")
        apply_payload = _shell(
            f"bash {seed} --source cursor --cursor-session-id {sid} --apply --yes", root, sid,
        )
        assert cpre.decision(apply_payload)["permission"] == "deny"

        cgs.begin_prompt(root, sid, "/latch-seed")
        preview = _shell(
            f"bash {seed} --source cursor --cursor-session-id {sid}", root, sid,
        )
        assert cpre.decision(preview) == {}
        assert cpre.decision(preview)["permission"] == "deny"

        cgs.begin_prompt(root, sid, "/latch-seed apply")
        assert cpre.decision(apply_payload) == {}
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def test_pm_operation_receipt_allows_only_one_staging_decision_insert():
    import cursor_pre_tool_use as cpre

    root, project_dir = _tmp()
    sid = "pm-session"
    try:
        cgs.begin_prompt(root, sid, "Latch operation id: latch-pm prepare")
        cgs.begin_prompt(root, sid, "/latch-pm apply")
        insert = {
            "workspaceRoot": root,
            "conversation_id": sid,
            "tool_name": "mcp__latch__latch_insert",
            "tool_input": {
                "kind": "decision", "status": "staging",
                "title": "Ruled out path", "body": "Do not use X because Y.",
            },
        }
        assert cpre.decision(insert) == {}
        assert cpre.decision(insert)["permission"] == "deny"

        cgs.begin_prompt(root, sid, "Latch operation id: latch-pm prepare")
        cgs.begin_prompt(root, sid, "/latch-pm apply")
        wrong = {**insert, "tool_name": "mcp__latch__latch_update"}
        assert cpre.decision(wrong)["permission"] == "deny"
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def test_other_managed_operations_match_only_expected_wrappers():
    import cursor_pre_tool_use as cpre

    root, project_dir = _tmp()
    sid = "managed-operation-session"
    cases = [
        ("/latch-gate-report", f"bash {paths.KB_ROOT / 'bin' / 'latch_gate_report.sh'}"),
        ("/latch-budget-approve", f"python {paths.KB_ROOT / 'src' / 'budget.py'} approve {root}"),
        ("/latch-decay", f"python {paths.KB_ROOT / 'src' / 'maintenance.py'} weekly {root}"),
        ("/latch-heal", f"python {paths.KB_ROOT / 'src' / 'maintenance.py'} nightly {root}"),
        ("/latch-tree", f"python {paths.KB_ROOT / 'src' / 'maintenance.py'} tree {root}"),
    ]
    try:
        for prompt, command in cases:
            cgs.begin_prompt(root, sid, prompt)
            assert cpre.decision(_shell(command, root, sid)) == {}, prompt

        cgs.begin_prompt(root, sid, "/unlatch")
        unlatch = paths.KB_ROOT / "bin" / "unlatch.sh"
        assert cpre.decision(_shell(f"bash {unlatch}", root, sid)) == {}
        cgs.begin_prompt(root, sid, "unlatch")
        assert cpre.decision(_shell(f"bash {unlatch} --confirm unlatch", root, sid)) == {}

        cgs.begin_prompt(root, sid, "Latch operation id: latch-compact run")
        compact = paths.KB_ROOT / "bin" / "run_cursor_compact_now.sh"
        assert cpre.decision(_shell(f"bash {compact} {sid}", root, sid)) == {}

        cgs.begin_prompt(root, sid, "Explain the status")
        assert cpre.decision(_shell(f"bash {compact} {sid}", root, sid))["permission"] == "deny"
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
