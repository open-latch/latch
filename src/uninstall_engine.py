#!/usr/bin/env python3
"""latch engine uninstaller — the strict inverse of ``install_engine.py``.

Reverses exactly what the install did, and nothing more. It reuses
``install_engine``'s own constants and helpers (``SERVER_NAME``,
``MANAGED_EVENTS``, ``LATCH_HOOK_MARKER``, ``_is_latch_hook_entry``,
``mcp_status``, ``find_claude``, ``resolve_python`` …) so the two halves can
never drift: if install changes what it owns, uninstall removes the same set by
construction.

What it removes (mirrors the three install steps + the slash-command copy):

  1. **MCP registration** — removes the primary ``latch`` registration and the
     legacy ``claude-kb`` alias via ``claude mcp remove``. MCP config is touched
     ONLY through the ``claude`` CLI, never by hand-editing ``~/.claude.json``
     (which also holds OAuth session + caches), exactly as install only ever
     wrote it via ``claude mcp add``.

  2. **settings.json** — removes the four latch-owned hook entries (identified
     by the ``/src/hooks/`` command marker, the same predicate install uses to
     replace-not-duplicate), the ``mcp__latch`` permission rule and legacy
     ``mcp__claude-kb`` rules (plus stale per-tool variants from older installs),
     and any dead latch-owned ``mcpServers`` blocks. **Everyone else's hooks,
     permissions, theme, … are preserved** — empty containers latch emptied are
     pruned, populated ones are left alone.

  3. **Slash commands** — removes ``~/.claude/commands/<name>.md`` for each
     command latch shipped, including legacy ``/kb-*`` aliases. Guarded:
     a dest file is only removed if it still points at this KB_HOME, so a
     user's same-named custom command is never clobbered. Honors
     ``CLAUDE_COMMANDS_DIR`` like ``install_commands.sh``.

What it does NOT remove unless asked:

  * **The CLAUDE.md managed region** is per-project and latch cannot enumerate
    every project a user synced. Pass ``--claude-md PATH`` (repeatable) to strip
    the region from a given project's CLAUDE.md (delegates to
    ``claude_md_sync.unsync`` — the region-logic single source of truth).
  * **Project KB data** (``${LATCH_HOME}/projects/<proj>/`` SQLite + logs;
    legacy ``${CLAUDE_KB_HOME}`` installs are still honored),
    the ``DISABLE`` / ``DISABLE_WRITE`` kill-switch files, and the repo/.venv
    are left in place so a user can uninstall the wiring without losing their
    accumulated KB. ``--purge`` removes the projects/ data + kill-switch files
    (the repo + venv you delete by hand: ``rm -rf ${LATCH_HOME}``).

Design notes (same as install_engine):
  * **Stdlib only** — runs under a bare/system Python; does not import the venv.
  * **Idempotent** — safe to re-run; a second run is a no-op ("nothing to
    remove").
  * **Non-destructive to user config** — settings.json is backed up to a
    timestamped ``settings.json.latchbak-<UTC>`` before any write (the
    timestamped form addresses the install-side note that a fixed ``.latchbak``
    is overwritten on re-run).

Usage:
    python src/uninstall_engine.py [--dry-run] [--check] [--purge]
                                   [--claude-md PATH ...] [--yes]
or via the wrappers:
    bash bin/uninstall.sh
    .\\bin\\uninstall.ps1   (PowerShell)

Exit code: 0 = success (or, with --check, fully removed); non-zero = a step
failed (or, with --check, latch wiring is still present).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Reuse install's contract verbatim so the inverse stays in lockstep.
import install_engine as ie

KB_HOME = ie.KB_HOME
SETTINGS_PATH = ie.SETTINGS_PATH
SERVER_NAME = ie.SERVER_NAME
ALL_SERVER_NAMES = ie.ALL_SERVER_NAMES
PERMISSION_RULE = ie.PERMISSION_RULE
ALL_PERMISSION_RULES = ie.ALL_PERMISSION_RULES
PER_TOOL_PREFIXES = tuple(rule + "__" for rule in ALL_PERMISSION_RULES)
MANAGED_EVENTS = ie.MANAGED_EVENTS
COMMANDS_SRC = KB_HOME / "commands"
LEGACY_COMMAND_ALIASES = ie.LEGACY_COMMAND_ALIASES
STALE_LEGACY_COMMANDS = ie.STALE_LEGACY_COMMANDS


# --------------------------------------------------------------------------- #
# MCP server deregistration (via the claude CLI only)
# --------------------------------------------------------------------------- #
def unregister_mcp(claude: str | None, dry_run: bool) -> tuple[str, str]:
    """Remove the user-scope MCP registration. Returns (level, message)."""
    if not claude:
        return "WARN", ("claude CLI not on PATH — cannot deregister MCP. "
                        "Remove latch-owned servers by hand: "
                        + ", ".join(f"claude mcp remove {n} -s user" for n in ALL_SERVER_NAMES))
    present: list[str] = []
    for name in ALL_SERVER_NAMES:
        try:
            p = ie._run([claude, "mcp", "get", name], timeout=30)
            if p.returncode == 0:
                present.append(name)
        except Exception:
            pass
    if not present:
        return "OK", "no latch-owned MCP server registered (nothing to remove)"
    if dry_run:
        return "DRY", "would run: " + "; ".join(
            f"claude mcp remove {name} -s user" for name in present
        )
    removed: list[str] = []
    for name in present:
        rp = ie._run([claude, "mcp", "remove", name, "-s", "user"], timeout=30)
        if rp.returncode != 0:
            return "FAIL", (f"`claude mcp remove {name}` failed (rc={rp.returncode}): "
                            + (rp.stderr or rp.stdout or "").strip()[-300:])
        removed.append(name)
    return "OK", "deregistered latch-owned MCP server(s): " + ", ".join(removed)


# --------------------------------------------------------------------------- #
# settings.json un-merge (remove only latch-owned bits, preserve the rest)
# --------------------------------------------------------------------------- #
def unmerge_settings(settings: dict) -> tuple[dict, list[str]]:
    """Return (new_settings, change_log) without writing to disk."""
    changes: list[str] = []

    # 1. hooks — drop latch-owned entries, keep everyone else's.
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        for event in MANAGED_EVENTS:
            existing = hooks.get(event)
            if not isinstance(existing, list):
                continue
            kept = [e for e in existing if not ie._is_latch_hook_entry(e)]
            if kept != existing:
                removed = len(existing) - len(kept)
                changes.append(f"hooks.{event}: removed {removed} latch hook entr"
                               f"{'y' if removed == 1 else 'ies'} "
                               f"({len(kept)} other entr"
                               f"{'y' if len(kept) == 1 else 'ies'} preserved)")
            if kept:
                hooks[event] = kept
            else:
                # We emptied this event list — remove the key rather than leave [].
                del hooks[event]
        if not hooks:
            del settings["hooks"]

    # 2. permission rules — remove the bare rule + any stale per-tool rules.
    perms = settings.get("permissions")
    if isinstance(perms, dict) and isinstance(perms.get("allow"), list):
        allow = perms["allow"]
        keep = [
            r for r in allow
            if r not in ALL_PERMISSION_RULES
            and not any(str(r).startswith(prefix) for prefix in PER_TOOL_PREFIXES)
        ]
        for r in allow:
            if r not in keep:
                changes.append(f"permissions.allow -= {r!r}")
        if keep:
            perms["allow"] = keep
        else:
            del perms["allow"]
        if not perms:
            del settings["permissions"]

    # 3. dead latch-owned mcpServers blocks (defensive — install removes them too).
    ms = settings.get("mcpServers")
    if isinstance(ms, dict):
        removed = [name for name in ALL_SERVER_NAMES if name in ms]
        for name in removed:
            del ms[name]
        if removed:
            changes.append("removed latch-owned mcpServers block(s): " + ", ".join(removed))
        if not ms:
            del settings["mcpServers"]

    return settings, changes


def write_settings_with_backup(settings: dict) -> str:
    """Write settings.json, backing up to a timestamped sibling first.

    Returns the backup filename. Timestamped (vs install's fixed .latchbak) so a
    second run never overwrites the first run's safety copy.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_name = SETTINGS_PATH.name + f".latchbak-{stamp}"
    SETTINGS_PATH.with_name(backup_name).write_text(
        SETTINGS_PATH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return backup_name


# --------------------------------------------------------------------------- #
# Slash commands
# --------------------------------------------------------------------------- #
def _commands_dest() -> Path:
    import os
    override = os.environ.get("CLAUDE_COMMANDS_DIR")
    return Path(override) if override else Path.home() / ".claude" / "commands"


def _resolved_source_command_body(name: str) -> str | None:
    src = COMMANDS_SRC / name
    if not src.is_file() and name in LEGACY_COMMAND_ALIASES:
        src = COMMANDS_SRC / LEGACY_COMMAND_ALIASES[name]
    if not src.is_file():
        return None
    try:
        return src.read_text(encoding="utf-8").replace(
            ie.COMMAND_PLACEHOLDER, ie._resolved_kb_home()
        )
    except OSError:
        return None


def remove_commands(dry_run: bool) -> list[str]:
    """Remove latch's installed slash commands; skip anything user-modified."""
    changes: list[str] = []
    dest = _commands_dest()
    if not COMMANDS_SRC.is_dir():
        return changes
    names = {src.name for src in COMMANDS_SRC.glob("*.md")}
    names.update(LEGACY_COMMAND_ALIASES)
    names.update(STALE_LEGACY_COMMANDS)
    for name in sorted(names):
        installed = dest / name
        if not installed.is_file():
            continue
        # Guard: install_commands.sh substitutes <KB_HOME> -> this repo path, so
        # a latch-installed wrapper contains it or one of latch's wrapper paths.
        # Pure instruction commands (for example latch-pm.md) may contain no path,
        # so an exact match against the resolved source body is also latch-owned.
        body = ie._read_text(installed)
        source_body = _resolved_source_command_body(name)
        if body != source_body and not ie._is_latch_command_body(body):
            changes.append(f"skipped {installed.name} (looks user-owned, not latch-installed)")
            continue
        if dry_run:
            changes.append(f"would remove command {installed}")
        else:
            installed.unlink()
            changes.append(f"removed command {installed.name}")
    return changes


# --------------------------------------------------------------------------- #
# CLAUDE.md managed region (delegates to the region-logic SSOT)
# --------------------------------------------------------------------------- #
def strip_claude_md(targets: list[str], dry_run: bool) -> list[str]:
    changes: list[str] = []
    if not targets:
        return changes
    import claude_md_sync as cms
    for t in targets:
        path = Path(t)
        if dry_run:
            status = cms.evaluate(path)
            verb = ("would strip managed region from"
                    if status in (cms.OK, cms.DRIFT) else
                    f"no managed region in ({status}):")
            changes.append(f"{verb} {path}")
        else:
            action = cms.unsync(path)
            if action == "removed":
                changes.append(f"stripped managed region from {path} "
                               f"(backup: {path}.latchbak)")
            else:
                changes.append(f"{action}: {path} (no managed region)")
    return changes


# --------------------------------------------------------------------------- #
# Purge (opt-in: KB data + kill-switch files)
# --------------------------------------------------------------------------- #
def purge_data(dry_run: bool) -> list[str]:
    changes: list[str] = []
    projects = KB_HOME / "projects"
    if projects.is_dir():
        if dry_run:
            changes.append(f"would delete KB data dir {projects} "
                           "(all projects' SQLite + logs)")
        else:
            shutil.rmtree(projects, ignore_errors=True)
            changes.append(f"deleted KB data dir {projects}")
    for flag in ("DISABLE", "DISABLE_WRITE"):
        f = KB_HOME / flag
        if f.exists():
            if dry_run:
                changes.append(f"would remove kill-switch file {f}")
            else:
                f.unlink()
                changes.append(f"removed kill-switch file {f}")
    return changes


# --------------------------------------------------------------------------- #
# --check (verify nothing latch-owned remains in Claude Code config)
# --------------------------------------------------------------------------- #
def check() -> int:
    claude = ie.find_claude()
    rows: list[tuple[bool, str]] = []

    if claude:
        present = []
        for name in ALL_SERVER_NAMES:
            try:
                if ie._run([claude, "mcp", "get", name], timeout=30).returncode == 0:
                    present.append(name)
            except Exception:
                pass
        rows.append((not present,
                     "latch-owned MCP servers deregistered"
                     if not present else f"still registered: {', '.join(present)}"))
    else:
        rows.append((False, "claude CLI on PATH (needed to verify MCP state)"))

    settings: dict = {}
    if SETTINGS_PATH.exists():
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"FAIL - {SETTINGS_PATH} is not valid JSON")
            return 1
    hooks = settings.get("hooks", {})
    for event in MANAGED_EVENTS:
        clean = not any(ie._is_latch_hook_entry(e) for e in hooks.get(event, []))
        rows.append((clean, f"no latch hook in hooks.{event}"))
    allow = settings.get("permissions", {}).get("allow", [])
    perm_clean = (
        not any(r in ALL_PERMISSION_RULES for r in allow)
        and not any(any(str(r).startswith(prefix) for prefix in PER_TOOL_PREFIXES) for r in allow)
    )
    rows.append((perm_clean, "no latch-owned MCP permission rules"))
    ms = settings.get("mcpServers")
    dead = [name for name in ALL_SERVER_NAMES if isinstance(ms, dict) and name in ms]
    rows.append((not dead,
                 "no latch-owned mcpServers block" if not dead
                 else f"dead mcpServers block(s) present: {', '.join(dead)}"))

    failed = 0
    for ok, label in rows:
        print(f"  [{'OK' if ok else 'XX'}] {label}")
        failed += 0 if ok else 1
    print()
    if failed:
        print(f"STILL PRESENT - {failed} latch item(s) remain. "
              "Run: bash bin/uninstall.sh")
        return 1
    print("OK - no latch wiring remains in Claude Code config.")
    return 0


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="latch engine uninstaller (inverse of install_engine).")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would change; write nothing")
    ap.add_argument("--check", action="store_true",
                    help="verify removal only; exit 1 if any latch wiring remains")
    ap.add_argument("--claude-md", action="append", default=[], metavar="PATH",
                    help="also strip latch's managed region from this CLAUDE.md "
                         "(repeatable)")
    ap.add_argument("--purge", action="store_true",
                    help="also delete KB data (projects/) and kill-switch files")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="skip the confirmation prompt")
    args = ap.parse_args(argv)

    if args.check:
        return check()

    print("\nlatch engine uninstaller")
    print(f"  KB_HOME  : {KB_HOME}")
    print(f"  settings : {SETTINGS_PATH}")
    print(f"  mode     : {'DRY-RUN (no writes)' if args.dry_run else 'apply'}\n")

    if not args.dry_run and not args.yes:
        try:
            ans = input("Remove latch's MCP registration, hooks, permission, and "
                        "slash commands from Claude Code? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("Aborted — nothing changed.")
            return 0

    rc = 0

    # --- 1. MCP deregistration ----------------------------------------------
    claude = ie.find_claude()
    level, msg = unregister_mcp(claude, args.dry_run)
    print(f"  [{level:4}] MCP: {msg}")
    if level == "FAIL":
        rc = 1

    # --- 2. settings.json ----------------------------------------------------
    if SETTINGS_PATH.exists():
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"  [FAIL] settings.json is not valid JSON ({e}); fix by hand.")
            return 1
        new_settings, changes = unmerge_settings(settings)
        if not changes:
            print("  [OK  ] settings.json: no latch hooks/permission present")
        elif args.dry_run:
            print("  [DRY ] settings.json would change:")
            for c in changes:
                print(f"           - {c}")
        else:
            backup = write_settings_with_backup(new_settings)
            print(f"  [OK  ] settings.json updated (backup: {backup}):")
            for c in changes:
                print(f"           - {c}")
    else:
        print("  [OK  ] settings.json: not present")

    # --- 3. slash commands ---------------------------------------------------
    cmd_changes = remove_commands(args.dry_run)
    if not cmd_changes:
        print("  [OK  ] slash commands: none of latch's commands present")
    else:
        print(f"  [{'DRY ' if args.dry_run else 'OK  '}] slash commands:")
        for c in cmd_changes:
            print(f"           - {c}")

    # --- 4. CLAUDE.md managed region (opt-in per project) --------------------
    md_changes = strip_claude_md(args.claude_md, args.dry_run)
    if md_changes:
        print(f"  [{'DRY ' if args.dry_run else 'OK  '}] CLAUDE.md:")
        for c in md_changes:
            print(f"           - {c}")

    # --- 5. purge (opt-in) ---------------------------------------------------
    if args.purge:
        purge_changes = purge_data(args.dry_run)
        if purge_changes:
            print(f"  [{'DRY ' if args.dry_run else 'OK  '}] purge:")
            for c in purge_changes:
                print(f"           - {c}")

    # --- next steps ----------------------------------------------------------
    print()
    if args.dry_run:
        print("Dry run only — re-run without --dry-run to apply.\n")
        return rc
    print("Done. Restart Claude Code so the MCP roster + hooks reload.")
    if not args.claude_md:
        print("Note: any CLAUDE.md you synced still has the managed region — strip "
              "it with:  bash bin/uninstall.sh --claude-md /path/to/CLAUDE.md")
    if not args.purge:
        print(f"Your KB data is kept at {KB_HOME}/projects/ (re-add with "
              "bin/install_engine.sh, or --purge to delete).")
    print(f"To remove latch entirely, delete the repo:  rm -rf {KB_HOME}\n")
    print("Verify removal any time with: bash bin/uninstall.sh --check")
    return rc


if __name__ == "__main__":
    sys.exit(main())
