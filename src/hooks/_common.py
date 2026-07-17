"""Shared hook utilities. Hooks read UTF-8 JSON from agent stdin."""
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
    if sys.stdin.isatty():
        return {}

    # Cursor on Windows writes BOM-prefixed UTF-8 hook payloads, while the
    # inherited console locale can wrap stdin as cp1252.  Reading through that
    # text wrapper turns EF BB BF into the three characters ``ï»¿`` and makes
    # otherwise-valid JSON fail at column 1.  Decode the underlying bytes
    # explicitly; utf-8-sig accepts both BOM and BOM-free UTF-8.
    stream = getattr(sys.stdin, "buffer", None)
    if stream is not None:
        # Deliberately let UnicodeDecodeError escape. Cursor marks the prompt
        # and pre-tool hooks failClosed; converting undecodable bytes to an
        # empty payload would bypass that contract exactly when the request
        # cannot be fingerprinted.
        raw = stream.read().decode("utf-8-sig")
    else:
        # StringIO and other text-only streams are useful in tests and embeds.
        # Their decoder may already have preserved a Unicode BOM marker.
        raw = sys.stdin.read().removeprefix("\ufeff")
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
    cwd = hook_field(payload, "cwd", "workingDirectory", default=os.getcwd())
    paths.enforce_vault_policy(cwd)
    return cwd


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
