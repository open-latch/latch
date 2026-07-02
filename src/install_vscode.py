#!/usr/bin/env python3
"""latch VS Code/Copilot installer.

This adapter wires the single-player local latch engine into VS Code/Copilot
without touching Claude Code or Codex config:

* ``.vscode/mcp.json`` gets the local ``latch`` MCP server.
* ``AGENTS.md`` gets the same latch contract Codex uses.
* optionally, VS Code preview hooks install read-side context hooks only:
  SessionStart, UserPromptSubmit, and PostToolUse.

Stop/SessionEnd compaction is deliberately not installed for VS Code yet. VS
Code hook and transcript behavior is still a preview surface, and the existing
Claude Code compactor assumes Claude-shaped transcripts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import agents_md_sync
import codex_hooks
import install_engine

SERVER_NAME = "latch"
LEGACY_SERVER_NAMES = ("claude-kb", "claudeKb")
ADAPTER_NAME = "vscode-copilot"
KB_HOME = Path(
    os.environ.get("LATCH_HOME")
    or os.environ.get("CLAUDE_KB_HOME")
    or Path(__file__).resolve().parent.parent
)
DEFAULT_MCP_PATH = Path(".vscode") / "mcp.json"
DEFAULT_HOOKS_PATH = Path(".github") / "hooks" / "latch.json"
DEFAULT_SETTINGS_PATH = Path(".vscode") / "settings.json"


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
    env = {"LATCH_ADAPTER": ADAPTER_NAME}
    if model_backend:
        env["LATCH_MODEL_BACKEND"] = model_backend
        env["LATCH_GATE_BACKEND"] = model_backend
    return env


def render_mcp_server(python_path: str, server_py: str, *, model_backend: str | None = None) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": _forward_slash(python_path),
        "args": [_forward_slash(server_py)],
        "env": _adapter_env(model_backend),
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
    servers = obj.get("servers")
    if not isinstance(servers, dict):
        servers = {}
        obj["servers"] = servers

    desired = render_mcp_server(python_path, server_py, model_backend=model_backend)
    changes: list[str] = []
    if servers.get(SERVER_NAME) != desired:
        action = "updated" if SERVER_NAME in servers else "added"
        servers[SERVER_NAME] = desired
        changes.append(f"{action} VS Code MCP server {SERVER_NAME}")

    for legacy_name in LEGACY_SERVER_NAMES:
        if legacy_name in servers and legacy_name != SERVER_NAME:
            del servers[legacy_name]
            changes.append(f"removed legacy VS Code MCP server {legacy_name}")

    new = _dump(obj)
    if new == existing:
        return new, []
    return new, changes or ["formatted VS Code MCP config"]


def mcp_status(path: Path, python_path: str, server_py: str, *, model_backend: str | None = None) -> tuple[bool, str]:
    if not path.exists():
        return False, f"VS Code MCP config missing: {path}"
    current = path.read_text(encoding="utf-8")
    desired, changes = merge_mcp_config(
        current,
        python_path,
        server_py,
        path=path,
        model_backend=model_backend,
    )
    if desired == current and not changes:
        return True, f"VS Code MCP server installed in {path}"
    return False, f"VS Code MCP server missing or drifted in {path}"


def render_hooks_config(
    python_path: str,
    *,
    model_backend: str | None = None,
) -> dict[str, Any]:
    hook_dir = KB_HOME / "src" / "hooks"
    common_env = _adapter_env(model_backend)

    def entry(script: str, timeout: int) -> dict[str, Any]:
        return {
            "type": "command",
            "command": codex_hooks.hook_command(
                _forward_slash(python_path),
                _forward_slash(str(hook_dir / script)),
            ),
            "timeout": timeout,
            "env": common_env,
        }

    return {
        "hooks": {
            "SessionStart": [entry("vscode_session_start.py", 15)],
            "UserPromptSubmit": [entry("user_prompt_submit.py", 5)],
            "PostToolUse": [entry("post_tool_use.py", 5)],
        }
    }


def merge_hooks_config(
    existing: str,
    python_path: str,
    *,
    path: Path = DEFAULT_HOOKS_PATH,
    model_backend: str | None = None,
) -> tuple[str, list[str]]:
    _json_object(existing, path=path)  # validate any existing file before overwrite
    desired = render_hooks_config(python_path, model_backend=model_backend)
    new = _dump(desired)
    if new == existing:
        return new, []
    return new, ["installed VS Code preview latch hooks"]


def hooks_status(path: Path, python_path: str, *, model_backend: str | None = None) -> tuple[bool, str]:
    if not path.exists():
        return False, f"VS Code hooks missing: {path}"
    current = path.read_text(encoding="utf-8")
    desired, changes = merge_hooks_config(
        current,
        python_path,
        path=path,
        model_backend=model_backend,
    )
    if desired == current and not changes:
        return True, f"VS Code preview hooks installed in {path}"
    return False, f"VS Code preview hooks missing or drifted in {path}"


def merge_vscode_settings(existing: str, *, path: Path = DEFAULT_SETTINGS_PATH) -> tuple[str, list[str]]:
    obj = _json_object(existing, path=path)
    desired_scalars = {
        "chat.mcp.autostart": True,
    }

    changes: list[str] = []
    for key, value in desired_scalars.items():
        if obj.get(key) != value:
            obj[key] = value
            changes.append(f"{key} = {value}")

    locations = obj.get("chat.hookFilesLocations")
    if not isinstance(locations, dict):
        locations = {}
        obj["chat.hookFilesLocations"] = locations

    desired = {
        ".github/hooks": True,
        ".claude/settings.local.json": False,
        ".claude/settings.json": False,
        "~/.claude/settings.json": False,
    }
    for key, value in desired.items():
        if locations.get(key) != value:
            locations[key] = value
            changes.append(f"chat.hookFilesLocations[{key!r}] = {value}")

    new = _dump(obj)
    if new == existing:
        return new, []
    return new, changes or ["formatted VS Code settings"]


def settings_status(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, f"VS Code settings missing: {path}"
    current = path.read_text(encoding="utf-8")
    desired, changes = merge_vscode_settings(current, path=path)
    if desired == current and not changes:
        return True, f"VS Code hook/MCP discovery configured in {path}"
    return False, f"VS Code hook/MCP discovery missing or drifted in {path}"


def write_config(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_suffix(path.suffix + ".latchbak").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    path.write_text(content, encoding="utf-8")


def _print_changes(label: str, changes: list[str], *, dry_run: bool) -> None:
    tag = "DRY " if dry_run else "OK  "
    print(f"  [{tag}] {label}:")
    for change in changes:
        print(f"          - {change}")


def _check(args: argparse.Namespace, python_path: str, server_py: str) -> int:
    checks: list[tuple[bool, str]] = []
    if not args.skip_mcp:
        checks.append(mcp_status(Path(args.mcp_json), python_path, server_py, model_backend=args.model_backend))
    if not args.skip_agents:
        status = agents_md_sync.evaluate(Path(args.agents_md))
        checks.append((status == agents_md_sync.OK, f"AGENTS.md managed region: {status}"))
    if args.with_hooks:
        checks.append(hooks_status(Path(args.hooks), python_path, model_backend=args.model_backend))
        if args.isolate_hooks:
            checks.append(settings_status(Path(args.settings)))

    failed = 0
    for ok, label in checks:
        print(f"  [{'OK' if ok else 'XX'}] {label}")
        failed += 0 if ok else 1
    print()
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="latch VS Code/Copilot installer (MCP + AGENTS.md + optional hooks).")
    ap.add_argument("--python", help="interpreter to register for the MCP server and hooks")
    ap.add_argument("--mcp-json", default=str(DEFAULT_MCP_PATH),
                    help="VS Code MCP config path (default: .vscode/mcp.json)")
    ap.add_argument("--agents-md", default="AGENTS.md",
                    help="AGENTS.md path to sync (default: ./AGENTS.md)")
    ap.add_argument("--hooks", default=str(DEFAULT_HOOKS_PATH),
                    help="VS Code hooks file path (default: .github/hooks/latch.json)")
    ap.add_argument("--settings", default=str(DEFAULT_SETTINGS_PATH),
                    help="VS Code settings path for hook discovery isolation (default: .vscode/settings.json)")
    ap.add_argument("--model-backend", choices=("claude", "codex"),
                    help="set LATCH_MODEL_BACKEND/LATCH_GATE_BACKEND for this adapter")
    ap.add_argument("--with-hooks", action="store_true",
                    help="install VS Code preview SessionStart/UserPromptSubmit/PostToolUse hooks")
    ap.add_argument("--no-isolate-hooks", dest="isolate_hooks", action="store_false",
                    help="do not write chat.hookFilesLocations when --with-hooks is used")
    ap.set_defaults(isolate_hooks=True)
    ap.add_argument("--skip-mcp", action="store_true", help="do not touch .vscode/mcp.json")
    ap.add_argument("--skip-agents", action="store_true", help="do not touch AGENTS.md")
    ap.add_argument("--yes", "-y", action="store_true", help="confirm first-time AGENTS.md wiring")
    ap.add_argument("--dry-run", action="store_true", help="print what would change")
    ap.add_argument("--check", action="store_true", help="verify wiring only")
    args = ap.parse_args(argv)

    python_path = install_engine.resolve_python(args.python)
    server_py = str((KB_HOME / "src" / "mcp_server.py")).replace("\\", "/")

    if args.check:
        return _check(args, python_path, server_py)

    print("\nlatch VS Code/Copilot installer")
    print(f"  KB_HOME      : {KB_HOME}")
    print(f"  interpreter  : {python_path}")
    print(f"  MCP config   : {'skipped' if args.skip_mcp else args.mcp_json}")
    print(f"  AGENTS.md    : {'skipped' if args.skip_agents else args.agents_md}")
    print(f"  hooks        : {args.hooks if args.with_hooks else 'skipped (pass --with-hooks)'}")
    print(f"  hook isolation: {args.settings if (args.with_hooks and args.isolate_hooks) else 'skipped'}")
    print(f"  model backend: {args.model_backend or 'engine default'}")
    print(f"  mode         : {'DRY-RUN (no writes)' if args.dry_run else 'apply'}\n")

    if not args.skip_mcp:
        mcp_path = Path(args.mcp_json)
        existing = mcp_path.read_text(encoding="utf-8") if mcp_path.exists() else ""
        new_mcp, changes = merge_mcp_config(
            existing,
            python_path,
            server_py,
            path=mcp_path,
            model_backend=args.model_backend,
        )
        if changes:
            if not args.dry_run:
                write_config(mcp_path, new_mcp)
            _print_changes("VS Code MCP config", changes, dry_run=args.dry_run)
        else:
            print("  [OK  ] VS Code MCP config already has latch")

    if not args.skip_agents:
        if args.dry_run:
            status = agents_md_sync.evaluate(Path(args.agents_md))
            print(f"  [DRY ] AGENTS.md status: {status}")
        else:
            sync_args = ["--yes", str(args.agents_md)] if args.yes else [str(args.agents_md)]
            rc = agents_md_sync.main(sync_args)
            if rc != 0:
                return rc

    if args.with_hooks:
        hooks_path = Path(args.hooks)
        existing_hooks = hooks_path.read_text(encoding="utf-8") if hooks_path.exists() else ""
        new_hooks, hook_changes = merge_hooks_config(
            existing_hooks,
            python_path,
            path=hooks_path,
            model_backend=args.model_backend,
        )
        if hook_changes:
            if not args.dry_run:
                write_config(hooks_path, new_hooks)
            _print_changes("VS Code hooks", hook_changes, dry_run=args.dry_run)
        else:
            print("  [OK  ] VS Code hooks already have latch")

        if args.isolate_hooks:
            settings_path = Path(args.settings)
            existing_settings = settings_path.read_text(encoding="utf-8") if settings_path.exists() else ""
            new_settings, settings_changes = merge_vscode_settings(existing_settings, path=settings_path)
            if settings_changes:
                if not args.dry_run:
                    write_config(settings_path, new_settings)
                _print_changes("VS Code hook/MCP discovery", settings_changes, dry_run=args.dry_run)
            else:
                print("  [OK  ] VS Code hook/MCP discovery already configured")

    print()
    if args.with_hooks:
        print("Done. Restart VS Code or run 'MCP: List Servers' so Copilot reloads the MCP server and hooks.")
    else:
        print("Done. Restart VS Code or run 'MCP: List Servers' so Copilot reloads the MCP server.")
        print("Preview hooks were not installed; re-run with --with-hooks for SessionStart/UserPromptSubmit/PostToolUse.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
