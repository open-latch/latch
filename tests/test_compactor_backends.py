"""Unit tests for compactor summarizer backends."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import compactor  # noqa: E402


COMPACTION_JSON = (
    '{"session_summary":{"title":"Codex compact","body":"Summary body"},'
    '"extracted_nodes":[],"links":[]}'
)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="latch-compactor-backend-"))


def _fake_exe(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_codex(path: Path) -> Path:
    return _fake_exe(
        path,
        "printf '%s\\n' \"$@\" > \"$FAKE_CODEX_ARGS\"\n"
        "out=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  if [ \"$1\" = '--output-last-message' ]; then shift; out=\"$1\"; fi\n"
        "  shift || break\n"
        "done\n"
        "cat >/dev/null\n"
        "printf '%s\\n' \"$FAKE_CODEX_RESPONSE\" > \"$out\"\n"
        "printf '%s\\n' \"$FAKE_CODEX_RESPONSE\"\n",
    )


def _fake_claude(path: Path) -> Path:
    return _fake_exe(
        path,
        "printf '%s\\n' \"$@\" > \"$FAKE_CLAUDE_ARGS\"\n"
        "cat >/dev/null\n"
        "printf '%s\\n' \"$FAKE_CLAUDE_RESPONSE\"\n",
    )


def test_invoke_claude_once_disallows_action_tools():
    d = _tmp()
    old_response = os.environ.get("FAKE_CLAUDE_RESPONSE")
    old_args = os.environ.get("FAKE_CLAUDE_ARGS")
    try:
        args_file = d / "args.txt"
        fake = _fake_claude(d / "claude")
        os.environ["FAKE_CLAUDE_RESPONSE"] = (
            '{"type":"result","result":"' + COMPACTION_JSON.replace('"', '\\"') + '"}'
        )
        os.environ["FAKE_CLAUDE_ARGS"] = str(args_file)
        raw, err = compactor._invoke_claude_once(
            "summarize this", claude_bin=str(fake), timeout_s=1,
        )
        _assert(err is None, err)
        _assert(raw and "Codex compact" in raw, raw)
        args = args_file.read_text(encoding="utf-8").splitlines()
        _assert(
            args[:4] == ["-p", "--no-session-persistence", "--output-format", "json"],
            args,
        )
        _assert("--disallowedTools" in args, args)
        denied = args[args.index("--disallowedTools") + 1]
        _assert(denied == compactor.CLAUDE_COMPACTOR_DISALLOWED_TOOLS, args)
        for tool in ("Bash", "Edit", "Write", "NotebookEdit"):
            _assert(tool in denied.split(","), args)
    finally:
        if old_response is None:
            os.environ.pop("FAKE_CLAUDE_RESPONSE", None)
        else:
            os.environ["FAKE_CLAUDE_RESPONSE"] = old_response
        if old_args is None:
            os.environ.pop("FAKE_CLAUDE_ARGS", None)
        else:
            os.environ["FAKE_CLAUDE_ARGS"] = old_args
        shutil.rmtree(d, ignore_errors=True)
    print("PASS invoke_claude_once_disallows_action_tools")


def test_invoke_codex_once_uses_isolated_exec_shape():
    d = _tmp()
    old_response = os.environ.get("FAKE_CODEX_RESPONSE")
    old_args = os.environ.get("FAKE_CODEX_ARGS")
    try:
        args_file = d / "args.txt"
        fake = _fake_codex(d / "codex")
        os.environ["FAKE_CODEX_RESPONSE"] = COMPACTION_JSON
        os.environ["FAKE_CODEX_ARGS"] = str(args_file)
        raw, err = compactor._invoke_codex_once(
            "summarize this", codex_bin=str(fake), timeout_s=1,
        )
        _assert(err is None, err)
        _assert(raw and "Codex compact" in raw, raw)
        args = args_file.read_text(encoding="utf-8").splitlines()
        _assert(args[:2] == ["exec", "--ignore-user-config"], args)
        _assert("--ephemeral" in args, args)
        _assert("--skip-git-repo-check" in args, args)
        _assert("--sandbox" in args and "read-only" in args, args)
        _assert(args[-1] == "-", args)
    finally:
        if old_response is None:
            os.environ.pop("FAKE_CODEX_RESPONSE", None)
        else:
            os.environ["FAKE_CODEX_RESPONSE"] = old_response
        if old_args is None:
            os.environ.pop("FAKE_CODEX_ARGS", None)
        else:
            os.environ["FAKE_CODEX_ARGS"] = old_args
        shutil.rmtree(d, ignore_errors=True)
    print("PASS invoke_codex_once_uses_isolated_exec_shape")


def test_invoke_summarizer_parses_codex_result():
    d = _tmp()
    old_bin = compactor.CODEX_BIN
    old_response = os.environ.get("FAKE_CODEX_RESPONSE")
    old_args = os.environ.get("FAKE_CODEX_ARGS")
    try:
        fake = _fake_codex(d / "codex")
        os.environ["FAKE_CODEX_RESPONSE"] = COMPACTION_JSON
        os.environ["FAKE_CODEX_ARGS"] = str(d / "args.txt")
        compactor.CODEX_BIN = str(fake)
        payload = {
            "prior_summary": "",
            "related_kb_nodes": [],
            "transcript": "[user] do work",
            "project_path": "/repo",
            "session_id": "sid",
        }
        parsed = compactor._invoke_summarizer(payload, backend="codex")
        _assert(parsed is not None, "expected parsed result")
        _assert(parsed["session_summary"]["title"] == "Codex compact", parsed)
    finally:
        compactor.CODEX_BIN = old_bin
        if old_response is None:
            os.environ.pop("FAKE_CODEX_RESPONSE", None)
        else:
            os.environ["FAKE_CODEX_RESPONSE"] = old_response
        if old_args is None:
            os.environ.pop("FAKE_CODEX_ARGS", None)
        else:
            os.environ["FAKE_CODEX_ARGS"] = old_args
        shutil.rmtree(d, ignore_errors=True)
    print("PASS invoke_summarizer_parses_codex_result")


if __name__ == "__main__":
    test_invoke_claude_once_disallows_action_tools()
    test_invoke_codex_once_uses_isolated_exec_shape()
    test_invoke_summarizer_parses_codex_result()
    print("\nAll compactor_backends tests pass.")
