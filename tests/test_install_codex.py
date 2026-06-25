"""Unit tests for the Codex installer config merge."""
from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import install_codex as ic  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_render_mcp_block_uses_codex_shape():
    out = ic.render_mcp_block("/PY", "/repo/src/mcp_server.py")
    _assert(ic.BEGIN_MARK in out and ic.END_MARK in out, "managed markers missing")
    _assert("[mcp_servers.latch]" in out, "Codex MCP table missing")
    _assert('command = "/PY"' in out, "python command missing")
    _assert('args = ["/repo/src/mcp_server.py"]' in out, "server args missing")
    _assert("tool_timeout_sec = 300" in out, "Codex gate needs room for backend calls")
    _assert('default_tools_approval_mode = "approve"' in out,
            "approval mode should be server-level")
    _assert("[mcp_servers.latch.env]" in out, "Codex MCP env table missing")
    _assert('LATCH_MODEL_BACKEND = "codex"' in out,
            "Codex install must select the generic Codex model backend")
    _assert('LATCH_GATE_BACKEND = "codex"' in out,
            "Codex install must select the Codex gate backend")
    print("PASS render_mcp_block_uses_codex_shape")


def test_merge_config_preserves_unrelated_tables():
    existing = """theme = "dark"

[mcp_servers.node_repl]
command = "/node"
args = []

[plugins."browser@openai-bundled"]
enabled = true
"""
    new, changes = ic.merge_config(existing, "/PY", "/srv.py")
    _assert(changes, "merge should report changes")
    _assert("[mcp_servers.node_repl]" in new, "unrelated MCP server must survive")
    _assert('[plugins."browser@openai-bundled"]' in new,
            "plugin config must survive")
    _assert(new.count("[mcp_servers.latch]") == 1,
            "managed server table should appear once")
    print("PASS merge_config_preserves_unrelated_tables")


def test_merge_config_replaces_existing_server_tables():
    existing = """[mcp_servers.claude-kb]
command = "/old"
args = ["/old.py"]

[mcp_servers.claude-kb.tools.kb_get]
approval_mode = "approve"

[mcp_servers.claude-kb.env]
LATCH_GATE_BACKEND = "claude"

[mcp_servers.other]
command = "node"
"""
    new, changes = ic.merge_config(existing, "/new/python", "/new/server.py")
    _assert("replaced existing latch-owned MCP server table" in changes,
            f"expected replacement change, got {changes}")
    _assert("/old" not in new, "old server config should be removed")
    _assert("claude-kb.tools.kb_get" not in new,
            "old nested tool table should be removed")
    _assert('LATCH_GATE_BACKEND = "claude"' not in new,
            "old nested env table should be removed")
    _assert('LATCH_MODEL_BACKEND = "codex"' in new,
            "new generic Codex backend env should be installed")
    _assert('LATCH_GATE_BACKEND = "codex"' in new,
            "new Codex backend env should be installed")
    _assert("[mcp_servers.other]" in new, "following unrelated table should survive")
    _assert("[mcp_servers.latch]" in new, "new primary server name should be installed")
    _assert("/new/server.py" in new, "new server path should be installed")
    print("PASS merge_config_replaces_existing_server_tables")


def test_merge_config_preserves_foreign_tables_inside_managed_block():
    existing = f"""theme = "dark"

{ic.BEGIN_MARK}
[mcp_servers.claude-kb]
command = "/old"
args = ["/old.py"]

[mcp_servers.claude-kb.env]
LATCH_GATE_BACKEND = "codex"

[hooks.state]

[hooks.state."/Users/me/.codex/hooks.json:session_start:0:0"]
trusted_hash = "sha256:test"
{ic.END_MARK}
"""
    new, changes = ic.merge_config(existing, "/new/python", "/new/server.py")
    _assert("replaced existing latch-managed Codex MCP block" in changes,
            f"expected managed replacement, got {changes}")
    _assert('[hooks.state."/Users/me/.codex/hooks.json:session_start:0:0"]' in new,
            "foreign hook trust state inside marker should be preserved")
    _assert('trusted_hash = "sha256:test"' in new,
            "hook trust hash should be preserved")
    _assert("/old.py" not in new, "old managed MCP server should be removed")
    _assert("/new/server.py" in new, "new server path should be installed")
    _assert(new.count(ic.BEGIN_MARK) == 1 and new.count(ic.END_MARK) == 1,
            "managed markers should appear once")
    print("PASS merge_config_preserves_foreign_tables_inside_managed_block")


def test_config_status_accepts_legacy_server_name():
    d = Path(tempfile.mkdtemp(prefix="latch-codex-config-"))
    try:
        p = d / "config.toml"
        p.write_text("""[mcp_servers.claude-kb]
command = "/PY"
args = ["/srv.py"]
""", encoding="utf-8")
        ok, detail = ic.config_status(p, "/PY", "/srv.py")
        _assert(ok, f"legacy Codex config should remain supported: {detail}")
        _assert("legacy server name" in detail, detail)
        print("PASS config_status_accepts_legacy_server_name")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_merge_config_idempotent():
    new1, changes1 = ic.merge_config("", "/PY", "/srv.py")
    _assert(changes1, "first merge should change")
    new2, changes2 = ic.merge_config(new1, "/PY", "/srv.py")
    _assert(new2 == new1, "second merge should be byte-identical")
    _assert(changes2 == [], f"second merge should report no changes, got {changes2}")
    print("PASS merge_config_idempotent")


def test_write_config_backs_up_existing():
    d = Path(tempfile.mkdtemp(prefix="latch-codex-config-"))
    try:
        p = d / "config.toml"
        p.write_text('theme = "dark"\n', encoding="utf-8")
        ic.write_config(p, 'theme = "light"\n')
        _assert((d / "config.toml.latchbak").exists(), "backup should exist")
        _assert((d / "config.toml.latchbak").read_text(encoding="utf-8") ==
                'theme = "dark"\n', "backup should hold old content")
        _assert(p.read_text(encoding="utf-8") == 'theme = "light"\n',
                "config should be updated")
        print("PASS write_config_backs_up_existing")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_no_seed_prompt_prints_seed_handoff_unless_suppressed():
    d = Path(tempfile.mkdtemp(prefix="latch-codex-seed-output-"))
    try:
        config = d / "config.toml"
        hooks = d / "hooks.json"
        agents = d / "AGENTS.md"

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = ic.main([
                "--python",
                sys.executable,
                "--config",
                str(config),
                "--hooks",
                str(hooks),
                "--agents-md",
                str(agents),
                "--skip-agents",
                "--skip-hooks",
                "--no-seed-prompt",
            ])
        text = output.getvalue()
        _assert(rc == 0, f"Codex installer should complete, got {rc}:\n{text}")
        _assert(text.count("Seed latch from prior work") == 1,
                f"--no-seed-prompt should still print standalone seed handoff:\n{text}")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = ic.main([
                "--python",
                sys.executable,
                "--config",
                str(config),
                "--hooks",
                str(hooks),
                "--agents-md",
                str(agents),
                "--skip-agents",
                "--skip-hooks",
                "--no-seed-prompt",
                "--suppress-seed-output",
            ])
        text = output.getvalue()
        _assert(rc == 0, f"suppressed Codex installer should complete, got {rc}:\n{text}")
        _assert("Seed latch from prior work" not in text,
                f"--suppress-seed-output should silence Codex seed handoff:\n{text}")
        print("PASS no_seed_prompt_prints_seed_handoff_unless_suppressed")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_render_mcp_block_uses_codex_shape()
    test_merge_config_preserves_unrelated_tables()
    test_merge_config_replaces_existing_server_tables()
    test_merge_config_preserves_foreign_tables_inside_managed_block()
    test_config_status_accepts_legacy_server_name()
    test_merge_config_idempotent()
    test_write_config_backs_up_existing()
    test_no_seed_prompt_prints_seed_handoff_unless_suppressed()
    print("\nAll install_codex tests pass.")
