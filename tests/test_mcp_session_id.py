"""Unit tests for MCP session-id resolution across adapters."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import codex_session  # noqa: E402
import cursor_session  # noqa: E402
import db  # noqa: E402
import mcp_server  # noqa: E402
import paths  # noqa: E402
import project_config  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_resolve_project_session_id_prefers_neutral_override():
    env = {
        "LATCH_SESSION_ID": " latch-session ",
        "CLAUDE_CODE_SESSION_ID": "claude-session",
        "CODEX_THREAD_ID": "codex-thread",
    }
    _assert(
        mcp_server._resolve_project_session_id(env) == "latch-session",
        "neutral latch session id should win when explicitly set",
    )
    print("PASS resolve_project_session_id_prefers_neutral_override")


def test_resolve_project_session_id_preserves_claude_precedence():
    env = {
        "CLAUDE_CODE_SESSION_ID": "claude-session",
        "CODEX_THREAD_ID": "codex-thread",
    }
    _assert(
        mcp_server._resolve_project_session_id(env) == "claude-session",
        "Claude Code session id should preserve existing behavior",
    )
    print("PASS resolve_project_session_id_preserves_claude_precedence")


def test_resolve_project_session_id_uses_codex_fallback():
    env = {"CODEX_THREAD_ID": " codex-thread "}
    _assert(
        mcp_server._resolve_project_session_id(env) == "codex-thread",
        "Codex thread id should be used when Claude session id is absent",
    )
    print("PASS resolve_project_session_id_uses_codex_fallback")


def test_resolve_project_session_id_ignores_blank_values():
    env = {
        "LATCH_SESSION_ID": " ",
        "CLAUDE_CODE_SESSION_ID": "",
        "CODEX_THREAD_ID": "\tcodex-thread\n",
    }
    _assert(
        mcp_server._resolve_project_session_id(env) == "codex-thread",
        "blank higher-priority values should not block Codex fallback",
    )
    _assert(
        mcp_server._resolve_project_session_id({}) is None,
        "missing session env should remain None",
    )
    print("PASS resolve_project_session_id_ignores_blank_values")


def test_resolve_project_session_id_uses_codex_marker_when_env_lacks_thread():
    tmp = tempfile.mkdtemp(prefix="mcp_session_marker_")
    project_dir = paths.project_dir(tmp)
    try:
        codex_session.write_marker(tmp, "marker-thread")
        env = {"LATCH_MODEL_BACKEND": "codex", "LATCH_GATE_BACKEND": "codex"}
        _assert(
            mcp_server._resolve_project_session_id(env, project_cwd=tmp) == "marker-thread",
            "Codex MCP env without CODEX_THREAD_ID should read the SessionStart marker",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS resolve_project_session_id_uses_codex_marker_when_env_lacks_thread")


def test_resolve_project_session_id_leaves_cursor_mcp_calls_unattributed():
    tmp = tempfile.mkdtemp(prefix="mcp_cursor_session_marker_")
    project_dir = paths.project_dir(tmp)
    try:
        cursor_session.write_marker(tmp, "cursor-conversation")
        codex_session.write_marker(tmp, "wrong-codex-thread")
        env = {
            "LATCH_ADAPTER": "cursor",
            "LATCH_MODEL_BACKEND": "codex",
            "LATCH_GATE_BACKEND": "codex",
        }
        _assert(mcp_server._resolve_project_session_id(env, project_cwd=tmp) is None,
                "a project marker cannot identify one interleaved Cursor MCP request")
        env["CODEX_THREAD_ID"] = "wrong-backend-thread"
        env["LATCH_SESSION_ID"] = "wrong-process-session"
        _assert(mcp_server._resolve_project_session_id(env, project_cwd=tmp) is None,
                "backend/process ids must not override the Cursor request boundary")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS resolve_project_session_id_leaves_cursor_mcp_calls_unattributed")


def test_project_session_id_never_attributes_cursor_marker_changes():
    tmp = tempfile.mkdtemp(prefix="mcp_cursor_session_refresh_")
    project_dir = paths.project_dir(tmp)
    env = {
        "LATCH_ADAPTER": "cursor",
        paths.TEST_ROOT_ENV: str(paths.validated_test_root()),
        paths.TEST_CAPABILITY_ENV: os.environ[paths.TEST_CAPABILITY_ENV],
        project_config.CONTROL_ROOT_ENV: os.environ[
            project_config.CONTROL_ROOT_ENV
        ],
        "LATCH_HOME": os.environ["LATCH_HOME"],
    }
    try:
        cursor_session.write_marker(tmp, "conversation-one")
        with mock.patch.dict(mcp_server.os.environ, env, clear=True), \
                mock.patch.object(mcp_server, "PROJECT_CWD", tmp), \
                mock.patch.object(mcp_server, "PROJECT_SESSION_ID", "stale-cached-session"):
            _assert(mcp_server._project_session_id() is None,
                    "Cursor MCP requests must ignore a process-cached id and project marker")
            cursor_session.write_marker(tmp, "conversation-two")
            _assert(mcp_server._project_session_id() is None,
                    "later marker changes must not attribute unrelated MCP calls")
            cursor_session.marker_path(tmp).unlink()
            _assert(
                mcp_server._project_session_id() is None,
                "a missing current Cursor marker must not fall back to stale provenance",
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS project_session_id_never_attributes_cursor_marker_changes")


def test_project_session_id_interleaved_cursor_conversations_are_unattributed():
    tmp = tempfile.mkdtemp(prefix="mcp_cursor_session_concurrent_")
    project_dir = paths.project_dir(tmp)
    env = {
        "LATCH_ADAPTER": "cursor",
        paths.TEST_ROOT_ENV: str(paths.validated_test_root()),
        paths.TEST_CAPABILITY_ENV: os.environ[paths.TEST_CAPABILITY_ENV],
        project_config.CONTROL_ROOT_ENV: os.environ[
            project_config.CONTROL_ROOT_ENV
        ],
        "LATCH_HOME": os.environ["LATCH_HOME"],
    }
    try:
        cursor_session.write_marker(tmp, "conversation-a")
        with mock.patch.dict(mcp_server.os.environ, env, clear=True), \
                mock.patch.object(mcp_server, "PROJECT_CWD", tmp), \
                mock.patch.object(mcp_server, "PROJECT_SESSION_ID", "stale-cached-session"):
            _assert(mcp_server._project_session_id() is None,
                    "conversation A's project marker is not MCP request provenance")
            cursor_session.write_marker(tmp, "conversation-b")
            _assert(mcp_server._project_session_id() is None,
                    "conversation B cannot steal attribution from an interleaved A request")
            cursor_session.write_marker(tmp, "conversation-a")
            _assert(mcp_server._project_session_id() is None,
                    "last-marker-wins must not be treated as request identity")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS project_session_id_interleaved_cursor_conversations_are_unattributed")


def test_legacy_binding_snapshot_rejects_old_or_unattributed_agent_task():
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)
    root = Path(tempfile.mkdtemp(prefix="mcp_legacy_binding_"))
    project = root / "project"
    project.mkdir()
    (project / ".git").mkdir()
    kb_a = paths.validated_test_root() / "vaults" / f"mcp-legacy-a-{root.name}"
    kb_b = paths.validated_test_root() / "vaults" / f"mcp-legacy-b-{root.name}"
    kb_a.mkdir(parents=True)
    kb_b.mkdir(parents=True)
    try:
        project_config.mark_kb_target(kb_a)
        project_config.mark_kb_target(kb_b)
        binding_a = project_config.write_binding(
            project, mode=project_config.MODE_LATCHED, kb_dir=kb_a,
        )
        session_id = "mcp-session-id-old-task"
        project_config.record_session_binding(project, session_id)
        _assert(
            mcp_server.project_binding_snapshot(
                str(project), session_id=session_id, require_session=True,
            ) == (binding_a.revision, str(kb_a)),
            "a current authenticated task should retain its exact target",
        )
        project_config.repin_private_scope(project, kb_b)
        stale = mcp_server.project_binding_snapshot(
            str(project), session_id=session_id, require_session=True,
        )
        missing = mcp_server.project_binding_snapshot(
            str(project), require_session=True,
        )
        _assert(stale == ("stale-session", None), stale)
        _assert(missing == ("stale-session", None), missing)
        with mock.patch.object(mcp_server, "PROJECT_CWD", str(project)), \
                mock.patch.object(mcp_server, "_DIRECT_BINDING_SNAPSHOT", stale):
            try:
                mcp_server._conn()
            except db.ProjectTargetChangedError:
                pass
            else:
                raise AssertionError("legacy stdio opened the repinned KB")
        _assert(not (kb_b / "kb.db").exists(), "stale task touched the new KB")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(kb_a, ignore_errors=True)
        shutil.rmtree(kb_b, ignore_errors=True)
    print("PASS legacy_binding_snapshot_rejects_old_or_unattributed_agent_task")


def test_stale_direct_server_exits_before_runtime_initialization():
    calls: list[str] = []
    with mock.patch.object(
        mcp_server, "_DIRECT_BINDING_SNAPSHOT", ("stale-session", None),
    ), mock.patch.object(
        mcp_server,
        "initialize_runtime",
        side_effect=lambda *_a, **_k: calls.append("runtime"),
    ), mock.patch.object(
        mcp_server.mcp,
        "run",
        side_effect=lambda: calls.append("serve"),
    ):
        assert mcp_server._run_direct_server() == 2
    assert calls == []


if __name__ == "__main__":
    test_resolve_project_session_id_prefers_neutral_override()
    test_resolve_project_session_id_preserves_claude_precedence()
    test_resolve_project_session_id_uses_codex_fallback()
    test_resolve_project_session_id_ignores_blank_values()
    test_resolve_project_session_id_uses_codex_marker_when_env_lacks_thread()
    test_resolve_project_session_id_leaves_cursor_mcp_calls_unattributed()
    test_project_session_id_never_attributes_cursor_marker_changes()
    test_project_session_id_interleaved_cursor_conversations_are_unattributed()
    test_legacy_binding_snapshot_rejects_old_or_unattributed_agent_task()
    test_stale_direct_server_exits_before_runtime_initialization()
    print("\nAll mcp_session_id tests pass.")
