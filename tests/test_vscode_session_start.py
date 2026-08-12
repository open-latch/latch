"""Focused tests for the silent VS Code SessionStart adapter."""
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
import agents_md_sync  # noqa: E402
import vscode_session_start as vss  # noqa: E402


def _healthy_guards(monkeypatch) -> None:
    monkeypatch.setattr(vss, "is_in_compact", lambda: False)
    monkeypatch.setattr(vss, "is_unlatched_mode", lambda *_args: False)
    monkeypatch.setattr(vss, "is_disabled", lambda *_args: False)


def test_healthy_vscode_startup_is_silent_but_repairs_agents_wiring(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    transcript = tmp_path / "conversation.jsonl"
    monkeypatch.setattr(
        vss,
        "read_hook_input",
        lambda: {
            "workspaceRoot": str(tmp_path),
            "sessionId": "vscode-session",
            "transcript_path": str(transcript),
        },
    )
    monkeypatch.setattr(
        vss, "_auto_sync_agents_md", lambda _cwd, *_args: "unchanged"
    )
    monkeypatch.setattr(
        db,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("VS Code SessionStart must not open the DB")
        ),
    )

    assert vss.main() == 0
    assert capsys.readouterr().out == ""


def test_vscode_wiring_repair_is_visible_without_brief(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _healthy_guards(monkeypatch)
    monkeypatch.setattr(
        vss,
        "read_hook_input",
        lambda: {
            "workspaceRoot": str(tmp_path),
            "sessionId": "vscode-session",
        },
    )
    monkeypatch.setattr(
        vss, "_auto_sync_agents_md", lambda _cwd, *_args: "synced"
    )

    assert vss.main() == 0
    output = json.loads(capsys.readouterr().out)
    notice = output["hookSpecificOutput"]["additionalContext"]
    assert "repaired older AGENTS.md project wiring once" in notice
    assert "session brief" not in notice


def test_vscode_wiring_error_log_uses_payload_project_and_binding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    logged = []
    monkeypatch.setattr(
        agents_md_sync,
        "sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("sync failed")),
    )
    monkeypatch.setattr(
        vss,
        "log",
        lambda message, project_path=None, **kwargs: logged.append(
            (message, project_path, kwargs)
        ),
    )

    assert vss._auto_sync_agents_md(str(tmp_path), "binding-rev") == "error"
    assert logged == [
        (
            "agents_md auto-sync skipped: sync failed",
            str(tmp_path),
            {"expected_revision": "binding-rev"},
        )
    ]
