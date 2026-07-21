"""Lightweight, dev-only trigger queue for the local incident detector.

This module intentionally uses only the Python standard library. Hooks import it
only after confirming ``LATCH_DEV_DETECTOR=1`` and it never performs tracing or
database work inline.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


DEV_FLAG = "LATCH_DEV_DETECTOR"
_CLAUDE_CODE_ADAPTERS = {"", "claude-code", "claude_code"}
AUTO_TRIGGER_TYPES = frozenset({
    "explicit_correction",
    "runtime_degraded",
    "direct_authority_conflict",
    "corrected_node_cited_current",
})


def enabled() -> bool:
    """Return True only for exact opt-in on the Claude Code host slice."""
    adapter = os.environ.get("LATCH_ADAPTER", "").strip().lower()
    return os.environ.get(DEV_FLAG) == "1" and adapter in _CLAUDE_CODE_ADAPTERS


def queue(
    *,
    project_path: str,
    session_id: str,
    trigger_types: Iterable[str],
    transcript_path: str | None = None,
    prompt_hash: str | None = None,
    event_ts: str | None = None,
    node_ids: Iterable[int] = (),
    turn: int | None = None,
) -> bool:
    """Spawn the deterministic trace worker and return whether launch succeeded.

    No prompt or transcript content is placed on argv. The child re-opens the
    local transcript and structural receipts after the latency-sensitive hook
    has returned.
    """
    if not enabled() or not project_path or not session_id:
        return False
    requested = {str(t).strip() for t in trigger_types if str(t).strip()}
    if not requested or not requested.issubset(AUTO_TRIGGER_TYPES):
        return False
    triggers = sorted(requested)

    worker = Path(__file__).resolve().parent / "detector_worker.py"
    args = [
        sys.executable,
        str(worker),
        "--project",
        str(project_path),
        "--session-id",
        str(session_id),
        "--event-ts",
        event_ts or _now_iso(),
    ]
    for trigger in triggers:
        args.extend(["--trigger", trigger])
    if transcript_path:
        args.extend(["--transcript", str(transcript_path)])
    if prompt_hash:
        args.extend(["--prompt-hash", str(prompt_hash)])
    if turn is not None:
        args.extend(["--turn", str(int(turn))])
    for node_id in sorted({int(n) for n in node_ids}):
        args.extend(["--node-id", str(node_id)])

    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        popen_kwargs["start_new_session"] = True
    try:
        subprocess.Popen(args, **popen_kwargs)
        return True
    except Exception:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def encode_trigger_receipt(trigger_types: Iterable[str], node_ids: Iterable[int]) -> str:
    """Stable structural helper used by tests; never contains raw content."""
    return json.dumps(
        {
            "triggers": sorted(
                {str(t) for t in trigger_types} & AUTO_TRIGGER_TYPES
            ),
            "node_ids": sorted({int(n) for n in node_ids}),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
