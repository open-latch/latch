"""Shared hook utilities. Hooks read UTF-8 JSON from agent stdin."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

# Ensure src/ on sys.path for sibling imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lockfile  # noqa: E402
import paths  # noqa: E402
import project_config  # noqa: E402

PYTHON_BIN = sys.executable
STALE_SESSION_MESSAGE = (
    "Latch's project mode or KB changed after this agent session started. "
    "Latch skipped this stale session; start a fresh agent task in this project "
    "and do not resume the old one."
)


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
    return hook_field(payload, "cwd", "workingDirectory", default=os.getcwd())


def session_id(payload: dict) -> str | None:
    return hook_field(payload, "session_id", "sessionId")


def transcript_path(payload: dict) -> str | None:
    return hook_field(payload, "transcript_path", "transcriptPath")


@contextlib.contextmanager
def session_start_transition(project_path: str):
    """Share the canonical scope lease while SessionStart binds its task."""
    if paths.is_unlatched_mode(project_path):
        yield
        return
    target = project_config.resolve(project_path)
    if target.state != project_config.MODE_LATCHED:
        yield
        return
    with lockfile.project_access_lock(project_path):
        yield


def record_session_binding(project_path: str, sid: str | None) -> str | None:
    if not sid:
        return None
    return project_config.record_session_binding(project_path, sid)


def record_session_boundary(project_path: str, sid: str | None) -> str | None:
    if not sid:
        return None
    return project_config.record_session_boundary(project_path, sid)


def fence_inactive_session(project_path: str, sid: str | None) -> None:
    """Best-effort tombstone for a task observed while Latch is unavailable."""
    if not sid:
        return
    # Failure to write a receipt never grants access: every agent data-plane
    # entry point also rejects a missing receipt.  Avoid trying to log through
    # the KB while the scope is deliberately OFF or LOCKED.
    with contextlib.suppress(OSError, project_config.ProjectConfigError):
        project_config.record_session_boundary(project_path, sid)


def current_session_revision(project_path: str, sid: str | None) -> str | None:
    if not sid:
        return None
    return project_config.current_session_revision(project_path, sid)


def clear_session_binding(
    project_path: str,
    sid: str | None,
    *,
    expected_revision: str | None = None,
) -> None:
    if sid:
        project_config.clear_session_binding(
            project_path,
            sid,
            expected_revision=expected_revision,
        )


def spawn_compactor_detached(
    session_id: str,
    project_path: str,
    transcript: str | None,
    final: bool = False,
    *,
    binding_revision: str | None = None,
    expected_kb_dir: str | None = None,
) -> None:
    """Fire-and-forget the compactor so the hook returns immediately."""
    compactor = str(Path(__file__).resolve().parent.parent / "compactor.py")
    args = [PYTHON_BIN, compactor, session_id, project_path]
    if transcript:
        args.append(transcript)
    if final:
        args.append("--final")
    if binding_revision:
        args.extend(["--binding-revision", binding_revision])
    if expected_kb_dir:
        args.extend(["--kb-dir", expected_kb_dir])
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
        log(
            f"spawn_compactor failed: {e}",
            project_path,
            expected_revision=binding_revision,
        )


def log(
    msg: str,
    project_path: str | None = None,
    *,
    expected_revision: str | None = None,
) -> None:
    try:
        from datetime import datetime

        lockfile.append_project_log(
            project_path,
            "hooks.log",
            f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n",
            expected_revision=expected_revision,
        )
    except Exception:
        pass
