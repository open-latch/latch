"""Negligible Cursor bundle version check and safe one-time self-repair."""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

import agents_md_sync
import cursor_hooks
import cursor_rules_sync
import install_cursor
from versioning import WIRING_VERSION

_MARKER_RE = re.compile(r"latch-wiring-version:\s*([0-9]+)")


@dataclass(frozen=True)
class RepairResult:
    action: str
    notice: str | None = None
    restart_required: bool = False


def _manual(project: Path) -> str:
    return (
        f"from `{project}`, run `"
        f"{shlex.quote(sys.executable)} {shlex.quote(str(install_cursor.KB_HOME / 'src' / 'install_cursor.py'))} --yes`"
    )


def _embedded_version(text: str) -> int | None:
    match = _MARKER_RE.search(text)
    return int(match.group(1)) if match else None


def _assert_no_newer_surface(path: Path) -> None:
    version = _embedded_version(path.read_text(encoding="utf-8"))
    if version is not None and version > WIRING_VERSION:
        raise RuntimeError(f"{path} has newer wiring {version}; refusing downgrade")


def _owned_hooks(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read Cursor hooks: {exc}") from exc
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        normalized = raw.replace("\\", "/")
        if any(name in normalized for name in cursor_hooks.OWNED_HOOK_BASENAMES):
            raise RuntimeError(
                "Cursor hooks contain latch-owned entries but are invalid JSON; refusing partial repair"
            ) from exc
        return False
    hooks = obj.get("hooks") if isinstance(obj, dict) else None
    return isinstance(hooks, dict) and any(
        cursor_hooks._is_owned(entry)
        for entries in hooks.values() if isinstance(entries, list)
        for entry in entries
    )


def _server_details(path: Path) -> tuple[str, str, str | None]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    servers = obj.get("mcpServers") if isinstance(obj, dict) else None
    server = servers.get("latch") if isinstance(servers, dict) else None
    if not isinstance(server, dict):
        raise RuntimeError("Cursor MCP config has no latch-owned server entry")
    command = server.get("command")
    args = server.get("args")
    if not isinstance(command, str) or not isinstance(args, list) or not args or not isinstance(args[0], str):
        raise RuntimeError("Cursor MCP latch entry has an unsupported command shape")
    env = server.get("env")
    backend = env.get("LATCH_MODEL_BACKEND") if isinstance(env, dict) else None
    return command, args[0], backend if isinstance(backend, str) else None


def repair_project(project: str | Path) -> RepairResult:
    """Repair an already-wired Cursor project once after WIRING_VERSION changes.

    The owned rule is the bundle marker and is written last. Equal/newer/missing
    markers return after one local file read. No network, Git, DB, model, or
    embeddings work occurs here.
    """
    root = Path(project).expanduser().resolve()
    rule = root / install_cursor.DEFAULT_RULE_PATH
    state = cursor_rules_sync.wiring_state(rule)
    if state == cursor_rules_sync.CURRENT:
        return RepairResult("unchanged")
    if state == cursor_rules_sync.ABSENT:
        return RepairResult("unmanaged")
    if state == cursor_rules_sync.NEWER:
        return RepairResult(
            "newer",
            "_⚠ Cursor project wiring is newer than this latch engine. Latch did not "
            "downgrade it; update latch before opening another task._",
        )
    if state == cursor_rules_sync.INVALID:
        return RepairResult(
            "invalid",
            f"_⚠ latch could not read the Cursor wiring marker. This task will continue; "
            f"{_manual(root)} manually._",
        )

    mcp_path = root / install_cursor.DEFAULT_MCP_PATH
    agents_path = root / "AGENTS.md"
    commands_dir = root / install_cursor.DEFAULT_COMMANDS_DIR
    skills_dir = root / install_cursor.DEFAULT_SKILLS_DIR
    hooks_path = root / install_cursor.DEFAULT_HOOKS_PATH
    try:
        agents_state = agents_md_sync.wiring_state(agents_path)
        if agents_state in (agents_md_sync.ABSENT, "missing"):
            raise RuntimeError("AGENTS.md has no latch-managed region")
        if agents_state in (agents_md_sync.NEWER, agents_md_sync.INVALID):
            raise RuntimeError(f"AGENTS.md wiring is {agents_state}; refusing repair")
        python_path, server_py, backend = _server_details(mcp_path)
        for name in install_cursor.CURSOR_COMMAND_FILES:
            target = commands_dir / name
            if target.is_file():
                _assert_no_newer_surface(target)
        for name in install_cursor.CURSOR_SKILL_NAMES:
            target = skills_dir / name / "SKILL.md"
            if target.is_file():
                _assert_no_newer_surface(target)
        mcp_obj = json.loads(mcp_path.read_text(encoding="utf-8"))
        mcp_env = mcp_obj["mcpServers"]["latch"].get("env", {})
        embedded = mcp_env.get("LATCH_WIRING_VERSION") if isinstance(mcp_env, dict) else None
        if embedded is not None and int(embedded) > WIRING_VERSION:
            raise RuntimeError("Cursor MCP entry has newer wiring; refusing downgrade")
        install_cursor._raise_command_collisions(
            install_cursor.cursor_command_collisions(commands_dir, model_backend=backend)
        )
        install_cursor._raise_skill_collisions(
            install_cursor.cursor_skill_collisions(skills_dir, model_backend=backend)
        )

        changed_reload = False
        current_mcp = mcp_path.read_text(encoding="utf-8")
        desired_mcp, mcp_changes = install_cursor.merge_mcp_config(
            current_mcp, python_path, server_py, path=mcp_path, model_backend=backend
        )
        desired_hooks = current_hooks = None
        hook_changes: list[str] = []
        if _owned_hooks(hooks_path):
            current_hooks = hooks_path.read_text(encoding="utf-8")
            desired_hooks, hook_changes = cursor_hooks.merge_hooks(
                current_hooks,
                python_path,
                str(install_cursor.KB_HOME / "src" / "hooks" / "cursor_session_start.py"),
                str(install_cursor.KB_HOME / "src" / "hooks" / "cursor_before_submit.py"),
                str(install_cursor.KB_HOME / "src" / "hooks" / "cursor_pre_tool_use.py"),
                str(install_cursor.KB_HOME / "src" / "hooks" / "cursor_post_tool_use.py"),
                path=hooks_path,
            )
            for entries in json.loads(current_hooks).get("hooks", {}).values():
                if isinstance(entries, list):
                    for entry in entries:
                        if cursor_hooks._is_owned(entry):
                            command = str(entry.get("command", ""))
                            version = _embedded_version(command)
                            if version is not None and version > WIRING_VERSION:
                                raise RuntimeError("Cursor hooks have newer wiring; refusing downgrade")

        if mcp_changes:
            install_cursor.write_config(mcp_path, desired_mcp)
            changed_reload = True
        install_cursor.sync_cursor_commands(commands_dir, model_backend=backend)
        install_cursor.sync_cursor_skills(skills_dir, model_backend=backend)
        if desired_hooks is not None and current_hooks is not None and hook_changes:
            cursor_hooks.write_hooks(hooks_path, desired_hooks)
            changed_reload = True
        agents_action = agents_md_sync.sync(agents_path, create=False)
        if agents_action in (agents_md_sync.NEWER, agents_md_sync.INVALID):
            raise RuntimeError(f"AGENTS.md wiring is {agents_action}")
        rule_action = cursor_rules_sync.sync(rule, create=False)
        if rule_action not in ("synced", "unchanged"):
            raise RuntimeError(f"Cursor rule repair returned {rule_action}")
    except Exception as exc:
        return RepairResult(
            "error",
            f"_⚠ latch could not safely repair older Cursor project wiring ({exc}). "
            f"This task will continue; {_manual(root)} manually._",
        )

    restart = (
        " Cursor reloads MCP when project wiring changes and may disable the "
        "workspace server again. Open Cursor Settings > Tools & MCP, select this "
        "workspace, re-enable latch, confirm '48 tools enabled', and then start a "
        "fresh Agent chat."
        if changed_reload else ""
    )
    return RepairResult(
        "synced",
        "_↻ latch repaired older Cursor project wiring once; only recognized "
        f"latch-owned files/regions changed and backups were kept.{restart}_",
        restart_required=changed_reload,
    )


def repair_from_mcp_startup() -> RepairResult:
    if os.environ.get("LATCH_ADAPTER") != "cursor":
        return RepairResult("not-cursor")
    return repair_project(Path.cwd())
