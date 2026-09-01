"""Tests for the isolated read-only Cursor Agent CLI model backend."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from latch.hosts import cursor_backend  # noqa: E402
from latch.mcp import mcp_runtime  # noqa: E402


def test_cursor_backend_uses_headless_ask_mode_and_stdin(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": '{"ok": true}',
                "session_id": "backend-session",
            }),
            stderr="",
        )

    monkeypatch.setattr(cursor_backend.subprocess, "run", fake_run)
    text, error, timed_out = cursor_backend.invoke_prompt(
        "return JSON", timeout_s=2, purpose="test",
        agent_bin="cursor-agent-test", model="test-model",
    )
    assert error is None and timed_out is False
    assert text == '{"ok": true}'
    assert captured["kwargs"]["input"] == "return JSON"
    args = captured["args"]
    assert args[:5] == [
        "cursor-agent-test", "--print", "--output-format", "json", "--mode",
    ]
    assert "ask" in args and "--trust" in args and "--workspace" in args
    assert "--force" not in args and "--yolo" not in args
    assert args[-2:] == ["--model", "test-model"]


def test_cursor_backend_rejects_invalid_and_error_results(monkeypatch):
    response = SimpleNamespace(returncode=0, stdout="not-json", stderr="")
    monkeypatch.setattr(cursor_backend.subprocess, "run", lambda *_args, **_kwargs: response)
    text, error, _ = cursor_backend.invoke_prompt(
        "x", timeout_s=2, purpose="test", agent_bin="cursor-agent-test",
    )
    assert text is None and "invalid JSON" in error

    response.stdout = json.dumps({
        "type": "result", "subtype": "error", "is_error": True,
        "result": "authentication required",
    })
    text, error, _ = cursor_backend.invoke_prompt(
        "x", timeout_s=2, purpose="test", agent_bin="cursor-agent-test",
    )
    assert text is None and "non-success" in error


def test_cursor_backend_uses_private_connection_environment(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "cursor-sentinel-secret",
            }),
            stderr="",
        )

    context = mcp_runtime.ConnectionContext(
        connection_id="cursor-private",
        project_cwd="/tmp/project",
        session_id=None,
        session_source="test",
        proxy_pid=123,
        proxy_started_at="now",
        runtime_key="test",
        gate_backend="cursor",
        maintenance_backend="cursor",
    )
    private = mcp_runtime.validate_child_environment({
        "PATH": "/cursor/bin",
        "CURSOR_AGENT_BIN": "/cursor/bin/agent",
        "CURSOR_API_KEY": "cursor-sentinel-secret",
        "ANTHROPIC_API_KEY": "anthropic-sentinel-secret",
        "LATCH_CURSOR_MODEL": "cursor-test-model",
    })
    monkeypatch.setattr(cursor_backend.subprocess, "run", fake_run)
    with mcp_runtime.bind_connection(context, child_environment=private):
        text, error, timed_out = cursor_backend.invoke_prompt(
            "x", timeout_s=2, purpose="test"
        )
    assert error is None and timed_out is False
    assert text == "<redacted>"
    assert captured["args"][0] == "/cursor/bin/agent"
    assert captured["args"][-2:] == ["--model", "cursor-test-model"]
    assert captured["kwargs"]["env"]["CURSOR_API_KEY"] == "cursor-sentinel-secret"
    assert "ANTHROPIC_API_KEY" not in captured["kwargs"]["env"]
