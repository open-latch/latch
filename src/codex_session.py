"""Codex session handoff helpers.

Codex exposes the current thread id to hooks, but not necessarily to MCP server
children. The SessionStart hook writes a small per-project marker and the MCP
server can read it lazily when it is running under the Codex adapter.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import paths


MARKER_FILE = "codex_session.json"


def marker_path(project_path: str | os.PathLike | None = None) -> Path:
    return paths.project_dir(project_path) / MARKER_FILE


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
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": sid,
        "transcript_path": transcript_path,
        "project_path": str(project_path or os.getcwd()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "codex_session_start",
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_marker(project_path: str | os.PathLike | None = None) -> dict | None:
    path = marker_path(project_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def read_session_id(project_path: str | os.PathLike | None = None) -> str | None:
    payload = read_marker(project_path)
    if not payload:
        return None
    sid = payload.get("session_id")
    if not isinstance(sid, str):
        return None
    sid = sid.strip()
    return sid or None
