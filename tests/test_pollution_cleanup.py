from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import db  # noqa: E402
import embeddings  # noqa: E402
import measure_write_path  # noqa: E402
import pollution_cleanup  # noqa: E402


def _embedding(text: str) -> bytes:
    value = np.zeros(384, dtype=np.float32)
    value[0] = 1.0
    if "other" in text:
        value[0] = 0.8
        value[1] = 0.6
    return embeddings.to_blob(value)


def _fixture(conn, index: int) -> int:
    return db.insert_node(
        conn,
        kind="fact",
        title=f"seed {index}",
        body=(
            f"seed node {index} about topic {index % 23}; filler so the body is a "
            "plausible length and the vector is not degenerate."
        ),
        status="canonical",
        embedding=_embedding(str(index)),
    )


def _ended_zero_turn_session(conn, session_id: str) -> None:
    db.upsert_session(conn, session_id, "/project", None)
    conn.execute(
        "UPDATE sessions SET turn_count=0, ended_at=? WHERE id=?",
        ("2030-01-02 03:04:05", session_id),
    )
    conn.commit()


def test_exact_pollution_predicates_reject_near_misses(tmp_path: Path):
    conn = db.connect(str(tmp_path))
    try:
        fixture = _fixture(conn, 7)
        wrong_body = db.insert_node(
            conn,
            kind="fact",
            title="seed 8",
            body=(
                "seed node 8 about topic 999; filler so the body is a plausible "
                "length and the vector is not degenerate."
            ),
            status="canonical",
        )
        _ended_zero_turn_session(conn, "empty-session")
        no_op = db.insert_node(
            conn,
            kind="progress",
            title="Session opened; no substantive work performed",
            body="No user request arrived and no files or tools were used.",
            status="staging",
            session_id="empty-session",
        )
        _ended_zero_turn_session(conn, "real-session")
        conn.execute(
            "UPDATE sessions SET turn_count=1 WHERE id=?",
            ("real-session",),
        )
        conn.commit()
        real = db.insert_node(
            conn,
            kind="progress",
            title="Session opened; no substantive work performed",
            body="This title is misleading, but a real turn was recorded.",
            status="staging",
            session_id="real-session",
        )

        found = pollution_cleanup.discover_known_pollution(conn)
        assert {row["id"] for row in found} == {fixture, no_op}
        assert {row["category"] for row in found} == {
            "benchmark_fixture",
            "no_op_session",
        }
        assert wrong_body not in {row["id"] for row in found}
        assert real not in {row["id"] for row in found}
    finally:
        conn.close()


def test_manifest_apply_is_backup_bound_and_auditable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    project = str(tmp_path)
    conn = db.connect(project)
    try:
        fixture_a = _fixture(conn, 1)
        fixture_b = _fixture(conn, 2)
        _ended_zero_turn_session(conn, "empty-session")
        no_op = db.insert_node(
            conn,
            kind="progress",
            title="Session initialized; no work yet",
            body="Context only. No user task and no substantive work.",
            status="canonical",
            session_id="empty-session",
        )
        legitimate = db.insert_node(
            conn,
            kind="decision",
            title="Keep this",
            body="A real durable decision.",
            status="canonical",
            embedding=_embedding("other"),
        )
        db.add_edge(conn, src=fixture_a, dst=fixture_b, relation="related_to")
        db.add_edge(conn, src=legitimate, dst=no_op, relation="related_to")
        manifest = pollution_cleanup.build_manifest(conn, project_path=project)
    finally:
        conn.close()

    manifest_path = tmp_path / "cleanup.json"
    pollution_cleanup._atomic_write_json(manifest_path, manifest)
    backup_calls = []

    def fake_backup(project_path: str, *, reason: str):
        backup_calls.append((project_path, reason))
        return {
            "manifest": str(tmp_path / "backup.json"),
            "database": str(tmp_path / "backup.db"),
            "vault_uuid": manifest["vault"]["uuid"],
        }

    monkeypatch.setattr(pollution_cleanup.vault_backup, "create_snapshot", fake_backup)
    monkeypatch.setattr(
        pollution_cleanup.log_utils,
        "emit_event",
        lambda *_args, **_kwargs: None,
    )

    receipt = pollution_cleanup.apply_manifest(
        manifest_path,
        project_path=project,
        expected_plan_sha256=manifest["plan_sha256"],
    )

    assert receipt["ok"] is True
    assert receipt["retired_nodes"] == 3
    assert receipt["tombstoned_edges"] == 2
    assert backup_calls == [(project, "known-pollution-cleanup")]

    conn = db.connect(project)
    try:
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(
                "SELECT id,status FROM nodes WHERE id IN (?,?,?,?)",
                (fixture_a, fixture_b, no_op, legitimate),
            )
        }
        assert statuses == {
            fixture_a: "stale",
            fixture_b: "stale",
            no_op: "stale",
            legitimate: "canonical",
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM edges WHERE status='active'"
        ).fetchone()[0] == 0
    finally:
        conn.close()

    applied = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert applied["application"]["state"] == "applied"
    assert applied["application"]["backup"]["manifest"].endswith("backup.json")


def test_manifest_refuses_live_candidate_drift_before_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    project = str(tmp_path)
    conn = db.connect(project)
    try:
        node_id = _fixture(conn, 4)
        manifest = pollution_cleanup.build_manifest(conn, project_path=project)
        db.update_node(conn, node_id, body="changed after the dry run")
    finally:
        conn.close()
    manifest_path = tmp_path / "cleanup.json"
    pollution_cleanup._atomic_write_json(manifest_path, manifest)

    def forbidden_backup(*_args, **_kwargs):
        raise AssertionError("backup must not run after plan drift")

    monkeypatch.setattr(
        pollution_cleanup.vault_backup,
        "create_snapshot",
        forbidden_backup,
    )
    with pytest.raises(
        pollution_cleanup.CleanupSafetyError,
        match="candidates changed",
    ):
        pollution_cleanup.apply_manifest(
            manifest_path,
            project_path=project,
            expected_plan_sha256=manifest["plan_sha256"],
        )


def test_write_path_benchmark_requires_disposable_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    target = measure_write_path._assert_disposable_target(tmp_path)
    assert target.is_relative_to(
        (measure_write_path.paths.validated_test_root() / "vaults").resolve()
    )
    monkeypatch.setattr(
        measure_write_path.paths,
        "validated_test_root",
        lambda: None,
    )
    with pytest.raises(RuntimeError, match="authenticated disposable test root"):
        measure_write_path._assert_disposable_target(tmp_path)
