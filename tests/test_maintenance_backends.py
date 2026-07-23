"""Backend-selection tests for heal/tree maintenance model calls."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import heal  # noqa: E402
import mcp_runtime  # noqa: E402
import model_backends  # noqa: E402
import tree  # noqa: E402


HEAL_JSON = '{"decision":"keep_both","reason":"distinct enough"}'
TREE_JSON = '{"title":"deployment notes","body":"summarizes deployment decisions"}'
BACKEND_ENV = (
    "LATCH_MAINTENANCE_BACKEND",
    "CLAUDE_KB_MAINTENANCE_BACKEND",
    "LATCH_MODEL_BACKEND",
    "LATCH_GATE_BACKEND",
    "CLAUDE_KB_GATE_BACKEND",
    "FAKE_MODEL_RESPONSE",
    "FAKE_MODEL_ARGS",
    "CLAUDE_KB_IN_COMPACT",
)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="latch-maint-backends-"))


def _fake_exe(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_claude(path: Path) -> Path:
    return _fake_exe(
        path,
        "printf '%s\\n' \"$@\" > \"$FAKE_MODEL_ARGS\"\n"
        "cat >/dev/null\n"
        "printf '%s\\n' \"$FAKE_MODEL_RESPONSE\"\n",
    )


def _fake_codex(path: Path) -> Path:
    return _fake_exe(
        path,
        "printf '%s\\n' \"$@\" > \"$FAKE_MODEL_ARGS\"\n"
        "out=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  if [ \"$1\" = '--output-last-message' ]; then shift; out=\"$1\"; fi\n"
        "  shift || break\n"
        "done\n"
        "cat >/dev/null\n"
        "if [ -n \"$out\" ]; then printf '%s\\n' \"$FAKE_MODEL_RESPONSE\" > \"$out\"; fi\n"
        "printf '%s\\n' \"$FAKE_MODEL_RESPONSE\"\n",
    )


def _restore_env(snapshot: dict[str, str | None]) -> None:
    for name, old in snapshot.items():
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


def _snapshot_env() -> dict[str, str | None]:
    return {name: os.environ.get(name) for name in BACKEND_ENV}


def _old_bins() -> tuple[str, str]:
    return model_backends.CLAUDE_BIN, model_backends.CODEX_BIN


def _restore_bins(old: tuple[str, str]) -> None:
    model_backends.CLAUDE_BIN, model_backends.CODEX_BIN = old


def _nodes() -> tuple[dict, dict]:
    new = {"kind": "fact", "title": "new", "body": "new body"}
    old = {
        "id": 1,
        "kind": "fact",
        "title": "old",
        "body": "old body",
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
    }
    return new, old


def test_heal_defaults_to_claude_backend():
    d = _tmp()
    env = _snapshot_env()
    bins = _old_bins()
    try:
        args_file = d / "claude_args.txt"
        model_backends.CLAUDE_BIN = str(_fake_claude(d / "claude"))
        model_backends.CODEX_BIN = str(d / "missing-codex")
        os.environ["FAKE_MODEL_RESPONSE"] = HEAL_JSON
        os.environ["FAKE_MODEL_ARGS"] = str(args_file)
        for name in BACKEND_ENV:
            if name not in ("FAKE_MODEL_RESPONSE", "FAKE_MODEL_ARGS"):
                os.environ.pop(name, None)

        out = heal.arbitrate(*_nodes(), similarity=0.91)

        _assert(out["decision"] == "keep_both", out)
        _assert(out["backend"] == "claude", out)
        args = args_file.read_text(encoding="utf-8").splitlines()
        _assert(args == ["-p", "--no-session-persistence", "--output-format", "json"], args)
    finally:
        _restore_env(env)
        _restore_bins(bins)
        shutil.rmtree(d, ignore_errors=True)
    print("PASS heal_defaults_to_claude_backend")


def test_heal_codex_backend_uses_existing_gate_env_fallback():
    d = _tmp()
    env = _snapshot_env()
    bins = _old_bins()
    try:
        args_file = d / "codex_args.txt"
        model_backends.CODEX_BIN = str(_fake_codex(d / "codex"))
        model_backends.CLAUDE_BIN = str(d / "missing-claude")
        os.environ["LATCH_GATE_BACKEND"] = "codex"
        os.environ["FAKE_MODEL_RESPONSE"] = HEAL_JSON
        os.environ["FAKE_MODEL_ARGS"] = str(args_file)

        new, old = _nodes()
        out = heal.arbitrate(new, old, similarity=0.91)
        out2 = heal._arbitrate_nightly(old, {**new, "id": 2}, similarity=0.71)

        _assert(out["decision"] == "keep_both" and out["backend"] == "codex", out)
        _assert(out2["decision"] == "keep_both" and out2["backend"] == "codex", out2)
        args = args_file.read_text(encoding="utf-8").splitlines()
        _assert(args[:2] == ["exec", "--ignore-user-config"], args)
        _assert("--ignore-rules" in args, args)
        _assert("--ephemeral" in args, args)
        _assert("--sandbox" in args and "read-only" in args, args)
        _assert(args[-1] == "-", args)
    finally:
        _restore_env(env)
        _restore_bins(bins)
        shutil.rmtree(d, ignore_errors=True)
    print("PASS heal_codex_backend_uses_existing_gate_env_fallback")


def test_tree_codex_backend_uses_generic_model_env():
    d = _tmp()
    env = _snapshot_env()
    bins = _old_bins()
    try:
        args_file = d / "tree_args.txt"
        model_backends.CODEX_BIN = str(_fake_codex(d / "codex"))
        model_backends.CLAUDE_BIN = str(d / "missing-claude")
        os.environ["LATCH_MODEL_BACKEND"] = "codex"
        os.environ["FAKE_MODEL_RESPONSE"] = TREE_JSON
        os.environ["FAKE_MODEL_ARGS"] = str(args_file)

        out = tree._invoke_summary([
            {"kind": "fact", "title": "deploy", "body": "deploy with compose"}
        ])

        _assert(out == {
            "title": "deployment notes",
            "body": "summarizes deployment decisions",
        }, out)
        args = args_file.read_text(encoding="utf-8").splitlines()
        _assert(args[:2] == ["exec", "--ignore-user-config"], args)
        _assert("--ignore-rules" in args, args)
        _assert("--ephemeral" in args, args)
        _assert("--sandbox" in args and "read-only" in args, args)
        _assert(args[-1] == "-", args)
    finally:
        _restore_env(env)
        _restore_bins(bins)
        shutil.rmtree(d, ignore_errors=True)
    print("PASS tree_codex_backend_uses_generic_model_env")


def test_heal_and_tree_use_cursor_maintenance_backend():
    env = _snapshot_env()
    original = model_backends.cursor_backend.invoke_prompt
    calls = []
    try:
        os.environ["LATCH_MAINTENANCE_BACKEND"] = "cursor"

        def fake_cursor(prompt, *, timeout_s, purpose, agent_bin=None, model=None):
            calls.append({
                "prompt": prompt,
                "timeout_s": timeout_s,
                "purpose": purpose,
                "agent_bin": agent_bin,
                "model": model,
            })
            response = TREE_JSON if purpose == "tree_summary" else HEAL_JSON
            return response, None, False

        model_backends.cursor_backend.invoke_prompt = fake_cursor
        heal_out = heal.arbitrate(*_nodes(), similarity=0.91)
        tree_out = tree._invoke_summary([
            {"kind": "fact", "title": "deploy", "body": "deploy with compose"}
        ])

        _assert(heal_out["decision"] == "keep_both", heal_out)
        _assert(heal_out["backend"] == "cursor", heal_out)
        _assert(tree_out == {
            "title": "deployment notes",
            "body": "summarizes deployment decisions",
        }, tree_out)
        _assert([call["purpose"] for call in calls] == ["arbitrate", "tree_summary"], calls)
    finally:
        model_backends.cursor_backend.invoke_prompt = original
        _restore_env(env)
    print("PASS heal_and_tree_use_cursor_maintenance_backend")


def test_connection_maintenance_backend_outranks_daemon_environment():
    env = _snapshot_env()
    context = mcp_runtime.ConnectionContext(
        connection_id="cursor-maintenance",
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
        os.environ["LATCH_MAINTENANCE_BACKEND"] = "claude"
        with mcp_runtime.bind_connection(context):
            _assert(
                model_backends.resolve_backend(
                    env_names=model_backends.MAINTENANCE_BACKEND_ENV
                )
                == "cursor",
                "daemon environment won over connection maintenance backend",
            )
            _assert(
                model_backends.resolve_backend(
                    "codex", env_names=model_backends.MAINTENANCE_BACKEND_ENV
                )
                == "codex",
                "explicit maintenance backend must remain authoritative",
            )
        _assert(
            model_backends.resolve_backend(
                env_names=model_backends.MAINTENANCE_BACKEND_ENV
            )
            == "claude",
            "legacy environment fallback broke",
        )
    finally:
        _restore_env(env)
    print("PASS connection_maintenance_backend_outranks_daemon_environment")


def test_private_claude_environment_is_backend_scoped_and_redacted(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout="anthropic-sentinel-secret",
            stderr="",
        )

    context = mcp_runtime.ConnectionContext(
        connection_id="claude-private",
        project_cwd="/tmp/project",
        session_id=None,
        session_source="test",
        proxy_pid=123,
        proxy_started_at="now",
        runtime_key="test",
        gate_backend="claude",
        maintenance_backend="claude",
    )
    private = mcp_runtime.validate_child_environment({
        "PATH": "/claude/bin",
        "CLAUDE_BIN": "/claude/bin/claude",
        "ANTHROPIC_API_KEY": "anthropic-sentinel-secret",
        "OPENAI_API_KEY": "openai-sentinel-secret",
    })
    monkeypatch.setattr(model_backends.subprocess, "run", fake_run)
    with mcp_runtime.bind_connection(context, child_environment=private):
        result = model_backends._invoke_claude(
            "prompt", timeout_s=2, purpose="test"
        )
    _assert(result.text == "<redacted>", result)
    _assert(captured["args"][0] == "/claude/bin/claude", captured)
    env = captured["kwargs"]["env"]
    _assert(env["ANTHROPIC_API_KEY"] == "anthropic-sentinel-secret", env)
    _assert("OPENAI_API_KEY" not in env, env)


if __name__ == "__main__":
    test_heal_defaults_to_claude_backend()
    test_heal_codex_backend_uses_existing_gate_env_fallback()
    test_tree_codex_backend_uses_generic_model_env()
    test_heal_and_tree_use_cursor_maintenance_backend()
    test_connection_maintenance_backend_outranks_daemon_environment()
    # pytest-only monkeypatch fixture covers private child environment.
    print("\nAll maintenance backend tests pass.")
