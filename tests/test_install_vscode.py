"""Unit tests for the VS Code/Copilot installer config merge."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import install_vscode as iv  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_render_mcp_server_uses_vscode_stdio_shape():
    server = iv.render_mcp_server("/PY", "/repo/src/mcp_server.py", model_backend="codex")
    _assert(server["type"] == "stdio", server)
    _assert(server["command"] == "/PY", server)
    _assert(server["args"] == ["/repo/src/mcp_server.py"], server)
    _assert(server["env"]["LATCH_ADAPTER"] == "vscode-copilot", server)
    _assert(server["env"]["LATCH_MODEL_BACKEND"] == "codex", server)
    _assert(server["env"]["LATCH_GATE_BACKEND"] == "codex", server)
    print("PASS render_mcp_server_uses_vscode_stdio_shape")


def test_merge_mcp_config_preserves_unrelated_servers_and_settings():
    existing = json.dumps({
        "inputs": [{"id": "token", "type": "promptString"}],
        "servers": {
            "playwright": {
                "type": "stdio",
                "command": "npx",
                "args": ["@playwright/mcp"],
            }
        },
        "sandbox": {"allowRead": ["${workspaceFolder}"]},
    }, indent=2) + "\n"

    new, changes = iv.merge_mcp_config(existing, "/py", "/srv.py")
    obj = json.loads(new)
    _assert(changes == ["added VS Code MCP server latch"], changes)
    _assert("playwright" in obj["servers"], obj)
    _assert(obj["inputs"][0]["id"] == "token", obj)
    _assert(obj["sandbox"]["allowRead"] == ["${workspaceFolder}"], obj)
    _assert(obj["servers"]["latch"]["command"] == "/py", obj)
    print("PASS merge_mcp_config_preserves_unrelated_servers_and_settings")


def test_merge_mcp_config_replaces_existing_claude_kb_only():
    existing = json.dumps({
        "servers": {
            "latch": {"type": "stdio", "command": "/old", "args": ["/old.py"]},
            "other": {"type": "http", "url": "https://example.invalid/mcp"},
        }
    }, indent=2) + "\n"
    new, changes = iv.merge_mcp_config(existing, "/new/python", "/new/server.py")
    obj = json.loads(new)
    _assert(changes == ["updated VS Code MCP server latch"], changes)
    _assert(obj["servers"]["latch"]["command"] == "/new/python", obj)
    _assert(obj["servers"]["other"]["url"] == "https://example.invalid/mcp", obj)
    print("PASS merge_mcp_config_replaces_existing_claude_kb_only")


def test_merge_mcp_config_migrates_legacy_hyphenated_server_name():
    existing = json.dumps({
        "servers": {
            "claude-kb": {"type": "stdio", "command": "/old", "args": ["/old.py"]},
            "claudeKb": {"type": "stdio", "command": "/old2", "args": ["/old2.py"]},
            "other": {"type": "http", "url": "https://example.invalid/mcp"},
        }
    }, indent=2) + "\n"
    new, changes = iv.merge_mcp_config(existing, "/new/python", "/new/server.py")
    obj = json.loads(new)
    _assert("latch" in obj["servers"], obj)
    _assert("claude-kb" not in obj["servers"], obj)
    _assert("claudeKb" not in obj["servers"], obj)
    _assert("removed legacy VS Code MCP server claude-kb" in changes, changes)
    _assert("removed legacy VS Code MCP server claudeKb" in changes, changes)
    _assert(obj["servers"]["other"]["url"] == "https://example.invalid/mcp", obj)
    print("PASS merge_mcp_config_migrates_legacy_hyphenated_server_name")


def test_render_hooks_config_installs_only_safe_preview_hooks():
    hooks = iv.render_hooks_config("/PY")
    events = set(hooks["hooks"])
    _assert(events == {"SessionStart", "UserPromptSubmit", "PostToolUse"}, events)
    rendered = json.dumps(hooks)
    _assert("vscode_session_start.py" in rendered, rendered)
    _assert("user_prompt_submit.py" in rendered, rendered)
    _assert("post_tool_use.py" in rendered, rendered)
    _assert("stop.py" not in rendered, rendered)
    _assert("session_end.py" not in rendered, rendered)
    print("PASS render_hooks_config_installs_only_safe_preview_hooks")


def test_merge_hooks_config_replaces_existing_file():
    existing = json.dumps({"hooks": {"Stop": [{"type": "command", "command": "echo old"}]}}) + "\n"
    new, changes = iv.merge_hooks_config(existing, "/PY")
    obj = json.loads(new)
    _assert(changes == ["installed VS Code preview latch hooks"], changes)
    _assert("Stop" not in obj["hooks"], obj)
    _assert("SessionStart" in obj["hooks"], obj)
    print("PASS merge_hooks_config_replaces_existing_file")


def test_merge_vscode_settings_isolates_claude_hook_discovery():
    existing = json.dumps({
        "editor.formatOnSave": True,
        "chat.hookFilesLocations": {
            "custom/hooks": True,
            "~/.claude/settings.json": True,
        },
    }, indent=2) + "\n"
    new, changes = iv.merge_vscode_settings(existing)
    obj = json.loads(new)
    locations = obj["chat.hookFilesLocations"]
    _assert(obj["editor.formatOnSave"] is True, obj)
    _assert(obj["chat.mcp.autostart"] is True, obj)
    _assert(locations["custom/hooks"] is True, obj)
    _assert(locations[".github/hooks"] is True, obj)
    _assert(locations[".claude/settings.local.json"] is False, obj)
    _assert(locations[".claude/settings.json"] is False, obj)
    _assert(locations["~/.claude/settings.json"] is False, obj)
    _assert(changes, "settings merge should report isolation changes")
    print("PASS merge_vscode_settings_isolates_claude_hook_discovery")


def test_write_config_backs_up_existing():
    d = Path(tempfile.mkdtemp(prefix="latch-vscode-config-"))
    try:
        p = d / "mcp.json"
        p.write_text('{"old": true}\n', encoding="utf-8")
        iv.write_config(p, '{"new": true}\n')
        _assert((d / "mcp.json.latchbak").exists(), "backup should exist")
        _assert((d / "mcp.json.latchbak").read_text(encoding="utf-8") == '{"old": true}\n',
                "backup should hold old content")
        _assert(p.read_text(encoding="utf-8") == '{"new": true}\n',
                "config should be updated")
        print("PASS write_config_backs_up_existing")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_render_mcp_server_uses_vscode_stdio_shape()
    test_merge_mcp_config_preserves_unrelated_servers_and_settings()
    test_merge_mcp_config_replaces_existing_claude_kb_only()
    test_merge_mcp_config_migrates_legacy_hyphenated_server_name()
    test_render_hooks_config_installs_only_safe_preview_hooks()
    test_merge_hooks_config_replaces_existing_file()
    test_merge_vscode_settings_isolates_claude_hook_discovery()
    test_write_config_backs_up_existing()
    print("\nAll install_vscode tests pass.")
