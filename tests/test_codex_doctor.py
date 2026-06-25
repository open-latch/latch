"""Unit tests for the Codex preview doctor."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import agents_md_sync  # noqa: E402
import codex_doctor as cd  # noqa: E402
import install_codex as ic  # noqa: E402

SID = "019ed000-0000-7000-8000-000000000001"


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="latch-codex-doctor-"))


def _write_rollout(home: Path, sid: str = SID) -> Path:
    p = home / "sessions" / "2026" / "06" / "15" / f"rollout-2026-06-15T00-00-00-{sid}.jsonl"
    p.parent.mkdir(parents=True)
    rows = [
        {"type": "session_meta", "payload": {"id": sid, "cwd": "/repo"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "compact"}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _fake_exe(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_check_codex_config_ok_and_missing():
    d = _tmp()
    try:
        config = d / "config.toml"
        body, _changes = ic.merge_config("", "/py", "/repo/src/mcp_server.py")
        config.write_text(body, encoding="utf-8")
        ok = cd.check_codex_config(config, "/py", "/repo/src/mcp_server.py")
        _assert(ok.level == cd.OK, ok)
        missing = cd.check_codex_config(d / "missing.toml", "/py", "/repo/src/mcp_server.py")
        _assert(missing.level == cd.FAIL, missing)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_codex_config_ok_and_missing")


def test_check_agents_md_status():
    d = _tmp()
    try:
        target = d / "AGENTS.md"
        agents_md_sync.sync(target, create=True)
        ok = cd.check_agents_md(target)
        _assert(ok.level == cd.OK, ok)
        target.write_text("# project only\n", encoding="utf-8")
        bad = cd.check_agents_md(target)
        _assert(bad.level == cd.FAIL and "status is missing" in bad.detail, bad)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_agents_md_status")


def test_check_mcp_launch_target():
    d = _tmp()
    try:
        server = d / "mcp_server.py"
        server.write_text("# ok\n", encoding="utf-8")
        ok = cd.check_mcp_launch_target(sys.executable, str(server))
        _assert(ok.level == cd.OK, ok)
        bad = cd.check_mcp_launch_target("/no/such/python", str(server))
        _assert(bad.level == cd.FAIL, bad)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_mcp_launch_target")


def test_check_codex_hooks():
    d = _tmp()
    try:
        hooks = d / "hooks.json"
        config = d / "config.toml"
        hook_py = "/repo/src/hooks/codex_session_start.py"
        body, _ = cd.codex_hooks.merge_hooks("", "/py", hook_py)
        hooks.write_text(body, encoding="utf-8")
        config.write_text(
            f'[hooks.state."{hooks}:session_start:0:0"]\n'
            'trusted_hash = "sha256:test"\n',
            encoding="utf-8",
        )
        checks = cd.check_codex_hooks(hooks, config, "/py", hook_py)
        _assert([c.level for c in checks] == [cd.OK, cd.WARN], checks)

        missing = cd.check_codex_hooks(d / "missing.json", config, "/py", hook_py)
        _assert(missing[0].level == cd.FAIL, missing)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_codex_hooks")


def test_check_compact_resolution():
    home = _tmp()
    old_home = os.environ.get("CODEX_HOME")
    old_sid = os.environ.get("CODEX_THREAD_ID")
    try:
        os.environ["CODEX_HOME"] = str(home)
        os.environ.pop("CODEX_THREAD_ID", None)
        warn = cd.check_compact_resolution(None)
        _assert(warn.level == cd.WARN, warn)
        fail = cd.check_compact_resolution(None, require=True)
        _assert(fail.level == cd.FAIL, fail)
        p = _write_rollout(home)
        ok = cd.check_compact_resolution(SID, require=True)
        _assert(ok.level == cd.OK and str(p) in ok.detail, ok)
    finally:
        if old_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = old_home
        if old_sid is None:
            os.environ.pop("CODEX_THREAD_ID", None)
        else:
            os.environ["CODEX_THREAD_ID"] = old_sid
        shutil.rmtree(home, ignore_errors=True)
    print("PASS check_compact_resolution")


def test_check_summarizer_backend():
    d = _tmp()
    try:
        codex_ok = _fake_exe(
            d / "codex-ok",
            "out=''\n"
            "while [ $# -gt 0 ]; do\n"
            "  if [ \"$1\" = '--output-last-message' ]; then shift; out=\"$1\"; fi\n"
            "  shift || break\n"
            "done\n"
            "printf '%s\\n' '{\"ok\": true}' > \"$out\"\n"
            "printf '%s\\n' '{\"ok\": true}'\n",
        )
        codex = cd.check_summarizer_backend(
            backend="codex", codex_bin=str(codex_ok), timeout_s=1,
        )
        _assert(codex.level == cd.OK and "exec reachable" in codex.detail, codex)

        ok_bin = _fake_exe(
            d / "claude-ok",
            "printf '%s\\n' '{\"type\":\"result\",\"result\":\"{\\\"ok\\\": true}\"}'\n",
        )
        ok = cd.check_summarizer_backend(
            backend="claude", claude_bin=str(ok_bin), timeout_s=1,
        )
        _assert(ok.level == cd.OK, ok)

        fail_bin = _fake_exe(
            d / "claude-fail",
            "printf '%s\\n' '{\"api_error_status\":400,\"result\":\"Credit balance is too low\"}'\n"
            "exit 1\n",
        )
        fail = cd.check_summarizer_backend(
            backend="claude", claude_bin=str(fail_bin), timeout_s=1,
        )
        _assert(fail.level == cd.FAIL and "Credit balance is too low" in fail.detail, fail)

        missing_codex = cd.check_summarizer_backend(
            backend="codex", codex_bin=str(d / "missing-codex"), timeout_s=1,
        )
        _assert(missing_codex.level == cd.FAIL and "not found" in missing_codex.detail,
                missing_codex)

        missing_claude = cd.check_summarizer_backend(
            backend="claude", claude_bin=str(d / "missing-claude"), timeout_s=1,
        )
        _assert(missing_claude.level == cd.FAIL and "not found" in missing_claude.detail,
                missing_claude)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_summarizer_backend")


if __name__ == "__main__":
    test_check_codex_config_ok_and_missing()
    test_check_agents_md_status()
    test_check_mcp_launch_target()
    test_check_codex_hooks()
    test_check_compact_resolution()
    test_check_summarizer_backend()
    print("\nAll codex_doctor tests pass.")
