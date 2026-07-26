"""Fail-closed Cursor transcript resolution.

Cursor's current-session hooks supply conversation identity and
``transcript_path``. Latch records that pair in its project marker and resolves
only that explicit handoff by default. Historical discovery is a separate,
explicit opt-in over
the current project's local IDE transcript artifacts. It reads Cursor's local
project-membership/header metadata to prove each conversation's project and
subagent status; it never reads CLI sessions, cloud chats, or another project.
"""
from __future__ import annotations

import json
import ntpath
import os
from pathlib import Path
import re
import sqlite3
import uuid

import cursor_session


class CursorTranscriptError(RuntimeError):
    pass


def _is_windows_path(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"))


def _is_windows_drive_path(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", value))


def _project_path_spellings(project_path: str) -> tuple[str, ...]:
    """Preserve Cursor-visible and canonical spellings without broad guessing."""
    raw = os.path.expanduser(str(project_path).strip())
    if not raw:
        return ()
    if _is_windows_path(raw):
        normalized = ntpath.normpath(raw)
        spellings = [normalized]
    else:
        absolute = os.path.abspath(raw)
        spellings = [absolute]
        try:
            resolved = str(Path(absolute).resolve())
        except OSError:
            resolved = absolute
        if resolved not in spellings:
            spellings.append(resolved)
    return tuple(spellings)


def _cursor_project_storage_key(path: str) -> str:
    # Mirrors Cursor's current local-agent workspace sanitizer: every run of
    # non-ASCII-alphanumeric characters becomes one hyphen.
    return re.sub(r"[^a-zA-Z0-9]+", "-", path).strip("-")


def cursor_project_storage_keys(project_path: str) -> tuple[str, ...]:
    """Return narrowly bounded local Cursor project-key candidates for one path.

    Both the path spelling Cursor may have received and its symlink-resolved
    spelling are retained. The format is a private implementation detail, so
    no directory-wide fuzzy matching is allowed.
    """
    keys: list[str] = []
    for spelling in _project_path_spellings(project_path):
        key = _cursor_project_storage_key(spelling)
        if key and key not in keys:
            keys.append(key)
        if _is_windows_drive_path(spelling) and key:
            alternate = key[0].swapcase() + key[1:]
            if alternate not in keys:
                keys.append(alternate)
    return tuple(keys)


def _project_identity(project_path: str) -> str:
    spellings = _project_path_spellings(project_path)
    if not spellings:
        return ""
    canonical = spellings[-1]
    if _is_windows_path(canonical):
        return ntpath.normcase(ntpath.normpath(canonical))
    return canonical


def _cursor_state_db_candidates(
    state_db: str | os.PathLike[str] | None,
) -> tuple[Path, ...]:
    if state_db:
        return (Path(state_db).expanduser(),)
    configured = os.environ.get("CURSOR_STATE_DB")
    if configured:
        return (Path(configured).expanduser(),)

    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(
            Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
        )
    candidates.append(
        Path.home()
        / "Library"
        / "Application Support"
        / "Cursor"
        / "User"
        / "globalStorage"
        / "state.vscdb"
    )
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    )
    candidates.append(
        config_home / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    )
    return tuple(dict.fromkeys(candidates))


def _cursor_state_db(
    state_db: str | os.PathLike[str] | None,
) -> Path:
    for candidate in _cursor_state_db_candidates(state_db):
        if candidate.is_file():
            return candidate
    raise CursorTranscriptError(
        "Cursor history metadata is unavailable; refusing project history "
        "discovery"
    )


def _json_item(conn: sqlite3.Connection, key: str) -> object:
    row = conn.execute(
        "SELECT value FROM ItemTable WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        raise CursorTranscriptError(
            "Cursor history metadata is missing the project-membership index; "
            "refusing historical discovery"
        )
    value = row[0]
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CursorTranscriptError("Cursor history metadata is malformed") from exc
    if not isinstance(value, str):
        raise CursorTranscriptError("Cursor history metadata is malformed")
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise CursorTranscriptError("Cursor history metadata is malformed") from exc


def _authorized_project_history_scope(
    project_path: str,
    *,
    state_db: str | os.PathLike[str] | None = None,
) -> tuple[set[str], tuple[str, ...]]:
    """Return authorized ids plus Cursor's exact matching workspace spellings.

    Cursor persists three independent facts: project rows, conversation/project
    membership, and typed composer headers. Latch intersects all three and
    excludes every row Cursor marks as a subagent.
    """
    db_path = _cursor_state_db(state_db)
    try:
        conn = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=2.0,
        )
    except (OSError, sqlite3.Error) as exc:
        raise CursorTranscriptError(
            "Cursor history metadata could not be opened read-only"
        ) from exc
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 2000")
        projects = _json_item(conn, "glass.localAgentProjects.v1")
        memberships = _json_item(
            conn, "glass.localAgentProjectMembership.v1",
        )
        if not isinstance(projects, list) or not isinstance(memberships, dict):
            raise CursorTranscriptError("Cursor history metadata is malformed")

        requested = _project_identity(project_path)
        project_workspaces: dict[str, str] = {}
        workspace_paths: list[str] = []
        for row in projects:
            if not isinstance(row, dict):
                continue
            project_id = row.get("id")
            workspace = row.get("workspace")
            if not isinstance(project_id, str) or not isinstance(workspace, dict):
                continue
            workspace_id = workspace.get("id")
            uri = workspace.get("uri")
            if not isinstance(workspace_id, str) or not isinstance(uri, dict):
                continue
            workspace_path = uri.get("fsPath") or uri.get("path")
            if not isinstance(workspace_path, str):
                continue
            if _project_identity(workspace_path) == requested:
                existing_workspace = project_workspaces.get(project_id)
                if existing_workspace not in (None, workspace_id):
                    raise CursorTranscriptError(
                        "Cursor history metadata has conflicting project rows"
                    )
                project_workspaces[project_id] = workspace_id
                if workspace_path not in workspace_paths:
                    workspace_paths.append(workspace_path)
        if not project_workspaces:
            raise CursorTranscriptError(
                "Cursor history metadata has no exact row for this project; "
                "refusing historical discovery"
            )

        headers: dict[str, tuple[str, int]] = {}
        for composer_id, workspace_id, is_subagent in conn.execute(
                "SELECT composerId, workspaceId, isSubagent "
                "FROM composerHeaders"
        ):
            if not isinstance(composer_id, str) \
                    or not isinstance(workspace_id, str) \
                    or is_subagent not in (0, 1):
                continue
            try:
                normalized_id = str(uuid.UUID(composer_id))
            except ValueError:
                continue
            headers[normalized_id] = (workspace_id, int(is_subagent))
        authorized: set[str] = set()
        for raw_session_id, raw_project_id in memberships.items():
            if not isinstance(raw_session_id, str) \
                    or not isinstance(raw_project_id, str):
                continue
            try:
                session_id = str(uuid.UUID(raw_session_id))
            except ValueError:
                continue
            expected_workspace = project_workspaces.get(raw_project_id)
            header = headers.get(session_id)
            if expected_workspace is None or header is None:
                continue
            header_workspace, is_subagent = header
            if header_workspace == expected_workspace and is_subagent == 0:
                authorized.add(session_id)
        return authorized, tuple(workspace_paths)
    except sqlite3.Error as exc:
        raise CursorTranscriptError(
            "Cursor history metadata schema is unavailable; refusing "
            "historical discovery"
        ) from exc
    finally:
        conn.close()


def authorized_project_history_ids(
    project_path: str,
    *,
    state_db: str | os.PathLike[str] | None = None,
) -> set[str]:
    """Return IDE conversation ids authoritatively assigned to this project."""
    authorized, _workspace_paths = _authorized_project_history_scope(
        project_path,
        state_db=state_db,
    )
    return authorized


def project_history_roots(
    project_path: str,
    *,
    cursor_home: str | os.PathLike[str] | None = None,
) -> tuple[Path, ...]:
    """Return existing narrowly derived local IDE transcript roots."""
    home = Path(
        cursor_home
        or os.environ.get("CURSOR_HOME")
        or (Path.home() / ".cursor")
    ).expanduser()
    roots: list[Path] = []
    for key in cursor_project_storage_keys(project_path):
        root = home / "projects" / key / "agent-transcripts"
        if root.is_dir():
            roots.append(root)
    return tuple(roots)


def discover_project_history(
    project_path: str,
    *,
    cursor_home: str | os.PathLike[str] | None = None,
    state_db: str | os.PathLike[str] | None = None,
) -> list[tuple[str, Path]]:
    """Discover top-level Cursor IDE transcripts for exactly one project.

    Only ``<uuid>/<uuid>.jsonl`` entries at the expected on-disk depth whose id
    passes Cursor's project-membership and typed non-subagent checks are
    accepted. The path check also excludes nested artifacts and refuses to
    follow a candidate outside the selected project root.
    """
    authorized, workspace_paths = _authorized_project_history_scope(
        project_path,
        state_db=state_db,
    )
    roots: list[Path] = []
    # Cursor's persisted spelling is authoritative for its on-disk bucket.
    # The caller spelling remains a bounded fallback for older layouts.
    for workspace_path in (*workspace_paths, project_path):
        for root in project_history_roots(
            workspace_path,
            cursor_home=cursor_home,
        ):
            try:
                already_seen = any(root.samefile(existing) for existing in roots)
            except OSError:
                already_seen = root in roots
            if not already_seen:
                roots.append(root)
    if not roots:
        return []
    found: dict[str, Path] = {}
    for root in roots:
        try:
            root_resolved = root.resolve()
            session_dirs = sorted(root.iterdir(), key=lambda path: path.name)
        except OSError:
            continue
        for session_dir in session_dirs:
            if not session_dir.is_dir():
                continue
            try:
                session_id = str(uuid.UUID(session_dir.name))
            except (ValueError, AttributeError):
                continue
            if session_id not in authorized:
                continue
            transcript = session_dir / f"{session_dir.name}.jsonl"
            try:
                resolved = transcript.resolve()
            except OSError:
                continue
            if not resolved.is_relative_to(root_resolved) or not resolved.is_file():
                continue
            previous = found.get(session_id)
            if previous is not None and previous != resolved:
                raise CursorTranscriptError(
                    "one Cursor conversation appears in multiple project "
                    "history roots; refusing ambiguous discovery"
                )
            found[session_id] = resolved
    return [(session_id, found[session_id]) for session_id in sorted(found)]


def resolve_current(
    project_path: str,
    *,
    session_id: str | None = None,
    transcript_path: str | None = None,
) -> tuple[str, Path]:
    explicit_sid = (session_id or "").strip()
    if not explicit_sid:
        raise CursorTranscriptError(
            "an explicit current Cursor session id is required; use the id "
            "shown in the current prompt context"
        )
    marker = cursor_session.read_marker(project_path, session_id=explicit_sid)
    if not marker:
        raise CursorTranscriptError(
            f"no current-session marker for requested Cursor session {explicit_sid}"
        )
    marker_sid = marker.get("session_id")
    if not isinstance(marker_sid, str) or not marker_sid.strip():
        raise CursorTranscriptError("current Cursor marker has no session id")
    marker_sid = marker_sid.strip()
    if explicit_sid != marker_sid:
        raise CursorTranscriptError(
            f"requested Cursor session {explicit_sid} does not match current session {marker_sid}"
        )

    marker_transcript = marker.get("transcript_path")
    if not isinstance(marker_transcript, str) or not marker_transcript.strip():
        raise CursorTranscriptError(
            "current Cursor marker has no transcript_path; refusing to discover "
            "or guess from undocumented Cursor storage"
        )
    marker_path = Path(marker_transcript).expanduser().resolve()
    if transcript_path:
        explicit_path = Path(transcript_path).expanduser().resolve()
        if explicit_path != marker_path:
            raise CursorTranscriptError(
                f"requested transcript {explicit_path} does not match current Cursor marker {marker_path}"
            )
    if not marker_path.is_file():
        raise CursorTranscriptError(f"current Cursor transcript is not a readable file: {marker_path}")
    return marker_sid, marker_path
