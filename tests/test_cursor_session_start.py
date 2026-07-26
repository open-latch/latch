"""Focused tests for the silent Cursor SessionStart adapter."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

import cursor_session_start as css  # noqa: E402
import cursor_wiring  # noqa: E402
import db  # noqa: E402


def _healthy_guards(monkeypatch) -> None:
    monkeypatch.setattr(css, "is_in_compact", lambda: False)
    monkeypatch.setattr(css, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(css, "is_disabled", lambda: False)


def test_healthy_cursor_startup_is_silent_and_preserves_mechanics(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    transcript = tmp_path / "conversation.jsonl"
    gate_calls: list[tuple[str, str | None]] = []
    marker_calls: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        css,
        "read_hook_input",
        lambda: {
            "workspaceRoot": str(tmp_path),
            "conversation_id": "cursor-conversation",
            "transcript_path": str(transcript),
        },
    )
    monkeypatch.setattr(
        css.cursor_wiring,
        "repair_project",
        lambda _cwd: cursor_wiring.RepairResult("unchanged"),
    )
    monkeypatch.setattr(
        css.cursor_gate_state,
        "initialize_session",
        lambda cwd, sid: gate_calls.append((cwd, sid)),
    )
    monkeypatch.setattr(
        css.cursor_session,
        "write_marker",
        lambda cwd, sid, transcript_path=None: marker_calls.append(
            (cwd, sid, transcript_path)
        ),
    )
    monkeypatch.setattr(
        db,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Cursor SessionStart must not open the DB")
        ),
    )

    assert css.main() == 0
    assert capsys.readouterr().out == ""
    assert gate_calls == [(str(tmp_path), "cursor-conversation")]
    assert marker_calls == [
        (str(tmp_path), "cursor-conversation", str(transcript)),
    ]


def test_cursor_startup_never_opens_db_and_preserves_marker(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    marker_calls: list[str] = []
    monkeypatch.setattr(
        css,
        "read_hook_input",
        lambda: {
            "workspaceRoot": str(tmp_path),
            "conversation_id": "cursor-conversation",
        },
    )
    monkeypatch.setattr(
        css.cursor_wiring,
        "repair_project",
        lambda _cwd: cursor_wiring.RepairResult("unchanged"),
    )
    monkeypatch.setattr(
        css.cursor_gate_state,
        "initialize_session",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        css.cursor_session,
        "write_marker",
        lambda _cwd, sid, transcript_path=None: marker_calls.append(sid),
    )
    monkeypatch.setattr(
        db,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("sensitive db failure")
        ),
    )

    assert css.main() == 0
    assert capsys.readouterr().out == ""
    assert marker_calls == ["cursor-conversation"]


def test_cursor_wiring_notice_is_the_only_healthy_output(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    monkeypatch.setattr(
        css,
        "read_hook_input",
        lambda: {
            "workspaceRoot": str(tmp_path),
            "conversation_id": "cursor-conversation",
        },
    )
    monkeypatch.setattr(
        css.cursor_wiring,
        "repair_project",
        lambda _cwd: cursor_wiring.RepairResult(
            "synced",
            "_↻ Latch repaired older Cursor project wiring once._",
        ),
    )
    monkeypatch.setattr(
        css.cursor_gate_state,
        "initialize_session",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        css.cursor_session,
        "write_marker",
        lambda *_args, **_kwargs: None,
    )

    assert css.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "additional_context": "_↻ Latch repaired older Cursor project wiring once._"
    }


def test_cursor_missing_conversation_id_is_visible_degradation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    monkeypatch.setattr(
        css,
        "read_hook_input",
        lambda: {"workspaceRoot": str(tmp_path)},
    )
    monkeypatch.setattr(
        css.cursor_wiring,
        "repair_project",
        lambda _cwd: cursor_wiring.RepairResult("unchanged"),
    )
    monkeypatch.setattr(
        css.cursor_gate_state,
        "initialize_session",
        lambda *_args, **_kwargs: None,
    )

    assert css.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert "current-session attribution" in output["additional_context"]


def test_cursor_gate_state_failure_is_visible_but_marker_still_writes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    marker_calls: list[str] = []
    monkeypatch.setattr(
        css,
        "read_hook_input",
        lambda: {
            "workspaceRoot": str(tmp_path),
            "conversation_id": "cursor-conversation",
        },
    )
    monkeypatch.setattr(
        css.cursor_wiring,
        "repair_project",
        lambda _cwd: cursor_wiring.RepairResult("unchanged"),
    )
    monkeypatch.setattr(
        css.cursor_gate_state,
        "initialize_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("private gate-state detail")
        ),
    )
    monkeypatch.setattr(
        css.cursor_session,
        "write_marker",
        lambda _cwd, sid, transcript_path=None: marker_calls.append(sid),
    )

    assert css.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert "could not complete silent session setup" in output["additional_context"]
    assert "private gate-state detail" not in output["additional_context"]
    assert marker_calls == ["cursor-conversation"]


def test_cursor_marker_failure_is_visible_after_gate_state_initializes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    gate_calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        css,
        "read_hook_input",
        lambda: {
            "workspaceRoot": str(tmp_path),
            "conversation_id": "cursor-conversation",
        },
    )
    monkeypatch.setattr(
        css.cursor_wiring,
        "repair_project",
        lambda _cwd: cursor_wiring.RepairResult("unchanged"),
    )
    monkeypatch.setattr(
        css.cursor_gate_state,
        "initialize_session",
        lambda cwd, sid: gate_calls.append((cwd, sid)),
    )
    monkeypatch.setattr(
        css.cursor_session,
        "write_marker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("private marker detail")
        ),
    )

    assert css.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert "could not complete silent session setup" in output["additional_context"]
    assert "private marker detail" not in output["additional_context"]
    assert gate_calls == [(str(tmp_path), "cursor-conversation")]
