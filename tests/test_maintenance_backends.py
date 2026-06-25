"""Backend-selection tests for heal/tree maintenance model calls."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import heal  # noqa: E402
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


if __name__ == "__main__":
    test_heal_defaults_to_claude_backend()
    test_heal_codex_backend_uses_existing_gate_env_fallback()
    test_tree_codex_backend_uses_generic_model_env()
    print("\nAll maintenance backend tests pass.")
