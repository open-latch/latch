"""Unit tests for MCP session-id resolution across adapters."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from latch.hosts import codex_session  # noqa: E402
from latch.hosts import cursor_session  # noqa: E402
from latch.mcp import mcp_server  # noqa: E402
from latch.store import paths  # noqa: E402


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
    env = {"LATCH_ADAPTER": "cursor"}
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
    env = {"LATCH_ADAPTER": "cursor"}
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


if __name__ == "__main__":
    test_resolve_project_session_id_prefers_neutral_override()
    test_resolve_project_session_id_preserves_claude_precedence()
    test_resolve_project_session_id_uses_codex_fallback()
    test_resolve_project_session_id_ignores_blank_values()
    test_resolve_project_session_id_uses_codex_marker_when_env_lacks_thread()
    test_resolve_project_session_id_leaves_cursor_mcp_calls_unattributed()
    test_project_session_id_never_attributes_cursor_marker_changes()
    test_project_session_id_interleaved_cursor_conversations_are_unattributed()
    print("\nAll mcp_session_id tests pass.")
