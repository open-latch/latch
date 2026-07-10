"""Unit tests for the Cursor installer config merge."""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import agents_md_sync  # noqa: E402
import cursor_rules_sync  # noqa: E402
import cursor_hooks  # noqa: E402
import install_engine  # noqa: E402
import install_cursor as ic  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_render_cursor_server_uses_cursor_mcp_shape():
    server = ic.render_cursor_server("/PY", "/repo/src/mcp_server.py", model_backend="codex")
    _assert(server["type"] == "stdio", server)
    _assert(server["command"] == "/PY", server)
    _assert(server["args"] == ["/repo/src/mcp_server.py"], server)
    _assert(server["env"]["LATCH_ADAPTER"] == "cursor", server)
    _assert(server["env"]["LATCH_MODEL_BACKEND"] == "codex", server)
    _assert(server["env"]["LATCH_GATE_BACKEND"] == "codex", server)
    _assert(server["env"]["LATCH_MAINTENANCE_BACKEND"] == "codex", server)
    _assert(server["env"]["LATCH_COMPACTOR_BACKEND"] == "codex", server)

    default = ic.render_cursor_server("/PY", "/repo/src/mcp_server.py")
    _assert(default["type"] == "stdio", default)
    _assert(default["env"]["LATCH_ADAPTER"] == "cursor", default)
    for key in (
        "LATCH_MODEL_BACKEND", "LATCH_GATE_BACKEND",
        "LATCH_MAINTENANCE_BACKEND", "LATCH_COMPACTOR_BACKEND",
    ):
        _assert(default["env"][key] == "cursor", default)
    print("PASS render_cursor_server_uses_cursor_mcp_shape")


def test_merge_mcp_config_preserves_unrelated_servers_and_settings():
    existing = json.dumps({
        "inputs": [{"id": "token", "type": "promptString"}],
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": ["@playwright/mcp"],
            }
        },
        "someCursorSetting": True,
    }, indent=2) + "\n"

    new, changes = ic.merge_mcp_config(existing, "/py", "/srv.py")
    obj = json.loads(new)
    _assert(changes == ["added Cursor MCP server latch"], changes)
    _assert("playwright" in obj["mcpServers"], obj)
    _assert(obj["inputs"][0]["id"] == "token", obj)
    _assert(obj["someCursorSetting"] is True, obj)
    _assert(obj["mcpServers"]["latch"]["command"] == "/py", obj)
    print("PASS merge_mcp_config_preserves_unrelated_servers_and_settings")


def test_merge_mcp_config_replaces_existing_latch_only():
    existing = json.dumps({
        "mcpServers": {
            "latch": {"command": "/old", "args": ["/old.py"]},
            "other": {"command": "node", "args": ["server.js"]},
        }
    }, indent=2) + "\n"
    new, changes = ic.merge_mcp_config(existing, "/new/python", "/new/server.py")
    obj = json.loads(new)
    _assert(changes == ["updated Cursor MCP server latch"], changes)
    _assert(obj["mcpServers"]["latch"]["command"] == "/new/python", obj)
    _assert(obj["mcpServers"]["other"]["command"] == "node", obj)
    print("PASS merge_mcp_config_replaces_existing_latch_only")


def test_merge_mcp_config_migrates_legacy_adapter_names():
    existing = json.dumps({
        "mcpServers": {
            "claude-kb": {"command": "/old", "args": ["/old.py"]},
            "claudeKb": {"command": "/old2", "args": ["/old2.py"]},
            "other": {"command": "node", "args": ["server.js"]},
        }
    }, indent=2) + "\n"
    new, changes = ic.merge_mcp_config(existing, "/new/python", "/new/server.py")
    obj = json.loads(new)
    _assert("latch" in obj["mcpServers"], obj)
    _assert("claude-kb" not in obj["mcpServers"], obj)
    _assert("claudeKb" not in obj["mcpServers"], obj)
    _assert("removed legacy Cursor MCP server claude-kb" in changes, changes)
    _assert("removed legacy Cursor MCP server claudeKb" in changes, changes)
    _assert(obj["mcpServers"]["other"]["command"] == "node", obj)
    print("PASS merge_mcp_config_migrates_legacy_adapter_names")


def test_merge_mcp_config_idempotent():
    new1, changes1 = ic.merge_mcp_config("", "/PY", "/srv.py")
    _assert(changes1, "first merge should change")
    new2, changes2 = ic.merge_mcp_config(new1, "/PY", "/srv.py")
    _assert(new2 == new1, "second merge should be byte-identical")
    _assert(changes2 == [], f"second merge should report no changes, got {changes2}")
    print("PASS merge_mcp_config_idempotent")


def test_merge_mcp_config_rejects_present_non_object_mcpservers():
    for value in (None, [], ["user-server"], "user-server", 7, True):
        existing = json.dumps({"mcpServers": value, "setting": True}) + "\n"
        try:
            ic.merge_mcp_config(existing, "/PY", "/srv.py")
        except ic.CursorConfigError as exc:
            _assert("expected a JSON object" in str(exc), exc)
            _assert("did not modify the file" in str(exc), exc)
        else:
            raise AssertionError(f"non-object mcpServers must fail closed: {value!r}")
    print("PASS merge_mcp_config_rejects_present_non_object_mcpservers")


def test_installer_preserves_non_object_mcpservers_byte_for_byte():
    d = Path(tempfile.mkdtemp(prefix="latch-cursor-mcp-type-"))
    try:
        config = d / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b'{"mcpServers":[{"command":"user-owned"}],"setting":true}\n'
        config.write_bytes(original)
        common = [
            "--mcp-json", str(config),
            "--skip-agents", "--skip-rules", "--skip-commands", "--yes",
        ]

        _assert(ic.main(common) == 2, "apply must fail closed")
        _assert(config.read_bytes() == original, "apply must preserve active bytes")
        _assert(not config.with_suffix(".json.latchbak").exists(),
                "a failed preflight must not create a misleading backup")

        _assert(ic.main([*common, "--dry-run"]) == 2, "dry-run must report unsafe config")
        _assert(config.read_bytes() == original, "dry-run must preserve active bytes")

        _assert(ic.main([*common, "--check"]) == 1, "check must report unsafe config")
        _assert(config.read_bytes() == original, "check must preserve active bytes")
        print("PASS installer_preserves_non_object_mcpservers_byte_for_byte")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_write_config_backs_up_existing():
    d = Path(tempfile.mkdtemp(prefix="latch-cursor-config-"))
    try:
        p = d / "mcp.json"
        p.write_text('{"old": true}\n', encoding="utf-8")
        ic.write_config(p, '{"new": true}\n')
        backup = d / "mcp.json.latchbak"
        _assert(backup.exists(), "backup should exist")
        _assert(backup.read_text(encoding="utf-8") == '{"old": true}\n',
                "backup should hold old content")
        _assert(p.read_text(encoding="utf-8") == '{"new": true}\n',
                "config should be updated")
        print("PASS write_config_backs_up_existing")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_agents_sync_args_are_cursor_branded():
    yes_args = ic._agents_sync_args("AGENTS.md", yes=True)
    _assert(yes_args == [
        "--yes",
        "--surface-name", "Cursor",
        "--wording-label", "shared AGENTS.md",
        "AGENTS.md",
    ], yes_args)

    prompt_args = ic._agents_sync_args("AGENTS.md", yes=False)
    _assert(prompt_args == [
        "--surface-name", "Cursor",
        "--wording-label", "shared AGENTS.md",
        "AGENTS.md",
    ], prompt_args)
    print("PASS agents_sync_args_are_cursor_branded")


def test_first_wire_notice_is_cursor_branded():
    d = Path(tempfile.mkdtemp(prefix="latch-cursor-agents-"))
    try:
        rule = d / ".cursor" / "rules" / "latch.mdc"
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ic.main([
                "--skip-mcp",
                "--agents-md", str(d / "AGENTS.md"),
                "--rules-mdc", str(rule),
                "--commands-dir", str(d / ".cursor" / "commands"),
                "--yes",
            ])
        text = out.getvalue()
        _assert(rc == 0, f"expected install success, got {rc}")
        _assert("into Cursor for this project" in text, text)
        _assert("shared AGENTS.md wording" in text, text)
        _assert("into Codex for this project" not in text, text)
        _assert(cursor_rules_sync.evaluate(rule) == cursor_rules_sync.OK,
                "Cursor rule should be installed by default")
        _assert((d / ".cursor" / "commands" / "latch-gate.md").is_file(),
                "Cursor command prompts should be installed by default")
        print("PASS first_wire_notice_is_cursor_branded")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_check_mode_verifies_mcp_and_agents():
    d = Path(tempfile.mkdtemp(prefix="latch-cursor-check-"))
    try:
        config = d / ".cursor" / "mcp.json"
        rule = d / ".cursor" / "rules" / "latch.mdc"
        agents = d / "AGENTS.md"
        python_path = install_engine.resolve_python(sys.executable)
        server_py = str((ic.KB_HOME / "src" / "mcp_server.py")).replace("\\", "/")
        body, _ = ic.merge_mcp_config("", python_path, server_py)
        config.parent.mkdir(parents=True)
        config.write_text(body, encoding="utf-8")
        agents_md_sync.sync(agents, create=True)
        cursor_rules_sync.sync(rule)
        ic.sync_cursor_commands(d / ".cursor" / "commands")

        rc = ic.main([
            "--python", sys.executable,
            "--mcp-json", str(config),
            "--agents-md", str(agents),
            "--rules-mdc", str(rule),
            "--commands-dir", str(d / ".cursor" / "commands"),
            "--check",
        ])
        _assert(rc == 0, f"expected check success, got {rc}")

        rc = ic.main([
            "--python", sys.executable,
            "--mcp-json", str(d / "missing.json"),
            "--agents-md", str(agents),
            "--rules-mdc", str(rule),
            "--commands-dir", str(d / ".cursor" / "commands"),
            "--check",
        ])
        _assert(rc == 1, f"expected check failure for missing config, got {rc}")

        rc = ic.main([
            "--python", sys.executable,
            "--mcp-json", str(config),
            "--agents-md", str(agents),
            "--rules-mdc", str(d / "missing-rule.mdc"),
            "--commands-dir", str(d / ".cursor" / "commands"),
            "--check",
        ])
        _assert(rc == 1, f"expected check failure for missing rule, got {rc}")
        print("PASS check_mode_verifies_mcp_and_agents")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cursor_commands_sync_status_and_remove():
    d = Path(tempfile.mkdtemp(prefix="latch-cursor-commands-"))
    try:
        commands = d / ".cursor" / "commands"
        changes = ic.sync_cursor_commands(commands)
        _assert(any("Cursor command latch-gate.md" in c for c in changes), changes)
        _assert((commands / "latch-compact.md").exists(),
                "Cursor-native compaction command should be installed")
        gate = commands / "latch-gate.md"
        body = gate.read_text(encoding="utf-8")
        _assert("<KB_HOME>" not in body, "Cursor commands should resolve KB_HOME")
        _assert("Cursor boundary" in body, "Cursor commands should state adapter boundary")
        _assert("LATCH_GATE_BACKEND=cursor" in body,
                "shell fallback should inherit the native Cursor backend")
        compact = (commands / "latch-compact.md").read_text(encoding="utf-8")
        _assert("run_cursor_compact_now" in compact and "fail-closed" in compact,
                compact)
        _assert("LATCH_COMPACTOR_BACKEND=cursor" in compact, compact)
        ok, detail = ic.cursor_commands_status(commands)
        _assert(ok, detail)

        gate.write_text(body + "\nmanaged drift\n", encoding="utf-8")
        changes = ic.sync_cursor_commands(commands)
        _assert(any("updated Cursor command latch-gate.md" in c for c in changes), changes)
        _assert(gate.read_text(encoding="utf-8") == body,
                "latch-owned command drift should be repaired")
        _assert(gate.with_name("latch-gate.md.latchbak").read_text(encoding="utf-8").endswith(
            "managed drift\n"
        ), "latch-owned drift should retain a backup")
        _assert(ic.sync_cursor_commands(commands) == [],
                "repeated command install should be idempotent")

        custom = commands / "custom.md"
        custom.write_text("user command\n", encoding="utf-8")
        removed = ic.remove_cursor_commands(commands)
        _assert(any("removed Cursor command latch-gate.md" in c for c in removed), removed)
        _assert(custom.exists(), "uninstall should preserve unrelated Cursor command files")
        ok, detail = ic.cursor_commands_status(commands)
        _assert(not ok and "missing" in detail, detail)
        print("PASS cursor_commands_sync_status_and_remove")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cursor_commands_refuse_user_owned_same_name_collision():
    d = Path(tempfile.mkdtemp(prefix="latch-cursor-command-collision-"))
    try:
        commands = d / ".cursor" / "commands"
        commands.mkdir(parents=True)
        gate = commands / "latch-gate.md"
        custom = b"user-owned gate command\n"
        gate.write_bytes(custom)

        try:
            ic.sync_cursor_commands(commands)
        except ic.CursorAssetCollisionError as exc:
            _assert("refusing to overwrite user-owned Cursor command" in str(exc), exc)
        else:
            raise AssertionError("same-name user command must fail closed")

        _assert(gate.read_bytes() == custom, "collision must preserve the user file byte-for-byte")
        _assert(not gate.with_name("latch-gate.md.latchbak").exists(),
                "fail-closed collision must not create a misleading backup")
        _assert(not (commands / "latch-pm.md").exists(),
                "collision preflight must happen before any command writes")

        rc = ic.main([
            "--skip-mcp", "--skip-agents", "--skip-rules",
            "--commands-dir", str(commands),
        ])
        _assert(rc == 2, rc)
        removed = ic.remove_cursor_commands(commands)
        _assert(gate.read_bytes() == custom, "uninstall must preserve a collision-blocked user file")
        _assert(any("looks user-owned" in row for row in removed), removed)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cursor_commands_render_selected_compatibility_backend():
    d = Path(tempfile.mkdtemp(prefix="latch-cursor-commands-backend-"))
    try:
        commands = d / ".cursor" / "commands"
        ic.sync_cursor_commands(commands, model_backend="codex")
        gate = (commands / "latch-gate.md").read_text(encoding="utf-8")
        _assert("LATCH_GATE_BACKEND=codex" in gate, gate)
        _assert("Cursor shell-fallback backend: `codex`" in gate, gate)
        compact = (commands / "latch-compact.md").read_text(encoding="utf-8")
        _assert("LATCH_COMPACTOR_BACKEND=codex" in compact, compact)
        _assert('$env:LATCH_COMPACTOR_BACKEND = "codex"' in compact, compact)
        ok, detail = ic.cursor_commands_status(commands, model_backend="codex")
        _assert(ok, detail)
        ok, detail = ic.cursor_commands_status(commands, model_backend="cursor")
        _assert(not ok and "drifted" in detail, detail)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_with_hooks_installs_and_check_requires_hooks():
    d = Path(tempfile.mkdtemp(prefix="latch-cursor-hooks-install-"))
    try:
        hooks = d / ".cursor" / "hooks.json"
        rc = ic.main([
            "--skip-mcp", "--skip-agents", "--skip-rules", "--skip-commands",
            "--hooks-json", str(hooks), "--with-hooks", "--yes",
        ])
        _assert(rc == 0, rc)
        ok, detail = cursor_hooks.hooks_status(
            hooks,
            install_engine.resolve_python(None),
            str(ic.KB_HOME / "src" / "hooks" / "cursor_session_start.py"),
            str(ic.KB_HOME / "src" / "hooks" / "cursor_before_submit.py"),
            str(ic.KB_HOME / "src" / "hooks" / "cursor_pre_tool_use.py"),
            str(ic.KB_HOME / "src" / "hooks" / "cursor_post_tool_use.py"),
        )
        _assert(ok, detail)
        rc = ic.main([
            "--skip-mcp", "--skip-agents", "--skip-rules", "--skip-commands",
            "--hooks-json", str(hooks), "--with-hooks", "--check",
        ])
        _assert(rc == 0, rc)
        hooks.unlink()
        rc = ic.main([
            "--skip-mcp", "--skip-agents", "--skip-rules", "--skip-commands",
            "--hooks-json", str(hooks), "--with-hooks", "--check",
        ])
        _assert(rc == 1, rc)
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_render_cursor_server_uses_cursor_mcp_shape()
    test_merge_mcp_config_preserves_unrelated_servers_and_settings()
    test_merge_mcp_config_replaces_existing_latch_only()
    test_merge_mcp_config_migrates_legacy_adapter_names()
    test_merge_mcp_config_idempotent()
    test_merge_mcp_config_rejects_present_non_object_mcpservers()
    test_installer_preserves_non_object_mcpservers_byte_for_byte()
    test_write_config_backs_up_existing()
    test_agents_sync_args_are_cursor_branded()
    test_first_wire_notice_is_cursor_branded()
    test_check_mode_verifies_mcp_and_agents()
    test_cursor_commands_sync_status_and_remove()
    test_cursor_commands_refuse_user_owned_same_name_collision()
    test_cursor_commands_render_selected_compatibility_backend()
    test_with_hooks_installs_and_check_requires_hooks()
    print("\nAll install_cursor tests pass.")
