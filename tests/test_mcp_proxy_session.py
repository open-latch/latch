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
import mcp_runtime  # noqa: E402
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
    isolated = {
        name: mcp_proxy.os.environ[name]
        for name in (paths.TEST_ROOT_ENV, paths.TEST_CAPABILITY_ENV)
        if name in mcp_proxy.os.environ
    }
    isolated.update(overrides)
    return mock.patch.dict(mcp_proxy.os.environ, isolated, clear=True)


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
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS resolve_session_reports_missing_codex_marker")


def test_connection_metadata_carries_typed_settings_and_private_child_env():
    with _clean_env(
        LATCH_IN_COMPACT="1",
        LATCH_UNLATCHED="1",
        LATCH_DISABLE_WRITE="1",
        CLAUDE_KB_IN_MAINTENANCE="1",
        LATCH_GATE_BACKEND=" CoDeX ",
        LATCH_MAINTENANCE_BACKEND="CURSOR",
        LATCH_GATE_CLASSIFIER_TIMEOUT_S="44",
        CLAUDE_KB_GATE_ADVERSARY_TIMEOUT_S="22",
        CLAUDE_KB_ADVERSARY="0",
        LATCH_MCP_PROXY_CAP="7",
        LATCH_MCP_PROXY_RETIRE_IDLE_SEC="11",
        LATCH_MCP_PROXY_HEARTBEAT_SEC="3",
        LATCH_MCP_PROXY_STALE_SEC="19",
        OPENAI_API_KEY="private-openai-secret",
        ANTHROPIC_API_KEY="private-anthropic-secret",
        LATCH_ARBITRARY_POISON="must-not-be-serialized",
    ):
        metadata = mcp_proxy.connection_metadata("/tmp/x")
    _assert(metadata["in_compact"] is True, metadata)
    _assert(metadata["unlatched"] is True, metadata)
    _assert(metadata["disabled"] is False, metadata)
    _assert(metadata["write_disabled"] is True, metadata)
    _assert(metadata["in_maintenance"] is True, metadata)
    _assert(metadata["gate_backend"] == "codex", metadata)
    _assert(metadata["maintenance_backend"] == "cursor", metadata)
    _assert(metadata["gate_classifier_timeout_s"] == 44, metadata)
    _assert(metadata["gate_adversary_timeout_s"] == 22, metadata)
    _assert(metadata["gate_adversary_enabled"] is False, metadata)
    _assert(metadata["proxy_policy"] == {
        "cap": 7,
        "retire_idle_s": 11.0,
        "heartbeat_s": 3.0,
        "stale_s": 19.0,
    }, metadata)
    digest = metadata["vault_context_digest"]
    _assert(
        isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest),
        metadata,
    )
    _assert(
        mcp_proxy.os.environ[paths.TEST_CAPABILITY_ENV] not in repr(metadata),
        "test capability must never be serialized into proxy metadata",
    )
    _assert("OPENAI_API_KEY" not in metadata, metadata)
    _assert("ANTHROPIC_API_KEY" not in metadata, metadata)
    _assert("LATCH_ARBITRARY_POISON" not in metadata, metadata)
    private = metadata["child_process_env"]
    _assert(private["OPENAI_API_KEY"] == "private-openai-secret", private)
    _assert(
        "ANTHROPIC_API_KEY" not in private,
        "an unselected backend credential was serialized",
    )
    _assert("LATCH_ARBITRARY_POISON" not in private, private)

    with _clean_env():
        defaults = mcp_proxy.connection_metadata("/tmp/x")
    _assert(defaults["in_compact"] is False, defaults)
    _assert(defaults["in_maintenance"] is False, defaults)
    _assert(defaults["gate_backend"] == "claude", defaults)
    _assert(defaults["maintenance_backend"] == "claude", defaults)
    _assert(defaults["gate_classifier_timeout_s"] == 300, defaults)
    _assert(defaults["gate_adversary_timeout_s"] == 120, defaults)
    _assert(defaults["gate_adversary_enabled"] is True, defaults)

    with _clean_env(LATCH_GATE_BACKEND="not-a-backend"):
        try:
            mcp_proxy.connection_metadata("/tmp/x")
        except ValueError as exc:
            _assert("unsupported gate backend" in str(exc), exc)
        else:
            raise AssertionError("invalid backend did not fail before startup")
    print("PASS connection_metadata_carries_typed_settings_and_private_child_env")


def test_command_resolution_cannot_preempt_explicit_path_with_cwd():
    allowed = str(Path(tempfile.mkdtemp(prefix="mcp-proxy-path-")))
    repo_local = str(Path.cwd() / "codex.cmd")
    try:
        with mock.patch.object(
            mcp_runtime.shutil, "which", return_value=repo_local
        ):
            _assert(
                mcp_proxy._which_on_explicit_path("codex", allowed) is None,
                "a cwd-local command absent from PATH was accepted",
            )
        allowed_command = str(Path(allowed) / "codex.cmd")
        with mock.patch.object(
            mcp_runtime.shutil, "which", return_value=allowed_command
        ):
            _assert(
                mcp_proxy._which_on_explicit_path("codex", allowed)
                == allowed_command,
                "an executable inside the explicit PATH was rejected",
            )
    finally:
        shutil.rmtree(allowed, ignore_errors=True)
    print("PASS command_resolution_cannot_preempt_explicit_path_with_cwd")


def test_windows_child_environment_deduplicates_case_insensitive_names():
    source = {
        "Path": r"C:\tools",
        "https_proxy": "http://proxy.example",
    }
    with mock.patch.object(mcp_proxy.os, "name", "nt"):
        child = mcp_proxy._child_process_environment(
            source, backends=frozenset()
        )
    folded = [name.upper() for name in child]
    _assert(len(folded) == len(set(folded)), child)
    _assert(child["PATH"] == r"C:\tools", child)
    _assert(child["HTTPS_PROXY"] == "http://proxy.example", child)
    print("PASS windows_child_environment_deduplicates_case_insensitive_names")


if __name__ == "__main__":
    test_resolve_session_prefers_neutral_latch_override()
    test_resolve_session_uses_claude_session_ahead_of_codex()
    test_resolve_session_falls_back_to_codex_thread_env()
    test_resolve_session_unavailable_without_env_or_codex_adapter()
    test_resolve_session_leaves_cursor_mcp_calls_unattributed()
    test_resolve_session_reads_codex_marker_when_env_lacks_thread()
    test_resolve_session_reports_missing_codex_marker()
    test_connection_metadata_carries_typed_settings_and_private_child_env()
    test_command_resolution_cannot_preempt_explicit_path_with_cwd()
    test_windows_child_environment_deduplicates_case_insensitive_names()
    print("\nAll mcp_proxy._resolve_session tests pass.")
