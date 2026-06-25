"""Unit tests for MCP session-id resolution across adapters."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import codex_session  # noqa: E402
import mcp_server  # noqa: E402
import paths  # noqa: E402


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
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS resolve_project_session_id_uses_codex_marker_when_env_lacks_thread")


if __name__ == "__main__":
    test_resolve_project_session_id_prefers_neutral_override()
    test_resolve_project_session_id_preserves_claude_precedence()
    test_resolve_project_session_id_uses_codex_fallback()
    test_resolve_project_session_id_ignores_blank_values()
    test_resolve_project_session_id_uses_codex_marker_when_env_lacks_thread()
    print("\nAll mcp_session_id tests pass.")
