"""Unit tests for the Cursor installer config merge."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import agents_md_sync  # noqa: E402
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

    default = ic.render_cursor_server("/PY", "/repo/src/mcp_server.py")
    _assert(default["type"] == "stdio", default)
    _assert(default["env"] == {"LATCH_ADAPTER": "cursor"}, default)
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


def test_check_mode_verifies_mcp_and_agents():
    d = Path(tempfile.mkdtemp(prefix="latch-cursor-check-"))
    try:
        config = d / ".cursor" / "mcp.json"
        agents = d / "AGENTS.md"
        python_path = install_engine.resolve_python(sys.executable)
        server_py = str((ic.KB_HOME / "src" / "mcp_server.py")).replace("\\", "/")
        body, _ = ic.merge_mcp_config("", python_path, server_py)
        config.parent.mkdir(parents=True)
        config.write_text(body, encoding="utf-8")
        agents_md_sync.sync(agents, create=True)

        rc = ic.main([
            "--python", sys.executable,
            "--mcp-json", str(config),
            "--agents-md", str(agents),
            "--check",
        ])
        _assert(rc == 0, f"expected check success, got {rc}")

        rc = ic.main([
            "--python", sys.executable,
            "--mcp-json", str(d / "missing.json"),
            "--agents-md", str(agents),
            "--check",
        ])
        _assert(rc == 1, f"expected check failure for missing config, got {rc}")
        print("PASS check_mode_verifies_mcp_and_agents")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_render_cursor_server_uses_cursor_mcp_shape()
    test_merge_mcp_config_preserves_unrelated_servers_and_settings()
    test_merge_mcp_config_replaces_existing_latch_only()
    test_merge_mcp_config_migrates_legacy_adapter_names()
    test_merge_mcp_config_idempotent()
    test_write_config_backs_up_existing()
    test_check_mode_verifies_mcp_and_agents()
    print("\nAll install_cursor tests pass.")
