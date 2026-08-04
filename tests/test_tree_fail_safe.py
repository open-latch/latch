"""Issue #81: transactional autonomous tree maintenance regressions."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import embeddings  # noqa: E402
import maintenance_receipts  # noqa: E402
import mcp_server  # noqa: E402
import model_backends  # noqa: E402
import paths  # noqa: E402
import selfheal  # noqa: E402
import tree  # noqa: E402


def _vector() -> bytes:
    vector = np.zeros(embeddings.DIM, dtype=np.float32)
    vector[0] = 1.0
    return embeddings.to_blob(vector)


def _seed_hierarchy(project: str, leaf_count: int) -> tuple[object, int, list[int]]:
    conn = db.connect(project)
    prior = db.insert_node(
        conn,
        kind="summary",
        title="last known good summary",
        body="the hierarchy that must survive a failed replacement",
        status="canonical",
        embedding=_vector(),
    )
    conn.execute(
        "UPDATE nodes SET depth=1, content_hash=? WHERE id=?",
        ("f" * 64, prior),
    )
    leaves = []
    for index in range(leaf_count):
        node_id = db.insert_node(
            conn,
            kind="fact",
            title=f"transactional tree leaf {index}",
            body=f"cluster-specific hierarchy evidence {index}",
            status="canonical",
            embedding=_vector(),
        )
        conn.execute("UPDATE nodes SET parent_id=? WHERE id=?", (prior, node_id))
        leaves.append(node_id)
    conn.commit()
    return conn, prior, leaves


def _hierarchy_snapshot(conn) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT id, parent_id, depth, status, content_hash, title, body "
            "FROM nodes ORDER BY id"
        ).fetchall()
    ]


def _runner(backend: str, root: Path) -> tuple[str, str, str, str]:
    return backend, str(root / backend), str(root), str(root)


def _auth_failure(backend: str) -> model_backends.ModelCallResult:
    return model_backends.ModelCallResult(
        None,
        "Failed to authenticate: OAuth session expired",
        False,
        backend,
        failure_kind="authentication",
        terminal=True,
    )


def _success(backend: str, title: str = "replacement summary") -> model_backends.ModelCallResult:
    return model_backends.ModelCallResult(
        json.dumps({"title": title, "body": "complete replacement hierarchy summary"}),
        None,
        False,
        backend,
    )


def test_auth_failure_is_one_attempt_and_leaves_hierarchy_and_budget_safe(
    tmp_path: Path, monkeypatch,
) -> None:
    project = str(tmp_path)
    conn, _prior, _leaves = _seed_hierarchy(project, 3)
    before = _hierarchy_snapshot(conn)
    attempts = []

    monkeypatch.setattr(tree, "_cluster_average_linkage", lambda *_a, **_k: [[0, 1, 2]])
    monkeypatch.setattr(
        tree.budget,
        "check_and_record",
        lambda *_a, **_k: (attempts.append("reserved") or True, {}),
    )
    monkeypatch.setattr(
        tree.model_backends,
        "invoke_prompt",
        lambda *_a, **kwargs: _auth_failure(str(kwargs.get("backend") or "claude")),
    )

    result = tree.build_tree(
        conn,
        project_path=project,
        backend_policy=[_runner("claude", tmp_path)],
    )

    assert result["ok"] is False
    assert result["failure_kind"] == "authentication"
    assert result["attempts"] == 1
    assert attempts == ["reserved"]
    assert result["retry_pending"] is True
    assert _hierarchy_snapshot(conn) == before
    conn.close()


def test_late_terminal_failure_rolls_back_every_prepared_cluster(
    tmp_path: Path, monkeypatch,
) -> None:
    project = str(tmp_path)
    conn, _prior, _leaves = _seed_hierarchy(project, 6)
    before = _hierarchy_snapshot(conn)
    calls = []

    monkeypatch.setattr(
        tree,
        "_cluster_average_linkage",
        lambda *_a, **_k: [[0, 1, 2], [3, 4, 5]],
    )
    monkeypatch.setattr(tree.budget, "check_and_record", lambda *_a, **_k: (True, {}))

    def invoke(*_args, **kwargs):
        backend = str(kwargs.get("backend") or "claude")
        calls.append(backend)
        return _success(backend, "first staged summary") if len(calls) == 1 else _auth_failure(backend)

    monkeypatch.setattr(tree.model_backends, "invoke_prompt", invoke)
    result = tree.build_tree(
        conn,
        project_path=project,
        backend_policy=[_runner("claude", tmp_path)],
    )

    assert result["ok"] is False
    assert result["attempts"] == 2
    assert result["summaries_generated"] == 0
    assert _hierarchy_snapshot(conn) == before
    assert tree._tree_stage_path(project).is_file()
    conn.close()


def test_commit_phase_error_rolls_back_the_complete_replacement(
    tmp_path: Path, monkeypatch,
) -> None:
    project = str(tmp_path)
    conn, _prior, _leaves = _seed_hierarchy(project, 3)
    before = _hierarchy_snapshot(conn)
    monkeypatch.setattr(tree, "_cluster_average_linkage", lambda *_a, **_k: [[0, 1, 2]])
    monkeypatch.setattr(tree.budget, "check_and_record", lambda *_a, **_k: (True, {}))
    monkeypatch.setattr(
        tree.model_backends,
        "invoke_prompt",
        lambda *_a, **_k: _success("claude"),
    )
    monkeypatch.setattr(
        tree.embeddings,
        "embed",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("embedding write failed")),
    )

    result = tree.build_tree(
        conn,
        project_path=project,
        backend_policy=[_runner("claude", tmp_path)],
    )

    assert result["ok"] is False and result["reason"] == "commit_failed"
    assert result["retry_pending"] is True
    assert _hierarchy_snapshot(conn) == before
    conn.close()


def test_oversized_degraded_replacement_preserves_prior_hierarchy(
    tmp_path: Path, monkeypatch,
) -> None:
    project = str(tmp_path)
    conn, _prior, _leaves = _seed_hierarchy(project, 6)
    before = _hierarchy_snapshot(conn)
    monkeypatch.setattr(
        tree,
        "_cluster_by_threshold",
        lambda *_a, **_k: [[0, 1, 2, 3, 4, 5]],
    )

    result = tree.build_tree(
        conn,
        project_path=project,
        use_llm=False,
        linkage="single",
        max_cluster_members=5,
    )

    assert result["ok"] is False and result["reason"] == "oversized_cluster"
    assert result["oversized_skipped"] == 1
    assert _hierarchy_snapshot(conn) == before
    conn.close()


def test_total_attempt_cap_stages_progress_and_retry_commits_once_complete(
    tmp_path: Path, monkeypatch,
) -> None:
    project = str(tmp_path)
    conn, prior, leaves = _seed_hierarchy(project, 9)
    before = _hierarchy_snapshot(conn)
    monkeypatch.setattr(
        tree,
        "_cluster_average_linkage",
        lambda *_a, **_k: [[0, 1, 2], [3, 4, 5], [6, 7, 8]],
    )
    monkeypatch.setattr(tree.budget, "check_and_record", lambda *_a, **_k: (True, {}))
    calls = []
    monkeypatch.setattr(
        tree.model_backends,
        "invoke_prompt",
        lambda *_a, **kwargs: (
            calls.append(str(kwargs.get("backend") or "claude"))
            or _success(str(kwargs.get("backend") or "claude"), f"summary {len(calls)}")
        ),
    )

    first = tree.build_tree(
        conn,
        project_path=project,
        max_summaries=2,
        backend_policy=[_runner("claude", tmp_path)],
    )
    assert first["ok"] is False and first["reason"] == "attempt_cap"
    assert first["attempts"] == 2
    assert _hierarchy_snapshot(conn) == before

    calls.clear()
    second = tree.build_tree(
        conn,
        project_path=project,
        max_summaries=2,
        backend_policy=[_runner("claude", tmp_path)],
    )
    assert second["ok"] is True
    assert second["attempts"] == 1
    assert second["summaries_staged_reused"] == 2
    assert calls == ["claude"]
    assert db.get_node(conn, prior)["status"] == "stale"
    assert all(db.get_node(conn, leaf)["parent_id"] != prior for leaf in leaves)
    assert not tree._tree_stage_path(project).exists()
    conn.close()


def test_explicit_approved_fallback_uses_only_order_and_records_backend(
    tmp_path: Path, monkeypatch,
) -> None:
    project = str(tmp_path)
    conn, prior, leaves = _seed_hierarchy(project, 3)
    monkeypatch.setattr(tree, "_cluster_average_linkage", lambda *_a, **_k: [[0, 1, 2]])
    monkeypatch.setattr(tree.budget, "check_and_record", lambda *_a, **_k: (True, {}))
    calls = []

    def invoke(*_args, **kwargs):
        backend = str(kwargs["backend"])
        calls.append(backend)
        return _auth_failure(backend) if backend == "claude" else _success(backend)

    monkeypatch.setattr(tree.model_backends, "invoke_prompt", invoke)
    result = tree.build_tree(
        conn,
        project_path=project,
        backend_policy=[_runner("claude", tmp_path), _runner("codex", tmp_path)],
    )

    assert result["ok"] is True
    assert calls == ["claude", "codex"]
    assert result["fallback_used"] is True
    assert result["backend_used"] == "codex"
    assert result["backends_used"] == ["codex"]
    assert db.get_node(conn, prior)["status"] == "stale"
    assert all(db.get_node(conn, leaf)["parent_id"] != prior for leaf in leaves)
    conn.close()


def test_terminal_primary_is_circuit_broken_for_remaining_clusters(
    tmp_path: Path, monkeypatch,
) -> None:
    project = str(tmp_path)
    conn, _prior, _leaves = _seed_hierarchy(project, 6)
    monkeypatch.setattr(
        tree,
        "_cluster_average_linkage",
        lambda *_a, **_k: [[0, 1, 2], [3, 4, 5]],
    )
    reservations = []
    monkeypatch.setattr(
        tree.budget,
        "check_and_record",
        lambda *_a, **_k: (reservations.append(True) or True, {}),
    )
    calls = []

    def invoke(*_args, **kwargs):
        backend = str(kwargs["backend"])
        calls.append(backend)
        if backend == "claude":
            return _auth_failure(backend)
        return _success(backend, f"codex replacement {len(calls)}")

    monkeypatch.setattr(tree.model_backends, "invoke_prompt", invoke)
    result = tree.build_tree(
        conn,
        project_path=project,
        backend_policy=[_runner("claude", tmp_path), _runner("codex", tmp_path)],
    )

    assert result["ok"] is True
    assert calls == ["claude", "codex", "codex"]
    assert result["attempts"] == 3
    assert len(reservations) == 3
    assert result["fallback_used"] is True
    assert result["backend_used"] == "codex"
    conn.close()


def test_no_approved_backend_ready_fails_safe_and_visible(
    tmp_path: Path, monkeypatch,
) -> None:
    project = str(tmp_path)
    conn, _prior, _leaves = _seed_hierarchy(project, 3)
    before = _hierarchy_snapshot(conn)
    monkeypatch.setattr(tree, "_cluster_average_linkage", lambda *_a, **_k: [[0, 1, 2]])
    monkeypatch.setattr(tree.budget, "check_and_record", lambda *_a, **_k: (True, {}))
    monkeypatch.setattr(
        tree.model_backends,
        "invoke_prompt",
        lambda *_a, **kwargs: _auth_failure(str(kwargs["backend"])),
    )
    result = tree.build_tree(
        conn,
        project_path=project,
        backend_policy=[_runner("claude", tmp_path), _runner("codex", tmp_path)],
    )
    assert result["ok"] is False
    assert [item["backend"] for item in result["backend_attempts"]] == ["claude", "codex"]
    assert _hierarchy_snapshot(conn) == before

    receipt = maintenance_receipts.record_tree_blocker(project, result)
    assert "last known-good hierarchy remains active" in receipt["text"]
    assert "Remediation:" in receipt["text"]
    monkeypatch.setattr(mcp_server, "_project_cwd", lambda: project)
    surfaced = mcp_server._attach_pending_lifecycle_notice({
        "kb_activity": {
            "must_display_to_user": True,
            "summary": "Read one authoritative node.",
        }
    })
    notice = surfaced["kb_activity"]["lifecycle_notice"]
    assert notice["surface_kind"] == "maintenance_blocker"
    assert "Impact:" in notice["text"] and "Remediation:" in notice["text"]
    assert maintenance_receipts.pending_blockers(project) == []
    conn.close()


def _fake_executable(path: Path) -> str:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize("backend", ["claude", "codex", "cursor"])
def test_single_host_policy_never_invents_fallback(
    tmp_path: Path, backend: str, monkeypatch,
) -> None:
    settings = tmp_path / f"{backend}.json"
    executable = _fake_executable(tmp_path / f"{backend}-bin")
    paths.write_maintenance_runner(
        backend=backend,
        executable=executable,
        home=str(tmp_path),
        search_path=str(tmp_path),
        runtime_settings_file=settings,
    )
    policy = paths.configured_maintenance_policy(settings)
    assert policy["fallback_approved"] is False
    assert policy["order"] == [backend]

    project = str(tmp_path)
    conn, _prior, _leaves = _seed_hierarchy(project, 3)
    monkeypatch.setattr(tree, "_cluster_average_linkage", lambda *_a, **_k: [[0, 1, 2]])
    monkeypatch.setattr(tree.budget, "check_and_record", lambda *_a, **_k: (True, {}))
    calls = []
    monkeypatch.setattr(
        tree.model_backends,
        "invoke_prompt",
        lambda *_a, **kwargs: (
            calls.append(str(kwargs["backend"]))
            or _success(str(kwargs["backend"]))
        ),
    )
    result = tree.build_tree(
        conn,
        project_path=project,
        backend_policy=[policy["runners"][backend]],
    )
    assert result["ok"] is True
    assert calls == [backend]
    assert result["backend_used"] == backend
    assert result["fallback_used"] is False
    conn.close()


def test_multi_host_policy_requires_and_preserves_exact_approved_order(tmp_path: Path) -> None:
    runners = {
        backend: {
            "executable": _fake_executable(tmp_path / f"{backend}-bin"),
            "home": str(tmp_path),
            "path": str(tmp_path),
        }
        for backend in ("claude", "codex", "cursor")
    }
    settings = tmp_path / "runtime_settings.json"
    paths.write_approved_maintenance_fallback_policy(
        order=["codex", "cursor", "claude"],
        runners=runners,
        runtime_settings_file=settings,
    )
    policy = paths.configured_maintenance_policy(settings)
    assert policy["fallback_approved"] is True
    assert policy["order"] == ["codex", "cursor", "claude"]

    payload = json.loads(settings.read_text(encoding="utf-8"))
    payload["maintenance_fallback_approved"] = False
    settings.write_text(json.dumps(payload), encoding="utf-8")
    disabled = paths.configured_maintenance_policy(settings)
    assert disabled["fallback_approved"] is False
    assert disabled["order"] == ["codex"]

    paths.write_maintenance_runner(
        backend="cursor",
        executable=runners["cursor"]["executable"],
        home=runners["cursor"]["home"],
        search_path=runners["cursor"]["path"],
        runtime_settings_file=settings,
    )
    reset = paths.configured_maintenance_policy(settings)
    assert reset["fallback_approved"] is False
    assert reset["order"] == ["cursor"]


def test_failed_autonomous_tree_does_not_stamp_success_and_remains_due(
    tmp_path: Path, monkeypatch,
) -> None:
    project = str(tmp_path)
    db.connect(project).close()
    now = datetime.now(timezone.utc)
    old_weekly = (now - timedelta(hours=169)).isoformat()
    selfheal._save_state(project, {
        "last_backup_at": now.isoformat(),
        "last_heal_at": now.isoformat(),
        "last_weekly_at": old_weekly,
        "last_workstream_shadow_at": now.isoformat(),
    })
    monkeypatch.setattr(selfheal, "_backup_db", lambda _project: True)
    monkeypatch.setattr(selfheal, "_prune_backups", lambda _project: None)
    monkeypatch.setattr(
        selfheal.maintenance,
        "run_weekly_maintenance",
        lambda _project: {"ok": True},
    )
    monkeypatch.setattr(
        selfheal.maintenance,
        "run_tree_rebuild",
        lambda *_a, **_k: {
            "ok": False,
            "reason": "backend_unavailable",
            "failure_kind": "authentication",
            "attempts": 1,
            "backend_attempts": [{
                "backend": "claude",
                "outcome": "failed",
                "failure_kind": "authentication",
            }],
            "retry_pending": True,
        },
    )

    result = selfheal.run_selfheal(project)
    state = selfheal._load_state(project)
    assert result["ok"] is False and result["reason"] == "tree_failed"
    assert state["last_weekly_at"] == old_weekly
    assert "last_weekly_decay_at" in state
    assert selfheal._due(state, "last_weekly_at", selfheal.WEEKLY_INTERVAL_H, now)
    assert maintenance_receipts.pending_blockers(project)


def test_degraded_autonomous_tree_does_not_stamp_success(
    tmp_path: Path, monkeypatch,
) -> None:
    project = str(tmp_path)
    db.connect(project).close()
    now = datetime.now(timezone.utc)
    old_weekly = (now - timedelta(hours=169)).isoformat()
    selfheal._save_state(project, {
        "last_backup_at": now.isoformat(),
        "last_heal_at": now.isoformat(),
        "last_weekly_at": old_weekly,
        "last_workstream_shadow_at": now.isoformat(),
    })
    monkeypatch.setattr(selfheal, "_backup_db", lambda _project: True)
    monkeypatch.setattr(selfheal, "_prune_backups", lambda _project: None)
    monkeypatch.setattr(
        selfheal.maintenance,
        "run_weekly_maintenance",
        lambda _project: {"ok": True},
    )
    monkeypatch.setattr(
        selfheal.maintenance,
        "run_tree_rebuild",
        lambda *_a, **_k: {
            "ok": True,
            "oversized_skipped": 1,
            "backend_used": "codex",
        },
    )

    result = selfheal.run_selfheal(project)
    state = selfheal._load_state(project)
    assert result["ok"] is False and result["reason"] == "tree_failed"
    assert result["tree"]["reason"] == "tree_degraded"
    assert state["last_weekly_at"] == old_weekly
    assert selfheal._due(state, "last_weekly_at", selfheal.WEEKLY_INTERVAL_H, now)
    assert maintenance_receipts.pending_blockers(project)


def test_terminal_failure_classification_covers_pre_invocation_cases() -> None:
    assert model_backends.classify_failure("Failed to authenticate: OAuth session expired") == (
        "authentication", True,
    )
    assert model_backends.classify_failure("subprocess failed: FileNotFoundError") == (
        "missing_executable", True,
    )
    assert model_backends.classify_failure("tree timed out", timed_out=True) == (
        "timeout", False,
    )
