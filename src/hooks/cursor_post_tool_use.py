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


def cursor_surface_message(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None
    response = None
    for key in ("tool_output", "tool_response", "result_json", "result"):
        if key in payload:
            response = payload[key]
            break
    return surface_message({"tool_response": response})


def main() -> int:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        payload = json.loads(raw) if raw.strip() else {}
        msg = cursor_surface_message(payload)
        if msg:
            print(json.dumps({
                "additional_context":
                    "IMPORTANT: Surface this latch activity to the user now: " + msg
            }))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
