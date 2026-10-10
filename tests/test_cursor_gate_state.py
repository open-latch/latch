"""Tests for fail-closed Cursor gate state and mutation classification."""
from __future__ import annotations

import json
import shlex
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "hooks"))

from latch.hosts import cursor_gate_state as cgs  # noqa: E402
from latch.store import paths  # noqa: E402


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
        shutil.rmtree(root, ignore_errors=True)


def test_session_start_initialization_preserves_a_concurrent_first_prompt():
    root, project_dir = _tmp()
    try:
        prompt = "Implement the exact request"
        started = cgs.begin_prompt(root, "conversation", prompt)

        initialized = cgs.initialize_session(root, "conversation")

        assert initialized == started
        assert initialized["turn"] == 1
        assert initialized["prompt_hash"] == cgs.prompt_hash(prompt)
    finally:
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
        {"tool_name": "MCP:latch_insert", "tool_input": {}},
        {"tool_name": "MCP:filesystem_read_file", "tool_input": {}},
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
        {"tool_name": "Read", "toolName": "Write", "tool_input": {}},
        {"tool_name": "Read", "toolName": "Shell",
         "tool_input": {"command": "rm x"}},
        {"tool_name": "Task", "tool_input": {"readonly": True},
         "toolInput": {"readonly": False}},
        {"tool_name": "Read", "toolName": "MCP",
         "toolInput": {"server": "latch", "tool": "latch_insert"}},
        {"tool_name": "Read", "toolName": "MCP",
         "toolInput": {"server": "filesystem", "tool": "write_file"}},
        {"tool_name": "evil_mcp",
         "toolInput": {"server": "latch", "tool": "latch_search"}},
        {},
    ]
    for payload in mutation_cases:
        assert cgs.mutation_capability(payload)[0] is True, payload

    read_cases = [
        {"tool_name": "Read", "tool_input": {"path": "x"}},
        {"tool_name": "Task", "tool_input": {"readonly": True}},
        {"tool_name": "mcp__latch__latch_gate", "tool_input": {}},
        {"tool_name": "MCP:latch_gate", "tool_input": {}},
        {"tool_name": "MCP:latch_search", "tool_input": {"query": "x"}},
        {"tool_name": "MCP", "tool_input": {"server": "latch", "tool": "latch_gate"}},
        {"tool_name": "mcp__claude-kb__kb_search", "tool_input": {}},
        {"tool_name": "MCP", "tool_input": {
            "serverName": "claude-kb", "toolName": "kb_correct_plan",
        }},
        {"tool_name": "latch_priority_list", "tool_input": {}},
        {"tool_name": "mcp__latch__latch_pm_preview", "tool_input": {}},
        {"tool_name": "Read", "toolName": "read",
         "tool_input": {"path": "x"}, "toolInput": {"path": "x"}},
        {"tool_name": "latch_search", "toolName": "MCP",
         "toolInput": {"server": "latch", "tool": "latch_search"}},
        {"tool_name": "mcp__latch__latch_search", "toolName": "MCP",
         "toolInput": {"server": "latch", "tool": "latch_search"}},
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
    from latch.hooks import cursor_pre_tool_use as cpre

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

        cgs.begin_prompt(root, sid, "/latch-compact")
        compact_ps1 = paths.KB_ROOT / "bin" / "run_cursor_compact_now.ps1"
        powershell = (
            '$env:LATCH_COMPACTOR_BACKEND = "cursor"\n'
            '$env:LATCH_MODEL_BACKEND = "cursor"\n'
            f'& "{compact_ps1}" "{sid}"'
        )
        assert cpre.decision(_shell(powershell, root, sid)) == {}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_managed_operations_reject_attacker_interpreter_paths(monkeypatch):
    from latch.hooks import cursor_pre_tool_use as cpre

    root, project_dir = _tmp()
    sid = "trusted-launcher-session"
    maintenance = paths.KB_ROOT / "src" / "latch" / "pipeline" / "maintenance.py"
    compact = paths.KB_ROOT / "bin" / "run_cursor_compact_now.sh"
    compact_ps1 = paths.KB_ROOT / "bin" / "run_cursor_compact_now.ps1"
    try:
        attacks = [
            ("/latch-heal", f"/tmp/python {maintenance} nightly {root}"),
            ("/latch-heal", f"python3 {maintenance} nightly {root}"),
            ("/latch-compact", f"/tmp/bash {compact} {sid}"),
            ("/latch-heal", f"C:/Temp/python.exe {maintenance} nightly {root}"),
            ("/latch-compact", f"C:/Temp/powershell.exe {compact_ps1} {sid}"),
        ]
        for prompt, command in attacks:
            cgs.begin_prompt(root, sid, prompt)
            assert cpre.decision(_shell(command, root, sid))["permission"] == "deny"

        cgs.begin_prompt(root, sid, "/latch-heal")
        trusted_python = shlex.quote(sys.executable)
        assert cpre.decision(_shell(
            f"{trusted_python} {maintenance} nightly {root}", root, sid,
        )) == {}

        configured = Path(root) / "configured-python"
        monkeypatch.setenv("LATCH_PYTHON", str(configured))
        cgs.begin_prompt(root, sid, "/latch-heal")
        assert cpre.decision(_shell(
            f"{configured} {maintenance} nightly {root}", root, sid,
        )) == {}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_seed_operation_requires_preview_then_explicit_apply():
    from latch.hooks import cursor_post_tool_use as cpost
    from latch.hooks import cursor_pre_tool_use as cpre

    root, project_dir = _tmp()
    sid = "seed-session"
    preview_digest = "a" * 64
    seed = paths.KB_ROOT / "bin" / "latch_seed.sh"
    try:
        cgs.begin_prompt(root, sid, "/latch-seed apply all")
        apply_payload = _shell(
            f"bash {seed} --source cursor --cursor-session-id {sid} "
            f"--format json --preview-digest {preview_digest} --apply --yes", root, sid,
        )
        assert cpre.decision(apply_payload)["permission"] == "deny"

        cgs.begin_prompt(root, sid, "/latch-seed")
        untrusted_python = _shell(
            f"LATCH_PYTHON=/tmp/python bash {seed} --source cursor "
            f"--cursor-session-id {sid} --format json", root, sid,
        )
        assert cpre.decision(untrusted_python)["permission"] == "deny"

        cgs.begin_prompt(root, sid, "/latch-seed")
        path_python = _shell(
            f"LATCH_PYTHON=python3 bash {seed} --source cursor "
            f"--cursor-session-id {sid} --format json", root, sid,
        )
        assert cpre.decision(path_python)["permission"] == "deny"

        cgs.begin_prompt(root, sid, "/latch-seed")
        preview = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed} --source cursor "
            f"--cursor-session-id {sid} --format json", root, sid,
        )
        assert cpre.decision(preview) == {}
        assert cpre.decision(preview)["permission"] == "deny"

        cgs.begin_prompt(root, sid, "/latch-seed apply all")
        apply_payload = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed} --source cursor --cursor-session-id {sid} "
            f"--format json --preview-digest {preview_digest} --apply --yes", root, sid,
        )
        assert cpre.decision(apply_payload)["permission"] == "deny"

        cgs.begin_prompt(root, sid, "/latch-seed")
        assert cpre.decision(preview) == {}
        success = {
            **preview,
            "tool_output": json.dumps({
                "ok": True, "source": "cursor", "apply": False,
                "project": str(Path(root).resolve()), "candidates": [],
                "preview_digest": preview_digest,
            }),
        }
        assert cpost.record_operation_success(success) == (
            True, "verified successful seed preview",
        )
        cgs.begin_prompt(root, sid, "/latch-seed apply all")
        assert cpre.decision(apply_payload) == {}

        for output in (
            None,
            "not json",
            {"exit_code": 1, "stdout": json.dumps({
                "ok": True, "source": "cursor", "apply": False,
                "project": str(Path(root).resolve()), "candidates": [],
            })},
            {"ok": True, "source": "cursor", "apply": False,
             "project": str(Path(root) / "other-project"), "candidates": []},
            {"status": "cancelled", "result": json.loads(success["tool_output"])},
            {"status": "timeout", "result": json.loads(success["tool_output"])},
            {"ok": False, "result": json.loads(success["tool_output"])},
            {"cancelled": True, "result": json.loads(success["tool_output"])},
            {"status": "skipped", "result": json.loads(success["tool_output"])},
            {"ok": 0, "result": json.loads(success["tool_output"])},
        ):
            cgs.begin_prompt(root, sid, "/latch-seed")
            assert cpre.decision(preview) == {}
            failed = {**preview, "tool_output": output}
            recorded = cpost.record_operation_success(failed)
            assert recorded is not None and recorded[0] is False
            cgs.begin_prompt(root, sid, "/latch-seed apply all")
            assert cpre.decision(apply_payload)["permission"] == "deny"

        cgs.begin_prompt(root, sid, "/latch-seed")
        unexecuted = {**preview, "tool_output": success["tool_output"]}
        assert cpost.record_operation_success(unexecuted) is None
        cgs.begin_prompt(root, sid, "/latch-seed apply all")
        assert cpre.decision(apply_payload)["permission"] == "deny"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_seed_operation_binds_scoped_apply_ids_to_preview():
    from latch.hooks import cursor_post_tool_use as cpost
    from latch.hooks import cursor_pre_tool_use as cpre

    root, project_dir = _tmp()
    seed_script = paths.KB_ROOT / "bin" / "latch_seed.sh"
    preview_digest = "b" * 64
    candidate_id = "cand-" + "c" * 12
    other_candidate_id = "cand-" + "e" * 12
    cluster_id = "cluster-" + "d" * 12
    other_cluster_id = "cluster-" + "f" * 12

    def arm_preview(sid: str):
        cgs.begin_prompt(root, sid, "/latch-seed")
        preview = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {sid} --format json",
            root,
            sid,
        )
        assert cpre.decision(preview) == {}
        success = {
            **preview,
            "tool_output": json.dumps({
                "ok": True,
                "source": "cursor",
                "apply": False,
                "project": str(Path(root).resolve()),
                "candidates": [
                    {"review_id": candidate_id},
                    {"review_id": other_candidate_id},
                ],
                "review_clusters": [
                    {"cluster_id": cluster_id},
                    {"cluster_id": other_cluster_id},
                ],
                "preview_digest": preview_digest,
            }),
        }
        assert cpost.record_operation_success(success) == (
            True,
            "verified successful seed preview",
        )

    try:
        allowed_sid = "seed-scoped-allowed"
        arm_preview(allowed_sid)
        state = cgs.begin_prompt(
            root,
            allowed_sid,
            f"/latch-seed apply {candidate_id} {cluster_id}",
        )
        assert state["operation_receipt"]["selection_mode"] == "scoped"
        assert state["operation_receipt"]["candidate_ids"] == [candidate_id]
        assert state["operation_receipt"]["cluster_ids"] == [cluster_id]
        missing_confirmed_id = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {allowed_sid} --format json "
            f"--preview-digest {preview_digest} --apply "
            f"--approve-candidate {candidate_id}",
            root,
            allowed_sid,
        )
        assert cpre.decision(missing_confirmed_id)["permission"] == "deny"
        scoped = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {allowed_sid} --format json "
            f"--preview-digest {preview_digest} --apply "
            f"--approve-candidate {candidate_id} --approve-cluster {cluster_id}",
            root,
            allowed_sid,
        )
        assert cpre.decision(scoped) == {}
        assert cpre.decision(scoped)["permission"] == "deny"

        mismatch_sid = "seed-scoped-mismatch"
        arm_preview(mismatch_sid)
        cgs.begin_prompt(
            root,
            mismatch_sid,
            f"/latch-seed apply {candidate_id}",
        )
        wrong_preview_member = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {mismatch_sid} --format json "
            f"--preview-digest {preview_digest} --apply "
            f"--approve-candidate {other_candidate_id}",
            root,
            mismatch_sid,
        )
        assert cpre.decision(wrong_preview_member)["permission"] == "deny"

        extra_preview_member = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {mismatch_sid} --format json "
            f"--preview-digest {preview_digest} --apply "
            f"--approve-candidate {candidate_id} "
            f"--approve-candidate {other_candidate_id}",
            root,
            mismatch_sid,
        )
        assert cpre.decision(extra_preview_member)["permission"] == "deny"

        candidate_to_cluster_substitution = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {mismatch_sid} --format json "
            f"--preview-digest {preview_digest} --apply "
            f"--approve-cluster {cluster_id}",
            root,
            mismatch_sid,
        )
        assert cpre.decision(
            candidate_to_cluster_substitution
        )["permission"] == "deny"

        reverse_substitution_sid = "seed-scoped-reverse-substitution"
        arm_preview(reverse_substitution_sid)
        cgs.begin_prompt(
            root,
            reverse_substitution_sid,
            f"/latch-seed apply {cluster_id}",
        )
        cluster_to_candidate_substitution = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {reverse_substitution_sid} "
            f"--format json --preview-digest {preview_digest} --apply "
            f"--approve-candidate {candidate_id}",
            root,
            reverse_substitution_sid,
        )
        assert cpre.decision(
            cluster_to_candidate_substitution
        )["permission"] == "deny"

        duplicate_selector = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {mismatch_sid} --format json "
            f"--preview-digest {preview_digest} --apply "
            f"--approve-candidate {candidate_id} "
            f"--approve-candidate {candidate_id}",
            root,
            mismatch_sid,
        )
        assert cpre.decision(duplicate_selector)["permission"] == "deny"

        whole_after_scoped = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {mismatch_sid} --format json "
            f"--preview-digest {preview_digest} --apply --yes",
            root,
            mismatch_sid,
        )
        assert cpre.decision(whole_after_scoped)["permission"] == "deny"

        wrong_digest = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {mismatch_sid} --format json "
            f"--preview-digest {'9' * 64} --apply "
            f"--approve-candidate {candidate_id}",
            root,
            mismatch_sid,
        )
        assert cpre.decision(wrong_digest)["permission"] == "deny"

        wrong_session = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id another-session --format json "
            f"--preview-digest {preview_digest} --apply "
            f"--approve-candidate {candidate_id}",
            root,
            mismatch_sid,
        )
        assert cpre.decision(wrong_session)["permission"] == "deny"

        extra_flag = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {mismatch_sid} --format json "
            f"--preview-digest {preview_digest} --apply "
            f"--approve-candidate {candidate_id} --force-reimport",
            root,
            mismatch_sid,
        )
        assert cpre.decision(extra_flag)["permission"] == "deny"

        unknown_sid = "seed-scoped-unknown"
        arm_preview(unknown_sid)
        unknown_id = "cand-" + "0" * 12
        state = cgs.begin_prompt(
            root,
            unknown_sid,
            f"/latch-seed apply {unknown_id}",
        )
        assert state["operation_receipt"] is None
        unknown = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {unknown_sid} --format json "
            f"--preview-digest {preview_digest} --apply "
            f"--approve-candidate {unknown_id}",
            root,
            unknown_sid,
        )
        assert cpre.decision(unknown)["permission"] == "deny"

        whole_sid = "seed-whole-allowed"
        arm_preview(whole_sid)
        state = cgs.begin_prompt(root, whole_sid, "/latch-seed apply all")
        assert state["operation_receipt"]["selection_mode"] == "all"
        assert state["operation_receipt"]["candidate_ids"] == []
        assert state["operation_receipt"]["cluster_ids"] == []
        whole = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {whole_sid} --format json "
            f"--preview-digest {preview_digest} --apply --yes",
            root,
            whole_sid,
        )
        assert cpre.decision(whole) == {}

        scoped_after_whole_sid = "seed-whole-mismatch"
        arm_preview(scoped_after_whole_sid)
        cgs.begin_prompt(root, scoped_after_whole_sid, "/latch-seed apply all")
        scoped_after_whole = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {scoped_after_whole_sid} "
            f"--format json --preview-digest {preview_digest} --apply "
            f"--approve-candidate {candidate_id}",
            root,
            scoped_after_whole_sid,
        )
        assert cpre.decision(scoped_after_whole)["permission"] == "deny"

        powershell_sid = "seed-scoped-powershell"
        arm_preview(powershell_sid)
        cgs.begin_prompt(
            root,
            powershell_sid,
            f"/latch-seed apply {other_cluster_id}",
        )
        seed_ps1 = paths.KB_ROOT / "bin" / "latch_seed.ps1"
        powershell_scoped = _shell(
            f'$env:LATCH_PYTHON = "{sys.executable}"\n'
            f'& "{seed_ps1}" --source cursor '
            f'--cursor-session-id "{powershell_sid}" --format json '
            f'--preview-digest "{preview_digest}" --apply '
            f'--approve-cluster "{other_cluster_id}"',
            root,
            powershell_sid,
        )
        assert cpre.decision(powershell_scoped) == {}

        for invalid_confirmation in (
            "/latch-seed apply",
            f"/latch-seed apply all {candidate_id}",
            f"/latch-seed apply none {candidate_id}",
            "/latch-seed apply none none",
            f"/latch-seed apply {candidate_id} {candidate_id}",
            "/latch-seed apply cand-not-a-review-id",
        ):
            invalid_sid = "seed-invalid-" + str(abs(hash(invalid_confirmation)))
            arm_preview(invalid_sid)
            state = cgs.begin_prompt(root, invalid_sid, invalid_confirmation)
            assert state["operation_receipt"] is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_seed_operation_binds_reject_all_to_nonempty_preview():
    from latch.hooks import cursor_post_tool_use as cpost
    from latch.hooks import cursor_pre_tool_use as cpre

    root, project_dir = _tmp()
    seed_script = paths.KB_ROOT / "bin" / "latch_seed.sh"
    seed_ps1 = paths.KB_ROOT / "bin" / "latch_seed.ps1"
    preview_digest = "7" * 64
    candidate_id = "cand-" + "8" * 12

    def arm_preview(sid: str, candidates: list[dict[str, str]]):
        cgs.begin_prompt(root, sid, "/latch-seed")
        preview = _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {sid} --format json",
            root,
            sid,
        )
        assert cpre.decision(preview) == {}
        success = {
            **preview,
            "tool_output": json.dumps({
                "ok": True,
                "source": "cursor",
                "apply": False,
                "project": str(Path(root).resolve()),
                "candidates": candidates,
                "preview_digest": preview_digest,
            }),
        }
        assert cpost.record_operation_success(success) == (
            True,
            "verified successful seed preview",
        )

    def dismiss_payload(sid: str, suffix: str):
        return _shell(
            f"LATCH_PYTHON={sys.executable} bash {seed_script} "
            f"--source cursor --cursor-session-id {sid} --format json "
            f"--preview-digest {preview_digest} --apply {suffix}".rstrip(),
            root,
            sid,
        )

    try:
        allowed_sid = "seed-none-allowed"
        arm_preview(allowed_sid, [{"review_id": candidate_id}])
        state = cgs.begin_prompt(root, allowed_sid, "/latch-seed apply none")
        receipt = state["operation_receipt"]
        assert receipt["selection_mode"] == "none"
        assert receipt["candidate_ids"] == []
        assert receipt["cluster_ids"] == []

        for suffix in (
            "",
            "--yes",
            f"--approve-candidate {candidate_id}",
            "--dismiss-all --yes",
            f"--dismiss-all --approve-candidate {candidate_id}",
            "--dismiss-all --force-reimport",
        ):
            assert cpre.decision(
                dismiss_payload(allowed_sid, suffix)
            )["permission"] == "deny"

        exact = dismiss_payload(allowed_sid, "--dismiss-all")
        assert cpre.decision(exact) == {}
        assert cpre.decision(exact)["permission"] == "deny"

        empty_sid = "seed-none-empty"
        arm_preview(empty_sid, [])
        state = cgs.begin_prompt(root, empty_sid, "/latch-seed apply none")
        assert state["operation_receipt"] is None
        assert cpre.decision(
            dismiss_payload(empty_sid, "--dismiss-all")
        )["permission"] == "deny"

        powershell_sid = "seed-none-powershell"
        arm_preview(powershell_sid, [{"review_id": candidate_id}])
        cgs.begin_prompt(root, powershell_sid, "/latch-seed apply none")
        powershell_none = _shell(
            f'$env:LATCH_PYTHON = "{sys.executable}"\n'
            f'& "{seed_ps1}" --source cursor '
            f'--cursor-session-id "{powershell_sid}" --format json '
            f'--preview-digest "{preview_digest}" --apply --dismiss-all',
            root,
            powershell_sid,
        )
        assert cpre.decision(powershell_none) == {}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_pm_operation_receipt_binds_exact_previewed_content():
    from latch.hooks import cursor_post_tool_use as cpost
    from latch.hooks import cursor_pre_tool_use as cpre

    root, project_dir = _tmp()
    sid = "pm-session"
    try:
        candidate = {
            "kind": "decision", "status": "staging",
            "title": "Ruled out path", "body": "Do not use X because Y.",
            "links": [
                {"relation": "constrains", "dst": 9},
                {"dst": "7", "relation": "related_to"},
            ],
            "workstream_id": 1369,
        }
        cgs.begin_prompt(root, sid, "/latch-pm apply")
        unprepared = {
            "workspaceRoot": root,
            "conversation_id": sid,
            "tool_name": "mcp__latch__latch_insert",
            "tool_input": candidate,
        }
        assert cpre.decision(unprepared)["permission"] == "deny"

        cgs.begin_prompt(root, sid, "Latch operation id: latch-pm prepare")
        preview = cgs.pm_preview_payload(candidate)
        preview_call = {
            "workspaceRoot": root,
            "conversation_id": sid,
            "tool_name": "mcp__latch__latch_pm_preview",
            "tool_input": candidate,
            "tool_output": preview,
        }
        assert cgs.mutation_capability(preview_call)[0] is False
        assert cpost.record_operation_success(preview_call) == (
            True, "verified PM preview and bound candidate digest",
        )
        cgs.begin_prompt(root, sid, "/latch-pm apply")
        insert = {
            **unprepared,
            "tool_input": {
                "body": candidate["body"],
                "title": candidate["title"],
                "status": "staging",
                "kind": "decision",
                "workstream_id": 1369,
                "links": list(reversed(candidate["links"])),
            },
        }
        variations = [
            {**insert, "tool_name": "mcp__latch__latch_update"},
            {**insert, "tool_input": {**insert["tool_input"], "title": "Changed"}},
            {**insert, "tool_input": {**insert["tool_input"], "body": "Changed"}},
            {**insert, "tool_input": {**insert["tool_input"], "status": "canonical"}},
            {**insert, "tool_input": {**insert["tool_input"], "kind": "fact"}},
            {**insert, "tool_input": {**insert["tool_input"], "links": []}},
            {**insert, "tool_input": {**insert["tool_input"], "workstream_id": 7}},
            {**insert, "tool_input": {**insert["tool_input"], "artifacts": [{"repo": root}]}},
        ]
        for wrong in variations:
            assert cpre.decision(wrong)["permission"] == "deny", wrong
        assert cpre.decision(insert) == {}
        assert cpre.decision(insert)["permission"] == "deny"

        cgs.begin_prompt(root, sid, "Latch operation id: latch-pm prepare")
        bad_preview = {**preview, "candidate_digest": "0" * 64}
        failed = {**preview_call, "tool_output": bad_preview}
        recorded = cpost.record_operation_success(failed)
        assert recorded is not None and recorded[0] is False
        cgs.begin_prompt(root, sid, "/latch-pm apply")
        assert cpre.decision(insert)["permission"] == "deny"

        for wrapper in (
            {"status": "cancelled", "result": preview},
            {"status": "timeout", "result": preview},
            {"ok": False, "result": preview},
            {"cancelled": True, "result": preview},
            {"status": "skipped", "result": preview},
            {"ok": 0, "result": preview},
        ):
            cgs.begin_prompt(root, sid, "Latch operation id: latch-pm prepare")
            failed = {**preview_call, "tool_output": wrapper}
            recorded = cpost.record_operation_success(failed)
            assert recorded is not None and recorded[0] is False
            cgs.begin_prompt(root, sid, "/latch-pm apply")
            assert cpre.decision(insert)["permission"] == "deny"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_managed_operation_intent_never_falls_through_to_general_gate():
    from latch.hooks import cursor_post_tool_use as cpost
    from latch.hooks import cursor_pre_tool_use as cpre

    root, project_dir = _tmp()
    sid = "exclusive-operation-session"

    def arm(prompt: str) -> None:
        assert cgs.record_gate(
            root, sid, request=prompt,
            gate_status="OK", recommendation="PROCEED",
        )[0]

    try:
        seed = paths.KB_ROOT / "bin" / "latch_seed.sh"
        prompt = "/latch-seed apply all"
        state = cgs.begin_prompt(root, sid, prompt)
        assert state["operation_intent"]["name"] == "latch-seed"
        assert state["operation_receipt"] is None
        assert cgs.managed_operation_intended(root, sid)[0]
        arm(prompt)
        seed_apply = _shell(
            f"bash {seed} --source cursor --cursor-session-id {sid} "
            "--format json --apply --yes", root, sid,
        )
        denied = cpre.decision(seed_apply)
        assert denied["permission"] == "deny"
        assert "general latch_gate receipt cannot override" in denied["user_message"]

        corrupt = cgs.read_state(root, sid)
        assert corrupt is not None
        corrupt["operation_intent"] = {"name": "unknown-operation"}
        cgs._atomic_write(cgs.state_path(root, sid), corrupt)
        assert cgs.managed_operation_intended(root, sid)[0]
        assert cpre.decision(seed_apply)["permission"] == "deny"

        candidate = {
            "kind": "decision", "status": "staging",
            "title": "Approved", "body": "Approved body", "links": [],
        }
        cgs.begin_prompt(root, sid, "Latch operation id: latch-pm prepare")
        preview_call = {
            "workspaceRoot": root,
            "conversation_id": sid,
            "tool_name": "mcp__latch__latch_pm_preview",
            "tool_input": candidate,
            "tool_output": cgs.pm_preview_payload(candidate),
        }
        assert cpost.record_operation_success(preview_call)[0]
        prompt = "/latch-pm apply"
        cgs.begin_prompt(root, sid, prompt)
        arm(prompt)
        changed = {
            "workspaceRoot": root,
            "conversation_id": sid,
            "tool_name": "mcp__latch__latch_insert",
            "tool_input": {**candidate, "body": "Changed body"},
        }
        assert cpre.decision(changed)["permission"] == "deny"

        maintenance = paths.KB_ROOT / "src" / "latch" / "pipeline" / "maintenance.py"
        prompt = "/latch-heal"
        for command in (
            f"/tmp/python {maintenance} nightly {root}",
            f"python /tmp/maintenance.py nightly {root}",
            f"python {maintenance} nightly {root} extra",
        ):
            cgs.begin_prompt(root, sid, prompt)
            arm(prompt)
            assert cpre.decision(_shell(command, root, sid))["permission"] == "deny"

        cgs.begin_prompt(root, sid, prompt)
        arm(prompt)
        valid = _shell(
            f"{shlex.quote(sys.executable)} {maintenance} nightly {root}", root, sid,
        )
        assert cpre.decision(valid) == {}
        assert cpre.decision(valid)["permission"] == "deny"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_other_managed_operations_match_only_expected_wrappers():
    from latch.hooks import cursor_pre_tool_use as cpre

    root, project_dir = _tmp()
    sid = "managed-operation-session"
    python = shlex.quote(sys.executable)
    cases = [
        ("/latch-gate-report", f"bash {paths.KB_ROOT / 'bin' / 'latch_gate_report.sh'}"),
        ("/latch-budget-approve", f"{python} {paths.KB_ROOT / 'src' / 'latch' / 'gate' / 'budget.py'} approve {root}"),
        ("/latch-decay", f"{python} {paths.KB_ROOT / 'src' / 'latch' / 'pipeline' / 'maintenance.py'} weekly {root}"),
        ("/latch-heal", f"{python} {paths.KB_ROOT / 'src' / 'latch' / 'pipeline' / 'maintenance.py'} nightly {root}"),
        ("/latch-tree", f"{python} {paths.KB_ROOT / 'src' / 'latch' / 'pipeline' / 'maintenance.py'} tree {root}"),
    ]
    try:
        for prompt, command in cases:
            cgs.begin_prompt(root, sid, prompt)
            assert cpre.decision(_shell(command, root, sid)) == {}, prompt

        legacy_cases = [
            ("/latch-budget-approve", f"{python} {paths.KB_ROOT / 'src' / 'budget.py'} approve {root}"),
            ("/latch-decay", f"{python} {paths.KB_ROOT / 'src' / 'maintenance.py'} weekly {root}"),
            ("/latch-heal", f"{python} {paths.KB_ROOT / 'src' / 'maintenance.py'} nightly {root}"),
            ("/latch-tree", f"{python} {paths.KB_ROOT / 'src' / 'maintenance.py'} tree {root}"),
        ]
        for prompt, command in legacy_cases:
            cgs.begin_prompt(root, sid, prompt)
            assert cpre.decision(_shell(command, root, sid)) == {}, prompt

        maintenance = paths.KB_ROOT / "src" / "latch" / "pipeline" / "maintenance.py"
        cgs.begin_prompt(root, sid, "/latch-heal")
        assert cpre.decision(_shell(
            f'{python} {maintenance} nightly "$PWD"', root, sid,
        )) == {}

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
        shutil.rmtree(root, ignore_errors=True)


def test_managed_maintenance_receipt_rejects_wrong_project_and_script_path():
    from latch.hooks import cursor_pre_tool_use as cpre

    root, project_dir = _tmp()
    other_root = tempfile.mkdtemp()
    sid = "managed-operation-project-binding"
    attacker_dir = paths.KB_ROOT / "attacker"
    attacker_script = attacker_dir / "maintenance.py"
    official_script = paths.KB_ROOT / "src" / "latch" / "pipeline" / "maintenance.py"
    python = shlex.quote(sys.executable)
    try:
        cgs.begin_prompt(root, sid, "/latch-heal")
        wrong_project = _shell(
            f"{python} {official_script} nightly {other_root}", root, sid,
        )
        assert cpre.decision(wrong_project)["permission"] == "deny"

        attacker_dir.mkdir(exist_ok=True)
        attacker_script.write_text("# not a managed script\n", encoding="utf-8")
        cgs.begin_prompt(root, sid, "/latch-heal")
        wrong_script = _shell(
            f"{python} {attacker_script} nightly {root}", root, sid,
        )
        assert cpre.decision(wrong_script)["permission"] == "deny"

        cgs.begin_prompt(root, sid, "/latch-heal")
        expected = _shell(f"{python} {official_script} nightly {root}", root, sid)
        assert cpre.decision(expected) == {}
    finally:
        attacker_script.unlink(missing_ok=True)
        try:
            attacker_dir.rmdir()
        except OSError:
            pass
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(other_root, ignore_errors=True)


def test_managed_budget_receipt_rejects_wrong_project_and_script_path():
    from latch.hooks import cursor_pre_tool_use as cpre

    root, project_dir = _tmp()
    other_root = tempfile.mkdtemp()
    sid = "managed-budget-project-binding"
    attacker_dir = paths.KB_ROOT / "attacker"
    attacker_script = attacker_dir / "budget.py"
    official_script = paths.KB_ROOT / "src" / "latch" / "gate" / "budget.py"
    python = shlex.quote(sys.executable)
    try:
        cgs.begin_prompt(root, sid, "/latch-budget-approve")
        wrong_project = _shell(
            f"{python} {official_script} approve {other_root}", root, sid,
        )
        assert cpre.decision(wrong_project)["permission"] == "deny"

        attacker_dir.mkdir(exist_ok=True)
        attacker_script.write_text("# not a managed script\n", encoding="utf-8")
        cgs.begin_prompt(root, sid, "/latch-budget-approve")
        wrong_script = _shell(
            f"{python} {attacker_script} approve {root}", root, sid,
        )
        assert cpre.decision(wrong_script)["permission"] == "deny"

        cgs.begin_prompt(root, sid, "/latch-budget-approve")
        expected = _shell(f"{python} {official_script} approve {root}", root, sid)
        assert cpre.decision(expected) == {}

        cgs.begin_prompt(root, sid, "/latch-budget-approve")
        pwd_form = _shell(f'{python} {official_script} approve "$PWD"', root, sid)
        assert cpre.decision(pwd_form) == {}

        cgs.begin_prompt(root, sid, "/latch-budget-approve")
        wrong_cwd = _shell(f'{python} {official_script} approve "$PWD"', root, sid)
        wrong_cwd["tool_input"]["cwd"] = other_root
        assert cpre.decision(wrong_cwd)["permission"] == "deny"
    finally:
        attacker_script.unlink(missing_ok=True)
        try:
            attacker_dir.rmdir()
        except OSError:
            pass
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(other_root, ignore_errors=True)
