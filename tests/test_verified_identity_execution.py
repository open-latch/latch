"""Execution regressions for verified session/scope handoff into SQLite."""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "hooks"))

import compactor  # noqa: E402
import db  # noqa: E402
import gate  # noqa: E402
import kb_gate_cli  # noqa: E402
import maintenance  # noqa: E402
import paths  # noqa: E402
import project_config  # noqa: E402
import selfheal  # noqa: E402
import session_end  # noqa: E402
import stop as stop_hook  # noqa: E402
import user_prompt_submit as prompt_hook  # noqa: E402


def _private_scope(tmp_path: Path, session_id: str = "payload-session"):
    project = tmp_path / "client"
    project.mkdir()
    test_root = paths.validated_test_root()
    assert test_root is not None
    vault = test_root / "vaults" / f"identity-handoff-{uuid.uuid4()}"
    vault.mkdir(parents=True)
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)
    project_config.create_scope(project, policy=project_config.POLICY_PRIVATE)
    project_config.authorize_scope(project, kb_dir=vault)
    db.connect(str(project)).close()
    target = project_config.resolve(project)
    assert target.kb_dir == vault.resolve()
    assert project_config.record_session_binding(project, session_id) == target.revision
    return project, vault.resolve(), target, session_id


def _poison_ambient_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LATCH_ADAPTER", "claude")
    monkeypatch.setenv("LATCH_SESSION_ID", "ambient-wrong-session")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ambient-wrong-session-2")


def test_claude_hooks_use_payload_session_not_ambient_process_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, vault, target, sid = _private_scope(tmp_path)
    _poison_ambient_session(monkeypatch)

    monkeypatch.setattr(stop_hook, "is_unlatched_mode", lambda *_args: False)
    monkeypatch.setattr(stop_hook, "is_write_disabled", lambda *_args: False)
    monkeypatch.setattr(stop_hook, "is_in_compact", lambda: False)
    monkeypatch.setattr(
        stop_hook,
        "read_hook_input",
        lambda: {"cwd": str(project), "session_id": sid},
    )
    monkeypatch.setattr(
        stop_hook,
        "_cite_presence_check",
        lambda *_args, **_kwargs: None,
    )
    assert stop_hook.main() == 0
    conn = db.connect(
        str(project),
        expected_binding_revision=target.revision,
        expected_kb_dir=str(vault),
    )
    try:
        assert db.get_session(conn, sid)["turn_count"] == 1
        db.set_pending_cite_nudge(conn, sid, 2)
    finally:
        conn.close()

    spawned: list[tuple[tuple, dict]] = []
    transcript = project / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(session_end, "is_unlatched_mode", lambda *_args: False)
    monkeypatch.setattr(session_end, "is_write_disabled", lambda *_args: False)
    monkeypatch.setattr(session_end, "is_in_compact", lambda: False)
    monkeypatch.setattr(
        session_end,
        "read_hook_input",
        lambda: {
            "cwd": str(project),
            "session_id": sid,
            "transcript_path": str(transcript),
        },
    )
    monkeypatch.setattr(
        session_end,
        "spawn_compactor_detached",
        lambda *args, **kwargs: spawned.append((args, kwargs)),
    )
    assert session_end.main() == 0
    assert spawned[0][1] == {
        "final": True,
        "binding_revision": target.revision,
        "expected_kb_dir": str(vault),
    }

    # SessionEnd clears the task receipt after handing the exact target to the
    # detached compactor. Recreate the same verified receipt so the prompt-hook
    # check below exercises a real vault read instead of its stale-session exit.
    assert project_config.record_session_binding(project, sid) == target.revision

    monkeypatch.setattr(prompt_hook, "is_unlatched_mode", lambda *_args: False)
    monkeypatch.setattr(prompt_hook, "is_disabled", lambda *_args: False)
    monkeypatch.setattr(prompt_hook, "is_in_compact", lambda: False)
    monkeypatch.setattr(
        prompt_hook,
        "read_hook_input",
        lambda: {
            "cwd": str(project),
            "session_id": sid,
            "prompt": "review this exact client implementation now",
        },
    )
    monkeypatch.setattr(prompt_hook.mcp_broker, "read_discovery", lambda: None)
    monkeypatch.setattr(
        prompt_hook.mcp_broker,
        "request_daemon_start",
        lambda _cwd: False,
    )
    monkeypatch.setattr(
        prompt_hook.mcp_broker,
        "emit_lifecycle",
        lambda *_args, **_kwargs: None,
    )
    assert prompt_hook.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert "temporarily unavailable" in output["hookSpecificOutput"][
        "additionalContext"
    ]
    conn = db.connect(
        str(project),
        expected_binding_revision=target.revision,
        expected_kb_dir=str(vault),
    )
    try:
        assert db.take_pending_cite_nudge(conn, sid) == 0
    finally:
        conn.close()


def test_detached_compactor_uses_explicit_binding_with_poisoned_ambient_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, vault, target, sid = _private_scope(tmp_path, "compact-session")
    transcript = project / "compact.jsonl"
    transcript.write_text(
        '{"type":"user","message":"compact exact client work"}\n',
        encoding="utf-8",
    )
    _poison_ambient_session(monkeypatch)
    monkeypatch.setattr(
        compactor,
        "_invoke_summarizer",
        lambda *_args, **_kwargs: {
            "session_summary": {
                "title": "Exact detached compact",
                "body": "The detached compactor retained its verified vault.",
            },
            "extracted_nodes": [],
            "links": [],
        },
    )
    monkeypatch.setattr(compactor, "_related_nodes_brief", lambda *_args: [])
    monkeypatch.setattr(compactor, "_merge_focus_workstreams", lambda _c, rows: rows)
    monkeypatch.setattr(compactor.feeders, "merge_feeder_rows", lambda _c, rows: rows)
    monkeypatch.setattr(
        compactor,
        "_merge_lifecycle_candidate_rows",
        lambda _c, rows: rows,
    )
    monkeypatch.setattr(
        compactor.artifacts,
        "attach_observed_artifacts",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        compactor.embeddings,
        "embed",
        lambda _text: np.full(384, 1 / np.sqrt(384), dtype=np.float32),
    )

    result = compactor.run_compaction(
        sid,
        str(project),
        str(transcript),
        summarizer_backend="codex",
        binding_revision=target.revision,
        expected_kb_dir=str(vault),
    )
    assert result["ok"] is True
    assert result["summary_node_id"] is not None


def test_detached_selfheal_threads_exact_target_through_every_nested_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, vault, target, _sid = _private_scope(tmp_path, "maintenance-origin")
    for name in project_config.AGENT_SESSION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LATCH_MAINTENANCE_BACKEND", "cursor")
    monkeypatch.setenv("LATCH_ADAPTER", "cursor")

    calls: list[tuple[str | None, str | None]] = []
    real_connect = db.connect

    def traced_connect(cwd=None, **kwargs):
        calls.append((
            kwargs.get("expected_binding_revision"),
            str(kwargs.get("expected_kb_dir"))
            if kwargs.get("expected_kb_dir") is not None else None,
        ))
        return real_connect(cwd, **kwargs)

    monkeypatch.setattr(db, "connect", traced_connect)
    result = selfheal.run_selfheal(
        str(project),
        expected_binding_revision=target.revision,
        expected_kb_dir=str(vault),
    )

    assert result["ok"] is True
    assert {"backup", "heal", "weekly", "workstream_shadow"}.issubset(
        result["ran"]
    )
    assert len(calls) >= 7
    assert set(calls) == {(target.revision, str(vault))}


def test_cursor_shell_fallbacks_snapshot_current_session_before_db_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, vault, target, sid = _private_scope(tmp_path, "cursor-current")
    monkeypatch.setenv("LATCH_ADAPTER", "cursor")
    monkeypatch.setenv("LATCH_MAINTENANCE_BACKEND", "cursor")
    monkeypatch.setenv("LATCH_GATE_BACKEND", "cursor")
    monkeypatch.setenv("LATCH_SESSION_ID", sid)

    assert maintenance.main(["weekly", str(project)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    monkeypatch.setattr(
        gate,
        "run_gate",
        lambda _conn, request, **_kwargs: {
            "request": request,
            "verdict": {"recommendation": "PROCEED"},
        },
    )
    assert kb_gate_cli.main([
        "kb_gate_cli.py",
        str(project),
        "review exact client change",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    monkeypatch.setenv("LATCH_SESSION_ID", "wrong-cursor-conversation")
    assert maintenance.main(["weekly", str(project)]) == 1
    denied = json.loads(capsys.readouterr().out)
    assert denied["reason"] == "stale_session_binding"
    assert not (vault / "wrong-cursor-conversation").exists()
