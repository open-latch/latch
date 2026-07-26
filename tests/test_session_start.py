"""Focused tests for the now-silent Claude SessionStart hook."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

import db  # noqa: E402
import session_start  # noqa: E402


def _healthy_guards(monkeypatch) -> None:
    monkeypatch.setattr(session_start, "is_in_compact", lambda: False)
    monkeypatch.setattr(session_start, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(session_start, "is_disabled", lambda: False)


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
    monkeypatch.setattr(session_start, "_auto_sync_claude_md", lambda _cwd: "unchanged")

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
    monkeypatch.setattr(session_start, "_auto_sync_claude_md", lambda _cwd: "skipped")
    monkeypatch.setattr(
        db,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("silent startup must not open the DB")
        ),
    )

    assert session_start.main() == 0
    assert capsys.readouterr().out == ""


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
    monkeypatch.setattr(session_start, "_auto_sync_claude_md", lambda _cwd: "synced")

    assert session_start.main() == 0
    output = json.loads(capsys.readouterr().out)
    notice = output["hookSpecificOutput"]["additionalContext"]
    assert "repaired older CLAUDE.md project wiring once" in notice
    assert "CLAUDE.md.latchbak" in notice
    assert "session brief" not in notice


def test_unlatched_startup_remains_visible_and_skips_db(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(session_start, "is_in_compact", lambda: False)
    monkeypatch.setattr(session_start, "is_unlatched_mode", lambda: True)
    monkeypatch.setattr(
        session_start,
        "read_hook_input",
        lambda: (_ for _ in ()).throw(AssertionError("must not read payload")),
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
    assert "Run `/unlatch` to re-latch" in notice


def test_disabled_and_compactor_startup_are_silent(
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
    monkeypatch.setattr(session_start, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(session_start, "is_disabled", lambda: True)
    assert session_start.main() == 0
    assert capsys.readouterr().out == ""
