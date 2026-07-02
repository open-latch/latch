#!/usr/bin/env python3
"""latch engine installer — wire the KB engine into Claude Code.

This is the *engine* half of latch's install (the behavior half is
``install_claude_md.py``). It also folds in the slash-command install (step 4
below) so a single run wires the latch tools, hooks, permission AND the /latch-*
commands — ``install_commands.{sh,ps1}`` remain for commands-only re-installs.
It does the things that previously lived as a manual README copy-paste
playbook and were the #1 source of install friction:

  1. **Register the MCP server the way Claude Code actually reads it.**
     Claude Code does NOT load MCP servers from ``mcpServers`` in
     ``~/.claude/settings.json`` — it only reads them from the ``claude mcp``
     registry (``~/.claude.json``, written by ``claude mcp add``) or a project
     ``.mcp.json``. Hooks *are* read from settings.json, so a settings.json-only
     install looks half-alive (the SessionStart brief fires) while the
     latch-named tools never connect. We register with
     ``claude mcp add --scope user`` so the tools load in every project.

  2. **Merge the hooks** (SessionStart / UserPromptSubmit / Stop / SessionEnd)
     into ``~/.claude/settings.json`` — this half genuinely belongs there.

  3. **Pre-approve the tools with ONE permission rule** — ``mcp__latch``
     (the bare server prefix) auto-approves every tool the server exposes,
     current and future, so the user is never prompted to accept latch_get /
     latch_insert / ... individually. This replaces the stale hand-pasted list of
     individual ``mcp__claude-kb__kb_*`` entries. Existing ``claude-kb``
     registrations remain supported as a legacy alias during the rename.

  4. **Install the slash commands.** Copy ``commands/*.md`` into
     ``~/.claude/commands/`` (resolving the ``<KB_HOME>`` placeholder) so
     ``/latch-compact`` & friends are discoverable. This used to be a separate
     manual step; skipping it left a "looks-done" install where the commands
     silently errored ``Unknown skill`` (id=1468 #1).

It also removes now-dead latch-owned ``mcpServers`` blocks from settings.json if
a previous (broken) install left one there.

Design notes:
  * **Stdlib only** — like ``doctor.py``, this must run under a bare/system
    Python, because the venv it configures may not be on the interpreter
    running the installer. The interpreter it *registers* (for the server +
    hooks) is resolved separately (``resolve_python``) and points at the venv.
  * **Idempotent** — safe to re-run. MCP registration is guarded by
    ``claude mcp get``; hook entries owned by latch are replaced (not
    duplicated) on re-run, which also re-points stale interpreter paths;
    the permission rule is union-added.
  * **Non-destructive** — settings.json is backed up to
    ``settings.json.latchbak`` before any write; everything outside the bits we
    own (other hooks, other permissions, theme, ...) is preserved.
  * MCP config is touched ONLY through the ``claude`` CLI — never by
    hand-editing ``~/.claude.json``, which also holds OAuth session + caches.

Usage:
    python src/install_engine.py [--python PATH] [--dry-run] [--check]
or via the wrappers:
    bash bin/install_engine.sh
    .\\bin\\install_engine.ps1   (PowerShell)

Exit code: 0 = success (or, with --check, fully wired); non-zero = a step
failed (or, with --check, something is missing).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

SERVER_NAME = "latch"
LEGACY_SERVER_NAMES = ("claude-kb",)
ALL_SERVER_NAMES = (SERVER_NAME, *LEGACY_SERVER_NAMES)
PERMISSION_RULE = f"mcp__{SERVER_NAME}"
LEGACY_PERMISSION_RULES = tuple(f"mcp__{name}" for name in LEGACY_SERVER_NAMES)
ALL_PERMISSION_RULES = (PERMISSION_RULE, *LEGACY_PERMISSION_RULES)
MANAGED_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PostToolUse")
# Substring that identifies a hook command as latch-owned (so re-runs replace
# rather than duplicate, and stale interpreter paths get re-pointed).
LATCH_HOOK_MARKER = "/src/hooks/"

KB_HOME = Path(
    os.environ.get("LATCH_HOME")
    or os.environ.get("CLAUDE_KB_HOME")
    or Path(__file__).resolve().parent.parent
)
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
SNIPPET_PATH = KB_HOME / "settings_snippet.json"

# Slash-command install (step 4): commands/*.md -> Claude Code's commands dir,
# with the <KB_HOME> placeholder resolved to this clone's path.
COMMANDS_SRC = KB_HOME / "commands"
COMMANDS_DEST = Path(os.environ.get("CLAUDE_COMMANDS_DIR") or (Path.home() / ".claude" / "commands"))
COMMAND_PLACEHOLDER = "<KB_HOME>"
LEGACY_COMMAND_ALIASES = {
    "kb-budget-approve.md": "latch-budget-approve.md",
    "kb-compact.md": "latch-compact.md",
    "kb-decay.md": "latch-decay.md",
    "kb-gate.md": "latch-gate.md",
    "kb-gate-report.md": "latch-gate-report.md",
    "kb-heal.md": "latch-heal.md",
    "kb-tree.md": "latch-tree.md",
}
STALE_LEGACY_COMMANDS = (
    "kb-focus.md",
    "kb-project-direction.md",
)
LATCH_COMMAND_MARKERS = (
    "/bin/run_kb_gate.sh",
    "/bin/run_latch_gate.sh",
    "/bin/latch_gate_report.sh",
    "/bin/run_compact_now.sh",
    "/bin/run_latch_compact_now.sh",
    "/bin/run_kb_focus.sh",
    "/bin/latch_direction.sh",
    "/src/budget.py",
    "/src/maintenance.py",
)

# Install-time KB-dir pin (KB id=1556): the single fixed KB directory, written to
# kb_location.json so paths._resolve_pinned_dir() never selects the DB from cwd.
# This is the install-half of the fix that makes the wrong-DB bug family
# (id=302/307/335/1461/1523/1555) structurally impossible.
KB_LOCATION_PATH = KB_HOME / "kb_location.json"
PROJECTS_DIR = KB_HOME / "projects"
DEFAULT_STORE_DIR = KB_HOME / "store"


def seed_next_step_message(command: str = "bash bin/latch_seed.sh --apply") -> str:
    """User-facing post-install prompt for the cold-start seed pass."""
    return (
        "Seed latch from prior work for immediate judgment value from latch:\n"
        f"  {command}\n"
        "Seeding reads selected local Claude and/or Codex chats for this project and "
        "uses LLM calls to propose decisions, preferences, and rejected paths that "
        "latch can judge against before the first new compacted session. It shows "
        "a structured seed report first, includes a rejected-path catch-demo command "
        "to run after you approve staging evidence, repeats that proof command "
        "after a successful write, asks which transcript source to use, asks for "
        "a lookback window, asks how many recent sessions to scan "
        "(default 20; configurable with --last-sessions N), keeps the LLM-call "
        "budget guardrail, and writes only staging evidence when you approve it."
    )


def seed_command_args(
    *,
    python_path: str,
    project: Path,
    source: str = "auto",
) -> list[str]:
    return [
        python_path,
        str(KB_HOME / "src" / "seed.py"),
        "--project",
        str(project),
        "--source",
        source,
        "--apply",
    ]


def format_command(args: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in args)


def _stdio_is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _prompt_yes_no(prompt: str, *, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    raw = input(prompt + suffix).strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def offer_seed_after_install(
    *,
    python_path: str,
    source: str = "auto",
    project: Path | None = None,
) -> None:
    """Offer an interactive LLM-backed seed pass without making install depend on it."""
    project = (project or Path.cwd()).resolve()
    command = seed_command_args(python_path=python_path, project=project, source=source)
    command_text = format_command(command)

    print()
    print(seed_next_step_message(command_text))
    print()

    if not _stdio_is_tty():
        print("Non-interactive shell: install is complete. Run the seed command above "
              "from the project you want latch to learn when you are ready.")
        return

    print(f"Current seed target: {project}")
    if not _prompt_yes_no("Run LLM-backed seed now for this project?", default=True):
        print("Skipped seed. Run the command above later to avoid a cold start.")
        return

    print()
    result = subprocess.run(command)
    if result.returncode == 0:
        print("Seed step finished.")
    else:
        print(f"Seed step exited with status {result.returncode}; install is still complete.")


# --------------------------------------------------------------------------- #
# Resolution helpers
# --------------------------------------------------------------------------- #
def resolve_python(override: str | None) -> str:
    """Absolute path to the interpreter the server + hooks should run under.

    Order: --python > $LATCH_PYTHON > $CLAUDE_KB_PYTHON > repo .venv >
    sys.executable > PATH.
    The venv is preferred because that is where latch's deps are installed; an
    explicit override wins so a user with a custom interpreter can force it.
    """
    if override:
        return _abs(override)
    env = os.environ.get("LATCH_PYTHON")
    if env:
        return _abs(env)
    env = os.environ.get("CLAUDE_KB_PYTHON")
    if env:
        return _abs(env)
    if platform.system() == "Windows":
        venv = KB_HOME / ".venv" / "Scripts" / "python.exe"
    else:
        venv = KB_HOME / ".venv" / "bin" / "python"
    if venv.exists():
        return str(venv)
    if sys.executable:
        return sys.executable
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return "python"  # last resort; resolved on PATH at runtime


def _abs(p: str) -> str:
    """Absolutize a path that exists; leave bare names (e.g. 'python3') alone
    so PATH resolution still happens at runtime."""
    path = Path(p)
    if path.exists():
        return str(path.resolve())
    return p


def find_claude() -> str | None:
    return shutil.which("claude")


def apply_preflight_errors(claude: str | None) -> list[str]:
    """Blocking prerequisite failures for apply mode.

    Apply mode mutates user config. If Claude Code is not installed yet, a
    partial install can make the setup look wired while the MCP server cannot
    actually be registered. Dry-run and --check still report the missing CLI
    without writing.
    """
    if claude:
        return []
    return [
        "Claude Code CLI (`claude`) not found on PATH.",
        "Install Claude Code, open/sign in once if needed, restart this shell, "
        "then re-run `bash bin/install_engine.sh`.",
    ]


def restart_next_step_message() -> str:
    return (
        "Done. Restart VS Code (or Claude Code, if you run it outside VS Code) "
        "so the MCP roster reloads; then the latch_* tools load automatically "
        "with no per-tool prompts. Legacy kb_* aliases remain available."
    )


def load_snippet(python_path: str) -> dict:
    """Read settings_snippet.json and substitute the path placeholders."""
    raw = SNIPPET_PATH.read_text(encoding="utf-8")
    # Forward-slash both paths before they land in JSON: a Windows backslash
    # path (e.g. C:\...\python.exe) injects invalid JSON escapes (\U, \A, ...)
    # and makes json.loads choke. Forward slashes work fine for the interpreter
    # + KB_HOME on Windows, and match how server_py is normalized elsewhere.
    raw = raw.replace("{{PYTHON_PATH}}", python_path.replace("\\", "/"))
    raw = raw.replace("{{CLAUDE_KB_HOME}}", str(KB_HOME).replace("\\", "/"))
    return json.loads(raw)


# --------------------------------------------------------------------------- #
# MCP server registration (via the claude CLI only)
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _mcp_status_for(claude: str, name: str, python_path: str, server_py: str) -> str:
    """Return one of: 'absent', 'matches', 'mismatch' for one server key."""
    try:
        p = _run([claude, "mcp", "get", name], timeout=30)
    except Exception:
        return "absent"
    if p.returncode != 0:
        return "absent"
    out = p.stdout or ""
    # `claude mcp get` prints the Command + Args lines; if both our paths show
    # up the existing registration already points where we want.
    return "matches" if (python_path in out and server_py in out) else "mismatch"


def mcp_statuses(claude: str, python_path: str, server_py: str) -> dict[str, str]:
    return {
        name: _mcp_status_for(claude, name, python_path, server_py)
        for name in ALL_SERVER_NAMES
    }


def matching_mcp_server(statuses: dict[str, str]) -> str | None:
    if statuses.get(SERVER_NAME) == "matches":
        return SERVER_NAME
    for name in LEGACY_SERVER_NAMES:
        if statuses.get(name) == "matches":
            return name
    return None


def mcp_status(claude: str, python_path: str, server_py: str) -> str:
    """Return one of: 'absent', 'matches', 'legacy_matches', 'mismatch'."""
    statuses = mcp_statuses(claude, python_path, server_py)
    if statuses.get(SERVER_NAME) == "matches":
        return "matches"
    if any(statuses.get(name) == "matches" for name in LEGACY_SERVER_NAMES):
        return "legacy_matches"
    if any(value == "mismatch" for value in statuses.values()):
        return "mismatch"
    return "absent"


def register_mcp(claude: str, python_path: str, server_py: str,
                 dry_run: bool) -> tuple[str, str]:
    """Register (or re-register) the server. Returns (level, message)."""
    statuses = mcp_statuses(claude, python_path, server_py)
    match = matching_mcp_server(statuses)
    add_cmd = [claude, "mcp", "add", SERVER_NAME, "--scope", "user",
               "--", python_path, server_py]
    if match == SERVER_NAME:
        legacy_matches = [n for n in LEGACY_SERVER_NAMES if statuses.get(n) == "matches"]
        extra = (f"; legacy alias still present: {', '.join(legacy_matches)}"
                 if legacy_matches else "")
        return "OK", f"already registered as {SERVER_NAME!r} (user scope) -> {python_path} {server_py}{extra}"
    if match:
        return "OK", (f"legacy MCP registration {match!r} still supported -> "
                      f"{python_path} {server_py}; fresh installs use {SERVER_NAME!r}")
    if dry_run:
        verb = "re-register" if any(v == "mismatch" for v in statuses.values()) else "register"
        return "DRY", f"would {verb}: {' '.join(add_cmd)}"
    for name, status in statuses.items():
        if status == "mismatch":
            _run([claude, "mcp", "remove", name, "-s", "user"], timeout=30)
    p = _run(add_cmd, timeout=60)
    if p.returncode != 0:
        return "FAIL", (f"`claude mcp add` failed (rc={p.returncode}): "
                        + (p.stderr or p.stdout or "").strip()[-300:])
    return "OK", f"registered (user scope) -> {python_path} {server_py}"


# --------------------------------------------------------------------------- #
# KB-dir pin (id=1556): one fixed KB, never derived from cwd
# --------------------------------------------------------------------------- #
def _has_legacy_project_dbs() -> bool:
    """True if the old per-cwd layout already holds at least one project KB."""
    if not PROJECTS_DIR.is_dir():
        return False
    return any(p.is_file() for p in PROJECTS_DIR.glob("*/kb.db"))


def _read_pin() -> str | None:
    """The kb_dir recorded in kb_location.json, or None if absent/malformed."""
    try:
        data = json.loads(KB_LOCATION_PATH.read_text(encoding="utf-8"))
        kb_dir = (data or {}).get("kb_dir")
        return kb_dir if isinstance(kb_dir, str) and kb_dir.strip() else None
    except (OSError, ValueError):
        return None


def pin_kb_dir(kb_dir_override: str | None, dry_run: bool) -> tuple[str, str]:
    """Write kb_location.json pinning the single KB directory (id=1556).

    Idempotent: an existing pin is never overwritten (re-running install must
    not relocate a user's KB). With no pin yet:
      * ``--kb-dir`` wins;
      * a FRESH install (no legacy per-cwd KBs) defaults to ``<KB_HOME>/store``;
      * an install that already has per-cwd KBs and no ``--kb-dir`` is LEFT in
        legacy mode — writing a default would silently orphan existing
        knowledge — with guidance to pin explicitly. This is the forward-only
        migration stance of id=1556 (new installs pay nothing; existing
        multi-DB users choose their one KB once, by hand).
    """
    existing = _read_pin()
    if existing is not None:
        return "OK", f"already pinned -> {existing} (left unchanged)"
    if kb_dir_override:
        target = Path(_abs(kb_dir_override))
    elif _has_legacy_project_dbs():
        return "WARN", (
            "existing per-cwd KBs found under projects/ and no --kb-dir given - "
            "leaving this install in LEGACY per-cwd mode (the wrong-DB bug class "
            "stays live; id=1556). Re-run with --kb-dir <path> to pin the one KB "
            "(e.g. the projects/<dir> you want to keep)."
        )
    else:
        target = DEFAULT_STORE_DIR
    target_str = str(target).replace("\\", "/")
    if dry_run:
        return "DRY", f"would pin KB dir -> {target_str} (write {KB_LOCATION_PATH.name})"
    target.mkdir(parents=True, exist_ok=True)
    KB_LOCATION_PATH.write_text(
        json.dumps({"kb_dir": target_str}, indent=2) + "\n", encoding="utf-8"
    )
    return "OK", f"pinned KB dir -> {target_str}"


# --------------------------------------------------------------------------- #
# settings.json merge (hooks + permission + dead-block removal)
# --------------------------------------------------------------------------- #
def _is_latch_hook_entry(entry: dict) -> bool:
    for h in entry.get("hooks", []):
        if LATCH_HOOK_MARKER in (h.get("command") or ""):
            return True
    return False


def merge_settings(snippet: dict) -> tuple[dict, list[str]]:
    """Return (new_settings, change_log) without writing to disk."""
    settings: dict = {}
    if SETTINGS_PATH.exists():
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SystemExit(f"error: {SETTINGS_PATH} is not valid JSON ({e}); "
                             "fix it by hand before running the installer.")
    changes: list[str] = []

    # 1. hooks — replace latch-owned entries, preserve everyone else's.
    hooks = settings.setdefault("hooks", {})
    for event in MANAGED_EVENTS:
        desired = snippet.get("hooks", {}).get(event, [])
        existing = hooks.get(event, [])
        kept = [e for e in existing if not _is_latch_hook_entry(e)]
        new_list = kept + desired
        if new_list != existing:
            changes.append(f"hooks.{event}: set latch hook ({len(kept)} non-latch entr"
                           f"{'y' if len(kept) == 1 else 'ies'} preserved)")
        hooks[event] = new_list

    # 2. permission rule — union-add.
    perms = settings.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    for rule in snippet.get("permissions", {}).get("allow", []):
        if rule not in allow:
            allow.append(rule)
            changes.append(f"permissions.allow += {rule!r}")

    # 3. remove dead latch-owned mcpServers blocks (Claude Code never read them).
    ms = settings.get("mcpServers")
    if isinstance(ms, dict):
        removed = [name for name in ALL_SERVER_NAMES if name in ms]
        for name in removed:
            del ms[name]
        if removed:
            names = ", ".join(f"mcpServers.{name}" for name in removed)
            changes.append(f"removed dead {names} block"
                           f"{'s' if len(removed) != 1 else ''} "
                           "(Claude Code reads MCP servers from the claude mcp registry, "
                           "not settings.json)")
        if not ms:
            del settings["mcpServers"]

    return settings, changes


def write_settings(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS_PATH.exists():
        backup = SETTINGS_PATH.with_suffix(SETTINGS_PATH.suffix + ".latchbak")
        backup.write_text(SETTINGS_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Slash-command install (commands/*.md -> ~/.claude/commands, <KB_HOME> resolved)
# --------------------------------------------------------------------------- #
def _resolved_kb_home() -> str:
    return str(KB_HOME).replace("\\", "/")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _is_latch_command_body(body: str) -> bool:
    normalized = body.replace("\\", "/")
    return (
        COMMAND_PLACEHOLDER in body
        or _resolved_kb_home() in normalized
        or any(marker in normalized for marker in LATCH_COMMAND_MARKERS)
    )


def _write_command(src: Path, dest: Path) -> None:
    dest.write_text(_command_content(src), encoding="utf-8")


def _command_content(src: Path) -> str:
    return src.read_text(encoding="utf-8").replace(COMMAND_PLACEHOLDER, _resolved_kb_home())


def _legacy_alias_content(legacy_name: str, primary: Path) -> str:
    content = _command_content(primary)
    if legacy_name == "kb-gate.md":
        content = content.replace("/bin/run_latch_gate.sh", "/bin/run_kb_gate.sh")
    return content


def _command_change_summary(changes: list[str]) -> str:
    installed = sum(1 for c in changes if c.startswith("installed "))
    updated = sum(1 for c in changes if c.startswith("updated legacy alias "))
    removed = sum(1 for c in changes if c.startswith("removed stale legacy command "))
    skipped = sum(1 for c in changes if c.startswith("skipped "))
    return (
        f"{installed} installed, {updated} legacy alias(es) updated, "
        f"{removed} stale command(s) removed, {skipped} user-owned file(s) skipped"
    )


def install_commands(dry_run: bool) -> tuple[str, list[str]]:
    """Copy latch's slash commands into Claude Code's commands dir.

    Claude Code only discovers commands under ``~/.claude/commands/`` (or a
    project ``.claude/commands/``) — it does NOT scan this repo's ``commands/``
    folder. So the source must be copied there, with the ``<KB_HOME>``
    placeholder resolved to this clone's path. Mirrors
    ``bin/install_commands.{sh,ps1}`` (kept for commands-only re-installs) so the
    one engine install also wires the commands — without this step ``/latch-compact``
    et al. error ``Unknown skill`` even though the engine + MCP are fully wired
    (the gap that bit the 2026-06-07 Mac install, id=1468 #1). Overwrite-always,
    matching the shell installers. Honors ``CLAUDE_COMMANDS_DIR`` via
    ``COMMANDS_DEST``.

    Returns ``(level, changes)``: 'OK' on success, 'WARN' if there is nothing to
    install (no ``commands/`` dir or no ``.md`` files).
    """
    if not COMMANDS_SRC.is_dir():
        return "WARN", [f"no commands/ directory at {COMMANDS_SRC} — skipped"]
    md_files = sorted(COMMANDS_SRC.glob("*.md"))
    if not md_files:
        return "WARN", [f"no command files in {COMMANDS_SRC} — skipped"]
    if not dry_run:
        COMMANDS_DEST.mkdir(parents=True, exist_ok=True)
    changes: list[str] = []
    for f in md_files:
        if dry_run:
            changes.append(f"would install {f.name}")
            continue
        _write_command(f, COMMANDS_DEST / f.name)
        changes.append(f"installed {f.name}")
    for legacy_name, primary_name in LEGACY_COMMAND_ALIASES.items():
        legacy = COMMANDS_DEST / legacy_name
        primary = COMMANDS_SRC / primary_name
        if not legacy.exists() or not primary.exists():
            continue
        body = _read_text(legacy)
        if not _is_latch_command_body(body):
            changes.append(f"skipped legacy alias {legacy_name} (looks user-owned)")
            continue
        if dry_run:
            changes.append(f"would update legacy alias {legacy_name} -> {primary_name}")
            continue
        legacy.write_text(_legacy_alias_content(legacy_name, primary), encoding="utf-8")
        changes.append(f"updated legacy alias {legacy_name} -> {primary_name}")
    for stale_name in STALE_LEGACY_COMMANDS:
        stale = COMMANDS_DEST / stale_name
        if not stale.exists():
            continue
        body = _read_text(stale)
        if not _is_latch_command_body(body):
            changes.append(f"skipped stale legacy command {stale_name} (looks user-owned)")
            continue
        if dry_run:
            changes.append(f"would remove stale legacy command {stale_name}")
            continue
        stale.unlink()
        changes.append(f"removed stale legacy command {stale_name}")
    return "OK", changes


def commands_status() -> tuple[bool, str]:
    """For --check: are all source commands present in COMMANDS_DEST with the
    ``<KB_HOME>`` placeholder resolved? Returns ``(ok, label)``."""
    if not COMMANDS_SRC.is_dir():
        return True, "slash commands: no commands/ source (nothing to install)"
    expected = sorted(p.name for p in COMMANDS_SRC.glob("*.md"))
    if not expected:
        return True, "slash commands: no command files to install"
    missing = [n for n in expected if not (COMMANDS_DEST / n).exists()]
    if missing:
        head = ", ".join(missing[:3]) + ("..." if len(missing) > 3 else "")
        return False, (f"slash commands: {len(missing)}/{len(expected)} not installed in "
                       f"{COMMANDS_DEST} (e.g. {head})")
    unresolved = [n for n in expected
                  if COMMAND_PLACEHOLDER in (COMMANDS_DEST / n).read_text(encoding="utf-8")]
    unresolved.extend(
        n for n in LEGACY_COMMAND_ALIASES
        if (COMMANDS_DEST / n).is_file()
        and _is_latch_command_body(_read_text(COMMANDS_DEST / n))
        and COMMAND_PLACEHOLDER in _read_text(COMMANDS_DEST / n)
    )
    if unresolved:
        head = ", ".join(unresolved[:3]) + ("..." if len(unresolved) > 3 else "")
        return False, (f"slash commands: {len(unresolved)} still contain a literal "
                       f"{COMMAND_PLACEHOLDER} placeholder (e.g. {head})")
    stale = [
        n for n in STALE_LEGACY_COMMANDS
        if (COMMANDS_DEST / n).is_file()
        and _is_latch_command_body(_read_text(COMMANDS_DEST / n))
    ]
    if stale:
        head = ", ".join(stale[:3]) + ("..." if len(stale) > 3 else "")
        return False, (f"slash commands: stale legacy latch command(s) still installed "
                       f"in {COMMANDS_DEST} (e.g. {head}); re-run install to prune")
    return True, f"slash commands: {len(expected)} installed in {COMMANDS_DEST}"


# --------------------------------------------------------------------------- #
# --check (verify-only)
# --------------------------------------------------------------------------- #
def check(python_path: str, server_py: str) -> int:
    claude = find_claude()
    rows: list[tuple[bool, str]] = []

    if claude:
        statuses = mcp_statuses(claude, python_path, server_py)
        match = matching_mcp_server(statuses)
        if match == SERVER_NAME:
            label = f"MCP server '{SERVER_NAME}' registered with Claude Code"
        elif match:
            label = (f"legacy MCP server '{match}' registered with Claude Code "
                     f"(supported alias; fresh installs use '{SERVER_NAME}')")
        else:
            label = (f"MCP server '{SERVER_NAME}' or legacy alias "
                     f"{LEGACY_SERVER_NAMES!r} registered with Claude Code")
        rows.append((match is not None, label))
    else:
        match = None
        rows.append((False, "claude CLI on PATH (required to register/verify MCP)"))

    settings: dict = {}
    if SETTINGS_PATH.exists():
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"FAIL - {SETTINGS_PATH} is not valid JSON")
            return 1
    allow = settings.get("permissions", {}).get("allow", [])
    active_rule = f"mcp__{match}" if match else PERMISSION_RULE
    rows.append((active_rule in allow,
                 f"permissions.allow contains {active_rule!r}"
                 + (" for the active legacy MCP alias" if match in LEGACY_SERVER_NAMES else "")))
    hooks = settings.get("hooks", {})
    for event in MANAGED_EVENTS:
        ok = any(_is_latch_hook_entry(e) for e in hooks.get(event, []))
        rows.append((ok, f"hooks.{event} has a latch hook"))
    ms = settings.get("mcpServers")
    dead = [name for name in ALL_SERVER_NAMES if isinstance(ms, dict) and name in ms]
    rows.append((not dead,
                 "no dead latch-owned mcpServers block in settings.json"
                 if not dead else f"dead mcpServers block(s) present: {', '.join(dead)}"))
    rows.append(commands_status())

    # KB-dir pin (id=1556): env var wins; else kb_location.json; a legacy
    # per-cwd install with no pin keeps the wrong-DB bug class live → flag it.
    env_pin = os.environ.get("LATCH_KB_DIR") or os.environ.get("CLAUDE_KB_DIR")
    if env_pin and env_pin.strip():
        src = "LATCH_KB_DIR" if os.environ.get("LATCH_KB_DIR") else "CLAUDE_KB_DIR"
        rows.append((True, f"KB pinned via {src} env -> {env_pin.strip()}"))
    else:
        pin = _read_pin()
        if pin:
            rows.append((True, f"KB pinned -> {pin} (kb_location.json)"))
        elif _has_legacy_project_dbs():
            rows.append((False, "KB NOT pinned: legacy per-cwd mode with existing project "
                                "KBs (wrong-DB bug class live - pin with --kb-dir; id=1556)"))
        else:
            rows.append((True, "KB not pinned yet (fresh install — pin defaults to store/)"))

    failed = 0
    for ok, label in rows:
        print(f"  [{'OK' if ok else 'XX'}] {label}")
        failed += 0 if ok else 1
    print()
    if failed:
        print(f"FAILED - {failed} item(s) missing. Run: bash bin/install_engine.sh")
        return 1
    print("OK - engine is fully wired into Claude Code.")
    return 0


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="latch engine installer (MCP + hooks + permissions).")
    ap.add_argument("--python", help="interpreter to register for the server + hooks "
                                      "(default: $LATCH_PYTHON, $CLAUDE_KB_PYTHON, "
                                      "repo .venv, or this python)")
    ap.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
    ap.add_argument("--check", action="store_true", help="verify wiring only; exit 1 if incomplete")
    ap.add_argument("--kb-dir", help="pin the single KB directory (id=1556); written to "
                                     "kb_location.json so the DB is never selected from cwd. "
                                     "Default on a fresh install: <KB_HOME>/store. An existing "
                                     "per-cwd install is left in legacy mode unless this is given.")
    ap.add_argument("--no-seed-prompt", action="store_true",
                    help="do not offer the post-install cold-start seed prompt")
    ap.add_argument("--suppress-seed-output", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    python_path = resolve_python(args.python)
    server_py = str((KB_HOME / "src" / "mcp_server.py")).replace("\\", "/")

    if args.check:
        return check(python_path, server_py)

    if not SNIPPET_PATH.exists():
        print(f"error: missing {SNIPPET_PATH}", file=sys.stderr)
        return 2

    print(f"\nlatch engine installer")
    print(f"  KB_HOME     : {KB_HOME}")
    print(f"  interpreter : {python_path}")
    print(f"  settings    : {SETTINGS_PATH}")
    print(f"  mode        : {'DRY-RUN (no writes)' if args.dry_run else 'apply'}\n")

    # --- 1. MCP registration -------------------------------------------------
    claude = find_claude()
    preflight_errors = apply_preflight_errors(claude)
    if preflight_errors and not args.dry_run:
        print("  [FAIL] preflight:")
        for msg in preflight_errors:
            print(f"         {msg}")
        print("\nNo changes written.")
        return 2

    if not claude:
        print("  [WARN] claude CLI not found on PATH — cannot register the MCP server.")
        print("         Install Claude Code, then re-run, or register manually:")
        print(f"           claude mcp add {SERVER_NAME} --scope user -- "
              f"{python_path} {server_py}")
    else:
        level, msg = register_mcp(claude, python_path, server_py, args.dry_run)
        print(f"  [{level:4}] MCP server: {msg}")
        if level == "FAIL":
            return 1

    # --- 2 & 3. settings.json (hooks + permission + dead-block removal) -------
    snippet = load_snippet(python_path)
    new_settings, changes = merge_settings(snippet)
    if not changes:
        print("  [OK  ] settings.json: hooks + permission already in place; nothing to change")
    elif args.dry_run:
        print("  [DRY ] settings.json would change:")
        for c in changes:
            print(f"           - {c}")
    else:
        write_settings(new_settings)
        print(f"  [OK  ] settings.json updated (backup: {SETTINGS_PATH.name}.latchbak):")
        for c in changes:
            print(f"           - {c}")

    # --- 4. slash commands ---------------------------------------------------
    cmd_level, cmd_changes = install_commands(args.dry_run)
    if cmd_level == "OK":
        if args.dry_run:
            print(f"  [DRY ] slash commands would install -> {COMMANDS_DEST}:")
            for c in cmd_changes:
                print(f"           - {c}")
        else:
            print(f"  [OK  ] slash commands updated -> {COMMANDS_DEST} "
                  f"({_command_change_summary(cmd_changes)})")
    else:
        print(f"  [WARN] {cmd_changes[0]}")

    # --- 5. pin the KB directory (id=1556) -----------------------------------
    pin_level, pin_msg = pin_kb_dir(args.kb_dir, args.dry_run)
    print(f"  [{pin_level:4}] KB dir: {pin_msg}")

    # --- next steps ----------------------------------------------------------
    print()
    if args.dry_run:
        print("Dry run only — re-run without --dry-run to apply.\n")
    else:
        print(restart_next_step_message())
        print("Verify env + wiring any time with: bash bin/latch_doctor.sh")
        if not args.suppress_seed_output:
            if args.no_seed_prompt:
                print()
                print(seed_next_step_message())
                print()
            else:
                offer_seed_after_install(python_path=python_path, source="auto")
    return 0


if __name__ == "__main__":
    sys.exit(main())
