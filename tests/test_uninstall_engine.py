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

from latch.install import install_engine as ie  # noqa: E402
from latch.hosts import agents_md_sync  # noqa: E402
from latch.hosts import cursor_hooks  # noqa: E402
from latch.hosts import cursor_rules_sync  # noqa: E402
from latch.install import install_cursor as ic  # noqa: E402
from latch.install import uninstall_engine as ue  # noqa: E402


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
        ic.sync_cursor_skills(root / ".cursor" / "skills")
        user_command = root / ".cursor" / "commands" / "mine.md"
        user_command.write_text("user-owned command\n", encoding="utf-8")
        user_skill = root / ".cursor" / "skills" / "mine" / "SKILL.md"
        user_skill.parent.mkdir(parents=True)
        user_skill.write_text("---\nname: mine\ndescription: user owned\n---\n", encoding="utf-8")
        hooks_path = root / ".cursor" / "hooks.json"
        hooks_body, _ = cursor_hooks.merge_hooks(
            json.dumps({
                "version": 1,
                "hooks": {"stop": [{"command": "user-stop"}]},
            }),
            "/py",
        "/repo/src/latch/hooks/cursor_session_start.py",
        "/repo/src/latch/hooks/cursor_before_submit.py",
        "/repo/src/latch/hooks/cursor_pre_tool_use.py",
        "/repo/src/latch/hooks/cursor_post_tool_use.py",
            path=hooks_path,
        )
        cursor_hooks.write_hooks(hooks_path, hooks_body)
        agents_md_sync.sync(root / "AGENTS.md", create=True)

        changes = ue.strip_cursor_project(str(root), dry_run=False)
        _assert(any("removed Cursor MCP server latch" in c for c in changes), changes)
        _assert(any("removed Cursor rule" in c for c in changes), changes)
        _assert(any("removed Cursor command latch-gate.md" in c for c in changes), changes)
        _assert(any("removed Cursor skill source-command-latch-gate" in c for c in changes), changes)
        _assert(any("latch-owned Cursor hook" in c for c in changes), changes)
        _assert(any("stripped managed region" in c for c in changes), changes)

        remaining = json.loads(mcp.read_text(encoding="utf-8"))
        _assert("latch" not in remaining.get("mcpServers", {}), remaining)
        _assert("other" in remaining.get("mcpServers", {}), remaining)
        _assert(user_command.exists(), "user-owned Cursor command should survive")
        _assert(user_skill.exists(), "user-owned Cursor skill should survive")
        remaining_hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        _assert(remaining_hooks["hooks"] == {"stop": [{"command": "user-stop"}]},
                remaining_hooks)
        rows = ue.cursor_project_removed(str(root))
        _assert(all(ok for ok, _label in rows), rows)
        print("PASS strip_cursor_project_removes_latch_owned_wiring_only")
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_cursor_only_main_never_calls_global_uninstall():
    root = Path(tempfile.mkdtemp(prefix="latch-uninstall-cursor-only-"))
    original_unregister = ue.unregister_mcp
    original_remove_commands = ue.remove_commands
    try:
        mcp = root / ".cursor" / "mcp.json"
        mcp.parent.mkdir(parents=True)
        body, _ = ic.merge_mcp_config("", "/py", "/srv.py")
        mcp.write_text(body, encoding="utf-8")

        def forbidden(*_args, **_kwargs):
            raise AssertionError("Cursor-only uninstall touched global Claude wiring")

        ue.unregister_mcp = forbidden
        ue.remove_commands = forbidden
        rc = ue.main([
            "--yes", "--cursor-only", "--cursor-project", str(root),
        ])
        _assert(rc == 0, rc)
        remaining = json.loads(mcp.read_text(encoding="utf-8"))
        _assert("latch" not in remaining.get("mcpServers", {}), remaining)
        _assert(ue.main([
            "--check", "--cursor-only", "--cursor-project", str(root),
        ]) == 0, "Cursor-only removal check should pass")
    finally:
        ue.unregister_mcp = original_unregister
        ue.remove_commands = original_remove_commands
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_source_checkout_removal_requires_effective_external_pin(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    external = tmp_path / "external-vault"
    pin_file = source / "kb_location.json"
    pin_file.write_text(
        json.dumps({"kb_dir": str(external)}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ue, "KB_HOME", source)
    monkeypatch.setattr(ie, "KB_LOCATION_PATH", pin_file)
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)

    _assert(
        "can be removed" in ue.source_checkout_removal_message(),
        "a persisted external pin should make source removal data-safe",
    )

    monkeypatch.setenv("LATCH_KB_DIR", str(source / "projects" / "active"))
    _assert(
        ue.source_checkout_removal_message().startswith("Keep the source checkout"),
        "an active in-source environment override must outrank an external file pin",
    )


if __name__ == "__main__":
    test_remove_commands_removes_exact_source_body_without_path_marker()
    test_remove_commands_preserves_user_modified_same_name_command()
    test_remove_commands_removes_existing_legacy_alias_exact_primary_body()
    test_strip_cursor_project_removes_latch_owned_wiring_only()
    test_cursor_only_main_never_calls_global_uninstall()
    print("\nAll uninstall_engine command tests pass.")
