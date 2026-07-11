"""Per-prompt Cursor gate receipt state and mutation classification.

The state is deliberately structural: prompt text is hashed, never stored.
``beforeSubmitPrompt`` invalidates the previous receipt; ``postToolUse`` arms
the current turn only when latch_gate ran on that exact prompt; ``preToolUse``
consults the state before mutation-capable tools execute.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cursor_session
import paths


STATE_FILE = "cursor_gate_state.json"
VALID_RECOMMENDATIONS = {
    "PROCEED",
    "MODIFY",
    "DO_NOT_PROCEED",
    "NEEDS_HUMAN_JUDGMENT",
}
_MUTATION_MARKERS = (
    "write", "edit", "replace", "delete", "patch", "createfile",
    "movefile", "rename", "notebookedit", "applypatch",
)
_SHELL_NAMES = {"shell", "terminal", "runcommand", "executecommand"}
_TASK_NAMES = {"task", "subagent", "spawnagent"}
_READ_ONLY_COMMANDS = {
    "pwd", "ls", "dir", "rg", "grep", "cat", "head", "tail", "wc",
    "stat", "file", "which", "where", "type", "get-childitem",
    "get-content", "select-string", "get-location", "test-path",
}
_CONTROL_TOKENS = ("\n", "\r", ";", "&&", "||", "|", ">", "<", "`", "$(")
_LATCH_SERVER_NAMES = {"latch", "claudekb"}
_PRE_GATE_LATCH_ALLOWLIST = {
    "latchsearch", "kbsearch",
    "latchget", "kbget",
    "latchrecent", "kbrecent",
    "latchprojectdirection", "kbprojectdirection",
    "latchgatereport", "kbgatereport",
    "latchgate", "kbgate",
    "latchverify", "kbverify",
    "latchcorrectplan", "kbcorrectplan",
    "latchprioritylist", "kbprioritylist",
    "latchembed", "kbembed",
}


def state_path(project_path: str | os.PathLike | None = None) -> Path:
    return paths.project_dir(project_path) / STATE_FILE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_state(project_path: str | os.PathLike | None = None) -> dict[str, Any] | None:
    try:
        payload = json.loads(state_path(project_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def project_cwd(payload: dict[str, Any]) -> str:
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots:
        first = roots[0]
        if isinstance(first, str) and first.strip():
            return first
        if isinstance(first, dict):
            for key in ("path", "uri", "root"):
                value = first.get(key)
                if isinstance(value, str) and value.strip():
                    return value.removeprefix("file://")
    for key in ("workspaceRoot", "cwd", "workingDirectory", "workdir"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return os.getcwd()


def session_id(payload: dict[str, Any], project_path: str | None = None) -> str | None:
    for key in ("conversation_id", "session_id", "sessionId", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return cursor_session.read_session_id(project_path or project_cwd(payload))


def prompt_text(payload: dict[str, Any]) -> str:
    for key in ("prompt", "user_prompt", "user_message"):
        value = payload.get(key)
        if isinstance(value, str):
            return _normalize_prompt(value)
    message = payload.get("message")
    if isinstance(message, str):
        return _normalize_prompt(message)
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return _normalize_prompt(content)
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            return _normalize_prompt("\n".join(parts))
    return ""


def _normalize_prompt(prompt: str) -> str:
    text = (prompt or "").strip()
    wrapped = re.fullmatch(r"<user_query>\s*(.*?)\s*</user_query>", text, re.DOTALL)
    return wrapped.group(1).strip() if wrapped else text


def prompt_hash(prompt: str) -> str | None:
    normalized = _normalize_prompt(prompt)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def reset_session(project_path: str, sid: str | None) -> dict[str, Any]:
    state = {
        "version": 1,
        "session_id": sid,
        "turn": 0,
        "prompt_hash": None,
        "gate_receipt": None,
        "updated_at": _now(),
    }
    _atomic_write(state_path(project_path), state)
    return state


def begin_prompt(project_path: str, sid: str | None, prompt: str) -> dict[str, Any]:
    previous = read_state(project_path) or {}
    same_session = previous.get("session_id") == sid
    previous_turn = previous.get("turn", 0) if same_session else 0
    try:
        turn = int(previous_turn) + 1
    except (TypeError, ValueError):
        turn = 1
    state = {
        "version": 1,
        "session_id": sid,
        "turn": turn,
        "prompt_hash": prompt_hash(prompt),
        "gate_receipt": None,
        "updated_at": _now(),
    }
    _atomic_write(state_path(project_path), state)
    return state


def record_gate(
    project_path: str,
    sid: str | None,
    *,
    request: str,
    gate_status: str | None,
    recommendation: str | None,
) -> tuple[bool, str]:
    state = read_state(project_path)
    if not state:
        return False, "no current Cursor prompt state"
    state_sid = state.get("session_id")
    if state_sid and sid and state_sid != sid:
        return False, "Cursor session mismatch"
    current_hash = state.get("prompt_hash")
    request_hash = prompt_hash(request)
    if not current_hash:
        return False, "current Cursor prompt was unavailable"
    if request_hash != current_hash:
        return False, "latch_gate request did not match the current Cursor prompt verbatim"
    rec = (recommendation or "").strip().upper()
    if gate_status != "OK" or rec not in VALID_RECOMMENDATIONS:
        return False, "latch_gate did not return a usable verdict"
    state["gate_receipt"] = {
        "prompt_hash": current_hash,
        "gate_status": gate_status,
        "recommendation": rec,
        "recorded_at": _now(),
    }
    state["updated_at"] = _now()
    _atomic_write(state_path(project_path), state)
    return True, rec


def mutation_authorized(project_path: str, sid: str | None) -> tuple[bool, str]:
    state = read_state(project_path)
    if not state:
        return False, "no current Cursor prompt state"
    state_sid = state.get("session_id")
    if state_sid and sid and state_sid != sid:
        return False, "Cursor session mismatch"
    current_hash = state.get("prompt_hash")
    if not current_hash:
        return False, "current Cursor prompt was unavailable"
    receipt = state.get("gate_receipt")
    if not isinstance(receipt, dict):
        return False, "no latch_gate receipt for the current Cursor prompt"
    if receipt.get("prompt_hash") != current_hash:
        return False, "latch_gate receipt belongs to an older Cursor prompt"
    if receipt.get("gate_status") != "OK":
        return False, "latch_gate receipt is not usable"
    if receipt.get("recommendation") not in VALID_RECOMMENDATIONS:
        return False, "latch_gate receipt has no usable verdict"
    return True, str(receipt["recommendation"])


def _tool_name(payload: dict[str, Any]) -> str:
    for key in ("tool_name", "toolName", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalized_tool_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("tool_input", "toolInput", "input"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _latch_tool_identity(payload: dict[str, Any], raw_name: str) -> str | None:
    """Return a normalized latch tool name, or None for a non-latch tool.

    Cursor has emitted both qualified tool names and generic MCP payloads.
    Parse only explicit latch/legacy server identities; never infer latch from
    an arbitrary non-latch tool whose name merely contains a familiar suffix.
    """
    name = (raw_name or "").strip()
    normalized_name = _normalized_tool_name(name)
    if normalized_name.startswith("latch") or normalized_name.startswith("kb"):
        return normalized_name

    tool_input = _tool_input(payload)
    server = ""
    for key in ("server", "server_name", "serverName"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            server = _normalized_tool_name(value)
            break

    if name.lower().startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3 and _normalized_tool_name(parts[1]) in _LATCH_SERVER_NAMES:
            return _normalized_tool_name(parts[-1])

    if "mcp" not in normalized_name or server not in _LATCH_SERVER_NAMES:
        return None
    for key in ("tool", "tool_name", "toolName", "name"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return _normalized_tool_name(value)
    return "unknownlatchtool"


def mutation_capability(payload: dict[str, Any]) -> tuple[bool, str]:
    """Return whether a preToolUse payload can mutate project/external state.

    Unknown or malformed payloads are mutation-capable by default. MCP calls
    stay available so latch_gate itself can run before the gate is armed.
    """
    if not isinstance(payload, dict):
        return True, "malformed preToolUse payload"
    raw_name = _tool_name(payload)
    name = _normalized_tool_name(raw_name)
    if not name:
        return True, "missing tool name"
    latch_tool = _latch_tool_identity(payload, raw_name)
    if latch_tool is not None:
        if latch_tool in _PRE_GATE_LATCH_ALLOWLIST:
            return False, f"pre-gate latch tool {latch_tool}"
        return True, f"mutation-capable or unknown latch tool {latch_tool}"
    if "mcp" in name:
        return True, f"non-latch MCP tool {raw_name}"
    if any(marker in name for marker in _MUTATION_MARKERS):
        return True, raw_name
    if name in _TASK_NAMES or name.endswith("task"):
        readonly = _tool_input(payload).get("readonly")
        return (not bool(readonly), raw_name)
    if name in _SHELL_NAMES or "shell" in name or "terminal" in name:
        tool_input = _tool_input(payload)
        command = tool_input.get("command") or payload.get("command")
        if not isinstance(command, str) or not command.strip():
            return True, "Shell with unavailable command"
        if read_only_shell_command(command):
            return False, "read-only Shell command"
        return True, "mutation-capable Shell command"

    read_markers = ("read", "search", "grep", "glob", "list", "view", "inspect")
    if any(marker in name for marker in read_markers):
        return False, raw_name
    if name in {"askquestion", "askuserquestion"}:
        return False, raw_name
    return True, f"unknown tool {raw_name}"


def read_only_shell_command(command: str) -> bool:
    text = command.strip()
    if not text or any(token in text for token in _CONTROL_TOKENS):
        return False
    try:
        parts = shlex.split(text, posix=(os.name != "nt"))
    except ValueError:
        return False
    if not parts:
        return False
    exe = Path(parts[0]).name.lower()
    args = parts[1:]
    if exe in _READ_ONLY_COMMANDS:
        if exe == "sed" and any(arg == "-i" or arg.startswith("-i") for arg in args):
            return False
        return True
    if exe == "sed":
        return not any(arg == "-i" or arg.startswith("-i") for arg in args)
    if exe == "git":
        while args and args[0] == "-C":
            if len(args) < 2:
                return False
            args = args[2:]
        if not args:
            return False
        subcommand, rest = args[0], args[1:]
        if subcommand in {"status", "diff", "log", "show", "rev-parse", "ls-files", "ls-tree", "ls-remote"}:
            return True
        if subcommand == "branch":
            mutation_flags = {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy"}
            return not any(arg in mutation_flags for arg in rest) and all(
                arg.startswith("-") for arg in rest
            )
        if subcommand == "remote":
            return not rest or rest == ["-v"] or rest == ["--verbose"]
        return False
    if exe == "gh" and len(args) >= 2:
        return (args[0], args[1]) in {
            ("pr", "view"), ("pr", "checks"), ("pr", "diff"),
            ("pr", "list"), ("pr", "status"), ("run", "view"),
            ("run", "list"),
        }
    return False
