#!/usr/bin/env python3
"""latch Codex installer — wire MCP, skills, hooks, and AGENTS.md.

This is intentionally separate from ``install_engine.py``.  Claude Code remains
the production baseline and keeps using ``claude mcp add``, ``~/.claude``
settings, hooks, permissions, and slash commands. Codex reads MCP servers from
``config.toml``, user skills from ``~/.agents/skills``, and project instructions
from ``AGENTS.md``, so this installer owns only those Codex surfaces.
"""
from __future__ import annotations

import argparse
import copy
import os
import re
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path

import agents_md_sync
import codex_hooks
import install_engine
import versioning

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
DEFAULT_SKILLS_DIR = Path.home() / ".agents" / "skills"
CODEX_SKILLS_SRC = KB_HOME / ".agents" / "skills"
CODEX_SKILL_MARKER = "<!-- latch-codex-skill: managed -->"
CODEX_SKILL_NAMES = (
    "source-command-latch-budget-approve",
    "source-command-latch-compact",
    "source-command-latch-decay",
    "source-command-latch-gate",
    "source-command-latch-gate-report",
    "source-command-latch-heal",
    "source-command-latch-pm",
    "source-command-latch-review",
    "source-command-latch-tree",
    "source-command-unlatch",
)

_TABLE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
_ARRAY_TABLE_RE = re.compile(r"^\s*\[\[([^\]]+)\]\]\s*(?:#.*)?$")
_FEATURE_HOOK_RE = re.compile(r"^(\s*)(hooks|codex_hooks)\s*=(.*)$")
_OWNED_TABLES = tuple(f"mcp_servers.{name}" for name in ALL_SERVER_NAMES)


class CodexSkillCollisionError(RuntimeError):
    pass


class CodexConfigMergeError(RuntimeError):
    pass


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _table_header(line: str) -> tuple[str, bool] | None:
    """Return a raw TOML table name and whether it is an array-of-tables."""
    match = _ARRAY_TABLE_RE.match(line)
    if match:
        return match.group(1).strip(), True
    match = _TABLE_RE.match(line)
    if match:
        return match.group(1).strip(), False
    return None


def _is_features_table_name(raw_name: str) -> bool:
    return raw_name in {"features", '"features"', "'features'"}


def _contains_multiline_toml_string(text: str) -> bool:
    """Detect real TOML multiline string delimiters, ignoring comments/strings."""
    index = 0
    while index < len(text):
        char = text[index]
        if char == "#":
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith('"""', index) or text.startswith("'''", index):
            return True
        if char not in {'"', "'"}:
            index += 1
            continue

        quote = char
        index += 1
        while index < len(text):
            char = text[index]
            if quote == '"' and char == "\\":
                index += 2
                continue
            index += 1
            if char == quote or char == "\n":
                break
    return False


def _parse_config(text: str, *, stage: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CodexConfigMergeError(
            f"Codex config is invalid TOML {stage}: {exc}; no changes were written"
        ) from exc


def _validate_supported_config_shape(
    text: str, parsed: dict, *, manage_hooks: bool = True
) -> None:
    """Reject valid TOML forms the comment-preserving line merge cannot edit."""
    if _contains_multiline_toml_string(text):
        raise CodexConfigMergeError(
            "Codex config contains a multiline TOML string, which this "
            "comment-preserving installer cannot safely rewrite; no changes were written"
        )

    if not manage_hooks:
        return

    features_headers = [
        header
        for line in text.splitlines()
        if (header := _table_header(line)) is not None
        and not header[1]
        and _is_features_table_name(header[0])
    ]
    features = parsed.get("features")
    if features is None:
        return
    if not isinstance(features, dict) or len(features_headers) != 1:
        raise CodexConfigMergeError(
            "Codex config uses an unsupported features representation. Convert "
            "inline/dotted features to a standalone [features] table and rerun; "
            "no changes were written"
        )


def _unowned_config_projection(parsed: dict, *, manage_hooks: bool = True) -> dict:
    """Remove only fields this installer owns for before/after comparison."""
    projected = copy.deepcopy(parsed)
    features = projected.get("features")
    if features is not None and not isinstance(features, dict):
        raise CodexConfigMergeError(
            "Codex config features value cannot be safely merged; no changes were written"
        )
    if isinstance(features, dict) and manage_hooks:
        features.pop("hooks", None)
        features.pop("codex_hooks", None)
        if not features:
            projected.pop("features", None)

    servers = projected.get("mcp_servers")
    if servers is not None and not isinstance(servers, dict):
        raise CodexConfigMergeError(
            "Codex config mcp_servers value cannot be safely merged; "
            "no changes were written"
        )
    if isinstance(servers, dict):
        for name in ALL_SERVER_NAMES:
            servers.pop(name, None)
        if not servers:
            projected.pop("mcp_servers", None)
    return projected


def _validate_merged_config(
    text: str, *, before: dict, mcp_block: str, manage_hooks: bool = True
) -> None:
    parsed = _parse_config(text, stage="after the proposed merge")
    features = parsed.get("features")
    if manage_hooks and (
        not isinstance(features, dict) or features.get("hooks") is not True
    ):
        raise CodexConfigMergeError(
            "Codex config merge did not produce [features] hooks = true; "
            "no changes were written"
        )
    if manage_hooks and isinstance(features, dict) and "codex_hooks" in features:
        raise CodexConfigMergeError(
            "Codex config merge left the deprecated features.codex_hooks alias; "
            "no changes were written"
        )
    rendered = _parse_config(mcp_block, stage="while rendering the managed MCP block")
    servers = parsed.get("mcp_servers")
    if (
        not isinstance(servers, dict)
        or servers.get(SERVER_NAME) != rendered["mcp_servers"][SERVER_NAME]
    ):
        raise CodexConfigMergeError(
            "Codex config merge did not produce the canonical latch MCP block; "
            "no changes were written"
        )
    legacy_names = [name for name in LEGACY_SERVER_NAMES if name in servers]
    if legacy_names:
        raise CodexConfigMergeError(
            "Codex config merge left deprecated latch MCP server name(s): "
            f"{', '.join(legacy_names)}; no changes were written"
        )
    if _unowned_config_projection(
        parsed, manage_hooks=manage_hooks
    ) != _unowned_config_projection(before, manage_hooks=manage_hooks):
        raise CodexConfigMergeError(
            "Codex config merge could not prove preservation of unrelated TOML "
            "settings; no changes were written"
        )


def render_mcp_block(python_path: str, server_py: str) -> str:
    py = python_path.replace("\\", "/")
    server = server_py.replace("\\", "/")
    return "\n".join([
        BEGIN_MARK,
        f"[mcp_servers.{SERVER_NAME}]",
        f"command = {_toml_string(py)}",
        f"args = [{_toml_string(server)}]",
        "startup_timeout_sec = 120",
        "required = true",
        f"tool_timeout_sec = {CODEX_TOOL_TIMEOUT_SEC}",
        'default_tools_approval_mode = "approve"',
        f"[mcp_servers.{SERVER_NAME}.env]",
        'LATCH_MODEL_BACKEND = "codex"',
        'LATCH_GATE_BACKEND = "codex"',
        'LATCH_ADAPTER = "codex"',
        'LATCH_TOOL_SURFACE = "latch"',
        f'LATCH_WIRING_VERSION = "{versioning.WIRING_VERSION}"',
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
        header = _table_header(line)
        if header:
            name, is_array = header
            if not is_array and _is_owned_table(name):
                removing = True
                changed = True
                continue
            removing = False
        if not removing:
            out.append(line)
    return "\n".join(out).rstrip("\n"), changed


def _feature_hook_line(line: str) -> tuple[str, str, str] | None:
    """Return indentation, key, and value/comment text for a hook feature line."""
    match = _FEATURE_HOOK_RE.match(line)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def _inline_comment(value: str) -> str:
    marker = value.find("#")
    return value[marker:].strip() if marker >= 0 else ""


def _merge_hooks_feature(text: str) -> tuple[str, bool]:
    """Canonicalize Codex lifecycle activation as ``[features] hooks = true``.

    The former ``codex_hooks`` feature name is removed.  Unrelated feature
    settings, tables, and comments stay byte-for-byte intact apart from the
    surrounding newline normalization already performed by ``merge_config``.
    """
    lines = text.splitlines()
    features_start: int | None = None
    for index, line in enumerate(lines):
        header = _table_header(line)
        if header and not header[1] and _is_features_table_name(header[0]):
            features_start = index
            break

    if features_start is None:
        addition = ["[features]", "hooks = true"]
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(addition)
        return "\n".join(lines).rstrip("\n"), True

    features_end = len(lines)
    for index in range(features_start + 1, len(lines)):
        if _table_header(lines[index]):
            features_end = index
            break

    flag_rows: list[tuple[int, str, str, str]] = []
    for index in range(features_start + 1, features_end):
        parsed = _feature_hook_line(lines[index])
        if parsed:
            indent, key, value = parsed
            flag_rows.append((index, indent, key, value))

    canonical = next((row for row in flag_rows if row[2] == "hooks"), None)
    selected = canonical or (flag_rows[0] if flag_rows else None)
    changed = False
    if selected is None:
        insert_at = features_end
        while insert_at > features_start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, "hooks = true")
        return "\n".join(lines).rstrip("\n"), True

    selected_index, indent, _key, value = selected
    comment = _inline_comment(value)
    replacement = f"{indent}hooks = true" + (f" {comment}" if comment else "")
    if lines[selected_index] != replacement:
        lines[selected_index] = replacement
        changed = True

    # Remove deprecated aliases and duplicate canonical keys. Preserve any
    # inline comment as a standalone comment at the same location.
    for index, row_indent, _row_key, row_value in reversed(flag_rows):
        if index == selected_index:
            continue
        comment = _inline_comment(row_value)
        if comment:
            lines[index] = f"{row_indent}{comment}"
        else:
            del lines[index]
        changed = True

    return "\n".join(lines).rstrip("\n"), changed


def _canonical_hooks_feature(text: str) -> tuple[bool, str]:
    """Whether config contains exactly the supported lifecycle feature flag."""
    try:
        parsed = _parse_config(text, stage="before status inspection")
        _validate_supported_config_shape(text, parsed)
    except CodexConfigMergeError as exc:
        return False, str(exc)
    features = parsed.get("features")
    if not isinstance(features, dict):
        return False, (
            "Codex lifecycle hooks are missing; run bin/install_codex.sh to set "
            "[features] hooks = true"
        )
    if "codex_hooks" in features:
        return False, (
            "Codex lifecycle hooks use deprecated features.codex_hooks; "
            "run bin/install_codex.sh to set [features] hooks = true"
        )
    if features.get("hooks") is not True:
        return False, (
            "Codex lifecycle hooks are disabled, missing, or invalid; "
            "run bin/install_codex.sh to set [features] hooks = true"
        )
    return True, "Codex lifecycle hooks enabled ([features] hooks = true)"


def merge_config(
    existing: str,
    python_path: str,
    server_py: str,
    *,
    enable_hooks: bool = True,
) -> tuple[str, list[str]]:
    parsed = _parse_config(existing, stage="before the proposed merge")
    _validate_supported_config_shape(existing, parsed, manage_hooks=enable_hooks)
    changes: list[str] = []
    text, removed_block = _strip_managed_block(existing)
    if removed_block:
        changes.append("replaced existing latch-managed Codex MCP block")
    text, removed_tables = _strip_existing_server_tables(text)
    if removed_tables:
        changes.append("replaced existing latch-owned MCP server table")
    if enable_hooks:
        text, feature_changed = _merge_hooks_feature(text)
        if feature_changed:
            changes.append("enabled Codex lifecycle hooks feature")
    block = render_mcp_block(python_path, server_py)
    new = (text.rstrip("\n") + "\n\n" + block + "\n") if text.strip() else block + "\n"
    _validate_merged_config(
        new, before=parsed, mcp_block=block, manage_hooks=enable_hooks
    )
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
    hooks_enabled, hooks_detail = _canonical_hooks_feature(current)
    if not hooks_enabled:
        return False, hooks_detail
    try:
        desired, changes = merge_config(current, python_path, server_py)
    except CodexConfigMergeError as exc:
        return False, f"Codex config cannot be safely merged: {exc}"
    if desired == current and not changes:
        return True, f"Codex MCP block installed in {path}; {hooks_detail}"
    normalized_py = python_path.replace("\\", "/")
    normalized_server = server_py.replace("\\", "/")
    parsed = _parse_config(current, stage="before status inspection")
    servers = parsed.get("mcp_servers")
    for legacy in LEGACY_SERVER_NAMES:
        legacy_server = servers.get(legacy) if isinstance(servers, dict) else None
        if (f"[mcp_servers.{legacy}]" in current
                and normalized_py in current
                and normalized_server in current
                and isinstance(legacy_server, dict)
                and legacy_server.get("required") is True):
            return True, (f"Codex MCP block uses legacy server name {legacy!r} in {path}; "
                          f"still supported, fresh installs use {SERVER_NAME!r}; "
                          f"{hooks_detail}")
    return False, f"Codex MCP block missing or drifted in {path}"


def _raw_codex_skill(name: str) -> str:
    if name not in CODEX_SKILL_NAMES:
        raise ValueError(f"unsupported Codex skill: {name}")
    source = CODEX_SKILLS_SRC / name / "SKILL.md"
    if not source.is_file():
        raise FileNotFoundError(source)
    return source.read_text(encoding="utf-8")


def render_codex_skill(name: str) -> str:
    body = _raw_codex_skill(name).replace(
        "<KB_HOME>", str(KB_HOME).replace("\\", "/")
    )
    footer = (
        "\n\n---\n\n"
        "Latch Codex user-skill sync metadata. Re-run `bin/install_codex` to "
        "refresh this managed copy.\n"
        f"{CODEX_SKILL_MARKER}\n"
        f"<!-- latch-wiring-version: {versioning.WIRING_VERSION} -->\n"
    )
    return body.rstrip() + footer


def _desired_codex_skill_files(name: str) -> dict[Path, str]:
    source_dir = CODEX_SKILLS_SRC / name
    if not source_dir.is_dir():
        raise FileNotFoundError(source_dir)
    desired: dict[Path, str] = {}
    for source in sorted(source_dir.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(source_dir)
        desired[relative] = (
            render_codex_skill(name)
            if relative == Path("SKILL.md")
            else source.read_text(encoding="utf-8")
        )
    return desired


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _selected_skill_names(names: Iterable[str] | None) -> tuple[str, ...]:
    selected = CODEX_SKILL_NAMES if names is None else tuple(dict.fromkeys(names))
    unknown = [name for name in selected if name not in CODEX_SKILL_NAMES]
    if unknown:
        raise ValueError(f"unsupported Codex skill(s): {', '.join(unknown)}")
    return selected


def _codex_skill_collisions(skills_dir: Path, names: Iterable[str]) -> list[Path]:
    if skills_dir.is_symlink() or (skills_dir.exists() and not skills_dir.is_dir()):
        return [skills_dir]
    collisions: list[Path] = []
    for name in names:
        skill_dir = skills_dir / name
        target = skill_dir / "SKILL.md"
        if skill_dir.is_symlink() or (skill_dir.exists() and not skill_dir.is_dir()):
            collisions.append(skill_dir)
            continue
        if target.is_symlink() or (target.exists() and not target.is_file()):
            collisions.append(target)
            continue
        if not target.exists():
            if skill_dir.exists() and any(skill_dir.iterdir()):
                collisions.append(skill_dir)
            continue
        existing = _read_text(target)
        if existing not in {_raw_codex_skill(name), render_codex_skill(name)} and (
            CODEX_SKILL_MARKER not in existing
        ):
            collisions.append(target)
            continue
        for relative in _desired_codex_skill_files(name):
            destination = skill_dir / relative
            parent = destination.parent
            unsafe = destination.is_symlink() or (
                destination.exists() and not destination.is_file()
            )
            while not unsafe and parent != skill_dir:
                unsafe = parent.is_symlink() or (
                    parent.exists() and not parent.is_dir()
                )
                parent = parent.parent
            if unsafe:
                collisions.append(destination)
    return collisions


def codex_skill_collisions(
    skills_dir: Path, *, names: Iterable[str] | None = None
) -> list[Path]:
    return _codex_skill_collisions(skills_dir, _selected_skill_names(names))


def _raise_skill_collisions(collisions: list[Path]) -> None:
    if not collisions:
        return
    raise CodexSkillCollisionError(
        "refusing to overwrite user-owned Codex skill path(s): "
        + ", ".join(str(path) for path in collisions)
        + "; move or rename them, then rerun the installer"
    )


def sync_codex_skills(
    skills_dir: Path = DEFAULT_SKILLS_DIR,
    *,
    dry_run: bool = False,
    names: Iterable[str] | None = None,
) -> list[str]:
    selected = _selected_skill_names(names)
    _raise_skill_collisions(codex_skill_collisions(skills_dir, names=selected))
    changes: list[str] = []
    for name in selected:
        skill_dir = skills_dir / name
        existed = (skill_dir / "SKILL.md").exists()
        changed = False
        for relative, desired in _desired_codex_skill_files(name).items():
            target = skill_dir / relative
            existing = _read_text(target)
            if target.is_file() and existing == desired:
                continue
            changed = True
            if dry_run:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file():
                target.with_name(target.name + ".latchbak").write_text(
                    existing, encoding="utf-8"
                )
            target.write_text(desired, encoding="utf-8")
        if changed:
            changes.append(f"{'updated' if existed else 'installed'} Codex skill {name}")
    return changes


def codex_skills_status(
    skills_dir: Path = DEFAULT_SKILLS_DIR,
) -> tuple[bool, str]:
    missing: list[str] = []
    drifted: list[str] = []
    for name in CODEX_SKILL_NAMES:
        desired = _desired_codex_skill_files(name)
        skill_dir = skills_dir / name
        if any(not (skill_dir / relative).is_file() for relative in desired):
            missing.append(name)
        elif any(
            _read_text(skill_dir / relative) != body
            for relative, body in desired.items()
        ):
            drifted.append(name)
    problems: list[str] = []
    if missing:
        problems.append(
            f"missing {', '.join(missing[:3])}{'...' if len(missing) > 3 else ''}"
        )
    if drifted:
        problems.append(
            f"drifted {', '.join(drifted[:3])}{'...' if len(drifted) > 3 else ''}"
        )
    if problems:
        return False, f"Codex skills missing or drifted in {skills_dir}: " + "; ".join(problems)
    return True, f"Codex skills installed in {skills_dir} ({len(CODEX_SKILL_NAMES)} workflows)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="latch Codex installer (MCP + skills + AGENTS.md + SessionStart hook)."
    )
    ap.add_argument("--python", help="interpreter to register for the MCP server")
    ap.add_argument("--config", default=str(CONFIG_PATH),
                    help="Codex config.toml path (default: $CODEX_HOME/config.toml)")
    ap.add_argument("--hooks", default=str(HOOKS_PATH),
                    help="Codex hooks.json path (default: $CODEX_HOME/hooks.json)")
    ap.add_argument("--skills-dir", default=str(DEFAULT_SKILLS_DIR),
                    help="Codex user skills directory (default: $HOME/.agents/skills)")
    ap.add_argument("--agents-md", default="AGENTS.md",
                    help="AGENTS.md path to sync (default: ./AGENTS.md)")
    ap.add_argument("--skip-agents", action="store_true",
                    help="do not touch AGENTS.md")
    ap.add_argument("--skip-hooks", action="store_true",
                    help="do not touch hooks.json")
    ap.add_argument("--skip-skills", action="store_true",
                    help="do not install Codex user skills")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="confirm first-time AGENTS.md wiring")
    ap.add_argument("--dry-run", action="store_true", help="print what would change")
    ap.add_argument("--check", action="store_true", help="verify wiring only")
    ap.add_argument("--kb-dir", help="pin one KB directory for every Codex project; "
                                     "fresh installs otherwise use the platform data "
                                     "root outside the source checkout")
    ap.add_argument("--no-seed-prompt", action="store_true",
                    help="do not offer the post-install cold-start seed prompt")
    ap.add_argument("--suppress-seed-output", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    python_path = install_engine.resolve_python(args.python)
    server_py = str((KB_HOME / "src" / "mcp_server.py")).replace("\\", "/")
    mcp_python, mcp_server = install_engine.mcp_launch_command(python_path, server_py)
    hook_py = str((KB_HOME / "src" / "hooks" / "codex_session_start.py")).replace("\\", "/")
    config_path = Path(args.config)
    hooks_path = Path(args.hooks)
    skills_dir = Path(args.skills_dir)
    agents_path = Path(args.agents_md)

    if args.check:
        ok_config, label = config_status(config_path, mcp_python, mcp_server)
        print(f"  [{'OK' if ok_config else 'XX'}] {label}")
        ok_hooks = True
        if not args.skip_hooks:
            ok_hooks, hook_label = codex_hooks.hooks_status(hooks_path, python_path, hook_py)
            print(f"  [{'OK' if ok_hooks else 'XX'}] {hook_label}")
        ok_skills = True
        if not args.skip_skills:
            ok_skills, skill_label = codex_skills_status(skills_dir)
            print(f"  [{'OK' if ok_skills else 'XX'}] {skill_label}")
        ok_agents = True
        if not args.skip_agents:
            status = agents_md_sync.evaluate(agents_path)
            ok_agents = status == agents_md_sync.OK
            print(f"  [{'OK' if ok_agents else 'XX'}] AGENTS.md managed region: {status}")
        return 0 if ok_config and ok_hooks and ok_skills and ok_agents else 1

    if not args.skip_skills:
        try:
            _raise_skill_collisions(codex_skill_collisions(skills_dir))
        except CodexSkillCollisionError as exc:
            print(f"  [XX] {exc}")
            return 1

    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    try:
        new_config, changes = merge_config(existing, mcp_python, mcp_server)
    except CodexConfigMergeError as exc:
        print("\nlatch Codex installer\n")
        print(f"  [XX] Codex config merge refused: {exc}")
        print("\nNo Codex configuration changes were written.")
        return 2
    new_hooks = ""
    hook_changes: list[str] = []
    if not args.skip_hooks:
        existing_hooks = hooks_path.read_text(encoding="utf-8") if hooks_path.exists() else ""
        new_hooks, hook_changes = codex_hooks.merge_hooks(existing_hooks, python_path, hook_py)

    print("\nlatch Codex installer")
    print(f"  version      : {versioning.LATCH_VERSION} (wiring {versioning.WIRING_VERSION})")
    print(f"  KB_HOME    : {KB_HOME}")
    print(f"  interpreter: {python_path}")
    print(f"  config     : {config_path}")
    print(f"  hooks      : {'skipped' if args.skip_hooks else hooks_path}")
    print(f"  skills     : {'skipped' if args.skip_skills else skills_dir}")
    print(f"  AGENTS.md  : {'skipped' if args.skip_agents else agents_path}")
    print(f"  mode       : {'DRY-RUN (no writes)' if args.dry_run else 'apply'}\n")

    pin_level, pin_msg = install_engine.pin_kb_dir(args.kb_dir, args.dry_run)
    print(f"  [{pin_level:4}] KB dir: {pin_msg}")
    if pin_level in {"ERROR", "FAIL"}:
        print("\nNo Codex configuration changes were written.")
        return 2

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
            print("  [OK  ] Codex hooks already include the latch SessionStart hook")

    if not args.skip_skills:
        skill_changes = sync_codex_skills(skills_dir, dry_run=args.dry_run)
        if skill_changes:
            print(f"  [{'DRY ' if args.dry_run else 'OK  '}] Codex skills:")
            for change in skill_changes:
                print(f"          - {change}")
        else:
            print("  [OK  ] Codex user skills already have latch")

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
        print("\nDone. Codex detects skills automatically; if they do not appear, "
              "restart Codex. Start a new Codex thread so the MCP roster, "
              "SessionStart hook, and AGENTS.md instruction chain reload.\n")

    if not args.suppress_seed_output:
        if args.dry_run or args.no_seed_prompt:
            print(install_engine.seed_next_step_message(
                command=(
                    f"{KB_HOME / 'bin' / 'latch_seed.sh'} "
                    "--source codex --backend codex --apply"
                )
            ))
            print()
        elif not args.dry_run:
            install_engine.offer_seed_after_install(
                python_path=python_path,
                source="codex",
                backend="codex",
                project=Path.cwd(),
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
