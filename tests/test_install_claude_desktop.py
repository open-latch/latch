"""Unit tests for the Claude Desktop local-MCP installer config merge."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import install_claude_desktop as icd  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_render_desktop_server_uses_claude_desktop_shape():
    server = icd.render_desktop_server("/PY", "/repo/src/mcp_server.py", model_backend="codex")
    _assert(server["command"] == "/PY", server)
    _assert(server["args"] == ["/repo/src/mcp_server.py"], server)
    _assert(server["env"]["LATCH_ADAPTER"] == "claude-desktop", server)
    _assert(server["env"]["LATCH_MODEL_BACKEND"] == "codex", server)
    _assert(server["env"]["LATCH_GATE_BACKEND"] == "codex", server)
    print("PASS render_desktop_server_uses_claude_desktop_shape")


def test_merge_desktop_config_preserves_unrelated_servers():
    existing = json.dumps({
        "globalShortcut": "Ctrl+Space",
        "mcpServers": {
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            }
        },
    }, indent=2) + "\n"
    new, changes = icd.merge_desktop_config(existing, "/py", "/srv.py", path=Path("config.json"))
    obj = json.loads(new)
    _assert(changes == ["added Claude Desktop MCP server latch"], changes)
    _assert("filesystem" in obj["mcpServers"], obj)
    _assert(obj["globalShortcut"] == "Ctrl+Space", obj)
    _assert(obj["mcpServers"]["latch"]["command"] == "/py", obj)
    print("PASS merge_desktop_config_preserves_unrelated_servers")


def test_merge_desktop_config_replaces_existing_claude_kb_only():
    existing = json.dumps({
        "mcpServers": {
            "latch": {"command": "/old", "args": ["/old.py"]},
            "other": {"command": "node", "args": ["server.js"]},
        }
    }, indent=2) + "\n"
    new, changes = icd.merge_desktop_config(
        existing,
        "/new/python",
        "/new/server.py",
        path=Path("config.json"),
    )
    obj = json.loads(new)
    _assert(changes == ["updated Claude Desktop MCP server latch"], changes)
    _assert(obj["mcpServers"]["latch"]["command"] == "/new/python", obj)
    _assert(obj["mcpServers"]["other"]["command"] == "node", obj)
    print("PASS merge_desktop_config_replaces_existing_claude_kb_only")


def test_merge_desktop_config_migrates_legacy_adapter_names():
    existing = json.dumps({
        "mcpServers": {
            "claude-kb": {"command": "/old", "args": ["/old.py"]},
            "claudeKb": {"command": "/old2", "args": ["/old2.py"]},
            "other": {"command": "node", "args": ["server.js"]},
        }
    }, indent=2) + "\n"
    new, changes = icd.merge_desktop_config(
        existing,
        "/new/python",
        "/new/server.py",
        path=Path("config.json"),
    )
    obj = json.loads(new)
    _assert("latch" in obj["mcpServers"], obj)
    _assert("claude-kb" not in obj["mcpServers"], obj)
    _assert("claudeKb" not in obj["mcpServers"], obj)
    _assert("removed legacy Claude Desktop MCP server claude-kb" in changes, changes)
    _assert("removed legacy Claude Desktop MCP server claudeKb" in changes, changes)
    _assert(obj["mcpServers"]["other"]["command"] == "node", obj)
    print("PASS merge_desktop_config_migrates_legacy_adapter_names")


def test_write_config_backs_up_existing():
    d = Path(tempfile.mkdtemp(prefix="latch-desktop-config-"))
    try:
        p = d / "claude_desktop_config.json"
        p.write_text('{"old": true}\n', encoding="utf-8")
        icd.write_config(p, '{"new": true}\n')
        backup = d / "claude_desktop_config.json.latchbak"
        _assert(backup.exists(), "backup should exist")
        _assert(backup.read_text(encoding="utf-8") == '{"old": true}\n',
                "backup should hold old content")
        _assert(p.read_text(encoding="utf-8") == '{"new": true}\n',
                "config should be updated")
        print("PASS write_config_backs_up_existing")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_render_desktop_server_uses_claude_desktop_shape()
    test_merge_desktop_config_preserves_unrelated_servers()
    test_merge_desktop_config_replaces_existing_claude_kb_only()
    test_merge_desktop_config_migrates_legacy_adapter_names()
    test_write_config_backs_up_existing()
    print("\nAll install_claude_desktop tests pass.")
