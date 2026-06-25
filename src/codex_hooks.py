#!/usr/bin/env python3
"""Codex hooks.json installer helpers.

This module manages only latch-owned Codex hooks. It preserves unrelated hooks,
removes older latch-owned Codex Stop/SessionStart entries, and installs the
current SessionStart brief hook. It does not write Claude Code settings.
"""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

BEGINNER_HOOK_EVENT = "SessionStart"
OWNED_HOOK_BASENAMES = {
    "codex_session_start.py",
    "session_start.py",
    "stop.py",
    "session_end.py",
    "user_prompt_submit.py",
}


def quote_command_arg(value: str) -> str:
    if os.name == "nt":
        return '"' + value.replace('"', '\\"') + '"' if any(c.isspace() for c in value) else value
    return shlex.quote(value)


def hook_command(python_path: str, hook_py: str) -> str:
    return f"{quote_command_arg(python_path)} {quote_command_arg(hook_py)}"


def render_session_start_entry(python_path: str, hook_py: str) -> dict[str, Any]:
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": hook_command(python_path, hook_py),
            }
        ],
    }


def _is_owned_command(command: str) -> bool:
    normalized = command.replace("\\", "/")
    return any(f"/src/hooks/{name}" in normalized for name in OWNED_HOOK_BASENAMES)


def _is_owned_hook(hook: Any) -> bool:
    return isinstance(hook, dict) and _is_owned_command(str(hook.get("command", "")))


def _load_hooks_json(existing: str) -> dict[str, Any]:
    if not existing.strip():
        return {}
    obj = json.loads(existing)
    return obj if isinstance(obj, dict) else {}


def merge_hooks(existing: str, python_path: str, hook_py: str) -> tuple[str, list[str]]:
    obj = _load_hooks_json(existing)
    hooks = obj.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        obj["hooks"] = hooks

    changes: list[str] = []
    removed = 0

    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            group_hooks = group.get("hooks")
            if not isinstance(group_hooks, list):
                new_groups.append(group)
                continue
            filtered = [h for h in group_hooks if not _is_owned_hook(h)]
            removed += len(group_hooks) - len(filtered)
            if filtered:
                new_group = dict(group)
                new_group["hooks"] = filtered
                new_groups.append(new_group)
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]

    desired = render_session_start_entry(python_path, hook_py)
    hooks.setdefault(BEGINNER_HOOK_EVENT, [])
    hooks[BEGINNER_HOOK_EVENT].insert(0, desired)
    if removed:
        changes.append(f"removed {removed} stale latch-owned Codex hook(s)")
    changes.append("installed latch Codex SessionStart brief hook")

    new = json.dumps(obj, indent=2, sort_keys=False) + "\n"
    if new == existing:
        return new, []
    return new, changes


def write_hooks(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_suffix(path.suffix + ".latchbak").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    path.write_text(content, encoding="utf-8")


def hooks_status(path: Path, python_path: str, hook_py: str) -> tuple[bool, str]:
    if not path.exists():
        return False, f"Codex hooks missing: {path}"
    try:
        current = path.read_text(encoding="utf-8")
        desired, changes = merge_hooks(current, python_path, hook_py)
    except (OSError, json.JSONDecodeError) as e:
        return False, f"Codex hooks unreadable: {e}"
    if desired == current and not changes:
        return True, f"Codex SessionStart hook installed in {path}"
    return False, f"Codex SessionStart hook missing or drifted in {path}"


def trust_state_hint(config_path: Path, hooks_path: Path) -> tuple[bool, str]:
    """Best-effort trust-state check.

    Codex owns hook trust hashes. latch can verify that a trust-state stanza is
    present for the first SessionStart hook, but does not recompute/forge the
    hash; Codex may still ask the user to trust a changed hook.
    """
    if not config_path.exists():
        return False, f"Codex config missing: {config_path}"
    text = config_path.read_text(encoding="utf-8", errors="replace")
    key = f'[hooks.state."{hooks_path}:session_start:0:0"]'
    if key in text:
        return True, "trust state present (hash owned by Codex; not recomputed by latch)"
    return False, "trust state missing; Codex may prompt to trust the SessionStart hook"
