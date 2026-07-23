"""Unit tests for kb_gate model backend selection."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import gate  # noqa: E402
import log_utils  # noqa: E402
import mcp_runtime  # noqa: E402
import paths  # noqa: E402


CLASSIFIER_JSON = (
    '{"recommendation":"PROCEED","summary":"ok","decision_chain":[],'
    '"abandoned_paths":[],"active_constraints":[],"current_direction":[],'
    '"risk_if_proceed":"","better_next_action":"","evidence_nodes":[],'
    '"load_bearing_claims":[]}'
)

ADVERSARY_JSON = (
    '{"objection":"","counter_node_id":null,"verdict_delta":"none",'
    '"design_decision_questions":[]}'
)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="latch-gate-backends-"))


def _fake_exe(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_claude(path: Path) -> Path:
    return _fake_exe(
        path,
        "printf '%s\\n' \"$@\" > \"$FAKE_GATE_ARGS\"\n"
        "cat >/dev/null\n"
        "printf '%s\\n' \"$FAKE_GATE_RESPONSE\"\n",
    )


def _fake_codex(path: Path) -> Path:
    return _fake_exe(
        path,
        "printf '%s\\n' \"$@\" > \"$FAKE_GATE_ARGS\"\n"
        "out=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  if [ \"$1\" = '--output-last-message' ]; then shift; out=\"$1\"; fi\n"
        "  shift || break\n"
        "done\n"
        "cat >/dev/null\n"
        "printf '%s\\n' \"$FAKE_GATE_RESPONSE\" > \"$out\"\n"
        "printf '%s\\n' \"$FAKE_GATE_RESPONSE\"\n",
    )


def _chain() -> dict:
    return {
        "query": "make a backend-neutral gate",
        "seeds": [],
        "chains": [],
        "evidence_node_ids": [],
        "priorities": [],
    }


def _cleanup_project(project_path: Path) -> None:
    shutil.rmtree(paths.project_dir(str(project_path)), ignore_errors=True)


def test_claude_backend_remains_supported():
    d = _tmp()
    project = d / "project"
    old_claude = gate.CLAUDE_BIN
    old_response = os.environ.get("FAKE_GATE_RESPONSE")
    old_args = os.environ.get("FAKE_GATE_ARGS")
    try:
        args_file = d / "args.txt"
        fake = _fake_claude(d / "claude")
        gate.CLAUDE_BIN = str(fake)
        os.environ["FAKE_GATE_RESPONSE"] = CLASSIFIER_JSON
        os.environ["FAKE_GATE_ARGS"] = str(args_file)
        out = gate.classify_gate(
            _chain(), project_path=str(project), backend="claude", timeout_s=2,
        )
        _assert(out["recommendation"] == "PROCEED", out)
        _assert(out["backend"] == "claude", out)
        args = args_file.read_text(encoding="utf-8").splitlines()
        _assert(args == ["-p", "--no-session-persistence", "--output-format", "json"], args)
    finally:
        gate.CLAUDE_BIN = old_claude
        _restore_env("FAKE_GATE_RESPONSE", old_response)
        _restore_env("FAKE_GATE_ARGS", old_args)
        _cleanup_project(project)
        shutil.rmtree(d, ignore_errors=True)
    print("PASS claude_backend_remains_supported")


def test_codex_backend_does_not_call_claude_classifier():
    d = _tmp()
    project = d / "project"
    old_codex = gate.CODEX_BIN
    old_claude = gate.CLAUDE_BIN
    old_response = os.environ.get("FAKE_GATE_RESPONSE")
    old_args = os.environ.get("FAKE_GATE_ARGS")
    try:
        args_file = d / "args.txt"
        fake = _fake_codex(d / "codex")
        gate.CODEX_BIN = str(fake)
        gate.CLAUDE_BIN = str(d / "missing-claude")
        os.environ["FAKE_GATE_RESPONSE"] = CLASSIFIER_JSON
        os.environ["FAKE_GATE_ARGS"] = str(args_file)
        out = gate.classify_gate(
            _chain(), project_path=str(project), backend="codex", timeout_s=2,
        )
        _assert(out["recommendation"] == "PROCEED", out)
        _assert(out["backend"] == "codex", out)
        args = args_file.read_text(encoding="utf-8").splitlines()
        _assert(args[:2] == ["exec", "--ignore-user-config"], args)
        _assert("--ignore-rules" in args, args)
        _assert("--ephemeral" in args, args)
        _assert("--sandbox" in args and "read-only" in args, args)
        _assert(args[-1] == "-", args)
    finally:
        gate.CODEX_BIN = old_codex
        gate.CLAUDE_BIN = old_claude
        _restore_env("FAKE_GATE_RESPONSE", old_response)
        _restore_env("FAKE_GATE_ARGS", old_args)
        _cleanup_project(project)
        shutil.rmtree(d, ignore_errors=True)
    print("PASS codex_backend_does_not_call_claude_classifier")


def test_codex_backend_does_not_call_claude_adversary():
    d = _tmp()
    project = d / "project"
    old_codex = gate.CODEX_BIN
    old_claude = gate.CLAUDE_BIN
    old_response = os.environ.get("FAKE_GATE_RESPONSE")
    old_args = os.environ.get("FAKE_GATE_ARGS")
    try:
        fake = _fake_codex(d / "codex")
        gate.CODEX_BIN = str(fake)
        gate.CLAUDE_BIN = str(d / "missing-claude")
        os.environ["FAKE_GATE_RESPONSE"] = ADVERSARY_JSON
        os.environ["FAKE_GATE_ARGS"] = str(d / "args.txt")
        out = gate.adversary_classify(
            _chain(), {"recommendation": "PROCEED"},
            project_path=str(project), backend="codex", timeout_s=2,
        )
        _assert(out["verdict_delta"] == "none", out)
        _assert(out["backend"] == "codex", out)
    finally:
        gate.CODEX_BIN = old_codex
        gate.CLAUDE_BIN = old_claude
        _restore_env("FAKE_GATE_RESPONSE", old_response)
        _restore_env("FAKE_GATE_ARGS", old_args)
        _cleanup_project(project)
        shutil.rmtree(d, ignore_errors=True)
    print("PASS codex_backend_does_not_call_claude_adversary")


def test_cursor_backend_does_not_call_claude_or_codex_classifier():
    d = _tmp()
    project = d / "project"
    old_response = os.environ.get("FAKE_CURSOR_RESPONSE")
    old_args = os.environ.get("FAKE_CURSOR_ARGS")
    old_stdin = os.environ.get("FAKE_CURSOR_STDIN")
    old_cursor_bin = gate.cursor_backend.CURSOR_AGENT_BIN
    try:
        fake = _fake_exe(
            d / "agent",
            "printf '%s\\n' \"$@\" > \"$FAKE_CURSOR_ARGS\"\n"
            "cat > \"$FAKE_CURSOR_STDIN\"\n"
            "printf '%s\\n' \"$FAKE_CURSOR_RESPONSE\"\n",
        )
        os.environ["FAKE_CURSOR_RESPONSE"] = json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": CLASSIFIER_JSON, "session_id": "cursor-backend",
        })
        os.environ["FAKE_CURSOR_ARGS"] = str(d / "args.txt")
        os.environ["FAKE_CURSOR_STDIN"] = str(d / "stdin.txt")
        raw, error, timed_out = gate._invoke_cursor_classifier_once(
            "classify", timeout_s=2, cursor_bin=str(fake),
        )
        _assert(error is None and timed_out is False, error)
        _assert(raw == CLASSIFIER_JSON, raw)
        args = (d / "args.txt").read_text(encoding="utf-8").splitlines()
        _assert("--mode" in args and "ask" in args, args)
        _assert("--force" not in args, args)
        gate.cursor_backend.CURSOR_AGENT_BIN = str(fake)
        out = gate.classify_gate(
            _chain(), project_path=str(project), backend="cursor", timeout_s=2,
        )
        _assert(out["recommendation"] == "PROCEED" and out["backend"] == "cursor", out)
    finally:
        gate.cursor_backend.CURSOR_AGENT_BIN = old_cursor_bin
        _restore_env("FAKE_CURSOR_RESPONSE", old_response)
        _restore_env("FAKE_CURSOR_ARGS", old_args)
        _restore_env("FAKE_CURSOR_STDIN", old_stdin)
        _cleanup_project(project)
        shutil.rmtree(d, ignore_errors=True)
    print("PASS cursor_backend_does_not_call_claude_or_codex_classifier")


def test_adversary_structural_log_carries_backend():
    d = _tmp()
    project = d / "project"
    try:
        gate._log_adversary(
            project_path=str(project),
            session_id=None,
            request="make a backend-neutral gate",
            verdict_before="PROCEED",
            adv={"verdict_delta": "none", "backend": "codex"},
            elapsed_ms=1,
        )
        today = datetime.now(timezone.utc).date()
        rows = list(log_utils.read_log_range("adversary", today, today, str(project)))
        _assert(rows and rows[0]["backend"] == "codex", rows)
    finally:
        _cleanup_project(project)
        shutil.rmtree(d, ignore_errors=True)
    print("PASS adversary_structural_log_carries_backend")


def test_connection_gate_backend_outranks_daemon_environment():
    old = os.environ.get("LATCH_GATE_BACKEND")
    context = mcp_runtime.ConnectionContext(
        connection_id="codex-connection",
        project_cwd="/tmp/project",
        session_id=None,
        session_source="test",
        proxy_pid=123,
        proxy_started_at="now",
        runtime_key="test",
        gate_backend="codex",
        maintenance_backend="cursor",
    )
    try:
        os.environ["LATCH_GATE_BACKEND"] = "claude"
        with mcp_runtime.bind_connection(context):
            _assert(gate._classifier_backend() == "codex", "daemon env won")
            _assert(
                gate._classifier_backend("cursor") == "cursor",
                "explicit backend must remain authoritative",
            )
        _assert(gate._classifier_backend() == "claude", "legacy env fallback broke")
    finally:
        _restore_env("LATCH_GATE_BACKEND", old)
    print("PASS connection_gate_backend_outranks_daemon_environment")


def test_connection_gate_policy_and_private_codex_environment(monkeypatch):
    context = mcp_runtime.ConnectionContext(
        connection_id="codex-private",
        project_cwd="/tmp/project",
        session_id=None,
        session_source="test",
        proxy_pid=123,
        proxy_started_at="now",
        runtime_key="test",
        gate_backend="codex",
        maintenance_backend="codex",
        gate_classifier_timeout_s=17,
        gate_adversary_timeout_s=9,
        gate_adversary_enabled=False,
    )
    private = mcp_runtime.validate_child_environment({
        "PATH": "/client/bin",
        "CODEX_BIN": "/client/bin/codex",
        "OPENAI_API_KEY": "openai-sentinel-secret",
        "OPENAI_BASE_URL": "https://user:base-secret@example.invalid/v1",
        "ANTHROPIC_API_KEY": "anthropic-sentinel-secret",
        "LATCH_GATE_CODEX_MODEL": "gpt-test",
    })
    policy_calls = []

    monkeypatch.setattr(
        gate.budget, "check_and_record", lambda *_args, **_kwargs: (True, {})
    )
    monkeypatch.setattr(
        gate,
        "_invoke_classifier_backend_once",
        lambda _prompt, **kwargs: (
            policy_calls.append(kwargs) or CLASSIFIER_JSON,
            None,
            False,
        ),
    )
    with mcp_runtime.bind_connection(context, child_environment=private):
        verdict = gate.classify_gate(
            _chain(), project_path="/tmp/project", backend="codex"
        )
        _assert(verdict["recommendation"] == "PROCEED", verdict)
        _assert(policy_calls[0]["timeout_s"] == 17, policy_calls)
        _assert(gate._should_fire_adversary(verdict) is False, verdict)

    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        out_path = Path(args[args.index("--output-last-message") + 1])
        out_path.write_text(
            CLASSIFIER_JSON.replace(
                "ok",
                "openai-sentinel-secret "
                "https://user:base-secret@example.invalid/v1",
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    with mcp_runtime.bind_connection(context, child_environment=private):
        raw, error, timed_out = gate._invoke_codex_classifier_once(
            "classify", timeout_s=17
        )
    _assert(error is None and timed_out is False, error)
    _assert("openai-sentinel-secret" not in raw, raw)
    _assert("base-secret" not in raw, raw)
    _assert("<redacted>" in raw, raw)
    _assert(captured["args"][0] == "/client/bin/codex", captured)
    _assert(captured["args"][-3:-1] == ["--model", "gpt-test"], captured)
    env = captured["kwargs"]["env"]
    _assert(env["OPENAI_API_KEY"] == "openai-sentinel-secret", env)
    _assert("ANTHROPIC_API_KEY" not in env, env)


def test_shared_gate_missing_absolute_binary_fails_closed(monkeypatch):
    context = mcp_runtime.ConnectionContext(
        connection_id="missing-codex-bin",
        project_cwd="/tmp/project",
        session_id=None,
        session_source="test",
        proxy_pid=123,
        proxy_started_at="now",
        runtime_key="test",
        gate_backend="codex",
        maintenance_backend="codex",
    )
    private = mcp_runtime.validate_child_environment({
        "PATH": "/client/bin",
        "OPENAI_API_KEY": "openai-sentinel-secret",
    })
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("shared gate fell back to a daemon-global bare command")
        ),
    )

    with mcp_runtime.bind_connection(context, child_environment=private):
        text, error, timed_out = gate._invoke_codex_classifier_once(
            "classify", timeout_s=1
        )

    _assert(text is None and timed_out is False, (text, error, timed_out))
    _assert("CODEX_BIN was not resolved" in str(error), error)


def _restore_env(name: str, old: str | None) -> None:
    if old is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = old


if __name__ == "__main__":
    test_claude_backend_remains_supported()
    test_codex_backend_does_not_call_claude_classifier()
    test_codex_backend_does_not_call_claude_adversary()
    test_cursor_backend_does_not_call_claude_or_codex_classifier()
    test_adversary_structural_log_carries_backend()
    test_connection_gate_backend_outranks_daemon_environment()
    # pytest-only monkeypatch fixture covers private child environment.
    print("\nAll gate_backends tests pass.")
