#!/usr/bin/env python3
"""latch Cursor installer.

This adapter wires the local latch engine into Cursor's project surfaces:

* ``.cursor/mcp.json`` gets the local ``latch`` MCP server.
* ``.cursor/rules/latch.mdc`` gets the Cursor-native activation rule.
* ``.cursor/commands/*.md`` gets project-local Cursor command prompts for the
  latch workflows that are safe on Cursor today.
* ``.cursor/skills/*/SKILL.md`` gets Cursor-native workflow skills. The same
  source skills are distributable through ``.cursor-plugin/plugin.json``.
* ``AGENTS.md`` gets the shared latch agent contract.
* With ``--with-hooks``, ``.cursor/hooks.json`` gets merge-safe session,
  per-prompt gate-enforcement, and activity hooks.

It intentionally does not discover historical Cursor transcripts. The native
Cursor backend is the default; ``--model-backend claude|codex`` remains an
explicit compatibility override.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

import agents_md_sync
import cursor_hooks
import cursor_rules_sync
import install_engine
from versioning import LATCH_VERSION, WIRING_VERSION

SERVER_NAME = "latch"
LEGACY_SERVER_NAMES = ("claude-kb", "claudeKb")
ADAPTER_NAME = "cursor"
KB_HOME = Path(
    os.environ.get("LATCH_HOME")
    or os.environ.get("CLAUDE_KB_HOME")
    or Path(__file__).resolve().parent.parent
)
DEFAULT_MCP_PATH = Path(".cursor") / "mcp.json"
DEFAULT_RULE_PATH = cursor_rules_sync.DEFAULT_RULE_PATH
DEFAULT_COMMANDS_DIR = Path(".cursor") / "commands"
DEFAULT_SKILLS_DIR = Path(".cursor") / "skills"
DEFAULT_HOOKS_PATH = cursor_hooks.DEFAULT_HOOKS_PATH
COMMANDS_SRC = KB_HOME / "commands"
CURSOR_COMMANDS_SRC = KB_HOME / "cursor_commands"
CURSOR_SKILLS_SRC = KB_HOME / "cursor_skills"
CURSOR_PLUGIN_MANIFEST = KB_HOME / ".cursor-plugin" / "plugin.json"
COMMAND_PLACEHOLDER = install_engine.COMMAND_PLACEHOLDER
CURSOR_BACKEND_PLACEHOLDER = "<CURSOR_MODEL_BACKEND>"
CURSOR_COMMAND_FILES = (
    "latch-budget-approve.md",
    "latch-compact.md",
    "latch-decay.md",
    "latch-gate.md",
    "latch-gate-report.md",
    "latch-heal.md",
    "latch-pm.md",
    "latch-seed.md",
    "latch-tree.md",
    "unlatch.md",
)
UNSUPPORTED_CURSOR_COMMAND_FILES: tuple[str, ...] = ()
CURSOR_SKILL_NAMES = (
    "source-command-latch-budget-approve",
    "source-command-latch-compact",
    "source-command-latch-decay",
    "source-command-latch-gate",
    "source-command-latch-gate-report",
    "source-command-latch-heal",
    "source-command-latch-pm",
    "source-command-latch-seed",
    "source-command-latch-tree",
    "source-command-unlatch",
)


def cursor_ide_enablement_guidance() -> str:
    """Return the user-controlled IDE activation step CLI probes cannot prove."""
    return (
        "Cursor IDE action required: open Cursor Settings > Tools & MCP, select "
        "this workspace, and enable latch. Confirm latch reports tools enabled; "
        "the exact count can grow with the MCP surface. A successful "
        "'agent mcp list' or 'agent mcp list-tools latch' result is CLI-only and "
        "does not prove the Cursor IDE workspace toggle is enabled."
    )
CURSOR_COMMAND_FOOTER = (
    "\n\n---\n\n"
    "Cursor boundary: this project-local command is a reusable prompt for "
    "Cursor Agent. It may ask Agent to call latch MCP tools or run latch shell "
    "wrappers. It never authorizes undocumented Cursor-history discovery.\n"
    f"<!-- latch-wiring-version: {WIRING_VERSION} -->\n"
)
CURSOR_PYTHON_BOUNDARY_NOTE = (
    "\n\nCursor shell interpreter boundary: before any Shell call, read the "
    "workspace `.cursor/mcp.json` and use the exact absolute "
    "`mcpServers.latch.env.LATCH_PYTHON` when present, otherwise "
    "`mcpServers.latch.command`, as `<CURSOR_MCP_PYTHON>` and `LATCH_PYTHON`. "
    "The Windows MCP command may be the windowless `pythonw.exe`; Shell "
    "workflows must use the console interpreter stored in the env field. "
    "Invoke that absolute `<CURSOR_MCP_PYTHON>` path directly. Never fall back to a PATH "
    "`python3`; the MCP interpreter owns latch's native dependencies. "
    "Use the rendered absolute script path directly; do not export "
    "`LATCH_HOME` or `CLAUDE_KB_HOME` in the Shell call."
)
CURSOR_HOME_ENV_BOUNDARY_NOTE = (
    "\n\nUse the resolved latch home only to construct the absolute script path; "
    "do not export `LATCH_HOME` or `CLAUDE_KB_HOME` in the Shell call."
)
CURSOR_COMPACT_ASSETS = (
    Path("src") / "cursor_backend.py",
    Path("src") / "cursor_compact.py",
    Path("src") / "cursor_transcript.py",
    Path("bin") / "run_cursor_compact_now.sh",
    Path("bin") / "run_cursor_compact_now.ps1",
    Path("cursor_commands") / "latch-compact.md",
)


class CursorAssetCollisionError(RuntimeError):
    """Raised before install would overwrite a user-owned Cursor asset."""


class CursorConfigError(ValueError):
    """Raised when active Cursor configuration cannot be merged safely."""


def _forward_slash(value: str) -> str:
    return value.replace("\\", "/")


def _json_object(text: str, *, path: Path) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise SystemExit(f"error: {path} is not valid JSON ({e}); fix it by hand before running installer.")
    if not isinstance(obj, dict):
        raise SystemExit(f"error: {path} must contain a JSON object.")
    return obj


def _dump(obj: dict[str, Any]) -> str:
    return json.dumps(obj, indent=2) + "\n"


def _adapter_env(model_backend: str | None = None) -> dict[str, str]:
    backend = model_backend or "cursor"
    env = {
        "LATCH_ADAPTER": ADAPTER_NAME,
        "LATCH_MODEL_BACKEND": backend,
        "LATCH_GATE_BACKEND": backend,
        "LATCH_MAINTENANCE_BACKEND": backend,
        "LATCH_COMPACTOR_BACKEND": backend,
        "LATCH_WIRING_VERSION": str(WIRING_VERSION),
    }
    return env


def cursor_mcp_launcher(
    python_path: str,
    *,
    system: str | None = None,
) -> str:
    """Use the windowless venv interpreter for Cursor's Windows MCP proxy.

    Cursor starts the configured stdio server as a long-lived child.  Launching
    ``python.exe`` directly gives that child a foreground console on Windows.
    A standard Windows venv installs ``pythonw.exe`` beside ``python.exe``.
    When present, it avoids allocating a console window; ``mcp_server.py``
    restores Python's standard streams from Cursor's inherited pipe handles.
    Custom interpreters without that sibling stay unchanged.
    """
    if (system or platform.system()) != "Windows":
        return python_path
    python = Path(python_path)
    if python.name.lower() != "python.exe":
        return python_path
    windowless = python.with_name("pythonw.exe")
    return str(windowless) if windowless.is_file() else python_path


def render_cursor_server(
    python_path: str,
    server_py: str,
    *,
    model_backend: str | None = None,
) -> dict[str, Any]:
    launcher = cursor_mcp_launcher(python_path)
    env = _adapter_env(model_backend)
    entry = server_py
    if launcher != python_path:
        # Shell-backed Cursor workflows need a console interpreter so their
        # JSON/stdout remains visible.  Installed command/skill assets read
        # this field instead of reusing the windowless MCP launcher.
        env["LATCH_PYTHON"] = _forward_slash(python_path)
        # Windows windowless transport: pythonw runs mcp_launcher_win.py, which
        # spawns a base console python.exe child with CREATE_NO_WINDOW, hands it
        # Cursor's inherited stdin/stdout/stderr pipes, runs mcp_server.py there,
        # and owns it with a JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE job. This gives
        # the server real OS pipes (no in-process handle recovery) with no
        # console window and no orphaned proxy/daemon processes.
        entry = str(Path(server_py).with_name("mcp_launcher_win.py"))
    return {
        "type": "stdio",
        "command": _forward_slash(launcher),
        "args": [_forward_slash(entry)],
        "env": env,
    }


def merge_mcp_config(
    existing: str,
    python_path: str,
    server_py: str,
    *,
    path: Path = DEFAULT_MCP_PATH,
    model_backend: str | None = None,
) -> tuple[str, list[str]]:
    obj = _json_object(existing, path=path)
    if "mcpServers" not in obj:
        servers = {}
        obj["mcpServers"] = servers
    else:
        servers = obj["mcpServers"]
        if not isinstance(servers, dict):
            raise CursorConfigError(
                f"{path} has an incompatible mcpServers value "
                f"({type(servers).__name__}); expected a JSON object. "
                "Fix or move that value manually, then rerun the installer; "
                "latch did not modify the file."
            )

    desired = render_cursor_server(python_path, server_py, model_backend=model_backend)
    changes: list[str] = []
    if servers.get(SERVER_NAME) != desired:
        action = "updated" if SERVER_NAME in servers else "added"
        servers[SERVER_NAME] = desired
        changes.append(f"{action} Cursor MCP server {SERVER_NAME}")

    for legacy_name in LEGACY_SERVER_NAMES:
        if legacy_name in servers and legacy_name != SERVER_NAME:
            del servers[legacy_name]
            changes.append(f"removed legacy Cursor MCP server {legacy_name}")

    new = _dump(obj)
    if new == existing:
        return new, []
    return new, changes or ["formatted Cursor MCP config"]


def mcp_status(
    path: Path,
    python_path: str,
    server_py: str,
    *,
    model_backend: str | None = None,
) -> tuple[bool, str]:
    if not path.exists():
        return False, f"Cursor MCP config missing: {path}"
    current = path.read_text(encoding="utf-8")
    try:
        desired, changes = merge_mcp_config(
            current,
            python_path,
            server_py,
            path=path,
            model_backend=model_backend,
        )
    except CursorConfigError as exc:
        return False, f"Cursor MCP config unsafe to merge: {exc}"
    if desired == current and not changes:
        return True, f"Cursor MCP server installed in {path}"
    return False, f"Cursor MCP server missing or drifted in {path}"


def cursor_mcp_launch_assets_status(
    python_path: str,
    server_py: str,
    *,
    system: str | None = None,
) -> tuple[bool, str]:
    """Validate the executable and script Cursor will actually launch."""
    launcher = cursor_mcp_launcher(python_path, system=system)
    entry = (
        str(Path(server_py).with_name("mcp_launcher_win.py"))
        if launcher != python_path
        else server_py
    )
    missing: list[str] = []
    if not Path(launcher).is_file():
        missing.append(f"interpreter not found: {launcher}")
    if not Path(entry).is_file():
        missing.append(f"launch script not found: {entry}")
    if missing:
        return False, "; ".join(missing)
    return True, f"Cursor MCP launch target: {launcher} -> {entry}"


def write_config(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_suffix(path.suffix + ".latchbak").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    path.write_text(content, encoding="utf-8")


def render_cursor_command(
    name: str,
    kb_home: str | None = None,
    *,
    model_backend: str | None = None,
) -> str:
    override = CURSOR_COMMANDS_SRC / name
    src = override if override.is_file() else COMMANDS_SRC / name
    if name not in CURSOR_COMMAND_FILES:
        raise ValueError(f"unsupported Cursor command: {name}")
    if not src.is_file():
        raise FileNotFoundError(src)
    home = kb_home if kb_home is not None else str(KB_HOME).replace("\\", "/")
    backend = model_backend or "cursor"
    body = src.read_text(encoding="utf-8").replace(COMMAND_PLACEHOLDER, home)
    body = body.replace(CURSOR_BACKEND_PLACEHOLDER, backend)
    # Managed Cursor receipts reject command substitution.  The constrained
    # $PWD sentinel is matched against the Shell tool cwd by cursor_gate_state.
    body = body.replace('"$(pwd)"', '"$PWD"')
    body = body.replace(
        f"bash {home}/bin/run_latch_gate.sh",
        f"LATCH_GATE_BACKEND={backend} LATCH_MODEL_BACKEND={backend} "
        f"bash {home}/bin/run_latch_gate.sh",
    )
    body = body.replace(
        f'python "{home}/src/maintenance.py"',
        f'LATCH_PYTHON="<CURSOR_MCP_PYTHON>" '
        f'LATCH_MAINTENANCE_BACKEND={backend} LATCH_MODEL_BACKEND={backend} '
        f'"<CURSOR_MCP_PYTHON>" "{home}/src/maintenance.py"',
    )
    body = body.replace(
        f'python "{home}/src/budget.py"',
        f'LATCH_PYTHON="<CURSOR_MCP_PYTHON>" '
        f'"<CURSOR_MCP_PYTHON>" "{home}/src/budget.py"',
    )
    if name == "latch-decay.md":
        body = body.replace(
            f'"<CURSOR_MCP_PYTHON>" "{home}/src/maintenance.py" "$PWD"',
            f'"<CURSOR_MCP_PYTHON>" "{home}/src/maintenance.py" weekly "$PWD"',
        )
    if "mcpServers.latch.command" not in body:
        body = body.rstrip() + CURSOR_PYTHON_BOUNDARY_NOTE
    elif "do not export `LATCH_HOME`" not in body:
        body = body.rstrip() + CURSOR_HOME_ENV_BOUNDARY_NOTE
    backend_note = (
        "\n\nCursor shell-fallback backend: `" + backend + "`. On PowerShell, "
        "set `LATCH_MODEL_BACKEND`, `LATCH_GATE_BACKEND`, "
        "`LATCH_MAINTENANCE_BACKEND`, and `LATCH_COMPACTOR_BACKEND` to `"
        + backend + "` before a shell fallback. MCP calls already inherit "
        "the configured backend.\n"
    )
    return body.rstrip() + backend_note + CURSOR_COMMAND_FOOTER


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _is_latch_cursor_command_body(body: str) -> bool:
    normalized = body.replace("\\", "/")
    return (
        "Cursor boundary: this project-local command is a reusable prompt" in body
        or str(KB_HOME).replace("\\", "/") in normalized
        or any(marker in normalized for marker in install_engine.LATCH_COMMAND_MARKERS)
    )


def _is_managed_cursor_command_body(body: str) -> bool:
    """Use the installed footer as the strict ownership marker for updates."""
    return "Cursor boundary: this project-local command is a reusable prompt" in body


def cursor_command_collisions(
    commands_dir: Path,
    *,
    model_backend: str | None = None,
) -> list[Path]:
    if commands_dir.is_symlink() or (commands_dir.exists() and not commands_dir.is_dir()):
        return [commands_dir]
    collisions: list[Path] = []
    for name in CURSOR_COMMAND_FILES:
        target = commands_dir / name
        if not target.exists() and not target.is_symlink():
            continue
        if target.is_symlink() or not target.is_file():
            collisions.append(target)
            continue
        existing = _read_text(target)
        desired = render_cursor_command(name, model_backend=model_backend)
        if existing != desired and not _is_managed_cursor_command_body(existing):
            collisions.append(target)
    return collisions


def _raise_command_collisions(collisions: list[Path]) -> None:
    if not collisions:
        return
    names = ", ".join(str(path) for path in collisions)
    raise CursorAssetCollisionError(
        "refusing to overwrite user-owned Cursor command(s): " + names
        + "; move or rename the file(s), then rerun the installer"
    )


def sync_cursor_commands(
    commands_dir: Path = DEFAULT_COMMANDS_DIR,
    *,
    dry_run: bool = False,
    model_backend: str | None = None,
) -> list[str]:
    if not COMMANDS_SRC.is_dir():
        return [f"no commands/ directory at {COMMANDS_SRC} - skipped"]
    _raise_command_collisions(cursor_command_collisions(
        commands_dir, model_backend=model_backend,
    ))
    changes: list[str] = []
    if not dry_run:
        commands_dir.mkdir(parents=True, exist_ok=True)
    for name in CURSOR_COMMAND_FILES:
        desired = render_cursor_command(name, model_backend=model_backend)
        target = commands_dir / name
        existing = _read_text(target)
        if existing == desired:
            continue
        action = "updated" if target.exists() else "installed"
        if not dry_run:
            if target.exists():
                target.with_name(target.name + ".latchbak").write_text(existing, encoding="utf-8")
            target.write_text(desired, encoding="utf-8")
        changes.append(f"{action} Cursor command {name}")
    for name in UNSUPPORTED_CURSOR_COMMAND_FILES:
        target = commands_dir / name
        if not target.exists():
            continue
        body = _read_text(target)
        if not _is_latch_cursor_command_body(body):
            changes.append(f"skipped unsupported Cursor command {name} (looks user-owned)")
            continue
        if not dry_run:
            target.with_name(target.name + ".latchbak").write_text(body, encoding="utf-8")
            target.unlink()
        changes.append(f"removed unsupported Cursor command {name}")
    return changes


def cursor_commands_status(
    commands_dir: Path = DEFAULT_COMMANDS_DIR,
    *,
    model_backend: str | None = None,
) -> tuple[bool, str]:
    if not COMMANDS_SRC.is_dir():
        return True, f"Cursor commands: no commands/ source at {COMMANDS_SRC}"
    missing: list[str] = []
    drifted: list[str] = []
    unresolved: list[str] = []
    for name in CURSOR_COMMAND_FILES:
        target = commands_dir / name
        if not target.is_file():
            missing.append(name)
            continue
        body = _read_text(target)
        if COMMAND_PLACEHOLDER in body:
            unresolved.append(name)
        if CURSOR_BACKEND_PLACEHOLDER in body:
            unresolved.append(name)
        if body != render_cursor_command(name, model_backend=model_backend):
            drifted.append(name)
    unsupported = [
        name for name in UNSUPPORTED_CURSOR_COMMAND_FILES
        if (commands_dir / name).is_file()
        and _is_latch_cursor_command_body(_read_text(commands_dir / name))
    ]
    problems = []
    if missing:
        problems.append(f"missing {', '.join(missing[:3])}{'...' if len(missing) > 3 else ''}")
    if drifted:
        problems.append(f"drifted {', '.join(drifted[:3])}{'...' if len(drifted) > 3 else ''}")
    if unresolved:
        problems.append(f"unresolved command placeholder in {', '.join(unresolved[:3])}")
    if unsupported:
        problems.append(f"unsupported installed {', '.join(unsupported)}")
    if problems:
        return False, f"Cursor commands missing or drifted in {commands_dir}: " + "; ".join(problems)
    return True, f"Cursor commands installed in {commands_dir} ({len(CURSOR_COMMAND_FILES)} workflows)"


def render_cursor_skill(
    name: str,
    kb_home: str | None = None,
    *,
    model_backend: str | None = None,
) -> str:
    if name not in CURSOR_SKILL_NAMES:
        raise ValueError(f"unsupported Cursor skill: {name}")
    src = CURSOR_SKILLS_SRC / name / "SKILL.md"
    if not src.is_file():
        raise FileNotFoundError(src)
    home = kb_home if kb_home is not None else str(KB_HOME).replace("\\", "/")
    backend = model_backend or "cursor"
    body = (
        src.read_text(encoding="utf-8")
        .replace(COMMAND_PLACEHOLDER, home)
        .replace(CURSOR_BACKEND_PLACEHOLDER, backend)
    )
    footer = (
        "\n\n---\n\n"
        f"Cursor project-sync metadata: latch checkout `{home}`; shell-fallback "
        f"model backend `{backend}`. Plugin installs instead use "
        "`${CURSOR_PLUGIN_ROOT}` and native `cursor`.\n"
        f"<!-- latch-wiring-version: {WIRING_VERSION} -->\n"
    )
    return body.rstrip() + footer


def _is_latch_cursor_skill_body(body: str) -> bool:
    return "Latch Cursor skill boundary:" in body


def cursor_skill_collisions(
    skills_dir: Path,
    *,
    model_backend: str | None = None,
) -> list[Path]:
    if skills_dir.is_symlink() or (skills_dir.exists() and not skills_dir.is_dir()):
        return [skills_dir]
    collisions: list[Path] = []
    for name in CURSOR_SKILL_NAMES:
        skill_dir = skills_dir / name
        target = skill_dir / "SKILL.md"
        if skill_dir.is_symlink() or (skill_dir.exists() and not skill_dir.is_dir()):
            collisions.append(skill_dir)
            continue
        if not target.exists() and not target.is_symlink():
            continue
        if target.is_symlink() or not target.is_file():
            collisions.append(target)
            continue
        existing = _read_text(target)
        desired = render_cursor_skill(name, model_backend=model_backend)
        if existing != desired and not _is_latch_cursor_skill_body(existing):
            collisions.append(target)
    return collisions


def _raise_skill_collisions(collisions: list[Path]) -> None:
    if not collisions:
        return
    names = ", ".join(str(path) for path in collisions)
    raise CursorAssetCollisionError(
        "refusing to overwrite user-owned Cursor skill(s): " + names
        + "; move or rename the skill(s), then rerun the installer"
    )


def sync_cursor_skills(
    skills_dir: Path = DEFAULT_SKILLS_DIR,
    *,
    dry_run: bool = False,
    model_backend: str | None = None,
) -> list[str]:
    if not CURSOR_SKILLS_SRC.is_dir():
        return [f"no cursor_skills/ directory at {CURSOR_SKILLS_SRC} - skipped"]
    _raise_skill_collisions(cursor_skill_collisions(
        skills_dir, model_backend=model_backend,
    ))
    changes: list[str] = []
    for name in CURSOR_SKILL_NAMES:
        desired = render_cursor_skill(name, model_backend=model_backend)
        target = skills_dir / name / "SKILL.md"
        existing = _read_text(target)
        if existing == desired:
            continue
        action = "updated" if target.exists() else "installed"
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.with_name(target.name + ".latchbak").write_text(existing, encoding="utf-8")
            target.write_text(desired, encoding="utf-8")
        changes.append(f"{action} Cursor skill {name}")
    return changes


def cursor_skills_status(
    skills_dir: Path = DEFAULT_SKILLS_DIR,
    *,
    model_backend: str | None = None,
) -> tuple[bool, str]:
    missing: list[str] = []
    drifted: list[str] = []
    for name in CURSOR_SKILL_NAMES:
        target = skills_dir / name / "SKILL.md"
        if not target.is_file():
            missing.append(name)
        elif _read_text(target) != render_cursor_skill(name, model_backend=model_backend):
            drifted.append(name)
    problems: list[str] = []
    if missing:
        problems.append(f"missing {', '.join(missing[:3])}{'...' if len(missing) > 3 else ''}")
    if drifted:
        problems.append(f"drifted {', '.join(drifted[:3])}{'...' if len(drifted) > 3 else ''}")
    if problems:
        return False, f"Cursor skills missing or drifted in {skills_dir}: " + "; ".join(problems)
    return True, f"Cursor skills installed in {skills_dir} ({len(CURSOR_SKILL_NAMES)} workflows)"


def cursor_plugin_status() -> tuple[bool, str]:
    if not CURSOR_PLUGIN_MANIFEST.is_file():
        return False, f"Cursor plugin manifest missing: {CURSOR_PLUGIN_MANIFEST}"
    try:
        manifest = json.loads(CURSOR_PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return False, f"Cursor plugin manifest is invalid: {e}"
    if not isinstance(manifest, dict) or manifest.get("name") != "latch":
        return False, "Cursor plugin manifest must define name=latch"
    if manifest.get("skills") != "./cursor_skills/":
        return False, "Cursor plugin manifest must expose ./cursor_skills/"
    missing = [
        name for name in CURSOR_SKILL_NAMES
        if not (CURSOR_SKILLS_SRC / name / "SKILL.md").is_file()
    ]
    if missing:
        return False, "Cursor plugin skill assets missing: " + ", ".join(missing)
    return True, f"Cursor plugin manifest and {len(CURSOR_SKILL_NAMES)} skill assets present"


def cursor_compact_assets_status() -> tuple[bool, str]:
    missing = [str(path) for path in CURSOR_COMPACT_ASSETS if not (KB_HOME / path).is_file()]
    if missing:
        return False, "Cursor native backend/compact assets missing: " + ", ".join(missing)
    return True, "Cursor native backend/current-session compact assets present"


def remove_cursor_mcp_config(path: Path, *, dry_run: bool = False) -> list[str]:
    if not path.exists():
        return []
    obj = _json_object(path.read_text(encoding="utf-8"), path=path)
    servers = obj.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    removed = [name for name in (SERVER_NAME, *LEGACY_SERVER_NAMES) if name in servers]
    if not removed:
        return []
    if not dry_run:
        for name in removed:
            del servers[name]
        if not servers:
            del obj["mcpServers"]
        write_config(path, _dump(obj))
    return [f"removed Cursor MCP server {name}" for name in removed]


def remove_cursor_commands(commands_dir: Path = DEFAULT_COMMANDS_DIR, *, dry_run: bool = False) -> list[str]:
    changes: list[str] = []
    for name in (*CURSOR_COMMAND_FILES, *UNSUPPORTED_CURSOR_COMMAND_FILES):
        target = commands_dir / name
        if not target.is_file():
            continue
        body = _read_text(target)
        desired = render_cursor_command(name) if name in CURSOR_COMMAND_FILES else None
        if desired is not None and body == desired:
            if dry_run:
                changes.append(f"would remove Cursor command {name}")
            else:
                target.unlink()
                changes.append(f"removed Cursor command {name}")
            continue
        if not _is_latch_cursor_command_body(body):
            changes.append(f"skipped Cursor command {name} (looks user-owned)")
            continue
        if dry_run:
            changes.append(f"would remove latch-owned Cursor command {name}")
        else:
            target.with_name(target.name + ".latchbak").write_text(body, encoding="utf-8")
            target.unlink()
            changes.append(f"removed latch-owned Cursor command {name}")
    return changes


def remove_cursor_skills(skills_dir: Path = DEFAULT_SKILLS_DIR, *, dry_run: bool = False) -> list[str]:
    changes: list[str] = []
    for name in CURSOR_SKILL_NAMES:
        target = skills_dir / name / "SKILL.md"
        if not target.is_file():
            continue
        body = _read_text(target)
        if not _is_latch_cursor_skill_body(body):
            changes.append(f"skipped Cursor skill {name} (looks user-owned)")
            continue
        if dry_run:
            changes.append(f"would remove Cursor skill {name}")
            continue
        known = {
            render_cursor_skill(name, model_backend=backend)
            for backend in ("cursor", "claude", "codex")
        }
        if body not in known:
            target.with_name(target.name + ".latchbak").write_text(body, encoding="utf-8")
        target.unlink()
        try:
            target.parent.rmdir()
        except OSError:
            pass
        changes.append(f"removed Cursor skill {name}")
    return changes


def _agents_sync_args(agents_md: str, *, yes: bool) -> list[str]:
    args: list[str] = []
    if yes:
        args.append("--yes")
    args.extend([
        "--surface-name", "Cursor",
        "--wording-label", "shared AGENTS.md",
        agents_md,
    ])
    return args


def _print_changes(label: str, changes: list[str], *, dry_run: bool) -> None:
    tag = "DRY " if dry_run else "OK  "
    print(f"  [{tag}] {label}:")
    for change in changes:
        print(f"          - {change}")


def _check(args: argparse.Namespace, python_path: str, server_py: str) -> int:
    checks: list[tuple[bool, str]] = []
    if not args.skip_mcp:
        checks.append(mcp_status(
            Path(args.mcp_json),
            python_path,
            server_py,
            model_backend=args.model_backend,
        ))
        checks.append(cursor_mcp_launch_assets_status(python_path, server_py))
    if not args.skip_agents:
        status = agents_md_sync.evaluate(Path(args.agents_md))
        checks.append((status == agents_md_sync.OK, f"AGENTS.md managed region: {status}"))
    if not args.skip_rules:
        status = cursor_rules_sync.evaluate(Path(args.rules_mdc))
        checks.append((
            status == cursor_rules_sync.OK,
            f"Cursor rule {args.rules_mdc}: {status}",
        ))
    if not args.skip_commands:
        checks.append(cursor_commands_status(
            Path(args.commands_dir), model_backend=args.model_backend,
        ))
    if not args.skip_skills:
        checks.append(cursor_skills_status(
            Path(args.skills_dir), model_backend=args.model_backend,
        ))
    if args.with_hooks:
        checks.append(cursor_hooks.hooks_status(
            Path(args.hooks_json),
            python_path,
            str(KB_HOME / "src" / "hooks" / "cursor_session_start.py"),
            str(KB_HOME / "src" / "hooks" / "cursor_before_submit.py"),
            str(KB_HOME / "src" / "hooks" / "cursor_pre_tool_use.py"),
            str(KB_HOME / "src" / "hooks" / "cursor_post_tool_use.py"),
        ))
    checks.append(cursor_compact_assets_status())
    checks.append(cursor_plugin_status())

    failed = 0
    for ok, label in checks:
        print(f"  [{'OK' if ok else 'XX'}] {label}")
        failed += 0 if ok else 1
    print()
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="latch Cursor installer (MCP + Rules + Commands + Skills + AGENTS.md; optional hooks).")
    ap.add_argument("--python", help="interpreter to register for the MCP server")
    ap.add_argument("--mcp-json", default=str(DEFAULT_MCP_PATH),
                    help="Cursor MCP config path (default: .cursor/mcp.json)")
    ap.add_argument("--agents-md", default="AGENTS.md",
                    help="AGENTS.md path to sync (default: ./AGENTS.md)")
    ap.add_argument("--rules-mdc", default=str(DEFAULT_RULE_PATH),
                    help="Cursor rule path (default: .cursor/rules/latch.mdc)")
    ap.add_argument("--commands-dir", default=str(DEFAULT_COMMANDS_DIR),
                    help="Cursor commands directory (default: .cursor/commands)")
    ap.add_argument("--skills-dir", default=str(DEFAULT_SKILLS_DIR),
                    help="Cursor skills directory (default: .cursor/skills)")
    ap.add_argument("--hooks-json", default=str(DEFAULT_HOOKS_PATH),
                    help="Cursor hooks config path (default: .cursor/hooks.json)")
    ap.add_argument("--with-hooks", action="store_true",
                    help="install/check opt-in Cursor session, gate-enforcement, and activity hooks")
    ap.add_argument("--model-backend", choices=("cursor", "claude", "codex"),
                    help="model backend (default: native Cursor Agent CLI)")
    ap.add_argument("--skip-mcp", action="store_true", help="do not touch .cursor/mcp.json")
    ap.add_argument("--skip-agents", action="store_true", help="do not touch AGENTS.md")
    ap.add_argument("--skip-rules", action="store_true", help="do not touch Cursor Rules")
    ap.add_argument("--skip-commands", action="store_true", help="do not touch .cursor/commands")
    ap.add_argument("--skip-skills", action="store_true", help="do not touch .cursor/skills")
    ap.add_argument("--yes", "-y", action="store_true", help="confirm first-time AGENTS.md wiring")
    ap.add_argument("--dry-run", action="store_true", help="print what would change")
    ap.add_argument("--check", action="store_true", help="verify wiring only")
    args = ap.parse_args(argv)

    python_path = install_engine.resolve_python(args.python)
    server_py = str((KB_HOME / "src" / "mcp_server.py")).replace("\\", "/")

    if args.check:
        return _check(args, python_path, server_py)

    # Asset ownership is a transaction precondition. Check every requested
    # command and skill before touching MCP, AGENTS.md, rules, hooks, or any
    # other managed asset so a late same-name collision cannot leave a partial
    # install behind.
    try:
        if not args.skip_commands:
            _raise_command_collisions(cursor_command_collisions(
                Path(args.commands_dir), model_backend=args.model_backend,
            ))
        if not args.skip_skills:
            _raise_skill_collisions(cursor_skill_collisions(
                Path(args.skills_dir), model_backend=args.model_backend,
            ))
    except CursorAssetCollisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print("\nlatch Cursor installer")
    print(f"  version      : {LATCH_VERSION} (wiring {WIRING_VERSION})")
    print(f"  KB_HOME      : {KB_HOME}")
    print(f"  interpreter  : {python_path}")
    print(f"  MCP config   : {'skipped' if args.skip_mcp else args.mcp_json}")
    print(f"  Cursor rule  : {'skipped' if args.skip_rules else args.rules_mdc}")
    print(f"  Commands     : {'skipped' if args.skip_commands else args.commands_dir}")
    print(f"  Skills       : {'skipped' if args.skip_skills else args.skills_dir}")
    print(f"  Hooks        : {args.hooks_json if args.with_hooks else 'skipped (pass --with-hooks)'}")
    print(f"  AGENTS.md    : {'skipped' if args.skip_agents else args.agents_md}")
    print(f"  model backend: {args.model_backend or 'cursor (native default)'}")
    print(f"  mode         : {'DRY-RUN (no writes)' if args.dry_run else 'apply'}\n")

    if not args.skip_mcp:
        mcp_path = Path(args.mcp_json)
        existing = mcp_path.read_text(encoding="utf-8") if mcp_path.exists() else ""
        try:
            new_mcp, changes = merge_mcp_config(
                existing,
                python_path,
                server_py,
                path=mcp_path,
                model_backend=args.model_backend,
            )
        except CursorConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if changes:
            if not args.dry_run:
                write_config(mcp_path, new_mcp)
            _print_changes("Cursor MCP config", changes, dry_run=args.dry_run)
        else:
            print("  [OK  ] Cursor MCP config already has latch")

    if not args.skip_agents:
        if args.dry_run:
            status = agents_md_sync.evaluate(Path(args.agents_md))
            print(f"  [DRY ] AGENTS.md status: {status}")
        else:
            rc = agents_md_sync.main(_agents_sync_args(str(args.agents_md), yes=args.yes))
            if rc != 0:
                return rc

    if not args.skip_rules:
        rules_path = Path(args.rules_mdc)
        if args.dry_run:
            status = cursor_rules_sync.evaluate(rules_path)
            print(f"  [DRY ] Cursor rule status: {status}")
        else:
            action = cursor_rules_sync.sync(rules_path)
            if action == "synced":
                print(f"  [OK  ] Cursor rule synced (backup: {rules_path}.latchbak)")
            elif action == "created":
                print(f"  [OK  ] Cursor rule created at {rules_path}")
            else:
                print(f"  [OK  ] Cursor rule {action}: {rules_path}")

    if not args.skip_commands:
        try:
            changes = sync_cursor_commands(
                Path(args.commands_dir), dry_run=args.dry_run,
                model_backend=args.model_backend,
            )
        except CursorAssetCollisionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if changes:
            _print_changes("Cursor commands", changes, dry_run=args.dry_run)
        else:
            print("  [OK  ] Cursor commands already have latch")

    if not args.skip_skills:
        try:
            changes = sync_cursor_skills(
                Path(args.skills_dir), dry_run=args.dry_run,
                model_backend=args.model_backend,
            )
        except CursorAssetCollisionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if changes:
            _print_changes("Cursor skills", changes, dry_run=args.dry_run)
        else:
            print("  [OK  ] Cursor skills already have latch")

    if args.with_hooks:
        hooks_path = Path(args.hooks_json)
        existing = hooks_path.read_text(encoding="utf-8") if hooks_path.exists() else ""
        new_hooks, changes = cursor_hooks.merge_hooks(
            existing,
            python_path,
            str(KB_HOME / "src" / "hooks" / "cursor_session_start.py"),
            str(KB_HOME / "src" / "hooks" / "cursor_before_submit.py"),
            str(KB_HOME / "src" / "hooks" / "cursor_pre_tool_use.py"),
            str(KB_HOME / "src" / "hooks" / "cursor_post_tool_use.py"),
            path=hooks_path,
        )
        if changes:
            if not args.dry_run:
                cursor_hooks.write_hooks(hooks_path, new_hooks)
            _print_changes("Cursor hooks", changes, dry_run=args.dry_run)
        else:
            print("  [OK  ] Cursor hooks already have latch")

    print()
    if args.dry_run:
        print("Dry run only - re-run without --dry-run to apply.")
    else:
        print("Done. Restart Cursor or run 'agent mcp list' so Cursor reloads the project wiring.")
        if not args.skip_mcp:
            print(cursor_ide_enablement_guidance())
        if not args.with_hooks:
            print("Cursor hooks were not installed; re-run with --with-hooks for session briefing, pre-edit gating, and activity context.")
        print("Cursor Agent CLI is the native model backend; pass --model-backend claude|codex only for an explicit compatibility override.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
