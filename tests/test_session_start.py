"""Focused tests for the now-silent Claude SessionStart hook."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("compatibility_scope_env")

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

import db  # noqa: E402
import claude_md_sync  # noqa: E402
import paths  # noqa: E402
import project_config  # noqa: E402
import session_start  # noqa: E402


def _healthy_guards(monkeypatch) -> None:
    monkeypatch.setattr(session_start, "is_in_compact", lambda: False)
    monkeypatch.setattr(session_start, "is_unlatched_mode", lambda *_args: False)
    monkeypatch.setattr(session_start, "is_disabled", lambda *_args: False)


def test_healthy_startup_is_silent_and_never_opens_db(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    transcript = tmp_path / "conversation.jsonl"
    monkeypatch.setattr(
        session_start,
        "read_hook_input",
        lambda: {
            "cwd": str(tmp_path),
            "session_id": "claude-session",
            "transcript_path": str(transcript),
        },
    )
    monkeypatch.setattr(
        session_start, "_auto_sync_claude_md", lambda _cwd, *_args: "unchanged"
    )

    monkeypatch.setattr(
        db,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("silent startup must not open the DB")
        ),
    )

    assert session_start.main() == 0
    assert capsys.readouterr().out == ""


def test_startup_without_session_id_also_never_opens_db(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    monkeypatch.setattr(
        session_start,
        "read_hook_input",
        lambda: {"cwd": str(tmp_path)},
    )
    monkeypatch.setattr(
        session_start, "_auto_sync_claude_md", lambda _cwd, *_args: "skipped"
    )
    monkeypatch.setattr(
        db,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("silent startup must not open the DB")
        ),
    )

    assert session_start.main() == 0
    notice = json.loads(capsys.readouterr().out)["hookSpecificOutput"][
        "additionalContext"
    ]
    assert "could not safely bind" in notice
    assert "fresh agent task" in notice


def test_wiring_repair_is_visible_without_a_routine_brief(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    monkeypatch.setattr(
        session_start,
        "read_hook_input",
        lambda: {"cwd": str(tmp_path), "session_id": "session"},
    )
    monkeypatch.setattr(
        session_start, "_auto_sync_claude_md", lambda _cwd, *_args: "synced"
    )

    assert session_start.main() == 0
    output = json.loads(capsys.readouterr().out)
    notice = output["hookSpecificOutput"]["additionalContext"]
    assert "repaired older CLAUDE.md project wiring once" in notice
    assert "CLAUDE.md.latchbak" in notice
    assert "session brief" not in notice


def test_wiring_error_log_uses_payload_project_and_binding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    logged = []
    monkeypatch.setattr(
        claude_md_sync,
        "sync_if_outdated",
        lambda _target: (_ for _ in ()).throw(OSError("sync failed")),
    )
    monkeypatch.setattr(
        session_start,
        "log",
        lambda message, project_path=None, **kwargs: logged.append(
            (message, project_path, kwargs)
        ),
    )

    assert session_start._auto_sync_claude_md(str(tmp_path), "binding-rev") == "error"
    assert logged == [
        (
            "claude_md auto-sync skipped: sync failed",
            str(tmp_path),
            {"expected_revision": "binding-rev"},
        )
    ]


def test_unlatched_startup_remains_visible_and_skips_db(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(session_start, "is_in_compact", lambda: False)
    monkeypatch.setattr(session_start, "is_unlatched_mode", lambda *_args: True)
    monkeypatch.setattr(
        session_start,
        "read_hook_input",
        lambda: {"cwd": str(tmp_path), "session_id": "unlatched-session"},
    )
    monkeypatch.setattr(
        db,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not open DB")
        ),
    )

    assert session_start.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert "LATCH UNLATCHED MODE ACTIVE" in output["systemMessage"]
    notice = output["hookSpecificOutput"]["additionalContext"]
    assert notice.startswith("# latch is unlatched")
    assert "automatic retrieval" in notice
    assert "Run `/latch` to re-latch" in notice


def test_disabled_and_compactor_startup_are_silent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        session_start,
        "read_hook_input",
        lambda: (_ for _ in ()).throw(AssertionError("must not read payload")),
    )

    monkeypatch.setattr(session_start, "is_in_compact", lambda: True)
    assert session_start.main() == 0
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(session_start, "is_in_compact", lambda: False)
    monkeypatch.setattr(session_start, "is_unlatched_mode", lambda *_args: False)
    monkeypatch.setattr(session_start, "is_disabled", lambda *_args: True)
    monkeypatch.setattr(
        session_start,
        "read_hook_input",
        lambda: {"cwd": str(tmp_path), "session_id": "disabled-session"},
    )
    assert session_start.main() == 0
    assert capsys.readouterr().out == ""


def test_resumed_unlatched_or_locked_session_cannot_bind_new_project_kb(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)
    for initial in ("unlatched", "locked"):
        project = tmp_path / initial
        project.mkdir()
        (project / ".git").mkdir()
        sid = f"{initial}-old-task"
        kb_a = paths.validated_test_root() / "vaults" / f"session-{initial}-a"
        kb_b = paths.validated_test_root() / "vaults" / f"session-{initial}-b"
        kb_a.mkdir(parents=True, exist_ok=True)
        kb_b.mkdir(parents=True, exist_ok=True)
        project_config.mark_kb_target(kb_a)
        project_config.mark_kb_target(kb_b)
        if initial == "unlatched":
            project_config.write_binding(
                project,
                mode=project_config.MODE_UNLATCHED,
                kb_dir=kb_a,
            )

        monkeypatch.setattr(session_start, "is_in_compact", lambda: False)
        monkeypatch.setattr(session_start, "is_disabled", lambda *_args: False)
        monkeypatch.setattr(
            session_start,
            "read_hook_input",
            lambda project=project, sid=sid: {
                "cwd": str(project),
                "session_id": sid,
            },
        )
        monkeypatch.setattr(
            session_start,
            "_auto_sync_claude_md",
            lambda _cwd, *_args: "unchanged",
        )

        assert session_start.main() == 0
        capsys.readouterr()
        if initial == "unlatched":
            assert project_config.current_session_revision(project, sid) is None
            project_config.set_scope_mode(project, project_config.MODE_LATCHED)
            project_config.repin_private_scope(project, kb_b)
        else:
            assert project_config.current_session_revision(project, sid) is None
            project_config.create_scope(
                project,
                policy=project_config.POLICY_PRIVATE,
            )
            project_config.authorize_scope(project, kb_dir=kb_b)
        monkeypatch.setattr(
            session_start,
            "_auto_sync_claude_md",
            lambda _cwd, *_args: (_ for _ in ()).throw(
                AssertionError("stale task changed project wiring")
            ),
        )
        assert session_start.main() == 0
        output = json.loads(capsys.readouterr().out)
        notice = output["hookSpecificOutput"]["additionalContext"]
        assert "fresh agent task" in notice
        assert not (kb_b / "kb.db").exists()
