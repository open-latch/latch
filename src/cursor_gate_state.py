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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paths


STATE_FILE_PREFIX = "cursor_gate_state"
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
_READ_ONLY_TOOL_NAMES = {
    "read", "search", "grep", "glob", "list", "view", "inspect",
    "askquestion", "askuserquestion",
}
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
_OPERATION_NAMES = {
    "latch-compact", "latch-seed", "latch-gate-report",
    "latch-budget-approve", "latch-decay", "latch-heal", "latch-tree",
    "latch-pm", "unlatch",
}
_OPERATION_MARKER_RE = re.compile(
    r"(?im)^\s*Latch operation id:\s*([a-z0-9-]+)(?:\s+(preview|apply|run|inspect))?\s*$"
)


def _session_key(sid: str | None) -> str:
    value = (sid or "").strip()
    if not value:
        return "unknown"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def state_path(
    project_path: str | os.PathLike | None = None,
    sid: str | None = None,
) -> Path:
    return paths.project_dir(project_path) / f"{STATE_FILE_PREFIX}.{_session_key(sid)}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def read_state(
    project_path: str | os.PathLike | None = None,
    sid: str | None = None,
) -> dict[str, Any] | None:
    try:
        payload = json.loads(state_path(project_path, sid).read_text(encoding="utf-8"))
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
    # Hook payload identity is request-scoped; the project marker is not. Two
    # Cursor conversations can interleave in one project, so a missing hook id
    # must fail closed instead of inheriting the last SessionStart marker.
    return None


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


def _pending_operation(previous: dict[str, Any]) -> dict[str, Any] | None:
    pending = previous.get("pending_operation")
    if not isinstance(pending, dict) or pending.get("name") not in _OPERATION_NAMES:
        return None
    try:
        age = int(pending.get("age_turns", 0)) + 1
    except (TypeError, ValueError):
        return None
    if age > 6:
        return None
    return {**pending, "age_turns": age}


def _operation_invocation(
    prompt: str,
    pending: dict[str, Any] | None,
) -> tuple[str, str, str | None] | None:
    """Recognize an explicit managed operation, never general prose.

    Returns ``(name, phase, confirmation)``.  Command assets carry a managed
    marker because Cursor may expand a slash command before the hook sees it;
    literal invocations remain supported for skills and confirmation turns.
    """
    text = _normalize_prompt(prompt)
    marker = _OPERATION_MARKER_RE.search(text)
    if marker:
        name = marker.group(1).lower()
        if name not in _OPERATION_NAMES:
            return None
        phase = (marker.group(2) or "run").lower()
        return name, phase, None

    literal = text.strip().lower()
    if literal.startswith("/"):
        literal = literal[1:]
    tokens = literal.split()
    if not tokens or len(tokens) > 2:
        return None
    name = tokens[0]
    arg = tokens[1] if len(tokens) == 2 else None
    if name in {"latch", "unlatch"} and arg is None \
            and pending and pending.get("name") == "unlatch":
        return "unlatch", "confirm", name
    if name in _OPERATION_NAMES:
        if name == "latch-seed":
            return name, "apply" if arg == "apply" else "preview", None
        if name == "latch-pm":
            return name, "apply" if arg == "apply" else "prepare", None
        if name == "unlatch":
            return name, "inspect", None
        if arg is not None:
            return None
        return name, "run", None
    return None


def reset_session(project_path: str, sid: str | None) -> dict[str, Any]:
    state = {
        "version": 1,
        "session_id": sid,
        "turn": 0,
        "prompt_hash": None,
        "gate_receipt": None,
        "operation_receipt": None,
        "pending_operation": None,
        "updated_at": _now(),
    }
    _atomic_write(state_path(project_path, sid), state)
    return state


def begin_prompt(project_path: str, sid: str | None, prompt: str) -> dict[str, Any]:
    previous = read_state(project_path, sid) or {}
    same_session = previous.get("session_id") == sid
    previous_turn = previous.get("turn", 0) if same_session else 0
    try:
        turn = int(previous_turn) + 1
    except (TypeError, ValueError):
        turn = 1
    pending = _pending_operation(previous)
    invocation = _operation_invocation(prompt, pending)
    operation_receipt: dict[str, Any] | None = None
    if invocation:
        name, phase, confirmation = invocation
        allowed = True
        if name == "latch-seed" and phase == "apply":
            allowed = bool(
                pending and pending.get("name") == name
                and pending.get("stage") == "previewed"
            )
        elif name == "latch-pm" and phase == "apply":
            allowed = bool(pending and pending.get("name") == name)
        elif name == "unlatch" and phase == "confirm":
            allowed = bool(pending and pending.get("name") == name)
        if name == "latch-seed" and phase == "preview":
            pending = {"name": name, "stage": "preview", "age_turns": 0}
        elif name == "latch-pm" and phase == "prepare":
            pending = {"name": name, "stage": "prepare", "age_turns": 0}
        elif name == "unlatch" and phase == "inspect":
            pending = {"name": name, "stage": "inspect", "age_turns": 0}
        elif name not in {"latch-seed", "latch-pm", "unlatch"}:
            pending = None
        if allowed and phase not in {"prepare"}:
            operation_receipt = {
                "name": name,
                "phase": phase,
                "confirmation": confirmation,
                "session_id": sid,
                "prompt_hash": prompt_hash(prompt),
                "consumed": False,
                "recorded_at": _now(),
            }

    state = {
        "version": 1,
        "session_id": sid,
        "turn": turn,
        "prompt_hash": prompt_hash(prompt),
        "gate_receipt": None,
        "operation_receipt": operation_receipt,
        "pending_operation": pending,
        "updated_at": _now(),
    }
    _atomic_write(state_path(project_path, sid), state)
    return state


def record_gate(
    project_path: str,
    sid: str | None,
    *,
    request: str,
    gate_status: str | None,
    recommendation: str | None,
) -> tuple[bool, str]:
    state = read_state(project_path, sid)
    if not state:
        return False, "no current Cursor prompt state"
    state_sid = state.get("session_id")
    if not state_sid or not sid:
        return False, "Cursor session identity was unavailable"
    if state_sid != sid:
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
    _atomic_write(state_path(project_path, sid), state)
    return True, rec


def mutation_authorized(project_path: str, sid: str | None) -> tuple[bool, str]:
    state = read_state(project_path, sid)
    if not state:
        return False, "no current Cursor prompt state"
    state_sid = state.get("session_id")
    if not state_sid or not sid:
        return False, "Cursor session identity was unavailable"
    if state_sid != sid:
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


def is_latch_gate_tool(payload: dict[str, Any]) -> bool:
    """Return whether this hook payload identifies the latch gate tool itself.

    A gate-shaped result is not provenance: arbitrary tools can return nested
    JSON.  Receipt creation therefore requires both a recognized latch/legacy
    tool identity and the exact gate operation.
    """
    if not isinstance(payload, dict):
        return False
    raw_name = _tool_name(payload)
    return _latch_tool_identity(payload, raw_name) in {"latchgate", "kbgate"}


_OPERATION_ENV_NAMES = {
    "LATCH_COMPACTOR_BACKEND", "LATCH_MODEL_BACKEND", "LATCH_SEED_BACKEND",
    "LATCH_MAINTENANCE_BACKEND", "LATCH_GATE_BACKEND",
}


def _strip_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _operation_shell_argv(payload: dict[str, Any]) -> tuple[str, list[str]] | None:
    raw_name = _normalized_tool_name(_tool_name(payload))
    if raw_name not in _SHELL_NAMES and "shell" not in raw_name and "terminal" not in raw_name:
        return None
    command = _tool_input(payload).get("command") or payload.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    text = command.replace("\\\n", " ").strip()
    if any(token in text for token in ("&&", "||", "|", ">", "<", "`", "$(", "\r", "\n")):
        return None

    # PowerShell skills may set only the documented backend variables before
    # invoking one exact wrapper in the same Shell tool call.
    segments = [segment.strip() for segment in text.split(";")]
    if len(segments) > 1:
        for segment in segments[:-1]:
            match = re.fullmatch(
                r"\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(['\"]?)(cursor|claude|codex)\2",
                segment,
                re.IGNORECASE,
            )
            if not match or match.group(1).upper() not in _OPERATION_ENV_NAMES:
                return None
        text = segments[-1]

    try:
        parts = shlex.split(text, posix=(os.name != "nt"))
    except ValueError:
        return None
    parts = [_strip_quotes(part) for part in parts]
    if parts and parts[0] == "&":
        parts = parts[1:]
    while parts and re.fullmatch(r"[A-Z_][A-Z0-9_]*=(cursor|claude|codex)", parts[0]):
        name, _value = parts.pop(0).split("=", 1)
        if name not in _OPERATION_ENV_NAMES:
            return None
    if parts and Path(parts[0]).name.lower() in {
        "bash", "sh", "pwsh", "pwsh.exe", "powershell", "powershell.exe",
    }:
        parts = parts[1:]
    if not parts:
        return None

    if Path(parts[0]).name.lower() in {"python", "python3", "python.exe", "py"}:
        parts = parts[1:]
        if not parts:
            return None
    script = Path(parts[0]).expanduser()
    try:
        script.resolve(strict=False).relative_to(paths.KB_ROOT.resolve(strict=False))
    except (OSError, ValueError):
        return None
    return script.name.lower(), parts[1:]


def _report_args_are_read_only(args: list[str]) -> bool:
    value_flags = {"--days", "--limit", "--start", "--end"}
    index = 0
    while index < len(args):
        flag = args[index]
        if flag not in value_flags or index + 1 >= len(args):
            return False
        value = args[index + 1]
        if not value or value.startswith("-") and flag in {"--days", "--limit"}:
            return False
        index += 2
    return True


def _operation_tool_matches(
    operation: dict[str, Any],
    payload: dict[str, Any],
) -> bool:
    name = operation.get("name")
    phase = operation.get("phase")
    if name == "latch-pm" and phase == "apply":
        latch_tool = _latch_tool_identity(payload, _tool_name(payload))
        tool_input = _tool_input(payload)
        return (
            latch_tool in {"latchinsert", "kbinsert"}
            and tool_input.get("kind") == "decision"
            and tool_input.get("status", "staging") == "staging"
            and isinstance(tool_input.get("title"), str) and bool(tool_input["title"].strip())
            and isinstance(tool_input.get("body"), str) and bool(tool_input["body"].strip())
        )

    parsed = _operation_shell_argv(payload)
    if parsed is None:
        return False
    script, args = parsed
    if name == "latch-compact":
        return (
            script in {"run_cursor_compact_now.sh", "run_cursor_compact_now.ps1"}
            and args == [operation.get("session_id")]
        )
    if name == "latch-seed":
        if script not in {"latch_seed.sh", "latch_seed.ps1"}:
            return False
        expected = ["--source", "cursor"]
        if phase == "apply":
            expected += ["--apply", "--yes"]
        return args == expected
    if name == "latch-gate-report":
        return (
            script in {"latch_gate_report.sh", "latch_gate_report.ps1"}
            and _report_args_are_read_only(args)
        )
    if name == "latch-budget-approve":
        return script == "budget.py" and len(args) == 2 and args[0] in {"approve", "status"}
    if name in {"latch-decay", "latch-heal", "latch-tree"}:
        expected = {"latch-decay": "weekly", "latch-heal": "nightly", "latch-tree": "tree"}[name]
        return script == "maintenance.py" and len(args) == 2 and args[0] == expected
    if name == "unlatch":
        if script not in {"unlatch.sh", "unlatch.ps1"}:
            return False
        if phase == "inspect":
            return not args
        return phase == "confirm" and args == ["--confirm", operation.get("confirmation")]
    return False


def consume_operation_authorization(
    project_path: str,
    sid: str | None,
    payload: dict[str, Any],
) -> tuple[bool, str]:
    """Consume one exact operation-specific receipt, atomically and once."""
    if not sid:
        return False, "Cursor session identity was unavailable"
    path = state_path(project_path, sid)
    lock = path.with_name(path.name + ".consume.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False, "operation receipt is already being consumed"
    try:
        os.close(fd)
        state = read_state(project_path, sid)
        if not state or state.get("session_id") != sid:
            return False, "no current Cursor operation state"
        receipt = state.get("operation_receipt")
        if not isinstance(receipt, dict) or receipt.get("consumed"):
            return False, "no unconsumed operation receipt for this prompt"
        if receipt.get("prompt_hash") != state.get("prompt_hash"):
            return False, "operation receipt belongs to another prompt"
        if not _operation_tool_matches(receipt, payload):
            return False, "tool or arguments do not match the authorized latch operation"
        receipt["consumed"] = True
        receipt["consumed_at"] = _now()
        name, phase = receipt.get("name"), receipt.get("phase")
        if name == "latch-seed" and phase == "preview":
            state["pending_operation"] = {"name": name, "stage": "previewed", "age_turns": 0}
        elif name == "unlatch" and phase == "inspect":
            state["pending_operation"] = {"name": name, "stage": "inspected", "age_turns": 0}
        elif phase in {"apply", "confirm"} or name not in {"latch-seed", "unlatch"}:
            state["pending_operation"] = None
        state["updated_at"] = _now()
        _atomic_write(path, state)
        return True, f"authorized one {name} {phase} operation"
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


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
        # Shell option surfaces are not closed: apparently read-only programs
        # such as sed, git diff, rg, and file all have write/exec variants.
        # Native exact read tools remain available; Shell requires a gate or a
        # one-shot managed-operation receipt.
        return True, "mutation-capable Shell command"

    if name in _READ_ONLY_TOOL_NAMES:
        return False, raw_name
    return True, f"unknown tool {raw_name}"


def read_only_shell_command(command: str) -> bool:
    """Compatibility helper: no free-form Shell command is pre-gate safe."""
    return False
