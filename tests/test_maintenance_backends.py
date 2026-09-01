"""Backend-selection tests for heal/tree maintenance model calls."""
from __future__ import annotations

import ast
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
    "LATCH_CLAUDE_MODEL",
    "LATCH_MAINTENANCE_CLAUDE_MODEL",
    "LATCH_HEAL_CLAUDE_MODEL",
    "LATCH_TREE_CLAUDE_MODEL",
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
        _assert(
            args == [
                "-p", "--no-session-persistence", "--output-format", "json",
                "--model", "sonnet",
            ],
            args,
        )
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


def test_claude_model_resolution_prefers_purpose_then_maintenance_then_generic(monkeypatch):
    for name in (
        "LATCH_HEAL_CLAUDE_MODEL",
        "LATCH_MAINTENANCE_CLAUDE_MODEL",
        "LATCH_CLAUDE_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    _assert(model_backends.DEFAULT_CLAUDE_MODEL == "sonnet", "public default drifted")
    _assert(
        model_backends.resolve_claude_model() == "sonnet",
        "unset resolution must use the Latch-owned default",
    )

    monkeypatch.setenv("LATCH_CLAUDE_MODEL", "generic-opus")
    _assert(model_backends.resolve_claude_model() == "generic-opus", "generic lost")

    monkeypatch.setenv("LATCH_MAINTENANCE_CLAUDE_MODEL", "maintenance-opus")
    _assert(
        model_backends.resolve_claude_model() == "maintenance-opus",
        "maintenance override must outrank generic",
    )

    monkeypatch.setenv("LATCH_HEAL_CLAUDE_MODEL", "heal-opus")
    _assert(
        model_backends.resolve_claude_model(("LATCH_HEAL_CLAUDE_MODEL",))
        == "heal-opus",
        "purpose override must outrank the generic chain",
    )


def test_codex_and_cursor_models_have_latch_owned_defaults(monkeypatch):
    for name in (
        "LATCH_CODEX_MODEL",
        "LATCH_MAINTENANCE_CODEX_MODEL",
        "LATCH_CURSOR_MODEL",
        "LATCH_MAINTENANCE_CURSOR_MODEL",
        "CURSOR_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    _assert(model_backends.DEFAULT_CODEX_MODEL == "gpt-5", "Codex default drifted")
    _assert(model_backends.DEFAULT_CURSOR_MODEL == "gpt-5", "Cursor default drifted")
    _assert(model_backends.resolve_codex_model() == "gpt-5", "Codex inherited CLI default")
    _assert(model_backends.resolve_cursor_model() == "gpt-5", "Cursor inherited CLI default")

    monkeypatch.setenv("LATCH_CODEX_MODEL", "gpt-generic")
    monkeypatch.setenv("LATCH_MAINTENANCE_CODEX_MODEL", "gpt-maintenance")
    _assert(
        model_backends.resolve_codex_model(("LATCH_TREE_CODEX_MODEL",))
        == "gpt-maintenance",
        "Codex maintenance selector lost",
    )

    monkeypatch.setenv("LATCH_CURSOR_MODEL", "cursor-generic")
    monkeypatch.setenv("LATCH_MAINTENANCE_CURSOR_MODEL", "cursor-maintenance")
    _assert(
        model_backends.resolve_cursor_model(("LATCH_TREE_CURSOR_MODEL",))
        == "cursor-maintenance",
        "Cursor maintenance selector lost",
    )


def test_tree_claude_model_override_reaches_argv(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return SimpleNamespace(
            returncode=0,
            stdout=TREE_JSON,
            stderr="",
        )

    monkeypatch.setenv("LATCH_TREE_CLAUDE_MODEL", "tree-opus")
    monkeypatch.setattr(model_backends.subprocess, "run", fake_run)
    result = model_backends.invoke_prompt(
        "summarize",
        backend="claude",
        timeout_s=1,
        purpose="tree_summary",
        claude_bin=str(tmp_path / "claude"),
        claude_model_env=("LATCH_TREE_CLAUDE_MODEL",),
    )

    _assert(result.error is None, result)
    _assert(result.model == "tree-opus", result)
    _assert(captured["args"][-2:] == ["--model", "tree-opus"], captured)


def test_invalid_claude_model_fails_structured_without_launch(monkeypatch):
    monkeypatch.setenv("LATCH_CLAUDE_MODEL", "   ")
    monkeypatch.setattr(
        model_backends.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid model must fail before subprocess launch")
        ),
    )

    result = model_backends.invoke_prompt(
        "summarize",
        backend="claude",
        timeout_s=1,
        purpose="maintenance",
    )

    _assert(result.text is None and result.timed_out is False, result)
    _assert(result.backend == "claude" and result.model is None, result)
    _assert("empty" in str(result.error).lower(), result)


def test_empty_model_selectors_fail_before_launch_for_all_backends(monkeypatch):
    monkeypatch.setattr(
        model_backends.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("empty model must fail before subprocess launch")
        ),
    )
    monkeypatch.setattr(
        model_backends.cursor_backend,
        "invoke_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("empty Cursor model must fail before invocation")
        ),
    )

    for backend, selector in (
        ("claude", "LATCH_CLAUDE_MODEL"),
        ("codex", "LATCH_CODEX_MODEL"),
        ("cursor", "LATCH_CURSOR_MODEL"),
    ):
        monkeypatch.setenv(selector, "")
        result = model_backends.invoke_prompt(
            "summarize",
            backend=backend,
            timeout_s=1,
            purpose="maintenance",
        )
        _assert(result.text is None and result.model is None, result)
        _assert("empty" in str(result.error).lower(), result)


def test_all_claude_model_selectors_are_allowlisted_but_not_sensitive():
    expected = {
        "LATCH_GATE_CLAUDE_MODEL",
        "LATCH_COMPACTOR_CLAUDE_MODEL",
        "LATCH_HEAL_CLAUDE_MODEL",
        "LATCH_TREE_CLAUDE_MODEL",
        "LATCH_MAINTENANCE_CLAUDE_MODEL",
        "LATCH_CLAUDE_MODEL",
    }
    _assert(set(mcp_runtime.CLAUDE_MODEL_ENV_VARS) == expected, "selector set drifted")
    _assert(
        expected <= set(mcp_runtime.CONNECTION_CHILD_BACKEND_ENV_VARS["claude"]),
        "shared connections would silently drop a Claude model override",
    )
    _assert(
        expected.isdisjoint(mcp_runtime.SENSITIVE_CHILD_ENV_VARS),
        "model selectors must not be treated as credentials",
    )


def test_all_backend_model_selectors_are_allowlisted_but_not_sensitive():
    expected = {
        "claude": set(mcp_runtime.CLAUDE_MODEL_ENV_VARS),
        "codex": {
            "LATCH_GATE_CODEX_MODEL", "CODEX_GATE_MODEL",
            "LATCH_COMPACTOR_CODEX_MODEL", "CODEX_COMPACTOR_MODEL",
            "LATCH_HEAL_CODEX_MODEL", "CODEX_HEAL_MODEL",
            "LATCH_TREE_CODEX_MODEL", "CODEX_TREE_MODEL",
            "LATCH_MAINTENANCE_CODEX_MODEL", "CODEX_MAINTENANCE_MODEL",
            "LATCH_CODEX_MODEL",
        },
        "cursor": {
            "LATCH_GATE_CURSOR_MODEL", "CURSOR_GATE_MODEL",
            "LATCH_COMPACTOR_CURSOR_MODEL", "CURSOR_COMPACTOR_MODEL",
            "LATCH_HEAL_CURSOR_MODEL", "CURSOR_HEAL_MODEL",
            "LATCH_TREE_CURSOR_MODEL", "CURSOR_TREE_MODEL",
            "LATCH_MAINTENANCE_CURSOR_MODEL",
            "LATCH_CURSOR_MODEL", "CURSOR_MODEL",
        },
    }
    _assert(
        {name: set(values) for name, values in mcp_runtime.MODEL_ENV_VARS_BY_BACKEND.items()}
        == expected,
        "backend selector policy drifted",
    )
    for backend, selectors in expected.items():
        _assert(
            selectors <= set(mcp_runtime.CONNECTION_CHILD_BACKEND_ENV_VARS[backend]),
            f"shared connections drop {backend} model selectors",
        )
        _assert(
            selectors.isdisjoint(mcp_runtime.SENSITIVE_CHILD_ENV_VARS),
            f"{backend} model selectors were classified as credentials",
        )


def test_no_claude_subprocess_invocation_omits_model_flag():
    root = Path(__file__).resolve().parent.parent / "src"
    offenders = []
    for path in sorted(root.glob("*.py")):
        tree_ast = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree_ast):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            constants = {
                value.value
                for value in ast.walk(node)
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            }
            if "-p" in constants and "subprocess" in ast.unparse(node):
                if "--model" not in constants:
                    offenders.append(f"{path.name}:{node.name}")
    _assert(not offenders, f"Claude subprocesses without --model: {offenders}")


def test_codex_maintenance_permission_error_is_structured(monkeypatch):
    def deny_launch(*_args, **_kwargs):
        raise PermissionError(
            5,
            "Access is denied",
            "C:/Program Files/WindowsApps/codex.exe",
        )

    monkeypatch.setattr(model_backends.subprocess, "run", deny_launch)

    result = model_backends.invoke_prompt(
        "summarize",
        backend="codex",
        timeout_s=1,
        purpose="maintenance",
        codex_bin="C:/Program Files/WindowsApps/codex.exe",
    )

    _assert(result.text is None, result)
    _assert(result.timed_out is False, result)
    _assert(result.backend == "codex", result)
    _assert("PermissionError" in str(result.error), result)
    _assert("Access is denied" in str(result.error), result)


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
        "LATCH_MAINTENANCE_CLAUDE_MODEL": "shared-opus",
    })
    monkeypatch.setattr(model_backends.subprocess, "run", fake_run)
    with mcp_runtime.bind_connection(context, child_environment=private):
        result = model_backends._invoke_claude(
            "prompt", timeout_s=2, purpose="test"
        )
    _assert(result.text == "<redacted>", result)
    _assert(result.model == "shared-opus", result)
    _assert(captured["args"][0] == "/claude/bin/claude", captured)
    _assert(captured["args"][-2:] == ["--model", "shared-opus"], captured)
    env = captured["kwargs"]["env"]
    _assert(env["ANTHROPIC_API_KEY"] == "anthropic-sentinel-secret", env)
    _assert(env["LATCH_MAINTENANCE_CLAUDE_MODEL"] == "shared-opus", env)
    _assert("OPENAI_API_KEY" not in env, env)
    _assert(
        "LATCH_MAINTENANCE_CLAUDE_MODEL" not in mcp_runtime.SENSITIVE_CHILD_ENV_VARS,
        "model selectors are observability metadata, not credentials",
    )


if __name__ == "__main__":
    test_heal_defaults_to_claude_backend()
    test_heal_codex_backend_uses_existing_gate_env_fallback()
    test_tree_codex_backend_uses_generic_model_env()
    test_heal_and_tree_use_cursor_maintenance_backend()
    test_connection_maintenance_backend_outranks_daemon_environment()
    # pytest-only monkeypatch fixture covers private child environment.
    print("\nAll maintenance backend tests pass.")
