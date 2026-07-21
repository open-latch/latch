"""Direct unit tests for ``mcp_proxy._resolve_session``.

In shared-daemon mode every host (Claude Code, Codex, Cursor, VSCode) delegates
``mcp_server.py`` to ``mcp_proxy.main()`` (see ``mcp_server.py`` ``__main__``),
so ``mcp_proxy._resolve_session`` is the AUTHORITATIVE per-connection session
resolver for real MCP tool calls.  ``mcp_server._resolve_project_session_id``
(covered by ``test_mcp_session_id.py``) runs only on the legacy / context-less
path.  These tests exercise the proxy resolver directly so Claude attribution
and the Codex SessionStart-marker fallback are guarded on every OS, matching the
coverage ``test_codex_session.py`` gives the Codex path.

Cursor is deliberately left unattributed because its reused MCP process does
not carry a verified per-request conversation identity.  This must hold even
when the Cursor install uses Codex model backends or stale Codex markers exist.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import codex_session  # noqa: E402
import mcp_proxy  # noqa: E402
import paths  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _clean_env(**overrides):
    """Isolate os.environ for the resolver.

    ``_resolve_session`` reads ``os.environ`` directly, so ``clear=True`` keeps
    the test runner's own ``CLAUDE_CODE_SESSION_ID`` (present when this suite is
    run from inside a Claude Code session) from leaking into the fallback cases.
    """
    return mock.patch.dict(mcp_proxy.os.environ, overrides, clear=True)


def test_resolve_session_prefers_neutral_latch_override():
    with _clean_env(
        LATCH_SESSION_ID=" latch-session ",
        CLAUDE_CODE_SESSION_ID="claude-session",
        CODEX_THREAD_ID="codex-thread",
    ):
        _assert(
            mcp_proxy._resolve_session("/tmp/x")
            == ("latch-session", "env:LATCH_SESSION_ID"),
            "neutral LATCH_SESSION_ID must win over host session ids",
        )
    print("PASS resolve_session_prefers_neutral_latch_override")


def test_resolve_session_uses_claude_session_ahead_of_codex():
    with _clean_env(
        CLAUDE_CODE_SESSION_ID="claude-session",
        CODEX_THREAD_ID="codex-thread",
    ):
        _assert(
            mcp_proxy._resolve_session("/tmp/x")
            == ("claude-session", "env:CLAUDE_CODE_SESSION_ID"),
            "Claude Code session id is first-class, ahead of the Codex thread id",
        )
    print("PASS resolve_session_uses_claude_session_ahead_of_codex")


def test_resolve_session_falls_back_to_codex_thread_env():
    with _clean_env(CODEX_THREAD_ID=" codex-thread "):
        _assert(
            mcp_proxy._resolve_session("/tmp/x")
            == ("codex-thread", "env:CODEX_THREAD_ID"),
            "CODEX_THREAD_ID is used when no higher-priority id is present",
        )
    print("PASS resolve_session_falls_back_to_codex_thread_env")


def test_resolve_session_unavailable_without_env_or_codex_adapter():
    with _clean_env():
        _assert(
            mcp_proxy._resolve_session("/tmp/x") == (None, "unavailable"),
            "no session env + non-codex adapter must yield (None, 'unavailable')",
        )
    print("PASS resolve_session_unavailable_without_env_or_codex_adapter")


def test_resolve_session_leaves_cursor_mcp_calls_unattributed():
    tmp = tempfile.mkdtemp(prefix="mcp_proxy_cursor_marker_")
    project_dir = paths.project_dir(tmp)
    try:
        codex_session.write_marker(tmp, "stale-codex-thread")
        with _clean_env(
            LATCH_ADAPTER="cursor",
            LATCH_MODEL_BACKEND="codex",
            LATCH_GATE_BACKEND="codex",
            CODEX_THREAD_ID="stale-backend-thread",
            LATCH_SESSION_ID="stale-process-session",
        ):
            _assert(
                mcp_proxy._resolve_session(tmp) == (None, "unavailable"),
                "Cursor MCP calls must not inherit process ids or Codex markers",
            )
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS resolve_session_leaves_cursor_mcp_calls_unattributed")


def test_resolve_session_reads_codex_marker_when_env_lacks_thread():
    tmp = tempfile.mkdtemp(prefix="mcp_proxy_marker_")
    project_dir = paths.project_dir(tmp)
    try:
        codex_session.write_marker(tmp, "marker-thread")
        with _clean_env(LATCH_MODEL_BACKEND="codex"):
            _assert(
                mcp_proxy._resolve_session(tmp)
                == ("marker-thread", "codex_session_start_marker"),
                "a codex-adapter MCP env without CODEX_THREAD_ID reads the "
                "SessionStart marker",
            )
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS resolve_session_reads_codex_marker_when_env_lacks_thread")


def test_resolve_session_reports_missing_codex_marker():
    tmp = tempfile.mkdtemp(prefix="mcp_proxy_no_marker_")
    project_dir = paths.project_dir(tmp)
    try:
        with _clean_env(LATCH_MODEL_BACKEND="codex"):
            _assert(
                mcp_proxy._resolve_session(tmp) == (None, "codex_marker_missing"),
                "codex adapter with no SessionStart marker stays unattributed "
                "instead of guessing an id",
            )
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS resolve_session_reports_missing_codex_marker")


def test_connection_metadata_carries_compaction_guard_per_proxy():
    with _clean_env(LATCH_IN_COMPACT="1"):
        _assert(
            mcp_proxy.connection_metadata("/tmp/x")["in_compact"] is True,
            "neutral compact guard must travel with the proxy connection",
        )
    with _clean_env(CLAUDE_KB_IN_COMPACT="1"):
        _assert(
            mcp_proxy.connection_metadata("/tmp/x")["in_compact"] is True,
            "legacy compact guard must travel with the proxy connection",
        )
    with _clean_env():
        _assert(
            mcp_proxy.connection_metadata("/tmp/x")["in_compact"] is False,
            "ordinary proxy connections must remain unguarded",
        )
    print("PASS connection_metadata_carries_compaction_guard_per_proxy")


if __name__ == "__main__":
    test_resolve_session_prefers_neutral_latch_override()
    test_resolve_session_uses_claude_session_ahead_of_codex()
    test_resolve_session_falls_back_to_codex_thread_env()
    test_resolve_session_unavailable_without_env_or_codex_adapter()
    test_resolve_session_leaves_cursor_mcp_calls_unattributed()
    test_resolve_session_reads_codex_marker_when_env_lacks_thread()
    test_resolve_session_reports_missing_codex_marker()
    test_connection_metadata_carries_compaction_guard_per_proxy()
    print("\nAll mcp_proxy._resolve_session tests pass.")
