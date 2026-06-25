#!/usr/bin/env python3
"""latch Codex installer — wire MCP + AGENTS.md without touching Claude Code.

This is intentionally separate from ``install_engine.py``.  Claude Code remains
the production baseline and keeps using ``claude mcp add``, ``~/.claude``
settings, hooks, permissions, and slash commands.  Codex reads MCP servers from
``config.toml`` and project instructions from ``AGENTS.md``, so this installer
owns only those Codex surfaces.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import agents_md_sync
import codex_hooks
import install_engine

SERVER_NAME = "latch"
LEGACY_SERVER_NAMES = ("claude-kb",)
ALL_SERVER_NAMES = (SERVER_NAME, *LEGACY_SERVER_NAMES)
BEGIN_MARK = "# BEGIN LATCH CODEX MCP : managed region, do not hand-edit; re-run bin/install_codex"
END_MARK = "# END LATCH CODEX MCP"
CODEX_TOOL_TIMEOUT_SEC = 300

KB_HOME = Path(
    os.environ.get("LATCH_HOME")
    or os.environ.get("CLAUDE_KB_HOME")
    or Path(__file__).resolve().parent.parent
)
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
CONFIG_PATH = CODEX_HOME / "config.toml"
HOOKS_PATH = CODEX_HOME / "hooks.json"

_TABLE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
_OWNED_TABLES = tuple(f"mcp_servers.{name}" for name in ALL_SERVER_NAMES)


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_mcp_block(python_path: str, server_py: str) -> str:
    py = python_path.replace("\\", "/")
    server = server_py.replace("\\", "/")
    return "\n".join([
        BEGIN_MARK,
        f"[mcp_servers.{SERVER_NAME}]",
        f"command = {_toml_string(py)}",
        f"args = [{_toml_string(server)}]",
        "startup_timeout_sec = 120",
        f"tool_timeout_sec = {CODEX_TOOL_TIMEOUT_SEC}",
        'default_tools_approval_mode = "approve"',
        f"[mcp_servers.{SERVER_NAME}.env]",
        'LATCH_MODEL_BACKEND = "codex"',
        'LATCH_GATE_BACKEND = "codex"',
        END_MARK,
    ])


def _strip_managed_block(text: str) -> tuple[str, bool]:
    if BEGIN_MARK not in text or END_MARK not in text:
        return text, False
    before = text.split(BEGIN_MARK, 1)[0].rstrip("\n")
    rest = text.split(BEGIN_MARK, 1)[1]
    inner = rest.split(END_MARK, 1)[0]
    after = rest.split(END_MARK, 1)[1].lstrip("\n")
    # Codex may append hook trust tables near EOF. If our managed block is the
    # last stanza, those foreign tables can land between BEGIN/END. Preserve any
    # non-latch tables rather than deleting trust state as if it were ours.
    preserved_inner, _ = _strip_existing_server_tables(inner)
    parts = [p for p in (before, preserved_inner, after) if p.strip()]
    return "\n\n".join(parts).rstrip("\n"), True


def _is_owned_table(name: str) -> bool:
    return any(name == table or name.startswith(table + ".") for table in _OWNED_TABLES)


def _strip_existing_server_tables(text: str) -> tuple[str, bool]:
    """Remove any existing latch-owned Codex MCP table before appending ours.

    TOML cannot contain duplicate table definitions.  Because latch owns the
    ``latch`` server name and the legacy ``claude-kb`` alias, replacing those
    tables is the merge-safe behavior; unrelated tables and comments are left
    alone.
    """
    lines = text.splitlines()
    out: list[str] = []
    removing = False
    changed = False
    for line in lines:
        m = _TABLE_RE.match(line)
        if m:
            name = m.group(1).strip()
            if _is_owned_table(name):
                removing = True
                changed = True
                continue
            removing = False
        if not removing:
            out.append(line)
    return "\n".join(out).rstrip("\n"), changed


def merge_config(existing: str, python_path: str, server_py: str) -> tuple[str, list[str]]:
    changes: list[str] = []
    text, removed_block = _strip_managed_block(existing)
    if removed_block:
        changes.append("replaced existing latch-managed Codex MCP block")
    text, removed_tables = _strip_existing_server_tables(text)
    if removed_tables:
        changes.append("replaced existing latch-owned MCP server table")
    block = render_mcp_block(python_path, server_py)
    new = (text.rstrip("\n") + "\n\n" + block + "\n") if text.strip() else block + "\n"
    if new == existing:
        return new, []
    if new != existing:
        if not changes:
            changes.append(f"added mcp_servers.{SERVER_NAME} managed block")
    return new, changes


def write_config(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_suffix(path.suffix + ".latchbak").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    path.write_text(content, encoding="utf-8")


def config_status(path: Path, python_path: str, server_py: str) -> tuple[bool, str]:
    if not path.exists():
        return False, f"Codex config missing: {path}"
    current = path.read_text(encoding="utf-8")
    desired, changes = merge_config(current, python_path, server_py)
    if desired == current and not changes:
        return True, f"Codex MCP block installed in {path}"
    normalized_py = python_path.replace("\\", "/")
    normalized_server = server_py.replace("\\", "/")
    for legacy in LEGACY_SERVER_NAMES:
        if (f"[mcp_servers.{legacy}]" in current
                and normalized_py in current
                and normalized_server in current):
            return True, (f"Codex MCP block uses legacy server name {legacy!r} in {path}; "
                          f"still supported, fresh installs use {SERVER_NAME!r}")
    return False, f"Codex MCP block missing or drifted in {path}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="latch Codex installer (MCP + AGENTS.md + SessionStart hook).")
    ap.add_argument("--python", help="interpreter to register for the MCP server")
    ap.add_argument("--config", default=str(CONFIG_PATH),
                    help="Codex config.toml path (default: $CODEX_HOME/config.toml)")
    ap.add_argument("--hooks", default=str(HOOKS_PATH),
                    help="Codex hooks.json path (default: $CODEX_HOME/hooks.json)")
    ap.add_argument("--agents-md", default="AGENTS.md",
                    help="AGENTS.md path to sync (default: ./AGENTS.md)")
    ap.add_argument("--skip-agents", action="store_true",
                    help="only update Codex MCP config; do not touch AGENTS.md")
    ap.add_argument("--skip-hooks", action="store_true",
                    help="only update Codex MCP/AGENTS surfaces; do not touch hooks.json")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="confirm first-time AGENTS.md wiring")
    ap.add_argument("--dry-run", action="store_true", help="print what would change")
    ap.add_argument("--check", action="store_true", help="verify wiring only")
    ap.add_argument("--no-seed-prompt", action="store_true",
                    help="do not offer the post-install cold-start seed prompt")
    ap.add_argument("--suppress-seed-output", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    python_path = install_engine.resolve_python(args.python)
    server_py = str((KB_HOME / "src" / "mcp_server.py")).replace("\\", "/")
    hook_py = str((KB_HOME / "src" / "hooks" / "codex_session_start.py")).replace("\\", "/")
    config_path = Path(args.config)
    hooks_path = Path(args.hooks)
    agents_path = Path(args.agents_md)

    if args.check:
        ok_config, label = config_status(config_path, python_path, server_py)
        print(f"  [{'OK' if ok_config else 'XX'}] {label}")
        ok_hooks = True
        if not args.skip_hooks:
            ok_hooks, hook_label = codex_hooks.hooks_status(hooks_path, python_path, hook_py)
            print(f"  [{'OK' if ok_hooks else 'XX'}] {hook_label}")
        ok_agents = True
        if not args.skip_agents:
            status = agents_md_sync.evaluate(agents_path)
            ok_agents = status == agents_md_sync.OK
            print(f"  [{'OK' if ok_agents else 'XX'}] AGENTS.md managed region: {status}")
        return 0 if ok_config and ok_hooks and ok_agents else 1

    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    new_config, changes = merge_config(existing, python_path, server_py)
    new_hooks = ""
    hook_changes: list[str] = []
    if not args.skip_hooks:
        existing_hooks = hooks_path.read_text(encoding="utf-8") if hooks_path.exists() else ""
        new_hooks, hook_changes = codex_hooks.merge_hooks(existing_hooks, python_path, hook_py)

    print("\nlatch Codex installer")
    print(f"  KB_HOME    : {KB_HOME}")
    print(f"  interpreter: {python_path}")
    print(f"  config     : {config_path}")
    print(f"  hooks      : {'skipped' if args.skip_hooks else hooks_path}")
    print(f"  AGENTS.md  : {'skipped' if args.skip_agents else agents_path}")
    print(f"  mode       : {'DRY-RUN (no writes)' if args.dry_run else 'apply'}\n")

    if changes:
        if args.dry_run:
            print("  [DRY ] Codex config would change:")
            for c in changes:
                print(f"          - {c}")
        else:
            write_config(config_path, new_config)
            print(f"  [OK  ] Codex config updated (backup: {config_path.name}.latchbak):")
            for c in changes:
                print(f"          - {c}")
    else:
        print("  [OK  ] Codex config already has the managed MCP block")

    if not args.skip_hooks:
        if hook_changes:
            if args.dry_run:
                print("  [DRY ] Codex hooks would change:")
                for c in hook_changes:
                    print(f"          - {c}")
            else:
                codex_hooks.write_hooks(hooks_path, new_hooks)
                print(f"  [OK  ] Codex hooks updated (backup: {hooks_path.name}.latchbak):")
                for c in hook_changes:
                    print(f"          - {c}")
        else:
            print("  [OK  ] Codex hooks already include the latch SessionStart brief")

    if not args.skip_agents:
        if args.dry_run:
            status = agents_md_sync.evaluate(agents_path)
            print(f"  [DRY ] AGENTS.md status: {status}")
        else:
            sync_args = ["--yes", str(agents_path)] if args.yes else [str(agents_path)]
            rc = agents_md_sync.main(sync_args)
            if rc != 0:
                return rc

    if args.dry_run:
        print("\nDry run only — re-run without --dry-run to apply.\n")
    else:
        print("\nDone. Restart Codex or start a new Codex thread so the MCP roster, "
              "SessionStart hook, and AGENTS.md instruction chain reload.\n")

    if not args.suppress_seed_output:
        if args.dry_run or args.no_seed_prompt:
            print(install_engine.seed_next_step_message(
                command=f"{KB_HOME / 'bin' / 'latch_seed.sh'} --source codex --apply"
            ))
            print()
        elif not args.dry_run:
            install_engine.offer_seed_after_install(
                python_path=python_path,
                source="codex",
                project=Path.cwd(),
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
