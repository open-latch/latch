"""SessionStart exposes scope state without opening or crossing a KB."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

import paths
import project_config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "hooks"))
import session_start


@pytest.fixture
def session_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> tuple[Path, Path]:
    test_root = paths.validated_test_root()
    assert test_root is not None
    control = test_root / "session-scope-tests" / tmp_path.name
    home = tmp_path / "latch-home"
    home.mkdir()
    monkeypatch.setenv(project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.delenv("CLAUDE_KB_HOME", raising=False)
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)

    root = tmp_path / "client"
    root.mkdir()
    vault = test_root / "vaults" / f"session-scope-{tmp_path.name}"
    vault.mkdir(parents=True)
    project_config.create_scope(root, policy=project_config.POLICY_PRIVATE)
    project_config.authorize_scope(root, kb_dir=vault)

    monkeypatch.setattr(session_start, "is_in_compact", lambda: False)
    monkeypatch.setattr(session_start, "is_disabled", lambda *_args: False)
    monkeypatch.setattr(
        session_start,
        "_auto_sync_claude_md",
        lambda _cwd, *_args: "unchanged",
    )
    monkeypatch.setattr(session_start, "log", lambda *_args, **_kwargs: None)
    return root, vault


def test_latched_session_records_exact_scope_without_opening_db(
    session_scope: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, vault = session_scope
    monkeypatch.setattr(
        session_start,
        "read_hook_input",
        lambda: {"cwd": str(root), "session_id": "fresh-task"},
    )

    assert session_start.main() == 0
    assert capsys.readouterr().out == ""
    assert project_config.current_session_revision(root, "fresh-task") == (
        project_config.resolve(root).revision
    )
    assert not (vault / "kb.db").exists()


def test_unlatched_session_is_visible_and_cannot_revive_after_relatch(
    session_scope: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, vault = session_scope
    project_config.set_scope_mode(root, project_config.MODE_UNLATCHED)
    monkeypatch.setattr(
        session_start,
        "read_hook_input",
        lambda: {"cwd": str(root), "session_id": "off-task"},
    )

    assert session_start.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["additionalContext"].startswith(
        "# latch is unlatched"
    )
    assert project_config.current_session_revision(root, "off-task") is None
    assert not (vault / "kb.db").exists()

    project_config.set_scope_mode(root, project_config.MODE_LATCHED)
    assert project_config.current_session_revision(root, "off-task") is None
    assert project_config.record_session_binding(root, "fresh-after-off") == (
        project_config.resolve(root).revision
    )


def test_unauthorized_copied_scope_is_visibly_locked_without_kb_access(
    session_scope: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    root, vault = session_scope
    clone = tmp_path / "clone"
    clone.mkdir()
    shutil.copytree(root / ".latch", clone / ".latch")
    monkeypatch.setattr(
        session_start,
        "read_hook_input",
        lambda: {"cwd": str(clone), "session_id": "copied-task"},
    )

    assert session_start.main() == 0
    output = json.loads(capsys.readouterr().out)
    notice = output["hookSpecificOutput"]["additionalContext"]
    assert notice.startswith("# latch is locked")
    assert str(clone) in notice
    assert "not authorized" in notice
    assert project_config.current_session_revision(clone, "copied-task") is None
    assert not (vault / "kb.db").exists()

    project_config.authorize_scope(clone, kb_dir=vault)
    assert project_config.current_session_revision(clone, "copied-task") is None
    assert project_config.record_session_binding(clone, "fresh-clone-task") == (
        project_config.resolve(clone).revision
    )


def test_explicit_scope_without_session_id_fails_closed(
    session_scope: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, vault = session_scope
    monkeypatch.setattr(
        session_start,
        "read_hook_input",
        lambda: {"cwd": str(root)},
    )

    assert session_start.main() == 0
    notice = json.loads(capsys.readouterr().out)["hookSpecificOutput"][
        "additionalContext"
    ]
    assert "fresh agent task" in notice
    assert not (vault / "kb.db").exists()


def test_cursor_backend_environment_is_an_agent_context() -> None:
    assert project_config.is_agent_context(
        {"CURSOR_KB_COMPACTOR_BACKEND": "cursor"}
    )


def test_duplicate_session_start_in_same_scope_stays_idempotent(
    session_scope: tuple[Path, Path],
) -> None:
    root, _vault = session_scope
    first = project_config.record_session_binding(root, "duplicate-start-task")
    assert first is not None
    assert project_config.record_session_binding(root, "duplicate-start-task") == first


def test_racing_first_bindings_cannot_both_claim_one_session(
    session_scope: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The one-way session fence must hold even when two roots record the same
    session id concurrently: exactly one exclusive create wins and the loser
    fails closed instead of silently overwriting the receipt."""
    import os

    root, _vault = session_scope
    test_root = paths.validated_test_root()
    assert test_root is not None
    other_root = tmp_path / "other-client"
    other_root.mkdir()
    other_vault = test_root / "vaults" / f"race-{tmp_path.name}"
    other_vault.mkdir(parents=True)
    project_config.create_scope(other_root, policy=project_config.POLICY_PRIVATE)
    project_config.authorize_scope(other_root, kb_dir=other_vault)

    session = "race-shared-session"
    receipt = project_config._session_path(session)
    original_open = os.open
    state = {"raced": False}

    def racing_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        if (
            not state["raced"]
            and isinstance(path, (str, os.PathLike))
            and Path(path) == receipt
            and flags & os.O_EXCL
        ):
            state["raced"] = True
            # The competitor's SessionStart in the other root lands between
            # this caller's decision to bind and its exclusive create.
            assert (
                project_config.record_session_binding(other_root, session)
                is not None
            )
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(
        project_config.ProjectConfigError, match="older Latch scope"
    ):
        project_config.record_session_binding(root, session)
    monkeypatch.setattr(os, "open", original_open)

    # The winner's receipt survives byte-exact; the loser gained no authority.
    assert project_config.current_session_revision(other_root, session) == (
        project_config.resolve(other_root).revision
    )
    assert project_config.current_session_revision(root, session) is None
