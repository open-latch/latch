"""Unit tests for kb_gate model backend selection."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import gate  # noqa: E402
import log_utils  # noqa: E402
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


def _restore_env(name: str, old: str | None) -> None:
    if old is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = old


if __name__ == "__main__":
    test_claude_backend_remains_supported()
    test_codex_backend_does_not_call_claude_classifier()
    test_codex_backend_does_not_call_claude_adversary()
    test_adversary_structural_log_carries_backend()
    print("\nAll gate_backends tests pass.")
