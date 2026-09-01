"""A2-F1 acceptance tests for the authority-safe global policy projection.

All policy domains and rejection text in this module are synthetic.  The
projector is deliberately tested through the ordinary additive DB substrate;
it must not depend on search, ranking, ref-count, or gate retrieval.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from latch.store import db  # noqa: E402
from latch.gate import predicate  # noqa: E402
from latch.gate import predicate_consumer  # noqa: E402
from latch.store import schema_version  # noqa: E402
from latch.install import versioning  # noqa: E402
from latch.gate import predicate_snapshot  # noqa: E402

try:  # Regression-first: make collection succeed before F1 exists.
    from latch.gate import predicate_policy  # noqa: E402
except ImportError:
    predicate_policy = None


DOMAIN_ALPHA = "domain:synthetic-alpha"
DOMAIN_BETA = "domain:synthetic-beta"


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(str(tmp_path))
    try:
        yield connection
    finally:
        connection.close()


def _owner(
    conn,
    name: str,
    *,
    kind: str = "decision",
    status: str = "canonical",
    latest_ratification: str | None = None,
) -> int:
    node_id = db.insert_node(
        conn,
        kind=kind,
        title=f"Synthetic owner {name}",
        body="Synthetic authority fixture.",
        status=status,
    )
    if latest_ratification is not None:
        db.insert_ratification_nc(
            conn,
            node_id,
            ratifier="fixture:founder",
            decided_at="2026-08-27 08:00:00",
            action=latest_ratification,
            source="capture_decision",
        )
        conn.commit()
    return node_id


def _rejection(
    conn,
    node_id: int,
    name: str,
    *,
    domain: str | None = DOMAIN_ALPHA,
    source: str = "declared",
    ratifier: str | None = "fixture:founder",
    decided_at: str | None = "2026-08-27 08:00:00",
    scope_predicate: str | None = None,
) -> int:
    row_id = db.insert_rejected_path_nc(
        conn,
        node_id,
        option=f"synthetic option {name}",
        reason=f"synthetic reason {name}",
        ratifier=ratifier,
        decided_at=decided_at,
        scope_predicate=scope_predicate or f"file:synthetic/{name}.txt",
        source=source,
        policy_domain_id=domain,
    )
    assert row_id is not None
    return int(row_id)


def _project(conn, domain: str | None = DOMAIN_ALPHA):
    assert predicate_policy is not None, "A2-F1 predicate_policy module is missing"
    return predicate_policy.project_policy_domain(conn, domain)


def test_global_projection_tail_rule_fires(conn):
    owner = _owner(conn, "tail-bank")
    for index in range(1_005):
        _rejection(
            conn,
            owner,
            f"tail-{index:04d}",
            scope_predicate=f"file:synthetic/tail-{index:04d}.txt",
        )
    conn.commit()

    projection = _project(conn)

    assert len(projection.binding_rows) == 1_005
    assert projection.advisory_rows == ()
    assert projection.binding_rows[-1].scope_predicate == (
        "file:synthetic/tail-1004.txt"
    )
    checks = predicate.compile_predicates(projection.binding_rows)
    verdict = predicate.evaluate(
        checks,
        predicate.ToolCallContext(
            policy_domain_id=DOMAIN_ALPHA,
            project_root="/repo/synthetic-alpha",
            cwd="/repo/synthetic-alpha",
            tool_name="synthetic-edit",
            proposed_file_paths=("synthetic/tail-1004.txt",),
            diff_paths=(),
            staged_paths=(),
            import_names=(),
            api_names=(),
            evidence_complete=True,
            evidence_provenance={"fixture": "test_predicate_policy"},
        ),
    )
    assert verdict["decision"] == "block"
    assert [match["rejected_path_id"] for match in verdict["matches"]] == [
        projection.binding_rows[-1].rejected_path_id
    ]


def test_two_projects_one_vault_never_cross_block(conn):
    alpha_owner = _owner(conn, "alpha")
    beta_owner = _owner(conn, "beta")
    alpha_id = _rejection(
        conn,
        alpha_owner,
        "alpha-only",
        domain=DOMAIN_ALPHA,
        scope_predicate="file:synthetic/alpha-only.txt",
    )
    beta_id = _rejection(
        conn,
        beta_owner,
        "beta-only",
        domain=DOMAIN_BETA,
        scope_predicate="file:synthetic/beta-only.txt",
    )
    unbound_id = _rejection(conn, alpha_owner, "legacy-unbound", domain=None)
    conn.commit()

    alpha = _project(conn, DOMAIN_ALPHA)
    beta = _project(conn, DOMAIN_BETA)

    assert [row.rejected_path_id for row in alpha.binding_rows] == [alpha_id]
    assert [row.rejected_path_id for row in beta.binding_rows] == [beta_id]
    assert [row.rejected_path_id for row in alpha.advisory_rows] == [unbound_id]
    assert [row.rejected_path_id for row in beta.advisory_rows] == [unbound_id]
    assert alpha.advisory_rows[0].reason_codes == ("row_domain_unbound",)
    assert beta.advisory_rows[0].reason_codes == ("row_domain_unbound",)

    # A proposed Beta path cannot be blocked by Alpha's complete projection.
    alpha_verdict = predicate.evaluate(
        predicate.compile_predicates(alpha.binding_rows),
        predicate.ToolCallContext(
            policy_domain_id=DOMAIN_ALPHA,
            project_root="/repo/synthetic-alpha",
            cwd="/repo/synthetic-alpha",
            tool_name="synthetic-edit",
            proposed_file_paths=("synthetic/beta-only.txt",),
            diff_paths=(),
            staged_paths=(),
            import_names=(),
            api_names=(),
            evidence_complete=True,
            evidence_provenance={"fixture": "test_predicate_policy"},
        ),
    )
    assert alpha_verdict["decision"] == "pass"
    assert all(
        row.rejected_path_id != beta_id
        for row in (*alpha.binding_rows, *alpha.advisory_rows)
    )
    with pytest.raises(ValueError, match="policy_domain_id"):
        _project(conn, None)
    for invalid_domain in (
        "",
        "   ",
        " leading-space",
        "trailing-space ",
        "line\nbreak",
        "path/segment",
        "unicode-\u2603",
        "x" * 257,
    ):
        with pytest.raises(ValueError, match="policy_domain_id"):
            _project(conn, invalid_domain)

    for invalid_domain in ("path/segment", "line\nbreak", "x" * 257):
        with pytest.raises(ValueError, match="policy_domain_id"):
            _rejection(conn, alpha_owner, "invalid-domain", domain=invalid_domain)


def test_authority_matrix(conn):
    assert schema_version.read(conn) == versioning.KB_SCHEMA_VERSION
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(rejected_path)")
    }
    assert "policy_domain_id" in columns

    legacy_owner = _owner(conn, "legacy-canonical")
    ratified_owner = _owner(conn, "explicit-ratify", latest_ratification="ratify")
    rejected_owner = _owner(conn, "latest-reject", latest_ratification="reject")
    staging_owner = _owner(conn, "staging", status="staging")
    stale_owner = _owner(conn, "stale", status="stale")
    evidence_owner = _owner(conn, "evidence", kind="fact")
    superseded_owner = _owner(conn, "superseded")
    replaced_owner = _owner(conn, "replaced")
    reconciled_owner = _owner(conn, "reconciled")
    stale_superseder_target = _owner(conn, "stale-superseder-target")
    inactive_superseder_target = _owner(conn, "inactive-superseder-target")

    winner = _owner(conn, "winner")
    replacer = _owner(conn, "replacer")
    reconciler = _owner(conn, "reconciler")
    stale_superseder = _owner(conn, "stale-superseder", status="stale")
    inactive_superseder = _owner(conn, "inactive-superseder")
    db.add_edge(conn, src=winner, dst=superseded_owner, relation="supersedes")
    db.add_edge(conn, src=replacer, dst=replaced_owner, relation="replaces")
    db.add_edge(
        conn, src=reconciled_owner, dst=reconciler, relation="reconciled_by"
    )
    db.add_edge(
        conn,
        src=stale_superseder,
        dst=stale_superseder_target,
        relation="supersedes",
    )
    db.add_edge(
        conn,
        src=inactive_superseder,
        dst=inactive_superseder_target,
        relation="supersedes",
    )
    assert db.tombstone_edge(
        conn,
        src=inactive_superseder,
        dst=inactive_superseder_target,
        relation="supersedes",
    ) == 1

    rows = {
        "legacy": _rejection(conn, legacy_owner, "legacy"),
        "ratified": _rejection(conn, ratified_owner, "ratified"),
        "latest-reject": _rejection(conn, rejected_owner, "latest-reject"),
        "staging": _rejection(conn, staging_owner, "staging"),
        "stale": _rejection(conn, stale_owner, "stale"),
        "non-judgment": _rejection(conn, evidence_owner, "non-judgment"),
        "superseded": _rejection(conn, superseded_owner, "superseded"),
        "replaced": _rejection(conn, replaced_owner, "replaced"),
        "reconciled": _rejection(conn, reconciled_owner, "reconciled"),
        "stale-superseder-ignored": _rejection(
            conn, stale_superseder_target, "stale-superseder-ignored"
        ),
        "inactive-superseder-ignored": _rejection(
            conn, inactive_superseder_target, "inactive-superseder-ignored"
        ),
        "backfill": _rejection(conn, legacy_owner, "backfill", source="backfill"),
        "unbound": _rejection(conn, legacy_owner, "unbound", domain=None),
        "no-ratifier": _rejection(
            conn, legacy_owner, "no-ratifier", ratifier=None
        ),
        "no-date": _rejection(conn, legacy_owner, "no-date", decided_at=None),
    }
    conn.commit()

    before_changes = conn.total_changes
    first = _project(conn)
    second = _project(conn)
    conn.execute("PRAGMA query_only = ON")
    try:
        query_only = _project(conn)
    finally:
        conn.execute("PRAGMA query_only = OFF")

    assert conn.total_changes == before_changes
    assert first.freshness_token == second.freshness_token
    assert query_only.freshness_token == first.freshness_token
    assert [row.rejected_path_id for row in first.binding_rows] == [
        rows["legacy"],
        rows["ratified"],
        rows["stale-superseder-ignored"],
        rows["inactive-superseder-ignored"],
    ]
    by_id = {row.rejected_path_id: row for row in first.advisory_rows}
    assert by_id[rows["latest-reject"]].reason_codes == (
        "owner_latest_ratification_rejected",
    )
    assert by_id[rows["staging"]].reason_codes == ("owner_not_canonical",)
    assert by_id[rows["stale"]].reason_codes == ("owner_not_canonical",)
    assert by_id[rows["non-judgment"]].reason_codes == ("owner_not_judgment",)
    assert by_id[rows["superseded"]].reason_codes == ("owner_superseded",)
    assert by_id[rows["replaced"]].reason_codes == ("owner_superseded",)
    assert by_id[rows["reconciled"]].reason_codes == (
        "owner_unresolved_reconciliation",
    )
    assert by_id[rows["backfill"]].reason_codes == ("row_source_backfill",)
    assert by_id[rows["unbound"]].reason_codes == ("row_domain_unbound",)
    assert by_id[rows["no-ratifier"]].reason_codes == (
        "row_declaration_incomplete",
    )
    assert by_id[rows["no-date"]].reason_codes == (
        "row_declaration_incomplete",
    )
    assert first.binding_rows[0].authority_basis == "legacy_canonical"
    assert first.binding_rows[1].authority_basis == "latest_ratification"
    assert first.binding_rows[1].policy_domain_id == DOMAIN_ALPHA
    assert first.reason_counts == {
        "owner_latest_ratification_rejected": 1,
        "owner_not_canonical": 2,
        "owner_not_judgment": 1,
        "owner_superseded": 2,
        "owner_unresolved_reconciliation": 1,
        "row_declaration_incomplete": 2,
        "row_domain_unbound": 1,
        "row_source_backfill": 1,
    }

    with pytest.raises(ValueError, match="policy_domain_id"):
        _rejection(conn, legacy_owner, "blank-domain", domain="   ")


def _complete_context(**overrides):
    values = {
        "policy_domain_id": DOMAIN_ALPHA,
        "project_root": "/repo/synthetic-alpha",
        "cwd": "/repo/synthetic-alpha",
        "tool_name": "synthetic-edit",
        "proposed_file_paths": ("synthetic/unrelated.txt",),
        "diff_paths": (),
        "staged_paths": (),
        "import_names": (),
        "api_names": (),
        "evidence_complete": True,
        "evidence_provenance": {"fixture": "test_predicate_policy"},
    }
    values.update(overrides)
    return predicate.ToolCallContext(**values)


def test_complete_action_block_flag_and_compiled_pass():
    check = predicate.compile_predicate(
        {
            "id": 8101,
            "node_id": 9101,
            "option": "synthetic private option",
            "reason": "synthetic private reason",
            "scope_predicate": "file:synthetic/blocked.txt",
            "source": "declared",
        }
    )

    blocked = predicate.evaluate(
        [check],
        _complete_context(
            proposed_file_paths=("synthetic/blocked.txt",),
        ),
    )
    compiled_pass = predicate.evaluate([check], _complete_context())
    flagged = predicate.evaluate(
        [check],
        _complete_context(evidence_complete=False),
    )

    assert blocked["decision"] == "block"
    assert [match["rejected_path_id"] for match in blocked["matches"]] == [8101]
    assert compiled_pass == {
        "engine": "predicate-v1",
        "decision": "pass",
        "llm_calls": 0,
        "matches": [],
    }
    assert flagged == {
        "engine": "predicate-v1",
        "decision": "flag",
        "llm_calls": 0,
        "matches": [],
    }


def _publish_projection(conn, tmp_path: Path):
    token_path = tmp_path / "synthetic-policy-generation.txt"
    token_path.write_text("generation-a\n", encoding="utf-8")
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    snapshot_path = private_dir / "policy.snapshot.json"
    document = predicate_snapshot.publish_policy_snapshot(
        snapshot_path,
        policy_domain_id=DOMAIN_ALPHA,
        projector=lambda: _project(conn),
        freshness_token_path=token_path,
    )
    return snapshot_path, document


def _action(**overrides):
    values = {
        "policy_domain_id": DOMAIN_ALPHA,
        "project_root": "/repo/synthetic-alpha",
        "cwd": "/repo/synthetic-alpha",
        "tool_name": "synthetic-edit",
        "proposed_file_paths": ["synthetic/unrelated.txt"],
        "diff_paths": [],
        "staged_paths": [],
        "import_names": [],
        "api_names": [],
        "evidence_complete": True,
        "evidence_provenance": ["test-predicate-policy"],
    }
    values.update(overrides)
    return values


def test_residual_is_advisory_not_full_compliance(conn, tmp_path):
    owner = _owner(conn, "residual-policy")
    _rejection(
        conn,
        owner,
        "compiled-nonmatch",
        scope_predicate="file:synthetic/blocked.txt",
    )
    _rejection(
        conn,
        owner,
        "unsupported-residual",
        scope_predicate="feature:synthetic-residual",
    )
    conn.commit()
    snapshot_path, _ = _publish_projection(conn, tmp_path)

    result = predicate_consumer.evaluate_policy(snapshot_path, _action())

    assert result.verdict["decision"] == "pass"
    assert result.receipt["decision"] == "pass"
    assert result.receipt["binding_rows"] == 2
    assert result.receipt["binding_compiled"] == 1
    assert result.receipt["advisory_rows"] == 0
    assert result.receipt["uncompilable_rows"] == 1
    assert result.receipt["advisory_reason_counts"] == {
        "uncompilable_predicate": 1
    }


def test_predicate_v1_golden_contract_matches_runtime():
    check = predicate.compile_predicate(
        {
            "id": 8201,
            "node_id": 9201,
            "option": "synthetic golden option",
            "reason": "synthetic golden reason",
            "scope_predicate": "api:SyntheticClient.create",
            "source": "declared",
        }
    )
    verdict = predicate.evaluate(
        [check],
        _complete_context(
            proposed_file_paths=(),
            api_names=("SyntheticClient.create",),
        ),
    )

    assert set(verdict) == {"engine", "decision", "llm_calls", "matches"}
    assert verdict["engine"] == "predicate-v1"
    assert verdict["decision"] == "block"
    assert verdict["llm_calls"] == 0
    assert verdict["matches"] == [
        {
            "rejected_path_id": 8201,
            "node_id": 9201,
            "option": "synthetic golden option",
            "predicate": "api:SyntheticClient.create",
            "reason": "synthetic golden reason",
            "source": "declared",
        }
    ]


def test_receipt_and_logs_redact_policy_and_action_text(conn, tmp_path):
    owner = _owner(conn, "redaction-policy")
    _rejection(
        conn,
        owner,
        "PRIVATE_OPTION_SENTINEL",
        scope_predicate="file:synthetic/private-target.txt",
    )
    conn.execute(
        "UPDATE rejected_path SET reason = ? WHERE node_id = ?",
        ("PRIVATE_REASON_SENTINEL", owner),
    )
    conn.commit()
    snapshot_path, document = _publish_projection(conn, tmp_path)

    private_action_text = "PRIVATE_ACTION_SENTINEL"
    private_action_path = "synthetic/private-target.txt"
    result = predicate_consumer.evaluate_policy(
        snapshot_path,
        _action(
            proposed_file_paths=[private_action_path],
            command_text=private_action_text,
        ),
    )
    serialized = json.dumps(result.receipt, sort_keys=True)

    assert result.verdict["decision"] == "block"
    assert result.receipt["policy_digest"] == document["digest"]
    for private_text in (
        "PRIVATE_OPTION_SENTINEL",
        "PRIVATE_REASON_SENTINEL",
        "file:synthetic/private-target.txt",
        private_action_text,
        private_action_path,
    ):
        assert private_text not in serialized
