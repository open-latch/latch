"""Cursor current-conversation transcript handoff helpers.

Cursor passes a conversation id and transcript path to project hooks.  The
SessionStart hook writes the current pair to this small project marker for
explicit current-session seed/compact workflows.  The long-lived MCP process
must not use the project-wide marker as request provenance because multiple
Cursor conversations can interleave in one project.
"""
from __future__ import annotations

import json
import hashlib
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import paths


MARKER_FILE = "cursor_session.json"


def marker_path(project_path: str | os.PathLike | None = None) -> Path:
    return paths.project_dir(project_path) / MARKER_FILE


def session_marker_path(
    project_path: str | os.PathLike | None,
    session_id: str,
) -> Path:
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    key = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:20]
    return paths.project_dir(project_path) / f"cursor_session.{key}.json"


def _write_payload(path: Path, payload: dict) -> None:
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


def write_marker(
    project_path: str | os.PathLike | None,
    session_id: str,
    *,
    transcript_path: str | None = None,
) -> Path:
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    path = marker_path(project_path)
    payload = {
        "session_id": sid,
        "transcript_path": transcript_path,
        "project_path": str(project_path or os.getcwd()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "cursor_session_start",
    }
    _write_payload(session_marker_path(project_path, sid), payload)
    _write_payload(path, payload)
    return path


def read_marker(
    project_path: str | os.PathLike | None = None,
    session_id: str | None = None,
) -> dict | None:
    path = (
        session_marker_path(project_path, session_id)
        if (session_id or "").strip()
        else marker_path(project_path)
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def read_session_id(
    project_path: str | os.PathLike | None = None,
    session_id: str | None = None,
) -> str | None:
    payload = read_marker(project_path, session_id=session_id)
    if not payload:
        return None
    sid = payload.get("session_id")
    if not isinstance(sid, str):
        return None
    sid = sid.strip()
    return sid or None
