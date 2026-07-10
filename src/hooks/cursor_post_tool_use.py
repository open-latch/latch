#!/usr/bin/env python3
"""Cursor postToolUse hook that carries latch activity into agent context.

Cursor does not expose Claude Code's user-visible ``systemMessage`` channel.
Use Cursor's native ``additional_context`` response and stay fail-open.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

from post_tool_use import surface_message  # noqa: E402
from _common import log  # noqa: E402
import cursor_gate_state  # noqa: E402


def cursor_tool_response(payload: dict):
    if not isinstance(payload, dict):
        return None
    for key in ("tool_output", "tool_response", "result_json", "result"):
        if key in payload:
            return payload[key]
    return None


def cursor_surface_message(payload: dict) -> str | None:
    return surface_message({"tool_response": cursor_tool_response(payload)})


def _coerce(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def find_gate_result(value, depth: int = 0) -> dict | None:
    if depth > 8 or value is None:
        return None
    value = _coerce(value)
    if isinstance(value, dict):
        if (
            isinstance(value.get("request"), str)
            and isinstance(value.get("verdict"), dict)
            and "gate_status" in value
        ):
            return value
        for child in value.values():
            found = find_gate_result(child, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_gate_result(child, depth + 1)
            if found is not None:
                return found
    return None


def record_gate_receipt(payload: dict) -> tuple[bool, str] | None:
    result = find_gate_result(cursor_tool_response(payload))
    if result is None:
        return None
    cwd = cursor_gate_state.project_cwd(payload)
    sid = cursor_gate_state.session_id(payload, cwd)
    verdict = result.get("verdict") or {}
    return cursor_gate_state.record_gate(
        cwd,
        sid,
        request=result.get("request", ""),
        gate_status=result.get("gate_status"),
        recommendation=verdict.get("recommendation"),
    )


def main() -> int:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        payload = json.loads(raw) if raw.strip() else {}
        msg = cursor_surface_message(payload)
        gate_record = record_gate_receipt(payload)
        if gate_record is not None and not gate_record[0]:
            mismatch = (
                "Latch gate receipt was not armed: " + gate_record[1] + ". "
                "Run latch_gate again with the current user request verbatim."
            )
            msg = f"{msg}\n\n{mismatch}" if msg else mismatch
        if msg:
            print(json.dumps({
                "additional_context":
                    "IMPORTANT: Surface this latch activity to the user now: " + msg
            }))
    except Exception as e:
        log(f"cursor_post_tool_use failed open: {e}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
