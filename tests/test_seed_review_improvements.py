from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import budget  # noqa: E402
import db  # noqa: E402
import model_backends  # noqa: E402
import paths  # noqa: E402
import seed  # noqa: E402


def _source(
    source_id: str,
    *,
    agent: str = "codex",
    mtime: str = "2026-07-20T12:00:00+00:00",
    text: str = "[user] Project direction reporting is read-only.",
    path: str | None = None,
) -> seed.SeedSource:
    return seed.SeedSource(
        id=source_id,
        agent=agent,
        path=path or f"/tmp/{source_id.replace(':', '-')}.jsonl",
        mtime=mtime,
        text=text,
        content_digest=seed.source_content_digest(text),
        subject=seed.source_subject(text),
    )


def _candidate(
    source: seed.SeedSource,
    *,
    title: str,
    claim: str,
    kind: str = "decision",
    signals: list[str] | None = None,
) -> seed.SeedCandidate:
    return seed.SeedCandidate(
        kind=kind,
        title=title,
        body=claim,
        confidence=0.9,
        signals=signals or ["decision", "llm_seed"],
        source_ids=[source.id],
        source_paths=[source.path],
        source_mtimes=[source.mtime],
        source_digests=[source.content_digest],
        source_excerpts=[source.text.removeprefix("[user] ")],
        llm_used=True,
    )


def test_dogfood_paraphrases_cluster_without_grouping_opposing_claims():
    first_source = _source("codex:project-direction")
    second_source = _source(
        "claude:project-direction",
        agent="claude",
        mtime="2026-07-21T12:00:00+00:00",
        text="[user] Project direction reports are read-only and do not modify KB state.",
    )
    first = _candidate(
        first_source,
        title="Project-direction is read-only",
        claim="Project direction reporting is read-only and must not alter KB state.",
    )
    second = _candidate(
        second_source,
        title="Project direction reporting is read-only",
        claim="Project direction reports are read-only and do not modify KB state.",
    )

    clusters = seed.build_review_clusters([first, second])
    assert len(clusters) == 1
    assert len(clusters[0].items) == 2
    assert seed.review_cluster_id([first, second]) == seed.review_cluster_id(
        [second, first]
    )
    assert seed.candidate_review_id(
        seed.review_cluster_representative(clusters[0])
    ) == seed.candidate_review_id(
        seed.review_cluster_representative(
            seed.build_review_clusters([second, first])[0]
        )
    )
    merged = seed.merge_review_cluster(clusters[0])
    assert {
        (ref["id"], ref["digest"]) for ref in seed.candidate_source_refs(merged)
    } == {
        (first_source.id, first_source.content_digest),
        (second_source.id, second_source.content_digest),
    }

    sqlite = _candidate(
        first_source,
        title="Use SQLite, not Postgres",
        claim="Use SQLite, not Postgres, for local decision storage.",
    )
    postgres = _candidate(
        second_source,
        title="Use Postgres, not SQLite",
        claim="Use Postgres, not SQLite, for local decision storage.",
    )
    assert seed.candidates_directionally_conflict(sqlite, postgres)
    assert not seed.review_cluster_compatible(sqlite, postgres)
    assert len(seed.build_review_clusters([sqlite, postgres])) == 2

    scoped = _candidate(
        second_source,
        title=first.title,
        claim=first.body,
    )
    scoped.workstream_key = "another-scope"
    assert not seed.review_cluster_compatible(first, scoped)


@pytest.mark.parametrize(
    ("left_title", "right_title"),
    [
        (
            "Use SQLite for local decision storage",
            "Use Postgres for local decision storage",
        ),
        (
            "Enable remote synchronization for project decision storage",
            "Disable remote synchronization for project decision storage",
        ),
        (
            "Expose project reports through a public endpoint",
            "Expose project reports through a private endpoint",
        ),
        (
            "Require manual approval for project deployments",
            "Require automatic approval for project deployments",
        ),
        (
            "Keep project reports read-only for operators",
            "Keep project reports read-write for operators",
        ),
        (
            "Synchronization is required for project decisions",
            "Synchronization is optional for project decisions",
        ),
        (
            "Retain project audit logs for review",
            "Delete project audit logs after review",
        ),
    ],
)
def test_positive_opposing_alternatives_never_dedupe_or_cluster(
    left_title, right_title,
):
    source = _source("codex:opposing-alternatives")
    left = _candidate(source, title=left_title, claim=left_title)
    right = _candidate(source, title=right_title, claim=right_title)

    assert seed.candidates_directionally_conflict(left, right)
    assert not seed.safe_candidates_equivalent(left, right)
    assert len(seed.dedupe_candidates([left, right])) == 2
    assert not seed.review_cluster_compatible(left, right)
    assert len(seed.build_review_clusters([left, right])) == 2


@pytest.mark.parametrize(
    ("left_title", "right_title"),
    [
        (
            "Require encrypted transport for agent traffic",
            "Require plaintext transport for agent traffic",
        ),
        (
            "Keep configuration immutable after startup",
            "Keep configuration mutable after startup",
        ),
        (
            "Keep audit logs enabled in production",
            "Keep audit logs off in production",
        ),
        (
            "Keep project reports local only",
            "Keep project reports remotely synchronized",
        ),
    ],
)
def test_unmatched_substantive_deltas_fail_closed_for_every_merge_path(
    left_title, right_title,
):
    source = _source("codex:substantive-delta")
    left = _candidate(source, title=left_title, claim=left_title)
    right = _candidate(source, title=right_title, claim=right_title)
    assert seed.candidate_semantic_anchor_terms(left) != (
        seed.candidate_semantic_anchor_terms(right)
    )
    assert not seed.safe_candidates_equivalent(left, right)
    assert len(seed.dedupe_candidates([left, right])) == 2
    assert not seed.review_cluster_compatible(left, right)
    assert len(seed.build_review_clusters([left, right])) == 2


def test_choice_conflict_guard_keeps_paraphrases_and_unrelated_choices_distinct():
    first_source = _source("codex:sqlite-choice")
    second_source = _source("claude:sqlite-choice", agent="claude")
    sqlite = _candidate(
        first_source,
        title="Use SQLite for local decision storage",
        claim="Use SQLite for local decision storage.",
    )
    sqlite_paraphrase = _candidate(
        second_source,
        title="Adopt SQLite as local decision storage",
        claim="Adopt SQLite as the local decision storage.",
    )
    assert not seed.candidates_directionally_conflict(sqlite, sqlite_paraphrase)
    assert seed.safe_candidates_equivalent(sqlite, sqlite_paraphrase)
    assert seed.review_cluster_compatible(sqlite, sqlite_paraphrase)

    unrelated = _candidate(
        second_source,
        title="Use React for the operator interface",
        claim="Use React for the operator interface.",
    )
    assert not seed.candidates_directionally_conflict(sqlite, unrelated)
    assert not seed.safe_candidates_equivalent(sqlite, unrelated)
    assert not seed.review_cluster_compatible(sqlite, unrelated)


@pytest.mark.parametrize(
    ("first_title", "second_title"),
    [
        (
            "Expose reports through a private endpoint",
            "Keep the reports endpoint private",
        ),
        (
            "Require manual deployment approval",
            "Keep deployment approval manual",
        ),
        (
            "Project reports are read-only",
            "Keep project reports read only",
        ),
    ],
)
def test_lexical_conflict_guard_preserves_same_direction_paraphrases(
    first_title, second_title,
):
    first_source = _source("codex:same-direction")
    second_source = _source("claude:same-direction", agent="claude")
    first = _candidate(first_source, title=first_title, claim=first_title)
    second = _candidate(second_source, title=second_title, claim=second_title)
    assert not seed.candidates_directionally_conflict(first, second)
    assert seed.review_cluster_compatible(first, second)


@pytest.mark.parametrize(
    ("left_title", "right_title"),
    [
        (
            "Keep audit logs on in production",
            "Keep audit logs off in production",
        ),
        (
            "Use API v1 for deployment",
            "Use API v2 for deployment",
        ),
        (
            "Retain audit logs for 30 days",
            "Retain audit logs for 60 days",
        ),
        (
            "Require 3 approvals for release",
            "Require 5 approvals for release",
        ),
        (
            "Require TLS 1.2 for agent transport",
            "Require TLS 1.3 for agent transport",
        ),
        (
            "Run validation before deployment",
            "Run validation after deployment",
        ),
        (
            "Run schema migration before service restart",
            "Run service restart before schema migration",
        ),
        (
            "Indexer depends on database",
            "Database depends on indexer",
        ),
        (
            "Use SQLite for metadata and Postgres for analytics",
            "Use Postgres for metadata and SQLite for analytics",
        ),
    ],
)
def test_short_numeric_version_and_order_anchors_block_every_merge_path(
    left_title, right_title,
):
    source = _source("codex:semantic-anchor")
    left = _candidate(source, title=left_title, claim=left_title)
    right = _candidate(source, title=right_title, claim=right_title)

    assert seed.candidate_semantic_anchor_terms(left) != (
        seed.candidate_semantic_anchor_terms(right)
    )
    assert not seed.safe_candidates_equivalent(left, right)
    assert len(seed.dedupe_candidates([left, right])) == 2
    assert not seed.review_cluster_compatible(left, right)
    assert len(seed.build_review_clusters([left, right])) == 2


def test_dependency_relation_anchor_preserves_directional_paraphrase():
    first_source = _source("codex:dependency-paraphrase")
    second_source = _source(
        "claude:dependency-paraphrase", agent="claude",
    )
    depends_on = _candidate(
        first_source,
        title="Indexer depends on database",
        claim="Indexer depends on database.",
    )
    prerequisite = _candidate(
        second_source,
        title="Database is a prerequisite for indexer",
        claim="Database is a prerequisite for indexer.",
    )

    assert seed.candidate_semantic_anchor_terms(depends_on) == (
        seed.candidate_semantic_anchor_terms(prerequisite)
    )
    assert seed.safe_candidates_equivalent(depends_on, prerequisite)
    assert seed.review_cluster_compatible(depends_on, prerequisite)


def test_rich_review_report_exposes_body_evidence_time_and_scoped_ids():
    first_source = _source("codex:project-direction")
    second_source = _source(
        "claude:project-direction",
        agent="claude",
        mtime="2026-07-21T12:00:00+00:00",
        text="[user] Project direction reports are read-only and do not modify KB state.",
    )
    candidates = [
        _candidate(
            first_source,
            title="Project-direction is read-only",
            claim="Project direction reporting is read-only and must not alter KB state.",
        ),
        _candidate(
            second_source,
            title="Project direction reporting is read-only",
            claim="Project direction reports are read-only and do not modify KB state.",
        ),
    ]
    args = seed.parse_args([
        "--lookback-days", "14",
        "--source", "both",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--project", os.getcwd(),
    ])
    text = seed.render_text(
        args=args,
        sources=[first_source, second_source],
        candidates=candidates,
        llm_estimate=0,
    )
    cluster = seed.build_review_clusters(candidates)[0]
    assert "Possible duplicate cluster" in text
    assert cluster.cluster_id in text
    assert "candidate body:" in text and "rationale:" in text
    assert "oldest=" in text and "latest=" in text and "age_days=" in text
    assert first_source.id in text and second_source.id in text
    assert first_source.text.removeprefix("[user] ") in text
    assert "--approve-candidate ID" in text and "--approve-cluster ID" in text

    payload = json.loads(seed.render_json(
        args=args,
        sources=[first_source, second_source],
        candidates=candidates,
        llm_estimate=0,
    ))
    assert payload["review_clusters"][0]["possible_duplicates"] is True
    assert payload["review_clusters"][0]["cluster_id"] == cluster.cluster_id
    assert not any(
        key == "confidence"
        for item in payload["review_clusters"]
        for candidate in item["items"]
        for key in candidate
    )
    assert all(item["review_id"].startswith("cand-") for item in payload["candidates"])
    assert all(item["observation"]["latest_observed_at"] for item in payload["candidates"])


def test_scoped_approval_can_choose_candidate_or_corroborated_cluster():
    first_source = _source("codex:project-direction")
    second_source = _source(
        "claude:project-direction",
        agent="claude",
        text="[user] Project direction reports are read-only and do not modify KB state.",
    )
    first = _candidate(
        first_source,
        title="Project-direction is read-only",
        claim="Project direction reporting is read-only and must not alter KB state.",
    )
    second = _candidate(
        second_source,
        title="Project direction reporting is read-only",
        claim="Project direction reports are read-only and do not modify KB state.",
    )
    candidates = [first, second]
    cluster = seed.build_review_clusters(candidates)[0]

    selected, covers_all = seed.resolve_approval_selection(
        candidates,
        cluster_ids=[cluster.cluster_id],
    )
    assert covers_all is True
    assert len(selected) == 1
    assert len(seed.candidate_revision_keys(selected[0])) == 2

    selected, covers_all = seed.resolve_approval_selection(
        candidates,
        candidate_ids=[seed.candidate_review_id(first)],
    )
    assert covers_all is False
    assert selected == [first]

    with pytest.raises(seed.SeedApprovalError, match="unknown candidate IDs"):
        seed.resolve_approval_selection(
            candidates,
            candidate_ids=["cand-does-not-exist"],
        )

    parent = _candidate(
        first_source,
        kind="workstream",
        title="Direction reporting",
        claim="Continue the direction-reporting workstream.",
        signals=["ongoing_workstream", "llm_seed"],
    )
    parent.workstream_key = "direction-reporting"
    child = _candidate(
        first_source,
        title="Keep direction reporting read-only",
        claim="Keep direction reporting read-only.",
    )
    child.workstream_key = parent.workstream_key
    with pytest.raises(seed.SeedApprovalError, match="require their reviewable workstream"):
        seed.resolve_approval_selection(
            [parent, child],
            candidate_ids=[seed.candidate_review_id(child)],
        )


def test_partial_approval_keeps_source_revision_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    project = tmp_path / "partial-review"
    project.mkdir()
    source = _source("codex:partial-review")
    first = _candidate(
        source,
        title="Keep review reports read-only",
        claim="Keep review reports read-only during preview.",
    )
    second = _candidate(
        source,
        title="Show evidence timestamps",
        claim="Show source evidence timestamps before approval.",
    )

    partial = seed.apply_candidates(
        [first],
        project_path=str(project),
        sources=[source],
        finalize_sources=False,
    )
    assert partial.complete
    assert len(partial.pending_source_import_keys) == 1
    pending, applied = seed.split_applied_sources(
        [source],
        project_path=str(project),
        workstream_scope="project",
    )
    assert pending == [source] and applied == []

    completed = seed.apply_candidates(
        [first, second],
        project_path=str(project),
        sources=[source],
        finalize_sources=True,
    )
    assert completed.complete
    pending, applied = seed.split_applied_sources(
        [source],
        project_path=str(project),
        workstream_scope="project",
    )
    assert pending == [] and applied == [source]
    conn = db.connect(str(project))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind = 'decision'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_redacted_subjects_and_grounded_excerpts_are_review_safe():
    secret = "sk-proj-" + "S" * 32
    text = (
        "[user] <environment_context>injected setup</environment_context>\n"
        f"[user] We decided to keep {secret} out of every seed review.\n"
        "[assistant] Understood."
    )
    subject = seed.source_subject(text)
    assert secret not in subject
    assert "<redacted:openai-key>" in subject
    source = _source("codex:subject", text=text)
    lines = "\n".join(seed.source_review_lines([source]))
    assert secret not in lines and "subject=" in lines

    candidate = seed.candidate_from_llm_item({
        "kind": "decision",
        "title": "Keep secrets out of seed reviews",
        "body": "Seed review output must not expose credentials.",
        "confidence": 0.9,
        "signals": ["decision"],
        "source_excerpt": "hallucinated source sentence",
    }, source)
    assert candidate is not None
    assert candidate.source_excerpts
    assert "hallucinated" not in candidate.source_excerpts[0]
    assert secret not in candidate.source_excerpts[0]


def test_source_subject_keeps_questions_and_prefers_redacted_codex_thread_name():
    assert seed.source_subject(
        "[user] Can you try to improve the initial seed review?"
    ) == "Can you try to improve the initial seed review?"
    assert seed.source_subject(
        "[session_meta] id=abc cwd=/tmp thread_name=Seed review hardening\n"
        "[user] A less useful fallback request"
    ) == "Seed review hardening"
    secret = "sk-proj-" + "T" * 32
    subject = seed.source_subject(
        f"[session_meta] id=abc thread_name=Keep {secret} private"
    )
    assert secret not in subject
    assert "<redacted:openai-key>" in subject
    wrapped = seed.source_subject(
        "[user] # Files mentioned by the user:\n"
        "/tmp/review.txt\n"
        "## My request for Codex:\n"
        "Work on the seeding improvements from the dogfood findings."
    )
    assert wrapped == "Work on the seeding improvements from the dogfood findings."


def test_grounded_excerpt_never_treats_assistant_text_as_user_evidence():
    source = _source(
        "codex:user-evidence-only",
        text=(
            "[assistant] We decided to use Postgres for local decision storage.\n"
            "[user] No, use SQLite for local decision storage."
        ),
    )
    candidate = seed.candidate_from_llm_item({
        "kind": "decision",
        "title": "Use SQLite for local decision storage",
        "body": "Use SQLite for local decision storage.",
        "confidence": 0.9,
        "signals": ["decision"],
        "source_excerpt": "We decided to use Postgres for local decision storage.",
    }, source)
    assert candidate is not None
    assert candidate.source_excerpts == [
        "No, use SQLite for local decision storage."
    ]


def test_grounded_excerpt_prefers_latest_compatible_user_correction():
    source = _source(
        "codex:corrected-user-direction",
        text=(
            "[user] We decided to use Postgres for local decision storage.\n"
            "[assistant] I will implement the Postgres direction.\n"
            "[user] Correction: use SQLite for local decision storage instead."
        ),
    )
    candidate = seed.candidate_from_llm_item({
        "kind": "decision",
        "title": "Use SQLite for local decision storage",
        "body": "Use SQLite for local decision storage.",
        "confidence": 0.9,
        "signals": ["decision"],
        "source_excerpt": "We decided to use Postgres for local decision storage.",
    }, source)

    assert candidate is not None
    assert candidate.source_excerpts == [
        "Correction: use SQLite for local decision storage instead."
    ]


def test_grounded_excerpt_rejects_stale_claim_after_newer_user_correction():
    source = _source(
        "codex:stale-user-direction",
        text=(
            "[user] We decided to use Postgres for local decision storage.\n"
            "[user] Correction: use SQLite for local decision storage instead."
        ),
    )

    assert seed.grounded_source_excerpt(
        "We decided to use Postgres for local decision storage.",
        source=source,
        title="Use Postgres for local decision storage",
        body="Use Postgres for local decision storage.",
    ) == ""


def test_grounded_excerpt_uses_latest_equal_score_user_line():
    source = _source(
        "codex:latest-equal-score",
        text=(
            "[user] Keep seed reports read-only for operators.\n"
            "[user] Keep seed reports read-only for reviewers."
        ),
    )

    assert seed.grounded_source_excerpt(
        "Keep seed reports read-only for operators.",
        source=source,
        title="Keep seed reports read-only",
        body="Keep seed reports read-only.",
    ) == "Keep seed reports read-only for reviewers."


def test_progress_is_source_agnostic_and_budget_errors_are_sanitized(
    tmp_path, monkeypatch, capsys,
):
    sources = [
        _source("codex:private-one", text="[user] Keep first private subject."),
        _source("claude:private-two", agent="claude", text="[user] Keep second private subject."),
    ]
    json_args = seed.parse_args([
        "--lookback-days", "14",
        "--source", "both",
        "--format", "json",
        "--yes",
    ])
    assert seed.confirm_source_use(json_args, sources) is True
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Selected local source receipts" in captured.err

    monkeypatch.setattr(
        budget,
        "check_and_record",
        lambda *_args, **_kwargs: (True, {"count_nonheal": 1}),
    )
    monkeypatch.setattr(
        model_backends,
        "invoke_prompt",
        lambda *_args, **_kwargs: model_backends.ModelCallResult(
            text=json.dumps({"seed_candidates": []}),
            error=None,
            timed_out=False,
            backend="codex",
        ),
    )
    seed.llm_candidates(
        sources,
        project_path=str(tmp_path),
        max_calls=2,
        max_candidates=5,
        backend="codex",
    )
    stderr = capsys.readouterr().err
    assert "1/2 started" in stderr and "2/2 completed" in stderr
    assert all(source.id not in stderr and source.subject not in stderr for source in sources)

    monkeypatch.setattr(
        model_backends,
        "invoke_prompt",
        lambda *_args, **_kwargs: model_backends.ModelCallResult(
            text="not-json",
            error=None,
            timed_out=False,
            backend="codex",
        ),
    )
    seed.llm_candidates(
        sources[:1],
        project_path=str(tmp_path),
        max_calls=1,
        max_candidates=5,
        backend="codex",
    )
    stderr = capsys.readouterr().err
    assert "failed safely" in stderr
    assert sources[0].id not in stderr

    private_path = "/private/sensitive/latch/budget.json"
    monkeypatch.setattr(
        budget,
        "check_and_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError(private_path)
        ),
    )
    invoked = False

    def forbidden_model(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("model must not run when budget state is unavailable")

    monkeypatch.setattr(model_backends, "invoke_prompt", forbidden_model)
    stats: dict[str, object] = {}
    assert seed.llm_candidates(
        sources[:1],
        project_path=str(tmp_path),
        max_calls=1,
        max_candidates=5,
        backend="codex",
        stats=stats,
    ) == []
    stderr = capsys.readouterr().err
    assert stats["budget_storage_unavailable"] is True
    assert "budget_storage_unavailable" in stderr
    assert private_path not in stderr
    assert invoked is False

    monkeypatch.setattr(
        budget,
        "prepare_storage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError(private_path)
        ),
    )
    assert seed.prepare_llm_budget_storage(str(tmp_path)) is False
    stderr = capsys.readouterr().err
    assert "before source consent" in stderr
    assert private_path not in stderr


def test_json_apply_keeps_stdout_machine_parseable(tmp_path, monkeypatch, capsys):
    source = _source(
        "codex:json-apply",
        text="[user] We decided to keep JSON apply output machine-readable.",
    )
    monkeypatch.setattr(seed, "discover_sources", lambda **_kwargs: [source])
    monkeypatch.setattr(
        seed,
        "apply_candidates",
        lambda *_args, **_kwargs: seed.SeedApplyResult(inserted_ids=[101]),
    )
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--format", "json",
        "--apply",
        "--yes",
    ])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload["ok"] is True and payload["apply"] is True
    assert payload["write_performed"] is True
    assert payload["apply_receipt"]["inserted_ids"] == [101]
    assert payload["apply_receipt"]["complete"] is True
    assert "Wrote 1 staging seed candidate" in captured.err


def test_json_interactive_setup_prompts_and_validation_use_stderr(
    monkeypatch, capsys,
):
    class TTYInput:
        @staticmethod
        def isatty():
            return True

    replies = iter(["7", "14", "invalid", "codex", "0", "2"])
    monkeypatch.setattr(seed.sys, "stdin", TTYInput())
    monkeypatch.setattr("builtins.input", lambda: next(replies))
    monkeypatch.setattr(seed, "default_source_choice", lambda _args: None)
    args = seed.parse_args(["--format", "json"])

    seed.prompt_choices(args)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Retention horizon" in captured.err
    assert "Transcript source" in captured.err
    assert "Maximum sessions" in captured.err
    assert "Please enter one of" in captured.err
    assert "Please enter a positive whole number" in captured.err
    assert args.lookback_days == 14
    assert args.source == "codex"
    assert args.max_sessions == 2


def test_main_fails_closed_when_budget_storage_breaks_after_preflight(
    tmp_path, monkeypatch, capsys,
):
    source = _source("codex:budget-race")
    monkeypatch.setattr(seed, "discover_sources", lambda **_kwargs: [source])
    monkeypatch.setattr(seed, "prepare_llm_budget_storage", lambda *_args: True)
    monkeypatch.setattr(
        budget,
        "check_and_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("/private/budget.json")
        ),
    )
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--max-llm-calls", "1",
        "--format", "json",
        "--yes",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out) == {
        "ok": False,
        "error": "budget_storage_unavailable",
    }


def test_json_source_consent_cancellation_is_structured(
    tmp_path, monkeypatch, capsys,
):
    source = _source("codex:consent-cancel")
    monkeypatch.setattr(seed, "discover_sources", lambda **_kwargs: [source])
    monkeypatch.setattr(seed, "prepare_llm_budget_storage", lambda *_args: True)
    monkeypatch.setattr(
        model_backends,
        "invoke_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cancelled consent must stop before model use")
        ),
    )
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--max-llm-calls", "1",
        "--format", "json",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out) == {
        "ok": False,
        "error": "source_consent_cancelled",
    }
    assert "cancelled before any LLM calls" in captured.err


def test_json_source_consent_tty_eof_is_structured(
    tmp_path, monkeypatch, capsys,
):
    source = _source("codex:consent-eof")

    class TTYInput:
        @staticmethod
        def isatty():
            return True

    monkeypatch.setattr(seed.sys, "stdin", TTYInput())
    monkeypatch.setattr(
        "builtins.input",
        lambda: (_ for _ in ()).throw(EOFError()),
    )
    monkeypatch.setattr(seed, "discover_sources", lambda **_kwargs: [source])
    monkeypatch.setattr(seed, "prepare_llm_budget_storage", lambda *_args: True)
    monkeypatch.setattr(
        model_backends,
        "invoke_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("consent EOF must stop before model use")
        ),
    )
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--max-llm-calls", "1",
        "--format", "json",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out) == {
        "ok": False,
        "error": "source_consent_cancelled",
    }
    assert captured.out.count('"ok"') == 1


def test_json_budget_confirmation_cancellation_is_structured(
    tmp_path, monkeypatch, capsys,
):
    sources = [
        _source(f"codex:budget-confirm-{index}") for index in range(2)
    ]
    monkeypatch.setattr(seed, "discover_sources", lambda **_kwargs: sources)
    monkeypatch.setattr(seed, "prepare_llm_budget_storage", lambda *_args: True)
    monkeypatch.setattr(seed, "confirm_source_use", lambda *_args: True)
    monkeypatch.setattr(
        model_backends,
        "invoke_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cancelled budget prompt must stop before model use")
        ),
    )
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", str(len(sources)),
        "--max-llm-calls", str(len(sources)),
        "--llm-warning-threshold", "1",
        "--format", "json",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out) == {
        "ok": False,
        "error": "llm_budget_cancelled",
    }
    assert "Seed pass cancelled before any LLM calls" in captured.err


def test_scoped_apply_uses_exact_digest_bound_preview(
    tmp_path, monkeypatch, capsys,
):
    source = _source(
        "codex:digest-bound",
        text="[user] We decided to bind scoped seed approval to its exact preview.",
    )
    monkeypatch.setattr(seed, "discover_sources", lambda **_kwargs: [source])
    preview_rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--format", "json",
    ])
    preview_output = json.loads(capsys.readouterr().out)
    digest = preview_output["preview_digest"]
    candidate_id = preview_output["candidates"][0]["review_id"]
    assert preview_rc == 0
    assert len(digest) == 64

    def forbidden_discovery(**_kwargs):
        raise AssertionError("digest-bound apply must not rediscover or rerun extraction")

    applied: list[seed.SeedCandidate] = []
    monkeypatch.setattr(seed, "discover_sources", forbidden_discovery)
    monkeypatch.setattr(
        seed,
        "apply_candidates",
        lambda candidates, **_kwargs: (
            applied.extend(candidates)
            or seed.SeedApplyResult(inserted_ids=[202])
        ),
    )
    apply_rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--format", "json",
        "--preview-digest", digest,
        "--approve-candidate", candidate_id,
        "--apply",
    ])
    apply_output = json.loads(capsys.readouterr().out)
    assert apply_rc == 0
    assert apply_output["preview_digest"] == digest
    assert [seed.candidate_review_id(item) for item in applied] == [candidate_id]
    assert not seed._seed_preview_path(str(tmp_path), digest).exists()


def test_scoped_apply_finalizes_review_and_dismisses_unselected_items(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    source = _source("codex:final-scoped-review")
    approved = _candidate(
        source,
        title="Keep scoped seed review",
        claim="Keep per-candidate approval in the seed review.",
    )
    dismissed = _candidate(
        source,
        title="Add unrelated seed automation",
        claim="Add an unrelated automation after the seed review.",
    )
    digest = seed.write_seed_preview(
        project_path=str(tmp_path),
        source_choice="codex",
        sources=[source],
        candidates=[approved, dismissed],
        llm_estimate=0,
        apply_sources=[source],
        source_failure_codes={},
        workstream_scope="project",
        llm_stats={},
        discovery_stats={},
        llm_refinement_empty=False,
    )
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--format", "json",
        "--preview-digest", digest,
        "--approve-candidate", seed.candidate_review_id(approved),
        "--apply",
    ])
    json.loads(capsys.readouterr().out)
    assert rc == 0
    pending, applied_sources = seed.split_applied_sources(
        [source],
        project_path=str(tmp_path),
        workstream_scope="project",
    )
    assert pending == [] and applied_sources == [source]
    conn = db.connect(str(tmp_path))
    try:
        titles = {
            row[0] for row in conn.execute(
                "SELECT title FROM nodes WHERE kind = 'decision'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert approved.title in titles
    assert dismissed.title not in titles
    assert not seed._seed_preview_path(str(tmp_path), digest).exists()


def test_digest_bound_preview_expires_before_apply(tmp_path, monkeypatch):
    source = _source("codex:stale-preview")
    candidate = _candidate(
        source,
        title="Keep seed previews fresh",
        claim="Apply reviewed seed previews only while they remain current.",
    )
    created_at = datetime(2026, 7, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(seed, "utc_now", lambda: created_at)
    digest = seed.write_seed_preview(
        project_path=str(tmp_path),
        source_choice="codex",
        sources=[source],
        candidates=[candidate],
        llm_estimate=1,
        apply_sources=[source],
        source_failure_codes={},
        workstream_scope="project",
        llm_stats={},
        discovery_stats={},
        llm_refinement_empty=False,
    )
    with pytest.raises(seed.SeedPreviewError, match="expired"):
        seed.load_seed_preview(
            project_path=str(tmp_path),
            source_choice="codex",
            preview_digest=digest,
            now=created_at + seed.timedelta(
                hours=seed.SEED_PREVIEW_MAX_AGE_HOURS + 1
            ),
        )
    assert not seed._seed_preview_path(str(tmp_path), digest).exists()


def test_preview_cache_is_bounded_and_avoids_duplicate_candidate_paths(
    tmp_path, monkeypatch,
):
    private_source_path = "/Users/private/transcripts/codex-bounded-cache.jsonl"
    secret = "sk-proj-" + "C" * 32
    source = _source(
        "codex:bounded-cache",
        path=private_source_path,
        text=f"[user] Keep {secret} out of bounded seed caches.",
    )
    candidate = _candidate(
        source,
        title="Bound review cache retention",
        claim="Keep only a bounded set of short-lived seed review caches.",
    )
    created_at = datetime(2026, 7, 20, tzinfo=timezone.utc)
    tick = 0

    def advancing_now():
        nonlocal tick
        current = created_at + seed.timedelta(minutes=tick)
        tick += 1
        return current

    monkeypatch.setattr(seed, "utc_now", advancing_now)
    for _index in range(seed.SEED_PREVIEW_CACHE_MAX_FILES + 3):
        digest = seed.write_seed_preview(
            project_path=str(tmp_path),
            source_choice="codex",
            sources=[source],
            candidates=[candidate],
            llm_estimate=1,
            apply_sources=[source],
            source_failure_codes={},
            workstream_scope="project",
            llm_stats={},
            discovery_stats={},
            llm_refinement_empty=False,
        )
    cache_paths = list(
        seed.paths.project_dir(str(tmp_path)).glob("seed_preview.*.json")
    )
    assert len(cache_paths) == seed.SEED_PREVIEW_CACHE_MAX_FILES
    newest = json.loads(
        seed._seed_preview_path(str(tmp_path), digest).read_text(encoding="utf-8")
    )
    assert "project" not in newest and newest["project_fingerprint"]
    raw_cache = json.dumps(newest)
    assert private_source_path not in raw_cache
    assert str(tmp_path) not in raw_cache
    assert secret not in raw_cache
    for key in ("sources", "apply_sources"):
        assert newest[key][0]["path"].startswith("seed-source:")
        assert not Path(newest[key][0]["path"]).is_absolute()
    assert newest["candidates"][0]["source_paths"] == [
        newest["sources"][0]["path"]
    ]


def test_cursor_preview_cache_is_bounded_expiring_and_path_minimal(
    tmp_path, monkeypatch,
):
    private_source_path = "/Users/private/transcripts/cursor-session.jsonl"
    secret = "sk-proj-" + "D" * 32
    source = _source(
        "cursor:bounded-cache",
        agent="cursor",
        path=private_source_path,
        text=f"[user] Keep {secret} out of Cursor seed caches.",
    )
    candidate = _candidate(
        source,
        title="Bound Cursor review cache retention",
        claim="Keep Cursor seed review caches short-lived and path-minimal.",
    )
    created_at = datetime(2026, 7, 20, tzinfo=timezone.utc)
    tick = 0

    def advancing_now():
        nonlocal tick
        current = created_at + seed.timedelta(minutes=tick)
        tick += 1
        return current

    monkeypatch.setattr(seed, "utc_now", advancing_now)
    newest_session = ""
    newest_digest = ""
    for index in range(seed.SEED_PREVIEW_CACHE_MAX_FILES + 3):
        newest_session = f"cursor-cache-{index}"
        newest_digest = seed.write_cursor_seed_preview(
            project_path=str(tmp_path),
            session_id=newest_session,
            sources=[source],
            candidates=[candidate],
            llm_estimate=1,
        )

    cache_paths = list(
        seed.paths.project_dir(str(tmp_path)).glob("cursor_seed_preview.*.json")
    )
    assert len(cache_paths) == seed.SEED_PREVIEW_CACHE_MAX_FILES
    newest_path = seed._cursor_seed_preview_path(
        str(tmp_path), newest_session,
    )
    newest = json.loads(newest_path.read_text(encoding="utf-8"))
    raw_cache = json.dumps(newest)
    assert private_source_path not in raw_cache
    assert str(tmp_path) not in raw_cache
    assert secret not in raw_cache
    assert newest["sources"][0]["path"].startswith("seed-source:")
    assert newest["candidates"][0]["source_paths"] == [
        newest["sources"][0]["path"]
    ]
    loaded_sources, loaded_candidates, estimate = seed.load_cursor_seed_preview(
        project_path=str(tmp_path),
        session_id=newest_session,
        preview_digest=newest_digest,
    )
    assert estimate == 1
    assert loaded_sources[0].path == newest["sources"][0]["path"]
    assert loaded_candidates[0].source_paths == [
        newest["sources"][0]["path"]
    ]

    expired_at = created_at + seed.timedelta(
        hours=seed.SEED_PREVIEW_MAX_AGE_HOURS + 2
    )
    with pytest.raises(seed.CursorSeedPreviewError, match="expired"):
        seed.load_cursor_seed_preview(
            project_path=str(tmp_path),
            session_id=newest_session,
            preview_digest=newest_digest,
            now=expired_at,
        )
    assert not newest_path.exists()

    seed.prune_cursor_seed_preview_cache(
        str(tmp_path),
        now=expired_at,
    )
    assert not list(
        seed.paths.project_dir(str(tmp_path)).glob(
            "cursor_seed_preview.*.json"
        )
    )


def test_opaque_source_identity_is_stable_across_preview_cache(
    tmp_path,
):
    secret = "sk-proj-" + "I" * 32
    source = _source(
        f"cursor:{secret}",
        agent="cursor",
        path="/Users/private/transcripts/secret-session.jsonl",
        text="[user] Keep source identity stable without caching its locator.",
    )
    candidate = _candidate(
        source,
        title="Keep opaque source identity stable",
        claim="Use one opaque identity before and after exact-preview caching.",
    )
    failure_codes = {
        seed.source_revision_token(source): "extractor_failed",
    }
    digest = seed.write_seed_preview(
        project_path=str(tmp_path),
        source_choice="codex",
        sources=[source],
        candidates=[candidate],
        llm_estimate=1,
        apply_sources=[source],
        source_failure_codes=failure_codes,
        workstream_scope="project",
        llm_stats={},
        discovery_stats={},
        llm_refinement_empty=False,
    )
    cache_path = seed._seed_preview_path(str(tmp_path), digest)
    raw_cache = cache_path.read_text(encoding="utf-8")
    assert secret not in raw_cache
    assert source.id not in raw_cache
    assert source.path not in raw_cache

    (
        loaded_sources,
        loaded_candidates,
        _estimate,
        loaded_apply_sources,
        loaded_failures,
        _scope,
        _llm_stats,
        _discovery_stats,
        _refinement_empty,
    ) = seed.load_seed_preview(
        project_path=str(tmp_path),
        source_choice="codex",
        preview_digest=digest,
    )
    loaded_source = loaded_sources[0]
    loaded_candidate = loaded_candidates[0]
    assert loaded_source.id == seed.seed_source_identity(source.id)
    assert loaded_apply_sources[0].id == loaded_source.id
    assert seed.seed_source_identity(loaded_source.id) == loaded_source.id
    assert seed.source_revision_token(loaded_source) == seed.source_revision_token(source)
    assert seed.seed_source_import_key(
        loaded_source,
        project_path=str(tmp_path),
        workstream_scope="project",
    ) == seed.seed_source_import_key(
        source,
        project_path=str(tmp_path),
        workstream_scope="project",
    )
    assert seed.candidate_import_key(
        loaded_candidate, project_path=str(tmp_path),
    ) == seed.candidate_import_key(candidate, project_path=str(tmp_path))
    assert seed.candidate_source_fingerprint(
        loaded_candidate
    ) == seed.candidate_source_fingerprint(candidate)
    assert seed.candidate_revision_keys(
        loaded_candidate
    ) == seed.candidate_revision_keys(candidate)
    assert loaded_failures == failure_codes


def test_existing_seed_ledger_read_failure_stops_before_consent_or_model(
    tmp_path, monkeypatch, capsys,
):
    source = _source("codex:ledger-read-failure")
    db_file = tmp_path / "kb.db"
    db_file.write_bytes(b"existing")
    private_error = "/private/sensitive/latch/kb.db"
    monkeypatch.setattr(paths, "db_path", lambda *_args, **_kwargs: db_file)
    monkeypatch.setattr(seed, "discover_sources", lambda **_kwargs: [source])
    monkeypatch.setattr(
        seed.sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            seed.sqlite3.OperationalError(private_error)
        ),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "ledger failure must stop before consent, budget, model, or apply"
        )

    monkeypatch.setattr(seed, "confirm_source_use", forbidden)
    monkeypatch.setattr(seed, "confirm_llm_budget", forbidden)
    monkeypatch.setattr(seed, "prepare_llm_budget_storage", forbidden)
    monkeypatch.setattr(model_backends, "invoke_prompt", forbidden)
    monkeypatch.setattr(seed, "apply_candidates", forbidden)
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--format", "json",
        "--yes",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out) == {
        "ok": False,
        "error": "seed_source_ledger_unavailable",
    }
    assert "could not be read safely" in captured.err
    assert private_error not in captured.err


def test_cursor_preview_cache_write_failure_is_sanitized_and_fail_closed(
    tmp_path, monkeypatch, capsys,
):
    source = _source(
        "cursor:cache-write-failure",
        agent="cursor",
        text="[user] We decided Cursor preview receipts must fail closed.",
    )
    private_error = "/private/sensitive/latch/cursor-preview.json"
    monkeypatch.setattr(seed, "discover_sources", lambda **_kwargs: [source])
    monkeypatch.setattr(
        seed,
        "write_cursor_seed_preview",
        lambda **_kwargs: (_ for _ in ()).throw(PermissionError(private_error)),
    )
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "cursor",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--cursor-session-id", "exact-session",
        "--format", "json",
        "--yes",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out) == {
        "ok": False,
        "error": "seed_preview_cache_unavailable",
    }
    assert "exact-review cache could not be written safely" in captured.err
    assert private_error not in captured.err


def test_cursor_main_preview_writes_exact_session_cache(
    tmp_path, monkeypatch, capsys,
):
    source = _source(
        "cursor:exact-main-preview",
        agent="cursor",
        text="[user] We decided exact Cursor preview must create its bound receipt.",
    )
    monkeypatch.setattr(seed, "discover_sources", lambda **_kwargs: [source])
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "cursor",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--cursor-session-id", "exact-main-session",
        "--format", "json",
        "--yes",
    ])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["apply"] is False
    assert re.fullmatch(r"[0-9a-f]{64}", payload["preview_digest"])
    assert seed._cursor_seed_preview_path(
        str(tmp_path), "exact-main-session",
    ).is_file()


def test_json_apply_cancellation_emits_one_false_final_document(
    tmp_path, monkeypatch, capsys,
):
    source = _source(
        "codex:approval-cancel",
        text="[user] We decided seed apply needs explicit final approval.",
    )
    monkeypatch.setattr(seed, "discover_sources", lambda **_kwargs: [source])
    monkeypatch.setattr(
        seed,
        "prompt_approval_selection",
        lambda *_args, **_kwargs: ([], False),
    )
    monkeypatch.setattr(
        seed,
        "apply_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cancelled approval must not write")
        ),
    )
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--format", "json",
        "--apply",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out) == {
        "ok": False,
        "error": "seed_approval_cancelled",
    }
    assert captured.out.count('"ok"') == 1
    assert '"candidates"' in captured.err


def test_json_apply_tty_eof_becomes_structured_cancellation(
    tmp_path, monkeypatch, capsys,
):
    source = _source(
        "codex:approval-eof",
        text="[user] We decided an approval EOF must cancel seed apply safely.",
    )

    class TTYInput:
        @staticmethod
        def isatty():
            return True

    monkeypatch.setattr(seed.sys, "stdin", TTYInput())
    monkeypatch.setattr(
        "builtins.input",
        lambda: (_ for _ in ()).throw(EOFError()),
    )
    monkeypatch.setattr(seed, "discover_sources", lambda **_kwargs: [source])
    monkeypatch.setattr(
        seed,
        "apply_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("EOF cancellation must not write")
        ),
    )
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--format", "json",
        "--apply",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out) == {
        "ok": False,
        "error": "seed_approval_cancelled",
    }
    assert captured.out.count('"ok"') == 1


def test_json_apply_block_emits_one_false_final_document(
    tmp_path, monkeypatch, capsys,
):
    source = _source(
        "codex:write-block",
        text="[user] We decided blocked seed writes must report failure.",
    )
    monkeypatch.setattr(seed, "discover_sources", lambda **_kwargs: [source])
    monkeypatch.setattr(
        seed,
        "apply_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            seed.SeedWriteBlocked(
                "compaction_in_progress",
                "Initial-KB apply is retryable; no batch lock was acquired.",
            )
        ),
    )
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--format", "json",
        "--apply",
        "--yes",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out) == {
        "ok": False,
        "error": "compaction_in_progress",
    }
    assert captured.out.count('"ok"') == 1
    assert "apply blocked (compaction_in_progress)" in captured.err


@pytest.mark.parametrize(
    "approval_args",
    [
        ["--apply", "--approve-candidate", "cand-123456789abc"],
        ["--apply", "--dismiss-all"],
        [
            "--apply",
            "--preview-digest",
            "0" * 64,
            "--dismiss-all",
            "--yes",
        ],
        [
            "--apply",
            "--preview-digest",
            "0" * 64,
            "--yes",
            "--approve-candidate",
            "cand-123456789abc",
        ],
    ],
)
def test_json_invalid_apply_selection_is_structured(
    approval_args, capsys,
):
    rc = seed.main([
        "--format", "json",
        "--llm", "no",
        "--allow-internal-no-llm",
        *approval_args,
    ])
    captured = capsys.readouterr()
    assert rc == 2
    assert json.loads(captured.out) == {
        "ok": False,
        "error": "invalid_seed_arguments",
    }
    assert captured.out.count('"ok"') == 1


def test_digest_bound_dismiss_all_finalizes_sources_without_nodes(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    source = _source(
        "codex:dismiss-all",
        text="[user] We decided every candidate in this seed preview is noise.",
    )
    candidate = _candidate(
        source,
        title="Dismiss this seed candidate",
        claim="This candidate should be explicitly dismissed after review.",
        signals=["decision", "rejected_path", "llm_seed"],
    )
    digest = seed.write_seed_preview(
        project_path=str(tmp_path),
        source_choice="codex",
        sources=[source],
        candidates=[candidate],
        llm_estimate=0,
        apply_sources=[source],
        source_failure_codes={},
        workstream_scope="project",
        llm_stats={},
        discovery_stats={},
        llm_refinement_empty=False,
    )
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--format", "json",
        "--preview-digest", digest,
        "--apply",
        "--dismiss-all",
    ])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["write_performed"] is True
    assert payload["apply_receipt"]["dismissed_all"] is True
    assert payload["apply_receipt"]["approved_candidate_count"] == 0
    assert payload["apply_receipt"]["inserted_count"] == 0
    assert payload["catch_demo"] is None
    assert not seed._seed_preview_path(str(tmp_path), digest).exists()
    pending, applied_sources = seed.split_applied_sources(
        [source],
        project_path=str(tmp_path),
        workstream_scope="project",
    )
    assert pending == [] and applied_sources == [source]
    conn = db.connect(str(tmp_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
    finally:
        conn.close()


def test_digest_bound_dismiss_all_rejects_empty_preview(
    tmp_path, monkeypatch, capsys,
):
    source = _source("codex:empty-dismiss-all")
    digest = seed.write_seed_preview(
        project_path=str(tmp_path),
        source_choice="codex",
        sources=[source],
        candidates=[],
        llm_estimate=0,
        apply_sources=[source],
        source_failure_codes={},
        workstream_scope="project",
        llm_stats={},
        discovery_stats={},
        llm_refinement_empty=False,
    )
    monkeypatch.setattr(
        seed,
        "apply_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("empty reject-all must not finalize source revisions")
        ),
    )
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--format", "json",
        "--preview-digest", digest,
        "--apply",
        "--dismiss-all",
    ])
    captured = capsys.readouterr()
    assert rc == 2
    assert json.loads(captured.out) == {
        "ok": False,
        "error": "seed_approval_selection_unavailable",
    }
    assert "at least one candidate" in captured.err
    assert seed._seed_preview_path(str(tmp_path), digest).is_file()


def test_digest_bound_empty_selector_cannot_finalize_sources(
    tmp_path, monkeypatch, capsys,
):
    source = _source("codex:empty-selector")
    candidate = _candidate(
        source,
        title="Require a nonempty approval selector",
        claim="Do not treat an empty scoped selector as reject-all approval.",
    )
    digest = seed.write_seed_preview(
        project_path=str(tmp_path),
        source_choice="codex",
        sources=[source],
        candidates=[candidate],
        llm_estimate=0,
        apply_sources=[source],
        source_failure_codes={},
        workstream_scope="project",
        llm_stats={},
        discovery_stats={},
        llm_refinement_empty=False,
    )
    monkeypatch.setattr(
        seed,
        "apply_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("empty selector must not finalize source revisions")
        ),
    )
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--format", "json",
        "--preview-digest", digest,
        "--approve-candidate", "",
        "--apply",
    ])
    captured = capsys.readouterr()
    assert rc == 2
    assert json.loads(captured.out) == {
        "ok": False,
        "error": "seed_approval_selection_unavailable",
    }
    assert "non-empty review ID" in captured.err
    assert seed._seed_preview_path(str(tmp_path), digest).is_file()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation may require elevation")
def test_dangling_seed_ledger_link_fails_closed_before_consent_or_model(
    tmp_path, monkeypatch, capsys,
):
    source = _source("codex:dangling-ledger")
    db_file = tmp_path / "kb.db"
    db_file.symlink_to(tmp_path / "missing-kb.db")
    monkeypatch.setattr(paths, "db_path", lambda *_args, **_kwargs: db_file)
    monkeypatch.setattr(seed, "discover_sources", lambda **_kwargs: [source])

    def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "dangling ledger state must stop before consent, budget, model, or apply"
        )

    monkeypatch.setattr(seed, "confirm_source_use", forbidden)
    monkeypatch.setattr(seed, "confirm_llm_budget", forbidden)
    monkeypatch.setattr(seed, "prepare_llm_budget_storage", forbidden)
    monkeypatch.setattr(model_backends, "invoke_prompt", forbidden)
    monkeypatch.setattr(seed, "apply_candidates", forbidden)
    rc = seed.main([
        "--project", str(tmp_path),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--format", "json",
        "--yes",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out) == {
        "ok": False,
        "error": "seed_source_ledger_unavailable",
    }
    assert "could not be read safely" in captured.err


def test_interactive_none_is_an_explicit_final_selection(
    monkeypatch,
):
    source = _source("codex:interactive-none")
    candidate = _candidate(
        source,
        title="Dismiss interactively",
        claim="Allow an explicit none choice after reviewing seed candidates.",
    )

    class TTYInput:
        @staticmethod
        def isatty():
            return True

    monkeypatch.setattr(seed.sys, "stdin", TTYInput())
    monkeypatch.setattr("builtins.input", lambda: "none")
    selected, finalized = seed.prompt_approval_selection([candidate])
    assert selected == []
    assert finalized is True
