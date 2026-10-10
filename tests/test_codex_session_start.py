"""Focused tests for the silent Codex SessionStart adapter."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

from latch.hooks import codex_session_start as css  # noqa: E402
from latch.store import db  # noqa: E402


def _healthy_guards(monkeypatch) -> None:
    monkeypatch.setattr(css, "is_in_compact", lambda: False)
    monkeypatch.setattr(css, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(css, "is_disabled", lambda: False)
    monkeypatch.setattr(
        css,
        "_auto_repair_codex_wiring",
        lambda _cwd: css.codex_wiring.RepairResult("unchanged"),
    )


def test_codex_payload_helpers(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", "env-thread")
    payload = {"workspaceRoot": "/repo", "threadId": "payload-thread"}
    assert css.codex_project_cwd(payload) == "/repo"
    assert css.codex_session_id(payload) == "payload-thread"
    assert css.codex_session_id({}) == "env-thread"


def test_auto_repair_wrapper_fails_open(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        css.codex_wiring,
        "repair_project",
        lambda _cwd: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    result = css._auto_repair_codex_wiring(str(tmp_path))
    assert result.action == "error"
    assert "task will continue" in (result.notice or "")


def test_healthy_codex_startup_is_silent_and_preserves_attribution(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    transcript = tmp_path / "thread.jsonl"
    marker_calls: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        css,
        "read_hook_input",
        lambda: {
            "cwd": str(tmp_path),
            "threadId": "codex-thread",
            "transcript_path": str(transcript),
        },
    )
    monkeypatch.setattr(
        css.codex_session,
        "write_marker",
        lambda cwd, sid, transcript_path=None: marker_calls.append(
            (cwd, sid, transcript_path)
        ) or (tmp_path / "marker.json"),
    )
    monkeypatch.setattr(
        db,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Codex SessionStart must not open the DB")
        ),
    )

    assert css.main() == 0
    assert capsys.readouterr().out == ""
    assert marker_calls == [
        (str(tmp_path), "codex-thread", str(transcript)),
    ]


def test_readonly_vault_needs_no_db_write_and_marker_survives(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    marker_calls: list[str] = []
    monkeypatch.setattr(
        css,
        "read_hook_input",
        lambda: {"cwd": str(tmp_path), "threadId": "readonly-thread"},
    )
    monkeypatch.setattr(
        css.codex_session,
        "write_marker",
        lambda _cwd, sid, transcript_path=None: marker_calls.append(sid),
    )
    monkeypatch.setattr(
        db,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Codex SessionStart must not open the DB")
        ),
    )

    assert css.main() == 0
    assert capsys.readouterr().out == ""
    assert marker_calls == ["readonly-thread"]


def test_marker_failure_warns_without_opening_db(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    monkeypatch.setattr(
        css,
        "read_hook_input",
        lambda: {"cwd": str(tmp_path), "threadId": "db-still-works"},
    )
    monkeypatch.setattr(
        css.codex_session,
        "write_marker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("sensitive marker detail")
        ),
    )
    monkeypatch.setattr(
        db,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Codex SessionStart must not open the DB")
        ),
    )

    assert css.main() == 0
    output = json.loads(capsys.readouterr().out)
    notice = output["hookSpecificOutput"]["additionalContext"]
    assert "could not complete silent session setup" in notice
    assert "sensitive marker detail" not in notice


def test_missing_session_id_invalidates_stale_marker_and_warns(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    invalidated: list[str] = []
    monkeypatch.setattr(
        css,
        "read_hook_input",
        lambda: {"cwd": str(tmp_path)},
    )
    monkeypatch.setattr(
        css.codex_session,
        "write_marker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing session id must not write attribution")
        ),
    )
    monkeypatch.setattr(
        css.codex_session,
        "invalidate_marker",
        lambda cwd: invalidated.append(cwd),
    )
    monkeypatch.setattr(
        db,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Codex SessionStart must not open the DB")
        ),
    )

    assert css.main() == 0
    output = json.loads(capsys.readouterr().out)
    notice = output["hookSpecificOutput"]["additionalContext"]
    assert "could not complete silent session setup" in notice
    assert invalidated == [str(tmp_path)]


def test_agents_wiring_repair_notice_is_visible_without_brief(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    monkeypatch.setattr(
        css,
        "read_hook_input",
        lambda: {"cwd": str(tmp_path), "threadId": "thread"},
    )
    monkeypatch.setattr(
        css.codex_session,
        "write_marker",
        lambda *_args, **_kwargs: tmp_path / "marker.json",
    )
    monkeypatch.setattr(
        css,
        "_auto_repair_codex_wiring",
        lambda _cwd: css.codex_wiring.RepairResult(
            "synced",
            "_↻ Latch repaired older Codex wiring once; backups were kept. "
            "Restart or open a new task._",
            restart_required=True,
        ),
    )

    assert css.main() == 0
    output = json.loads(capsys.readouterr().out)
    notice = output["hookSpecificOutput"]["additionalContext"]
    assert "repaired older Codex wiring once" in notice
    assert "Restart or open a new task" in notice
    assert "CLAUDE.md" not in notice
    assert "session brief" not in notice
