#!/usr/bin/env python3
"""latch Claude Desktop local-MCP installer.

This wires the local latch MCP server into Claude Desktop's
``claude_desktop_config.json``. It does not claim Claude Code lifecycle parity:
Claude Desktop local MCP gives tool access, not Claude Code hooks, slash
commands, or session compaction.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

import install_engine

SERVER_NAME = "latch"
LEGACY_SERVER_NAMES = ("claude-kb", "claudeKb")
ADAPTER_NAME = "claude-desktop"
KB_HOME = Path(
    os.environ.get("LATCH_HOME")
    or os.environ.get("CLAUDE_KB_HOME")
    or Path(__file__).resolve().parent.parent
)


def default_config_path() -> Path:
    override = os.environ.get("CLAUDE_DESKTOP_CONFIG")
    if override:
        return Path(override)
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


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


def render_desktop_server(
    python_path: str,
    server_py: str,
    *,
    model_backend: str | None = None,
) -> dict[str, Any]:
    return {
        "command": _forward_slash(python_path),
        "args": [_forward_slash(server_py)],
        "env": _adapter_env(model_backend),
    }


def merge_desktop_config(
    existing: str,
    python_path: str,
    server_py: str,
    *,
    path: Path,
    model_backend: str | None = None,
) -> tuple[str, list[str]]:
    obj = _json_object(existing, path=path)
    servers = obj.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        obj["mcpServers"] = servers

    desired = render_desktop_server(python_path, server_py, model_backend=model_backend)
    changes: list[str] = []
    if servers.get(SERVER_NAME) != desired:
        action = "updated" if SERVER_NAME in servers else "added"
        servers[SERVER_NAME] = desired
        changes.append(f"{action} Claude Desktop MCP server {SERVER_NAME}")

    for legacy_name in LEGACY_SERVER_NAMES:
        if legacy_name in servers and legacy_name != SERVER_NAME:
            del servers[legacy_name]
            changes.append(f"removed legacy Claude Desktop MCP server {legacy_name}")

    new = _dump(obj)
    if new == existing:
        return new, []
    return new, changes or ["formatted Claude Desktop config"]


def desktop_status(
    path: Path,
    python_path: str,
    server_py: str,
    *,
    model_backend: str | None = None,
) -> tuple[bool, str]:
    if not path.exists():
        return False, f"Claude Desktop config missing: {path}"
    current = path.read_text(encoding="utf-8")
    desired, changes = merge_desktop_config(
        current,
        python_path,
        server_py,
        path=path,
        model_backend=model_backend,
    )
    if desired == current and not changes:
        return True, f"Claude Desktop MCP server installed in {path}"
    return False, f"Claude Desktop MCP server missing or drifted in {path}"


def write_config(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_suffix(path.suffix + ".latchbak").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    path.write_text(content, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="latch Claude Desktop local-MCP installer.")
    ap.add_argument("--python", help="interpreter to register for the MCP server")
    ap.add_argument("--config", default=str(default_config_path()),
                    help="Claude Desktop config path (default: platform config path)")
    ap.add_argument("--model-backend", choices=("claude", "codex"),
                    help="set LATCH_MODEL_BACKEND/LATCH_GATE_BACKEND for this adapter")
    ap.add_argument("--dry-run", action="store_true", help="print what would change")
    ap.add_argument("--check", action="store_true", help="verify wiring only")
    args = ap.parse_args(argv)

    python_path = install_engine.resolve_python(args.python)
    server_py = str((KB_HOME / "src" / "mcp_server.py")).replace("\\", "/")
    config_path = Path(args.config)

    if args.check:
        ok, detail = desktop_status(
            config_path,
            python_path,
            server_py,
            model_backend=args.model_backend,
        )
        print(f"  [{'OK' if ok else 'XX'}] {detail}")
        return 0 if ok else 1

    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    new_config, changes = merge_desktop_config(
        existing,
        python_path,
        server_py,
        path=config_path,
        model_backend=args.model_backend,
    )

    print("\nlatch Claude Desktop installer")
    print(f"  KB_HOME      : {KB_HOME}")
    print(f"  interpreter  : {python_path}")
    print(f"  config       : {config_path}")
    print(f"  model backend: {args.model_backend or 'engine default'}")
    print(f"  mode         : {'DRY-RUN (no writes)' if args.dry_run else 'apply'}\n")

    if changes:
        if not args.dry_run:
            write_config(config_path, new_config)
        tag = "DRY " if args.dry_run else "OK  "
        print(f"  [{tag}] Claude Desktop config:")
        for change in changes:
            print(f"          - {change}")
    else:
        print("  [OK  ] Claude Desktop config already has latch")

    print()
    print("Done. Restart Claude Desktop so it reloads the local MCP server.")
    print("Note: this installs local Claude Desktop MCP access only.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
