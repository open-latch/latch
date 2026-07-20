"""Local dev-detector trace, privacy, and trigger integration tests."""
from __future__ import annotations

import hashlib
import copy
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(_SRC / "hooks"))

import db  # noqa: E402
import detector_snapshot  # noqa: E402
import detector_trace  # noqa: E402
import detector_trace_cli  # noqa: E402
import detector_trigger  # noqa: E402
import gate  # noqa: E402
import log_utils  # noqa: E402
import paths  # noqa: E402
import stop  # noqa: E402
import user_prompt_submit as ups  # noqa: E402


SID = "11111111-2222-3333-4444-555555555555"


def _write_transcript(root: Path, rows: list[tuple[str, object]]) -> Path:
    path = root / "session.jsonl"
    payloads = []
    for role, content in rows:
        payloads.append({
            "timestamp": "2026-07-19T20:00:00Z",
            "type": role,
            "message": {"role": role, "content": content},
        })
    path.write_text("\n".join(json.dumps(r) for r in payloads) + "\n", encoding="utf-8")
    return path


def _setup_project(tmp_path: Path) -> tuple[str, object]:
    project = str(tmp_path / "project")
    conn = db.connect(project)
    return project, conn


def _emit_retrieve(
    project: str,
    prompt: str,
    *,
    turn: int = 0,
    raw_hits=None,
    injected=None,
    skip=None,
    error=None,
    status=None,
    snapshots=None,
    active_ids=None,
    detector_event_ts=None,
) -> None:
    row = {
        "prompt_hash": detector_trace.hash_prompt(prompt),
        "turn": turn,
        "raw_hits": raw_hits or [],
        "injected": injected or [],
        "active_ids": active_ids or [],
        "filtered_out_kind": 0,
        "filtered_out_active": 0,
        "filtered_out_floor": 0,
        "path": "vector" if not skip and not error else None,
    }
    if skip:
        row["skip"] = skip
    if error:
        row["error"] = error
    if status:
        row["retrieval_status"] = status
    if snapshots:
        row["node_snapshots"] = snapshots
    if detector_event_ts:
        row["detector_event_ts"] = detector_event_ts
    log_utils.emit_event("retrieve", row, project_path=project, session_id=SID)


def test_manual_trace_exact_join_is_read_only(tmp_path, monkeypatch):
    project, conn = _setup_project(tmp_path)
    node_id = db.insert_node(
        conn, kind="decision", title="private title", body="private body", status="canonical"
    )
    transcript = _write_transcript(tmp_path, [
        ("user", [{"type": "text", "text": "use the canonical cache choice"}]),
        ("assistant", [
            {"type": "text", "text": f"I used node id={node_id}."},
            {"type": "tool_use", "name": "Read", "input": {}},
        ]),
        ("user", [{"type": "tool_result", "content": "secret tool output"}]),
    ])
    db.upsert_session(conn, SID, project, str(transcript))
    snapshots = detector_snapshot.snapshot_nodes(conn, [node_id])
    _emit_retrieve(
        project,
        "use the canonical cache choice",
        raw_hits=[(node_id, 0.91, "decision")],
        injected=[(node_id, 0.91)],
        snapshots=snapshots,
    )
    conn.close()

    db_file = paths.db_path(project)
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in db_file.parent.iterdir()
        if path.is_file()
    }
    packet = detector_trace.build_trace(
        project_path=project,
        session_id=SID,
        transcript_path=str(transcript),
    )
    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in db_file.parent.iterdir()
        if path.is_file()
    }
    assert before == after
    assert packet["receipts"]["retrieval"]["state"] == "executed"
    assert packet["receipts"]["retrieval"]["raw_hits"][0]["id"] == node_id
    assert packet["event_coordinate"]["subject_line"] == 1
    assert packet["event_coordinate"]["assistant_lines"] == [2]
    assert packet["candidate_node_snapshots"][0]["snapshot_basis"] == "event_time_receipt"
    assert "private title" not in json.dumps(packet["candidate_node_snapshots"])


def test_explicit_correction_traces_previous_turn(tmp_path):
    project, conn = _setup_project(tmp_path)
    transcript = _write_transcript(tmp_path, [
        ("user", "keep SQLite"),
        ("assistant", "We should replace it."),
        ("user", "That is wrong; I already told you to keep it."),
    ])
    db.upsert_session(conn, SID, project, str(transcript))
    conn.close()
    _emit_retrieve(project, "keep SQLite", turn=2)
    _emit_retrieve(project, "That is wrong; I already told you to keep it.", turn=3)

    packet = detector_trace.build_trace(
        project_path=project,
        session_id=SID,
        transcript_path=str(transcript),
        prompt_hash=detector_trace.hash_prompt("That is wrong; I already told you to keep it."),
        prompt_turn=3,
        trigger_types=["explicit_correction"],
    )
    assert packet["transcript_evidence"]["subject_prompt_snippet"] == "keep SQLite"
    assert packet["transcript_evidence"]["assistant_snippet"] == "We should replace it."
    assert packet["receipts"]["retrieval"]["turn"] == 2
    assert packet["classification"]["primary_failure_class"] is None
    assert packet["human_disposition"]["status"] == "unresolved"


def test_previous_selects_one_earlier_exchange_without_double_shifting(tmp_path):
    project, conn = _setup_project(tmp_path)
    transcript = _write_transcript(tmp_path, [
        ("user", "first prompt"),
        ("assistant", "first answer"),
        ("user", "second prompt"),
        ("assistant", "second answer"),
        ("user", "third prompt"),
        ("assistant", "third answer"),
    ])
    db.upsert_session(conn, SID, project, str(transcript))
    conn.close()

    packet = detector_trace.build_trace(
        project_path=project,
        session_id=SID,
        transcript_path=str(transcript),
        previous=1,
    )

    assert packet["transcript_evidence"]["trigger_snippet"] == "second prompt"
    assert packet["transcript_evidence"]["subject_prompt_snippet"] == "second prompt"
    assert packet["transcript_evidence"]["assistant_snippet"] == "second answer"


def test_missing_prompt_hash_never_falls_back_to_previous_exchange(tmp_path):
    project, conn = _setup_project(tmp_path)
    transcript = _write_transcript(tmp_path, [
        ("user", "previous unrelated prompt"),
        ("assistant", "previous unrelated answer"),
    ])
    db.upsert_session(conn, SID, project, str(transcript))
    conn.close()
    missing_hash = detector_trace.hash_prompt("current prompt not yet in transcript")

    packet = detector_trace.build_trace(
        project_path=project,
        session_id=SID,
        transcript_path=str(transcript),
        prompt_hash=missing_hash,
        trigger_types=["runtime_degraded"],
    )

    assert packet["trigger"]["prompt_hash"] == missing_hash
    assert packet["event_coordinate"]["transcript_status"] == "unavailable"
    assert packet["event_coordinate"]["transcript_limitation"] == "prompt_hash_not_found"
    assert packet["event_coordinate"]["trigger_line"] is None
    assert packet["event_coordinate"]["subject_line"] is None
    assert packet["event_coordinate"]["assistant_lines"] == []
    assert packet["transcript_evidence"]["trigger_snippet"] == ""
    assert packet["transcript_evidence"]["subject_prompt_snippet"] == ""
    assert packet["transcript_evidence"]["assistant_snippet"] == ""


def test_receipt_join_is_exact_turn_and_never_borrows_future_evidence():
    base = {
        "session_id": SID,
        "prompt_hash": "samehash",
        "raw_hits": [],
        "injected": [],
    }
    previous = {
        **base,
        "turn": 2,
        "ts": "2026-07-19T20:00:00.100Z",
        "detector_event_ts": "2026-07-19T20:00:00.000Z",
    }
    current = {
        **base,
        "turn": 3,
        "ts": "2026-07-19T20:00:01.100Z",
        "detector_event_ts": "2026-07-19T20:00:01.000Z",
    }
    rows = [previous, current]
    assert detector_trace._select_receipt(
        rows,
        session_id=SID,
        join_hash="samehash",
        turn=4,
        event_ts="2026-07-19T20:00:01.000Z",
    ) is None
    assert detector_trace._select_receipt(
        [current],
        session_id=SID,
        join_hash="samehash",
        event_ts="2026-07-19T20:00:00.000Z",
    ) is None
    assert detector_trace._select_receipt(
        rows,
        session_id=SID,
        join_hash="samehash",
        event_ts="2026-07-19T20:00:01.000Z",
    ) is current
    assert detector_trace._select_receipt(
        rows,
        session_id=SID,
        join_hash="samehash",
        event_ts="2026-07-19T20:00:01.000Z",
        allow_exact_coordinate=False,
    ) is previous
    assert detector_trace._select_receipt(
        [previous],
        session_id=SID,
        join_hash="samehash",
        event_ts="2026-07-19T20:00:01.000Z",
        require_exact_coordinate=True,
    ) is None


def test_prompt_receipt_coordinate_joins_own_short_correction(
    tmp_path, monkeypatch,
):
    project, conn = _setup_project(tmp_path)
    prompt = "Still broken."
    transcript = _write_transcript(tmp_path, [("user", prompt)])
    db.upsert_session(conn, SID, project, str(transcript))
    conn.close()
    monkeypatch.setenv("LATCH_DEV_DETECTOR", "1")
    monkeypatch.delenv("LATCH_ADAPTER", raising=False)
    entry = {
        "sid": SID,
        "prompt_hash": detector_trace.hash_prompt(prompt),
        "turn": 3,
        "skip": "prompt_too_short",
        "detector_correction_signal": True,
        "retrieval_status": "not_executed",
        "raw_hits": [],
        "injected": [],
        "active_ids": [],
    }
    ups._write_log(project, entry)
    assert entry["detector_event_ts"]
    queued = []
    monkeypatch.setattr(detector_trigger, "queue", lambda **kwargs: queued.append(kwargs) or True)
    ups._queue_detector(project, SID, str(transcript), entry)
    assert queued[0]["event_ts"] == entry["detector_event_ts"]
    packet = detector_trace.build_trace(
        project_path=project,
        session_id=SID,
        transcript_path=str(transcript),
        prompt_hash=entry["prompt_hash"],
        prompt_turn=3,
        event_ts=entry["detector_event_ts"],
        trigger_types=["explicit_correction"],
    )
    assert packet["receipts"]["trigger_retrieval"]["state"] == "not_executed"


def test_repeated_short_correction_does_not_borrow_missing_current_receipt(tmp_path):
    project, conn = _setup_project(tmp_path)
    prompt = "Still broken."
    transcript = _write_transcript(tmp_path, [
        ("user", prompt),
        ("assistant", "I will retry."),
        ("user", prompt),
    ])
    db.upsert_session(conn, SID, project, str(transcript))
    conn.close()
    _emit_retrieve(
        project,
        prompt,
        skip="prompt_too_short",
        detector_event_ts="2026-07-19T20:00:00.000Z",
    )
    packet = detector_trace.build_trace(
        project_path=project,
        session_id=SID,
        transcript_path=str(transcript),
        prompt_hash=detector_trace.hash_prompt(prompt),
        event_ts=detector_trace.now_iso(),
        trigger_types=["explicit_correction"],
    )
    assert packet["receipts"]["retrieval"]["state"] == "not_executed"
    assert packet["receipts"]["trigger_retrieval"]["state"] == "unavailable"


@pytest.mark.parametrize(
    ("receipt_kwargs", "state", "outcome"),
    [
        ({"skip": "prompt_too_short"}, "not_executed", "prompt_too_short"),
        ({"skip": "embed_daemon_unavailable"}, "degraded", "embed_daemon_unavailable"),
        ({"error": "RuntimeError: boom"}, "degraded", "error"),
        ({"raw_hits": [], "injected": []}, "executed", "no_candidates_in_recorded_top10"),
    ],
)
def test_retrieval_states_are_truthful(tmp_path, receipt_kwargs, state, outcome):
    project, conn = _setup_project(tmp_path)
    transcript = _write_transcript(tmp_path, [("user", "trace this receipt")])
    db.upsert_session(conn, SID, project, str(transcript))
    conn.close()
    _emit_retrieve(project, "trace this receipt", **receipt_kwargs)
    packet = detector_trace.build_trace(
        project_path=project, session_id=SID, transcript_path=str(transcript)
    )
    receipt = packet["receipts"]["retrieval"]
    assert (receipt["state"], receipt["outcome"]) == (state, outcome)
    assert receipt["top_k_boundary"] == 10
    assert "not absent from the KB" in receipt["boundary_note"]


def test_missing_receipt_is_unavailable_not_no_result(tmp_path):
    project, conn = _setup_project(tmp_path)
    transcript = _write_transcript(tmp_path, [("user", "missing receipt")])
    db.upsert_session(conn, SID, project, str(transcript))
    conn.close()
    packet = detector_trace.build_trace(
        project_path=project, session_id=SID, transcript_path=str(transcript)
    )
    receipt = packet["receipts"]["retrieval"]
    assert receipt["state"] == "unavailable"
    assert receipt["outcome"] == "receipt_missing_or_rotated"


def test_event_snapshot_survives_later_node_mutation(tmp_path):
    project, conn = _setup_project(tmp_path)
    node_id = db.insert_node(
        conn, kind="fact", title="before", body="before body", status="canonical"
    )
    original = detector_snapshot.snapshot_nodes(conn, [node_id])
    old_hash = original[0]["content_hash"]["value"]
    transcript = _write_transcript(tmp_path, [("user", "use this fact")])
    db.upsert_session(conn, SID, project, str(transcript))
    conn.close()
    _emit_retrieve(
        project,
        "use this fact",
        raw_hits=[(node_id, 0.8, "fact")],
        snapshots=original,
    )

    conn = db.connect(project)
    db.update_node(conn, node_id, title="after", body="after body")
    conn.close()
    packet = detector_trace.build_trace(
        project_path=project, session_id=SID, transcript_path=str(transcript)
    )
    snap = next(s for s in packet["candidate_node_snapshots"] if s["id"] == node_id)
    assert snap["content_hash"]["value"] == old_hash
    assert snap["snapshot_basis"] == "event_time_receipt"


def test_active_node_snapshot_survives_later_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("LATCH_DEV_DETECTOR", "1")
    monkeypatch.delenv("LATCH_ADAPTER", raising=False)
    project, conn = _setup_project(tmp_path)
    node_id = db.insert_node(
        conn, kind="decision", title="active before", body="before", status="canonical"
    )
    receipt: dict = {"raw_hits": [], "injected": [], "active_ids": [node_id]}
    ups._freeze_detector_node_snapshots(conn, receipt)
    old_hash = receipt["node_snapshots"][0]["content_hash"]["value"]
    transcript = _write_transcript(tmp_path, [("user", "use active context now")])
    db.upsert_session(conn, SID, project, str(transcript))
    conn.close()
    _emit_retrieve(
        project,
        "use active context now",
        active_ids=[node_id],
        snapshots=receipt["node_snapshots"],
    )
    conn = db.connect(project)
    db.update_node(conn, node_id, title="active after", body="after")
    conn.close()

    packet = detector_trace.build_trace(
        project_path=project, session_id=SID, transcript_path=str(transcript)
    )
    snap = next(s for s in packet["candidate_node_snapshots"] if s["id"] == node_id)
    assert snap["content_hash"]["value"] == old_hash
    assert snap["snapshot_source"] == "subject_retrieval"


def test_prompt_snapshot_cap_preserves_ranked_and_high_id_event_nodes(tmp_path, monkeypatch):
    monkeypatch.setenv("LATCH_DEV_DETECTOR", "1")
    monkeypatch.delenv("LATCH_ADAPTER", raising=False)
    project, conn = _setup_project(tmp_path)
    node_ids = [
        db.insert_node(conn, kind="fact", title=f"n{i}", body="b", status="canonical")
        for i in range(40)
    ]
    ranked = list(reversed(node_ids[-10:]))
    receipt = {
        "raw_hits": [(node_id, 1.0, "fact") for node_id in ranked],
        "injected": [(node_id, 0.9) for node_id in node_ids[20:25]],
        "active_ids": node_ids,
    }
    ups._freeze_detector_node_snapshots(conn, receipt)
    conn.close()
    captured = [snap["id"] for snap in receipt["node_snapshots"]]
    assert captured[:10] == ranked
    assert len(captured) == ups._DETECTOR_PROMPT_SNAPSHOT_LIMIT == 16
    assert set(node_ids[20:25]).issubset(captured)
    assert captured[-1] == node_ids[0]
    assert receipt["node_snapshot_omitted_count"] == 24


def test_write_time_redaction_and_projection_allowlist(tmp_path):
    project, conn = _setup_project(tmp_path)
    transcript = _write_transcript(tmp_path, [("user", "trace privacy")])
    db.upsert_session(conn, SID, project, str(transcript))
    conn.close()
    packet = detector_trace.build_trace(
        project_path=project,
        session_id=SID,
        transcript_path=str(transcript),
        trigger_types=["runtime_degraded"],
    )
    secrets = (
        "Authorization: Bearer abcdefghijklmnop\n"
        "Authorization: Basic dXNlcjpwYXNz\n"
        "api_key=sk-supersecret123456 "
        "OPENAI_API_KEY=plainsecret123 GITHUB_TOKEN=plainsecret456 "
        "AWS_SECRET_ACCESS_KEY=abcdef0123456789 "
        "DB_PASS=db-hunter2 PGPASS=pg-hunter2 DB_PWD=db-pwd-secret "
        "SERVICE_CRED=service-credential-secret "
        "ghp_abcdefghijklmnopqrstuvwxyz xoxb-1234567890-secret "
        "password=hunter2 {\"password\": \"jsonhunter2\"} "
        "https://dbuser:dbpass@example.test/path "
        + ("AKIA" + "ABCDEFGHIJKLMNOP")
        + " person@example.com"
    )
    packet["transcript_evidence"]["assistant_snippet"] = secrets
    packet["structured_secret_probe"] = {
        "password": "structuredhunter2",
        "OPENAI_API_KEY": "structured-openai-key",
        "PGPASSWORD": "structured-pg-password",
        "SERVICE_CREDENTIAL": "structured-service-credential",
        "AWS_CREDENTIALS": "structured-aws-credentials",
        "nested": {
            "authorization": "Basic c3RydWN0dXJlZA==",
            "secret": "structured-secret",
        },
    }
    path = detector_trace.write_incident(packet, project)
    disk = path.read_text(encoding="utf-8")
    for forbidden in (
        "abcdefghijklmnop", "dXNlcjpwYXNz", "sk-supersecret",
        "plainsecret123", "plainsecret456", "abcdef0123456789",
        "db-hunter2", "pg-hunter2", "db-pwd-secret",
        "service-credential-secret", "structured-pg-password",
        "structured-service-credential",
        "structured-aws-credentials",
        "ghp_abcdefghijklmnopqrstuvwxyz", "xoxb-1234567890-secret",
        "hunter2", "jsonhunter2", "dbuser", "dbpass",
        "AKIA" + "ABCDEFGHIJKLMNOP", "person@example.com",
        "structuredhunter2", "structured-openai-key", "c3RydWN0dXJlZA==",
        "structured-secret",
    ):
        assert forbidden not in disk
    assert "<redacted" in disk
    assert detector_trace.redact_text(
        "compass=north bypass=allowed"
    ) == "compass=north bypass=allowed"

    projection = json.dumps(packet["sanitized_projection"], sort_keys=True)
    for forbidden in (
        project, str(transcript), SID, packet["incident_id"], packet["fingerprint"],
        "prompt_hash", "node_snapshots", "receipt_path",
    ):
        assert forbidden not in projection
    assert packet["public_fixture_candidate"] is False


def test_snapshot_authority_states(tmp_path):
    project, conn = _setup_project(tmp_path)
    old = db.insert_node(conn, kind="decision", title="old", body="old", status="canonical")
    new = db.insert_node(conn, kind="decision", title="new", body="new", status="canonical")
    db.add_edge(conn, src=new, dst=old, relation="supersedes")
    snap = detector_snapshot.snapshot_node(conn, old)
    conn.close()
    assert snap["authority"] == "STALE"
    assert snap["superseded_by"] == [new]
    assert snap["content_hash"]["algorithm"] == "sha256"


def test_snapshot_batch_has_one_consistent_read_view(tmp_path):
    project, conn = _setup_project(tmp_path)
    old = db.insert_node(conn, kind="decision", title="old", body="old", status="canonical")
    successor = db.insert_node(
        conn, kind="decision", title="new", body="new", status="canonical"
    )
    writer_start = threading.Event()
    writer_done = threading.Event()
    callback_waited = False

    def writer():
        writer_start.wait(timeout=2)
        other = db.connect(project)
        try:
            db.add_edge(other, src=successor, dst=old, relation="supersedes")
        finally:
            other.close()
            writer_done.set()

    thread = threading.Thread(target=writer)
    thread.start()

    def trace(sql: str):
        nonlocal callback_waited
        if not callback_waited and "FROM edges" in sql:
            callback_waited = True
            writer_start.set()
            assert writer_done.wait(timeout=2)

    conn.set_trace_callback(trace)
    snapshot = detector_snapshot.snapshot_nodes(conn, [old])[0]
    conn.set_trace_callback(None)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert snapshot["authority"] == "OK"
    assert detector_snapshot.snapshot_node(conn, old)["authority"] == "STALE"
    conn.close()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("That is wrong.", True),
        ("I already told you to keep SQLite.", True),
        ("As I said, keep SQLite.", False),
        ("This is still broken.", True),
        ("What is wrong with this test?", False),
        ("Is this wrong?", False),
        ("Tell me whether this is wrong.", False),
        ("Do you think this is wrong?", False),
        ("Can you check if this is wrong?", False),
        ("I wonder whether this is wrong.", False),
        ("Why is this still broken?", True),
        ("> that is wrong\nplease inspect the log", False),
    ],
)
def test_strict_correction_predicate(text, expected):
    assert ups._is_detector_correction(text) is expected


def test_detector_receipt_surface_distinguishes_unavailable():
    entry = {"skip": "embed_daemon_unavailable"}
    assert ups._set_detector_receipt_status(entry, []) == "unavailable"
    rendered = ups._format_detector_retrieval_context([], entry)
    assert "retrieval unavailable" in rendered.lower()
    assert "distinct from finding no relevant node" in rendered


def test_flag_off_never_spawns(monkeypatch):
    called = []
    monkeypatch.setattr(detector_trigger.subprocess, "Popen", lambda *a, **k: called.append(a))
    monkeypatch.delenv("LATCH_DEV_DETECTOR", raising=False)
    assert detector_trigger.queue(
        project_path="/tmp/x", session_id=SID, trigger_types=["explicit_correction"]
    ) is False
    monkeypatch.setenv("LATCH_DEV_DETECTOR", "yes")
    assert detector_trigger.queue(
        project_path="/tmp/x", session_id=SID, trigger_types=["explicit_correction"]
    ) is False
    assert called == []


def test_queue_contains_only_structural_arguments(monkeypatch):
    captured = []

    class Dummy:
        pass

    monkeypatch.setenv("LATCH_DEV_DETECTOR", "1")
    monkeypatch.setattr(
        detector_trigger.subprocess,
        "Popen",
        lambda args, **kwargs: captured.append((args, kwargs)) or Dummy(),
    )
    assert detector_trigger.queue(
        project_path="/tmp/project",
        session_id=SID,
        trigger_types=["explicit_correction"],
        prompt_hash="abc123",
        node_ids=[2, 1],
    )
    argv = captured[0][0]
    joined = " ".join(argv)
    assert "raw user correction" not in joined
    assert "abc123" in argv and argv.count("--node-id") == 2


def test_queue_rejects_unknown_trigger_before_spawn(monkeypatch):
    called = []
    monkeypatch.setenv("LATCH_DEV_DETECTOR", "1")
    monkeypatch.delenv("LATCH_ADAPTER", raising=False)
    monkeypatch.setattr(
        detector_trigger.subprocess,
        "Popen",
        lambda *args, **kwargs: called.append(args),
    )
    assert detector_trigger.queue(
        project_path="/tmp/project",
        session_id=SID,
        trigger_types=["private-customer-name"],
    ) is False
    assert called == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Latch node id=42 is the current canonical decision.", [42]),
        ("Node 42 is stale historical context, not current.", []),
        ("PR #42 is current.", []),
        ("PR id=42 is current.", []),
        ("Incident id=42 is current.", []),
        ("The current task id=42 is authoritative.", []),
        ("Is node 42 current?", []),
        ("Check whether node 42 is current.", []),
        ("I cannot tell whether node 42 is current.", []),
        ("Node 42 might be current.", []),
        ("If node 42 is current, use it.", []),
        ("Perhaps node 42 is current.", []),
        ("Node 42 is not canonical.", []),
        ("Node 42 is not authoritative.", []),
        ("Node 42 is not governing.", []),
        ("Node 42 is not the active decision.", []),
        ("Node 42 is no longer canonical.", []),
        ("Node 42 was formerly canonical.", []),
        ("```Latch node id=42 is current```", []),
        ("> Latch node id=42 is current\nNo claim here.", []),
    ],
)
def test_current_authority_assertion_filter(text, expected):
    assert stop._current_node_assertions(text) == expected


def test_stop_emits_only_for_noncurrent_authority(tmp_path, monkeypatch):
    project, conn = _setup_project(tmp_path)
    old = db.insert_node(conn, kind="decision", title="old", body="old", status="canonical")
    new = db.insert_node(conn, kind="decision", title="new", body="new", status="canonical")
    db.add_edge(conn, src=new, dst=old, relation="supersedes")
    transcript = _write_transcript(tmp_path, [
        ("user", "which decision governs?"),
        ("assistant", f"Latch node id={old} is the current canonical decision."),
    ])
    db.upsert_session(conn, SID, project, str(transcript))
    conn.close()
    queued = []
    monkeypatch.setenv("LATCH_DEV_DETECTOR", "1")
    monkeypatch.setattr(detector_trigger, "queue", lambda **kwargs: queued.append(kwargs) or True)
    stop._detector_authority_check(SID, project, str(transcript))
    assert queued and queued[0]["node_ids"] == [old]
    rows = detector_trace._read_stream_rows("detector_trigger", project)
    assert rows[-1]["node_snapshots"][0]["authority"] == "STALE"


def test_stop_suppresses_reconciled_node_when_successor_is_also_cited(
    tmp_path, monkeypatch,
):
    project, conn = _setup_project(tmp_path)
    old = db.insert_node(conn, kind="decision", title="old", body="old", status="canonical")
    successor = db.insert_node(
        conn, kind="decision", title="new", body="new", status="canonical"
    )
    db.add_edge(conn, src=old, dst=successor, relation="reconciled_by")
    transcript = _write_transcript(tmp_path, [
        ("user", "which decision governs?"),
        (
            "assistant",
            f"Node {old} is current. It is reconciled by node {successor}.",
        ),
    ])
    db.upsert_session(conn, SID, project, str(transcript))
    conn.close()
    queued = []
    monkeypatch.setenv("LATCH_DEV_DETECTOR", "1")
    monkeypatch.delenv("LATCH_ADAPTER", raising=False)
    monkeypatch.setattr(detector_trigger, "queue", lambda **kwargs: queued.append(kwargs))
    stop._detector_authority_check(SID, project, str(transcript))
    assert stop._explicit_node_refs(
        f"Node {old} is current. It is reconciled by node {successor}."
    ) == [old, successor]
    assert queued == []


def test_gate_trigger_respects_current_vs_abandoned(tmp_path, monkeypatch):
    project, conn = _setup_project(tmp_path)
    live = db.insert_node(conn, kind="decision", title="live", body="live", status="canonical")
    old = db.insert_node(conn, kind="decision", title="old", body="old", status="stale")
    queued = []
    monkeypatch.setenv("LATCH_DEV_DETECTOR", "1")
    monkeypatch.setattr(gate, "_is_claude_code_session", lambda conn, sid: True)
    monkeypatch.setattr(detector_trigger, "queue", lambda **kwargs: queued.append(kwargs) or True)
    gate._maybe_queue_detector(
        conn,
        project_path=project,
        session_id=SID,
        request="revive old path",
        verdict={
            "recommendation": "MODIFY",
            "active_constraints": [live],
            "current_direction": [live],
            "abandoned_paths": [old],
            "skipped": False,
            "error": None,
        },
        evidence=[
            {"id": live, "status": "canonical"},
            {"id": old, "status": "stale"},
        ],
    )
    conn.close()
    assert queued
    assert queued[0]["trigger_types"] == ["direct_authority_conflict"]
    assert old not in queued[0]["node_ids"]


def test_gate_stale_current_triggers_but_stale_evidence_alone_does_not(tmp_path, monkeypatch):
    project, conn = _setup_project(tmp_path)
    stale = db.insert_node(conn, kind="decision", title="old", body="old", status="stale")
    queued = []
    monkeypatch.setenv("LATCH_DEV_DETECTOR", "1")
    monkeypatch.setattr(gate, "_is_claude_code_session", lambda conn, sid: True)
    monkeypatch.setattr(detector_trigger, "queue", lambda **kwargs: queued.append(kwargs) or True)
    gate._maybe_queue_detector(
        conn,
        project_path=project,
        session_id=SID,
        request="use the current direction",
        verdict={
            "recommendation": "PROCEED",
            "active_constraints": [],
            "current_direction": [stale],
            "abandoned_paths": [],
            "skipped": False,
            "error": None,
        },
        evidence=[{"id": stale, "status": "stale"}],
    )
    assert queued and queued[-1]["trigger_types"] == ["corrected_node_cited_current"]
    queued.clear()
    gate._maybe_queue_detector(
        conn,
        project_path=project,
        session_id=SID,
        request="mention history",
        verdict={
            "recommendation": "PROCEED",
            "active_constraints": [],
            "current_direction": [],
            "abandoned_paths": [stale],
            "skipped": False,
            "error": None,
        },
        evidence=[{"id": stale, "status": "stale"}],
    )
    conn.close()
    assert queued == []


def test_gate_reconciled_current_is_safe_when_successor_is_cited(tmp_path, monkeypatch):
    project, conn = _setup_project(tmp_path)
    old = db.insert_node(conn, kind="decision", title="old", body="old", status="canonical")
    successor = db.insert_node(
        conn, kind="decision", title="new", body="new", status="canonical"
    )
    db.add_edge(conn, src=old, dst=successor, relation="reconciled_by")
    queued = []
    monkeypatch.setenv("LATCH_DEV_DETECTOR", "1")
    monkeypatch.setattr(gate, "_is_claude_code_session", lambda conn, sid: True)
    monkeypatch.setattr(detector_trigger, "queue", lambda **kwargs: queued.append(kwargs) or True)
    verdict = {
        "recommendation": "PROCEED",
        "active_constraints": [],
        "current_direction": [old],
        "abandoned_paths": [],
        "skipped": False,
        "error": None,
    }
    gate._maybe_queue_detector(
        conn,
        project_path=project,
        session_id=SID,
        request="use reconciled decision",
        verdict=verdict,
        evidence=[
            {"id": old, "status": "canonical"},
            {"id": successor, "status": "canonical"},
        ],
    )
    assert queued == []
    gate._maybe_queue_detector(
        conn,
        project_path=project,
        session_id=SID,
        request="omit reconciler",
        verdict=verdict,
        evidence=[{"id": old, "status": "canonical"}],
    )
    conn.close()
    assert queued and queued[-1]["trigger_types"] == ["corrected_node_cited_current"]


def test_independent_trigger_is_not_vetoed_by_missing_authority_snapshot():
    assert detector_trace._should_emit(
        ["corrected_node_cited_current", "direct_authority_conflict"],
        [],
    ) is True
    assert detector_trace._should_emit(
        ["corrected_node_cited_current"],
        [],
    ) is False


def test_prompt_queue_uses_strict_signal_and_degraded_status(monkeypatch):
    queued = []
    monkeypatch.setenv("LATCH_DEV_DETECTOR", "1")
    monkeypatch.setattr(detector_trigger, "queue", lambda **kwargs: queued.append(kwargs) or True)
    entry = {
        "detector_correction_signal": True,
        "retrieval_status": "unavailable",
        "prompt_hash": "abc123",
        "ts": "2026-07-19T20:00:00Z",
        "turn": 4,
        "node_snapshots": [],
    }
    ups._queue_detector("/tmp/project", SID, "/tmp/transcript", entry)
    assert queued[0]["trigger_types"] == ["explicit_correction", "runtime_degraded"]
    assert queued[0]["turn"] == 4


def test_cold_public_runtime_still_queues_dev_detector(monkeypatch, capsys, tmp_path):
    written = []
    queued = []
    lifecycle = []
    prompt = "That is wrong and still broken."
    project = str(tmp_path / "project")
    transcript = str(tmp_path / "session.jsonl")

    monkeypatch.setenv("LATCH_DEV_DETECTOR", "1")
    monkeypatch.setenv("LATCH_ADAPTER", "claude-code")
    monkeypatch.setattr(ups, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(ups, "is_disabled", lambda: False)
    monkeypatch.setattr(ups, "is_in_compact", lambda: False)
    monkeypatch.setattr(ups, "read_hook_input", lambda: {})
    monkeypatch.setattr(ups, "session_id", lambda payload: SID)
    monkeypatch.setattr(ups, "project_cwd", lambda payload: project)
    monkeypatch.setattr(ups, "transcript_path", lambda payload: transcript)
    monkeypatch.setattr(ups, "hook_field", lambda *args: prompt)
    monkeypatch.setattr(ups.mcp_broker, "read_discovery", lambda: None)
    monkeypatch.setattr(ups.mcp_broker, "request_daemon_start", lambda cwd: True)
    monkeypatch.setattr(
        ups.mcp_broker,
        "emit_lifecycle",
        lambda event, **fields: lifecycle.append((event, fields)),
    )
    monkeypatch.setattr(ups, "_write_log", lambda cwd, entry: written.append(entry.copy()))
    monkeypatch.setattr(
        ups,
        "_queue_detector",
        lambda cwd, sid, tpath, entry: queued.append(
            (cwd, sid, tpath, entry.copy())
        ),
    )

    assert ups.main() == 0
    assert written[0]["retrieval_status"] == "unavailable"
    assert written[0]["detector_correction_signal"] is True
    assert queued[0][:3] == (project, SID, transcript)
    assert queued[0][3]["retrieval_status"] == "unavailable"
    assert lifecycle[0][0] == "prompt_retrieval_degraded"
    context = json.loads(capsys.readouterr().out)
    rendered = context["hookSpecificOutput"]["additionalContext"]
    assert "KB retrieval unavailable" in rendered
    assert "distinct from finding no relevant node" in rendered


def test_prompt_queue_is_inert_for_normal_success(monkeypatch):
    queued = []
    monkeypatch.setenv("LATCH_DEV_DETECTOR", "1")
    monkeypatch.setattr(detector_trigger, "queue", lambda **kwargs: queued.append(kwargs) or True)
    ups._queue_detector(
        "/tmp/project",
        SID,
        None,
        {"detector_correction_signal": False, "retrieval_status": "ok"},
    )
    assert queued == []


def test_cli_requires_exact_dev_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LATCH_DEV_DETECTOR", "0")
    rc = detector_trace_cli.main(["detector", "review", "--project", str(tmp_path)])
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "detector_disabled"


def test_cli_reports_snapshot_churn_as_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LATCH_DEV_DETECTOR", "1")
    monkeypatch.setattr(
        detector_trace,
        "open_readonly",
        lambda project: (_ for _ in ()).throw(
            OSError("detector SQLite source changed during snapshot")
        ),
    )

    rc = detector_trace_cli.main([
        "detector",
        "trace",
        "--project",
        str(tmp_path),
        "--session-id",
        SID,
    ])

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "ok": False,
        "reason": "snapshot_unavailable",
        "detail": "detector SQLite source changed during snapshot",
    }


def test_unknown_trigger_is_not_copied_to_projection(tmp_path):
    project, conn = _setup_project(tmp_path)
    transcript = _write_transcript(tmp_path, [("user", "manual trace request")])
    db.upsert_session(conn, SID, project, str(transcript))
    conn.close()
    secret_trigger = "customer-secret-project-name"
    packet = detector_trace.build_trace(
        project_path=project,
        session_id=SID,
        transcript_path=str(transcript),
        trigger_types=[secret_trigger],
    )
    assert packet["trigger"]["types"] == ["manual_trace"]
    assert secret_trigger not in json.dumps(packet["sanitized_projection"])


def test_subject_and_trigger_authority_are_kept_distinct(tmp_path):
    project, conn = _setup_project(tmp_path)
    old = db.insert_node(conn, kind="decision", title="old", body="old", status="canonical")
    transcript = _write_transcript(tmp_path, [
        ("user", "use the current node"),
        ("assistant", f"Node {old} is the current canonical decision."),
    ])
    db.upsert_session(conn, SID, project, str(transcript))
    subject_snapshot = detector_snapshot.snapshot_nodes(conn, [old])
    conn.close()
    _emit_retrieve(
        project,
        "use the current node",
        raw_hits=[(old, 0.9, "decision")],
        snapshots=subject_snapshot,
    )
    conn = db.connect(project)
    successor = db.insert_node(
        conn, kind="decision", title="new", body="new", status="canonical"
    )
    db.add_edge(conn, src=successor, dst=old, relation="supersedes")
    trigger_snapshot = detector_snapshot.snapshot_nodes(conn, [old])
    conn.close()
    prompt_hash = detector_trace.hash_prompt("use the current node")
    log_utils.emit_event(
        "detector_trigger",
        {
            "prompt_hash": prompt_hash,
            "triggers": ["corrected_node_cited_current"],
            "node_snapshots": trigger_snapshot,
        },
        project_path=project,
        session_id=SID,
    )
    packet = detector_trace.build_trace(
        project_path=project,
        session_id=SID,
        transcript_path=str(transcript),
        prompt_hash=prompt_hash,
        event_ts=detector_trace.now_iso(),
        trigger_types=["corrected_node_cited_current"],
        node_ids=[old],
    )
    assert packet["candidate_node_snapshots"][0]["authority"] == "OK"
    assert packet["candidate_node_snapshots"][0]["snapshot_source"] == "subject_retrieval"
    assert packet["trigger_node_snapshots"][0]["authority"] == "STALE"
    assert packet["classification"]["primary_failure_class"] == "contract_gap"
    mechanics = packet["sanitized_projection"]["authority_mechanics"]
    assert mechanics == {"subject": {"OK": 1}, "trigger": {"STALE": 1}}
    assert "subject authority={'OK': 1}" in packet["observed_behavior"]
    assert "trigger authority={'STALE': 1}" in packet["observed_behavior"]


def test_incident_append_is_checked_and_thread_safe(tmp_path):
    project, conn = _setup_project(tmp_path)
    transcript = _write_transcript(tmp_path, [("user", "trace concurrent writes")])
    db.upsert_session(conn, SID, project, str(transcript))
    conn.close()
    base = detector_trace.build_trace(
        project_path=project,
        session_id=SID,
        transcript_path=str(transcript),
        trigger_types=["runtime_degraded"],
    )

    def write_one(index: int):
        packet = copy.deepcopy(base)
        packet["incident_id"] = f"incident-{index}"
        packet["padding"] = "x" * 20000
        return detector_trace.write_incident(packet, project)

    with ThreadPoolExecutor(max_workers=16) as pool:
        paths_written = list(pool.map(write_one, range(24)))
    assert len(set(paths_written)) == 1
    rows = detector_trace.read_incidents(project, limit=100)
    assert len(rows) == 24
    assert {row["incident_id"] for row in rows} == {
        f"incident-{index}" for index in range(24)
    }


def test_incident_append_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    project, conn = _setup_project(tmp_path)
    transcript = _write_transcript(tmp_path, [("user", "trace append failure")])
    db.upsert_session(conn, SID, project, str(transcript))
    conn.close()
    packet = detector_trace.build_trace(
        project_path=project, session_id=SID, transcript_path=str(transcript)
    )

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(detector_trace, "_append_jsonl_checked", fail)
    with pytest.raises(OSError, match="disk full"):
        detector_trace.write_incident(packet, project)


def test_read_incidents_nonpositive_limit_returns_nothing(monkeypatch):
    monkeypatch.setattr(detector_trace, "_read_stream_rows", lambda *args: [{"id": 1}])
    assert detector_trace.read_incidents("/tmp/project", limit=0) == []
    assert detector_trace.read_incidents("/tmp/project", limit=-1) == []


def test_non_claude_adapter_disables_automatic_detector(monkeypatch):
    monkeypatch.setenv("LATCH_DEV_DETECTOR", "1")
    monkeypatch.setenv("LATCH_ADAPTER", "vscode-copilot")
    assert detector_trigger.enabled() is False
    assert ups._detector_auto_enabled() is False
    assert stop._detector_auto_enabled() is False
    assert gate._detector_auto_enabled() is False


def test_flag_off_stop_import_does_not_load_detector_trigger(tmp_path):
    code = f'''
import builtins, runpy, sys
sys.path[:0] = [{str(_SRC)!r}, {str(_SRC / "hooks")!r}]
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "detector_trigger":
        raise RuntimeError("eager detector import")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
runpy.run_path({str(_SRC / "hooks" / "stop.py")!r})
'''
    env = os.environ.copy()
    env["LATCH_DEV_DETECTOR"] = "0"
    subprocess.run([sys.executable, "-c", code], env=env, check=True)


@pytest.mark.skipif(
    os.name == "nt",
    reason="the dev-only shell wrapper uses POSIX venv layout and bash",
)
def test_wrapper_prefers_repo_venv_without_python_on_path(tmp_path):
    fake_root = tmp_path / "latch"
    (fake_root / ".venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, fake_root / ".venv" / "bin" / "python")
    os.symlink(_SRC, fake_root / "src", target_is_directory=True)
    env = os.environ.copy()
    env.pop("LATCH_PYTHON", None)
    env.pop("CLAUDE_KB_PYTHON", None)
    env["LATCH_HOME"] = str(fake_root)
    env["LATCH_DEV_DETECTOR"] = "1"
    env["PATH"] = "/definitely-not-on-path"
    result = subprocess.run(
        [
            "/bin/bash",
            str(_ROOT / "bin" / "run_latch_detector_trace.sh"),
            "review",
            "--project",
            str(tmp_path),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout)["ok"] is True
