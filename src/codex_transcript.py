"""Codex rollout transcript discovery and flattening.

Codex stores local thread transcripts under ``$CODEX_HOME/sessions`` as rollout
JSONL files.  This module is deliberately separate from the Claude transcript
locator: Codex compaction must fail closed rather than falling back to
``~/.claude/projects``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

MAX_FIELD_CHARS = 1200


class CodexTranscriptError(RuntimeError):
    pass


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def resolve_session_id(explicit: str | None = None) -> str:
    sid = (explicit or os.environ.get("CODEX_THREAD_ID") or "").strip()
    if not sid:
        raise CodexTranscriptError(
            "no Codex session id supplied (pass one, or run inside Codex with "
            "CODEX_THREAD_ID set)"
        )
    return sid


def find_transcript(session_id: str, *, home: Path | None = None) -> Path:
    root = home or codex_home()
    sessions = root / "sessions"
    if not sessions.is_dir():
        raise CodexTranscriptError(f"Codex sessions dir not found: {sessions}")

    candidates = sorted(
        sessions.glob(f"**/rollout-*{session_id}.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise CodexTranscriptError(
            f"no Codex rollout transcript found for session {session_id} under {sessions}"
        )

    for path in candidates:
        if transcript_session_id(path) == session_id:
            return path
    raise CodexTranscriptError(
        f"found rollout file(s) for {session_id}, but none validated via session_meta"
    )


def transcript_session_id(path: str | Path) -> str | None:
    p = Path(path)
    if not p.exists():
        return None
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "session_meta":
            payload = obj.get("payload") or {}
            sid = payload.get("id")
            return str(sid) if sid else None
    return None


def is_codex_transcript(path: str | Path) -> bool:
    return transcript_session_id(path) is not None


def read_transcript(path: str | Path) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    lines: list[str] = []
    call_names: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = _flatten_event(obj, call_names=call_names)
        if text:
            lines.append(text)
    return "\n\n".join(lines)


def _flatten_event(obj: dict[str, Any], *, call_names: dict[str, str] | None = None) -> str:
    typ = obj.get("type")
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

    if typ == "session_meta":
        fields = {
            "id": payload.get("id"),
            "cwd": payload.get("cwd"),
            "originator": payload.get("originator"),
            "source": payload.get("source"),
            "thread_name": payload.get("thread_name"),
        }
        bits = [f"{k}={v}" for k, v in fields.items() if v]
        return "[session_meta] " + " ".join(bits)

    if typ == "event_msg":
        ptype = payload.get("type")
        if ptype == "user_message":
            return _event_message("user", payload)
        if ptype == "agent_message":
            return _event_message("assistant", payload)
        if ptype in {"task_started", "task_complete"}:
            msg = payload.get("message") or payload.get("last_agent_message") or ""
            return f"[{ptype}] {_clip(str(msg))}" if msg else f"[{ptype}]"
        if ptype == "mcp_tool_call_end":
            tool = payload.get("tool_name") or payload.get("name") or "?"
            return f"[mcp_tool_call_end {tool}]"
        return None

    if typ == "response_item":
        return _flatten_response_item(payload, call_names=call_names)

    if typ == "turn_context":
        cwd = payload.get("cwd")
        return f"[turn_context] cwd={cwd}" if cwd else None

    return None


def _event_message(role: str, payload: dict[str, Any]) -> str | None:
    msg = payload.get("message") or payload.get("text") or payload.get("content")
    if not msg:
        return None
    return f"[{role}] {_clip(str(msg))}"


def _flatten_response_item(
    payload: dict[str, Any],
    *,
    call_names: dict[str, str] | None = None,
) -> str | None:
    ptype = payload.get("type")
    if ptype == "message":
        role = payload.get("role") or "message"
        text = _flatten_content(payload.get("content"))
        return f"[{role}] {text}" if text else None
    if ptype == "function_call":
        name = payload.get("name") or "?"
        call_id = payload.get("call_id")
        if call_names is not None and isinstance(call_id, str) and isinstance(name, str):
            call_names[call_id] = name
        args = payload.get("arguments") or ""
        return f"[tool_use {name}] {_clip(str(args))}"
    if ptype == "function_call_output":
        out = payload.get("output") or ""
        call_id = payload.get("call_id")
        tool_name = (
            call_names.get(call_id)
            if call_names is not None and isinstance(call_id, str)
            else None
        )
        agent_out = _flatten_agent_tool_output(out, tool_name=tool_name)
        if agent_out:
            return agent_out
        return f"[tool_result] {_clip(str(out))}" if out else "[tool_result]"
    if ptype == "web_search_call":
        action = payload.get("action") or {}
        query = action.get("query") if isinstance(action, dict) else None
        return f"[web_search] {query}" if query else "[web_search]"
    if ptype == "reasoning":
        return "[reasoning]"
    return None


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return _clip(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if "text" in item:
                parts.append(str(item["text"]))
            elif item.get("type") in {"input_text", "output_text"}:
                parts.append(str(item.get("text", "")))
        return _clip("\n".join(p for p in parts if p))
    if content:
        return _clip(str(content))
    return ""


def _flatten_agent_tool_output(output: Any, *, tool_name: str | None) -> str | None:
    tool_leaf = (tool_name or "").rsplit(".", 1)[-1]
    if tool_leaf not in {"spawn_agent", "wait_agent"}:
        return None

    obj = _decode_json_object(output)
    if not obj:
        return None

    agent_id = obj.get("agent_id")
    if tool_leaf == "spawn_agent" and isinstance(agent_id, str) and agent_id.strip():
        bits = [f"agent_id={agent_id.strip()}"]
        nickname = obj.get("nickname")
        if isinstance(nickname, str) and nickname.strip():
            bits.append(f"nickname={nickname.strip()}")
        return "[agent_spawned] " + " ".join(bits)

    if tool_leaf != "wait_agent":
        return None

    status = obj.get("status")
    timed_out = obj.get("timed_out")
    timeout_bit = f" timed_out={timed_out}" if isinstance(timed_out, bool) else ""
    if not isinstance(status, dict):
        if timeout_bit:
            return "[agent_status]" + timeout_bit
        return None

    parts: list[str] = []
    for raw_id, raw_state in status.items():
        aid = str(raw_id).strip()
        if not aid:
            continue
        if not isinstance(raw_state, dict):
            parts.append(f"agent_id={aid} status={_clip(str(raw_state), 400)}")
            continue
        state_name = next(
            (k for k in ("completed", "failed", "error", "running") if k in raw_state),
            None,
        )
        if state_name is None:
            parts.append(f"agent_id={aid} status={_clip(str(raw_state), 400)}")
            continue
        state_text = raw_state.get(state_name)
        if state_text:
            parts.append(
                f"agent_id={aid} status={state_name} {_clip(str(state_text), 800)}"
            )
        else:
            parts.append(f"agent_id={aid} status={state_name}")
    if not parts:
        return "[agent_status]" + timeout_bit if timeout_bit else None
    return "[agent_status] " + " | ".join(parts) + timeout_bit


def _decode_json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        obj = json.loads(value)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _clip(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"
