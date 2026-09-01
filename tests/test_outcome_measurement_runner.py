from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from latch.store import db
from latch.evals import outcome_measurement as om
from latch.evals import outcome_measurement_runner as runner
from latch.proof import project_proof


UTC = timezone.utc
FIXTURES = Path(__file__).parent / "fixtures" / "outcome_measurement"


def _frozen_fixture_pack() -> dict[str, bytes]:
    return {
        name: (FIXTURES / name).read_bytes()
        for name in om.FROZEN_FIXTURE_PACK_SHA256
    }


def test_pinned_runner_uses_readonly_vault_proof_and_full_envelope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    conn = db.connect(str(project))
    try:
        context = project_proof.ProjectProofContext.from_vault_identity(
            conn._kb_vault_identity,
            key_epoch="outcome-v2.6-key-test",
        )
        target_proof = context.prove(str(project))
    finally:
        conn.close()

    # Production discovery is source-specific: S1 accepts gate-*.log and S2
    # accepts Codex rollout-*.jsonl (or Claude project transcripts).
    gate = tmp_path / "gate-2026-08-03.log"
    host = tmp_path / "rollout-2026-07-29T00-00-00-sanitized.jsonl"
    gate.write_bytes(
        (FIXTURES / "gate-2026-08-03.sanitized.jsonl").read_bytes()
    )
    host.write_bytes(
        (FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl").read_bytes()
    )
    roots = {om.SOURCE_GATE: (str(gate),), om.SOURCE_HOST: (str(host),)}
    commit = "a" * 40
    config = om.MeasurementConfig(
        t0=datetime(2026, 7, 1, tzinfo=UTC),
        cap=datetime(2026, 7, 22, tzinfo=UTC),
        target_project_proof=target_proof,
        key_epoch="outcome-v2.6-key-test",
        pinned_runtime_version="runtime-test",
        implementation_commit=commit,
        source_roots=roots,
        require_fresh_snapshots=True,
    )
    contract = b"pinned test contract"
    contract_hash = hashlib.sha256(contract).hexdigest()
    monkeypatch.setattr(om, "CONTRACT_SHA256", contract_hash)
    capture = om.CapturePin(om.CAPTURE_NODE_ID, contract_hash)
    fixture_bytes = _frozen_fixture_pack()
    fixture_hashes = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in fixture_bytes.items()
    }
    manifest = om.MeasurementManifest(
        contract_sha256=contract_hash,
        ratification_node_ids=om.RATIFICATION_NODE_IDS,
        implementation_commit=commit,
        measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
        source_roots=roots,
        fixture_hashes=fixture_hashes,
        composite_sha256="",
    )
    manifest = replace(manifest, composite_sha256=manifest.expected_composite())
    canaries = tuple(
        om.CanaryEvidence(
            host=name,
            nonce=f"nonce-{name}",
            tool_result_seen=True,
            gate_log_seen=True,
            host_record_seen=True,
            dual_source_joined=True,
            runtime_version="runtime-test",
            measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
            key_epoch="outcome-v2.6-key-test",
            project_proof=target_proof,
        )
        for name in ("codex", "claude")
    )
    monkeypatch.setattr(runner, "_deployed_implementation_commit", lambda: commit)

    result = runner.run_pinned_audit(
        project_path=project,
        source_roots=roots,
        config=config,
        contract_bytes=contract,
        capture=capture,
        manifest=manifest,
        fixture_bytes=fixture_bytes,
        canaries=canaries,
    )

    payload = json.loads(result.report)
    assert payload["oracles"]["invalidated"] is False
    assert payload["oracles"]["v1_green"] is False


def test_pinned_runner_invalidates_configured_project_proof_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    conn = db.connect(str(project))
    try:
        context = project_proof.ProjectProofContext.from_vault_identity(
            conn._kb_vault_identity,
            key_epoch="outcome-v2.6-key-test",
        )
        foreign = context.prove(str(tmp_path / "foreign"))
    finally:
        conn.close()

    gate = tmp_path / "gate-2026-08-03.log"
    host = tmp_path / "rollout-2026-07-29T00-00-00-sanitized.jsonl"
    gate.write_bytes(
        (FIXTURES / "gate-2026-08-03.sanitized.jsonl").read_bytes()
    )
    host.write_bytes(
        (FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl").read_bytes()
    )
    roots = {om.SOURCE_GATE: (str(gate),), om.SOURCE_HOST: (str(host),)}
    contract = b"pinned test contract"
    contract_hash = hashlib.sha256(contract).hexdigest()
    monkeypatch.setattr(om, "CONTRACT_SHA256", contract_hash)
    commit = "b" * 40
    config = om.MeasurementConfig(
        t0=datetime(2026, 7, 1, tzinfo=UTC),
        cap=datetime(2026, 7, 22, tzinfo=UTC),
        target_project_proof=foreign,
        key_epoch="outcome-v2.6-key-test",
        pinned_runtime_version="runtime-test",
        implementation_commit=commit,
        source_roots=roots,
        require_fresh_snapshots=True,
    )
    fixture_bytes = _frozen_fixture_pack()
    manifest = om.MeasurementManifest(
        contract_sha256=contract_hash,
        ratification_node_ids=om.RATIFICATION_NODE_IDS,
        implementation_commit=commit,
        measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
        source_roots=roots,
        fixture_hashes={
            name: hashlib.sha256(data).hexdigest()
            for name, data in fixture_bytes.items()
        },
        composite_sha256="",
    )
    manifest = replace(manifest, composite_sha256=manifest.expected_composite())
    canaries = tuple(
        om.CanaryEvidence(
            host=name,
            nonce=f"nonce-{name}",
            tool_result_seen=True,
            gate_log_seen=True,
            host_record_seen=True,
            dual_source_joined=True,
            runtime_version="runtime-test",
            measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
            key_epoch="outcome-v2.6-key-test",
            project_proof=foreign,
        )
        for name in ("codex", "claude")
    )
    monkeypatch.setattr(runner, "_deployed_implementation_commit", lambda: commit)

    result = runner.run_pinned_audit(
        project_path=project,
        source_roots=roots,
        config=config,
        contract_bytes=contract,
        capture=om.CapturePin(om.CAPTURE_NODE_ID, contract_hash),
        manifest=manifest,
        fixture_bytes=fixture_bytes,
        canaries=canaries,
    )

    payload = json.loads(result.report)
    assert payload["oracles"]["invalidated"] is True
    assert "configured_target_project_proof:foreign_project" in payload[
        "oracles"
    ]["invalidation_reasons"]


def test_pinned_runner_rejects_freshness_bypass(tmp_path: Path) -> None:
    config = om.MeasurementConfig(
        t0=datetime(2026, 7, 1, tzinfo=UTC),
        cap=datetime(2026, 7, 22, tzinfo=UTC),
        target_project_proof={
            "version": project_proof.PROJECT_PROOF_VERSION,
            "key_epoch": "outcome-v2.6-key-test",
            "fingerprint": "0" * 64,
        },
        key_epoch="outcome-v2.6-key-test",
        pinned_runtime_version="runtime-test",
        implementation_commit="c" * 40,
        source_roots={om.SOURCE_GATE: ("g",), om.SOURCE_HOST: ("h",)},
        require_fresh_snapshots=False,
    )
    with pytest.raises(ValueError, match="fresh post-drain snapshots"):
        runner.run_pinned_audit(
            project_path=tmp_path,
            source_roots=config.source_roots,
            config=config,
            contract_bytes=b"unused",
            capture=om.CapturePin(om.CAPTURE_NODE_ID, om.CONTRACT_SHA256),
            manifest=om.MeasurementManifest(
                contract_sha256=om.CONTRACT_SHA256,
                ratification_node_ids=om.RATIFICATION_NODE_IDS,
                implementation_commit="c" * 40,
                measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
                source_roots=config.source_roots,
                fixture_hashes={"fixture": "0" * 64},
                composite_sha256="0" * 64,
            ),
            fixture_bytes={"fixture": b"unused"},
            canaries=(),
        )
