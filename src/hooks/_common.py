"""Shared hook utilities. Hooks read JSON on stdin from Claude Code."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Ensure src/ on sys.path for sibling imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402

PYTHON_BIN = sys.executable


def read_hook_input() -> dict:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def hook_field(payload: dict, *keys, default=None):
    """Pull a value out of the hook payload, tolerant to schema drift."""
    for k in keys:
        if k in payload and payload[k] is not None:
            return payload[k]
    return default


def project_cwd(payload: dict) -> str:
    return hook_field(payload, "cwd", "workingDirectory", default=os.getcwd())


def session_id(payload: dict) -> str | None:
    return hook_field(payload, "session_id", "sessionId")


def transcript_path(payload: dict) -> str | None:
    return hook_field(payload, "transcript_path", "transcriptPath")


def spawn_compactor_detached(session_id: str, project_path: str, transcript: str | None, final: bool = False) -> None:
    """Fire-and-forget the compactor so the hook returns immediately."""
    compactor = str(Path(__file__).resolve().parent.parent / "compactor.py")
    args = [PYTHON_BIN, compactor, session_id, project_path]
    if transcript:
        args.append(transcript)
    if final:
        args.append("--final")
    popen_kwargs = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — survive parent exit, no console window.
        popen_kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        # POSIX: start_new_session detaches the compactor into its own session
        # so it outlives this hook process and isn't killed by signals sent to
        # the parent's process group. Mirrors selfheal.spawn_detached.
        popen_kwargs["start_new_session"] = True
    try:
        subprocess.Popen(args, **popen_kwargs)
    except Exception as e:
        log(f"spawn_compactor failed: {e}")


def log(msg: str) -> None:
    log_path = paths.KB_ROOT / "hooks.log"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            from datetime import datetime
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass
