"""Unit tests for MCP session-id resolution across adapters."""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import codex_session  # noqa: E402
import cursor_session  # noqa: E402
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


def test_resolve_project_session_id_uses_cursor_marker_before_codex_backend():
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
        _assert(
            mcp_server._resolve_project_session_id(env, project_cwd=tmp) == "cursor-conversation",
            "Cursor adapter identity must win over its selected Codex model backend",
        )
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS resolve_project_session_id_uses_cursor_marker_before_codex_backend")


def test_project_session_id_refreshes_cursor_marker_per_call():
    tmp = tempfile.mkdtemp(prefix="mcp_cursor_session_refresh_")
    project_dir = paths.project_dir(tmp)
    env = {"LATCH_ADAPTER": "cursor"}
    try:
        cursor_session.write_marker(tmp, "conversation-one")
        with mock.patch.dict(mcp_server.os.environ, env, clear=True), \
                mock.patch.object(mcp_server, "PROJECT_CWD", tmp), \
                mock.patch.object(mcp_server, "PROJECT_SESSION_ID", "stale-cached-session"):
            _assert(
                mcp_server._project_session_id() == "conversation-one",
                "Cursor must ignore a process-cached project marker",
            )
            cursor_session.write_marker(tmp, "conversation-two")
            _assert(
                mcp_server._project_session_id() == "conversation-two",
                "Cursor must refresh the project marker after conversation changes",
            )
            cursor_session.marker_path(tmp).unlink()
            _assert(
                mcp_server._project_session_id() is None,
                "a missing current Cursor marker must not fall back to stale provenance",
            )
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS project_session_id_refreshes_cursor_marker_per_call")


def test_project_session_id_concurrent_cursor_marker_updates_are_uncached():
    tmp = tempfile.mkdtemp(prefix="mcp_cursor_session_concurrent_")
    project_dir = paths.project_dir(tmp)
    env = {"LATCH_ADAPTER": "cursor"}
    seen: list[str | None] = []
    errors: list[Exception] = []
    start = threading.Barrier(3)

    def writer(prefix: str) -> None:
        try:
            start.wait()
            for index in range(40):
                cursor_session.write_marker(tmp, f"{prefix}-{index}")
        except Exception as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    try:
        cursor_session.write_marker(tmp, "initial")
        with mock.patch.dict(mcp_server.os.environ, env, clear=True), \
                mock.patch.object(mcp_server, "PROJECT_CWD", tmp), \
                mock.patch.object(mcp_server, "PROJECT_SESSION_ID", "stale-cached-session"):
            threads = [
                threading.Thread(target=writer, args=("conversation-a",)),
                threading.Thread(target=writer, args=("conversation-b",)),
            ]
            for thread in threads:
                thread.start()
            start.wait()
            while any(thread.is_alive() for thread in threads):
                seen.append(mcp_server._project_session_id())
            for thread in threads:
                thread.join()
            seen.append(mcp_server._project_session_id())

        _assert(not errors, errors)
        _assert(seen and all(value is not None for value in seen), seen)
        _assert("stale-cached-session" not in seen, seen)
        _assert(
            all(value == "initial" or str(value).startswith("conversation-") for value in seen),
            seen,
        )
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS project_session_id_concurrent_cursor_marker_updates_are_uncached")


if __name__ == "__main__":
    test_resolve_project_session_id_prefers_neutral_override()
    test_resolve_project_session_id_preserves_claude_precedence()
    test_resolve_project_session_id_uses_codex_fallback()
    test_resolve_project_session_id_ignores_blank_values()
    test_resolve_project_session_id_uses_codex_marker_when_env_lacks_thread()
    test_resolve_project_session_id_uses_cursor_marker_before_codex_backend()
    test_project_session_id_refreshes_cursor_marker_per_call()
    test_project_session_id_concurrent_cursor_marker_updates_are_uncached()
    print("\nAll mcp_session_id tests pass.")
