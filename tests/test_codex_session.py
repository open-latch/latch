"""Unit tests for Codex hook-to-MCP session handoff helpers."""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import codex_session  # noqa: E402
import paths  # noqa: E402
import project_config  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


_MISSING_ENV = object()


def _test_vault(name: str) -> Path:
    test_root = paths.validated_test_root()
    if test_root is None:
        raise AssertionError("pytest isolation is required")
    vaults = test_root / "vaults"
    vaults.mkdir(parents=True, exist_ok=True)
    return vaults / name


def _set_test_pin(vault: Path):
    previous = os.environ.get("LATCH_KB_DIR", _MISSING_ENV)
    os.environ["LATCH_KB_DIR"] = str(vault)
    return previous


def _restore_test_pin(previous) -> None:
    if previous is _MISSING_ENV:
        os.environ.pop("LATCH_KB_DIR", None)
    else:
        os.environ["LATCH_KB_DIR"] = previous


def _bind_shared(project: Path) -> None:
    project_config.create_scope(project, policy=project_config.POLICY_SHARED)
    project_config.authorize_scope(project)


def _force_primary_write_failure():
    previous = codex_session._write_primary_marker

    def fail(_path: Path, _text: str) -> None:
        raise PermissionError("forced read-only primary")

    codex_session._write_primary_marker = fail
    return previous


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
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS read_session_marker_missing_or_invalid_returns_none")


def test_pinned_vault_keeps_workspace_markers_distinct():
    shared = _test_vault("codex_session_shared_vault")
    project_a = Path(tempfile.mkdtemp(prefix="codex_session_project_a_"))
    project_b = Path(tempfile.mkdtemp(prefix="codex_session_project_b_"))
    old = _set_test_pin(shared)
    try:
        shared.mkdir(exist_ok=True)
        _bind_shared(project_a)
        _bind_shared(project_b)
        marker_a = codex_session.write_marker(project_a, "session-a")
        marker_b = codex_session.write_marker(project_b, "session-b")
        _assert(marker_a != marker_b, "pinned vault markers must be keyed by workspace")
        _assert(codex_session.read_session_id(project_a) == "session-a", marker_a)
        _assert(codex_session.read_session_id(project_b) == "session-b", marker_b)
    finally:
        _restore_test_pin(old)
        shutil.rmtree(project_a, ignore_errors=True)
        shutil.rmtree(project_b, ignore_errors=True)
    print("PASS pinned_vault_keeps_workspace_markers_distinct")


def test_readonly_primary_uses_private_temp_fallback():
    if codex_session._current_uid() is None:
        return
    root = Path(tempfile.mkdtemp(prefix="codex_session_fallback_"))
    project = root / "workspace"
    project.mkdir()
    vault = _test_vault(f"fallback-{root.name}")
    vault.mkdir()
    old = _set_test_pin(vault)
    original_primary = None
    fallback_scope = None
    try:
        _bind_shared(project)
        expected_fallback = codex_session._fallback_marker_path(project)
        fallback_scope = expected_fallback.parents[2]
        original_primary = _force_primary_write_failure()
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
        if original_primary is not None:
            codex_session._write_primary_marker = original_primary
        _restore_test_pin(old)
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
    vault = _test_vault(f"precedence-{root.name}")
    vault.mkdir()
    old = _set_test_pin(vault)
    original_primary = None
    fallback_scope = None
    try:
        _bind_shared(project)
        original_primary = _force_primary_write_failure()
        fallback = codex_session.write_marker(project, "fallback-session")
        fallback_scope = fallback.parents[2]
        _assert(fallback == codex_session._fallback_marker_path(project), fallback)

        legacy = codex_session._legacy_marker_path(project)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(
            json.dumps(_payload(project, "legacy-session")) + "\n",
            encoding="utf-8",
        )
        _assert(
            codex_session.read_session_id(project) == "fallback-session",
            "newer fallback must win over older legacy",
        )

        codex_session._write_primary_marker = original_primary
        original_primary = None
        primary = codex_session.write_marker(project, "primary-session")
        _assert(primary == codex_session.marker_path(project), primary)
        _assert(
            codex_session.read_session_id(project) == "primary-session",
            "primary must win over fallback",
        )
    finally:
        if original_primary is not None:
            codex_session._write_primary_marker = original_primary
        _restore_test_pin(old)
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
    vault = _test_vault(f"freshest-{root.name}")
    vault.mkdir()
    old = _set_test_pin(vault)
    fallback_scope = None
    try:
        _bind_shared(project)
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
        _restore_test_pin(old)
        if fallback_scope is not None:
            shutil.rmtree(fallback_scope, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
    print("PASS newer_fallback_wins_over_stale_primary")


def test_fallback_path_is_scoped_by_vault_and_workspace():
    root = Path(tempfile.mkdtemp(prefix="codex_session_fallback_scope_"))
    vault_a = _test_vault(f"scope-a-{root.name}")
    vault_b = _test_vault(f"scope-b-{root.name}")
    project_a = root / "workspace-a"
    project_b = root / "workspace-b"
    project_a.mkdir()
    project_b.mkdir()
    old = _set_test_pin(vault_a)
    try:
        vault_a.mkdir()
        vault_b.mkdir()
        _bind_shared(project_a)
        _bind_shared(project_b)
        a1 = codex_session._fallback_marker_path(project_a)
        a2 = codex_session._fallback_marker_path(project_b)
        _set_test_pin(vault_b)
        project_config.reauthorize_shared_scope(project_a)
        b1 = codex_session._fallback_marker_path(project_a)
        _assert(a1 != a2, "different workspaces must not share a fallback marker")
        _assert(a1 != b1, "different vaults must not share a fallback marker")
        _assert(
            a1.name == a2.name == b1.name == codex_session.MARKER_FILE,
            "fallback must preserve the marker filename",
        )
    finally:
        _restore_test_pin(old)
        shutil.rmtree(root, ignore_errors=True)
    print("PASS fallback_path_is_scoped_by_vault_and_workspace")


def test_fallback_path_does_not_require_home_without_provable_owner():
    root = Path(tempfile.mkdtemp(prefix="codex_session_fallback_no_home_"))
    project = root / "workspace"
    project.mkdir()
    try:
        with (
            mock.patch.object(codex_session, "_current_uid", return_value=None),
            mock.patch.object(
                codex_session.Path,
                "home",
                side_effect=RuntimeError("home unavailable"),
            ),
        ):
            fallback = codex_session._fallback_marker_path(project)
        _assert(
            fallback.name == codex_session.MARKER_FILE,
            "fallback path calculation must remain available without a home",
        )
        _assert(
            Path(tempfile.gettempdir()) in fallback.parents,
            f"fallback path must remain below tempfile.gettempdir(): {fallback}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("PASS fallback_path_does_not_require_home_without_provable_owner")


def test_fallback_refuses_symlinked_scope_directory():
    if codex_session._current_uid() is None:
        return
    root = Path(tempfile.mkdtemp(prefix="codex_session_fallback_symlink_"))
    project = root / "workspace"
    project.mkdir()
    vault = _test_vault(f"symlink-vault-{root.name}")
    vault.mkdir()
    attacker = root / "attacker"
    attacker.mkdir()
    old = _set_test_pin(vault)
    original_primary = None
    scope = None
    try:
        _bind_shared(project)
        scope = codex_session._fallback_marker_path(project).parents[2]
        try:
            scope.symlink_to(attacker, target_is_directory=True)
        except OSError:
            return  # Symlinks may require elevation on Windows.
        original_primary = _force_primary_write_failure()
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
        if original_primary is not None:
            codex_session._write_primary_marker = original_primary
        _restore_test_pin(old)
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
    vault = _test_vault(f"no-owner-vault-{root.name}")
    vault.mkdir()
    old_pin = _set_test_pin(vault)
    old_uid = codex_session._current_uid
    original_primary = None
    fallback_scope = None
    try:
        _bind_shared(project)
        fallback_scope = codex_session._fallback_marker_path(project).parents[2]
        original_primary = _force_primary_write_failure()
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
        if original_primary is not None:
            codex_session._write_primary_marker = original_primary
        _restore_test_pin(old_pin)
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
    test_fallback_path_does_not_require_home_without_provable_owner()
    test_fallback_refuses_symlinked_scope_directory()
    test_fallback_fails_closed_without_provable_os_ownership()
    print("\nAll codex_session tests pass.")
