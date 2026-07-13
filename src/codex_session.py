"""Codex session handoff helpers.

Codex exposes the current thread id to hooks, but not necessarily to MCP server
children. The SessionStart hook writes a small per-project marker and the MCP
server can read it lazily when it is running under the Codex adapter.
"""
from __future__ import annotations

import json
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import paths


MARKER_FILE = "codex_session.json"
MARKER_DIR = "codex_sessions"


def marker_path(project_path: str | os.PathLike | None = None) -> Path:
    """Per-workspace rendezvous path, even when the KB itself is pinned.

    A pinned vault intentionally ignores cwd for database selection.  Session
    handoff is different: collapsing all workspaces onto one marker causes the
    newest Codex task to misattribute every other live MCP connection.  Keep
    markers under the pinned runtime directory but key them by canonical cwd.
    """
    project = _canonical_project(project_path)
    key = hashlib.sha256(project.encode("utf-8")).hexdigest()[:24]
    return paths.project_dir(project_path) / "runtime" / MARKER_DIR / key / MARKER_FILE


def _legacy_marker_path(project_path: str | os.PathLike | None = None) -> Path:
    return paths.project_dir(project_path) / MARKER_FILE


def _canonical_project(project_path: str | os.PathLike | None = None) -> str:
    source = Path(project_path or os.getcwd()).expanduser()
    try:
        return str(source.resolve())
    except OSError:
        return os.path.abspath(str(source))


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
        "project_path": _canonical_project(project_path),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "codex_session_start",
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_marker(project_path: str | os.PathLike | None = None) -> dict | None:
    expected_project = _canonical_project(project_path)
    for path in (marker_path(project_path), _legacy_marker_path(project_path)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        marker_project = payload.get("project_path")
        if not isinstance(marker_project, str):
            continue
        if _canonical_project(marker_project) != expected_project:
            continue
        return payload
    return None


def read_session_id(project_path: str | os.PathLike | None = None) -> str | None:
    payload = read_marker(project_path)
    if not payload:
        return None
    sid = payload.get("session_id")
    if not isinstance(sid, str):
        return None
    sid = sid.strip()
    return sid or None
