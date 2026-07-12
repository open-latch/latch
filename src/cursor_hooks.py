#!/usr/bin/env python3
"""Merge-safe project ``.cursor/hooks.json`` helpers.

Only latch-owned commands are replaced or removed.  Unrelated hook events and
entries remain byte-for-byte equivalent after JSON normalization.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codex_hooks import hook_command

DEFAULT_HOOKS_PATH = Path(".cursor") / "hooks.json"
OWNED_HOOK_BASENAMES = {
    "cursor_session_start.py",
    "cursor_before_submit.py",
    "cursor_pre_tool_use.py",
    "cursor_post_tool_use.py",
}


def _load(existing: str, *, path: Path = DEFAULT_HOOKS_PATH) -> dict[str, Any]:
    if not existing.strip():
        return {}
    try:
        obj = json.loads(existing)
    except json.JSONDecodeError as e:
        raise SystemExit(f"error: {path} is not valid JSON ({e}); fix it by hand before running installer.")
    if not isinstance(obj, dict):
        raise SystemExit(f"error: {path} must contain a JSON object.")
    version = obj.get("version", 1)
    if version != 1:
        raise SystemExit(f"error: {path} has unsupported Cursor hooks version {version!r}; expected 1.")
    return obj


def _is_owned(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    command = str(entry.get("command", "")).replace("\\", "/")
    return any(f"/src/hooks/{name}" in command for name in OWNED_HOOK_BASENAMES)


def render_entries(
    python_path: str,
    session_start_py: str,
    before_submit_py: str,
    pre_tool_use_py: str,
    post_tool_use_py: str,
) -> dict[str, dict[str, Any]]:
    return {
        "sessionStart": {
            "command": hook_command(python_path, session_start_py),
            "timeout": 15,
        },
        "beforeSubmitPrompt": {
            "command": hook_command(python_path, before_submit_py),
            "failClosed": True,
            "timeout": 5,
        },
        "preToolUse": {
            "command": hook_command(python_path, pre_tool_use_py),
            "failClosed": True,
            "timeout": 5,
        },
        "postToolUse": {
            "command": hook_command(python_path, post_tool_use_py),
            "timeout": 5,
        },
    }


def merge_hooks(
    existing: str,
    python_path: str,
    session_start_py: str,
    before_submit_py: str,
    pre_tool_use_py: str,
    post_tool_use_py: str,
    *,
    path: Path = DEFAULT_HOOKS_PATH,
) -> tuple[str, list[str]]:
    obj = _load(existing, path=path)
    obj["version"] = 1
    hooks = obj.get("hooks")
    if hooks is None:
        hooks = {}
        obj["hooks"] = hooks
    elif not isinstance(hooks, dict):
        raise SystemExit(f"error: {path} hooks field must contain a JSON object.")

    removed = 0
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        filtered = [entry for entry in entries if not _is_owned(entry)]
        removed += len(entries) - len(filtered)
        if filtered:
            hooks[event] = filtered
        else:
            del hooks[event]

    for event, entry in render_entries(
        python_path, session_start_py, before_submit_py, pre_tool_use_py,
        post_tool_use_py,
    ).items():
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise SystemExit(f"error: {path} hooks.{event} must contain a JSON array.")
        entries.insert(0, entry)

    new = json.dumps(obj, indent=2, sort_keys=False) + "\n"
    if new == existing:
        return new, []
    changes = []
    if removed:
        changes.append(f"removed {removed} stale latch-owned Cursor hook(s)")
    changes.append("installed latch Cursor session, gate-enforcement, and activity hooks")
    return new, changes


def write_hooks(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_suffix(path.suffix + ".latchbak").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    path.write_text(content, encoding="utf-8")


def hooks_status(
    path: Path,
    python_path: str,
    session_start_py: str,
    before_submit_py: str,
    pre_tool_use_py: str,
    post_tool_use_py: str,
) -> tuple[bool, str]:
    if not path.exists():
        return False, f"Cursor hooks missing: {path}"
    try:
        current = path.read_text(encoding="utf-8")
        desired, changes = merge_hooks(
            current, python_path, session_start_py, before_submit_py,
            pre_tool_use_py, post_tool_use_py, path=path
        )
    except (OSError, SystemExit) as e:
        return False, f"Cursor hooks unreadable: {e}"
    if desired == current and not changes:
        return True, f"Cursor session/gate/activity hooks installed in {path}"
    return False, f"Cursor session/gate/activity hooks missing or drifted in {path}"


def remove_hooks(path: Path, *, dry_run: bool = False) -> list[str]:
    if not path.exists():
        return []
    obj = _load(path.read_text(encoding="utf-8"), path=path)
    hooks = obj.get("hooks")
    if not isinstance(hooks, dict):
        return []
    removed = 0
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept = [entry for entry in entries if not _is_owned(entry)]
        removed += len(entries) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not removed:
        return []
    if not dry_run:
        if not hooks:
            obj.pop("hooks", None)
        write_hooks(path, json.dumps(obj, indent=2, sort_keys=False) + "\n")
    verb = "would remove" if dry_run else "removed"
    return [f"{verb} {removed} latch-owned Cursor hook(s) from {path}"]
