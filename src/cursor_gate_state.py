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
import shutil
import sys
import time
import uuid
from contextlib import contextmanager
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
    "latchpmpreview", "kbpmpreview",
}
_OPERATION_NAMES = {
    "latch-compact", "latch-seed", "latch-gate-report",
    "latch-budget-approve", "latch-decay", "latch-heal", "latch-tree",
    "latch-pm", "unlatch",
}
_OPERATION_MARKER_RE = re.compile(
    r"(?im)^\s*Latch operation id:\s*([a-z0-9-]+)"
    r"(?:\s+(preview|prepare|apply|run|inspect))?\s*$"
)
_TRANSPORT_IDENTITY_KEYS = {
    "server", "server_name", "serverName",
    "tool", "tool_name", "toolName", "name",
}
_PM_CANDIDATE_KEYS = {
    "kind", "title", "body", "status", "links", "workstream_id",
    "artifacts", "session_id",
}
_TOOL_NAME_KEYS = ("tool_name", "toolName", "name")
_TOOL_INPUT_KEYS = ("tool_input", "toolInput", "input")
_SERVER_IDENTITY_KEYS = ("server", "server_name", "serverName")
_NESTED_TOOL_KEYS = ("tool", "tool_name", "toolName", "name")


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
            paths.enforce_vault_policy(first)
            return first
        if isinstance(first, dict):
            for key in ("path", "uri", "root"):
                value = first.get(key)
                if isinstance(value, str) and value.strip():
                    cwd = value.removeprefix("file://")
                    paths.enforce_vault_policy(cwd)
                    return cwd
    for key in ("workspaceRoot", "cwd", "workingDirectory", "workdir"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            paths.enforce_vault_policy(value)
            return value
    cwd = os.getcwd()
    paths.enforce_vault_policy(cwd)
    return cwd


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


def _normalize_pm_links(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("PM links must be a list")
    normalized: set[tuple[int, str]] = set()
    for link in value:
        if not isinstance(link, dict) or set(link) - {"dst", "relation"}:
            raise ValueError("each PM link must contain only dst and relation")
        dst = link.get("dst")
        relation = link.get("relation")
        if isinstance(dst, bool):
            raise ValueError("PM link dst must be an integer node id")
        try:
            dst = int(dst)
        except (TypeError, ValueError):
            raise ValueError("PM link dst must be an integer node id") from None
        if dst <= 0:
            raise ValueError("PM link dst must be a positive node id")
        if not isinstance(relation, str) or not relation or relation != relation.strip():
            raise ValueError("PM link relation must be a non-empty normalized string")
        normalized.add((dst, relation))
    return [
        {"dst": dst, "relation": relation}
        for dst, relation in sorted(normalized, key=lambda item: (item[1], item[0]))
    ]


def canonical_pm_candidate(value: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize every field a one-shot PM insert is allowed to carry."""
    if not isinstance(value, dict):
        raise ValueError("PM candidate must be an object")
    candidate_keys = set(value) - _TRANSPORT_IDENTITY_KEYS
    unknown = candidate_keys - _PM_CANDIDATE_KEYS
    if unknown:
        raise ValueError(f"PM candidate has unsupported fields: {sorted(unknown)}")

    kind = value.get("kind", "decision")
    status = value.get("status", "staging")
    title = value.get("title")
    body = value.get("body")
    if kind != "decision" or status != "staging":
        raise ValueError("PM candidate must be a staging decision")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("PM candidate title must be non-empty")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("PM candidate body must be non-empty")

    workstream_id = value.get("workstream_id")
    if isinstance(workstream_id, bool):
        raise ValueError("PM workstream_id must be an integer node id")
    if workstream_id is not None:
        try:
            workstream_id = int(workstream_id)
        except (TypeError, ValueError):
            raise ValueError("PM workstream_id must be an integer node id") from None
        if workstream_id <= 0:
            raise ValueError("PM workstream_id must be a positive node id")

    artifacts = value.get("artifacts")
    session = value.get("session_id")
    if artifacts not in (None, []):
        raise ValueError("PM preview does not authorize artifact overrides")
    if session not in (None, ""):
        raise ValueError("PM preview does not authorize session overrides")

    return {
        "kind": kind,
        "title": title,
        "body": body,
        "status": status,
        "links": _normalize_pm_links(value.get("links")),
        "workstream_id": workstream_id,
        "artifacts": [],
        "session_id": None,
    }


def pm_candidate_digest(value: dict[str, Any]) -> str:
    candidate = canonical_pm_candidate(value)
    encoded = json.dumps(
        candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def pm_preview_payload(value: dict[str, Any]) -> dict[str, Any]:
    candidate = canonical_pm_candidate(value)
    return {
        "ok": True,
        "operation": "latch-pm",
        "phase": "prepare",
        "write_performed": False,
        "candidate_digest": pm_candidate_digest(candidate),
        "candidate": candidate,
    }


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
        "operation_intent": None,
        "operation_receipt": None,
        "pending_operation": None,
        "updated_at": _now(),
    }
    _atomic_write(state_path(project_path, sid), state)
    return state


def initialize_session(project_path: str, sid: str | None) -> dict[str, Any]:
    """Create turn-zero state without clobbering a concurrently started prompt.

    Cursor can launch ``sessionStart`` and ``beforeSubmitPrompt`` concurrently
    for the first turn.  ``beforeSubmitPrompt`` is the authoritative receipt
    invalidation boundary, so SessionStart must preserve any state already
    written for the same conversation.
    """
    path = state_path(project_path, sid)
    with _exclusive_state_lock(path, wait_s=2.0):
        existing = read_state(project_path, sid)
        if existing and existing.get("session_id") == sid:
            return existing
        state = {
            "version": 1,
            "session_id": sid,
            "turn": 0,
            "prompt_hash": None,
            "gate_receipt": None,
            "operation_intent": None,
            "operation_receipt": None,
            "pending_operation": None,
            "updated_at": _now(),
        }
        _atomic_write(path, state)
        return state


def _begin_prompt_unlocked(
    project_path: str, sid: str | None, prompt: str,
) -> dict[str, Any]:
    previous = read_state(project_path, sid) or {}
    same_session = previous.get("session_id") == sid
    previous_turn = previous.get("turn", 0) if same_session else 0
    try:
        turn = int(previous_turn) + 1
    except (TypeError, ValueError):
        turn = 1
    pending = _pending_operation(previous)
    invocation = _operation_invocation(prompt, pending)
    operation_intent: dict[str, Any] | None = None
    operation_receipt: dict[str, Any] | None = None
    if invocation:
        name, phase, confirmation = invocation
        operation_intent = {
            "name": name,
            "phase": phase,
            "session_id": sid,
            "prompt_hash": prompt_hash(prompt),
        }
        allowed = True
        if name == "latch-seed" and phase == "apply":
            allowed = bool(
                pending and pending.get("name") == name
                and pending.get("stage") == "previewed"
                and isinstance(pending.get("preview_digest"), str)
            )
        elif name == "latch-pm" and phase == "apply":
            allowed = bool(
                pending and pending.get("name") == name
                and pending.get("stage") == "prepared"
                and isinstance(pending.get("candidate_digest"), str)
            )
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
            if name == "latch-pm" and phase == "apply":
                operation_receipt["candidate_digest"] = pending["candidate_digest"]
            if name == "latch-seed" and phase == "apply":
                operation_receipt["preview_digest"] = pending["preview_digest"]

    state = {
        "version": 1,
        "session_id": sid,
        "turn": turn,
        "prompt_hash": prompt_hash(prompt),
        "gate_receipt": None,
        "operation_intent": operation_intent,
        "operation_receipt": operation_receipt,
        "pending_operation": pending,
        "updated_at": _now(),
    }
    _atomic_write(state_path(project_path, sid), state)
    return state


def begin_prompt(project_path: str, sid: str | None, prompt: str) -> dict[str, Any]:
    path = state_path(project_path, sid)
    with _exclusive_state_lock(path, wait_s=2.0):
        return _begin_prompt_unlocked(project_path, sid, prompt)


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


def managed_operation_intended(
    project_path: str,
    sid: str | None,
) -> tuple[bool, str]:
    """Return whether this prompt selected the exclusive managed-operation lane."""
    state = read_state(project_path, sid)
    if not state or not sid or state.get("session_id") != sid:
        return False, "no current Cursor operation state"
    intent = state.get("operation_intent")
    if intent is None:
        return False, "current prompt is not a managed latch operation"
    if not isinstance(intent, dict):
        return True, "managed operation intent is malformed"
    if intent.get("session_id") != sid:
        return True, "managed operation session mismatch"
    if intent.get("prompt_hash") != state.get("prompt_hash"):
        return True, "managed operation intent belongs to another prompt"
    if intent.get("name") not in _OPERATION_NAMES:
        return True, "managed operation identity is invalid"
    return True, f"managed operation {intent.get('name')} {intent.get('phase')}"


def _tool_name(payload: dict[str, Any]) -> str:
    values, malformed = _tool_name_evidence(payload)
    if malformed or not values:
        return ""
    parsed = [_parse_tool_identity(value) for value in values]
    if any(identity is None for identity in parsed):
        return ""
    specific = {
        (identity[0], identity[1])
        for identity in parsed if identity is not None and not identity[2]
    }
    generic_mcp = any(identity[2] for identity in parsed if identity is not None)
    if len(specific) > 1:
        return ""
    if specific:
        expected = next(iter(specific))
        if generic_mcp:
            expected_server, expected_tool = expected
            explicit_server, server_conflict = _explicit_server_identity(payload)
            nested_identity, nested_conflict = _nested_tool_identity(payload)
            if expected_server == "other" or server_conflict or nested_conflict \
                    or explicit_server != expected_server or nested_identity is None:
                return ""
            nested_server, nested_tool = nested_identity
            if nested_tool != expected_tool \
                    or nested_server not in {expected_server, "other"}:
                return ""
        for value, identity in zip(values, parsed):
            if identity is not None and not identity[2] \
                    and (identity[0], identity[1]) == expected:
                return value
    return values[0]


def _normalized_tool_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    values, malformed = _tool_input_evidence(payload)
    if malformed or not values:
        return {}
    first = values[0]
    if any(value != first for value in values[1:]):
        return {}
    return first


def _tool_name_evidence(payload: dict[str, Any]) -> tuple[list[str], bool]:
    values: list[str] = []
    malformed = False
    for key in _TOOL_NAME_KEYS:
        if key not in payload:
            continue
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            malformed = True
        else:
            values.append(value.strip())
    return values, malformed


def _tool_input_evidence(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    values: list[dict[str, Any]] = []
    malformed = False
    for key in _TOOL_INPUT_KEYS:
        if key not in payload:
            continue
        value = payload.get(key)
        if not isinstance(value, dict):
            malformed = True
        else:
            values.append(value)
    return values, malformed


def _explicit_server_identity(payload: dict[str, Any]) -> tuple[str | None, bool]:
    """Return one explicit server identity and whether the evidence conflicts."""
    identities: list[str] = []
    tool_inputs, malformed = _tool_input_evidence(payload)
    for container in (payload, *tool_inputs):
        for key in _SERVER_IDENTITY_KEYS:
            if key not in container:
                continue
            value = container.get(key)
            if not isinstance(value, str) or not value.strip():
                malformed = True
            else:
                identities.append(_normalized_tool_name(value))
    unique = set(identities)
    if malformed or len(unique) > 1:
        return None, True
    return (next(iter(unique)) if unique else None), False


def _parse_tool_identity(name: str) -> tuple[str | None, str | None, bool] | None:
    """Return ``(server, tool, generic_mcp)`` for one tool-name assertion."""
    text = name.strip()
    normalized = _normalized_tool_name(text)
    if not normalized:
        return None
    if text.lower().startswith("mcp__"):
        parts = text.split("__")
        if len(parts) != 3 or not parts[1].strip() or not parts[2].strip():
            return None
        return (
            _normalized_tool_name(parts[1]),
            _normalized_tool_name(parts[2]),
            False,
        )
    # Cursor IDE 3.10 emits direct MCP tool calls as ``MCP:<tool>`` in hook
    # payloads.  The MCP namespace is explicit even though the configured
    # server name is omitted, so accept only the latch/legacy tool families;
    # every other colon-form MCP call remains non-latch and fail-closed.
    if text.lower().startswith("mcp:"):
        tool = _normalized_tool_name(text.split(":", 1)[1])
        if not tool:
            return None
        if tool.startswith("latch"):
            return "latch", tool, False
        if tool.startswith("kb"):
            return "claudekb", tool, False
        return "other", tool, False
    # Cursor's observed generic dispatcher identity is exactly ``MCP``. Do not
    # let an arbitrary native tool name containing those letters claim the
    # generic transport contract.
    if normalized == "mcp":
        return None, None, True
    if normalized.startswith("latch"):
        return "latch", normalized, False
    if normalized.startswith("kb"):
        return "claudekb", normalized, False
    return "other", normalized, False


def _nested_tool_identity(
    payload: dict[str, Any],
) -> tuple[tuple[str | None, str] | None, bool]:
    tool_inputs, malformed = _tool_input_evidence(payload)
    identities: list[tuple[str | None, str]] = []
    for container in tool_inputs:
        for key in _NESTED_TOOL_KEYS:
            if key not in container:
                continue
            value = container.get(key)
            if not isinstance(value, str) or not value.strip():
                malformed = True
                continue
            parsed = _parse_tool_identity(value)
            if parsed is None or parsed[2] or parsed[1] is None:
                malformed = True
                continue
            identities.append((parsed[0], parsed[1]))
    unique = set(identities)
    if malformed or len(unique) > 1:
        return None, True
    return (next(iter(unique)) if unique else None), False


def _tool_matches_server_family(tool: str, server: str) -> bool:
    if server == "latch":
        return tool.startswith("latch")
    if server == "claudekb":
        return tool.startswith("kb")
    return False


def _latch_tool_identity(payload: dict[str, Any], raw_name: str) -> str | None:
    """Return a normalized latch tool name, or None for a non-latch tool.

    Cursor has emitted both qualified tool names and generic MCP payloads.
    Parse only explicit latch/legacy server identities; never infer latch from
    an arbitrary non-latch tool whose name merely contains a familiar suffix.
    """
    del raw_name  # Every alias is authoritative evidence; never select only one.
    raw_names, malformed_names = _tool_name_evidence(payload)
    if malformed_names or not raw_names:
        return None
    parsed_names = [_parse_tool_identity(name) for name in raw_names]
    if any(parsed is None for parsed in parsed_names):
        return None
    generic_mcp = any(parsed[2] for parsed in parsed_names if parsed is not None)
    raw_identities = {
        (parsed[0], parsed[1])
        for parsed in parsed_names if parsed is not None and not parsed[2]
    }
    if len(raw_identities) > 1:
        return None
    raw_identity = next(iter(raw_identities)) if raw_identities else None

    explicit_server, conflict = _explicit_server_identity(payload)
    if conflict or explicit_server is not None and explicit_server not in _LATCH_SERVER_NAMES:
        return None
    nested_identity, nested_conflict = _nested_tool_identity(payload)
    if nested_conflict:
        return None

    server = raw_identity[0] if raw_identity else None
    tool = raw_identity[1] if raw_identity else None
    if server == "other":
        return None
    if nested_identity is not None:
        nested_server, nested_tool = nested_identity
        if nested_server == "other":
            return None
        if tool is not None and tool != nested_tool:
            return None
        if server is not None and nested_server is not None and server != nested_server:
            return None
        tool = tool or nested_tool
        server = server or nested_server
    if explicit_server is not None:
        if server is not None and server != explicit_server:
            return None
        server = explicit_server
    if raw_identity is None and not generic_mcp:
        return None
    if raw_identity is None and explicit_server is None:
        return None
    if server not in _LATCH_SERVER_NAMES:
        return None
    if tool is None:
        return "unknownlatchtool"
    if not _tool_matches_server_family(tool, server):
        return None
    return tool


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


_SHELL_LAUNCHERS = {
    "bash", "sh", "pwsh", "pwsh.exe", "powershell", "powershell.exe",
}
_PYTHON_LAUNCHERS = {"python", "python3", "python.exe", "py", "py.exe"}


def _resolved_executable(value: str) -> Path | None:
    """Resolve an executable exactly as the hook's configured environment does."""
    text = _strip_quotes(value)
    if not text:
        return None
    has_path = Path(text).is_absolute() or "/" in text or "\\" in text
    if has_path:
        return Path(text).expanduser().resolve(strict=False)
    located = shutil.which(text)
    return Path(located).resolve(strict=False) if located else None


def _trusted_launcher(value: str) -> bool:
    text = _strip_quotes(value)
    has_path = Path(text).is_absolute() or "/" in text or "\\" in text
    # Shell wrappers may be located through PATH, but Python may not.  Cursor
    # current-session workflows must use the absolute MCP-bound interpreter;
    # accepting bare ``python3`` here revives the interpreter-drift/SIGILL path
    # even when every installed skill emits LATCH_PYTHON correctly.
    if not has_path and Path(text).name.lower() not in _SHELL_LAUNCHERS:
        return False
    candidate = _resolved_executable(text)
    if candidate is None:
        return False
    trusted: set[Path] = {Path(sys.executable).resolve(strict=False)}
    for env_name in ("LATCH_PYTHON", "CLAUDE_KB_PYTHON"):
        configured = (os.environ.get(env_name) or "").strip()
        if configured:
            trusted.add(Path(configured).expanduser().resolve(strict=False))
    for name in sorted(_SHELL_LAUNCHERS):
        located = shutil.which(name)
        if located:
            trusted.add(Path(located).resolve(strict=False))
    return candidate in trusted


def _allowed_operation_env(name: str, value: str) -> bool:
    if name in _OPERATION_ENV_NAMES:
        return value in {"cursor", "claude", "codex"}
    if name == "LATCH_PYTHON":
        return _trusted_launcher(value)
    return False


def _operation_shell_argv(payload: dict[str, Any]) -> tuple[Path, list[str]] | None:
    raw_name = _normalized_tool_name(_tool_name(payload))
    if raw_name not in _SHELL_NAMES and "shell" not in raw_name and "terminal" not in raw_name:
        return None
    command = _tool_input(payload).get("command") or payload.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    text = command.replace("\\\n", " ").strip()
    if any(token in text for token in ("&&", "||", "|", ">", "<", "`", "$(")):
        return None

    # PowerShell skills may set only the documented backend variables before
    # invoking one exact wrapper in the same Shell tool call.
    segments = [segment.strip() for segment in re.split(r";|\r?\n", text) if segment.strip()]
    if len(segments) > 1:
        for segment in segments[:-1]:
            match = re.fullmatch(
                r"\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(['\"]?)(.+?)\2",
                segment,
                re.IGNORECASE,
            )
            if not match or not _allowed_operation_env(
                match.group(1).upper(), match.group(3),
            ):
                return None
        text = segments[-1]

    try:
        parts = shlex.split(text, posix=(os.name != "nt"))
    except ValueError:
        return None
    parts = [_strip_quotes(part) for part in parts]
    if parts and parts[0] == "&":
        parts = parts[1:]
    while parts and re.fullmatch(r"[A-Z_][A-Z0-9_]*=.+", parts[0]):
        name, value = parts.pop(0).split("=", 1)
        if not _allowed_operation_env(name, value):
            return None
    if parts and (
        Path(parts[0]).name.lower() in _SHELL_LAUNCHERS
        or Path(parts[0]).name.lower() in _PYTHON_LAUNCHERS
        or _trusted_launcher(parts[0])
    ):
        if not _trusted_launcher(parts[0]):
            return None
        parts = parts[1:]
    if not parts:
        return None
    script = Path(parts[0]).expanduser().resolve(strict=False)
    try:
        script.resolve(strict=False).relative_to(paths.KB_ROOT.resolve(strict=False))
    except (OSError, ValueError):
        return None
    return script, parts[1:]


def _trusted_script(script: Path, relative_path: str) -> bool:
    """Require the exact managed script, not merely a trusted-looking basename."""
    expected = (paths.KB_ROOT / relative_path).resolve(strict=False)
    return script == expected


def _same_project(left: str | os.PathLike, right: str | os.PathLike) -> bool:
    try:
        return Path(left).expanduser().resolve(strict=False) == \
            Path(right).expanduser().resolve(strict=False)
    except (OSError, TypeError, ValueError):
        return False


def _project_argument_matches(
    value: str,
    project_path: str,
    payload: dict[str, Any],
) -> bool:
    """Match a literal project path or the shell's constrained PWD sentinel."""
    if value not in {"$PWD", "${PWD}"}:
        return _same_project(value, project_path)

    # The parser rejects command chaining and arbitrary environment setup, so
    # PWD can only mean the Shell tool's starting directory. If Cursor exposes
    # an explicit per-tool cwd, require it to be the hook's current project.
    tool_input = _tool_input(payload)
    for key in ("cwd", "workingDirectory", "workdir"):
        explicit_cwd = tool_input.get(key)
        if isinstance(explicit_cwd, str) and explicit_cwd.strip():
            return _same_project(explicit_cwd, project_path)
    return True


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
    project_path: str,
) -> bool:
    name = operation.get("name")
    phase = operation.get("phase")
    if name == "latch-pm" and phase == "apply":
        latch_tool = _latch_tool_identity(payload, _tool_name(payload))
        tool_input = _tool_input(payload)
        if latch_tool not in {"latchinsert", "kbinsert"}:
            return False
        try:
            digest = pm_candidate_digest(tool_input)
        except ValueError:
            return False
        return digest == operation.get("candidate_digest")

    parsed = _operation_shell_argv(payload)
    if parsed is None:
        return False
    script, args = parsed
    if name == "latch-compact":
        return (
            (
                _trusted_script(script, "bin/run_cursor_compact_now.sh")
                or _trusted_script(script, "bin/run_cursor_compact_now.ps1")
            )
            and args == [operation.get("session_id")]
        )
    if name == "latch-seed":
        if not (
            _trusted_script(script, "bin/latch_seed.sh")
            or _trusted_script(script, "bin/latch_seed.ps1")
        ):
            return False
        expected = [
            "--source", "cursor", "--cursor-session-id", operation.get("session_id"),
            "--format", "json",
        ]
        if phase == "apply":
            expected += [
                "--preview-digest", operation.get("preview_digest"), "--apply", "--yes",
            ]
        return args == expected
    if name == "latch-gate-report":
        return (
            (
                _trusted_script(script, "bin/latch_gate_report.sh")
                or _trusted_script(script, "bin/latch_gate_report.ps1")
            )
            and _report_args_are_read_only(args)
        )
    if name == "latch-budget-approve":
        return (
            _trusted_script(script, "src/budget.py")
            and len(args) == 2
            and args[0] in {"approve", "status"}
            and _project_argument_matches(args[1], project_path, payload)
        )
    if name in {"latch-decay", "latch-heal", "latch-tree"}:
        expected = {"latch-decay": "weekly", "latch-heal": "nightly", "latch-tree": "tree"}[name]
        return (
            _trusted_script(script, "src/maintenance.py")
            and len(args) == 2
            and args[0] == expected
            and _project_argument_matches(args[1], project_path, payload)
        )
    if name == "unlatch":
        if not (
            _trusted_script(script, "bin/unlatch.sh")
            or _trusted_script(script, "bin/unlatch.ps1")
        ):
            return False
        if phase == "inspect":
            return not args
        return phase == "confirm" and args == ["--confirm", operation.get("confirmation")]
    return False


@contextmanager
def _exclusive_state_lock(path: Path, *, wait_s: float = 0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".consume.lock")
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            try:
                stale = time.time() - lock.stat().st_mtime > 10
            except OSError:
                stale = False
            if stale:
                try:
                    lock.unlink()
                    continue
                except (FileNotFoundError, OSError):
                    pass
            if time.monotonic() < deadline:
                time.sleep(0.01)
                continue
            raise RuntimeError("operation receipt is already being consumed") from None
    try:
        os.close(fd)
        yield
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def _coerce_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _has_failure_signal(value: Any, depth: int = 0) -> bool:
    if depth > 8:
        return True
    value = _coerce_json(value)
    if not isinstance(value, (dict, list)):
        return False
    if isinstance(value, list):
        return any(_has_failure_signal(item, depth + 1) for item in value)
    if value.get("is_error") is True or value.get("isError") is True:
        return True
    for key in ("success", "ok", "completed", "done"):
        if key in value and value.get(key) is not True:
            return True
    negative_flags = (
        "cancelled", "canceled", "is_cancelled", "isCanceled",
        "timed_out", "timedOut", "timeout", "denied", "rejected",
        "aborted", "interrupted", "skipped", "expired",
        "was_cancelled", "wasCancelled",
    )
    if any(value.get(key) is True for key in negative_flags):
        return True
    for key in ("exit_code", "exitCode", "code"):
        if key in value:
            try:
                if int(value[key]) != 0:
                    return True
            except (TypeError, ValueError):
                return True
    negative_statuses = {
        "error", "failed", "failure", "cancelled", "canceled",
        "timeout", "timedout", "timed_out", "denied", "rejected",
        "aborted", "interrupted", "skipped", "expired", "incomplete",
        "notrun", "not_run",
    }
    for key in ("status", "state", "outcome", "reason", "stop_reason", "stopReason"):
        if str(value.get(key, "")).strip().lower() in negative_statuses:
            return True
    if str(value.get("permission", "")).strip().lower() in {"deny", "denied", "reject"}:
        return True
    if value.get("error") not in (None, False, "", {}):
        return True
    wrapper_keys = (
        "tool_output", "tool_response", "result_json", "result",
        "stdout", "stderr", "content",
    )
    return any(
        _has_failure_signal(value[key], depth + 1)
        for key in wrapper_keys if key in value
    )


def has_failure_signal(value: Any) -> bool:
    """Public fail-closed wrapper shared by gate and operation receipts."""
    return _has_failure_signal(value)


def _find_seed_preview(value: Any, depth: int = 0) -> dict[str, Any] | None:
    if depth > 8:
        return None
    value = _coerce_json(value)
    if isinstance(value, dict):
        if (
            value.get("ok") is True
            and value.get("source") == "cursor"
            and value.get("apply") is False
            and isinstance(value.get("project"), str)
            and isinstance(value.get("candidates"), list)
            and isinstance(value.get("preview_digest"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value.get("preview_digest"))
        ):
            return value
        for child in value.values():
            found = _find_seed_preview(child, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_seed_preview(child, depth + 1)
            if found is not None:
                return found
    return None


def _find_pm_preview(value: Any, depth: int = 0) -> dict[str, Any] | None:
    if depth > 8:
        return None
    value = _coerce_json(value)
    if isinstance(value, dict):
        if (
            value.get("ok") is True
            and value.get("operation") == "latch-pm"
            and value.get("phase") == "prepare"
            and value.get("write_performed") is False
            and isinstance(value.get("candidate_digest"), str)
            and isinstance(value.get("candidate"), dict)
        ):
            return value
        for child in value.values():
            found = _find_pm_preview(child, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_pm_preview(child, depth + 1)
            if found is not None:
                return found
    return None


def record_operation_success(
    project_path: str,
    sid: str | None,
    payload: dict[str, Any],
    tool_response: Any,
) -> tuple[bool, str] | None:
    """Advance preview/prepare state only from an exact successful tool result."""
    if not sid:
        return None
    path = state_path(project_path, sid)
    try:
        with _exclusive_state_lock(path):
            state = read_state(project_path, sid)
            if not state or state.get("session_id") != sid:
                return False, "no current Cursor operation state"
            if _has_failure_signal(payload) or _has_failure_signal(tool_response):
                return False, "managed operation tool reported failure"

            latch_tool = _latch_tool_identity(payload, _tool_name(payload))
            pending = state.get("pending_operation")
            if latch_tool in {"latchpmpreview", "kbpmpreview"}:
                if not isinstance(pending, dict) or pending.get("name") != "latch-pm" \
                        or pending.get("stage") != "prepare":
                    return False, "no pending PM prepare operation"
                try:
                    candidate = canonical_pm_candidate(_tool_input(payload))
                except ValueError as exc:
                    return False, str(exc)
                preview = _find_pm_preview(tool_response)
                digest = pm_candidate_digest(candidate)
                if preview is None:
                    return False, "PM preview result was missing or malformed"
                try:
                    returned = canonical_pm_candidate(preview["candidate"])
                except ValueError:
                    return False, "PM preview returned an invalid candidate"
                if returned != candidate or preview.get("candidate_digest") != digest:
                    return False, "PM preview result did not match the requested candidate"
                state["pending_operation"] = {
                    "name": "latch-pm", "stage": "prepared",
                    "candidate_digest": digest, "age_turns": 0,
                }
                state["updated_at"] = _now()
                _atomic_write(path, state)
                return True, "verified PM preview and bound candidate digest"

            receipt = state.get("operation_receipt")
            if not isinstance(receipt, dict) or not receipt.get("consumed"):
                return None
            if receipt.get("name") != "latch-seed" or receipt.get("phase") != "preview":
                return None
            if receipt.get("prompt_hash") != state.get("prompt_hash"):
                return False, "seed preview receipt belongs to another prompt"
            if not _operation_tool_matches(receipt, payload, project_path):
                return False, "seed preview tool or arguments did not match the receipt"
            preview = _find_seed_preview(tool_response)
            if preview is None or not _same_project(preview.get("project"), project_path):
                return False, "seed preview result was missing, malformed, or for another project"
            if not isinstance(pending, dict) or pending.get("name") != "latch-seed" \
                    or pending.get("stage") != "preview":
                return False, "no pending seed preview operation"
            preview_digest = preview["preview_digest"]
            state["pending_operation"] = {
                "name": "latch-seed", "stage": "previewed",
                "preview_digest": preview_digest, "age_turns": 0,
            }
            state["updated_at"] = _now()
            _atomic_write(path, state)
            return True, "verified successful seed preview"
    except RuntimeError as exc:
        return False, str(exc)


def consume_operation_authorization(
    project_path: str,
    sid: str | None,
    payload: dict[str, Any],
) -> tuple[bool, str]:
    """Consume one exact operation-specific receipt, atomically and once."""
    if not sid:
        return False, "Cursor session identity was unavailable"
    try:
        path = state_path(project_path, sid)
        with _exclusive_state_lock(path):
            state = read_state(project_path, sid)
            if not state or state.get("session_id") != sid:
                return False, "no current Cursor operation state"
            receipt = state.get("operation_receipt")
            if not isinstance(receipt, dict) or receipt.get("consumed"):
                return False, "no unconsumed operation receipt for this prompt"
            if receipt.get("prompt_hash") != state.get("prompt_hash"):
                return False, "operation receipt belongs to another prompt"
            if not _operation_tool_matches(receipt, payload, project_path):
                return False, "tool or arguments do not match the authorized latch operation"
            receipt["consumed"] = True
            receipt["consumed_at"] = _now()
            name, phase = receipt.get("name"), receipt.get("phase")
            if name == "unlatch" and phase == "inspect":
                state["pending_operation"] = {"name": name, "stage": "inspected", "age_turns": 0}
            elif phase in {"apply", "confirm"} or name not in {"latch-seed", "unlatch"}:
                state["pending_operation"] = None
            state["updated_at"] = _now()
            _atomic_write(path, state)
            return True, f"authorized one {name} {phase} operation"
    except RuntimeError as exc:
        return False, str(exc)


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
