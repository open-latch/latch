"""Unit tests for the Cursor preview doctor."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import agents_md_sync  # noqa: E402
import cursor_rules_sync  # noqa: E402
import cursor_hooks  # noqa: E402
import cursor_doctor as cd  # noqa: E402
import install_cursor as ic  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="latch-cursor-doctor-"))


def _fake_exe(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_check_cursor_config_ok_and_missing():
    d = _tmp()
    try:
        config = d / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        body, _changes = ic.merge_mcp_config("", "/py", "/repo/src/mcp_server.py")
        config.write_text(body, encoding="utf-8")
        ok = cd.check_cursor_config(config, "/py", "/repo/src/mcp_server.py")
        _assert(ok.level == cd.OK, ok)
        missing = cd.check_cursor_config(d / "missing.json", "/py", "/repo/src/mcp_server.py")
        _assert(missing.level == cd.FAIL, missing)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_cursor_config_ok_and_missing")


def test_json_mode_reports_malformed_cursor_config():
    d = _tmp()
    try:
        config = d / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text("{bad json", encoding="utf-8")
        server = d / "mcp_server.py"
        server.write_text("# ok\n", encoding="utf-8")
        agents = d / "AGENTS.md"
        agents_md_sync.sync(agents, create=True)

        out = io.StringIO()
        with redirect_stdout(out):
            rc = cd.main([
                "--json",
                "--skip-cli",
                "--skip-commands",
                "--python", sys.executable,
                "--mcp-json", str(config),
                "--agents-md", str(agents),
            ])
        payload = json.loads(out.getvalue())
        config_check = payload["checks"][0]
        _assert(rc == 1, rc)
        _assert(payload["ok"] is False, payload)
        _assert(config_check["level"] == cd.FAIL, config_check)
        _assert("not valid JSON" in config_check["detail"], config_check)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS json_mode_reports_malformed_cursor_config")


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


def test_check_cursor_rule_status():
    d = _tmp()
    try:
        target = d / ".cursor" / "rules" / "latch.mdc"
        cursor_rules_sync.sync(target)
        ok = cd.check_cursor_rule(target)
        _assert(ok.level == cd.OK, ok)
        target.write_text("custom\n", encoding="utf-8")
        bad = cd.check_cursor_rule(target)
        _assert(bad.level == cd.FAIL and "status is drift" in bad.detail, bad)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_cursor_rule_status")


def test_check_cursor_commands_status():
    d = _tmp()
    try:
        commands = d / ".cursor" / "commands"
        ic.sync_cursor_commands(commands)
        ok = cd.check_cursor_commands(commands)
        _assert(ok.level == cd.OK, ok)
        (commands / "latch-gate.md").write_text("custom\n", encoding="utf-8")
        bad = cd.check_cursor_commands(commands)
        _assert(bad.level == cd.FAIL and "drifted" in bad.detail, bad)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_cursor_commands_status")


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


def test_check_cursor_cli_mcp_warns_when_missing():
    d = _tmp()
    try:
        check = cd.check_cursor_cli_mcp(agent_bin=str(d / "missing-agent"), timeout_s=1)
        _assert(check.level == cd.WARN and "not found" in check.detail, check)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_cursor_cli_mcp_warns_when_missing")


def test_check_cursor_cli_mcp_ok_and_failure():
    d = _tmp()
    old_args = os.environ.get("FAKE_CURSOR_AGENT_ARGS")
    try:
        args_file = d / "args.txt"
        os.environ["FAKE_CURSOR_AGENT_ARGS"] = str(args_file)
        ok_agent = _fake_exe(
            d / "agent-ok",
            "printf '%s\\n' \"$@\" >> \"$FAKE_CURSOR_AGENT_ARGS\"\n"
            "printf '%s\\n' 'latch_search latch_get latch_recent latch_gate'\n",
        )
        ok = cd.check_cursor_cli_mcp(agent_bin=str(ok_agent), timeout_s=1)
        _assert(ok.level == cd.OK, ok)
        args = args_file.read_text(encoding="utf-8")
        _assert("mcp\nlist\n" in args and "mcp\nlist-tools\nlatch\n" in args, args)

        fail_agent = _fake_exe(
            d / "agent-fail",
            "printf '%s\\n' 'nope' >&2\n"
            "exit 2\n",
        )
        warn = cd.check_cursor_cli_mcp(agent_bin=str(fail_agent), timeout_s=1)
        _assert(warn.level == cd.WARN and "exit 2" in warn.detail, warn)
    finally:
        if old_args is None:
            os.environ.pop("FAKE_CURSOR_AGENT_ARGS", None)
        else:
            os.environ["FAKE_CURSOR_AGENT_ARGS"] = old_args
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_cursor_cli_mcp_ok_and_failure")


def test_check_cursor_cli_mcp_fails_when_critical_tools_missing():
    d = _tmp()
    try:
        agent = _fake_exe(
            d / "agent-missing-tools",
            "printf '%s\\n' 'latch_search latch_gate'\n",
        )
        check = cd.check_cursor_cli_mcp(agent_bin=str(agent), timeout_s=1)
        _assert(check.level == cd.FAIL, check)
        _assert("latch_get" in check.detail and "latch_recent" in check.detail,
                check)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS check_cursor_cli_mcp_fails_when_critical_tools_missing")


def test_run_all_static_and_cli_checks():
    d = _tmp()
    try:
        config = d / ".cursor" / "mcp.json"
        rule = d / ".cursor" / "rules" / "latch.mdc"
        config.parent.mkdir(parents=True)
        server = d / "mcp_server.py"
        server.write_text("# ok\n", encoding="utf-8")
        body, _ = ic.merge_mcp_config("", sys.executable, str(server))
        config.write_text(body, encoding="utf-8")
        agents = d / "AGENTS.md"
        agents_md_sync.sync(agents, create=True)
        cursor_rules_sync.sync(rule)
        commands = d / ".cursor" / "commands"
        ic.sync_cursor_commands(commands)
        agent = _fake_exe(
            d / "agent",
            "printf '%s\\n' 'latch_search latch_get latch_recent latch_gate'\n",
        )

        checks = cd.run_all(
            config_path=config,
            agents_path=agents,
            rules_path=rule,
            commands_dir=commands,
            python_path=sys.executable,
            server_py=str(server),
            agent_bin=str(agent),
            cli_timeout_s=1,
        )
        _assert([c.level for c in checks] == [cd.OK, cd.OK, cd.OK, cd.OK, cd.OK, cd.OK], checks)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS run_all_static_and_cli_checks")


def test_run_all_requires_hooks_when_requested():
    d = _tmp()
    try:
        config = d / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        server = d / "mcp_server.py"
        server.write_text("# ok\n", encoding="utf-8")
        body, _ = ic.merge_mcp_config("", sys.executable, str(server))
        config.write_text(body, encoding="utf-8")
        agents = d / "AGENTS.md"
        agents_md_sync.sync(agents, create=True)
        rule = d / ".cursor" / "rules" / "latch.mdc"
        cursor_rules_sync.sync(rule)
        commands = d / ".cursor" / "commands"
        ic.sync_cursor_commands(commands)
        hooks = d / ".cursor" / "hooks.json"
        hooks_body, _ = cursor_hooks.merge_hooks(
            "", sys.executable,
            str(ic.KB_HOME / "src" / "hooks" / "cursor_session_start.py"),
            str(ic.KB_HOME / "src" / "hooks" / "cursor_post_tool_use.py"),
            path=hooks,
        )
        cursor_hooks.write_hooks(hooks, hooks_body)
        checks = cd.run_all(
            config_path=config, agents_path=agents, rules_path=rule,
            commands_dir=commands, python_path=sys.executable,
            server_py=str(server), skip_cli=True, with_hooks=True,
            hooks_path=hooks,
        )
        hook_check = next(c for c in checks if "hooks.json" in c.name)
        _assert(hook_check.level == cd.OK, hook_check)
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_check_cursor_config_ok_and_missing()
    test_json_mode_reports_malformed_cursor_config()
    test_check_agents_md_status()
    test_check_cursor_rule_status()
    test_check_cursor_commands_status()
    test_check_mcp_launch_target()
    test_check_cursor_cli_mcp_warns_when_missing()
    test_check_cursor_cli_mcp_ok_and_failure()
    test_check_cursor_cli_mcp_fails_when_critical_tools_missing()
    test_run_all_static_and_cli_checks()
    test_run_all_requires_hooks_when_requested()
    print("\nAll cursor_doctor tests pass.")
