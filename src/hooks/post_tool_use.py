#!/usr/bin/env python3
"""PostToolUse hook — deterministic latch activity surface.

When a `mcp__latch__*` or legacy `mcp__claude-kb__*` tool returns a
`kb_activity` block (ordinary KB
reads/writes) or a gate `findings` block, both carry `must_display_to_user`.
The agent is *asked* to echo those via the CLAUDE.md/AGENTS.md contract, but
that depends on the agent cooperating. This hook makes the surface
DETERMINISTIC: it parses the tool result itself and emits the one-line summary
on `systemMessage`, the one PostToolUse output channel that renders inline to
the USER (confirmed empirically + against the Claude Code hooks docs; KB
id=552). The channels split cleanly — `additionalContext` reaches only the
model, `systemMessage` reaches only the user — so this never double-feeds the
model.

Design constraints:
  * Claude Code prefixes the line with `PostToolUse:<tool> says:`, which we
    cannot remove — the message text must read well after that prefix.
  * Fail SILENT and OPEN: any parse/shape problem exits 0 with no output. A
    surfacing hook must never break the tool call it observes.
  * No filesystem writes (the spike's probe log is gone) and no
    `additionalContext` echo — the model already has the tool result.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Cap the surfaced line so a long gate/why-it-matters summary stays a glance,
# not a wall. Generous enough to never clip a normal one-liner.
_MAX_LEN = 600


def _coerce(obj):
    """Best-effort parse of a tool_response value into a Python structure.

    Claude Code may hand us the result as a JSON string (observed:
    `'{"result": [...]}'`), an already-parsed dict/list, or MCP text-content
    blocks. Return the parsed object, or None when a string will not parse.
    """
    if isinstance(obj, str):
        s = obj.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except (ValueError, TypeError):
            return None
    return obj


def _find_surface(obj, depth: int = 0):
    """Walk a parsed tool result for the first dict carrying a displayable
    `kb_activity` or gate `findings` block. Returns that block, or None.

    Tolerant to wrappers (`{"result": ...}`, MCP `content`/`structuredContent`,
    list rows where kb_search parks `kb_activity` on row 0) and to nested
    JSON-encoded strings.
    """
    if depth > 8 or obj is None:
        return None
    if isinstance(obj, str):
        parsed = _coerce(obj)
        if parsed is None or parsed == obj:
            return None
        return _find_surface(parsed, depth + 1)
    if isinstance(obj, dict):
        for key in ("kb_activity", "findings"):
            block = obj.get(key)
            if isinstance(block, dict) and block.get("summary"):
                return block
        for value in obj.values():
            hit = _find_surface(value, depth + 1)
            if hit is not None:
                return hit
        return None
    if isinstance(obj, list):
        for item in obj:
            hit = _find_surface(item, depth + 1)
            if hit is not None:
                return hit
    return None


def surface_message(payload: dict) -> str | None:
    """The one-line message to show the user, or None when nothing should show.

    Pure function (no I/O) so it is unit-testable. Honors
    `must_display_to_user`: a block that does not opt in is never surfaced.
    """
    if not isinstance(payload, dict):
        return None
    block = _find_surface(payload.get("tool_response"))
    if not isinstance(block, dict):
        return None
    if not block.get("must_display_to_user"):
        return None
    summary = (block.get("summary") or "").strip()
    if not summary:
        return None

    label = (block.get("label") or "latch").strip()
    rec = block.get("recommendation")
    if rec:  # gate findings carry a verdict
        msg = f"{label} [{rec}]: {summary}"
    else:
        msg = f"{label}: {summary}"

    # A why-it-matters sentence, when present and not already in the summary.
    why = (block.get("why_it_matters") or "").strip()
    if why and why not in summary:
        msg = f"{msg} — {why}"

    if len(msg) > _MAX_LEN:
        msg = msg[: _MAX_LEN - 1].rstrip() + "…"
    return msg


def main() -> int:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        payload = json.loads(raw) if raw.strip() else {}
        msg = surface_message(payload)
        if msg:
            print(json.dumps({"systemMessage": msg}))
    except Exception:
        # Fail silent-open: never break or annotate the observed tool call.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
