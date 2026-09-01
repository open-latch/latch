"""Acceptance tests for private, fresh, atomic A2 policy snapshots.

Every policy row and filesystem coordinate in this module is synthetic.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import importlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time

import pytest


_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"


def _module(name: str):
    sys.path.insert(0, str(_SRC))
    try:
        return importlib.import_module(f"latch.gate.{name}")
    finally:
        sys.path.remove(str(_SRC))


@dataclass(frozen=True)
class SyntheticProjection:
    engine: str
    policy_domain_id: str
    binding_rows: tuple[dict[str, object], ...]
    advisory_rows: tuple[dict[str, object], ...]
    reason_counts: dict[str, int]
    freshness_token: str


def _row(
    row_id: int = 1,
    *,
    predicate: str = "file:src/synthetic_target.py",
    classification: str = "binding",
) -> dict[str, object]:
    return {
        "rejected_path_id": row_id,
        "node_id": 7000 + row_id,
        "option": f"synthetic rejected option {row_id}",
        "reason": f"synthetic rejection reason {row_id}",
        "ratifier": "synthetic-founder",
        "decided_at": "2026-08-27T00:00:00Z",
        "scope_predicate": predicate,
        "source": "declared",
        "policy_domain_id": "synthetic-project-a",
        "owner_kind": "decision",
        "owner_status": "canonical",
        "owner_updated_at": "2026-08-27T00:00:00Z",
        "latest_ratification_id": 9000 + row_id,
        "latest_ratification_action": "ratify",
        "latest_ratification_ratifier": "synthetic-founder",
        "latest_ratification_decided_at": "2026-08-27T00:00:00Z",
        "latest_ratification_source": "declared",
        "superseder_ids": (),
        "reconciler_ids": (),
        "authority_basis": "synthetic-ratified-declared",
        "classification": classification,
        "reason_codes": (),
    }


def _projection(
    *,
    binding_rows: tuple[dict[str, object], ...] | None = None,
    advisory_rows: tuple[dict[str, object], ...] = (),
    domain: str = "synthetic-project-a",
    token: str = "projection-token-a",
    reason_counts: dict[str, int] | None = None,
) -> SyntheticProjection:
    return SyntheticProjection(
        engine="predicate-policy-projection-v1",
        policy_domain_id=domain,
        binding_rows=(_row(),) if binding_rows is None else binding_rows,
        advisory_rows=advisory_rows,
        reason_counts={} if reason_counts is None else reason_counts,
        freshness_token=token,
    )


def _action(project_root: Path, *, domain: str = "synthetic-project-a"):
    return {
        "policy_domain_id": domain,
        "project_root": str(project_root),
        "cwd": str(project_root),
        "tool_name": "synthetic.write",
        "proposed_file_paths": ("src/unrelated.py",),
        "diff_paths": (),
        "staged_paths": (),
        "import_names": (),
        "api_names": (),
        "evidence_complete": True,
        "evidence_provenance": ("synthetic-test",),
    }


def test_policy_digest_covers_all_enforcement_inputs():
    snapshot = _module("predicate_snapshot")
    baseline = snapshot.build_policy_snapshot(_projection())["digest"]

    row = _row()
    mutations = {
        "rejected_path_id": 2,
        "node_id": 8001,
        "option": "different synthetic option",
        "reason": "different synthetic reason",
        "ratifier": "different-ratifier",
        "decided_at": "2026-08-28T00:00:00Z",
        "scope_predicate": "file:src/different.py",
        "source": "backfill",
        "owner_kind": "fact",
        "owner_status": "staging",
        "owner_updated_at": "2026-08-28T00:00:00Z",
        "latest_ratification_id": 9999,
        "latest_ratification_action": "reject",
        "latest_ratification_ratifier": "different-ratifier",
        "latest_ratification_decided_at": "2026-08-28T00:00:00Z",
        "latest_ratification_source": "different-source",
        "superseder_ids": (9998,),
        "reconciler_ids": (9997,),
        "authority_basis": "different-authority-basis",
        "reason_codes": ("synthetic_reason",),
    }
    for field, replacement in mutations.items():
        changed = dict(row)
        changed[field] = replacement
        projection = _projection(binding_rows=(changed,))
        assert snapshot.build_policy_snapshot(projection)["digest"] != baseline, field

    other_domain_row = dict(_row())
    other_domain_row["policy_domain_id"] = "synthetic-project-b"
    top_level_changes = (
        _projection(
            binding_rows=(other_domain_row,),
            domain="synthetic-project-b",
        ),
        replace(_projection(), freshness_token="projection-token-b"),
        replace(_projection(), reason_counts={"synthetic_reason": 1}),
        _projection(binding_rows=(), advisory_rows=(_row(classification="advisory"),)),
    )
    for changed in top_level_changes:
        assert snapshot.build_policy_snapshot(changed)["digest"] != baseline

    wrong_binding = dict(_row())
    wrong_binding["policy_domain_id"] = "synthetic-project-b"
    with pytest.raises(ValueError, match="mismatched policy_domain_id"):
        snapshot.build_policy_snapshot(_projection(binding_rows=(wrong_binding,)))

    wrong_advisory = dict(_row(classification="advisory"))
    wrong_advisory["policy_domain_id"] = "synthetic-project-b"
    with pytest.raises(ValueError, match="mismatched policy_domain_id"):
        snapshot.build_policy_snapshot(
            _projection(binding_rows=(), advisory_rows=(wrong_advisory,))
        )

    unbound_advisory = dict(_row(classification="advisory"))
    unbound_advisory["policy_domain_id"] = None
    unbound_digest = snapshot.build_policy_snapshot(
        _projection(binding_rows=(), advisory_rows=(unbound_advisory,))
    )["digest"]
    assert unbound_digest != baseline


def test_next_evaluation_never_uses_stale_snapshot(tmp_path):
    consumer = _module("predicate_consumer")
    snapshot = _module("predicate_snapshot")
    token_path = tmp_path / "source-generation.txt"
    token_path.write_text("generation-a\n", encoding="utf-8")
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    target = private_dir / "policy.snapshot.json"
    project_root = tmp_path / "synthetic-project"
    project_root.mkdir()

    snapshot.publish_policy_snapshot(
        target,
        policy_domain_id="synthetic-project-a",
        projector=_projection,
        freshness_token_path=token_path,
    )
    first = consumer.evaluate_policy(target, _action(project_root))
    assert first.verdict["decision"] == "pass"

    token_path.write_text("generation-b\n", encoding="utf-8")
    stale = consumer.evaluate_policy(target, _action(project_root))
    assert stale.verdict["decision"] == "flag"
    assert "source_changed" in stale.receipt["reason_codes"]

    snapshot.publish_policy_snapshot(
        target,
        policy_domain_id="synthetic-project-a",
        projector=lambda: _projection(token="projection-token-b"),
        freshness_token_path=token_path,
    )
    fresh = consumer.evaluate_policy(target, _action(project_root))
    assert fresh.verdict["decision"] == "pass"

    vault_path = tmp_path / "synthetic-vault.sqlite3"
    vault_path.write_bytes(b"synthetic-vault-generation-a")
    snapshot.publish_policy_snapshot(
        target,
        policy_domain_id="synthetic-project-a",
        projector=lambda: _projection(token="projection-token-vault-a"),
        source_vault_path=vault_path,
    )
    vault_fresh = consumer.evaluate_policy(target, _action(project_root))
    assert vault_fresh.verdict["decision"] == "pass"

    wal_path = Path(f"{vault_path}-wal")
    wal_path.write_bytes(b"synthetic-wal-generation-b")
    vault_stale = consumer.evaluate_policy(target, _action(project_root))
    assert vault_stale.verdict["decision"] == "flag"
    assert "source_changed" in vault_stale.receipt["reason_codes"]


@pytest.mark.parametrize(
    "proposed_path",
    ("src/unrelated.py", "src/synthetic_target.py"),
    ids=("would-pass", "would-block"),
)
def test_source_change_during_evaluation_flags_current_call(
    tmp_path,
    monkeypatch,
    proposed_path,
):
    consumer = _module("predicate_consumer")
    snapshot = _module("predicate_snapshot")
    token_path = tmp_path / "source-generation.txt"
    token_path.write_text("generation-a\n", encoding="utf-8")
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    target = private_dir / "policy.snapshot.json"
    project_root = tmp_path / "synthetic-project"
    project_root.mkdir()

    snapshot.publish_policy_snapshot(
        target,
        policy_domain_id="synthetic-project-a",
        projector=_projection,
        freshness_token_path=token_path,
    )
    action = dict(_action(project_root))
    action["proposed_file_paths"] = (proposed_path,)
    original_evaluate = consumer.predicate.evaluate

    def evaluate_then_change_source(*args, **kwargs):
        verdict = original_evaluate(*args, **kwargs)
        token_path.write_text("generation-b\n", encoding="utf-8")
        return verdict

    monkeypatch.setattr(
        consumer.predicate,
        "evaluate",
        evaluate_then_change_source,
    )
    raced = consumer.evaluate_policy(target, action)

    assert raced.verdict == {
        "engine": "predicate-v1",
        "decision": "flag",
        "llm_calls": 0,
        "matches": [],
    }
    assert raced.receipt["decision"] == "flag"
    assert raced.receipt["reason_codes"] == ["source_changed"]


def test_atomic_publish_concurrent_readers(tmp_path):
    snapshot = _module("predicate_snapshot")
    token_path = tmp_path / "source-generation.txt"
    token_path.write_text("stable-generation\n", encoding="utf-8")
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    target = private_dir / "policy.snapshot.json"
    domain = "synthetic-project-a"
    published: set[str] = set()

    def publish(index: int) -> None:
        document = snapshot.publish_policy_snapshot(
            target,
            policy_domain_id=domain,
            projector=lambda: _projection(token=f"projection-token-{index}"),
            freshness_token_path=token_path,
        )
        published.add(document["digest"])

    publish(0)
    failures: list[object] = []
    observed: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            result = snapshot.load_policy_snapshot(target, policy_domain_id=domain)
            if result.snapshot is None:
                failures.append(result.reason_codes)
            else:
                observed.append(result.snapshot.digest)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    try:
        for index in range(1, 25):
            publish(index)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5)

    assert failures == []
    assert observed
    assert set(observed) <= published


def test_missing_corrupt_or_wrong_domain_snapshot_flags(tmp_path):
    consumer = _module("predicate_consumer")
    snapshot = _module("predicate_snapshot")
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    target = private_dir / "policy.snapshot.json"
    token_path = tmp_path / "source-generation.txt"
    token_path.write_text("generation-a\n", encoding="utf-8")
    project_root = tmp_path / "synthetic-project"
    project_root.mkdir()

    missing = consumer.evaluate_policy(target, _action(project_root))
    assert missing.verdict["decision"] == "flag"
    assert missing.receipt["reason_codes"] == ["snapshot_missing"]

    target.write_text("not-json", encoding="utf-8")
    corrupt = consumer.evaluate_policy(target, _action(project_root))
    assert corrupt.verdict["decision"] == "flag"
    assert corrupt.receipt["reason_codes"] == ["snapshot_corrupt"]

    document = snapshot.publish_policy_snapshot(
        target,
        policy_domain_id="synthetic-project-a",
        projector=_projection,
        freshness_token_path=token_path,
    )
    wrong_domain = consumer.evaluate_policy(
        target,
        _action(project_root, domain="synthetic-project-b"),
    )
    assert wrong_domain.verdict["decision"] == "flag"
    assert wrong_domain.receipt["reason_codes"] == ["wrong_policy_domain"]

    wrong_version_document = json.loads(json.dumps(document))
    wrong_version_document["snapshot_version"] = "predicate-policy-snapshot-v999"
    target.write_text(json.dumps(wrong_version_document), encoding="utf-8")
    wrong_version = consumer.evaluate_policy(target, _action(project_root))
    assert wrong_version.verdict["decision"] == "flag"
    assert wrong_version.receipt["reason_codes"] == ["wrong_snapshot_version"]

    document = snapshot.publish_policy_snapshot(
        target,
        policy_domain_id="synthetic-project-a",
        projector=_projection,
        freshness_token_path=token_path,
    )
    token_path.unlink()
    missing_source = consumer.evaluate_policy(target, _action(project_root))
    assert missing_source.verdict["decision"] == "flag"
    assert missing_source.receipt["reason_codes"] == ["source_missing"]

    token_path.write_text("generation-b\n", encoding="utf-8")
    document = snapshot.publish_policy_snapshot(
        target,
        policy_domain_id="synthetic-project-a",
        projector=_projection,
        freshness_token_path=token_path,
    )
    document["binding_rows"][0]["row"]["node_id"] = 123456
    target.write_text(json.dumps(document), encoding="utf-8")
    mismatched = consumer.evaluate_policy(target, _action(project_root))
    assert mismatched.verdict["decision"] == "flag"
    assert mismatched.receipt["reason_codes"] == ["snapshot_digest_mismatch"]


def test_snapshot_is_private_and_untracked(tmp_path):
    snapshot = _module("predicate_snapshot")
    token_path = tmp_path / "source-generation.txt"
    token_path.write_text("generation-a\n", encoding="utf-8")
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    target = private_dir / "policy.snapshot.json"

    snapshot.publish_policy_snapshot(
        target,
        policy_domain_id="synthetic-project-a",
        projector=_projection,
        freshness_token_path=token_path,
    )
    assert stat.S_IMODE(target.stat().st_mode) & 0o077 == 0

    public_target = _ROOT / ".synthetic-policy.snapshot.json"
    with pytest.raises(ValueError, match="public source tree"):
        snapshot.publish_policy_snapshot(
            public_target,
            policy_domain_id="synthetic-project-a",
            projector=_projection,
            freshness_token_path=token_path,
        )
    assert not public_target.exists()

    tracked = subprocess.run(
        ["git", "ls-files", "*.snapshot.json", "*predicate-policy*.json"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert tracked == ""
