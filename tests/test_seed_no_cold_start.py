"""Hermetic acceptance tests for Latch's no-cold-start seed path."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import sqlite3

import pytest

from latch.store import artifacts
from latch.gate import budget
from latch.store import db
from latch.retrieval import feeders
from latch.gate import gate
from latch.pipeline import model_backends
from latch.store import paths
from latch.pipeline import seed


def _source(
    source_id: str,
    *,
    agent: str = "claude",
    mtime: str = "2026-07-16T12:00:00+00:00",
    text: str = "[user] We decided to keep local SQLite state.",
    value_score: float = 0.0,
) -> seed.SeedSource:
    digest = seed.source_content_digest(text)
    return seed.SeedSource(
        id=source_id,
        agent=agent,
        path=f"/private/local-history/{source_id.replace(':', '-')}.jsonl",
        mtime=mtime,
        text=text,
        content_digest=digest,
        value_score=value_score,
    )


def _candidate(
    source: seed.SeedSource,
    *,
    kind: str = "decision",
    title: str = "Keep local SQLite state",
    claim: str = "Use local SQLite state so installation remains self-contained.",
    signals: list[str] | None = None,
    confidence: float = 0.9,
    workstream_key: str | None = None,
) -> seed.SeedCandidate:
    chosen_signals = list(signals or ["decision", "llm_seed"])
    body = (
        "Seed candidate from prior local agent history. Treat as low-authority "
        "staging evidence until reviewed/promoted.\n\n"
        f"{claim}\n\n"
        f"Signals: {', '.join(chosen_signals)}\n\n"
        "Source evidence:\n"
        f"- {source.id}; observed_at={source.mtime}; "
        f"digest={source.content_digest[:16]}"
    )
    return seed.SeedCandidate(
        kind=kind,
        title=title,
        body=body,
        confidence=confidence,
        signals=chosen_signals,
        source_ids=[source.id],
        source_paths=[source.path],
        source_mtimes=[source.mtime],
        source_digests=[source.content_digest],
        llm_used=True,
        workstream_key=workstream_key,
    )


def test_source_selection_reserves_recent_value_and_exact_cursor_inputs():
    cursor = _source(
        "cursor:explicit",
        agent="cursor",
        mtime="2026-01-01T00:00:00+00:00",
        value_score=0.0,
    )
    newest = [
        _source(
            f"claude:recent-{index}",
            mtime=f"2026-07-{15 + index:02d}T12:00:00+00:00",
            value_score=0.0,
        )
        for index in range(2)
    ]
    valuable_old = [
        _source(
            f"codex:valuable-{index}",
            agent="codex",
            mtime=f"2026-05-{index + 1:02d}T12:00:00+00:00",
            value_score=100.0 - index,
        )
        for index in range(10)
    ]

    selected = seed.select_sources(
        [cursor, *valuable_old, *newest], max_sessions=10,
    )
    selected_ids = {item.id for item in selected}

    assert len(selected) == 10
    assert cursor.id in selected_ids
    assert {item.id for item in newest} <= selected_ids
    assert valuable_old[0].id in selected_ids


def test_source_selection_does_not_make_cursor_history_mandatory():
    cursor_history = seed.SeedSource(
        **{
            **_source(
                "cursor:history",
                agent="cursor",
                mtime="2026-01-01T00:00:00+00:00",
                value_score=0.0,
            ).__dict__,
            "history_discovered": True,
        }
    )
    recent = _source(
        "claude:recent",
        mtime="2026-07-16T12:00:00+00:00",
        value_score=0.0,
    )
    valuable = _source(
        "codex:valuable",
        agent="codex",
        mtime="2026-05-01T12:00:00+00:00",
        value_score=100.0,
    )

    selected = seed.select_sources(
        [cursor_history, recent, valuable],
        max_sessions=2,
    )

    assert {source.id for source in selected} == {recent.id, valuable.id}


def test_redaction_precedes_prompt_preview_cache_and_persisted_body(
    tmp_path, monkeypatch,
):
    raw_secret = "sk-proj-" + "A" * 28
    bearer = "Bearer " + "b" * 30
    raw = (
        "[user] Always keep credentials local. "
        f"api_key={raw_secret} Authorization: {bearer} "
        "postgres://alice:hunter2secret@localhost/db"
    )
    safe_text, redaction_count = seed.redact_seed_text(raw)
    # Detector hits are intentionally aggregate rather than a unique-secret
    # count; layered high-confidence detectors may both match one credential.
    assert redaction_count >= 3
    assert raw_secret not in safe_text
    assert bearer not in safe_text
    assert "hunter2secret" not in safe_text

    source = _source("cursor:redacted", agent="cursor", text=safe_text)
    source = seed.SeedSource(
        **{
            **source.__dict__,
            "redaction_count": redaction_count,
        }
    )
    project = tmp_path / "private-project"
    project.mkdir()
    prompts: list[str] = []

    monkeypatch.setattr(
        budget,
        "check_and_record",
        lambda *_args, **_kwargs: (True, {"count_nonheal": 1}),
    )

    def invoke(prompt: str, **_kwargs):
        prompts.append(prompt)
        return model_backends.ModelCallResult(
            text=json.dumps({
                "seed_candidates": [{
                    "kind": "preference",
                    "title": "Keep credentials local",
                    "body": f"Never publish the credential {raw_secret}.",
                    "confidence": 0.91,
                    "signals": ["preference"],
                }],
            }),
            error=None,
            timed_out=False,
            backend="codex",
        )

    monkeypatch.setattr(model_backends, "invoke_prompt", invoke)
    candidates = seed.llm_candidates(
        [source],
        project_path=str(project),
        max_calls=1,
        max_candidates=5,
        backend="codex",
    )

    assert len(prompts) == 1
    assert raw_secret not in prompts[0]
    assert source.path not in prompts[0]
    assert str(project.resolve()) not in prompts[0]
    assert len(candidates) == 1
    assert raw_secret not in candidates[0].body
    assert "<redacted:openai-key>" in candidates[0].body

    digest = seed.write_cursor_seed_preview(
        project_path=str(project),
        session_id="redacted-session",
        sources=[source],
        candidates=candidates,
        llm_estimate=1,
    )
    cache_path = seed._cursor_seed_preview_path(str(project), "redacted-session")
    cached = cache_path.read_text(encoding="utf-8")
    assert raw_secret not in cached
    assert raw not in cached
    assert json.loads(cached)["sources"][0]["text"] == ""

    body = seed.body_with_import_receipt(
        candidates[0],
        import_key=digest,
        project_path=str(project),
        workstream_id=None,
    )
    assert raw_secret not in body
    assert source.path not in body
    assert source.id in body
    assert source.content_digest in body


def test_model_calls_are_source_bounded_valid_empty_is_success_and_output_is_capped(
    tmp_path, monkeypatch,
):
    """A node-dense session is no longer truncated per source.

    A flat per-source cap of 6 held total recall to a 60.6% ceiling against
    the restored KB: the richest sessions lost the most. Boundedness now
    comes from the global --max-candidates bound instead.
    """
    sources = [_source(f"claude:session-{index}") for index in range(3)]
    prompts: list[str] = []
    responses = [
        json.dumps({"seed_candidates": []}),
        "not-json",
        json.dumps({
            "seed_candidates": [
                {
                    "kind": kind,
                    "title": title,
                    "body": body,
                    "confidence": 0.9,
                    "signals": signals,
                }
                for kind, title, body, signals in [
                    ("decision", "Choose WAL journaling", "Use write-ahead logging for crash recovery.", ["decision"]),
                    ("preference", "Prefer concise reviews", "Keep review summaries compact and evidence backed.", ["preference"]),
                    ("fact", "Migration ordering correction", "Create additive tables before reading import ledgers.", ["correction"]),
                    ("open_question", "Choose retention window", "We still need to decide the long-term retention window.", ["open_question"]),
                    ("workstream", "Activation improvements", "Continue the multi-session activation improvement lane.", ["ongoing_workstream"]),
                    ("idea", "Add source receipts", "Expose structured source receipts during review.", ["idea"]),
                    ("fact", "Seventh candidate", "This item survives: there is no per-source cap.", ["verified_outcome"]),
                    ("fact", "Eighth candidate", "This item also survives; the global bound applies instead.", ["verified_outcome"]),
                ]
            ],
        }),
    ]
    monkeypatch.setattr(
        budget,
        "check_and_record",
        lambda *_args, **_kwargs: (True, {"count_nonheal": 1}),
    )
    original_selection = seed.balanced_candidate_selection
    selection_calls: list[tuple[int, int]] = []

    def capture_selection(candidates, *, max_candidates):
        selection_calls.append((len(candidates), max_candidates))
        return original_selection(candidates, max_candidates=max_candidates)

    monkeypatch.setattr(seed, "balanced_candidate_selection", capture_selection)

    def invoke(prompt: str, **_kwargs):
        prompts.append(prompt)
        return model_backends.ModelCallResult(
            text=responses[len(prompts) - 1],
            error=None,
            timed_out=False,
            backend="codex",
        )

    monkeypatch.setattr(model_backends, "invoke_prompt", invoke)
    stats: dict[str, object] = {}
    candidates = seed.llm_candidates(
        sources,
        project_path=str(tmp_path),
        max_calls=20,
        max_candidates=20,
        backend="codex",
        stats=stats,
    )

    assert len(prompts) == min(len(sources), 20)
    assert stats["attempted"] == 3
    assert stats["succeeded"] == 2
    assert stats["failed"] == 1
    assert stats["succeeded_source_ids"] == [sources[0].id, sources[2].id]
    assert stats["failed_source_ids"] == [sources[1].id]
    assert stats["accepted_candidates_by_source"][sources[0].id] == 0
    assert stats["accepted_candidates_by_source"][sources[2].id] == 8
    assert selection_calls == [(8, 20)]
    assert seed.MAX_CANDIDATES_PER_SOURCE is None
    assert len(candidates) <= 20
    assert all(candidate.source_ids == [sources[2].id] for candidate in candidates)


def test_dedupe_unions_aligned_provenance_preserves_conflicts_and_balances_sections():
    first_source = _source("claude:one", mtime="2026-07-15T00:00:00+00:00")
    second_source = _source("codex:two", agent="codex", mtime="2026-07-16T00:00:00+00:00")
    first = _candidate(first_source)
    second = _candidate(second_source)

    merged = seed.dedupe_candidates([first, second])
    assert len(merged) == 1
    refs = {ref["id"]: ref for ref in seed.candidate_source_refs(merged[0])}
    assert set(refs) == {first_source.id, second_source.id}
    assert refs[first_source.id]["path"] == first_source.path
    assert refs[first_source.id]["mtime"] == first_source.mtime
    assert refs[first_source.id]["digest"] == first_source.content_digest
    assert refs[second_source.id]["path"] == second_source.path
    assert "corroborated" in merged[0].signals
    assert first_source.id in merged[0].body and second_source.id in merged[0].body

    positive = _candidate(
        first_source,
        title="Use Redis for the shared cache",
        claim="Use Redis for the shared cache in the hosted service.",
    )
    negative = _candidate(
        second_source,
        title="Do not use Redis for the shared cache",
        claim="Do not use Redis for the shared cache in the hosted service.",
    )
    assert len(seed.dedupe_candidates([positive, negative])) == 2

    section_candidates = [
        _candidate(first_source, title="Decision", claim="Keep the durable decision.", signals=["decision"]),
        _candidate(first_source, kind="preference", title="Preference", claim="Prefer concise output.", signals=["preference"]),
        _candidate(first_source, kind="fact", title="Outcome", claim="The migration completed.", signals=["verified_outcome"]),
        _candidate(first_source, kind="fact", title="Alignment", claim="The agent revived a ruled-out daemon.", signals=["possible_agent_mistake"], confidence=0.9),
        _candidate(first_source, kind="workstream", title="Continuity", claim="Continue activation work.", signals=["ongoing_workstream"]),
    ]
    section_candidates.extend(
        _candidate(
            first_source,
            title=f"Extra decision {index}",
            claim=f"Keep extra durable decision number {index}.",
            signals=["decision"],
        )
        for index in range(8)
    )
    selected = seed.balanced_candidate_selection(section_candidates, max_candidates=5)
    assert {seed.report_section_key(candidate) for candidate in selected} == {
        key for key, _title, _summary in seed.REPORT_SECTION_DEFS
    }


def test_apply_is_exactly_idempotent_staging_repo_scoped_and_source_ledged(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "idempotent-project"
    project.mkdir()
    source = _source("claude:idempotent")
    candidate = _candidate(source)

    first = seed.apply_candidates(
        [candidate],
        project_path=str(project),
        sources=[source],
        workstream_scope="project",
    )
    second = seed.apply_candidates(
        [candidate],
        project_path=str(project),
        sources=[source],
        workstream_scope="project",
    )

    assert first.complete and len(first.inserted_ids) == 1
    assert second.complete and second.inserted_ids == []
    assert second.skipped_node_ids == first.inserted_ids
    pending, applied = seed.split_applied_sources(
        [source], project_path=str(project), workstream_scope="project",
    )
    assert pending == [] and applied == [source]

    conn = db.connect(str(project))
    try:
        node = db.get_node(conn, first.inserted_ids[0])
        assert node is not None
        assert node["status"] == "staging"
        assert source.path not in node["body"]
        assert "authority: staging" in node["body"]
        assert "Latch-Seed-Import-Key" in node["body"]

        node_artifacts = artifacts.get_node_artifacts(conn, node["id"])
        assert len(node_artifacts) == 1
        assert node_artifacts[0]["repo"] == artifacts.canonicalize_repo(str(project))
        assert node_artifacts[0]["path"] is None

        source_key = seed.seed_source_import_key(
            source, project_path=str(project), workstream_scope="project",
        )
        source_row = db.get_seed_source_import(conn, source_key)
        assert source_row is not None
        assert source_row["state"] == "applied"
        assert source_row["source_path"] == source.path
        assert source_row["source_digest"] == source.content_digest

        candidate_key = seed.candidate_import_key(
            candidate, project_path=str(project), target_workstream_id=None,
        )
        candidate_row = db.get_seed_import(conn, candidate_key)
        assert candidate_row is not None
        assert candidate_row["state"] == "applied"
        assert candidate_row["node_id"] == node["id"]
        assert conn.execute("SELECT COUNT(*) FROM focus").fetchone()[0] == 0
    finally:
        conn.close()


def test_new_and_existing_workstreams_attach_reuse_and_do_not_gain_focus(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)

    existing_project = tmp_path / "existing-workstream"
    existing_project.mkdir()
    conn = db.connect(str(existing_project))
    try:
        existing_id = db.insert_node(
            conn,
            kind="workstream",
            title="Reviewed activation lane",
            body="Existing reviewed workstream.",
            status="canonical",
        )
    finally:
        conn.close()
    existing_source = _source("codex:existing", agent="codex")
    existing_child = _candidate(existing_source, title="Keep reviewed installer copy")
    existing_scope = f"existing:{existing_id}"
    existing_first = seed.apply_candidates(
        [existing_child],
        project_path=str(existing_project),
        existing_workstream_id=existing_id,
        sources=[existing_source],
        workstream_scope=existing_scope,
    )
    existing_second = seed.apply_candidates(
        [existing_child],
        project_path=str(existing_project),
        existing_workstream_id=existing_id,
        sources=[existing_source],
        workstream_scope=existing_scope,
    )
    assert len(existing_first.inserted_ids) == 1
    assert existing_second.inserted_ids == []
    conn = db.connect(str(existing_project))
    try:
        child = db.get_node(conn, existing_first.inserted_ids[0])
        parent = db.get_node(conn, existing_id)
        assert child is not None and child["workstream_id"] == existing_id
        assert child["status"] == "staging"
        assert parent is not None and parent["status"] == "canonical"
        assert conn.execute("SELECT COUNT(*) FROM focus").fetchone()[0] == 0
    finally:
        conn.close()

    new_project = tmp_path / "new-workstream"
    new_project.mkdir()
    new_source = _source("claude:new-workstream")
    parent_candidate = seed.new_workstream_candidate("No-cold-start activation")
    requested_key = parent_candidate.workstream_key
    assert requested_key is not None
    new_child = _candidate(
        new_source,
        title="Seed decisions during activation",
        workstream_key=requested_key,
    )
    new_first = seed.apply_candidates(
        [new_child, parent_candidate],
        project_path=str(new_project),
        sources=[new_source],
        workstream_scope=requested_key,
    )
    new_second = seed.apply_candidates(
        [new_child, parent_candidate],
        project_path=str(new_project),
        sources=[new_source],
        workstream_scope=requested_key,
    )
    parent_id = new_first.workstream_attachments[requested_key]
    assert len(new_first.inserted_ids) == 2
    assert new_second.inserted_ids == []
    assert new_second.workstream_attachments[requested_key] == parent_id
    conn = db.connect(str(new_project))
    try:
        parent = db.get_node(conn, parent_id)
        children = conn.execute(
            "SELECT * FROM nodes WHERE workstream_id = ?", (parent_id,),
        ).fetchall()
        assert parent is not None and parent["status"] == "staging"
        assert len(children) == 1 and children[0]["status"] == "staging"
        assert conn.execute("SELECT COUNT(*) FROM focus").fetchone()[0] == 0
    finally:
        conn.close()


def test_seeded_forward_looking_children_feed_attached_workstream(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "seeded-feeders"
    project.mkdir()
    conn = db.connect(str(project))
    try:
        workstream_id = db.insert_node(
            conn,
            kind="workstream",
            title="Seed activation lane",
            body="Reviewed workstream for imported continuity evidence.",
            status="canonical",
        )
    finally:
        conn.close()

    source = _source("codex:seeded-feeders", agent="codex")
    candidates = [
        _candidate(
            source,
            kind="idea",
            title="Keep a bounded activation report",
            claim="Explore a bounded activation report before expanding scope.",
            signals=["idea", "llm_seed"],
        ),
        _candidate(
            source,
            kind="open_question",
            title="Which activation evidence is sufficient?",
            claim="Decide which activation evidence is sufficient for review.",
            signals=["open_question", "llm_seed"],
        ),
    ]
    result = seed.apply_candidates(
        candidates,
        project_path=str(project),
        existing_workstream_id=workstream_id,
        sources=[source],
        workstream_scope=f"existing:{workstream_id}",
    )
    assert len(result.inserted_ids) == 2

    conn = db.connect(str(project))
    try:
        rows = feeders.open_feeders(conn, workstream_id, limit=10)
        assert {row["id"] for row in rows} == set(result.inserted_ids)
        assert {row["via"] for row in rows} == {"member"}
    finally:
        conn.close()


def test_partial_apply_recovers_checkpointed_nodes_without_duplicates(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "partial-recovery"
    project.mkdir()
    source = _source("claude:partial")
    candidates = [
        _candidate(
            source,
            title="Keep atomic source ledgers",
            claim="Record source completion only after candidate writes finish.",
        ),
        _candidate(
            source,
            kind="preference",
            title="Preserve structured provenance",
            claim="Keep source identity and digest aligned during import.",
            signals=["preference", "llm_seed"],
        ),
    ]
    original_capture = artifacts.capture_for_node
    calls = 0

    def fail_second_capture(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated artifact checkpoint interruption")
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(artifacts, "capture_for_node", fail_second_capture)
    partial = seed.apply_candidates(
        candidates,
        project_path=str(project),
        sources=[source],
        workstream_scope="project",
    )
    assert not partial.complete
    assert {failure["error_code"] for failure in partial.failures} == {
        "node_write_failed",
    }

    conn = db.connect(str(project))
    try:
        before_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        assert before_count == 2
    finally:
        conn.close()

    monkeypatch.setattr(artifacts, "capture_for_node", original_capture)
    recovered = seed.apply_candidates(
        candidates,
        project_path=str(project),
        sources=[source],
        workstream_scope="project",
    )
    assert recovered.complete
    assert recovered.inserted_ids == []
    conn = db.connect(str(project))
    try:
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == before_count
        assert conn.execute("SELECT COUNT(*) FROM node_artifact").fetchone()[0] == 2
        source_key = seed.seed_source_import_key(
            source, project_path=str(project), workstream_scope="project",
        )
        assert db.get_seed_source_import(conn, source_key)["state"] == "applied"
    finally:
        conn.close()


def test_attached_candidate_write_failure_uses_write_telemetry_and_recovers(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "attached-write-recovery"
    project.mkdir()
    conn = db.connect(str(project))
    try:
        parent_id = db.insert_node(
            conn,
            kind="workstream",
            title="Reviewed activation lane",
            body="Existing reviewed workstream.",
            status="canonical",
        )
    finally:
        conn.close()

    scope = f"existing:{parent_id}"
    source = _source("codex:attached-write", agent="codex")
    candidate = _candidate(
        source,
        title="Keep attached seed writes retryable",
        workstream_key=scope,
    )
    candidate_key = seed.candidate_import_key(
        candidate,
        project_path=str(project),
        target_workstream_id=parent_id,
    )
    source_key = seed.seed_source_import_key(
        source,
        project_path=str(project),
        workstream_scope=scope,
    )
    original_capture = artifacts.capture_for_node
    capture_calls = 0

    def fail_first_capture(*args, **kwargs):
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == 1:
            raise RuntimeError("simulated attached artifact checkpoint failure")
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(artifacts, "capture_for_node", fail_first_capture)
    failed = seed.apply_candidates(
        [candidate],
        project_path=str(project),
        existing_workstream_id=parent_id,
        sources=[source],
        workstream_scope=scope,
    )
    assert not failed.complete
    assert failed.failures == [
        {"import_key": candidate_key, "error_code": "node_write_failed"},
        {"import_key": source_key, "error_code": "node_write_failed"},
    ]
    assert len(failed.inserted_ids) == 1
    child_id = failed.inserted_ids[0]

    conn = db.connect(str(project))
    try:
        child = db.get_node(conn, child_id)
        assert child is not None and child["workstream_id"] == parent_id
        candidate_row = db.get_seed_import(conn, candidate_key)
        source_row = db.get_seed_source_import(conn, source_key)
        assert candidate_row is not None
        assert candidate_row["state"] == "failed"
        assert candidate_row["error_code"] == "node_write_failed"
        assert candidate_row["node_id"] == child_id
        assert source_row is not None
        assert source_row["state"] == "failed"
        assert source_row["error_code"] == "node_write_failed"
        before_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    finally:
        conn.close()

    recovered = seed.apply_candidates(
        [candidate],
        project_path=str(project),
        existing_workstream_id=parent_id,
        sources=[source],
        workstream_scope=scope,
    )
    assert recovered.complete
    assert recovered.inserted_ids == []
    assert recovered.resumed_import_keys == [candidate_key]
    assert recovered.resumed_node_ids == [child_id]

    conn = db.connect(str(project))
    try:
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == before_count
        candidate_row = db.get_seed_import(conn, candidate_key)
        source_row = db.get_seed_source_import(conn, source_key)
        assert candidate_row is not None
        assert candidate_row["state"] == "applied"
        assert candidate_row["error_code"] is None
        assert candidate_row["attempt_count"] == 2
        assert source_row is not None
        assert source_row["state"] == "applied"
        assert source_row["error_code"] is None
        assert source_row["attempt_count"] == 2
        assert len(artifacts.get_node_artifacts(conn, child_id)) == 1
    finally:
        conn.close()


def test_unlatched_apply_blocks_before_lock_or_database_write(tmp_path, monkeypatch):
    source = _source("claude:blocked")
    candidate = _candidate(source)
    database_touched = False

    def forbidden_connect(_project_path):
        nonlocal database_touched
        database_touched = True
        raise AssertionError("database must not be opened while Unlatched")

    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: True)
    monkeypatch.setattr(db, "connect", forbidden_connect)
    with pytest.raises(seed.SeedWriteBlocked) as exc_info:
        seed.apply_candidates(
            [candidate],
            project_path=str(tmp_path / "unlatched"),
            sources=[source],
        )
    assert exc_info.value.reason == "unlatched"
    assert database_touched is False


def test_same_source_id_revisions_keep_outcomes_separate_and_defer_unattempted(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "source-revisions"
    project.mkdir()
    first = _source(
        "claude:mutable-session",
        mtime="2026-07-16T12:00:00+00:00",
        text="[user] We decided to keep the first revision.",
        value_score=10.0,
    )
    second = _source(
        "claude:mutable-session",
        mtime="2026-07-15T12:00:00+00:00",
        text="[user] We decided to keep the amended second revision.",
        value_score=1.0,
    )
    assert first.id == second.id
    assert first.content_digest != second.content_digest
    assert seed.source_revision_token(first) != seed.source_revision_token(second)

    monkeypatch.setattr(seed, "discover_sources", lambda **_kwargs: [first, second])
    monkeypatch.setattr(
        budget,
        "check_and_record",
        lambda *_args, **_kwargs: (True, {"count_nonheal": 1}),
    )
    monkeypatch.setattr(
        model_backends,
        "invoke_prompt",
        lambda *_args, **_kwargs: model_backends.ModelCallResult(
            text=json.dumps({
                "seed_candidates": [{
                    "kind": "decision",
                    "title": "Keep the selected revision",
                    "body": "Use the selected revision as reviewed evidence.",
                    "confidence": 0.9,
                    "signals": ["decision"],
                }],
            }),
            error=None,
            timed_out=False,
            backend="codex",
        ),
    )
    original_apply = seed.apply_candidates
    captured_apply_sources: list[seed.SeedSource] = []

    def capture_apply(_candidates, **kwargs):
        captured_apply_sources.extend(kwargs["sources"])
        return seed.SeedApplyResult()

    monkeypatch.setattr(seed, "apply_candidates", capture_apply)
    rc = seed.main([
        "--project", str(project),
        "--source", "claude",
        "--lookback-days", "5",
        "--last-sessions", "2",
        "--max-llm-calls", "1",
        "--max-candidates", "5",
        "--format", "json",
        "--apply",
        "--yes",
    ])
    output = capsys.readouterr().out
    assert rc == 0
    assert len(captured_apply_sources) == 1
    assert captured_apply_sources[0].content_digest == first.content_digest
    assert '"sources_deferred": 1' in output

    # The deferred revision never enters the write ledger. When both revisions
    # are later attempted, their terminal outcomes remain digest-specific even
    # though the provider-level source id is identical.
    monkeypatch.setattr(seed, "apply_candidates", original_apply)
    candidate = _candidate(first, title="Keep digest-specific source outcomes")
    result = original_apply(
        [candidate],
        project_path=str(project),
        sources=[first, second],
        workstream_scope="project",
        source_failure_codes={
            seed.source_revision_token(second): "extractor_failed",
        },
    )
    assert not result.complete
    first_key = seed.seed_source_import_key(
        first, project_path=str(project), workstream_scope="project",
    )
    second_key = seed.seed_source_import_key(
        second, project_path=str(project), workstream_scope="project",
    )
    assert first_key != second_key
    conn = db.connect(str(project))
    try:
        assert db.get_seed_source_import(conn, first_key)["state"] == "applied"
        failed = db.get_seed_source_import(conn, second_key)
        assert failed["state"] == "failed"
        assert failed["error_code"] == "extractor_failed"
    finally:
        conn.close()


def test_discovery_redacts_pem_and_bearer_before_source_truncation(tmp_path):
    project = tmp_path / "redaction-boundary-project"
    project.mkdir()
    claude_home = tmp_path / "claude-home"
    transcript_dir = claude_home / "projects" / "history"
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / "boundary.jsonl"
    pem_payload = "PRIVATEPAYLOAD" * (seed.MAX_SOURCE_CHARS // 10)
    bearer_payload = "B" * (seed.MAX_SOURCE_CHARS + 101)
    content = (
        "-----BEGIN PRIVATE KEY-----\n"
        f"{pem_payload}\n"
        "-----END PRIVATE KEY-----\n"
        f"Authorization: Bearer {bearer_payload}\n"
        "[user] Keep all credentials local."
    )
    transcript.write_text(
        json.dumps({
            "type": "user",
            "message": {"content": content},
        }) + "\n",
        encoding="utf-8",
    )
    observed_now = datetime.fromtimestamp(
        transcript.stat().st_mtime, tz=timezone.utc,
    )

    sources = seed.discover_sources(
        source="claude",
        project_path=str(project),
        lookback_days=5,
        max_sessions=1,
        claude_home=str(claude_home),
        codex_home=str(tmp_path / "codex-home"),
        all_projects=True,
        now=observed_now,
    )

    assert len(sources) == 1
    assert sources[0].redaction_count >= 2
    assert "<redacted:private-key>" in sources[0].text
    assert "<redacted:bearer-token>" in sources[0].text
    assert "PRIVATEPAYLOAD" * 10 not in sources[0].text
    assert "B" * 100 not in sources[0].text
    assert len(sources[0].text) <= seed.MAX_SOURCE_CHARS


def test_llm_invocation_honours_an_explicit_cap_above_the_default(
    tmp_path, monkeypatch,
):
    """20 is the default, not a ceiling.

    It used to be a hard clamp, so a user who asked for more coverage silently
    got 20 sessions read and the rest skipped without being told — which reads
    as latch missing their history. An explicit cap is now honoured up to
    HARD_MAX_LLM_CALLS_CEILING, which exists only so a typo cannot start an
    unbounded model-call run.
    """
    sources = [_source(f"claude:explicit-cap-{index}") for index in range(25)]
    calls = 0
    monkeypatch.setattr(
        budget,
        "check_and_record",
        lambda *_args, **_kwargs: (True, {"count_nonheal": 1}),
    )

    def invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return model_backends.ModelCallResult(
            text=json.dumps({"seed_candidates": []}),
            error=None,
            timed_out=False,
            backend="codex",
        )

    monkeypatch.setattr(model_backends, "invoke_prompt", invoke)
    stats: dict[str, object] = {}
    assert seed.llm_candidates(
        sources,
        project_path=str(tmp_path),
        max_calls=25,
        max_candidates=20,
        backend="codex",
        stats=stats,
    ) == []
    assert calls == 25
    assert stats["attempted"] == 25
    assert seed.DEFAULT_MAX_LLM_CALLS_BASE == 20
    assert seed.HARD_MAX_LLM_CALLS_CEILING >= 25


def test_llm_invocation_still_has_an_absolute_outer_ceiling(tmp_path, monkeypatch):
    """Removing the clamp must not make the run unbounded."""
    sources = [_source(f"claude:ceiling-{index}") for index in range(3)]
    calls = 0
    monkeypatch.setattr(
        budget,
        "check_and_record",
        lambda *_args, **_kwargs: (True, {"count_nonheal": 1}),
    )
    monkeypatch.setattr(seed, "HARD_MAX_LLM_CALLS_CEILING", 2)

    def invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return model_backends.ModelCallResult(
            text=json.dumps({"seed_candidates": []}),
            error=None,
            timed_out=False,
            backend="codex",
        )

    monkeypatch.setattr(model_backends, "invoke_prompt", invoke)
    assert seed.llm_candidates(
        sources,
        project_path=str(tmp_path),
        max_calls=10_000,
        max_candidates=20,
        backend="codex",
        stats={},
    ) == []
    assert calls == 2


def test_existing_workstream_preflight_stops_before_discovery_or_model(
    tmp_path, monkeypatch, capsys,
):
    project = tmp_path / "missing-existing-workstream"
    project.mkdir()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid workstream must stop before source/model work")

    monkeypatch.setattr(seed, "discover_sources", forbidden)
    monkeypatch.setattr(seed, "llm_candidates", forbidden)
    monkeypatch.setattr(model_backends, "invoke_prompt", forbidden)
    rc = seed.main([
        "--project", str(project),
        "--source", "claude",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--max-llm-calls", "1",
        "--workstream-id", "999999",
        "--yes",
    ])
    captured = capsys.readouterr()
    assert rc == 2
    assert "project KB does not exist" in captured.err
    assert not paths.db_path(str(project)).exists()


def test_targeted_new_workstream_accepts_only_explicit_requested_children():
    source = _source("claude:targeted")

    def inputs():
        return [
            _candidate(
                source,
                title="Seed activation decisions",
                claim="Seed activation decisions from reviewed history.",
                workstream_key="requested",
            ),
            _candidate(
                source,
                title="Unrelated branch cleanup",
                claim="Clean an unrelated branch after release.",
                workstream_key=None,
            ),
            _candidate(
                source,
                title="Other keyed lane",
                claim="Continue an unrelated explicitly keyed lane.",
                workstream_key="another-lane",
            ),
        ]

    scoped = seed.apply_requested_workstream_scope(
        inputs(),
        new_workstream="Activation seeding",
        workstream_id=None,
        max_candidates=4,
    )
    assert [candidate.kind for candidate in scoped] == ["workstream", "decision"]
    assert scoped[1].title == "Seed activation decisions"
    assert scoped[1].workstream_key == seed.requested_workstream_key(
        "Activation seeding"
    )

    parent_only = seed.apply_requested_workstream_scope(
        inputs(),
        new_workstream="Activation seeding",
        workstream_id=None,
        max_candidates=1,
    )
    assert len(parent_only) == 1
    assert parent_only[0].kind == "workstream"


def test_hostile_existing_workstream_key_is_cleared_and_written_unattached(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "hostile-existing-key"
    project.mkdir()
    source = _source("claude:hostile-key")
    hostile = seed.candidate_from_llm_item({
        "kind": "decision",
        "title": "Hostile model attachment",
        "body": "This evidence must remain unattached without a user target.",
        "confidence": 0.9,
        "signals": ["decision"],
        "workstream_key": "existing:424242",
    }, source)
    assert hostile is not None and hostile.workstream_key == "existing:424242"

    result = seed.apply_candidates(
        [hostile],
        project_path=str(project),
        sources=[source],
        workstream_scope="project",
    )
    assert result.complete and len(result.inserted_ids) == 1
    assert hostile.workstream_key is None
    conn = db.connect(str(project))
    try:
        node = db.get_node(conn, result.inserted_ids[0])
        assert node is not None and node["workstream_id"] is None
    finally:
        conn.close()


def test_cross_batch_workstream_reuse_and_ambiguous_or_stale_parent_fail_closed(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "cross-batch-workstream"
    project.mkdir()
    source = _source("claude:cross-batch")
    parent = seed.new_workstream_candidate("Activation imports")
    key = parent.workstream_key
    assert key is not None
    first_child = _candidate(
        source,
        title="Import activation decisions",
        claim="Import reviewed activation decisions during initialization.",
        workstream_key=key,
    )
    first = seed.apply_candidates(
        [parent, first_child],
        project_path=str(project),
        sources=[source],
        workstream_scope=key,
    )
    parent_id = first.workstream_attachments[key]
    assert first.complete and len(first.inserted_ids) == 2

    second_child = _candidate(
        source,
        title="Import activation constraints",
        claim="Import reviewed activation constraints in a later batch.",
        workstream_key=key,
    )
    second = seed.apply_candidates(
        [seed.new_workstream_candidate("Activation imports"), second_child],
        project_path=str(project),
        sources=[source],
        workstream_scope=key,
    )
    assert second.complete and len(second.inserted_ids) == 1
    assert second.workstream_attachments[key] == parent_id
    conn = db.connect(str(project))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind = 'workstream'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE workstream_id = ?", (parent_id,),
        ).fetchone()[0] == 2
        conn.execute("UPDATE nodes SET status = 'stale' WHERE id = ?", (parent_id,))
        conn.commit()
    finally:
        conn.close()

    stale_attempt = seed.apply_candidates(
        [
            seed.new_workstream_candidate("Activation imports"),
            _candidate(
                source,
                title="Third activation child",
                claim="This child must not attach through a stale parent.",
                workstream_key=key,
            ),
        ],
        project_path=str(project),
        sources=[source],
        workstream_scope=key,
    )
    assert not stale_attempt.complete
    assert stale_attempt.inserted_ids == []
    assert {failure["error_code"] for failure in stale_attempt.failures} >= {
        "candidate_invalid", "workstream_attach_failed",
    }

    ambiguous_project = tmp_path / "ambiguous-workstream"
    ambiguous_project.mkdir()
    first_parent = seed.new_workstream_candidate("First parent")
    ambiguous_key = first_parent.workstream_key
    assert ambiguous_key is not None
    second_parent = seed.new_workstream_candidate("Second parent")
    second_parent.workstream_key = ambiguous_key
    ambiguous_child = _candidate(
        source,
        title="Ambiguous child",
        claim="This child has two candidate parents.",
        workstream_key=ambiguous_key,
    )
    ambiguous = seed.apply_candidates(
        [first_parent, second_parent, ambiguous_child],
        project_path=str(ambiguous_project),
        workstream_scope=ambiguous_key,
    )
    assert not ambiguous.complete
    assert ambiguous.inserted_ids == []
    conn = db.connect(str(ambiguous_project))
    try:
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
    finally:
        conn.close()


def test_cursor_cache_round_trips_apply_state_and_main_rejects_scope_mismatch(
    tmp_path, monkeypatch, capsys,
):
    project = tmp_path / "cursor-apply-state"
    project.mkdir()
    first = _source("cursor:cache-one", agent="cursor")
    second = _source(
        "cursor:cache-two",
        agent="cursor",
        text="[user] We decided to retain the failed source receipt.",
    )
    candidate = _candidate(first, title="Cache exact apply state")
    failure_codes = {
        seed.source_revision_token(second): "extractor_failed",
    }
    digest = seed.write_cursor_seed_preview(
        project_path=str(project),
        session_id="cursor-cache-session",
        sources=[first, second],
        candidates=[candidate],
        llm_estimate=2,
        apply_sources=[first, second],
        source_failure_codes=failure_codes,
        workstream_scope="project",
        llm_stats={"attempted": 2, "failed": 1},
        discovery_stats={"selected": 2},
        llm_refinement_empty=False,
    )
    loaded = seed.load_cursor_seed_preview(
        project_path=str(project),
        session_id="cursor-cache-session",
        preview_digest=digest,
        include_apply_state=True,
    )
    (
        loaded_sources,
        loaded_candidates,
        estimate,
        apply_sources,
        loaded_failures,
        scope,
        llm_stats,
        discovery_stats,
        refinement_empty,
    ) = loaded
    assert [item.id for item in loaded_sources] == [
        seed.seed_source_identity(first.id),
        seed.seed_source_identity(second.id),
    ]
    assert all(item.text == "" for item in loaded_sources)
    assert len(loaded_candidates) == 1
    assert seed.candidate_import_key(
        loaded_candidates[0], project_path=str(project),
    ) == seed.candidate_import_key(candidate, project_path=str(project))
    assert [item.content_digest for item in apply_sources] == [
        first.content_digest, second.content_digest,
    ]
    assert all(item.text == "" for item in apply_sources)
    assert estimate == 2
    assert loaded_failures == failure_codes
    assert scope == "project"
    assert llm_stats == {"attempted": 2, "failed": 1}
    assert discovery_stats == {"selected": 2}
    assert refinement_empty is False

    def forbidden(*_args, **_kwargs):
        raise AssertionError("scope mismatch must stop before discovery/model/apply")

    monkeypatch.setattr(seed, "discover_sources", forbidden)
    monkeypatch.setattr(seed, "apply_candidates", forbidden)
    monkeypatch.setattr(model_backends, "invoke_prompt", forbidden)
    rc = seed.main([
        "--project", str(project),
        "--source", "cursor",
        "--lookback-days", "5",
        "--last-sessions", "2",
        "--max-llm-calls", "2",
        "--new-workstream", "A different reviewed scope",
        "--cursor-session-id", "cursor-cache-session",
        "--preview-digest", digest,
        "--apply",
        "--yes",
    ])
    captured = capsys.readouterr()
    assert rc == 2
    assert "scope does not match" in captured.err


def test_force_reimport_allows_touched_mtime_but_failed_force_returns_nonzero(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "force-reimport"
    project.mkdir()
    original = _source(
        "claude:force",
        mtime="2026-07-15T00:00:00+00:00",
    )
    candidate = _candidate(original, title="Keep force import retryable")
    first = seed.apply_candidates(
        [candidate], project_path=str(project), sources=[original],
    )
    assert first.complete
    touched = seed.SeedSource(
        id=original.id,
        agent=original.agent,
        path=original.path,
        mtime="2026-07-16T23:59:59+00:00",
        text=original.text,
        content_digest=original.content_digest,
        value_score=original.value_score,
        redaction_count=original.redaction_count,
    )
    repeated = seed.apply_candidates(
        [candidate], project_path=str(project), sources=[touched],
    )
    assert repeated.complete
    assert repeated.inserted_ids == []
    assert repeated.skipped_node_ids == first.inserted_ids

    monkeypatch.setattr(seed, "discover_sources", lambda **_kwargs: [touched])
    monkeypatch.setattr(
        budget,
        "check_and_record",
        lambda *_args, **_kwargs: (True, {"count_nonheal": 1}),
    )
    calls = 0

    def failed_model(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return model_backends.ModelCallResult(
            text=None,
            error="simulated unavailable backend",
            timed_out=False,
            backend="codex",
        )

    monkeypatch.setattr(model_backends, "invoke_prompt", failed_model)
    rc = seed.main([
        "--project", str(project),
        "--source", "claude",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--max-llm-calls", "1",
        "--force-reimport",
        "--apply",
        "--yes",
    ])
    captured = capsys.readouterr()
    assert calls == 1
    assert rc == 1
    assert "did not complete successfully" in captured.err


def test_source_batch_finalization_rolls_back_every_outcome_on_one_failure(tmp_path):
    project = tmp_path / "atomic-source-finalization"
    project.mkdir()
    sources = [
        _source("claude:atomic-one"),
        _source(
            "claude:atomic-two",
            text="[user] We decided to keep the second atomic outcome.",
        ),
    ]
    conn = db.connect(str(project))
    try:
        keys: list[str] = []
        for source in sources:
            key = seed.seed_source_import_key(
                source, project_path=str(project), workstream_scope="project",
            )
            keys.append(key)
            db.begin_seed_source_import(
                conn,
                import_key=key,
                source_id=source.id,
                source_agent=source.agent,
                source_path=source.path,
                source_mtime=source.mtime,
                source_digest=source.content_digest,
                project_path=str(project.resolve()),
                extractor_name="latch_seed",
                extractor_version=seed.SEED_EXTRACTOR_VERSION,
            )
        conn.execute(f"""
            CREATE TRIGGER fail_second_source_finalization
            BEFORE UPDATE OF state ON seed_source_import
            WHEN NEW.import_key = '{keys[1]}'
            BEGIN
                SELECT RAISE(ABORT, 'simulated source finalization failure');
            END
        """)
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            db.finish_seed_source_imports(conn, {
                keys[0]: ("applied", None),
                keys[1]: ("failed", "extractor_failed"),
            })
        assert [
            db.get_seed_source_import(conn, key)["state"] for key in keys
        ] == ["pending", "pending"]
    finally:
        conn.close()


def test_gate_relevance_uses_seed_observed_at_not_recent_import_time(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "observed-at-gate"
    project.mkdir()
    old_source = _source(
        "claude:old-evidence",
        mtime="2020-01-02T03:04:05+00:00",
    )
    candidate = _candidate(old_source, title="Old imported decision")
    applied = seed.apply_candidates(
        [candidate], project_path=str(project), sources=[old_source],
    )
    assert applied.complete
    conn = db.connect(str(project))
    try:
        evidence = gate._evidence_node(
            conn,
            applied.inserted_ids[0],
            via_relation="related_to",
            direction="out",
            hop=1,
            path=[applied.inserted_ids[0]],
            body_excerpt_chars=240,
        )
    finally:
        conn.close()
    assert evidence is not None
    assert evidence["seed_observed_at"] == old_source.mtime
    newer_regular = {
        **evidence,
        "id": evidence["id"] + 1,
        "title": "Actually newer evidence",
        "updated_at": "2025-01-01T00:00:00+00:00",
        "seed_observed_at": None,
    }
    ranked = gate._evidence_sort_for_relevance([evidence, newer_regular])
    assert [item["title"] for item in ranked] == [
        "Actually newer evidence", "Old imported decision",
    ]
    rendered = gate._render_chain_for_prompt({
        "seeds": [{
            "id": 999,
            "kind": "decision",
            "status": "staging",
            "source": "search",
            "title": "Seed",
            "body_excerpt": "",
        }],
        "chains": [{"seed_id": 999, "evidence": [evidence]}],
    })
    assert f"imported evidence observed_at: {old_source.mtime}" in rendered


def test_cross_batch_exact_claim_unions_provenance_across_extractor_upgrades(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "cross-batch-claim"
    project.mkdir()
    first_source = _source(
        "claude:claim-first",
        mtime="2026-06-01T10:00:00+00:00",
    )
    second_source = _source(
        "codex:claim-second",
        agent="codex",
        mtime="2026-07-01T10:00:00+00:00",
        text="[user] We decided to keep local SQLite state for installation.",
    )
    first_candidate = _candidate(first_source)
    second_candidate = _candidate(second_source)
    first_claim_key = seed.candidate_claim_key(
        first_candidate, project_path=str(project),
    )

    first = seed.apply_candidates(
        [first_candidate], project_path=str(project), sources=[first_source],
    )
    monkeypatch.setattr(seed, "SEED_EXTRACTOR_VERSION", "seed-vNext")
    assert seed.candidate_claim_key(
        second_candidate, project_path=str(project),
    ) == first_claim_key
    second = seed.apply_candidates(
        [second_candidate], project_path=str(project), sources=[second_source],
    )

    assert first.complete and len(first.inserted_ids) == 1
    assert second.complete and second.inserted_ids == []
    assert second.skipped_import_keys == []
    assert second.corroborated_node_ids == first.inserted_ids
    assert len(second.corroborated_import_keys) == 1
    conn = db.connect(str(project))
    try:
        node = db.get_node(conn, first.inserted_ids[0])
        assert node is not None
        assert "Additional seed corroboration" in node["body"]
        assert first_source.id in node["body"]
        assert second_source.id in node["body"]
        assert conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind = 'decision'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM seed_import WHERE state = 'applied'"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM seed_source_import WHERE state = 'applied'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_provenance_merge_keeps_distinct_revisions_of_the_same_source_id():
    first_source = _source(
        "claude:revised-session",
        text="[user] We decided to use local SQLite state.",
    )
    revised_source = _source(
        "claude:revised-session",
        text="[user] We decided to use local SQLite state after review.",
    )
    merged = seed.dedupe_candidates([
        _candidate(first_source),
        _candidate(revised_source),
    ])

    assert len(merged) == 1
    refs = seed.candidate_source_refs(merged[0])
    assert {(ref["id"], ref["digest"]) for ref in refs} == {
        (first_source.id, first_source.content_digest),
        (revised_source.id, revised_source.content_digest),
    }


def test_inverse_directional_decisions_never_semantically_merge():
    source = _source("claude:inverse-direction")
    sqlite_choice = _candidate(
        source,
        title="Use SQLite, not Postgres",
        claim="Use SQLite, not Postgres, for local decision storage.",
    )
    postgres_choice = _candidate(
        source,
        title="Use Postgres, not SQLite",
        claim="Use Postgres, not SQLite, for local decision storage.",
    )

    assert not seed.safe_candidates_equivalent(sqlite_choice, postgres_choice)
    assert len(seed.dedupe_candidates([sqlite_choice, postgres_choice])) == 2


def test_injected_excerpt_delimiter_cannot_hide_opposing_claims():
    source = _source("claude:injected-excerpt")
    sqlite_choice = _candidate(
        source,
        title="Database choice",
        claim="Use SQLite, not Postgres.\n\nExcerpt:\n> shared injected tail",
    )
    postgres_choice = _candidate(
        source,
        title="Database choice",
        claim="Use Postgres, not SQLite.\n\nExcerpt:\n> shared injected tail",
    )

    assert not seed.safe_candidates_equivalent(sqlite_choice, postgres_choice)
    assert len(seed.dedupe_candidates([sqlite_choice, postgres_choice])) == 2


def test_seed_nodes_require_explicit_promotion_even_after_many_references(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "seed-authority"
    project.mkdir()
    source = _source("claude:authority")
    applied = seed.apply_candidates(
        [_candidate(source)], project_path=str(project), sources=[source],
    )
    assert applied.complete

    conn = db.connect(str(project))
    try:
        seed_id = applied.inserted_ids[0]
        orphan_marker_id = db.insert_node(
            conn,
            kind="fact",
            title="Interrupted seed write",
            body="Latch-Seed-Import-Key: interrupted-before-ledger-checkpoint",
            status="staging",
        )
        regular_id = db.insert_node(
            conn,
            kind="fact",
            title="Ordinary staging evidence",
            body="Eligible ordinary evidence.",
            status="staging",
        )
        pending_id = db.insert_node(
            conn,
            kind="fact",
            title="Pending seed checkpoint",
            body="Checkpointed before source finalization.",
            status="staging",
        )
        failed_id = db.insert_node(
            conn,
            kind="fact",
            title="Failed seed checkpoint",
            body="Checkpointed before a retryable failure.",
            status="staging",
        )
        for import_key, node_id in (
            ("pending-authority-import", pending_id),
            ("failed-authority-import", failed_id),
        ):
            db.begin_seed_import(
                conn,
                import_key=import_key,
                claim_key=f"claim-{import_key}",
                project_path=str(project.resolve()),
                extractor_name="latch_seed",
                extractor_version=seed.SEED_EXTRACTOR_VERSION,
            )
            db.set_seed_import_node(conn, import_key, node_id)
        db.finish_seed_import(
            conn,
            "failed-authority-import",
            state="failed",
            node_id=failed_id,
            error_code="node_write_failed",
        )
        conn.execute(
            "UPDATE nodes SET ref_count = 99 WHERE id IN (?, ?, ?, ?, ?)",
            (seed_id, orphan_marker_id, regular_id, pending_id, failed_id),
        )
        conn.commit()

        assert db.promote_by_ref_count(conn, min_ref_count=3) == [regular_id]
        assert db.get_node(conn, seed_id)["status"] == "staging"
        assert db.get_node(conn, orphan_marker_id)["status"] == "staging"
        assert db.get_node(conn, pending_id)["status"] == "staging"
        assert db.get_node(conn, failed_id)["status"] == "staging"
        db.insert_ratification_nc(
            conn,
            seed_id,
            ratifier="test:explicit-seed-review",
            action="ratify",
            source="latch_update",
        )
        db.update_node(conn, seed_id, status="canonical")
        assert db.get_node(conn, seed_id)["status"] == "canonical"
    finally:
        conn.close()


@pytest.mark.parametrize("mutation", ["edited", "stale"])
def test_cross_batch_claim_reuse_fails_closed_after_user_change(
    tmp_path, monkeypatch, mutation,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / f"changed-claim-{mutation}"
    project.mkdir()
    first_source = _source(f"claude:first-{mutation}")
    first = seed.apply_candidates(
        [_candidate(first_source)],
        project_path=str(project),
        sources=[first_source],
    )
    conn = db.connect(str(project))
    try:
        if mutation == "edited":
            db.update_node(
                conn, first.inserted_ids[0], title="User-reframed seeded claim",
            )
        else:
            db.update_node(conn, first.inserted_ids[0], status="stale")
    finally:
        conn.close()

    second_source = _source(f"codex:second-{mutation}", agent="codex")
    second = seed.apply_candidates(
        [_candidate(second_source)],
        project_path=str(project),
        sources=[second_source],
    )

    assert not second.complete
    assert second.inserted_ids == []
    assert second.failures[0]["error_code"] == "candidate_invalid"
    conn = db.connect(str(project))
    try:
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 1
    finally:
        conn.close()


def test_cross_batch_claim_reuse_fails_closed_after_workstream_move(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "moved-seed-claim"
    project.mkdir()
    conn = db.connect(str(project))
    try:
        first_parent = db.insert_node(
            conn, kind="workstream", title="First lane", body="Reviewed.",
        )
        second_parent = db.insert_node(
            conn, kind="workstream", title="Second lane", body="Reviewed.",
        )
    finally:
        conn.close()
    first_source = _source("claude:moved-first")
    first = seed.apply_candidates(
        [_candidate(first_source)],
        project_path=str(project),
        existing_workstream_id=first_parent,
        sources=[first_source],
        workstream_scope=f"existing:{first_parent}",
    )
    conn = db.connect(str(project))
    try:
        conn.execute(
            "UPDATE nodes SET workstream_id = ? WHERE id = ?",
            (second_parent, first.inserted_ids[0]),
        )
        conn.commit()
    finally:
        conn.close()

    second_source = _source("codex:moved-second", agent="codex")
    second = seed.apply_candidates(
        [_candidate(second_source)],
        project_path=str(project),
        existing_workstream_id=first_parent,
        sources=[second_source],
        workstream_scope=f"existing:{first_parent}",
    )
    assert not second.complete
    assert second.inserted_ids == []
    assert second.failures[0]["error_code"] == "candidate_invalid"


def test_new_batch_reuses_semantically_intact_failed_claim_checkpoint(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "failed-claim-checkpoint"
    project.mkdir()
    failed_source = _source("claude:failed-checkpoint")
    failed_candidate = _candidate(failed_source)
    source_key = seed.seed_source_import_key(
        failed_source, project_path=str(project), workstream_scope="project",
    )
    import_key = seed.candidate_import_key(
        failed_candidate, project_path=str(project),
    )
    claim_key = seed.candidate_claim_key(
        failed_candidate, project_path=str(project),
    )
    conn = db.connect(str(project))
    try:
        db.begin_seed_source_import(
            conn,
            import_key=source_key,
            source_id=failed_source.id,
            source_agent=failed_source.agent,
            source_path=failed_source.path,
            source_mtime=failed_source.mtime,
            source_digest=failed_source.content_digest,
            project_path=str(project.resolve()),
            extractor_name="latch_seed",
            extractor_version=seed.SEED_EXTRACTOR_VERSION,
        )
        db.begin_seed_import(
            conn,
            import_key=import_key,
            claim_key=claim_key,
            source_import_keys=[source_key],
            source_ids=[failed_source.id],
            project_path=str(project.resolve()),
            extractor_name="latch_seed",
            extractor_version=seed.SEED_EXTRACTOR_VERSION,
            observed_at=failed_source.mtime,
        )
        checkpoint_id = db.insert_node(
            conn,
            kind=failed_candidate.kind,
            title=failed_candidate.title,
            body=seed.body_with_import_receipt(
                failed_candidate,
                import_key=import_key,
                project_path=str(project),
                workstream_id=None,
            ),
            status="staging",
        )
        db.set_seed_import_node(conn, import_key, checkpoint_id)
        db.finish_seed_import(
            conn,
            import_key,
            state="failed",
            node_id=checkpoint_id,
            error_code="node_write_failed",
        )
        db.finish_seed_source_import(
            conn, source_key, state="failed", error_code="node_write_failed",
        )
    finally:
        conn.close()

    later_source = _source("codex:later-checkpoint", agent="codex")
    later = seed.apply_candidates(
        [_candidate(later_source)],
        project_path=str(project),
        sources=[later_source],
    )
    assert later.complete and later.inserted_ids == []
    assert later.corroborated_node_ids == [checkpoint_id]
    conn = db.connect(str(project))
    try:
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 1
    finally:
        conn.close()


def test_pending_workstream_checkpoint_is_reused_by_stable_key(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "pending-workstream-checkpoint"
    project.mkdir()
    original_parent = seed.new_workstream_candidate("Interrupted activation")
    key = original_parent.workstream_key
    assert key is not None
    original_import = seed.candidate_import_key(
        original_parent, project_path=str(project),
    )
    conn = db.connect(str(project))
    try:
        db.begin_seed_import(
            conn,
            import_key=original_import,
            claim_key=seed.candidate_claim_key(
                original_parent, project_path=str(project),
            ),
            project_path=str(project.resolve()),
            extractor_name="latch_seed",
            extractor_version=seed.SEED_EXTRACTOR_VERSION,
            source_ids=[
                seed.seed_source_identity(source_id)
                for source_id in original_parent.source_ids
            ],
            workstream_key=key,
        )
        parent_id = db.insert_node(
            conn,
            kind="workstream",
            title=original_parent.title,
            body=seed.body_with_import_receipt(
                original_parent,
                import_key=original_import,
                project_path=str(project),
                workstream_id=None,
            ),
            status="staging",
        )
        db.set_seed_import_node(conn, original_import, parent_id)
    finally:
        conn.close()

    later_parent = seed.new_workstream_candidate("Interrupted activation")
    child_source = _source("claude:pending-parent-child")
    child = _candidate(
        child_source,
        title="Continue interrupted activation",
        workstream_key=key,
    )
    applied = seed.apply_candidates(
        [later_parent, child],
        project_path=str(project),
        sources=[child_source],
        workstream_scope=key,
    )
    assert applied.complete
    assert applied.workstream_attachments[key] == parent_id
    conn = db.connect(str(project))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind = 'workstream'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE workstream_id = ?", (parent_id,)
        ).fetchone()[0] == 1
        applied_claims = conn.execute(
            """
            SELECT si.claim_key, si.project_path, si.workstream_key,
                   si.workstream_id, n.kind, n.title, n.body, n.workstream_id
            FROM seed_import si JOIN nodes n ON n.id = si.node_id
            WHERE si.state = 'applied'
            """
        ).fetchall()
        for row in applied_claims:
            snapshot = seed.SeedCandidate(
                kind=row["kind"],
                title=row["title"],
                body=row["body"],
                confidence=0.0,
                signals=[],
                source_ids=[],
                source_paths=[],
                source_mtimes=[],
                source_digests=[],
                llm_used=False,
                workstream_key=row["workstream_key"],
            )
            target = None if row["kind"] == "workstream" else row["workstream_id"]
            assert seed.candidate_claim_key(
                snapshot,
                project_path=row["project_path"],
                target_workstream_id=target,
                persisted_body=True,
            ) == row["claim_key"]
    finally:
        conn.close()

    conflicting_parent = seed.new_workstream_candidate(
        "Interrupted activation redefined"
    )
    conflicting_parent.workstream_key = key
    conflicting_source = _source("codex:conflicting-parent", agent="codex")
    conflict = seed.apply_candidates(
        [
            conflicting_parent,
            _candidate(
                conflicting_source,
                title="Child of conflicting parent",
                workstream_key=key,
            ),
        ],
        project_path=str(project),
        sources=[conflicting_source],
        workstream_scope=key,
    )
    assert not conflict.complete
    assert conflict.inserted_ids == []
    assert {item["error_code"] for item in conflict.failures} == {
        "candidate_invalid", "workstream_attach_failed",
    }


def test_source_ids_are_redacted_on_model_report_and_durable_surfaces(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    secret = "sk-proj-" + "Z" * 32
    source = _source(f"claude:{secret}")
    candidate = _candidate(source)
    project = tmp_path / "source-id-privacy"
    project.mkdir()

    assert secret not in seed.seed_prompt(
        project_path=str(project), source=source,
    )
    assert secret not in "\n".join(seed.source_review_lines([source]))
    public = seed.public_candidate_dict(candidate)
    assert "source_paths" not in public
    assert secret not in json.dumps(public)
    public_stats = seed.public_seed_stats({
        "source_ids": [source.id],
        "accepted_candidates_by_source": {source.id: 1},
    })
    assert secret not in json.dumps(public_stats)
    assert "accepted_candidates_by_source" not in public_stats
    monkeypatch.setattr(
        budget,
        "check_and_record",
        lambda *_args, **_kwargs: (True, {"count_nonheal": 1}),
    )
    monkeypatch.setattr(
        model_backends,
        "invoke_prompt",
        lambda *_args, **_kwargs: model_backends.ModelCallResult(
            text=None,
            error="simulated failure",
            timed_out=False,
            backend="codex",
        ),
    )
    assert seed.llm_candidates(
        [source],
        project_path=str(project),
        max_calls=1,
        max_candidates=1,
        backend="codex",
    ) == []
    assert secret not in capsys.readouterr().err
    applied = seed.apply_candidates(
        [candidate], project_path=str(project), sources=[source],
    )
    conn = db.connect(str(project))
    try:
        node = db.get_node(conn, applied.inserted_ids[0])
        assert node is not None and secret not in node["body"]
        # Durable identity is opaque and stable across direct and cached apply.
        ledger_id = conn.execute(
            "SELECT source_id FROM seed_source_import"
        ).fetchone()[0]
        assert ledger_id == seed.seed_source_identity(source.id)
        assert secret not in ledger_id
    finally:
        conn.close()


def test_untrusted_fields_cannot_redact_the_trusted_import_marker(tmp_path):
    source = _source("claude:-----END PRIVATE KEY-----")
    candidate = _candidate(
        source,
        claim="-----BEGIN PRIVATE KEY-----\nmalformed historical fragment",
    )
    import_key = seed.candidate_import_key(
        candidate, project_path=str(tmp_path),
    )
    body = seed.body_with_import_receipt(
        candidate,
        import_key=import_key,
        project_path=str(tmp_path),
        workstream_id=None,
    )

    assert f"Latch-Seed-Import-Key: {import_key}" in body
    assert "BEGIN PRIVATE KEY" not in body
    assert "END PRIVATE KEY" not in body
    assert "malformed historical fragment" not in body


def test_inline_corroboration_is_bounded_while_ledgers_remain_complete(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "bounded-corroboration"
    project.mkdir()
    bodies: list[str] = []
    node_id = None
    total = seed.MAX_INLINE_CORROBORATIONS + 4
    for index in range(total):
        source = _source(
            f"claude:corroboration-{index}",
            mtime=f"2026-07-{index + 1:02d}T12:00:00+00:00",
            text=f"[user] Evidence batch {index} confirms local SQLite state.",
        )
        result = seed.apply_candidates(
            [_candidate(source)], project_path=str(project), sources=[source],
        )
        if node_id is None:
            node_id = result.inserted_ids[0]
        conn = db.connect(str(project))
        try:
            bodies.append(db.get_node(conn, node_id)["body"])
        finally:
            conn.close()

    assert bodies[-1] == bodies[-2]
    assert bodies[-1].count("Additional seed corroboration:") == (
        seed.MAX_INLINE_CORROBORATIONS
    )
    assert "inline provenance cap reached" in bodies[-1]
    conn = db.connect(str(project))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM seed_import WHERE state = 'applied'"
        ).fetchone()[0] == total
    finally:
        conn.close()


def test_cursor_preview_is_bound_to_extractor_version(tmp_path, monkeypatch):
    project = tmp_path / "cursor-extractor-version"
    project.mkdir()
    source = _source("cursor:versioned", agent="cursor")
    digest = seed.write_cursor_seed_preview(
        project_path=str(project),
        session_id="cursor-versioned",
        sources=[source],
        candidates=[_candidate(source)],
        llm_estimate=1,
    )
    monkeypatch.setattr(seed, "SEED_EXTRACTOR_VERSION", "seed-vNext")
    with pytest.raises(seed.CursorSeedPreviewError, match="extractor changed"):
        seed.load_cursor_seed_preview(
            project_path=str(project),
            session_id="cursor-versioned",
            preview_digest=digest,
            include_apply_state=True,
        )


def test_gate_recency_parses_mixed_formats_and_timezone_offsets():
    ordinary = {
        "id": 1,
        "updated_at": "2026-07-16 23:00:00",
        "seed_observed_at": None,
    }
    early_seed = {
        "id": 2,
        "updated_at": "2026-07-17 23:59:59",
        "seed_observed_at": "2026-07-16T01:00:00+00:00",
    }
    offset_seed = {
        "id": 3,
        "updated_at": "2020-01-01 00:00:00",
        "seed_observed_at": "2026-07-16T23:30:00-07:00",
    }

    assert [item["id"] for item in gate._evidence_sort_for_relevance(
        [early_seed, ordinary]
    )] == [1, 2]
    assert [item["id"] for item in gate._evidence_sort_for_relevance(
        [ordinary, offset_seed]
    )] == [3, 1]


def test_gate_relevance_prefers_canonical_over_newer_staging_at_equal_hop():
    canonical = {
        "id": 1,
        "status": "canonical",
        "hop": 1,
        "via_relation": "related_to",
        "updated_at": "2025-01-01 00:00:00",
    }
    staging = {
        "id": 2,
        "status": "staging",
        "hop": 1,
        "via_relation": "related_to",
        "seed_observed_at": "2026-07-16T23:59:59+00:00",
    }
    assert [item["id"] for item in gate._evidence_sort_for_relevance(
        [staging, canonical]
    )] == [1, 2]


def test_legacy_null_claim_is_backfilled_before_cross_source_corroboration(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "legacy-null-claim"
    project.mkdir()
    first_source = _source("claude:legacy-claim")
    first = seed.apply_candidates(
        [_candidate(first_source)],
        project_path=str(project),
        sources=[first_source],
    )
    conn = db.connect(str(project))
    try:
        conn.execute(
            "UPDATE seed_import SET claim_key = NULL, observed_at = NULL"
        )
        conn.commit()
    finally:
        conn.close()

    second_source = _source("codex:legacy-corroboration", agent="codex")
    second = seed.apply_candidates(
        [_candidate(second_source)],
        project_path=str(project),
        sources=[second_source],
    )
    assert second.complete and second.inserted_ids == []
    assert second.corroborated_node_ids == first.inserted_ids
    conn = db.connect(str(project))
    try:
        rows = conn.execute(
            "SELECT claim_key, observed_at FROM seed_import ORDER BY created_at"
        ).fetchall()
        assert len(rows) == 2
        assert all(row["claim_key"] for row in rows)
        assert all(row["observed_at"] for row in rows)
    finally:
        conn.close()


def test_discovery_scans_past_two_hundred_unrelated_but_stops_at_hard_bound(
    tmp_path,
):
    project = tmp_path / "target-repository"
    project.mkdir()
    claude_home = tmp_path / "bounded-claude-home"
    transcript_dir = claude_home / "projects" / "history"
    transcript_dir.mkdir(parents=True)
    base = datetime.now(timezone.utc).timestamp()
    unrelated_record = json.dumps({
        "type": "user",
        "message": {"content": "cwd=/unrelated/repository transient chat"},
    }) + "\n"
    for index in range(seed.MAX_SOURCE_SCAN + 1):
        transcript = transcript_dir / f"unrelated-{index:04d}.jsonl"
        transcript.write_text(unrelated_record, encoding="utf-8")
        os.utime(transcript, (base - index, base - index))

    relevant = transcript_dir / "relevant-after-two-hundred.jsonl"
    relevant.write_text(
        json.dumps({
            "type": "user",
            "message": {
                "content": (
                    f"cwd={project.resolve()} We decided to keep bounded "
                    "history acquisition."
                ),
            },
        }) + "\n",
        encoding="utf-8",
    )
    os.utime(relevant, (base - 200.5, base - 200.5))
    stats: dict[str, object] = {}
    sources = seed.discover_sources(
        source="claude",
        project_path=str(project),
        lookback_days=5,
        max_sessions=10,
        claude_home=str(claude_home),
        codex_home=str(tmp_path / "codex-home"),
        all_projects=False,
        stats=stats,
        now=datetime.fromtimestamp(base, tz=timezone.utc),
    )

    assert len(sources) == 1
    assert "bounded history acquisition" in sources[0].text
    assert stats["inventory_considered"] == seed.MAX_SOURCE_SCAN
    assert stats["project_excluded"] == seed.MAX_SOURCE_SCAN - 1
    assert stats["eligible"] == 1
