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


def test_check_kb_access_readable_fails_without_writes():
    d = _tmp()
    db_file = d / "kb.db"
    db_file.write_bytes(b"fixture")
    original_db_path = cd.paths.db_path
    original_connect = cd.db.connect_readonly
    original_access = cd.os.access
    closed: list[bool] = []

    class FakeConnection:
        def close(self):
            closed.append(True)

    try:
        cd.paths.db_path = lambda _cwd=None: db_file
        cd.db.connect_readonly = lambda _cwd=None: FakeConnection()

        cd.os.access = lambda path, mode: Path(path) == db_file.parent
        failed = cd.check_kb_access("/repo")
        _assert(failed.level == cd.FAIL, failed)
        _assert("database file is not writable" in failed.detail, failed)
        _assert("ordinary retrieval, gate, capture, and lifecycle" in failed.detail, failed)

        cd.os.access = lambda path, mode: Path(path) == db_file
        parent_failed = cd.check_kb_access("/repo")
        _assert(parent_failed.level == cd.FAIL, parent_failed)
        _assert("parent directory is not writable" in parent_failed.detail, parent_failed)

        cd.os.access = lambda _path, _mode: True
        ok = cd.check_kb_access("/repo")
        _assert(ok.level == cd.OK and "schema compatible" in ok.detail, ok)
        _assert(len(closed) == 3, closed)
    finally:
        cd.paths.db_path = original_db_path
        cd.db.connect_readonly = original_connect
        cd.os.access = original_access
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_kb_access_readable_warns_without_writes")


def test_check_kb_access_missing_db_preserves_quickstart_seed_order():
    d = _tmp()
    db_file = d / "kb.db"
    original_db_path = cd.paths.db_path
    original_connect = cd.db.connect_readonly
    original_access = cd.os.access
    original_config = cd.check_codex_config
    original_launch = cd.check_mcp_launch_target
    connect_calls: list[bool] = []

    def unexpected_connect(_cwd=None):
        connect_calls.append(True)
        raise AssertionError("missing DB should be classified before read-only open")

    try:
        cd.paths.db_path = lambda _cwd=None: db_file
        cd.db.connect_readonly = unexpected_connect
        cd.os.access = lambda path, mode: Path(path) == db_file.parent

        warned = cd.check_kb_access("/repo")
        _assert(warned.level == cd.WARN, warned)
        _assert("not initialized yet" in warned.detail, warned)
        _assert("quickstart may continue to the seed step" in warned.detail, warned)
        _assert("seed step" in warned.detail, warned)
        _assert(not connect_calls, connect_calls)

        # Quickstart runs doctor before offering seed. WARN must leave run_all
        # free of failures so that ordering remains install -> doctor -> seed.
        cd.check_codex_config = lambda *_args: cd.Check("Codex config", cd.OK, "ok")
        cd.check_mcp_launch_target = lambda *_args: cd.Check("MCP launch", cd.OK, "ok")
        checks = cd.run_all(
            config_path=d / "config.toml",
            hooks_path=d / "hooks.json",
            agents_path=d / "AGENTS.md",
            python_path=sys.executable,
            server_py=str(Path(__file__)),
            hook_py="/repo/src/hooks/codex_session_start.py",
            session_id=None,
            skip_agents=True,
            skip_hooks=True,
            skip_compact=True,
            skip_summarizer=True,
        )
        kb_check = next(c for c in checks if c.name == "Latch KB access")
        _assert(kb_check.level == cd.WARN, checks)
        _assert(not any(c.level == cd.FAIL for c in checks), checks)

        cd.os.access = lambda _path, _mode: False
        failed = cd.check_kb_access("/repo")
        _assert(failed.level == cd.FAIL, failed)
        _assert("parent directory is not writable" in failed.detail, failed)
        _assert("cannot be created" in failed.detail, failed)
        _assert(not connect_calls, connect_calls)
    finally:
        cd.paths.db_path = original_db_path
        cd.db.connect_readonly = original_connect
        cd.os.access = original_access
        cd.check_codex_config = original_config
        cd.check_mcp_launch_target = original_launch
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_kb_access_missing_db_preserves_quickstart_seed_order")


def test_check_kb_access_missing_parent_uses_nearest_existing_ancestor():
    d = _tmp()
    db_file = d / "missing" / "nested" / "kb.db"
    original_db_path = cd.paths.db_path
    original_connect = cd.db.connect_readonly
    original_access = cd.os.access
    connect_calls: list[bool] = []

    def unexpected_connect(_cwd=None):
        connect_calls.append(True)
        raise AssertionError("missing DB should not be opened read-only")

    try:
        cd.paths.db_path = lambda _cwd=None: db_file
        cd.db.connect_readonly = unexpected_connect

        warned = cd.check_kb_access("/repo")
        _assert(warned.level == cd.WARN, warned)
        _assert("nearest existing ancestor" in warned.detail, warned)
        _assert("missing parent directories can be created" in warned.detail, warned)
        _assert(not db_file.parent.exists(), "doctor must remain non-mutating")
        _assert(not connect_calls, connect_calls)

        blocker = d / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        cd.paths.db_path = lambda _cwd=None: blocker / "nested" / "kb.db"
        blocked = cd.check_kb_access("/repo")
        _assert(blocked.level == cd.FAIL, blocked)
        _assert("is not a directory" in blocked.detail, blocked)

        denied_file = d / "denied" / "nested" / "kb.db"
        cd.paths.db_path = lambda _cwd=None: denied_file
        cd.os.access = lambda _path, _mode: False
        denied = cd.check_kb_access("/repo")
        _assert(denied.level == cd.FAIL, denied)
        _assert("nearest existing ancestor" in denied.detail, denied)
        _assert("is not writable" in denied.detail, denied)
        _assert(not denied_file.parent.exists(), "doctor must remain non-mutating")
        _assert(not connect_calls, connect_calls)
    finally:
        cd.paths.db_path = original_db_path
        cd.db.connect_readonly = original_connect
        cd.os.access = original_access
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_kb_access_missing_parent_uses_nearest_existing_ancestor")


def test_check_kb_access_dangling_db_symlink_fails_closed():
    d = _tmp()
    db_file = d / "kb.db"
    original_db_path = cd.paths.db_path
    original_connect = cd.db.connect_readonly
    connect_calls: list[bool] = []

    def unexpected_connect(_cwd=None):
        connect_calls.append(True)
        raise AssertionError("dangling DB symlink should fail before open")

    try:
        try:
            db_file.symlink_to(d / "missing" / "kb.db")
        except (NotImplementedError, OSError):
            print("SKIP check_kb_access_dangling_db_symlink_fails_closed")
            return
        cd.paths.db_path = lambda _cwd=None: db_file
        cd.db.connect_readonly = unexpected_connect

        result = cd.check_kb_access("/repo")
        _assert(result.level == cd.FAIL, result)
        _assert("does not resolve to a usable KB file" in result.detail, result)
        _assert("dangling link" in result.detail, result)
        _assert(not connect_calls, connect_calls)
    finally:
        cd.paths.db_path = original_db_path
        cd.db.connect_readonly = original_connect
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_kb_access_dangling_db_symlink_fails_closed")


def test_check_kb_access_fails_when_readonly_open_fails():
    d = _tmp()
    db_file = d / "kb.db"
    db_file.write_bytes(b"fixture")
    original_db_path = cd.paths.db_path
    original_connect = cd.db.connect_readonly
    original_access = cd.os.access
    access_calls: list[Path] = []

    def fail_open(_cwd=None):
        raise RuntimeError("KB schema 9 is newer than this latch engine supports")

    try:
        cd.paths.db_path = lambda _cwd=None: db_file
        cd.db.connect_readonly = fail_open
        cd.os.access = lambda path, _mode: access_calls.append(Path(path)) or True
        failed = cd.check_kb_access("/repo")
        _assert(failed.level == cd.FAIL, failed)
        _assert("could not be opened read-only with a compatible schema" in failed.detail, failed)
        _assert("schema 9 is newer" in failed.detail, failed)
        _assert(not access_calls, access_calls)
    finally:
        cd.paths.db_path = original_db_path
        cd.db.connect_readonly = original_connect
        cd.os.access = original_access
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_kb_access_fails_when_readonly_open_fails")


def test_run_all_includes_kb_access_check():
    d = _tmp()
    original = cd.check_kb_access
    calls: list[bool] = []
    try:
        cd.check_kb_access = lambda: (
            calls.append(True)
            or cd.Check("Latch KB access", cd.OK, "read-only probe passed")
        )
        checks = cd.run_all(
            config_path=d / "missing.toml",
            hooks_path=d / "missing-hooks.json",
            agents_path=d / "missing-AGENTS.md",
            python_path=sys.executable,
            server_py=str(Path(__file__)),
            hook_py="/repo/src/hooks/codex_session_start.py",
            session_id=None,
            skip_agents=True,
            skip_hooks=True,
            skip_compact=True,
            skip_summarizer=True,
        )
        _assert(calls == [True], calls)
        _assert(any(c.name == "Latch KB access" for c in checks), checks)
    finally:
        cd.check_kb_access = original
        shutil.rmtree(d, ignore_errors=True)
    print("PASS run_all_includes_kb_access_check")


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
    test_check_kb_access_readable_fails_without_writes()
    test_check_kb_access_missing_db_preserves_quickstart_seed_order()
    test_check_kb_access_missing_parent_uses_nearest_existing_ancestor()
    test_check_kb_access_dangling_db_symlink_fails_closed()
    test_check_kb_access_fails_when_readonly_open_fails()
    test_run_all_includes_kb_access_check()
    test_check_codex_hooks()
    test_check_compact_resolution()
    test_check_summarizer_backend()
    print("\nAll codex_doctor tests pass.")
