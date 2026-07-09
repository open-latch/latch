#!/usr/bin/env python3
"""Unit tests for slash-command removal in uninstall_engine."""
from __future__ import annotations

import os
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import install_engine as ie  # noqa: E402
import agents_md_sync  # noqa: E402
import cursor_rules_sync  # noqa: E402
import install_cursor as ic  # noqa: E402
import uninstall_engine as ue  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp_commands_env(kb_home: str, src_files: dict[str, str]):
    root = Path(tempfile.mkdtemp(prefix="latch-uninstall-cmd-"))
    src = root / "commands"
    dest = root / "dest"
    src.mkdir()
    dest.mkdir()
    for name, body in src_files.items():
        (src / name).write_text(body, encoding="utf-8")
    saved = (ie.KB_HOME, ue.KB_HOME, ue.COMMANDS_SRC, os.environ.get("CLAUDE_COMMANDS_DIR"))
    ie.KB_HOME = Path(kb_home)
    ue.KB_HOME = Path(kb_home)
    ue.COMMANDS_SRC = src
    os.environ["CLAUDE_COMMANDS_DIR"] = str(dest)

    def restore():
        ie.KB_HOME, ue.KB_HOME, ue.COMMANDS_SRC, old_dest = saved
        if old_dest is None:
            os.environ.pop("CLAUDE_COMMANDS_DIR", None)
        else:
            os.environ["CLAUDE_COMMANDS_DIR"] = old_dest

    return src, dest, restore


def test_remove_commands_removes_exact_source_body_without_path_marker():
    _src, dest, restore = _tmp_commands_env(
        "/opt/latch",
        {"latch-pm.md": "pure instruction command\n"},
    )
    try:
        (dest / "latch-pm.md").write_text("pure instruction command\n", encoding="utf-8")
        changes = ue.remove_commands(dry_run=False)
        _assert(not (dest / "latch-pm.md").exists(),
                f"exact source-body command should be removed: {changes}")
        print("PASS remove_commands_removes_exact_source_body_without_path_marker")
    finally:
        restore()


def test_remove_commands_preserves_user_modified_same_name_command():
    _src, dest, restore = _tmp_commands_env(
        "/opt/latch",
        {"latch-pm.md": "pure instruction command\n"},
    )
    try:
        custom = "my custom command\n"
        (dest / "latch-pm.md").write_text(custom, encoding="utf-8")
        changes = ue.remove_commands(dry_run=False)
        _assert((dest / "latch-pm.md").read_text(encoding="utf-8") == custom,
                f"user-owned same-name command should survive: {changes}")
        _assert(any("skipped latch-pm.md" in c for c in changes), changes)
        print("PASS remove_commands_preserves_user_modified_same_name_command")
    finally:
        restore()


def test_remove_commands_removes_existing_legacy_alias_exact_primary_body():
    _src, dest, restore = _tmp_commands_env(
        "/opt/latch",
        {"latch-gate.md": "bash <KB_HOME>/bin/run_latch_gate.sh\n"},
    )
    try:
        (dest / "kb-gate.md").write_text(
            "bash /opt/latch/bin/run_latch_gate.sh\n", encoding="utf-8")
        changes = ue.remove_commands(dry_run=False)
        _assert(not (dest / "kb-gate.md").exists(),
                f"legacy alias matching primary body should be removed: {changes}")
        print("PASS remove_commands_removes_existing_legacy_alias_exact_primary_body")
    finally:
        restore()


def test_strip_cursor_project_removes_latch_owned_wiring_only():
    root = Path(tempfile.mkdtemp(prefix="latch-uninstall-cursor-"))
    try:
        mcp = root / ".cursor" / "mcp.json"
        mcp.parent.mkdir(parents=True)
        body, _ = ic.merge_mcp_config(
            json.dumps({
                "mcpServers": {
                    "other": {"command": "node", "args": ["server.js"]},
                },
                "setting": True,
            }) + "\n",
            "/py",
            "/srv.py",
        )
        mcp.write_text(body, encoding="utf-8")
        cursor_rules_sync.sync(root / ".cursor" / "rules" / "latch.mdc")
        ic.sync_cursor_commands(root / ".cursor" / "commands")
        user_command = root / ".cursor" / "commands" / "mine.md"
        user_command.write_text("user-owned command\n", encoding="utf-8")
        agents_md_sync.sync(root / "AGENTS.md", create=True)

        changes = ue.strip_cursor_project(str(root), dry_run=False)
        _assert(any("removed Cursor MCP server latch" in c for c in changes), changes)
        _assert(any("removed Cursor rule" in c for c in changes), changes)
        _assert(any("removed Cursor command latch-gate.md" in c for c in changes), changes)
        _assert(any("stripped managed region" in c for c in changes), changes)

        remaining = json.loads(mcp.read_text(encoding="utf-8"))
        _assert("latch" not in remaining.get("mcpServers", {}), remaining)
        _assert("other" in remaining.get("mcpServers", {}), remaining)
        _assert(user_command.exists(), "user-owned Cursor command should survive")
        rows = ue.cursor_project_removed(str(root))
        _assert(all(ok for ok, _label in rows), rows)
        print("PASS strip_cursor_project_removes_latch_owned_wiring_only")
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    test_remove_commands_removes_exact_source_body_without_path_marker()
    test_remove_commands_preserves_user_modified_same_name_command()
    test_remove_commands_removes_existing_legacy_alias_exact_primary_body()
    test_strip_cursor_project_removes_latch_owned_wiring_only()
    print("\nAll uninstall_engine command tests pass.")
