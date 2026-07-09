#!/usr/bin/env python3
"""latch Cursor installer.

This adapter wires the local latch engine into Cursor's project surfaces:

* ``.cursor/mcp.json`` gets the local ``latch`` MCP server.
* ``.cursor/rules/latch.mdc`` gets the Cursor-native activation rule.
* ``AGENTS.md`` gets the shared latch agent contract.

It intentionally does not install Cursor hooks, plugins, skills, or a native
Cursor model backend. Cursor can use latch MCP tools through the project MCP
config; model-backed gate calls continue to use the existing Claude/Codex
backends when explicitly selected.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import agents_md_sync
import cursor_rules_sync
import install_engine

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


def render_cursor_server(
    python_path: str,
    server_py: str,
    *,
    model_backend: str | None = None,
) -> dict[str, Any]:
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
    servers = obj.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        obj["mcpServers"] = servers

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
    desired, changes = merge_mcp_config(
        current,
        python_path,
        server_py,
        path=path,
        model_backend=model_backend,
    )
    if desired == current and not changes:
        return True, f"Cursor MCP server installed in {path}"
    return False, f"Cursor MCP server missing or drifted in {path}"


def write_config(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_suffix(path.suffix + ".latchbak").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    path.write_text(content, encoding="utf-8")


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
    if not args.skip_agents:
        status = agents_md_sync.evaluate(Path(args.agents_md))
        checks.append((status == agents_md_sync.OK, f"AGENTS.md managed region: {status}"))
    if not args.skip_rules:
        status = cursor_rules_sync.evaluate(Path(args.rules_mdc))
        checks.append((
            status == cursor_rules_sync.OK,
            f"Cursor rule {args.rules_mdc}: {status}",
        ))

    failed = 0
    for ok, label in checks:
        print(f"  [{'OK' if ok else 'XX'}] {label}")
        failed += 0 if ok else 1
    print()
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="latch Cursor installer (MCP + Cursor Rules + AGENTS.md).")
    ap.add_argument("--python", help="interpreter to register for the MCP server")
    ap.add_argument("--mcp-json", default=str(DEFAULT_MCP_PATH),
                    help="Cursor MCP config path (default: .cursor/mcp.json)")
    ap.add_argument("--agents-md", default="AGENTS.md",
                    help="AGENTS.md path to sync (default: ./AGENTS.md)")
    ap.add_argument("--rules-mdc", default=str(DEFAULT_RULE_PATH),
                    help="Cursor rule path (default: .cursor/rules/latch.mdc)")
    ap.add_argument("--model-backend", choices=("claude", "codex"),
                    help="set LATCH_MODEL_BACKEND/LATCH_GATE_BACKEND to an existing backend")
    ap.add_argument("--skip-mcp", action="store_true", help="do not touch .cursor/mcp.json")
    ap.add_argument("--skip-agents", action="store_true", help="do not touch AGENTS.md")
    ap.add_argument("--skip-rules", action="store_true", help="do not touch Cursor Rules")
    ap.add_argument("--yes", "-y", action="store_true", help="confirm first-time AGENTS.md wiring")
    ap.add_argument("--dry-run", action="store_true", help="print what would change")
    ap.add_argument("--check", action="store_true", help="verify wiring only")
    args = ap.parse_args(argv)

    python_path = install_engine.resolve_python(args.python)
    server_py = str((KB_HOME / "src" / "mcp_server.py")).replace("\\", "/")

    if args.check:
        return _check(args, python_path, server_py)

    print("\nlatch Cursor installer")
    print(f"  KB_HOME      : {KB_HOME}")
    print(f"  interpreter  : {python_path}")
    print(f"  MCP config   : {'skipped' if args.skip_mcp else args.mcp_json}")
    print(f"  Cursor rule  : {'skipped' if args.skip_rules else args.rules_mdc}")
    print(f"  AGENTS.md    : {'skipped' if args.skip_agents else args.agents_md}")
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

    print()
    if args.dry_run:
        print("Dry run only - re-run without --dry-run to apply.")
    else:
        print("Done. Restart Cursor or run 'agent mcp list' so Cursor reloads the MCP server and project rule.")
        print("Native Cursor-backed gate calls were not installed; pass --model-backend claude|codex to use an existing backend.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
