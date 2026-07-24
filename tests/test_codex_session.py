"""Unit tests for Codex hook-to-MCP session handoff helpers."""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import codex_session  # noqa: E402
import paths  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _payload(
    project: str | Path,
    session_id: str,
    *,
    updated_at: str = "2026-01-01T00:00:00+00:00",
) -> dict:
    return {
        "session_id": session_id,
        "transcript_path": None,
        "project_path": codex_session._canonical_project(project),
        "updated_at": updated_at,
        "source": "codex_session_start",
    }


def test_write_and_read_session_marker():
    tmp = tempfile.mkdtemp(prefix="codex_session_marker_")
    project_dir = paths.project_dir(tmp)
    try:
        marker = codex_session.write_marker(
            tmp,
            " session-123 ",
            transcript_path="/tmp/rollout.jsonl",
        )
        _assert(marker == codex_session.marker_path(tmp), marker)
        _assert(marker.exists(), "marker should be written")
        payload = codex_session.read_marker(tmp)
        _assert(payload["session_id"] == "session-123", payload)
        _assert(payload["transcript_path"] == "/tmp/rollout.jsonl", payload)
        _assert(payload["source"] == "codex_session_start", payload)
        _assert(codex_session.read_session_id(tmp) == "session-123", payload)
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS write_and_read_session_marker")


def test_invalidate_marker_prevents_stale_session_inheritance():
    tmp = tempfile.mkdtemp(prefix="codex_session_marker_")
    project_dir = paths.project_dir(tmp)
    try:
        codex_session.write_marker(tmp, "previous-task")
        codex_session.invalidate_marker(tmp)
        _assert(
            codex_session.read_marker(tmp) is None,
            "newer invalidation must hide the prior marker",
        )
        _assert(
            codex_session.read_session_id(tmp) is None,
            "new task without an id must not inherit prior attribution",
        )
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS invalidate_marker_prevents_stale_session_inheritance")


def test_read_session_marker_missing_or_invalid_returns_none():
    tmp = tempfile.mkdtemp(prefix="codex_session_marker_")
    project_dir = paths.project_dir(tmp)
    try:
        _assert(codex_session.read_marker(tmp) is None, "missing marker should be None")
        project_dir.mkdir(parents=True, exist_ok=True)
        marker = codex_session.marker_path(tmp)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{not json", encoding="utf-8")
        _assert(codex_session.read_marker(tmp) is None, "bad marker should be None")
        _assert(codex_session.read_session_id(tmp) is None, "bad marker session id should be None")
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS read_session_marker_missing_or_invalid_returns_none")


def test_pinned_vault_keeps_workspace_markers_distinct():
    shared = Path(tempfile.mkdtemp(prefix="codex_session_shared_vault_"))
    project_a = Path(tempfile.mkdtemp(prefix="codex_session_project_a_"))
    project_b = Path(tempfile.mkdtemp(prefix="codex_session_project_b_"))
    old = paths._PINNED_DIR
    try:
        paths._PINNED_DIR = shared
        marker_a = codex_session.write_marker(project_a, "session-a")
        marker_b = codex_session.write_marker(project_b, "session-b")
        _assert(marker_a != marker_b, "pinned vault markers must be keyed by workspace")
        _assert(codex_session.read_session_id(project_a) == "session-a", marker_a)
        _assert(codex_session.read_session_id(project_b) == "session-b", marker_b)
    finally:
        paths._PINNED_DIR = old
        shutil.rmtree(shared, ignore_errors=True)
        shutil.rmtree(project_a, ignore_errors=True)
        shutil.rmtree(project_b, ignore_errors=True)
    print("PASS pinned_vault_keeps_workspace_markers_distinct")


def test_readonly_primary_uses_private_temp_fallback():
    if codex_session._current_uid() is None:
        return
    root = Path(tempfile.mkdtemp(prefix="codex_session_fallback_"))
    project = root / "workspace"
    project.mkdir()
    blocked_vault = root / "blocked-vault"
    blocked_vault.write_text("not a directory", encoding="utf-8")
    old = paths._PINNED_DIR
    fallback_scope = None
    try:
        paths._PINNED_DIR = blocked_vault
        expected_fallback = codex_session._fallback_marker_path(project)
        fallback_scope = expected_fallback.parents[2]
        written = codex_session.write_marker(
            project,
            "fallback-session",
            transcript_path="/tmp/fallback-rollout.jsonl",
        )
        _assert(written == expected_fallback, written)
        _assert(
            Path(tempfile.gettempdir()) in written.parents,
            f"fallback must live below tempfile.gettempdir(): {written}",
        )
        _assert(codex_session.read_session_id(project) == "fallback-session", written)
        payload = codex_session.read_marker(project)
        _assert(payload["transcript_path"] == "/tmp/fallback-rollout.jsonl", payload)
        if os.name != "nt":
            _assert(
                stat.S_IMODE(written.stat().st_mode) == 0o600,
                f"fallback file must be user-private: {oct(written.stat().st_mode)}",
            )
            for directory in (
                fallback_scope,
                fallback_scope / codex_session.MARKER_DIR,
                written.parent,
            ):
                _assert(
                    stat.S_IMODE(directory.stat().st_mode) == 0o700,
                    f"fallback directory must be user-private: {directory}",
                )
    finally:
        paths._PINNED_DIR = old
        if fallback_scope is not None:
            shutil.rmtree(fallback_scope, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
    print("PASS readonly_primary_uses_private_temp_fallback")


def test_marker_read_uses_freshness_then_path_precedence():
    if codex_session._current_uid() is None:
        return
    root = Path(tempfile.mkdtemp(prefix="codex_session_precedence_"))
    project = root / "workspace"
    project.mkdir()
    vault = root / "vault"
    vault.write_text("initially block the primary path", encoding="utf-8")
    old = paths._PINNED_DIR
    fallback_scope = None
    try:
        paths._PINNED_DIR = vault
        fallback = codex_session.write_marker(project, "fallback-session")
        fallback_scope = fallback.parents[2]
        _assert(fallback == codex_session._fallback_marker_path(project), fallback)

        vault.unlink()
        vault.mkdir()
        legacy = codex_session._legacy_marker_path(project)
        legacy.write_text(
            json.dumps(_payload(project, "legacy-session")) + "\n",
            encoding="utf-8",
        )
        _assert(
            codex_session.read_session_id(project) == "fallback-session",
            "newer fallback must win over older legacy",
        )

        primary = codex_session.write_marker(project, "primary-session")
        _assert(primary == codex_session.marker_path(project), primary)
        _assert(
            codex_session.read_session_id(project) == "primary-session",
            "primary must win over fallback",
        )
    finally:
        paths._PINNED_DIR = old
        if fallback_scope is not None:
            shutil.rmtree(fallback_scope, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
    print("PASS marker_read_uses_freshness_then_path_precedence")


def test_newer_fallback_wins_over_stale_primary():
    if codex_session._current_uid() is None:
        return
    root = Path(tempfile.mkdtemp(prefix="codex_session_freshest_"))
    project = root / "workspace"
    project.mkdir()
    vault = root / "vault"
    vault.mkdir()
    old = paths._PINNED_DIR
    fallback_scope = None
    try:
        paths._PINNED_DIR = vault
        primary = codex_session.marker_path(project)
        primary.parent.mkdir(parents=True)
        primary.write_text(
            json.dumps(
                _payload(
                    project,
                    "stale-primary",
                    updated_at="2026-01-01T00:00:00+00:00",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        fallback = codex_session._fallback_marker_path(project)
        fallback_scope = fallback.parents[2]
        codex_session._write_fallback_marker(
            fallback,
            json.dumps(
                _payload(
                    project,
                    "newer-fallback",
                    updated_at="2026-01-02T00:00:00Z",
                )
            )
            + "\n",
        )
        _assert(
            codex_session.read_session_id(project) == "newer-fallback",
            "a stale readable primary must not override the newer fallback",
        )
    finally:
        paths._PINNED_DIR = old
        if fallback_scope is not None:
            shutil.rmtree(fallback_scope, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
    print("PASS newer_fallback_wins_over_stale_primary")


def test_fallback_path_is_scoped_by_vault_and_workspace():
    root = Path(tempfile.mkdtemp(prefix="codex_session_fallback_scope_"))
    vault_a = root / "vault-a"
    vault_b = root / "vault-b"
    project_a = root / "workspace-a"
    project_b = root / "workspace-b"
    old = paths._PINNED_DIR
    try:
        paths._PINNED_DIR = vault_a
        a1 = codex_session._fallback_marker_path(project_a)
        a2 = codex_session._fallback_marker_path(project_b)
        paths._PINNED_DIR = vault_b
        b1 = codex_session._fallback_marker_path(project_a)
        _assert(a1 != a2, "different workspaces must not share a fallback marker")
        _assert(a1 != b1, "different vaults must not share a fallback marker")
        _assert(
            a1.name == a2.name == b1.name == codex_session.MARKER_FILE,
            "fallback must preserve the marker filename",
        )
    finally:
        paths._PINNED_DIR = old
        shutil.rmtree(root, ignore_errors=True)
    print("PASS fallback_path_is_scoped_by_vault_and_workspace")


def test_fallback_refuses_symlinked_scope_directory():
    if codex_session._current_uid() is None:
        return
    root = Path(tempfile.mkdtemp(prefix="codex_session_fallback_symlink_"))
    project = root / "workspace"
    project.mkdir()
    blocked_vault = root / "blocked-vault"
    blocked_vault.write_text("not a directory", encoding="utf-8")
    attacker = root / "attacker"
    attacker.mkdir()
    old = paths._PINNED_DIR
    scope = None
    try:
        paths._PINNED_DIR = blocked_vault
        scope = codex_session._fallback_marker_path(project).parents[2]
        try:
            scope.symlink_to(attacker, target_is_directory=True)
        except OSError:
            return  # Symlinks may require elevation on Windows.
        try:
            codex_session.write_marker(project, "must-not-follow")
        except PermissionError:
            pass
        else:
            raise AssertionError("fallback must reject a symlinked scope directory")
        _assert(
            not list(attacker.rglob(codex_session.MARKER_FILE)),
            "fallback must not write through a symlinked scope directory",
        )
    finally:
        paths._PINNED_DIR = old
        if scope is not None:
            try:
                scope.unlink()
            except OSError:
                pass
        shutil.rmtree(root, ignore_errors=True)
    print("PASS fallback_refuses_symlinked_scope_directory")


def test_fallback_fails_closed_without_provable_os_ownership():
    root = Path(tempfile.mkdtemp(prefix="codex_session_no_owner_"))
    project = root / "workspace"
    project.mkdir()
    blocked_vault = root / "blocked-vault"
    blocked_vault.write_text("not a directory", encoding="utf-8")
    old_pin = paths._PINNED_DIR
    old_uid = codex_session._current_uid
    fallback_scope = None
    try:
        paths._PINNED_DIR = blocked_vault
        fallback_scope = codex_session._fallback_marker_path(project).parents[2]
        codex_session._current_uid = lambda: None
        try:
            codex_session.write_marker(project, "must-fail-closed")
        except PermissionError as exc:
            _assert("cannot prove" in str(exc), exc)
        else:
            raise AssertionError(
                "temp fallback must fail closed without provable OS ownership"
            )
        _assert(
            not codex_session._fallback_marker_path(project).exists(),
            "unsupported fallback must not write a marker",
        )
    finally:
        codex_session._current_uid = old_uid
        paths._PINNED_DIR = old_pin
        if fallback_scope is not None:
            shutil.rmtree(fallback_scope, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
    print("PASS fallback_fails_closed_without_provable_os_ownership")


if __name__ == "__main__":
    test_write_and_read_session_marker()
    test_invalidate_marker_prevents_stale_session_inheritance()
    test_read_session_marker_missing_or_invalid_returns_none()
    test_pinned_vault_keeps_workspace_markers_distinct()
    test_readonly_primary_uses_private_temp_fallback()
    test_marker_read_uses_freshness_then_path_precedence()
    test_newer_fallback_wins_over_stale_primary()
    test_fallback_path_is_scoped_by_vault_and_workspace()
    test_fallback_refuses_symlinked_scope_directory()
    test_fallback_fails_closed_without_provable_os_ownership()
    print("\nAll codex_session tests pass.")
