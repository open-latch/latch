"""Unit tests for the Cursor installer config merge."""
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


def test_cursor_mcp_launcher_uses_pythonw_only_when_available_on_windows():
    d = Path(tempfile.mkdtemp(prefix="latch-cursor-pythonw-"))
    try:
        python = d / "python.exe"
        pythonw = d / "pythonw.exe"
        python.write_bytes(b"")

        _assert(
            ic.cursor_mcp_launcher(str(python), system="Windows") == str(python),
            "python.exe must remain when pythonw.exe is unavailable",
        )
        pythonw.write_bytes(b"")
        _assert(
            ic.cursor_mcp_launcher(str(python), system="Windows") == str(pythonw),
            "Windows MCP launch should use the sibling pythonw.exe",
        )
        _assert(
            ic.cursor_mcp_launcher(str(python), system="Linux") == str(python),
            "non-Windows hosts must keep their configured interpreter",
        )
        _assert(
            ic.cursor_mcp_launcher(str(d / "custom.exe"), system="Windows")
            == str(d / "custom.exe"),
            "custom Windows launchers must remain unchanged",
        )
        original_system = ic.platform.system
        ic.platform.system = lambda: "Windows"
        try:
            standalone = d / "standalone" / "mcp_server.py"
            standalone.parent.mkdir()
            standalone.write_text("# standalone\n", encoding="utf-8")
            server = ic.render_cursor_server(str(python), str(standalone))
            _assert(
                server["args"] == [str(standalone).replace("\\", "/")],
                "a standalone server must not be redirected to a missing launcher",
            )

            server_py = d / "src" / "mcp_server.py"
            launcher_py = d / "src" / "mcp_launcher_win.py"
            server_py.parent.mkdir()
            server_py.write_text("# server\n", encoding="utf-8")
            launcher_py.write_text("# launcher\n", encoding="utf-8")
            server = ic.render_cursor_server(str(python), str(server_py))
        finally:
            ic.platform.system = original_system
        _assert(server["command"] == str(pythonw).replace("\\", "/"), server)
        _assert(
            server["env"]["LATCH_PYTHON"] == str(python).replace("\\", "/"),
            "windowless MCP config must retain the console interpreter",
        )
        _assert(
            server["args"] == [
                str(launcher_py).replace("\\", "/")
            ],
            server,
        )

        ok, detail = ic.cursor_mcp_launch_assets_status(
            str(python), str(server_py), system="Windows",
        )
        _assert(ok, detail)
        server_py.unlink()
        ok, detail = ic.cursor_mcp_launch_assets_status(
            str(python), str(server_py), system="Windows",
        )
        _assert(not ok and "MCP server" in detail, detail)
        server_py.write_text("# server\n", encoding="utf-8")
        python.unlink()
        ok, detail = ic.cursor_mcp_launch_assets_status(
            str(python), str(server_py), system="Windows",
        )
        _assert(not ok and "console interpreter" in detail, detail)

        path_python = d / "path-python.exe"
        path_python.write_text("", encoding="utf-8")
        path_python.chmod(0o755)
        old_path = os.environ.get("PATH")
        os.environ["PATH"] = str(d) + os.pathsep + (old_path or "")
        try:
            ok, detail = ic.cursor_mcp_launch_assets_status(
                path_python.name, str(server_py), system="Linux",
            )
            _assert(ok, detail)
        finally:
            if old_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = old_path
        print("PASS cursor_mcp_launcher_uses_pythonw_only_when_available_on_windows")
    finally:
        shutil.rmtree(d, ignore_errors=True)


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
                "--skills-dir", str(d / ".cursor" / "skills"),
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
        _assert((d / ".cursor" / "skills" / "source-command-latch-gate" / "SKILL.md").is_file(),
                "Cursor skills should be installed by default")
        print("PASS first_wire_notice_is_cursor_branded")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cursor_ide_enablement_guidance_is_explicit_and_proof_honest():
    text = ic.cursor_ide_enablement_guidance()
    _assert("Cursor Settings > Tools & MCP" in text, text)
    _assert("workspace" in text.lower() and "latch" in text, text)
    _assert("tools enabled" in text and "exact count can grow" in text, text)
    _assert("agent mcp" in text and "does not prove" in text, text)
    print("PASS cursor_ide_enablement_guidance_is_explicit_and_proof_honest")


def test_current_session_workflows_use_prompt_time_id_handoff():
    for path in (
        ic.CURSOR_COMMANDS_SRC / "latch-seed.md",
        ic.CURSOR_COMMANDS_SRC / "latch-compact.md",
        ic.CURSOR_SKILLS_SRC / "source-command-latch-seed" / "SKILL.md",
        ic.CURSOR_SKILLS_SRC / "source-command-latch-compact" / "SKILL.md",
    ):
        text = path.read_text(encoding="utf-8")
        _assert("current prompt context" in text, path)
        _assert("re-injected" in text and "beforeSubmitPrompt" in text, path)
        _assert("required_permissions" in text and '["all"]' in text, path)
        _assert("first Shell" in text and "one-shot" in text, path)
        _assert("LATCH_PYTHON" in text and ".cursor/mcp.json" in text, path)
        if "latch-seed" in str(path):
            _assert("preview_digest" in text and "second model call" in text, path)
            _assert("apply Shell call" in text and "first and only attempt" in text, path)
    print("PASS current_session_workflows_use_prompt_time_id_handoff")


def test_cursor_shell_workflows_pin_mcp_interpreter():
    for name in ic.CURSOR_COMMAND_FILES:
        text = ic.render_cursor_command(name)
        _assert("LATCH_PYTHON" in text and ".cursor/mcp.json" in text, name)
        _assert("mcpServers.latch.command" in text, name)
        _assert("mcpServers.latch.env.LATCH_PYTHON" in text, name)
        _assert("Never fall back" in text and "PATH `python3`" in text, name)
        _assert("do not export `LATCH_HOME`" in text, name)
        _assert('\npython "' not in text, f"bare PATH Python remains in {name}")
        _assert("$(pwd)" not in text, f"command substitution remains in {name}")
        if name in {
            "latch-budget-approve.md", "latch-decay.md",
            "latch-heal.md", "latch-tree.md",
        }:
            _assert('"<CURSOR_MCP_PYTHON>"' in text, name)
        if name == "latch-decay.md":
            _assert('maintenance.py" weekly "$PWD"' in text, text)

    for name in ic.CURSOR_SKILL_NAMES:
        if name == "source-command-latch-pm":
            continue  # MCP-only preview/apply; it does not invoke Shell.
        path = ic.CURSOR_SKILLS_SRC / name / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        _assert("LATCH_PYTHON" in text and ".cursor/mcp.json" in text, path)
        _assert("mcpServers.latch.command" in text, path)
        _assert("mcpServers.latch.env.LATCH_PYTHON" in text, path)
        _assert("Never fall back" in text and "PATH `python3`" in text, path)
        _assert("Do not export" in text and "`LATCH_HOME`" in text, path)
        if name in {
            "source-command-latch-budget-approve", "source-command-latch-decay",
            "source-command-latch-heal", "source-command-latch-tree",
        }:
            _assert("<CURSOR_MCP_PYTHON>" in text, path)
    print("PASS cursor_shell_workflows_pin_mcp_interpreter")


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
        ic.sync_cursor_skills(d / ".cursor" / "skills")

        rc = ic.main([
            "--python", sys.executable,
            "--mcp-json", str(config),
            "--agents-md", str(agents),
            "--rules-mdc", str(rule),
            "--commands-dir", str(d / ".cursor" / "commands"),
            "--skills-dir", str(d / ".cursor" / "skills"),
            "--check",
        ])
        _assert(rc == 0, f"expected check success, got {rc}")

        rc = ic.main([
            "--python", sys.executable,
            "--mcp-json", str(d / "missing.json"),
            "--agents-md", str(agents),
            "--rules-mdc", str(rule),
            "--commands-dir", str(d / ".cursor" / "commands"),
            "--skills-dir", str(d / ".cursor" / "skills"),
            "--check",
        ])
        _assert(rc == 1, f"expected check failure for missing config, got {rc}")

        rc = ic.main([
            "--python", sys.executable,
            "--mcp-json", str(config),
            "--agents-md", str(agents),
            "--rules-mdc", str(d / "missing-rule.mdc"),
            "--commands-dir", str(d / ".cursor" / "commands"),
            "--skills-dir", str(d / ".cursor" / "skills"),
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
        _assert((commands / "latch-seed.md").exists(),
                "Cursor-origin seed command should be installed")
        gate = commands / "latch-gate.md"
        body = gate.read_text(encoding="utf-8")
        _assert("<KB_HOME>" not in body, "Cursor commands should resolve KB_HOME")
        _assert("Cursor boundary" in body, "Cursor commands should state adapter boundary")
        _assert("LATCH_GATE_BACKEND=cursor" in body,
                "shell fallback should inherit the native Cursor backend")
        compact = (commands / "latch-compact.md").read_text(encoding="utf-8")
        _assert("run_cursor_compact_now" in compact and "fail-closed" in compact,
                compact)
        _assert("Latch operation id: latch-compact run" in compact, compact)
        _assert("LATCH_COMPACTOR_BACKEND=cursor" in compact, compact)
        seed_command = (commands / "latch-seed.md").read_text(encoding="utf-8")
        _assert("--source cursor" in seed_command and "--format json" in seed_command
                and "Never add `--yes`" in seed_command,
                seed_command)
        pm_command = (commands / "latch-pm.md").read_text(encoding="utf-8")
        _assert("Latch operation id: latch-pm prepare" in pm_command, pm_command)
        _assert("latch_pm_preview" in pm_command and "/latch-pm apply" in pm_command,
                pm_command)
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


def test_cursor_skills_sync_status_remove_and_plugin_manifest():
    d = Path(tempfile.mkdtemp(prefix="latch-cursor-skills-"))
    try:
        skills = d / ".cursor" / "skills"
        changes = ic.sync_cursor_skills(skills, model_backend="codex")
        _assert(len(changes) == len(ic.CURSOR_SKILL_NAMES), changes)
        gate = skills / "source-command-latch-gate" / "SKILL.md"
        seed_skill = skills / "source-command-latch-seed" / "SKILL.md"
        _assert(gate.is_file() and seed_skill.is_file(), changes)
        gate_body = gate.read_text(encoding="utf-8")
        _assert("<KB_HOME>" not in gate_body and "<CURSOR_MODEL_BACKEND>" not in gate_body,
                gate_body)
        _assert("codex" in gate_body and "Latch Cursor skill boundary:" in gate_body,
                gate_body)
        seed_body = seed_skill.read_text(encoding="utf-8")
        _assert("Latch operation id: latch-seed preview" in seed_body, seed_body)
        _assert("--cursor-session-id" in seed_body and "--format json" in seed_body
                and "/latch-seed apply" in seed_body,
                seed_body)
        compact_body = (skills / "source-command-latch-compact" / "SKILL.md").read_text(
            encoding="utf-8",
        )
        _assert("Latch operation id: latch-compact run" in compact_body, compact_body)
        pm_body = (skills / "source-command-latch-pm" / "SKILL.md").read_text(
            encoding="utf-8",
        )
        _assert("latch_pm_preview" in pm_body and "/latch-pm apply" in pm_body, pm_body)
        _assert("Do not substitute agent prose" in pm_body, pm_body)
        ok, detail = ic.cursor_skills_status(skills, model_backend="codex")
        _assert(ok, detail)
        ok, detail = ic.cursor_skills_status(skills, model_backend="cursor")
        _assert(not ok and "drifted" in detail, detail)
        plugin_ok, plugin_detail = ic.cursor_plugin_status()
        _assert(plugin_ok, plugin_detail)

        gate.write_text(gate_body + "\nmanaged drift\n", encoding="utf-8")
        changes = ic.sync_cursor_skills(skills, model_backend="codex")
        _assert(any("updated Cursor skill source-command-latch-gate" in row for row in changes),
                changes)
        _assert(gate.read_text(encoding="utf-8") == gate_body,
                "latch-owned skill drift should be repaired")
        _assert(gate.with_name("SKILL.md.latchbak").read_text(encoding="utf-8").endswith(
            "managed drift\n"
        ), "latch-owned skill drift should retain a backup")
        _assert(ic.sync_cursor_skills(skills, model_backend="codex") == [],
                "repeated skill install should be idempotent")

        custom = skills / "custom-skill" / "SKILL.md"
        custom.parent.mkdir(parents=True)
        custom.write_text("---\nname: custom-skill\ndescription: user owned\n---\n", encoding="utf-8")
        removed = ic.remove_cursor_skills(skills)
        _assert(any("removed Cursor skill source-command-latch-gate" in row for row in removed),
                removed)
        _assert(custom.is_file(), "uninstall should preserve unrelated Cursor skills")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cursor_skills_refuse_user_owned_same_name_collision():
    d = Path(tempfile.mkdtemp(prefix="latch-cursor-skill-collision-"))
    try:
        skills = d / ".cursor" / "skills"
        gate = skills / "source-command-latch-gate" / "SKILL.md"
        gate.parent.mkdir(parents=True)
        custom = b"---\nname: source-command-latch-gate\ndescription: user owned\n---\n"
        gate.write_bytes(custom)

        try:
            ic.sync_cursor_skills(skills)
        except ic.CursorAssetCollisionError as exc:
            _assert("refusing to overwrite user-owned Cursor skill" in str(exc), exc)
        else:
            raise AssertionError("same-name user skill must fail closed")

        _assert(gate.read_bytes() == custom, "collision must preserve the user skill byte-for-byte")
        _assert(not gate.with_name("SKILL.md.latchbak").exists(),
                "fail-closed collision must not create a misleading backup")
        _assert(not (skills / "source-command-latch-seed").exists(),
                "collision preflight must happen before any skill writes")

        rc = ic.main([
            "--skip-mcp", "--skip-agents", "--skip-rules", "--skip-commands",
            "--skills-dir", str(skills),
        ])
        _assert(rc == 2, rc)
        removed = ic.remove_cursor_skills(skills)
        _assert(gate.read_bytes() == custom, "uninstall must preserve a collision-blocked user skill")
        _assert(any("looks user-owned" in row for row in removed), removed)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_asset_collision_preflight_prevents_any_partial_install():
    collision_cases = (
        ("command-file", "command", "file"),
        ("command-directory", "command", "directory"),
        ("command-dangling-symlink", "command", "dangling-symlink"),
        ("skill-file", "skill", "file"),
        ("skill-directory", "skill", "directory"),
        ("skill-dangling-symlink", "skill", "dangling-symlink"),
    )
    for case_name, collision_kind, path_kind in collision_cases:
        d = Path(tempfile.mkdtemp(prefix=f"latch-cursor-{collision_kind}-transaction-"))
        try:
            cursor = d / ".cursor"
            commands = cursor / "commands"
            skills = cursor / "skills"
            if collision_kind == "command":
                collision = commands / "latch-gate.md"
                payload = b"user command bytes\x00\xff"
            else:
                collision = skills / "source-command-latch-gate" / "SKILL.md"
                payload = b"---\nname: source-command-latch-gate\ndescription: user skill\n---\n"
            collision.parent.mkdir(parents=True)
            outside_target = d / "outside" / f"{case_name}.md"
            if path_kind == "file":
                collision.write_bytes(payload)
            elif path_kind == "directory":
                collision.mkdir()
            else:
                collision.symlink_to(outside_target)

            rc = ic.main([
                "--mcp-json", str(cursor / "mcp.json"),
                "--agents-md", str(d / "AGENTS.md"),
                "--rules-mdc", str(cursor / "rules" / "latch.mdc"),
                "--commands-dir", str(commands),
                "--skills-dir", str(skills),
                "--hooks-json", str(cursor / "hooks.json"),
                "--with-hooks", "--yes",
            ])
            _assert(rc == 2, (case_name, rc))
            if path_kind == "file":
                _assert(collision.read_bytes() == payload, case_name)
            elif path_kind == "directory":
                _assert(collision.is_dir(), case_name)
            else:
                _assert(collision.is_symlink(), case_name)
                _assert(not outside_target.exists(), case_name)
            _assert(not (cursor / "mcp.json").exists(), case_name)
            _assert(not (d / "AGENTS.md").exists(), case_name)
            _assert(not (cursor / "rules" / "latch.mdc").exists(), case_name)
            _assert(not (cursor / "hooks.json").exists(), case_name)
            if collision_kind == "skill":
                _assert(not commands.exists(), "command assets must not precede skill preflight")
            else:
                _assert(not skills.exists(), "skill assets must not follow command collision")
        finally:
            shutil.rmtree(d, ignore_errors=True)


def test_asset_directory_symlink_preflight_prevents_any_partial_install():
    for asset_kind in ("commands", "skills"):
        for target_kind in ("existing", "dangling"):
            d = Path(tempfile.mkdtemp(
                prefix=f"latch-cursor-{asset_kind}-{target_kind}-parent-symlink-",
            ))
            outside = Path(tempfile.mkdtemp(prefix="latch-cursor-outside-assets-"))
            try:
                cursor = d / ".cursor"
                cursor.mkdir(parents=True)
                commands = cursor / "commands"
                skills = cursor / "skills"
                asset_dir = commands if asset_kind == "commands" else skills
                symlink_target = outside if target_kind == "existing" else outside / "missing"
                asset_dir.symlink_to(symlink_target, target_is_directory=True)

                rc = ic.main([
                    "--mcp-json", str(cursor / "mcp.json"),
                    "--agents-md", str(d / "AGENTS.md"),
                    "--rules-mdc", str(cursor / "rules" / "latch.mdc"),
                    "--commands-dir", str(commands),
                    "--skills-dir", str(skills),
                    "--hooks-json", str(cursor / "hooks.json"),
                    "--with-hooks", "--yes",
                ])
                _assert(rc == 2, (asset_kind, target_kind, rc))
                _assert(asset_dir.is_symlink(), (asset_kind, target_kind))
                _assert(not (outside / "latch-gate.md").exists(), asset_kind)
                _assert(not (outside / "source-command-latch-gate").exists(), asset_kind)
                _assert(not (cursor / "mcp.json").exists(), asset_kind)
                _assert(not (d / "AGENTS.md").exists(), asset_kind)
                _assert(not (cursor / "rules" / "latch.mdc").exists(), asset_kind)
                _assert(not (cursor / "hooks.json").exists(), asset_kind)
            finally:
                shutil.rmtree(d, ignore_errors=True)
                shutil.rmtree(outside, ignore_errors=True)


def test_with_hooks_installs_and_check_requires_hooks():
    d = Path(tempfile.mkdtemp(prefix="latch-cursor-hooks-install-"))
    try:
        hooks = d / ".cursor" / "hooks.json"
        rc = ic.main([
            "--skip-mcp", "--skip-agents", "--skip-rules", "--skip-commands", "--skip-skills",
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
            "--skip-mcp", "--skip-agents", "--skip-rules", "--skip-commands", "--skip-skills",
            "--hooks-json", str(hooks), "--with-hooks", "--check",
        ])
        _assert(rc == 0, rc)
        hooks.unlink()
        rc = ic.main([
            "--skip-mcp", "--skip-agents", "--skip-rules", "--skip-commands", "--skip-skills",
            "--hooks-json", str(hooks), "--with-hooks", "--check",
        ])
        _assert(rc == 1, rc)
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_render_cursor_server_uses_cursor_mcp_shape()
    test_cursor_mcp_launcher_uses_pythonw_only_when_available_on_windows()
    test_merge_mcp_config_preserves_unrelated_servers_and_settings()
    test_merge_mcp_config_replaces_existing_latch_only()
    test_merge_mcp_config_migrates_legacy_adapter_names()
    test_merge_mcp_config_idempotent()
    test_merge_mcp_config_rejects_present_non_object_mcpservers()
    test_installer_preserves_non_object_mcpservers_byte_for_byte()
    test_write_config_backs_up_existing()
    test_agents_sync_args_are_cursor_branded()
    test_first_wire_notice_is_cursor_branded()
    test_cursor_ide_enablement_guidance_is_explicit_and_proof_honest()
    test_current_session_workflows_use_prompt_time_id_handoff()
    test_check_mode_verifies_mcp_and_agents()
    test_cursor_commands_sync_status_and_remove()
    test_cursor_commands_refuse_user_owned_same_name_collision()
    test_cursor_commands_render_selected_compatibility_backend()
    test_cursor_skills_sync_status_remove_and_plugin_manifest()
    test_cursor_skills_refuse_user_owned_same_name_collision()
    test_with_hooks_installs_and_check_requires_hooks()
    print("\nAll install_cursor tests pass.")
