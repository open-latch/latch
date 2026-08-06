"""Regressions for session lifecycle after removing SessionStart DB writes."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "hooks"))

import compactor  # noqa: E402
import db  # noqa: E402
import project_config  # noqa: E402
import session_end  # noqa: E402
import stop  # noqa: E402
import user_prompt_submit as prompt_hook  # noqa: E402


def _session(project: Path, session_id: str) -> dict | None:
    conn = db.connect(str(project))
    try:
        return db.get_session(conn, session_id)
    finally:
        conn.close()


def test_eligible_prompt_retrieval_creates_session_lazily(
    tmp_path: Path, monkeypatch,
):
    session_id = "lazy-prompt-session"
    conn = db.connect(str(tmp_path))
    try:
        node_id = db.insert_node(
            conn,
            kind="fact",
            title="Lazy prompt retrieval",
            body="Prompt retrieval can be the first session-aware DB contact.",
        )
        assert db.get_session(conn, session_id) is None
    finally:
        conn.close()

    prompt_hook._load_runtime()
    monkeypatch.setattr(db, "_now", lambda: "2030-01-02 03:04:05")
    monkeypatch.setattr(
        prompt_hook,
        "_embed_with_bounded_wake",
        lambda *_args, **_kwargs: np.array([1.0], dtype=np.float32),
    )
    monkeypatch.setattr(
        prompt_hook.search,
        "vector_search",
        lambda *_args, **_kwargs: [{
            "id": node_id,
            "kind": "fact",
            "title": "Lazy prompt retrieval",
            "body": "Prompt retrieval can be the first session-aware DB contact.",
            "status": "staging",
            "workstream_id": None,
            "score": 0.99,
        }],
    )

    result = prompt_hook._retrieve_and_inject(
        str(tmp_path),
        session_id,
        "explain the lazy session lifecycle",
        {},
    )

    assert [row["id"] for row in result] == [node_id]
    conn = db.connect(str(tmp_path))
    try:
        session = db.get_session(conn, session_id)
        assert session is not None
        assert session["started_at"] == "2030-01-02 03:04:05"
        assert session["turn_count"] == 0
        assert session["last_prompt_embedding"] is not None
        retrieval = conn.execute(
            """
            SELECT source, first_injected_turn
            FROM session_retrievals
            WHERE session_id = ? AND node_id = ?
            """,
            (session_id, node_id),
        ).fetchone()
        assert dict(retrieval) == {
            "source": "prompt",
            "first_injected_turn": 0,
        }
    finally:
        conn.close()


def test_stop_creates_missing_session_before_incrementing_turn(
    tmp_path: Path, monkeypatch,
):
    session_id = "lazy-stop-session"
    binding_revision = project_config.resolve(tmp_path).revision
    assert _session(tmp_path, session_id) is None
    monkeypatch.setattr(stop, "is_unlatched_mode", lambda *_args: False)
    monkeypatch.setattr(stop, "is_write_disabled", lambda *_args: False)
    monkeypatch.setattr(stop, "is_in_compact", lambda: False)
    monkeypatch.setattr(
        stop,
        "current_session_revision",
        lambda *_args: binding_revision,
    )
    monkeypatch.setattr(db, "_now", lambda: "2030-02-03 04:05:06")
    monkeypatch.setattr(
        stop,
        "read_hook_input",
        lambda: {"session_id": session_id, "cwd": str(tmp_path)},
    )
    monkeypatch.setattr(stop, "_cite_presence_check", lambda *_args: None)
    spawned: list[tuple] = []
    monkeypatch.setattr(
        stop,
        "spawn_compactor_detached",
        lambda *args, **kwargs: spawned.append((args, kwargs)),
    )

    assert stop.main() == 0

    session = _session(tmp_path, session_id)
    assert session is not None
    assert session["started_at"] == "2030-02-03 04:05:06"
    assert session["turn_count"] == 1
    assert spawned == []


def test_session_end_creates_missing_session_and_schedules_final_compaction(
    tmp_path: Path, monkeypatch,
):
    session_id = "lazy-session-end"
    binding_revision = project_config.resolve(tmp_path).revision
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    assert _session(tmp_path, session_id) is None
    monkeypatch.setattr(session_end, "is_unlatched_mode", lambda *_args: False)
    monkeypatch.setattr(session_end, "is_write_disabled", lambda *_args: False)
    monkeypatch.setattr(session_end, "is_in_compact", lambda: False)
    monkeypatch.setattr(
        session_end,
        "current_session_revision",
        lambda *_args: binding_revision,
    )
    monkeypatch.setattr(db, "_now", lambda: "2030-03-04 05:06:07")
    monkeypatch.setattr(
        session_end,
        "read_hook_input",
        lambda: {
            "session_id": session_id,
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
    )
    spawned: list[tuple] = []
    monkeypatch.setattr(
        session_end,
        "spawn_compactor_detached",
        lambda *args, **kwargs: spawned.append((args, kwargs)),
    )

    assert session_end.main() == 0

    session = _session(tmp_path, session_id)
    assert session is not None
    assert session["started_at"] == "2030-03-04 05:06:07"
    assert session["transcript_path"] == str(transcript)
    assert session["ended_at"] is None
    assert spawned == [
        (
            (session_id, str(tmp_path), str(transcript)),
            {
                "final": True,
                "binding_revision": binding_revision,
                "expected_kb_dir": str(db.paths.project_dir(tmp_path)),
            },
        )
    ]


def test_manual_compaction_creates_session_at_first_compaction_contact(
    tmp_path: Path, monkeypatch,
):
    session_id = "lazy-manual-compact"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"user","message":"compact this"}\n', encoding="utf-8")
    assert _session(tmp_path, session_id) is None
    monkeypatch.setattr(db, "_now", lambda: "2030-04-05 06:07:08")
    monkeypatch.setattr(
        compactor,
        "_invoke_summarizer",
        lambda *_args, **_kwargs: {
            "session_summary": {
                "title": "Lazy manual compact",
                "body": "The first durable session contact was manual compaction.",
            },
            "extracted_nodes": [],
            "links": [],
        },
    )
    monkeypatch.setattr(compactor, "_related_nodes_brief", lambda *_args: [])
    monkeypatch.setattr(compactor, "_merge_focus_workstreams", lambda _conn, rows: rows)
    monkeypatch.setattr(compactor.feeders, "merge_feeder_rows", lambda _conn, rows: rows)
    monkeypatch.setattr(
        compactor,
        "_merge_lifecycle_candidate_rows",
        lambda _conn, rows: rows,
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

    result = compactor._run_compaction_locked(
        session_id,
        str(tmp_path),
        str(transcript),
        summarizer_backend="codex",
    )

    assert result["ok"] is True
    assert result["final"] is False
    session = _session(tmp_path, session_id)
    assert session is not None
    assert session["started_at"] == "2030-04-05 06:07:08"
    assert session["turn_count"] == 0
    assert session["last_compact_turn"] == 0
    assert session["summary_node_id"] == result["summary_node_id"]
    assert session["ended_at"] is None
