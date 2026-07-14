from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import agents_md_sync  # noqa: E402
import claude_md_sync  # noqa: E402
import cursor_hooks  # noqa: E402
import cursor_rules_sync  # noqa: E402
import cursor_wiring  # noqa: E402
import install_cursor  # noqa: E402
import versioning  # noqa: E402


def _replace(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def test_managed_doc_marker_repairs_once_and_preserves_user_content(tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_text("user-before\n", encoding="utf-8")
    assert claude_md_sync.sync(target) == "appended"
    _replace(target, "latch-wiring-version: 1", "latch-wiring-version: 0")
    _replace(target, "KB usage", "old KB usage")
    assert claude_md_sync.sync_if_outdated(target) == "synced"
    repaired = target.read_bytes()
    assert target.read_text(encoding="utf-8").startswith("user-before\n")
    assert "latch-wiring-version: 1" in target.read_text(encoding="utf-8")
    assert claude_md_sync.sync_if_outdated(target) == "unchanged"
    assert target.read_bytes() == repaired


def test_legacy_unmanaged_and_newer_managed_doc_boundaries(tmp_path):
    legacy = tmp_path / "legacy.md"
    claude_md_sync.sync(legacy)
    _replace(legacy, "<!-- latch-wiring-version: 1 -->\n", "")
    assert claude_md_sync.sync_if_outdated(legacy) == "synced"

    unmanaged = tmp_path / "plain.md"
    unmanaged.write_text("user only\n", encoding="utf-8")
    before = unmanaged.read_bytes()
    assert claude_md_sync.sync_if_outdated(unmanaged) == "skipped"
    assert unmanaged.read_bytes() == before

    newer = tmp_path / "newer.md"
    claude_md_sync.sync(newer)
    _replace(newer, "latch-wiring-version: 1", "latch-wiring-version: 999")
    before = newer.read_bytes()
    assert claude_md_sync.sync_if_outdated(newer) == "newer"
    assert newer.read_bytes() == before


def test_engine_release_version_does_not_drive_project_rewrite(tmp_path, monkeypatch):
    target = tmp_path / "CLAUDE.md"
    claude_md_sync.sync(target)
    before = target.read_bytes()
    monkeypatch.setattr(versioning, "LATCH_VERSION", "0.1.99")
    assert claude_md_sync.sync_if_outdated(target) == "unchanged"
    assert target.read_bytes() == before


def _install_cursor_bundle(root: Path, *, with_hooks: bool = True) -> None:
    mcp = root / ".cursor" / "mcp.json"
    existing = json.dumps({"setting": "keep", "mcpServers": {"other": {"command": "node"}}}) + "\n"
    rendered, _ = install_cursor.merge_mcp_config(
        existing, sys.executable, str(ROOT / "src" / "mcp_server.py"), path=mcp
    )
    install_cursor.write_config(mcp, rendered)
    agents = root / "AGENTS.md"
    agents.write_text("user-before\n", encoding="utf-8")
    agents_md_sync.sync(agents)
    cursor_rules_sync.sync(root / ".cursor" / "rules" / "latch.mdc")
    install_cursor.sync_cursor_commands(root / ".cursor" / "commands")
    install_cursor.sync_cursor_skills(root / ".cursor" / "skills")
    if with_hooks:
        hooks = root / ".cursor" / "hooks.json"
        existing_hooks = json.dumps({
            "version": 1,
            "hooks": {"sessionStart": [{"command": "user-hook", "timeout": 1}]},
        }) + "\n"
        rendered_hooks, _ = cursor_hooks.merge_hooks(
            existing_hooks,
            sys.executable,
            str(ROOT / "src" / "hooks" / "cursor_session_start.py"),
            str(ROOT / "src" / "hooks" / "cursor_before_submit.py"),
            str(ROOT / "src" / "hooks" / "cursor_pre_tool_use.py"),
            str(ROOT / "src" / "hooks" / "cursor_post_tool_use.py"),
            path=hooks,
        )
        cursor_hooks.write_hooks(hooks, rendered_hooks)


def _downgrade_cursor_bundle(root: Path) -> None:
    _replace(root / ".cursor" / "rules" / "latch.mdc", "latch-wiring-version: 1", "latch-wiring-version: 0")
    _replace(root / ".cursor" / "commands" / "latch-gate.md", "latch-wiring-version: 1", "latch-wiring-version: 0")
    data = json.loads((root / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    data["mcpServers"]["latch"]["env"]["LATCH_WIRING_VERSION"] = "0"
    (root / ".cursor" / "mcp.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_cursor_bundle_repairs_once_and_preserves_unrelated_content(tmp_path):
    _install_cursor_bundle(tmp_path)
    _downgrade_cursor_bundle(tmp_path)
    first = cursor_wiring.repair_project(tmp_path)
    assert first.action == "synced"
    assert "repaired older Cursor project wiring once" in (first.notice or "")
    assert first.restart_required is True
    assert "Cursor Settings > Tools & MCP" in (first.notice or "")
    assert "tools enabled" in (first.notice or "")
    assert "exact count can grow" in (first.notice or "")
    mcp = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert mcp["setting"] == "keep"
    assert mcp["mcpServers"]["other"] == {"command": "node"}
    hooks = json.loads((tmp_path / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
    assert any(entry.get("command") == "user-hook" for entry in hooks["hooks"]["sessionStart"])
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8").startswith("user-before\n")
    snapshot = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file() and not path.name.endswith(".latchbak")
    }
    second = cursor_wiring.repair_project(tmp_path)
    assert second.action == "unchanged"
    assert second.notice is None
    assert snapshot == {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file() and not path.name.endswith(".latchbak")
    }


def test_cursor_unmanaged_newer_and_collision_boundaries(tmp_path):
    unmanaged = tmp_path / "unmanaged"
    unmanaged.mkdir()
    assert cursor_wiring.repair_project(unmanaged).action == "unmanaged"
    assert list(unmanaged.iterdir()) == []

    newer = tmp_path / "newer"
    newer.mkdir()
    _install_cursor_bundle(newer, with_hooks=False)
    _replace(newer / ".cursor" / "rules" / "latch.mdc", "latch-wiring-version: 1", "latch-wiring-version: 999")
    before = {p.relative_to(newer): p.read_bytes() for p in newer.rglob("*") if p.is_file()}
    assert cursor_wiring.repair_project(newer).action == "newer"
    assert before == {p.relative_to(newer): p.read_bytes() for p in newer.rglob("*") if p.is_file()}

    collision = tmp_path / "collision"
    collision.mkdir()
    _install_cursor_bundle(collision, with_hooks=False)
    _downgrade_cursor_bundle(collision)
    user_file = collision / ".cursor" / "commands" / "latch-gate.md"
    user_file.write_text("user-owned command\n", encoding="utf-8")
    result = cursor_wiring.repair_project(collision)
    assert result.action == "error"
    assert "user-owned" in (result.notice or "")
    assert user_file.read_text(encoding="utf-8") == "user-owned command\n"
    assert cursor_rules_sync.wiring_state(collision / ".cursor" / "rules" / "latch.mdc") == cursor_rules_sync.OLDER


def test_cursor_additive_bundle_assets_are_installed_safely(tmp_path):
    _install_cursor_bundle(tmp_path, with_hooks=False)
    _downgrade_cursor_bundle(tmp_path)
    command = tmp_path / ".cursor" / "commands" / "latch-gate.md"
    skill = tmp_path / ".cursor" / "skills" / "source-command-latch-gate" / "SKILL.md"
    command.unlink()
    skill.unlink()

    result = cursor_wiring.repair_project(tmp_path)
    assert result.action == "synced"
    assert command.is_file()
    assert skill.is_file()
    assert "latch-wiring-version: 1" in command.read_text(encoding="utf-8")
    assert "latch-wiring-version: 1" in skill.read_text(encoding="utf-8")
    assert cursor_rules_sync.wiring_state(tmp_path / ".cursor" / "rules" / "latch.mdc") == cursor_rules_sync.CURRENT


def test_cursor_without_hooks_uses_mcp_startup_fallback(tmp_path, monkeypatch):
    _install_cursor_bundle(tmp_path, with_hooks=False)
    _downgrade_cursor_bundle(tmp_path)
    monkeypatch.setenv("LATCH_ADAPTER", "cursor")
    monkeypatch.chdir(tmp_path)
    result = cursor_wiring.repair_from_mcp_startup()
    assert result.action == "synced"
    assert cursor_rules_sync.wiring_state(tmp_path / ".cursor" / "rules" / "latch.mdc") == cursor_rules_sync.CURRENT


def test_cursor_malformed_owned_hooks_do_not_mark_bundle_current(tmp_path):
    _install_cursor_bundle(tmp_path)
    _downgrade_cursor_bundle(tmp_path)
    hooks = tmp_path / ".cursor" / "hooks.json"
    hooks.write_text('{"hooks": [{"command": "/src/hooks/cursor_session_start.py"}', encoding="utf-8")

    result = cursor_wiring.repair_project(tmp_path)
    assert result.action == "error"
    assert "invalid JSON" in (result.notice or "")
    assert cursor_rules_sync.wiring_state(tmp_path / ".cursor" / "rules" / "latch.mdc") == cursor_rules_sync.OLDER
