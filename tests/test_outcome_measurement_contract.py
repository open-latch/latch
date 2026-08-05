"""Executable acceptance matrix for frozen outcome-measurement contract v2.6."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path

import pytest

import outcome_measurement as om


UTC = timezone.utc
T0 = datetime(2026, 8, 1, tzinfo=UTC)
CAP = T0 + timedelta(days=21)
KEY = b"v2.6 test vault key material...."  # exactly 32 bytes
EPOCH = "epoch-2026-08"
RUNTIME = "runtime-pinned"
PROJECT = "/sanitized/project"
FIXTURES = Path("tests") / "fixtures" / "outcome_measurement"


def _proof_context(*, key: bytes = KEY, epoch: str = EPOCH):
    return om.ProjectProofContext.from_vault_key(key, key_epoch=epoch)


def _proof(path: str = PROJECT, *, key: bytes = KEY, epoch: str = EPOCH):
    return _proof_context(key=key, epoch=epoch).prove(path)


def _config(
    *,
    protocol: str = om.MEASUREMENT_PROTOCOL_VERSION,
    fresh: bool = False,
    roots=None,
    cap_reached: bool = False,
    implementation_commit: str | None = "a" * 40,
):
    if roots is None:
        roots = {
            om.SOURCE_GATE: (
                str(FIXTURES / "gate-2026-08-03.sanitized.jsonl"),
            ),
            om.SOURCE_HOST: (
                str(FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl"),
            ),
        }
    return om.MeasurementConfig(
        t0=T0,
        cap=CAP,
        target_project_proof=_proof(),
        key_epoch=EPOCH,
        pinned_runtime_version=RUNTIME,
        measurement_protocol_version=protocol,
        implementation_commit=implementation_commit,
        source_roots=roots,
        require_fresh_snapshots=fresh,
        cap_reached=cap_reached,
    )


def _canaries(config=None):
    config = config or _config()
    return tuple(
        om.CanaryEvidence(
            host=host,
            nonce=f"nonce-{host}",
            tool_result_seen=True,
            gate_log_seen=True,
            host_record_seen=True,
            dual_source_joined=True,
            runtime_version=config.pinned_runtime_version,
            measurement_protocol_version=config.measurement_protocol_version,
            key_epoch=config.key_epoch,
            project_proof=config.target_project_proof,
        )
        for host in ("codex", "claude code")
    )


def test_measurement_cap_is_exactly_21_days():
    assert _config().cap - _config().t0 == timedelta(days=21)
    with pytest.raises(ValueError, match="exactly 21 days"):
        replace(_config(), cap=CAP - timedelta(seconds=1))
    with pytest.raises(ValueError, match="exactly 21 days"):
        replace(_config(), cap=CAP + timedelta(seconds=1))


def test_cap_reached_is_derived_from_trusted_post_drain_clock_inclusively():
    before = _measure_current(
        measurement_now=CAP + timedelta(seconds=om.FRESHNESS_SECONDS - 1),
        cap_reached=True,
    )
    assert before.measurement_taken_at == (
        CAP + timedelta(seconds=om.FRESHNESS_SECONDS - 1)
    )
    assert before.config.cap_reached is False

    at_close = _measure_current(
        measurement_now=CAP + timedelta(seconds=om.FRESHNESS_SECONDS),
        cap_reached=False,
    )
    assert at_close.config.cap_reached is True
    assert om.compute_oracles(at_close).verdict == "insufficient-n"

    after = _measure_current(
        measurement_now=CAP + timedelta(days=1),
        cap_reached=False,
    )
    assert after.config.cap_reached is True


def test_oracle_api_fails_closed_without_measurement_clock_provenance():
    missing_clock = replace(
        _state_from_receipts([_receipt("no-clock", 0)]),
        measurement_taken_at=None,
    )
    result = om.compute_oracles(missing_clock)
    assert result.invalidated is True
    assert "measurement_time_missing" in result.invalidation_reasons

    forged_close = replace(
        missing_clock,
        config=replace(missing_clock.config, cap_reached=True),
    )
    reasons = om.compute_oracles(forged_close).invalidation_reasons
    assert "cap_reached_unverified" in reasons


@lru_cache(maxsize=2)
def _corpus_seed(source: str) -> om.Observation:
    """Parse a sanitized real corpus record before applying a test mutation."""

    config = _config()
    if source == om.SOURCE_GATE:
        rows, _markers, _checks = om.parse_gate_log_bytes(
            (FIXTURES / "gate-2026-08-03.sanitized.jsonl").read_bytes(),
            file="tests/fixtures/outcome_measurement/gate-2026-08-03.sanitized.jsonl",
            config=config,
        )
    else:
        rows, _markers = om.parse_host_record_bytes(
            (FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl").read_bytes(),
            file="tests/fixtures/outcome_measurement/codex-rollout-2026-07-29.sanitized.jsonl",
            config=config,
            project_proof_context=_proof_context(),
        )
    assert rows
    return rows[0]


def _ids(valid: bool = True):
    if not valid:
        return {"evidence_ids": "bad"}
    return {name: [] for name in om.REQUIRED_ID_LIST_FIELDS}


def _obs(
    nonce: str,
    source: str,
    minute: int,
    *,
    session: str = "session-a",
    protocol: str = om.MEASUREMENT_PROTOCOL_VERSION,
    proof=None,
    epoch: str | None = EPOCH,
    runtime: str | None = RUNTIME,
    attestation: str | None = RUNTIME,
    verdict: str | None = "PROCEED",
    skipped: bool = False,
    observable: bool | None = True,
    evidence_available: bool = True,
    progress: int = 1,
    inserts: int = 0,
    linked: bool = False,
    cited: bool = False,
    touches: int = 0,
    ts: datetime | None = None,
    valid_ids: bool = True,
):
    seed = _corpus_seed(source)
    return replace(
        seed,
        source=source,
        byte_offset=seed.byte_offset + max(1, minute + 1),
        nonce=nonce,
        ts=ts if ts is not None else T0 + timedelta(minutes=minute),
        session_id=session,
        adapter="codex",
        attestation=attestation,
        measurement_protocol_version=protocol,
        project_proof=_proof() if proof is None else proof,
        host_scope_project_proof=(
            (_proof() if proof is None else proof)
            if source == om.SOURCE_HOST
            else None
        ),
        key_epoch=epoch,
        runtime_version=runtime,
        verdict=verdict if source == om.SOURCE_GATE else None,
        verdict_id_lists=_ids(valid_ids) if source == om.SOURCE_GATE else None,
        skipped=skipped if source == om.SOURCE_GATE else None,
        observable=observable,
        evidence_available=evidence_available,
        progress_inserts=progress,
        inserts=inserts,
        linked_cited_insert=linked,
        cited_edge_activity=cited,
        touches=touches,
        legacy_project=False,
        hash_annotated=False,
        pre_nonce=False,
    )


def _pair(nonce: str, minute: int, **kwargs):
    return [
        _obs(nonce, om.SOURCE_GATE, minute, **kwargs),
        _obs(nonce, om.SOURCE_HOST, minute, **kwargs),
    ]


@lru_cache(maxsize=1)
def _corpus_accounting_seed():
    roots = {
        om.SOURCE_GATE: (
            str(FIXTURES / "gate-2026-08-03.sanitized.jsonl"),
        ),
        om.SOURCE_HOST: (
            str(FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl"),
        ),
    }
    config = _config(roots=roots)
    return om.measure(
        roots,
        config,
        project_proof_context=_proof_context(),
        now=datetime(2026, 8, 6, tzinfo=UTC),
        enumerate_files=lambda items: tuple(Path(item) for item in items),
    )


def _accounting():
    seed = _corpus_accounting_seed()
    return (
        tuple(
            replace(
                row,
                missing_files=0,
                unreadable_files=0,
                malformed_regions=0,
                unstable_files=0,
            )
            for row in seed.source_health
        ),
        tuple(replace(row, complete=True) for row in seed.candidate_completeness),
    )


def _state(rows, *, config=None, markers=(), prior=(), snapshots=(), gate_rows=()):
    health, completeness = _accounting()
    return replace(
        om.fold_observations(
            rows,
            config or _config(),
            prior_receipts=prior,
            source_health=health,
            snapshots=snapshots,
            loss_markers=markers,
            gate_rows=gate_rows,
            candidate_completeness=completeness,
        ),
        measurement_taken_at=T0 + timedelta(days=2),
    )


def _receipt(
    nonce: str,
    index: int,
    *,
    disposition="confirmatory",
    outcome="ACCEPTED",
    protocol=om.MEASUREMENT_PROTOCOL_VERSION,
):
    ts = T0 + timedelta(minutes=index)
    seed = _receipt_seed()
    observations = tuple(
        replace(row, nonce=nonce, ts=ts, byte_offset=row.byte_offset + index + 1)
        for row in seed.observations
    )
    return replace(
        seed,
        nonce=nonce,
        measurement_protocol_version=protocol,
        observations=observations,
        disposition=disposition,
        lineage_order_key=ts,
        fresh_ts=ts,
        session_id=f"s-{nonce}",
        outcome=outcome,
        loss_reasons=(
            ("corpus-derived-loss",) if disposition == "loss_signal" else ()
        ),
        conflict_reasons=(
            ("corpus-derived-conflict",) if disposition == "conflict" else ()
        ),
    )


@lru_cache(maxsize=1)
def _receipt_seed():
    return _state(_pair("corpus-receipt-seed", 0)).receipts[0]


def _state_from_receipts(receipts, *, markers=(), gate_rows=()):
    health, completeness = _accounting()
    return om.AuditState(
        config=_config(),
        receipts=tuple(receipts),
        loss_markers=tuple(markers),
        source_health=health,
        snapshots=(),
        gate_rows=tuple(gate_rows),
        candidate_completeness=completeness,
        measurement_taken_at=T0 + timedelta(days=2),
    )


def _current_measurement_bytes(
    nonce="corpus-mutated-current",
    *,
    minute=10,
    host_minute=None,
    project_proof=None,
    host_project=PROJECT,
):
    """Mutate real captured shapes into a minimal current-protocol pair."""

    ts = (T0 + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
    host_ts = (
        T0 + timedelta(minutes=minute if host_minute is None else host_minute)
    ).isoformat().replace("+00:00", "Z")
    session = "sanitized-current-session"
    common = {
        "gate_call_id": nonce,
        "session_id": session,
        "attestation": RUNTIME,
        "measurement_protocol_version": om.MEASUREMENT_PROTOCOL_VERSION,
        "project_proof": _proof() if project_proof is None else project_proof,
        "key_epoch": EPOCH,
        "runtime_version": RUNTIME,
        "host_adapter": "codex",
        "skipped": False,
    }
    gate = json.loads(
        (FIXTURES / "gate-2026-08-03.sanitized.jsonl").read_text()
    )
    gate.update(common)
    gate.update(
        {
            "ts": ts,
            "recommendation": "PROCEED",
            **_ids(),
        }
    )

    host = [
        json.loads(line)
        for line in (
            FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl"
        ).read_text().splitlines()
    ]
    host[0]["timestamp"] = host_ts
    host[0]["payload"].update(
        {
            "session_id": session,
            "id": session,
            "timestamp": host_ts,
            "cwd": host_project,
        }
    )
    host[1]["timestamp"] = host_ts
    host[2]["timestamp"] = host_ts
    host_result = {
        **common,
        "verdict": {"recommendation": "PROCEED", **_ids(), "skipped": False},
    }
    host[2]["payload"]["output"] = json.dumps(host_result, sort_keys=True)
    return (
        (json.dumps(gate, separators=(",", ":")) + "\n").encode(),
        ("\n".join(json.dumps(row, separators=(",", ":")) for row in host) + "\n").encode(),
    )


def _compact_gate_result(nonce="compact-current"):
    return {
        "gate_call_id": nonce,
        "session_id": "compact-session",
        "attestation": RUNTIME,
        "measurement_protocol_version": om.MEASUREMENT_PROTOCOL_VERSION,
        "project_proof": _proof(),
        "key_epoch": EPOCH,
        "runtime_version": RUNTIME,
        "host_adapter": "codex",
        "gate_status": "OK",
        "verdict": {
            "recommendation": "PROCEED",
            "decision_chain": [4164],
            "abandoned_paths": [4066],
            "active_constraints": [4113, 4137],
            "current_direction": [4179],
            "skipped": False,
        },
        "evidence": [{"id": 4175}, {"id": 4164}],
        "chain_summary": {"seed_ids": [4113, 4137]},
    }


def _wrapped_codex_host_bytes(
    result,
    *,
    use_exec=True,
    script=None,
):
    """Mutate the corpus-derived Codex outer envelope, preserving encoding."""

    records = [
        json.loads(line)
        for line in (
            FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl"
        ).read_text().splitlines()
    ]
    ts = (T0 + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    records[0]["timestamp"] = ts
    records[0]["payload"].update(
        {
            "id": "compact-session",
            "session_id": "compact-session",
            "timestamp": ts,
            "cwd": PROJECT,
        }
    )
    call_id = "call_compact_current"
    if use_exec:
        records[1]["payload"] = {
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": call_id,
            "input": script
            or (
                "const r = await tools.mcp__latch__latch_gate("
                '{request: "SANITIZED"});\ntext(r);'
            ),
        }
        output_type = "custom_tool_call_output"
    else:
        records[1]["payload"] = {
            "type": "function_call",
            "name": "latch_gate",
            "call_id": call_id,
            "arguments": '{"request":"SANITIZED","verbose":false}',
        }
        output_type = "function_call_output"
    wrapped = [
        {
            "type": "text",
            "text": json.dumps(result, sort_keys=True, separators=(",", ":")),
        }
    ]
    records[2]["payload"] = {
        "type": output_type,
        "call_id": call_id,
        "output": (
            "Wall time: 0.1000 seconds\nOutput:\n"
            + json.dumps(wrapped, separators=(",", ":"))
        ),
    }
    records[1]["timestamp"] = ts
    records[2]["timestamp"] = ts
    return (
        "\n".join(
            json.dumps(record, separators=(",", ":")) for record in records
        )
        + "\n"
    ).encode()


def _measure_current(
    *,
    evidence_resolver=None,
    enumerate_files=None,
    minute=10,
    host_minute=None,
    project_proof=None,
    host_project=PROJECT,
    measurement_now=None,
    cap_reached: bool = False,
):
    gate_path = FIXTURES / "gate-2026-08-03.sanitized.jsonl"
    host_path = FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl"
    roots = {
        om.SOURCE_GATE: (str(gate_path),),
        om.SOURCE_HOST: (str(host_path),),
    }
    gate_bytes, host_bytes = _current_measurement_bytes(
        minute=minute,
        host_minute=host_minute,
        project_proof=project_proof,
        host_project=host_project,
    )
    content = {gate_path: gate_bytes, host_path: host_bytes}
    return om.measure(
        roots,
        _config(roots=roots, cap_reached=cap_reached),
        project_proof_context=_proof_context(),
        now=measurement_now or T0 + timedelta(days=2),
        read_bytes=lambda path: content[path],
        enumerate_files=(
            enumerate_files
            if enumerate_files is not None
            else lambda items: tuple(Path(item) for item in items)
        ),
        evidence_resolver=evidence_resolver,
    )


def test_b1_corpus_derived_end_to_end_report_is_byte_stable_and_redacted():
    def resolver(receipts, stable_bytes, _config):
        assert [row.nonce for row in receipts] == ["corpus-mutated-current"]
        assert all(stable_bytes[source] for source in om.SOURCES)
        return (
            om.OutcomeEvidence(
                nonce="corpus-mutated-current",
                session_id="sanitized-current-session",
                observable=True,
                evidence_available=True,
                adapter="codex",
                project_fingerprint=_proof()["fingerprint"],
                progress_inserts=1,
            ),
        )

    # Both current records are mutations of parsed real S1/S2 byte shapes and
    # traverse snapshot -> segmented parse -> preliminary fold -> evidence fold.
    state = _measure_current(evidence_resolver=resolver)
    assert len(state.receipts) == 1
    rendered = om.render_report(state, om.compute_oracles(state))
    assert rendered == (FIXTURES / "golden-report-v2.6.json").read_text()
    assert rendered == om.render_report(state, om.compute_oracles(state))
    assert str(FIXTURES) not in rendered
    assert "sanitized-current-session" not in rendered
    assert PROJECT not in rendered
    for row in state.candidate_completeness:
        for _file, digest in row.stable_file_hashes:
            assert digest not in rendered


def test_fixture_provenance_manifest_pins_every_fixture_byte():
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert set(manifest["fixtures"]) == {
        "gate-2026-08-03.sanitized.jsonl",
        "codex-rollout-2026-07-29.sanitized.jsonl",
        "claude-transcript-2026-07-22.sanitized.jsonl",
        "golden-report-v2.6.json",
    }
    for name, metadata in manifest["fixtures"].items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == metadata[
            "sha256"
        ]
        assert metadata["source_category"]
        assert metadata["source_date"]


def test_b2_per_host_nonce_canary_evidence_path_is_validated_without_live_calls():
    config = _config()
    rows = [
        om.CanaryEvidence(
            host=host,
            nonce=f"nonce-{host}",
            tool_result_seen=True,
            gate_log_seen=True,
            host_record_seen=True,
            dual_source_joined=True,
            runtime_version=RUNTIME,
            measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
            key_epoch=EPOCH,
            project_proof=_proof(),
        )
        for host in ("codex", "claude code")
    ]
    assert om.verify_canary_evidence(rows, config) == ()
    assert om.verify_canary_evidence(rows[:1], config) == ("missing_canary:claude",)
    assert "duplicate_canary:codex" in om.verify_canary_evidence(
        rows + [rows[0]], config
    )
    shared_nonce = [rows[0], replace(rows[1], nonce=rows[0].nonce)]
    assert "canary_nonce_not_host_unique" in om.verify_canary_evidence(
        shared_nonce, config
    )


def test_b3_reconciliation_deletion_malformed_and_conflict():
    host_only = _state([_obs("deleted-gate", om.SOURCE_HOST, 1)])
    assert host_only.receipts[0].disposition == "loss_signal"
    assert host_only.receipts[0].loss_reasons == ("host_only",)

    observations, markers, _ = om.parse_gate_log_bytes(
        b'{"ts":"2026-08-01T00:01:00Z"}\nnot-json\n',
        file="gate.jsonl",
        config=_config(),
    )
    assert observations and [row.reason for row in markers] == ["schema_invalid"]

    conflict_rows = _pair("conflict", 2)
    conflict_rows.append(_obs("conflict", om.SOURCE_HOST, 2, session="other"))
    conflict = _state(conflict_rows)
    assert conflict.receipts[0].disposition == "conflict"


def test_b3_defects_force_threshold_safe_oracle_indeterminate_with_counts():
    clean = [_receipt(f"b3-clean-{index}", index) for index in range(30)]
    host_only = replace(
        _receipt("b3-host-only", 0, disposition="loss_signal", outcome="CENSORED"),
        loss_reasons=("host_only",),
        censored_reason="instrument_unavailable",
        lineage_order_key=T0 + timedelta(seconds=30),
        fresh_ts=T0 + timedelta(seconds=30),
    )
    loss_result = om.compute_oracles(
        _state_from_receipts([*clean, host_only])
    )
    assert loss_result.d_min == 31 and loss_result.eligible_n == 30
    assert loss_result.o2 == "indeterminate"
    assert loss_result.disposition_counts["loss_signal"] == 1

    marker_result = om.compute_oracles(
        _state_from_receipts(
            clean,
            markers=(
                om.LossMarker(
                    "schema_invalid",
                    source=om.SOURCE_GATE,
                    ts=T0 + timedelta(minutes=10),
                    in_scope=True,
                ),
            ),
        )
    )
    assert marker_result.marker_count == 1
    assert marker_result.o2 == "indeterminate"

    conflict = replace(
        _receipt("b3-conflict", 0, disposition="conflict", outcome="CENSORED"),
        conflict_reasons=("non_identical_duplicate:S2",),
        censored_reason="instrument_unavailable",
        lineage_order_key=T0 + timedelta(seconds=45),
        fresh_ts=T0 + timedelta(seconds=45),
    )
    conflict_result = om.compute_oracles(
        _state_from_receipts([*clean, conflict])
    )
    assert conflict_result.d_min == 31 and conflict_result.eligible_n == 30
    assert conflict_result.o2 == "indeterminate"
    assert conflict_result.disposition_counts["conflict"] == 1


def test_b4_foreign_non_pinned_and_missing_attestation_are_separate():
    foreign_proof = _proof("/different/project")
    foreign = _state(_pair("foreign", 1, proof=foreign_proof))
    pilot = _state(_pair("pilot", 2, runtime="other-runtime"))
    missing = _state(_pair("missing", 3, attestation=None))
    assert foreign.receipts[0].disposition == "foreign_project"
    assert pilot.receipts[0].disposition == "pilot"
    assert missing.receipts[0].disposition == "loss_signal"
    assert "attestation_missing" in missing.receipts[0].loss_reasons


def test_b5_freshness_uses_current_timestamp_and_rejects_cached_snapshot():
    config = _config(fresh=True)
    rows = _pair("fresh", 10)
    fresh_ts = T0 + timedelta(minutes=10)
    cached = [
        om.SnapshotReceipt(
            row.source,
            row.file,
            "a" * 64,
            "a" * 64,
            fresh_ts + timedelta(seconds=om.FRESHNESS_SECONDS - 1),
        )
        for row in rows
    ]
    assert not _state(rows, config=config, snapshots=cached).receipts[0].finalized
    qualified = [
        replace(row, snapshot_taken=fresh_ts + timedelta(seconds=om.FRESHNESS_SECONDS))
        for row in cached
    ]
    assert _state(rows, config=config, snapshots=qualified).receipts[0].finalized


def test_b6_skipped_same_session_bounds_without_emitting_and_foreign_never_bounds():
    rows = _pair("first", 0, session="same")
    rows += _pair("skipped", 5, session="same", skipped=True)
    rows += _pair("foreign", 3, session="same", proof=_proof("/foreign"))
    state = _state(rows)
    by_nonce = {row.nonce: row for row in state.receipts}
    assert by_nonce["skipped"].disposition == "skipped"
    assert by_nonce["skipped"].outcome is None
    assert by_nonce["first"].window_end == T0 + timedelta(minutes=5)


def test_b6_host_unknown_does_not_conflict_with_gate_skipped():
    rows = _pair("skipped", 5, skipped=True)
    assert rows[1].skipped is None
    receipt = _state(rows).receipts[0]
    assert receipt.disposition == "skipped"
    assert receipt.outcome is None


def test_boundary_requires_exact_adapter_project_proof_and_session_tuple():
    def adapter_pair(nonce, minute, adapter, *, proof=None):
        return [
            replace(
                row,
                # Reproduce real S1+S2 pairs: both hosts may use the same
                # classifier backend, while S2 carries the true host adapter.
                adapter=("shared-classifier" if row.source == om.SOURCE_GATE else adapter),
            )
            for row in _pair(
                nonce,
                minute,
                session="colliding-session",
                proof=proof,
            )
        ]

    rows = adapter_pair("measured", 0, "codex")
    rows += adapter_pair(
        "foreign-project-collision",
        3,
        "codex",
        proof=_proof("/foreign-project"),
    )
    rows += adapter_pair("cross-adapter-collision", 5, "claude")
    rows += adapter_pair("exact-boundary", 7, "codex")
    state = _state(rows)
    by_nonce = {row.nonce: row for row in state.receipts}
    assert by_nonce["foreign-project-collision"].disposition == "foreign_project"
    assert by_nonce["measured"].window_end == T0 + timedelta(minutes=7)


def test_conflicted_boundary_uses_matching_session_coordinate_not_receipt_minimum():
    rows = _pair("measured-coordinate", 0, session="session-a")
    rows.extend(
        (
            _obs(
                "conflicted-boundary",
                om.SOURCE_HOST,
                2,
                session="session-b",
            ),
            _obs(
                "conflicted-boundary",
                om.SOURCE_HOST,
                10,
                session="session-a",
            ),
        )
    )
    state = _state(rows)
    by_nonce = {row.nonce: row for row in state.receipts}
    assert by_nonce["conflicted-boundary"].disposition == "conflict"
    assert by_nonce["conflicted-boundary"].fresh_ts == T0 + timedelta(minutes=2)
    assert by_nonce["measured-coordinate"].window_end == T0 + timedelta(minutes=10)


@pytest.mark.parametrize("mixed_host", [False, True])
def test_conflict_precedence_does_not_hide_exact_gate_only_boundary_loss(mixed_host):
    rows = _pair("measured-gate-boundary", 0, session="session-a")
    rows.append(
        _obs("boundary-conflict", om.SOURCE_GATE, 5, session="session-a")
    )
    rows.append(
        _obs(
            "boundary-conflict",
            om.SOURCE_HOST if mixed_host else om.SOURCE_GATE,
            3 if mixed_host else 6,
            session="session-b",
        )
    )
    state = _state(rows)
    by_nonce = {row.nonce: row for row in state.receipts}
    assert by_nonce["boundary-conflict"].disposition == "conflict"
    measured = by_nonce["measured-gate-boundary"]
    assert measured.window_end == T0 + timedelta(minutes=5)
    assert measured.outcome == "CENSORED"
    assert measured.censored_reason == "boundary_uncertain"


def test_boundary_marker_requires_exact_adapter_project_and_session_scope():
    target_rows = [
        replace(
            row,
            adapter=("shared-classifier" if row.source == om.SOURCE_GATE else "codex"),
        )
        for row in _pair("marker-target", 0, session="marker-session")
    ]
    foreign_marker = om.LossMarker(
        "schema_invalid",
        source=om.SOURCE_HOST,
        ts=T0 + timedelta(minutes=5),
        session_id="marker-session",
        adapter="claude",
        project_proof=_proof("/foreign-project"),
        in_scope=True,
    )
    foreign_state = _state(target_rows, markers=(foreign_marker,))
    assert foreign_state.receipts[0].outcome == "ACCEPTED"
    assert foreign_state.receipts[0].censored_reason is None

    exact_marker = replace(
        foreign_marker,
        adapter="codex",
        project_proof=_proof(),
    )
    exact_state = _state(target_rows, markers=(exact_marker,))
    assert exact_state.receipts[0].outcome == "CENSORED"
    assert exact_state.receipts[0].censored_reason == "boundary_uncertain"

    at_window_start = replace(exact_marker, ts=T0, byte_offset=999)
    start_state = _state(target_rows, markers=(at_window_start,))
    assert start_state.receipts[0].outcome == "CENSORED"
    assert start_state.receipts[0].censored_reason == "boundary_uncertain"


def test_b7_censored_rows_never_improve_clean_quality_rate():
    receipts = [
        _receipt(f"clean-{i}", i, outcome="AMBIGUOUS" if i < 5 else "ACCEPTED")
        for i in range(20)
    ]
    receipts += [
        _receipt(f"censored-{i}", 20 + i, outcome="CENSORED") for i in range(10)
    ]
    result = om.compute_oracles(_state_from_receipts(receipts))
    assert result.eligible_n == 20
    assert result.clean_label_counts == {"ACCEPTED": 15, "AMBIGUOUS": 5}
    assert result.ambiguous_rate == 25.0
    assert result.raw_label_counts["CENSORED"] == 10


def test_b8_d_min_three_valued_arithmetic_o1_and_o3_edges():
    clean = [_receipt(f"e-{i}", i) for i in range(30)]
    losses = [
        _receipt(f"loss-{i}", 30 + i, disposition="loss_signal", outcome="CENSORED")
        for i in range(7)
    ]
    false_fail = om.compute_oracles(
        _state_from_receipts(
            clean + losses,
            markers=(om.LossMarker("unjoinable"),),
        )
    )
    assert (false_fail.eligible_n, false_fail.d_min, false_fail.o2) == (
        30,
        37,
        "indeterminate",
    )

    d42 = clean + [
        _receipt(f"p-{i}", 40 + i, disposition="pilot") for i in range(12)
    ]
    d43 = d42 + [_receipt("p-12", 52, disposition="pilot")]
    assert om.compute_oracles(_state_from_receipts(d42)).o2 == "pass"
    assert om.compute_oracles(_state_from_receipts(d43)).o2 == "fail"

    six = [
        _receipt(f"o3-{i}", i, outcome="AMBIGUOUS" if i < 6 else "ACCEPTED")
        for i in range(30)
    ]
    seven = [replace(row, outcome="AMBIGUOUS") if i == 6 else row for i, row in enumerate(six)]
    assert om.compute_oracles(_state_from_receipts(six)).o3_pass is True
    assert om.compute_oracles(_state_from_receipts(seven)).o3_pass is False

    bad_o1 = om.GateRowCheck((om.SOURCE_GATE, "foreign", 0), T0, True, False)
    assert om.compute_oracles(_state_from_receipts(clean, gate_rows=(bad_o1,))).o1_pass is False


def test_b9_key_rotation_is_loss_never_foreign_or_conflict():
    rotated = _proof(epoch="epoch-rotated")
    state = _state(
        _pair("rotated", 1, proof=rotated, epoch="epoch-rotated")
    )
    receipt = state.receipts[0]
    assert receipt.disposition == "loss_signal"
    assert "key_epoch_mismatch" in receipt.loss_reasons


def test_b9_same_epoch_different_vault_key_is_loss_not_conflict():
    rotated_context = om.ProjectProofContext.from_vault_key(
        bytes.fromhex("55" * 32),
        key_epoch=EPOCH,
        vault_id="rotated-vault",
    )
    rows = [
        _obs("same-epoch-rotation", om.SOURCE_GATE, 1),
        _obs(
            "same-epoch-rotation",
            om.SOURCE_HOST,
            1,
            proof=rotated_context.prove(PROJECT),
        ),
    ]
    receipt = _state(rows).receipts[0]
    assert receipt.disposition == "loss_signal"
    assert receipt.conflict_reasons == ()
    assert "key_epoch_mismatch" in receipt.loss_reasons
    assert not receipt.conflict_reasons


def test_b9_rotated_same_scope_s2_invocation_still_bounds_prior_window():
    """Result proof rotation is loss, but S2 call cwd still proves scope."""

    rotated_result = _compact_gate_result("rotated-boundary")
    rotated_result["project_proof"] = _proof(epoch="epoch-rotated")
    rotated_result["key_epoch"] = "epoch-rotated"
    rotated_rows, markers = om.parse_host_record_bytes(
        _wrapped_codex_host_bytes(rotated_result),
        file="rollout-rotated-boundary.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not markers
    assert len(rotated_rows) == 1
    assert rotated_rows[0].project_proof == rotated_result["project_proof"]
    assert rotated_rows[0].host_scope_project_proof == _proof()

    first = _pair("before-rotation", 0, session="compact-session")
    state = _state(first + rotated_rows)
    by_nonce = {row.nonce: row for row in state.receipts}
    rotated = by_nonce["rotated-boundary"]
    assert rotated.disposition == "loss_signal"
    assert "key_epoch_mismatch" in rotated.loss_reasons
    assert not rotated.conflict_reasons
    assert by_nonce["before-rotation"].window_end == T0 + timedelta(minutes=10)
    assert by_nonce["before-rotation"].outcome == "ACCEPTED"
    assert by_nonce["before-rotation"].censored_reason is None


def test_s2_declared_project_proof_cannot_override_foreign_call_scope():
    result = _compact_gate_result("scope-mismatch")
    records = [
        json.loads(line)
        for line in _wrapped_codex_host_bytes(result).splitlines()
    ]
    records[0]["payload"]["cwd"] = "/foreign/project"
    data = (
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records)
        + "\n"
    ).encode()
    host_rows, markers = om.parse_host_record_bytes(
        data,
        file="rollout-scope-mismatch.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not markers
    assert len(host_rows) == 1
    assert host_rows[0].project_proof == _proof()
    assert host_rows[0].host_scope_project_proof == _proof("/foreign/project")
    assert "project_scope_mismatch" in host_rows[0].embedded_conflict_reasons

    gate = _obs(
        "scope-mismatch",
        om.SOURCE_GATE,
        10,
        session="compact-session",
    )
    receipt = _state([gate, *host_rows]).receipts[0]
    assert receipt.disposition == "conflict"
    assert "project_scope_mismatch" in receipt.conflict_reasons


def test_s2_declared_foreign_proof_cannot_override_target_call_scope():
    result = _compact_gate_result("inverse-scope-mismatch")
    result["project_proof"] = _proof("/foreign/project")
    rows, markers = om.parse_host_record_bytes(
        _wrapped_codex_host_bytes(result),
        file="rollout-inverse-scope-mismatch.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not markers and len(rows) == 1
    assert rows[0].project_proof == _proof("/foreign/project")
    assert rows[0].host_scope_project_proof == _proof()
    receipt = _state(rows).receipts[0]
    assert receipt.disposition == "conflict"
    assert "project_scope_mismatch" in receipt.conflict_reasons


def test_explicitly_incomplete_s2_project_proof_is_named_loss_not_cwd_repaired():
    result = _compact_gate_result("malformed-proof")
    result["project_proof"] = {
        "version": om.PROJECT_PROOF_VERSION,
        "key_epoch": EPOCH,
    }
    rows, markers = om.parse_host_record_bytes(
        _wrapped_codex_host_bytes(result),
        file="rollout-malformed-proof.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not markers
    assert len(rows) == 1
    assert rows[0].project_proof is None
    assert rows[0].host_scope_project_proof == _proof()
    receipt = _state(rows).receipts[0]
    assert receipt.disposition == "loss_signal"
    assert "project_proof_missing" in receipt.loss_reasons


@pytest.mark.parametrize("boundary", ["t0", "t1"])
@pytest.mark.parametrize("earliest_source", [om.SOURCE_GATE, om.SOURCE_HOST])
@pytest.mark.parametrize("deleted_source", [om.SOURCE_GATE, om.SOURCE_HOST])
def test_b10_source_deletion_straddles_preserve_monotone_lineage(
    boundary, earliest_source, deleted_source
):
    """Exercise both source directions and deletion sides at T0 and T1."""

    later_source = (
        om.SOURCE_HOST
        if earliest_source == om.SOURCE_GATE
        else om.SOURCE_GATE
    )
    if boundary == "t0":
        early_ts = T0 - timedelta(seconds=1)
        late_ts = T0 + timedelta(seconds=1)
        prefix_rows = []
        tail_rows = []
    else:
        # The target is initially the 30th eligible invocation. A tail row
        # lies between its source timestamps: deleting the earlier source
        # would move the target past T1 unless the inherited order key wins.
        t1_ts = T0 + timedelta(minutes=30)
        early_ts = t1_ts - timedelta(seconds=1)
        late_ts = t1_ts + timedelta(seconds=1)
        prefix_rows = [
            row
            for index in range(29)
            for row in _pair(
                f"prefix-{index}",
                index,
                session=f"prefix-session-{index}",
            )
        ]
        tail_rows = _pair(
            "t1-tail",
            30,
            session="tail-session",
            ts=t1_ts,
        )

    target_rows = [
        _obs(
            "lineage-straddle",
            earliest_source,
            1000,
            session="target-session",
            ts=early_ts,
        ),
        _obs(
            "lineage-straddle",
            later_source,
            1000,
            session="target-session",
            ts=late_ts,
        ),
    ]
    original_state = _state(prefix_rows + target_rows + tail_rows)
    original = next(
        row for row in original_state.receipts if row.nonce == "lineage-straddle"
    )
    original_order = [
        row.nonce
        for row in sorted(
            (row for row in original_state.receipts if row.admitted),
            key=lambda row: (row.lineage_order_key, row.nonce),
        )
    ]
    assert original.admitted is True
    assert original.lineage_order_key == early_ts
    assert original.prefix_member is True
    if boundary == "t1":
        assert original_order.index("lineage-straddle") == 29
        assert next(
            row for row in original_state.receipts if row.nonce == "t1-tail"
        ).prefix_member is False

    next_protocol = "outcome-v2.6.1"
    surviving_source = (
        om.SOURCE_HOST
        if deleted_source == om.SOURCE_GATE
        else om.SOURCE_GATE
    )
    surviving_ts = early_ts if surviving_source == earliest_source else late_ts
    current_rows = [
        _obs(
            "lineage-straddle",
            surviving_source,
            1000,
            protocol=next_protocol,
            session="target-session",
            ts=surviving_ts,
        )
    ]
    if boundary == "t1":
        current_rows.extend(
            row
            for index in range(29)
            for row in _pair(
                f"prefix-{index}",
                index,
                protocol=next_protocol,
                session=f"prefix-session-{index}",
            )
        )
        current_rows.extend(
            _pair(
                "t1-tail",
                30,
                protocol=next_protocol,
                session="tail-session",
                ts=T0 + timedelta(minutes=30),
            )
        )

    state = _state(
        current_rows,
        config=_config(protocol=next_protocol),
        prior=original_state.receipts,
    )
    receipt = next(
        row
        for row in state.receipts
        if row.identity == ("lineage-straddle", next_protocol)
    )
    current_order = [
        row.nonce
        for row in sorted(
            (
                row
                for row in state.receipts
                if row.admitted
                and row.measurement_protocol_version == next_protocol
            ),
            key=lambda row: (row.lineage_order_key, row.nonce),
        )
    ]
    assert receipt.admitted is True
    assert receipt.lineage_order_key == original.lineage_order_key == early_ts
    assert current_order.index("lineage-straddle") == original_order.index(
        "lineage-straddle"
    )
    assert receipt.prefix_member is True
    assert receipt.disposition == "loss_signal"
    assert receipt.loss_reasons == (
        "gate_only" if surviving_source == om.SOURCE_GATE else "host_only",
    )
    assert {row.source for row in receipt.observations} == {surviving_source}
    assert receipt.fresh_ts == surviving_ts
    if deleted_source == earliest_source:
        assert receipt.fresh_ts > receipt.lineage_order_key
    else:
        assert receipt.fresh_ts == receipt.lineage_order_key


def test_m5_protocol_bump_preserves_admitted_lineage_after_total_source_loss():
    original = _state(_pair("lost-all", 1)).receipts[0]
    assert original.finalized and original.admitted and original.prefix_member
    next_protocol = "outcome-v2.6.1"
    state = _state(
        [],
        config=_config(protocol=next_protocol),
        prior=(original,),
    )
    assert len(state.receipts) == 1
    receipt = state.receipts[0]
    assert receipt.identity == ("lost-all", next_protocol)
    assert receipt.admitted and receipt.finalized and receipt.prefix_member
    assert receipt.lineage_order_key == original.lineage_order_key
    assert receipt.fresh_ts is None
    assert receipt.window_start is receipt.window_end is None
    assert receipt.disposition == "loss_signal"
    assert receipt.loss_reasons == ("gate_only", "host_only")
    assert receipt.outcome == "CENSORED"
    assert receipt.censored_reason == "instrument_unavailable"
    deletion_markers = [
        marker
        for marker in state.loss_markers
        if marker.reason == "finalized_source_deleted"
    ]
    assert {marker.source for marker in deletion_markers} == set(om.SOURCES)
    result = om.compute_oracles(state)
    assert result.d_min == 1
    # With no eligible numerator left, the frozen D_min arithmetic reaches
    # definitive fail before marker-driven indeterminate.
    assert result.o2 == "fail"


def test_m5_total_loss_preserves_prior_exact_boundary_coordinate():
    original = _state(
        _pair("lost-boundary", 10, session="boundary-session")
    ).receipts[0]
    next_protocol = "outcome-v2.6.1"
    current = _pair(
        "earlier-current",
        0,
        session="boundary-session",
        protocol=next_protocol,
    )
    state = _state(
        current,
        config=_config(protocol=next_protocol),
        prior=(original,),
    )
    by_nonce = {row.nonce: row for row in state.receipts}
    assert by_nonce["lost-boundary"].boundary_evidence
    assert by_nonce["earlier-current"].window_end == T0 + timedelta(minutes=10)


def test_m5_bare_admitted_lineage_cannot_silently_eject_invocation():
    lineage = om.ReceiptLineage(
        nonce="bare-lineage",
        admitted=True,
        lineage_order_key=T0 + timedelta(minutes=3),
        measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
    )
    state = _state(
        [],
        config=_config(protocol="outcome-v2.6.1"),
        prior=(lineage,),
    )
    assert "admitted_lineage_authority_missing" in state.hard_invalidations
    assert om.compute_oracles(state).invalidated is True


def test_immutable_foreign_receipt_cannot_hide_later_target_same_nonce():
    foreign = _state(
        _pair("foreign-replay", 1, proof=_proof("/foreign"))
    ).receipts[0]
    state = _state(_pair("foreign-replay", 2), prior=(foreign,))
    assert state.receipts[0].disposition == "foreign_project"
    marker = next(
        row
        for row in state.loss_markers
        if row.reason == "finalized_candidate_conflict"
    )
    assert marker.ts == T0 + timedelta(minutes=2)
    result = om.compute_oracles(state)
    assert result.marker_count == 1
    assert result.o2 == "indeterminate"


def test_immutable_all_foreign_candidates_stay_out_of_target_accounting():
    foreign = _state(
        _pair("foreign-only-replay", 1, proof=_proof("/foreign-a"))
    ).receipts[0]
    state = _state(
        _pair("foreign-only-replay", 2, proof=_proof("/foreign-b")),
        prior=(foreign,),
    )
    assert state.receipts[0].disposition == "foreign_project"
    assert not any(
        row.reason == "finalized_candidate_conflict"
        for row in state.loss_markers
    )
    assert om.compute_oracles(state).o2 == "pass"


@pytest.mark.parametrize("prior_foreign", [False, True])
def test_protocol_bump_reconciles_same_nonce_project_scope_both_directions(
    prior_foreign,
):
    old_proof = _proof("/foreign") if prior_foreign else _proof()
    current_proof = _proof() if prior_foreign else _proof("/foreign")
    original = _state(
        _pair("cross-generation-scope", 1, proof=old_proof)
    ).receipts[0]
    next_protocol = "outcome-v2.6.1"
    state = _state(
        _pair(
            "cross-generation-scope",
            2,
            proof=current_proof,
            protocol=next_protocol,
        ),
        config=_config(protocol=next_protocol),
        prior=(original,),
    )
    receipt = next(
        row
        for row in state.receipts
        if row.measurement_protocol_version == next_protocol
    )
    assert receipt.admitted is True
    assert receipt.lineage_order_key == original.lineage_order_key
    assert receipt.disposition == "conflict"
    assert "cross_generation_project_scope_mismatch" in receipt.conflict_reasons
    assert receipt.in_d_min is True


def test_t1_prefix_is_recomputed_for_same_generation_immutable_receipts():
    late = [
        _receipt(f"late-pilot-{index}", 100 + index, disposition="pilot")
        for index in range(13)
    ]
    assert all(row.finalized and row.prefix_member for row in late)
    current_rows = [
        observation
        for index in range(30)
        for observation in _pair(f"early-{index}", index)
    ]
    state = _state(current_rows, prior=late)
    by_nonce = {row.nonce: row for row in state.receipts}
    assert all(by_nonce[f"early-{index}"].prefix_member for index in range(30))
    assert all(
        not by_nonce[f"late-pilot-{index}"].prefix_member
        for index in range(13)
    )
    assert len(
        [
            marker
            for marker in state.loss_markers
            if marker.reason == "finalized_source_deleted"
        ]
    ) == 26
    result = om.compute_oracles(state)
    assert (result.eligible_n, result.d_min, result.o2) == (30, 30, "pass")


def test_b12_foreign_finalized_source_loss_never_poison_target_accounting():
    foreign = _state(
        _pair("foreign-deleted", 1, proof=_proof("/foreign"))
    ).receipts[0]
    assert foreign.disposition == "foreign_project"

    same_generation = _state([], prior=(foreign,))
    assert same_generation.receipts == (foreign,)
    assert not same_generation.loss_markers
    assert om.compute_oracles(same_generation).o2 == "pass"

    bumped = _state(
        [],
        config=_config(protocol="outcome-v2.6.1"),
        prior=(foreign,),
    )
    assert len(bumped.receipts) == 1
    assert bumped.receipts[0].disposition == "foreign_project"
    assert not bumped.loss_markers
    assert om.compute_oracles(bumped).o2 == "pass"


def test_b12_foreign_scoped_parser_loss_is_diagnostic_not_target_accounting(
    tmp_path,
):
    gate_root = tmp_path / "gate"
    host_root = tmp_path / "host"
    gate_root.mkdir()
    host_root.mkdir()
    gate_bytes, target_host_bytes = _current_measurement_bytes()
    (gate_root / "gate-2026-08-01.log").write_bytes(gate_bytes)
    (host_root / "rollout-target.jsonl").write_bytes(target_host_bytes)

    foreign_result = _compact_gate_result("foreign-invalid")
    foreign_result["skipped"] = "false"
    foreign_records = [
        json.loads(line)
        for line in _wrapped_codex_host_bytes(foreign_result).splitlines()
    ]
    foreign_records[0]["payload"].update(
        {
            "id": "foreign-session",
            "session_id": "foreign-session",
            "cwd": "/foreign/project",
        }
    )
    foreign_bytes = (
        "\n".join(
            json.dumps(record, separators=(",", ":"))
            for record in foreign_records
        )
        + "\n"
    ).encode()
    (host_root / "rollout-foreign.jsonl").write_bytes(foreign_bytes)
    roots = {
        om.SOURCE_GATE: (str(gate_root),),
        om.SOURCE_HOST: (str(host_root),),
    }
    state = om.measure(
        roots,
        _config(roots=roots),
        project_proof_context=_proof_context(),
        now=T0 + timedelta(days=2),
        evidence_resolver=lambda _receipts, _stable_bytes, _config: (
            om.OutcomeEvidence(
                nonce="corpus-mutated-current",
                session_id="sanitized-current-session",
                observable=True,
                evidence_available=True,
                adapter="codex",
                project_fingerprint=_proof()["fingerprint"],
                progress_inserts=1,
            ),
        ),
    )
    foreign_markers = [
        marker
        for marker in state.loss_markers
        if marker.reason == "schema_invalid"
        and om._marker_is_proven_foreign(marker, state.config)
    ]
    assert len(foreign_markers) == 1
    assert all(row.malformed_regions == 0 for row in state.source_health)
    assert all(row.complete for row in state.candidate_completeness)
    result = om.compute_oracles(state)
    assert result.marker_count == 0
    assert result.o2 == "pass"


def test_b12_identity_missing_marker_preserves_foreign_host_scope():
    row = replace(
        _obs("will-be-removed", om.SOURCE_HOST, 1),
        nonce=None,
        host_scope_project_proof=_proof("/foreign/project"),
    )
    state = _state([row])
    marker = next(
        item for item in state.loss_markers if item.reason == "identity_missing"
    )
    assert marker.host_scope_project_proof == _proof("/foreign/project")
    assert om._marker_is_proven_foreign(marker, state.config)
    result = om.compute_oracles(state)
    assert result.marker_count == 0
    assert result.o2 == "pass"


def test_b11_gate_only_exact_session_censors_affected_window_only():
    rows = _pair("measured", 0, session="target")
    rows.append(
        _obs(
            "gate-only",
            om.SOURCE_GATE,
            6,
            session="target",
            ts=T0 + timedelta(seconds=391),
        )
    )
    rows += _pair("host-clean", 1, session="host-session")
    rows.append(_obs("unrelated-loss", om.SOURCE_GATE, 2, session="elsewhere"))
    state = _state(rows)
    by_nonce = {row.nonce: row for row in state.receipts}
    assert by_nonce["measured"].outcome == "CENSORED"
    assert by_nonce["measured"].censored_reason == "boundary_uncertain"
    assert by_nonce["measured"].window_end == T0 + timedelta(seconds=391)
    assert by_nonce["host-clean"].censored_reason is None


def test_b11_host_only_exact_session_bounds_without_censoring_prior_window():
    rows = _pair("measured", 0, session="target")
    rows.append(_obs("host-only", om.SOURCE_HOST, 6, session="target"))
    state = _state(rows)
    by_nonce = {row.nonce: row for row in state.receipts}
    assert by_nonce["host-only"].loss_reasons == ("host_only",)
    assert by_nonce["measured"].window_end == T0 + timedelta(minutes=6)
    assert by_nonce["measured"].outcome == "ACCEPTED"
    assert by_nonce["measured"].censored_reason is None


def test_b12_proven_foreign_exits_before_o2_and_m1_accounting():
    base = _state_from_receipts([_receipt("target", 0)])
    foreign = _receipt("foreign", 1, disposition="foreign_project", outcome=None)
    expanded = _state_from_receipts([_receipt("target", 0), foreign])
    before = om.compute_oracles(base)
    after = om.compute_oracles(expanded)
    assert (before.eligible_n, before.d_min, before.o2, before.clean_label_counts) == (
        after.eligible_n,
        after.d_min,
        after.o2,
        after.clean_label_counts,
    )


def test_b13_required_field_cross_product_maps_every_missing_field():
    nonce_missing = _obs("placeholder", om.SOURCE_GATE, 1)
    nonce_missing = replace(nonce_missing, nonce=None)
    ts_missing = replace(_obs("ts-missing", om.SOURCE_GATE, 1), ts=None)
    markers = _state([nonce_missing, ts_missing]).loss_markers
    assert [row.reason for row in markers] == ["identity_missing", "identity_missing"]

    cases = {
        "attestation_missing": _pair("a", 2, attestation=None),
        "project_proof_missing": [
            replace(_obs("p", om.SOURCE_GATE, 3), project_proof=None),
            replace(_obs("p", om.SOURCE_HOST, 3), project_proof=None),
        ],
        "version_missing": [
            replace(_obs("v", om.SOURCE_GATE, 4), measurement_protocol_version=None),
            replace(_obs("v", om.SOURCE_HOST, 4), measurement_protocol_version=None),
        ],
    }
    for reason, rows in cases.items():
        receipt = _state(rows).receipts[0]
        assert receipt.disposition == "loss_signal"
        assert reason in receipt.loss_reasons


def test_b14_full_file_middle_rewrite_is_caught_by_full_sha():
    original = b'{"a":1,"middle":"OLD","z":1}\n'
    rewritten = b'{"a":1,"middle":"NEW","z":1}\n'
    reads = iter([original, rewritten] * 3)
    data, snapshot = om.take_snapshot(
        Path("unused"),
        om.SOURCE_GATE,
        snapshot_taken=T0,
        read_bytes=lambda _path: next(reads),
    )
    assert data is None
    assert snapshot.stable is False
    assert snapshot.first_sha256 != snapshot.second_sha256


def test_b15_malformed_marker_at_t1_edge_forces_indeterminate():
    clean = [_receipt(f"e-{i}", i) for i in range(30)]
    result = om.compute_oracles(
        _state_from_receipts(
            clean,
            markers=(
                om.LossMarker(
                    "schema_invalid", ts=clean[-1].lineage_order_key, in_scope=True
                ),
            ),
        )
    )
    assert result.o2 == "indeterminate"
    assert "loss_markers_present" in result.o2_reasons


def test_b15_parser_marker_just_after_t1_is_diagnostic_only():
    clean = [_receipt(f"t1-{index}", index) for index in range(30)]

    def parsed_schema_marker(minute):
        gate_bytes, _host_bytes = _current_measurement_bytes(
            nonce=f"malformed-{minute}", minute=minute
        )
        gate = json.loads(gate_bytes)
        gate["host_adapter"] = 7
        rows, markers, checks = om.parse_gate_log_bytes(
            (json.dumps(gate, separators=(",", ":")) + "\n").encode(),
            file="gate-2026-08-01.log",
            config=_config(),
        )
        assert not rows
        assert len(markers) == len(checks) == 1
        assert markers[0].reason == "schema_invalid"
        return markers[0], checks[0]

    def state_with(marker, check):
        state = _state_from_receipts(clean, markers=(marker,), gate_rows=(check,))
        health = tuple(
            replace(row, malformed_regions=1)
            if row.source == om.SOURCE_GATE
            else row
            for row in state.source_health
        )
        completeness = tuple(
            replace(row, complete=False)
            if row.source == om.SOURCE_GATE
            else row
            for row in state.candidate_completeness
        )
        return replace(
            state,
            source_health=health,
            candidate_completeness=completeness,
        )

    edge_marker, edge_check = parsed_schema_marker(29)
    edge = om.compute_oracles(state_with(edge_marker, edge_check))
    assert edge.o2 == "indeterminate"
    assert edge.marker_count == 1

    after_marker, after_check = parsed_schema_marker(30)
    after = om.compute_oracles(state_with(after_marker, after_check))
    assert after.o2 == "pass"
    assert after.marker_count == 0
    assert after.source_health_clean is True
    assert "candidate_completeness_unproven" not in after.o2_reasons


def test_o1_spans_full_window_while_missing_daily_closes_at_t1():
    clean = [_receipt(f"close-{index}", index) for index in range(30)]

    def invalid_check(minute):
        gate_bytes, _host_bytes = _current_measurement_bytes(
            nonce=f"invalid-o1-{minute}", minute=minute
        )
        gate = json.loads(gate_bytes)
        gate["evidence_ids"] = "bad"
        rows, markers, checks = om.parse_gate_log_bytes(
            (json.dumps(gate, separators=(",", ":")) + "\n").encode(),
            file="gate-2026-08-01.log",
            config=_config(),
        )
        assert len(rows) == len(checks) == 1
        assert not markers and checks[0].id_lists_valid is False
        return checks[0]

    edge_check = invalid_check(29)
    edge_o1 = om.compute_oracles(
        _state_from_receipts(clean, gate_rows=(edge_check,))
    )
    assert edge_o1.o1_pass is False

    after_check = invalid_check(30)
    after_o1 = om.compute_oracles(
        _state_from_receipts(clean, gate_rows=(after_check,))
    )
    assert after_o1.o1_pass is False

    def state_with_missing_daily(marker_ts):
        marker = om.LossMarker(
            "missing_daily_file",
            source=om.SOURCE_HOST,
            ts=marker_ts,
            in_scope=True,
        )
        state = _state_from_receipts(clean, markers=(marker,))
        health = tuple(
            replace(row, missing_files=1)
            if row.source == om.SOURCE_HOST
            else row
            for row in state.source_health
        )
        completeness = tuple(
            replace(row, complete=False)
            if row.source == om.SOURCE_HOST
            else row
            for row in state.candidate_completeness
        )
        return replace(
            state,
            source_health=health,
            candidate_completeness=completeness,
        )

    edge_missing = om.compute_oracles(
        state_with_missing_daily(T0 + timedelta(minutes=29))
    )
    assert edge_missing.o2 == "indeterminate"
    assert edge_missing.marker_count == 1

    after_missing = om.compute_oracles(
        state_with_missing_daily(T0 + timedelta(days=1))
    )
    assert after_missing.o2 == "pass"
    assert after_missing.marker_count == 0
    assert after_missing.source_health_clean is True


def test_o1_prefix_membership_beats_cross_source_300s_timestamp_skew():
    rows = [
        observation
        for index in range(29)
        for observation in _pair(f"o1-clean-{index}", index)
    ]
    host = _obs("o1-t1", om.SOURCE_HOST, 29)
    gate = _obs(
        "o1-t1",
        om.SOURCE_GATE,
        29,
        ts=T0 + timedelta(minutes=34),
        valid_ids=False,
    )
    state = _state([*rows, host, gate])
    t1 = next(receipt for receipt in state.receipts if receipt.nonce == "o1-t1")
    assert t1.disposition == "confirmatory"
    assert t1.prefix_member is True
    assert t1.lineage_order_key == T0 + timedelta(minutes=29)
    result = om.compute_oracles(state)
    assert result.eligible_n == 30
    assert result.o1_pass is False


def test_b16_timestamp_join_edges_are_300_inclusive_301_conflict():
    gate = _obs("join", om.SOURCE_GATE, 0)
    host_300 = _obs(
        "join", om.SOURCE_HOST, 0, ts=gate.ts + timedelta(seconds=300)
    )
    host_301 = replace(host_300, ts=gate.ts + timedelta(seconds=301))
    assert _state([gate, host_300]).receipts[0].disposition == "confirmatory"
    assert _state([gate, host_301]).receipts[0].disposition == "conflict"


def test_b17_generations_are_distinct_and_audit_reads_pinned_only():
    old = "outcome-v2.5.0"
    raw = _pair("same", 0, protocol=old)
    old_state = _state(raw, config=_config(protocol=old))
    old_receipt = old_state.receipts[0]
    new_state = _state(raw, prior=(old_receipt,))
    new_receipt = new_state.receipts[0]
    assert {old_receipt.identity, new_receipt.identity} == {
        ("same", old),
        ("same", om.MEASUREMENT_PROTOCOL_VERSION),
    }
    combined = replace(new_state, receipts=(old_receipt, new_receipt))
    result = om.compute_oracles(combined)
    assert result.d_min == 1
    assert result.disposition_counts == {"pilot": 1}


def test_b18_capture_hash_and_same_node_supersession_tamper_invalidate_first():
    config = _config(roots={om.SOURCE_GATE: ("g",), om.SOURCE_HOST: ("h",)})
    capture = om.CapturePin(om.CAPTURE_NODE_ID, om.CONTRACT_SHA256)
    prior = om.CapturePin(om.CAPTURE_NODE_ID, "f" * 64)
    manifest = om.MeasurementManifest(
        contract_sha256=om.CONTRACT_SHA256,
        ratification_node_ids=om.RATIFICATION_NODE_IDS,
        implementation_commit="a" * 40,
        measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
        source_roots=config.source_roots,
        fixture_hashes={},
        composite_sha256="",
    )
    manifest = replace(manifest, composite_sha256=manifest.expected_composite())
    state = _state_from_receipts([_receipt("one", 0)])
    state = replace(state, config=config)
    result = om.audit(
        state,
        contract_bytes=b"tampered contract",
        capture=capture,
        manifest=manifest,
        prior_capture=prior,
        fixture_bytes={},
        canaries=_canaries(config),
    )
    assert result.invalidated is True
    assert result.o1_pass is result.o2 is result.o3_pass is None
    assert "contract_hash_mismatch" in result.invalidation_reasons
    assert "capture_hash_mismatch" in result.invalidation_reasons
    assert "supersession_without_new_node" in result.invalidation_reasons
    report = om.audit_report(
        state,
        contract_bytes=b"tampered contract",
        capture=capture,
        manifest=manifest,
        prior_capture=prior,
        fixture_bytes={},
        canaries=_canaries(config),
    )
    assert report == om.audit_report(
        state,
        contract_bytes=b"tampered contract",
        capture=capture,
        manifest=manifest,
        prior_capture=prior,
        fixture_bytes={},
        canaries=_canaries(config),
    )
    assert json.loads(report)["oracles"]["invalidated"] is True


def test_b18_manifest_requires_exact_capture_lineage_roots_and_fixture_hashes():
    config = _config(roots={om.SOURCE_GATE: ("g",), om.SOURCE_HOST: ("h",)})
    capture = om.CapturePin(
        om.CAPTURE_NODE_ID,
        om.CONTRACT_SHA256,
        supersedes_node_id=999,
    )
    manifest = om.MeasurementManifest(
        contract_sha256=om.CONTRACT_SHA256,
        ratification_node_ids=om.RATIFICATION_NODE_IDS,
        implementation_commit="a" * 40,
        measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
        source_roots={om.SOURCE_GATE: ("g",)},
        fixture_hashes={},
        composite_sha256="",
    )
    manifest = replace(manifest, composite_sha256=manifest.expected_composite())
    errors = om.verify_pins(
        contract_bytes=b"tampered contract",
        capture=capture,
        manifest=manifest,
        config=config,
        fixture_bytes={},
    )
    assert "capture_supersession_mismatch" in errors
    assert "manifest_source_roots_incomplete" in errors
    assert "manifest_source_roots_mismatch" in errors
    assert "manifest_fixture_hashes_invalid" in errors


def test_m1_unrelated_nonmeasured_candidates_change_nothing():
    base = _state(_pair("target", 0, session="target"))
    unrelated = _pair(
        "unrelated", 2, session="other", proof=_proof("/different/project")
    )
    expanded = _state(_pair("target", 0, session="target") + unrelated)
    a = om.compute_oracles(base)
    b = om.compute_oracles(expanded)
    assert a.quality_summary == b.quality_summary
    assert a.clean_label_counts == b.clean_label_counts
    target = next(row for row in expanded.receipts if row.nonce == "target")
    assert base.receipts[0] == target


def test_m2_competing_candidate_before_finalization_degrades_to_conflict():
    base = _state(_pair("candidate", 1))
    assert base.receipts[0].disposition == "confirmatory"
    competing = _pair("candidate", 1)
    competing.append(_obs("candidate", om.SOURCE_HOST, 1, session="competitor"))
    assert _state(competing).receipts[0].disposition == "conflict"


def test_m3_diagnostic_pilot_changes_d_side_not_clean_quality_or_prose():
    clean = [_receipt(f"e-{i}", i) for i in range(10)]
    base = om.compute_oracles(_state_from_receipts(clean))
    with_pilot = om.compute_oracles(
        _state_from_receipts(clean + [_receipt("pilot", 11, disposition="pilot")])
    )
    assert base.clean_label_counts == with_pilot.clean_label_counts
    assert base.quality_summary == with_pilot.quality_summary
    assert with_pilot.d_min == base.d_min + 1
    base_quality = json.loads(
        om.render_report(_state_from_receipts(clean), base)
    )["quality"]
    pilot_quality = json.loads(
        om.render_report(
            _state_from_receipts(
                clean + [_receipt("pilot", 11, disposition="pilot")]
            ),
            with_pilot,
        )
    )["quality"]
    assert {
        key: base_quality[key] for key in ("clean", "summary")
    } == {key: pilot_quality[key] for key in ("clean", "summary")}


def test_m5_protocol_bump_reprocesses_while_same_generation_is_immutable():
    original = _state(_pair("immutable", 1)).receipts[0]
    changed_same = _pair("immutable", 5, verdict="DO_NOT_PROCEED", progress=0)
    changed_same += _pair("later", 6, session="session-a")
    same = _state(changed_same, prior=(original,))
    kept = next(row for row in same.receipts if row.identity == original.identity)
    assert kept == original

    bumped_protocol = "outcome-v2.6.1"
    bumped = _state(
        _pair("immutable", 5),
        config=_config(protocol=bumped_protocol),
        prior=(original,),
    )
    new = next(row for row in bumped.receipts if row.measurement_protocol_version == bumped_protocol)
    assert new.identity != original.identity
    assert new.admitted is True
    assert new.lineage_order_key == original.lineage_order_key
    assert new.disposition == "pilot"


def test_unfinalized_prior_receipt_is_not_lineage_authority():
    provisional = replace(
        _state(_pair("provisional", 1)).receipts[0], finalized=False
    )
    outside = CAP + timedelta(minutes=1)
    current = _pair("provisional", 1, ts=outside)
    state = _state(current, prior=(provisional,))
    receipt = state.receipts[0]
    assert receipt.admitted is False
    assert receipt.lineage_order_key == outside


def test_impossible_stored_receipts_hard_invalidate_before_arithmetic():
    original = _receipt("duplicate", 1)
    impossible = replace(original, outcome="OVERRIDDEN")
    result = om.compute_oracles(_state_from_receipts([original, impossible]))
    assert result.invalidated is True
    assert "duplicate_receipt_identity" in result.invalidation_reasons
    assert "non_identical_duplicate_receipt_identity" in result.invalidation_reasons

    bad_prefix = replace(original, nonce="bad-prefix", admitted=False, prefix_member=True)
    result = om.compute_oracles(_state_from_receipts([bad_prefix]))
    assert result.invalidated is True
    assert "non_admitted_prefix_member" in result.invalidation_reasons


def test_fold_preserves_and_invalidates_conflicting_finalized_prior_duplicates():
    original = _state(_pair("prior-duplicate", 1)).receipts[0]
    conflicting = replace(
        original, outcome="AMBIGUOUS", censored_reason=None
    )
    state = _state([], prior=(original, conflicting))
    assert state.receipts == (original, conflicting)
    assert {
        "duplicate_receipt_identity",
        "non_identical_duplicate_receipt_identity",
    }.issubset(state.hard_invalidations)
    result = om.compute_oracles(state)
    assert result.invalidated is True
    assert {
        "duplicate_receipt_identity",
        "non_identical_duplicate_receipt_identity",
    }.issubset(result.invalidation_reasons)


def test_protocol_bump_invalidates_duplicate_finalized_prior_identity_first():
    original = _state(_pair("old-prior-duplicate", 1)).receipts[0]
    conflicting = replace(original, outcome="AMBIGUOUS", censored_reason=None)
    state = _state(
        [],
        config=_config(protocol="outcome-v2.6.1"),
        prior=(original, conflicting),
    )
    assert {
        "duplicate_receipt_identity",
        "non_identical_duplicate_receipt_identity",
    }.issubset(state.hard_invalidations)
    result = om.compute_oracles(state)
    assert result.invalidated is True
    assert {
        "duplicate_receipt_identity",
        "non_identical_duplicate_receipt_identity",
    }.issubset(result.invalidation_reasons)


@pytest.mark.parametrize("flag", ["legacy_project", "hash_annotated", "pre_nonce"])
def test_classification_flags_participate_in_nonidentical_dedup(flag):
    base = _obs("classification-flag", om.SOURCE_GATE, 1)
    changed = replace(base, **{flag: True})
    receipt = _state([base, changed]).receipts[0]
    assert receipt.disposition == "conflict"
    assert "non_identical_duplicate:S1" in receipt.conflict_reasons


def test_measure_runner_prior_seam_cannot_collapse_conflicting_receipts():
    prior = _measure_current().receipts[0]
    conflicting = replace(prior, outcome="AMBIGUOUS", censored_reason=None)
    gate_path = FIXTURES / "gate-2026-08-03.sanitized.jsonl"
    host_path = FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl"
    roots = {
        om.SOURCE_GATE: (str(gate_path),),
        om.SOURCE_HOST: (str(host_path),),
    }
    gate_bytes, host_bytes = _current_measurement_bytes()
    content = {gate_path: gate_bytes, host_path: host_bytes}
    state = om.measure(
        roots,
        _config(roots=roots),
        prior_receipts=(prior, conflicting),
        project_proof_context=_proof_context(),
        now=T0 + timedelta(days=2),
        read_bytes=lambda path: content[path],
        enumerate_files=lambda items: tuple(Path(item) for item in items),
    )
    assert len(state.receipts) == 2
    assert om.compute_oracles(state).invalidated is True
    assert "non_identical_duplicate_receipt_identity" in state.hard_invalidations


def test_report_like_receipts_fail_closed_without_explicit_drain_flags():
    health, completeness = _accounting()
    result = om.audit_rows(
        (
            {
                "nonce": "pre-drain",
                "ts": T0,
                "disposition": "confirmatory",
                "outcome": "ACCEPTED",
            },
        ),
        _config(),
        source_health=health,
        candidate_completeness=completeness,
    )
    assert (result.eligible_n, result.d_min) == (0, 0)


def test_s2_o3_is_diagnostic_when_o2_is_indeterminate_and_order_is_lineage_stable():
    receipts = [
        _receipt(f"amb-{i}", i, outcome="AMBIGUOUS") for i in range(7)
    ] + [_receipt(f"ok-{i}", 7 + i) for i in range(24)]
    receipts.reverse()  # input order must not decide the first-30 prefix
    receipts.append(_receipt("loss", 101, disposition="loss_signal", outcome="CENSORED"))
    result = om.compute_oracles(_state_from_receipts(receipts))
    assert result.o2 == "indeterminate"
    assert result.o3_pass is False
    assert result.verdict == "indeterminate"


def test_observable_must_be_explicit_and_boundary_order_is_strict():
    unavailable = _state(_pair("unknown-observable", 1, observable=None))
    assert unavailable.receipts[0].outcome == "CENSORED"
    assert unavailable.receipts[0].censored_reason == "instrument_unavailable"

    rows = _pair("a", 1, session="same") + _pair("b", 1, session="same")
    state = _state(rows)
    by_nonce = {row.nonce: row for row in state.receipts}
    assert by_nonce["a"].window_end == T0 + timedelta(minutes=1)
    assert by_nonce["b"].window_end == T0 + timedelta(minutes=31)


def test_public_measure_fails_closed_without_resolver_and_accepts_exact_session_evidence():
    raw = _measure_current()
    assert raw.receipts[0].outcome == "CENSORED"
    assert raw.receipts[0].censored_reason == "instrument_unavailable"

    def resolver(_receipts, stable_bytes, _config):
        assert set(stable_bytes) == {om.SOURCE_GATE, om.SOURCE_HOST}
        assert all(stable_bytes[source] for source in om.SOURCES)
        return (
            om.OutcomeEvidence(
                nonce="corpus-mutated-current",
                session_id="sanitized-current-session",
                observable=True,
                evidence_available=True,
                adapter="codex",
                project_fingerprint=_proof()["fingerprint"],
                progress_inserts=1,
            ),
        )

    resolved = _measure_current(evidence_resolver=resolver)
    assert resolved.receipts[0].outcome == "ACCEPTED"


def test_exact_s2_receipt_evidence_propagates_to_s1_without_session_id():
    rows = _pair(
        "s2-authoritative-session",
        1,
        session="exact-host-session",
        observable=None,
        evidence_available=False,
        progress=0,
    )
    rows[0] = replace(rows[0], session_id=None)
    preliminary = _state(rows)
    receipt = preliminary.receipts[0]
    assert receipt.disposition == "confirmatory"
    assert receipt.session_id == "exact-host-session"

    applied, markers = om._apply_outcome_evidence(
        rows,
        preliminary.receipts,
        {},
        _config(),
        lambda _receipts, _stable_bytes, _config: (
            om.OutcomeEvidence(
                nonce="s2-authoritative-session",
                session_id="exact-host-session",
                observable=True,
                evidence_available=True,
                adapter="codex",
                project_fingerprint=_proof()["fingerprint"],
                progress_inserts=1,
            ),
        ),
    )
    assert not markers
    assert all(row.observable is True for row in applied)
    assert all(row.evidence_available is True for row in applied)
    assert all(row.progress_inserts == 1 for row in applied)
    final = _state(applied)
    assert final.receipts[0].disposition == "confirmatory"
    assert final.receipts[0].outcome == "ACCEPTED"
    assert final.receipts[0].censored_reason is None


def test_resolver_failure_censors_but_unscoped_marker_cannot_set_a_boundary():
    failed = _measure_current(
        evidence_resolver=lambda _receipts, _stable_bytes, _config: (
            _ for _ in ()
        ).throw(
            RuntimeError("sanitized")
        )
    )
    assert failed.receipts[0].outcome == "CENSORED"
    assert any(
        marker.reason == "evidence_resolver_failed"
        for marker in failed.loss_markers
    )

    state = _state(
        _pair("bounded", 1, session="same"),
        markers=(
            om.LossMarker(
                "schema_invalid", session_id="same", ts=None, in_scope=True
            ),
        ),
    )
    assert state.receipts[0].outcome == "ACCEPTED"
    assert state.receipts[0].censored_reason is None


def test_symmetric_source_receipts_are_required_for_o2():
    state = _state_from_receipts([_receipt("one", 0)])
    state = replace(
        state,
        source_health=(state.source_health[0],),
        candidate_completeness=(state.candidate_completeness[0],),
    )
    result = om.compute_oracles(state)
    assert result.o2 == "indeterminate"
    assert "source_health_missing_source" in result.o2_reasons
    assert "candidate_completeness_missing_source" in result.o2_reasons


def test_post_read_inventory_change_fails_completeness():
    gate_path = FIXTURES / "gate-2026-08-03.sanitized.jsonl"
    late_path = FIXTURES / "gate-2026-08-03-late.sanitized.jsonl"
    host_path = FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl"
    calls = {str(gate_path): 0, str(host_path): 0}

    def enumerator(roots):
        root = str(roots[0])
        calls[root] += 1
        if root == str(gate_path) and calls[root] >= 3:
            return (gate_path, late_path)
        return (Path(root),)

    gate_bytes, host_bytes = _current_measurement_bytes()
    content = {gate_path: gate_bytes, late_path: gate_bytes, host_path: host_bytes}
    roots = {
        om.SOURCE_GATE: (str(gate_path),),
        om.SOURCE_HOST: (str(host_path),),
    }
    state = om.measure(
        roots,
        _config(roots=roots),
        now=T0 + timedelta(days=2),
        read_bytes=lambda path: content[path],
        enumerate_files=enumerator,
    )
    gate_completeness = next(
        row for row in state.candidate_completeness if row.source == om.SOURCE_GATE
    )
    assert gate_completeness.complete is False
    assert any(
        marker.reason == "candidate_inventory_changed"
        for marker in state.loss_markers
    )


def test_incomplete_inventory_cannot_finalize_before_m2_competitor_is_seen():
    rows = _pair("m2-finalization", 1)
    health, completeness = _accounting()
    incomplete = tuple(
        replace(row, complete=False)
        if row.source == om.SOURCE_HOST
        else row
        for row in completeness
    )
    provisional = om.fold_observations(
        rows,
        _config(),
        source_health=health,
        candidate_completeness=incomplete,
    )
    assert provisional.receipts[0].finalized is False

    competitor = replace(
        rows[1],
        byte_offset=rows[1].byte_offset + 1000,
        session_id="competing-session",
    )
    complete = om.fold_observations(
        [*rows, competitor],
        _config(),
        source_health=health,
        candidate_completeness=completeness,
    )
    assert complete.receipts[0].finalized is True
    assert complete.receipts[0].disposition == "conflict"


def test_post_t1_schema_defect_is_excluded_by_real_fold_finalization():
    rows = [
        observation
        for index in range(30)
        for observation in _pair(f"fold-t1-{index}", index)
    ]
    marker = om.LossMarker(
        "schema_invalid",
        source=om.SOURCE_GATE,
        file="gate-2026-08-01.log",
        ts=T0 + timedelta(minutes=30),
        in_scope=True,
    )
    health, completeness = _accounting()
    health = tuple(
        replace(row, malformed_regions=1)
        if row.source == om.SOURCE_GATE
        else row
        for row in health
    )
    completeness = tuple(
        replace(row, complete=False)
        if row.source == om.SOURCE_GATE
        else row
        for row in completeness
    )
    state = replace(
        om.fold_observations(
            rows,
            _config(),
            source_health=health,
            candidate_completeness=completeness,
            loss_markers=(marker,),
        ),
        measurement_taken_at=T0 + timedelta(days=2),
    )
    assert sum(row.finalized for row in state.receipts) == 30
    result = om.compute_oracles(state)
    assert result.eligible_n == 30
    assert result.marker_count == 0
    assert result.o2 == "pass"


def test_t1_marker_scope_uses_full_lineage_nonce_order_for_timestamp_ties():
    rows = [
        observation
        for index in range(29)
        for observation in _pair(f"tie-clean-{index}", index)
    ]
    rows.extend(_pair("a-t1", 29))
    rows.extend(_pair("z-after", 29))
    marker = om.LossMarker(
        "schema_invalid",
        source=om.SOURCE_GATE,
        file="gate-2026-08-01.log",
        ts=T0 + timedelta(minutes=29),
        nonce="z-after",
        in_scope=True,
    )
    health, completeness = _accounting()
    health = tuple(
        replace(row, malformed_regions=1)
        if row.source == om.SOURCE_GATE
        else row
        for row in health
    )
    completeness = tuple(
        replace(row, complete=False)
        if row.source == om.SOURCE_GATE
        else row
        for row in completeness
    )
    state = replace(
        om.fold_observations(
            rows,
            _config(),
            source_health=health,
            candidate_completeness=completeness,
            loss_markers=(marker,),
        ),
        measurement_taken_at=T0 + timedelta(days=2),
    )
    by_nonce = {row.nonce: row for row in state.receipts}
    assert by_nonce["a-t1"].finalized and by_nonce["a-t1"].prefix_member
    assert not by_nonce["z-after"].finalized
    assert not by_nonce["z-after"].prefix_member
    result = om.compute_oracles(state)
    assert result.eligible_n == 30
    assert result.marker_count == 0
    assert result.o2 == "pass"


def test_t1_marker_scope_uses_source_coordinate_under_join_skew():
    rows = []
    for index in range(30):
        rows.append(_obs(f"skew-{index}", om.SOURCE_GATE, index))
        rows.append(
            _obs(
                f"skew-{index}",
                om.SOURCE_HOST,
                index,
                ts=T0 + timedelta(minutes=index + 4),
            )
        )
    marker = om.LossMarker(
        "schema_invalid",
        source=om.SOURCE_HOST,
        file="rollout-skew.jsonl",
        ts=T0 + timedelta(minutes=31),
        in_scope=True,
    )
    health, completeness = _accounting()
    health = tuple(
        replace(row, malformed_regions=1)
        if row.source == om.SOURCE_HOST
        else row
        for row in health
    )
    completeness = tuple(
        replace(row, complete=False)
        if row.source == om.SOURCE_HOST
        else row
        for row in completeness
    )
    state = replace(
        om.fold_observations(
            rows,
            _config(),
            source_health=health,
            candidate_completeness=completeness,
            loss_markers=(marker,),
        ),
        measurement_taken_at=T0 + timedelta(days=2),
    )
    assert not any(row.finalized for row in state.receipts)
    result = om.compute_oracles(state)
    assert result.marker_count == 1
    assert result.o2 == "indeterminate"
    assert result.v1_green is False


def test_without_t1_every_in_window_marker_remains_in_prefix():
    marker = om.LossMarker(
        "schema_invalid",
        source=om.SOURCE_GATE,
        file="gate-2026-08-03.log",
        ts=T0 + timedelta(days=2),
        in_scope=True,
    )
    health, completeness = _accounting()
    health = tuple(
        replace(row, malformed_regions=1)
        if row.source == om.SOURCE_GATE
        else row
        for row in health
    )
    completeness = tuple(
        replace(row, complete=False)
        if row.source == om.SOURCE_GATE
        else row
        for row in completeness
    )
    state = replace(
        om.fold_observations(
            _pair("no-t1", 1),
            _config(),
            source_health=health,
            candidate_completeness=completeness,
            loss_markers=(marker,),
        ),
        measurement_taken_at=T0 + timedelta(days=3),
    )
    assert state.receipts[0].finalized is False
    result = om.compute_oracles(state)
    assert result.marker_count == 1
    assert result.o2 == "indeterminate"


@pytest.mark.parametrize(
    ("filename", "finalized"),
    (("gate-2026-08-01.log", False), ("gate-2026-08-02.log", True)),
)
def test_dated_s1_malformed_file_places_health_relative_to_t1(filename, finalized):
    _rows, markers, _checks = om.parse_gate_log_bytes(
        b"not-json\n", file=filename, config=_config()
    )
    assert len(markers) == 1
    assert markers[0].ts == datetime.fromisoformat(
        filename.removeprefix("gate-").removesuffix(".log")
    ).replace(tzinfo=UTC)
    rows = [
        observation
        for index in range(30)
        for observation in _pair(f"dated-health-{index}", index)
    ]
    health, completeness = _accounting()
    health = tuple(
        replace(row, malformed_regions=1)
        if row.source == om.SOURCE_GATE
        else row
        for row in health
    )
    completeness = tuple(
        replace(row, complete=False)
        if row.source == om.SOURCE_GATE
        else row
        for row in completeness
    )
    state = replace(
        om.fold_observations(
            rows,
            _config(),
            source_health=health,
            candidate_completeness=completeness,
            loss_markers=markers,
        ),
        measurement_taken_at=T0 + timedelta(days=2),
    )
    assert all(row.finalized is finalized for row in state.receipts)
    result = om.compute_oracles(state)
    assert (result.marker_count, result.o2) == (
        (0, "pass") if finalized else (1, "indeterminate")
    )


def test_s1_missing_ts_uses_dated_file_scope():
    gate_bytes, _host_bytes = _current_measurement_bytes()
    gate = json.loads(gate_bytes)
    gate.pop("ts", None)
    encoded = (json.dumps(gate, separators=(",", ":")) + "\n").encode()

    old_rows, old_markers, old_checks = om.parse_gate_log_bytes(
        encoded,
        file="gate-2025-01-01.log",
        config=_config(),
    )
    assert len(old_rows) == len(old_checks) == 1 and not old_markers
    assert old_checks[0].in_scope is False
    old_state = _state(old_rows, gate_rows=old_checks)
    assert old_state.loss_markers[-1].reason == "identity_missing"
    assert old_state.loss_markers[-1].in_scope is False
    assert om.compute_oracles(old_state).marker_count == 0

    current_rows, current_markers, current_checks = om.parse_gate_log_bytes(
        encoded,
        file="gate-2026-08-01.log",
        config=_config(),
    )
    assert len(current_rows) == len(current_checks) == 1 and not current_markers
    assert current_checks[0].in_scope is True
    current_state = _state(current_rows, gate_rows=current_checks)
    assert current_state.loss_markers[-1].in_scope is True
    assert om.compute_oracles(current_state).marker_count == 1


@pytest.mark.parametrize("bad_ts", ["not-a-timestamp", 123, True])
def test_s1_invalid_ts_is_schema_invalid_with_dated_file_scope(bad_ts):
    gate_bytes, _host_bytes = _current_measurement_bytes()
    gate = json.loads(gate_bytes)
    gate["ts"] = bad_ts
    encoded = (json.dumps(gate, separators=(",", ":")) + "\n").encode()

    old_rows, old_markers, old_checks = om.parse_gate_log_bytes(
        encoded,
        file="gate-2025-01-01.log",
        config=_config(),
    )
    assert not old_rows
    assert len(old_checks) == len(old_markers) == 1
    assert old_checks[0].in_scope is False
    assert old_markers[0].reason == "schema_invalid"
    assert old_markers[0].in_scope is False
    assert "invalid_timestamp:ts" in (old_markers[0].detail or "")
    assert om.compute_oracles(_state([], markers=old_markers)).marker_count == 0

    current_rows, current_markers, current_checks = om.parse_gate_log_bytes(
        encoded,
        file="gate-2026-08-01.log",
        config=_config(),
    )
    assert not current_rows
    assert len(current_checks) == len(current_markers) == 1
    assert current_checks[0].in_scope is True
    assert current_markers[0].in_scope is True
    assert om.compute_oracles(
        _state([], markers=current_markers)
    ).marker_count == 1


@pytest.mark.parametrize("bad_ts", ["not-a-timestamp", 123, True])
def test_s2_invalid_timestamp_is_schema_invalid_not_identity_missing(bad_ts):
    _gate_bytes, host_bytes = _current_measurement_bytes()
    records = [json.loads(line) for line in host_bytes.splitlines()]
    records[1]["timestamp"] = bad_ts
    encoded = (
        "\n".join(json.dumps(row, separators=(",", ":")) for row in records)
        + "\n"
    ).encode()

    rows, markers = om.parse_host_record_segments(
        (("host-current.jsonl", encoded),),
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not rows
    schema_markers = [row for row in markers if row.reason == "schema_invalid"]
    assert len(schema_markers) == 1
    assert schema_markers[0].ts is None
    assert schema_markers[0].in_scope is True
    assert "invalid_timestamp:timestamp" in (schema_markers[0].detail or "")


def test_unreadable_old_dated_s1_file_does_not_poison_current_health(tmp_path):
    gate_root = tmp_path / "gate"
    host_root = tmp_path / "host"
    gate_root.mkdir()
    host_root.mkdir()
    gate_current = gate_root / "gate-2026-08-01.log"
    gate_old = gate_root / "gate-2025-01-01.log"
    host_current = host_root / "rollout-current.jsonl"
    gate_bytes, host_bytes = _current_measurement_bytes()
    roots = {
        om.SOURCE_GATE: (str(gate_root),),
        om.SOURCE_HOST: (str(host_root),),
    }

    def enumerate_files(items):
        return (
            (gate_current, gate_old)
            if str(items[0]) == str(gate_root)
            else (host_current,)
        )

    def read_bytes(path):
        if path == gate_old:
            raise OSError("old file unavailable")
        return gate_bytes if path == gate_current else host_bytes

    state = om.measure(
        roots,
        _config(roots=roots),
        project_proof_context=_proof_context(),
        now=T0 + timedelta(days=2),
        enumerate_files=enumerate_files,
        read_bytes=read_bytes,
    )
    gate_health = next(
        row for row in state.source_health if row.source == om.SOURCE_GATE
    )
    gate_completeness = next(
        row
        for row in state.candidate_completeness
        if row.source == om.SOURCE_GATE
    )
    old_marker = next(
        row
        for row in state.loss_markers
        if row.reason == "unreadable_file"
    )
    assert gate_health.clean is True
    assert gate_completeness.complete is True
    assert old_marker.ts == datetime(2025, 1, 1, tzinfo=UTC)
    assert old_marker.in_scope is False
    assert om.compute_oracles(state).marker_count == 0


def test_foreign_marker_reusing_target_nonce_cannot_exit_target_accounting():
    clean = [_receipt(f"foreign-marker-{index}", index) for index in range(30)]
    marker = om.LossMarker(
        "schema_invalid",
        source=om.SOURCE_GATE,
        ts=T0 + timedelta(minutes=5),
        nonce=clean[0].nonce,
        project_proof=_proof("/foreign"),
        in_scope=True,
    )
    result = om.compute_oracles(
        _state_from_receipts(clean, markers=(marker,))
    )
    assert result.marker_count == 1
    assert result.o2 == "indeterminate"
    assert result.v1_green is False


def test_foreign_prefix_receipt_cannot_extend_target_source_cutoff():
    rows = [
        observation
        for index in range(30)
        for observation in _pair(f"cutoff-target-{index}", index)
    ]
    marker = om.LossMarker(
        "schema_invalid",
        source=om.SOURCE_GATE,
        ts=T0 + timedelta(minutes=31),
        in_scope=True,
    )
    base = om.compute_oracles(_state(rows, markers=(marker,)))
    assert base.marker_count == 0 and base.o2 == "pass"

    foreign_proof = _proof("/foreign-cutoff")
    foreign = [
        _obs(
            "foreign-cutoff",
            om.SOURCE_GATE,
            28,
            proof=foreign_proof,
            ts=T0 + timedelta(minutes=32),
        ),
        _obs(
            "foreign-cutoff",
            om.SOURCE_HOST,
            28,
            proof=foreign_proof,
            ts=T0 + timedelta(minutes=28),
        ),
    ]
    expanded = om.compute_oracles(
        _state([*rows, *foreign], markers=(marker,))
    )
    assert expanded.marker_count == 0
    assert expanded.o2 == "pass"


def test_resumed_host_activity_day_comes_from_record_timestamp_not_old_path():
    state = _measure_current(minute=24 * 60 + 10)
    assert not any(
        marker.reason == "missing_daily_file" for marker in state.loss_markers
    )


def test_foreign_or_unproven_rows_do_not_assert_same_project_daily_activity():
    state = _measure_current(
        minute=10,
        host_minute=24 * 60 + 10,
        project_proof=_proof("/proven/foreign"),
        host_project="/proven/foreign",
    )
    assert all(row.disposition == "foreign_project" for row in state.receipts)
    assert not any(
        marker.reason == "missing_daily_file" for marker in state.loss_markers
    )


def test_claude_corpus_shape_and_unmatched_output_marker_are_supported():
    data = (FIXTURES / "claude-transcript-2026-07-22.sanitized.jsonl").read_bytes()
    rows, markers = om.parse_host_record_bytes(
        data,
        file="claude-transcript-2026-07-22.sanitized.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not markers
    assert len(rows) == 1
    assert (rows[0].adapter, rows[0].session_id, rows[0].skipped) == (
        "claude",
        "sanitized-claude-session",
        True,
    )

    call_only = data.splitlines(keepends=True)[0]
    rows, markers = om.parse_host_record_bytes(
        call_only,
        file="claude-transcript-2026-07-22.sanitized.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not rows
    assert [marker.reason for marker in markers] == ["host_call_output_missing"]


def test_public_audit_requires_complete_envelope_canaries_and_exact_commit():
    state = _state_from_receipts([_receipt("envelope", 0)])
    missing = om.audit(state)
    assert missing.invalidated is True
    assert {
        "incomplete_audit_envelope",
        "fixture_bytes_missing",
        "canary_evidence_missing",
    }.issubset(missing.invalidation_reasons)

    config = replace(state.config, implementation_commit="a" * 40)
    state = replace(state, config=config)
    manifest = om.MeasurementManifest(
        contract_sha256=om.CONTRACT_SHA256,
        ratification_node_ids=om.RATIFICATION_NODE_IDS,
        implementation_commit="b" * 40,
        measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
        source_roots=config.source_roots,
        fixture_hashes={},
        composite_sha256="",
    )
    manifest = replace(manifest, composite_sha256=manifest.expected_composite())
    result = om.audit(
        state,
        contract_bytes=b"tampered contract",
        capture=om.CapturePin(om.CAPTURE_NODE_ID, om.CONTRACT_SHA256),
        manifest=manifest,
        fixture_bytes={},
        canaries=_canaries(config),
    )
    assert "implementation_commit_mismatch" in result.invalidation_reasons


def test_missing_pinned_roots_are_dirty_incomplete_and_hard_invalid():
    empty = om.measure({}, _config(roots={}), now=T0)
    assert {
        "source_roots_missing:S1",
        "source_roots_missing:S2",
    }.issubset(empty.hard_invalidations)
    assert all(not row.clean for row in empty.source_health)
    assert all(not row.complete for row in empty.candidate_completeness)
    assert om.compute_oracles(empty).invalidated is True

    roots = {
        om.SOURCE_GATE: ("/definitely/missing/v2.6/gate",),
        om.SOURCE_HOST: ("/definitely/missing/v2.6/host",),
    }
    missing = om.measure(roots, _config(roots=roots), now=T0)
    assert {
        "source_root_unavailable:S1",
        "source_root_unavailable:S2",
    }.issubset(missing.hard_invalidations)
    assert all(not row.complete for row in missing.candidate_completeness)


def test_one_empty_existing_source_with_peer_activity_is_loss_not_invalidation(
    tmp_path,
):
    gate_root = tmp_path / "gate"
    host_root = tmp_path / "host"
    gate_root.mkdir()
    host_root.mkdir()
    host_path = host_root / "rollout.jsonl"
    _gate_bytes, host_bytes = _current_measurement_bytes()
    roots = {
        om.SOURCE_GATE: (str(gate_root),),
        om.SOURCE_HOST: (str(host_root),),
    }
    state = om.measure(
        roots,
        _config(roots=roots),
        project_proof_context=_proof_context(),
        now=T0 + timedelta(days=2),
        enumerate_files=lambda items: (
            () if str(items[0]) == str(gate_root) else (host_path,)
        ),
        read_bytes=lambda path: host_bytes,
    )
    assert not state.hard_invalidations
    assert any(row.disposition == "loss_signal" for row in state.receipts)
    assert any(
        marker.reason == "missing_daily_file"
        and marker.source == om.SOURCE_GATE
        for marker in state.loss_markers
    )
    assert om.compute_oracles(state).o2 == "indeterminate"


def test_dual_empty_existing_sources_are_clean_and_insufficient_n_at_cap(tmp_path):
    gate_root = tmp_path / "gate"
    host_root = tmp_path / "host"
    gate_root.mkdir()
    host_root.mkdir()
    roots = {
        om.SOURCE_GATE: (str(gate_root),),
        om.SOURCE_HOST: (str(host_root),),
    }
    state = om.measure(
        roots,
        _config(roots=roots),
        now=CAP + timedelta(seconds=om.FRESHNESS_SECONDS),
        enumerate_files=lambda _items: (),
    )
    assert not state.hard_invalidations
    assert not state.receipts
    assert not state.loss_markers
    assert all(row.clean for row in state.source_health)
    assert all(row.complete for row in state.candidate_completeness)
    result = om.compute_oracles(state)
    assert result.o1_pass is True
    assert result.o2 == "pass"
    assert result.verdict == "insufficient-n"


def test_legacy_project_key_is_pilot_but_nonce_less_is_still_loss_marker():
    rows = [
        replace(row, project_proof=None, legacy_project=True)
        for row in _pair("legacy", 1)
    ]
    receipt = _state(rows).receipts[0]
    assert receipt.disposition == "pilot"
    assert "project_proof_missing" not in receipt.loss_reasons

    nonce_less = replace(rows[0], nonce=None)
    state = _state([nonce_less])
    assert not state.receipts
    assert [marker.reason for marker in state.loss_markers][-1] == "identity_missing"


def test_s2_nested_shared_fields_are_parsed_and_disagreements_conflict():
    gate_bytes, host_bytes = _current_measurement_bytes()
    gate_rows, _markers, _checks = om.parse_gate_log_bytes(
        gate_bytes, file="gate-current.jsonl", config=_config()
    )
    host_rows, markers = om.parse_host_record_bytes(
        host_bytes,
        file="host-current.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not markers
    assert len(host_rows) == 1
    assert host_rows[0].verdict == "PROCEED"
    assert host_rows[0].verdict_id_lists == _ids()
    assert _state(gate_rows + host_rows).receipts[0].disposition == "confirmatory"

    records = [json.loads(line) for line in host_bytes.splitlines()]
    result = json.loads(records[2]["payload"]["output"])
    result["verdict"]["recommendation"] = "MODIFY"
    result["verdict"]["decision_chain"] = [4164]
    result["runtime_attestation"] = "different-runtime"
    records[2]["payload"]["output"] = json.dumps(result, sort_keys=True)
    changed = ("\n".join(json.dumps(row) for row in records) + "\n").encode()
    changed_rows, _ = om.parse_host_record_bytes(
        changed,
        file="host-current.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    receipt = _state(gate_rows + changed_rows).receipts[0]
    assert receipt.disposition == "conflict"
    assert {
        "runtime_attestation_alias_mismatch",
        "verdict_mismatch",
        "verdict_id_lists_mismatch",
    }.issubset(receipt.conflict_reasons)


def test_multisegment_s2_joins_across_files_dedups_and_surfaces_conflict():
    gate_bytes, host_bytes = _current_measurement_bytes()
    host_lines = host_bytes.splitlines(keepends=True)
    call_segment = host_lines[0] + host_lines[1]
    result_segment = host_lines[0] + host_lines[2]
    rows, markers = om.parse_host_record_segments(
        (("a-segment.jsonl", call_segment), ("b-segment.jsonl", result_segment)),
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not markers
    assert len(rows) == 1
    assert (rows[0].file, rows[0].byte_offset) == (
        "a-segment.jsonl",
        len(host_lines[0]),
    )

    duplicate_rows, markers = om.parse_host_record_segments(
        (
            ("a-segment.jsonl", call_segment),
            ("b-segment.jsonl", result_segment),
            ("c-segment.jsonl", result_segment),
        ),
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not markers
    assert len(duplicate_rows) == 1

    changed_records = [json.loads(line) for line in result_segment.splitlines()]
    changed_result = json.loads(changed_records[1]["payload"]["output"])
    changed_result["runtime_version"] = "different-runtime"
    changed_records[1]["payload"]["output"] = json.dumps(
        changed_result, sort_keys=True
    )
    changed_segment = (
        "\n".join(json.dumps(row) for row in changed_records) + "\n"
    ).encode()
    competing, markers = om.parse_host_record_segments(
        (
            ("a-segment.jsonl", call_segment),
            ("b-segment.jsonl", result_segment),
            ("c-segment.jsonl", changed_segment),
        ),
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not markers
    assert len(competing) == 2
    gate_rows, _markers, _checks = om.parse_gate_log_bytes(
        gate_bytes, file="gate-current.jsonl", config=_config()
    )
    receipt = _state(gate_rows + competing).receipts[0]
    assert receipt.disposition == "conflict"
    assert "non_identical_duplicate:S2" in receipt.conflict_reasons


def test_m2_nonidentical_call_candidates_with_one_result_conflict():
    gate_bytes, host_bytes = _current_measurement_bytes()
    records = [json.loads(line) for line in host_bytes.splitlines()]
    competing_call = json.loads(json.dumps(records[1]))
    arguments = json.loads(competing_call["payload"]["arguments"])
    arguments["request"] = "different structural request"
    competing_call["payload"]["arguments"] = json.dumps(
        arguments, separators=(",", ":")
    )
    data = (
        "\n".join(
            json.dumps(record, separators=(",", ":"))
            for record in (records[0], records[1], competing_call, records[2])
        )
        + "\n"
    ).encode()
    host_rows, markers = om.parse_host_record_bytes(
        data,
        file="rollout-competing-call.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not markers
    assert len(host_rows) == 2
    assert len({row.raw_sha256 for row in host_rows}) == 2
    gate_rows, _markers, _checks = om.parse_gate_log_bytes(
        gate_bytes, file="gate-current.jsonl", config=_config()
    )
    receipt = _state([*gate_rows, *host_rows]).receipts[0]
    assert receipt.disposition == "conflict"
    assert "non_identical_duplicate:S2" in receipt.conflict_reasons


def test_m2_gate_and_unrelated_call_reusing_identity_conflict():
    gate_bytes, host_bytes = _current_measurement_bytes()
    records = [json.loads(line) for line in host_bytes.splitlines()]
    unrelated = json.loads(json.dumps(records[1]))
    unrelated["payload"]["name"] = "apply_patch"
    data = (
        "\n".join(
            json.dumps(record, separators=(",", ":"))
            for record in (records[0], records[1], unrelated, records[2])
        )
        + "\n"
    ).encode()
    host_rows, markers = om.parse_host_record_bytes(
        data,
        file="rollout-reused-call-id.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not markers and len(host_rows) == 1
    assert "call_identity_reused_by_unrelated_tool" in (
        host_rows[0].embedded_conflict_reasons
    )
    gate_rows, _markers, _checks = om.parse_gate_log_bytes(
        gate_bytes, file="gate-current.jsonl", config=_config()
    )
    receipt = _state([*gate_rows, *host_rows]).receipts[0]
    assert receipt.disposition == "conflict"


def test_host_proof_derivation_requires_explicit_canonical_context():
    data = (FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl").read_bytes()
    with pytest.raises(ValueError, match="ProjectProofContext"):
        om.parse_host_record_bytes(
            data,
            file="historical.jsonl",
            config=_config(),
            vault_key=KEY,
        )
    unproved, _ = om.parse_host_record_bytes(
        data, file="historical.jsonl", config=_config()
    )
    proved, _ = om.parse_host_record_bytes(
        data,
        file="historical.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert unproved[0].project_proof is None
    assert proved[0].project_proof == _proof()


def test_compact_mcp_exec_envelope_reconstructs_all_six_id_lists_without_conflict():
    result = _compact_gate_result()
    host_rows, markers = om.parse_host_record_bytes(
        _wrapped_codex_host_bytes(result),
        file="rollout-compact.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    expected_ids = {
        "evidence_ids": [4164, 4175],
        "decision_chain": [4164],
        "abandoned_paths": [4066],
        "active_constraints": [4113, 4137],
        "current_direction": [4179],
        "seed_ids": [4113, 4137],
    }
    assert not markers
    assert len(host_rows) == 1
    assert host_rows[0].verdict_id_lists == expected_ids

    gate = json.loads(
        (FIXTURES / "gate-2026-08-03.sanitized.jsonl").read_text()
    )
    gate.update(
        {
            "ts": (T0 + timedelta(minutes=10)).isoformat().replace(
                "+00:00", "Z"
            ),
            "gate_call_id": result["gate_call_id"],
            "session_id": result["session_id"],
            "attestation": RUNTIME,
            "measurement_protocol_version": om.MEASUREMENT_PROTOCOL_VERSION,
            "project_proof": _proof(),
            "key_epoch": EPOCH,
            "runtime_version": RUNTIME,
            "host_adapter": "codex",
            "recommendation": "PROCEED",
            "skipped": False,
            **expected_ids,
        }
    )
    gate_rows, gate_markers, _checks = om.parse_gate_log_bytes(
        (json.dumps(gate, separators=(",", ":")) + "\n").encode(),
        file="gate-compact.log",
        config=_config(),
    )
    assert not gate_markers
    receipt = _state(gate_rows + host_rows).receipts[0]
    assert receipt.disposition == "confirmatory"
    assert not receipt.conflict_reasons

    # The historical direct function-call envelope uses the same structural
    # Wall-time/Output wrapper and must remain supported.
    historical, historical_markers = om.parse_host_record_bytes(
        _wrapped_codex_host_bytes(result, use_exec=False),
        file="rollout-historical.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not historical_markers
    assert len(historical) == 1
    assert historical[0].verdict_id_lists == expected_ids

    # verbose=True returns the seed records under chains.seeds instead of the
    # compact chain_summary.seed_ids projection.
    full = json.loads(json.dumps(result))
    full.pop("chain_summary")
    full["chains"] = {
        "seeds": [{"id": 4113}, {"id": 4137}],
        "chains": [],
        "evidence_node_ids": [],
    }
    full_rows, full_markers = om.parse_host_record_bytes(
        _wrapped_codex_host_bytes(full),
        file="rollout-full.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not full_markers
    assert len(full_rows) == 1
    assert full_rows[0].verdict_id_lists == expected_ids
    assert _state(gate_rows + full_rows).receipts[0].disposition == "confirmatory"


def test_verbose_mcp_chain_seed_ids_are_strictly_typed():
    result = _compact_gate_result()
    result.pop("chain_summary")
    result["chains"] = {"seeds": [{"id": "4113"}, {"id": True}]}
    rows, markers = om.parse_host_record_bytes(
        _wrapped_codex_host_bytes(result),
        file="rollout-full-invalid-seeds.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not markers
    assert len(rows) == 1
    assert "seed_ids" not in (rows[0].verdict_id_lists or {})


@pytest.mark.parametrize(
    ("field", "bad_value", "expected_detail"),
    [
        ("skipped", "false", "invalid_bool:skipped"),
        ("progress_inserts", "3", "invalid_int:progress_inserts"),
        ("touches", True, "invalid_int:touches"),
        ("host_adapter", 7, "invalid_host_adapter"),
    ],
)
def test_compact_mcp_strict_scalar_types_become_schema_loss(
    field, bad_value, expected_detail
):
    result = _compact_gate_result()
    if field == "skipped":
        result["verdict"][field] = bad_value
    else:
        result[field] = bad_value
    rows, markers = om.parse_host_record_bytes(
        _wrapped_codex_host_bytes(result),
        file="rollout-invalid-typed.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not rows
    assert len(markers) == 1
    assert markers[0].reason == "schema_invalid"
    assert expected_detail in (markers[0].detail or "")


def test_wrong_typed_verdict_id_lists_remain_observations_and_fail_o1():
    gate_bytes, _host_bytes = _current_measurement_bytes()
    gate = json.loads(gate_bytes)
    gate["evidence_ids"] = "bad"
    gate_rows, gate_markers, checks = om.parse_gate_log_bytes(
        (json.dumps(gate, separators=(",", ":")) + "\n").encode(),
        file="gate-invalid-id-list.log",
        config=_config(),
    )
    assert not gate_markers
    assert len(gate_rows) == 1
    assert len(checks) == 1
    assert checks[0].id_lists_valid is False
    assert "evidence_ids" in checks[0].missing_fields
    assert om.compute_oracles(
        _state(gate_rows, gate_rows=checks)
    ).o1_pass is False

    host_result = _compact_gate_result("invalid-host-id-list")
    host_result["evidence"] = [{"id": "4164"}]
    host_rows, host_markers = om.parse_host_record_bytes(
        _wrapped_codex_host_bytes(host_result),
        file="rollout-invalid-id-list.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not host_markers
    assert len(host_rows) == 1
    assert "evidence_ids" not in (host_rows[0].verdict_id_lists or {})


def test_nonstring_s1_identity_fields_fail_closed_without_minting_receipt():
    gate_bytes, _host_bytes = _current_measurement_bytes()
    base = json.loads(gate_bytes)
    cases = []
    gate_call_id = dict(base)
    gate_call_id["gate_call_id"] = True
    cases.append((gate_call_id, "invalid_string:gate_call_id"))
    legacy_nonce = dict(base)
    legacy_nonce.pop("gate_call_id")
    legacy_nonce["nonce"] = 17
    cases.append((legacy_nonce, "invalid_string:nonce"))
    session_id = dict(base)
    session_id["session_id"] = False
    cases.append((session_id, "invalid_string:session_id"))

    for index, (row, expected_detail) in enumerate(cases):
        observations, markers, _checks = om.parse_gate_log_bytes(
            (json.dumps(row, separators=(",", ":")) + "\n").encode(),
            file=f"gate-invalid-identity-{index}.log",
            config=_config(),
        )
        assert not observations
        assert len(markers) == 1
        assert markers[0].reason == "schema_invalid"
        assert expected_detail in (markers[0].detail or "")


def test_s1_requires_explicit_attestation_and_strict_structural_strings():
    gate_bytes, _host_bytes = _current_measurement_bytes()
    missing = json.loads(gate_bytes)
    missing.pop("attestation", None)
    missing.pop("runtime_attestation", None)
    rows, markers, _checks = om.parse_gate_log_bytes(
        (json.dumps(missing, separators=(",", ":")) + "\n").encode(),
        file="gate-missing-attestation.log",
        config=_config(),
    )
    assert not markers and len(rows) == 1
    assert rows[0].attestation is None
    assert rows[0].runtime_version == RUNTIME
    receipt = _state(rows).receipts[0]
    assert receipt.disposition == "loss_signal"
    assert "attestation_missing" in receipt.loss_reasons

    for field in (
        "attestation",
        "runtime_attestation",
        "runtime_version",
        "measurement_protocol_version",
        "key_epoch",
    ):
        invalid = json.loads(gate_bytes)
        invalid[field] = 7
        parsed, field_markers, _checks = om.parse_gate_log_bytes(
            (json.dumps(invalid, separators=(",", ":")) + "\n").encode(),
            file=f"gate-invalid-{field}.log",
            config=_config(),
        )
        assert not parsed
        assert len(field_markers) == 1
        assert field_markers[0].reason == "schema_invalid"
        assert f"invalid_string:{field}" in (field_markers[0].detail or "")


def test_o1_requires_flat_s1_id_list_fields_not_verbose_fallbacks():
    gate_bytes, _host_bytes = _current_measurement_bytes()
    gate = json.loads(gate_bytes)
    gate.pop("evidence_ids")
    gate["evidence"] = [{"id": 4164}]
    rows, markers, checks = om.parse_gate_log_bytes(
        (json.dumps(gate, separators=(",", ":")) + "\n").encode(),
        file="gate-missing-flat-evidence-ids.log",
        config=_config(),
    )
    assert not markers and len(rows) == len(checks) == 1
    assert checks[0].id_lists_valid is False
    assert "evidence_ids" in checks[0].missing_fields
    assert om.compute_oracles(_state(rows, gate_rows=checks)).o1_pass is False


def test_missing_project_key_components_are_named_project_proof_loss():
    gate_bytes, host_bytes = _current_measurement_bytes()
    for field in ("version", "key_epoch", "key_id", "fingerprint"):
        gate = json.loads(gate_bytes)
        gate["project_proof"].pop(field, None)
        rows, markers, _checks = om.parse_gate_log_bytes(
            (json.dumps(gate, separators=(",", ":")) + "\n").encode(),
            file=f"gate-missing-proof-{field}.log",
            config=_config(),
        )
        assert not markers and len(rows) == 1
        assert rows[0].project_proof is None
        assert "project_proof_missing" in _state(rows).receipts[0].loss_reasons

    gate = json.loads(gate_bytes)
    gate.pop("key_epoch", None)
    rows, markers, _checks = om.parse_gate_log_bytes(
        (json.dumps(gate, separators=(",", ":")) + "\n").encode(),
        file="gate-missing-top-key-epoch.log",
        config=_config(),
    )
    assert not markers and len(rows) == 1
    receipt = _state(rows).receipts[0]
    assert "project_proof_missing" in receipt.loss_reasons
    assert "key_epoch_mismatch" not in receipt.loss_reasons


def test_nonstring_s2_identity_fields_fail_closed_without_minting_receipt():
    for field, bad_value in (
        ("gate_call_id", True),
        ("session_id", 17),
    ):
        result = _compact_gate_result(f"invalid-{field}")
        result[field] = bad_value
        rows, markers = om.parse_host_record_bytes(
            _wrapped_codex_host_bytes(result),
            file=f"rollout-invalid-{field}.jsonl",
            config=_config(),
            project_proof_context=_proof_context(),
        )
        assert not rows
        assert len(markers) == 1
        assert markers[0].reason == "schema_invalid"
        assert f"invalid_string:{field}" in (markers[0].detail or "")

    records = [
        json.loads(line)
        for line in _wrapped_codex_host_bytes(
            _compact_gate_result("invalid-session-context")
        ).splitlines()
    ]
    records[0]["payload"]["id"] = True
    records[0]["payload"]["session_id"] = True
    invalid_session = (
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records)
        + "\n"
    ).encode()
    rows, markers = om.parse_host_record_bytes(
        invalid_session,
        file="rollout-invalid-session-context.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not rows
    assert len(markers) == 1
    assert markers[0].reason == "schema_invalid"
    assert "invalid_string:id" in (markers[0].detail or "")

    records = [
        json.loads(line)
        for line in _wrapped_codex_host_bytes(
            _compact_gate_result("invalid-outer-call-id")
        ).splitlines()
    ]
    records[1]["payload"]["call_id"] = True
    records[2]["payload"]["call_id"] = True
    invalid_call_id = (
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records)
        + "\n"
    ).encode()
    rows, markers = om.parse_host_record_bytes(
        invalid_call_id,
        file="rollout-invalid-call-id.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not rows
    assert len(markers) == 2
    assert all(marker.reason == "schema_invalid" for marker in markers)
    assert all(
        "invalid_string:call_id" in (marker.detail or "")
        for marker in markers
    )


def test_result_host_adapter_must_match_outer_s2_adapter():
    result = _compact_gate_result()
    result["host_adapter"] = "claude"
    host_rows, markers = om.parse_host_record_bytes(
        _wrapped_codex_host_bytes(result),
        file="rollout-adapter-mismatch.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not markers
    assert len(host_rows) == 1
    assert "host_adapter_mismatch" in host_rows[0].embedded_conflict_reasons
    gate_row = replace(
        _obs(
            "compact-current",
            om.SOURCE_GATE,
            10,
            session="compact-session",
        ),
        verdict_id_lists=host_rows[0].verdict_id_lists,
    )
    receipt = _state([gate_row, host_rows[0]]).receipts[0]
    assert receipt.disposition == "conflict"
    assert "host_adapter_mismatch" in receipt.conflict_reasons


def test_exec_prompt_or_comment_text_cannot_manufacture_a_gate_call():
    decoy = (
        'const quoted = "tools.mcp__latch__latch_gate(";\n'
        "// tools.mcp__latch__latch_gate({request: 'prompt'})\n"
        "/* tools.mcp__latch__latch_gate({request: 'comment'}) */\n"
        "text(quoted);"
    )
    rows, markers = om.parse_host_record_bytes(
        _wrapped_codex_host_bytes(_compact_gate_result(), script=decoy),
        file="rollout-decoy.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not rows
    assert not markers


def test_unrelated_codex_tool_result_is_neither_observation_nor_loss():
    records = [
        json.loads(line)
        for line in _wrapped_codex_host_bytes(
            _compact_gate_result(), use_exec=False
        ).splitlines()
    ]
    records[1]["payload"]["name"] = "read_file"
    records[2]["payload"]["output"] = "ordinary non-gate tool output"
    data = (
        "\n".join(
            json.dumps(record, separators=(",", ":")) for record in records
        )
        + "\n"
    ).encode()
    rows, markers = om.parse_host_record_bytes(
        data,
        file="rollout-unrelated.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not rows
    assert not markers


def test_unrelated_claude_tool_result_is_neither_observation_nor_loss():
    records = [
        json.loads(line)
        for line in (
            FIXTURES / "claude-transcript-2026-07-22.sanitized.jsonl"
        ).read_text().splitlines()
    ]
    records[0]["message"]["content"][0]["name"] = "Read"
    records[1]["message"]["content"][0]["content"] = [
        {"type": "text", "text": "ordinary non-gate tool output"}
    ]
    data = (
        "\n".join(
            json.dumps(record, separators=(",", ":")) for record in records
        )
        + "\n"
    ).encode()
    rows, markers = om.parse_host_record_bytes(
        data,
        file="claude-unrelated.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not rows
    assert not markers


def test_old_protocol_same_nonce_candidate_conflicts_but_identity_stays_current():
    rows = _pair("generation-conflict", 1)
    rows.append(
        _obs(
            "generation-conflict",
            om.SOURCE_HOST,
            1,
            protocol="outcome-v2.5.0",
        )
    )
    state = _state(rows)
    assert len(state.receipts) == 1
    receipt = state.receipts[0]
    assert receipt.identity == (
        "generation-conflict",
        om.MEASUREMENT_PROTOCOL_VERSION,
    )
    assert receipt.disposition == "conflict"
    assert "protocol_mismatch" in receipt.conflict_reasons


@pytest.mark.parametrize("surviving_source", [om.SOURCE_GATE, om.SOURCE_HOST])
def test_finalized_same_generation_source_deletion_is_loss_without_mutation(
    surviving_source,
):
    original = _state(_pair("finalized-delete", 1)).receipts[0]
    surviving = tuple(
        row for row in original.observations if row.source == surviving_source
    )
    state = _state(surviving, prior=(original,))
    assert state.receipts == (original,)
    markers = [
        marker
        for marker in state.loss_markers
        if marker.reason == "finalized_source_deleted"
    ]
    assert len(markers) == 1
    assert markers[0].source == (
        om.SOURCE_HOST
        if surviving_source == om.SOURCE_GATE
        else om.SOURCE_GATE
    )
    result = om.compute_oracles(state)
    assert result.o2 == "indeterminate"
    assert "loss_markers_present" in result.o2_reasons

    unchanged = _state(original.observations, prior=(original,))
    assert not any(
        marker.reason == "finalized_source_deleted"
        for marker in unchanged.loss_markers
    )


def test_source_specific_discovery_accepts_only_gate_codex_and_claude_shapes(
    tmp_path,
):
    root = tmp_path / "corpus"
    claude = root / ".claude" / "projects" / "project-a"
    unrelated_projects = root / "projects" / "project-a"
    unrelated_claude = root / ".claude" / "other"
    for directory in (root, claude, unrelated_projects, unrelated_claude):
        directory.mkdir(parents=True, exist_ok=True)
    candidates = {
        "gate": root / "gate-2026-08-04.log",
        "gate_jsonl": root / "gate-2026-08-04.jsonl",
        "random_log": root / "events.log",
        "rollout": root / "rollout-2026-08-04.jsonl",
        "random_jsonl": root / "events.jsonl",
        "claude": claude / "session.jsonl",
        "projects_only": unrelated_projects / "session.jsonl",
        "claude_only": unrelated_claude / "session.jsonl",
    }
    for path in candidates.values():
        path.write_text("\n")

    gate_files, gate_errors = om.discover_source_files(
        (root,), om.SOURCE_GATE
    )
    host_files, host_errors = om.discover_source_files(
        (root,), om.SOURCE_HOST
    )
    assert gate_errors == host_errors == 0
    assert gate_files == (candidates["gate"],)
    assert host_files == tuple(
        sorted((candidates["claude"], candidates["rollout"]), key=os.fspath)
    )


def test_traversal_errors_are_counted_and_candidate_receipts_fail_closed(
    tmp_path,
):
    gate_root = tmp_path / "gate-root"
    host_root = tmp_path / "host-root"
    gate_root.mkdir()
    host_root.mkdir()
    roots = {
        om.SOURCE_GATE: (str(gate_root),),
        om.SOURCE_HOST: (str(host_root),),
    }

    def failed_enumerator(_roots):
        raise OSError("sanitized traversal failure")

    state = om.measure(
        roots,
        _config(roots=roots),
        now=T0 + timedelta(days=2),
        enumerate_files=failed_enumerator,
    )
    assert all(row.unreadable_files == 1 for row in state.source_health)
    assert all(not row.complete for row in state.candidate_completeness)
    assert {
        marker.source
        for marker in state.loss_markers
        if marker.reason == "source_traversal_error"
    } == set(om.SOURCES)


def test_terminal_full_hash_pass_detects_rewrite_after_stable_double_read():
    gate_path = FIXTURES / "gate-2026-08-03.sanitized.jsonl"
    host_path = FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl"
    roots = {
        om.SOURCE_GATE: (str(gate_path),),
        om.SOURCE_HOST: (str(host_path),),
    }
    gate_bytes, host_bytes = _current_measurement_bytes()
    content = {gate_path: gate_bytes, host_path: host_bytes}
    reads = {gate_path: 0, host_path: 0}

    def reader(path):
        reads[path] += 1
        if path == gate_path and reads[path] >= 3:
            return content[path] + b" "
        return content[path]

    state = om.measure(
        roots,
        _config(roots=roots),
        project_proof_context=_proof_context(),
        now=T0 + timedelta(days=2),
        read_bytes=reader,
        enumerate_files=lambda items: tuple(Path(item) for item in items),
    )
    gate_completeness = next(
        row
        for row in state.candidate_completeness
        if row.source == om.SOURCE_GATE
    )
    assert reads[gate_path] == 3
    assert gate_completeness.complete is False
    assert any(
        marker.reason == "final_hash_changed"
        and marker.source == om.SOURCE_GATE
        for marker in state.loss_markers
    )


def test_malformed_region_makes_candidate_completeness_false(tmp_path):
    gate_root = tmp_path / "gate"
    host_root = tmp_path / "host"
    gate_root.mkdir()
    host_root.mkdir()
    gate_path = gate_root / "gate-2026-08-04.log"
    host_path = host_root / "rollout-2026-08-04.jsonl"
    gate_bytes, host_bytes = _current_measurement_bytes()
    gate_path.write_bytes(gate_bytes + b"not-json\n")
    host_path.write_bytes(host_bytes)
    roots = {
        om.SOURCE_GATE: (str(gate_root),),
        om.SOURCE_HOST: (str(host_root),),
    }
    state = om.measure(
        roots,
        _config(roots=roots),
        project_proof_context=_proof_context(),
        now=T0 + timedelta(days=2),
        evidence_resolver=lambda _receipts, _stable_bytes, _config: (
            om.OutcomeEvidence(
                nonce="corpus-mutated-current",
                session_id="sanitized-current-session",
                observable=True,
                evidence_available=True,
                adapter="codex",
                project_fingerprint=_proof()["fingerprint"],
                progress_inserts=1,
            ),
        ),
    )
    gate_health = next(
        row for row in state.source_health if row.source == om.SOURCE_GATE
    )
    gate_completeness = next(
        row
        for row in state.candidate_completeness
        if row.source == om.SOURCE_GATE
    )
    assert gate_health.malformed_regions == 1
    assert gate_completeness.complete is False


def test_unparseable_region_scope_uses_content_range_then_file_date():
    cases = (
        ("gate-2026-07-01.log", False),
        ("gate-2026-08-02.log", True),
        ("/archive/2020-01-01/gate-2026-08-02.log", True),
        ("gate-undated.log", True),
    )
    for file, expected in cases:
        rows, markers, _checks = om.parse_gate_log_bytes(
            b"not-json\n", file=file, config=_config()
        )
        assert not rows
        assert len(markers) == 1
        assert markers[0].in_scope is expected

    historical_meta = json.loads(
        (
            FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl"
        ).read_text().splitlines()[0]
    )
    historical_meta["timestamp"] = "2026-07-01T00:00:00Z"
    historical_meta["payload"]["timestamp"] = "2026-07-01T00:00:00Z"
    historical_after = json.loads(json.dumps(historical_meta))
    historical_after["timestamp"] = "2026-07-01T00:01:00Z"
    historical_after["payload"]["timestamp"] = "2026-07-01T00:01:00Z"
    bracketed = (
        json.dumps(historical_meta, separators=(",", ":")).encode()
        + b"\nnot-json\n"
        + json.dumps(historical_after, separators=(",", ":")).encode()
        + b"\n"
    )
    host_rows, host_markers = om.parse_host_record_bytes(
        bracketed,
        file="rollout-2026-07-01.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert not host_rows
    assert len(host_markers) == 1
    assert host_markers[0].in_scope is False

    # An old valid prefix cannot prove that a malformed tail was historical;
    # long-lived S2 files can resume years after their path date.
    _rows, tail_markers = om.parse_host_record_bytes(
        bracketed.rsplit(b"not-json\n", 1)[0] + b"not-json\n",
        file="rollout-2026-07-01.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert len(tail_markers) == 1
    assert tail_markers[0].in_scope is True

    # A resumed rollout's current content outranks its historical path date.
    session_meta = json.loads(
        (
            FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl"
        ).read_text().splitlines()[0]
    )
    current_ts = (T0 + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    session_meta["timestamp"] = current_ts
    session_meta["payload"]["timestamp"] = current_ts
    resumed = (
        json.dumps(session_meta, separators=(",", ":")).encode()
        + b"\nnot-json\n"
    )
    _rows, resumed_markers = om.parse_host_record_bytes(
        resumed,
        file="rollout-2026-07-01.jsonl",
        config=_config(),
        project_proof_context=_proof_context(),
    )
    assert len(resumed_markers) == 1
    assert resumed_markers[0].in_scope is True


def test_historical_malformed_files_do_not_dirty_measurement_health_or_o2(
    tmp_path,
):
    gate_root = tmp_path / "gate"
    host_root = tmp_path / "host"
    gate_root.mkdir()
    host_root.mkdir()
    gate_bytes, host_bytes = _current_measurement_bytes()
    (gate_root / "gate-2026-08-01.log").write_bytes(gate_bytes)
    (host_root / "rollout-2026-08-01.jsonl").write_bytes(host_bytes)
    (gate_root / "gate-2026-07-01.log").write_bytes(b"not-json\n")
    historical_meta = json.loads(
        (
            FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl"
        ).read_text().splitlines()[0]
    )
    historical_meta["timestamp"] = "2026-07-01T00:00:00Z"
    historical_meta["payload"]["timestamp"] = "2026-07-01T00:00:00Z"
    historical_after = json.loads(json.dumps(historical_meta))
    historical_after["timestamp"] = "2026-07-01T00:01:00Z"
    historical_after["payload"]["timestamp"] = "2026-07-01T00:01:00Z"
    (host_root / "rollout-2026-07-01.jsonl").write_bytes(
        json.dumps(historical_meta, separators=(",", ":")).encode()
        + b"\nnot-json\n"
        + json.dumps(historical_after, separators=(",", ":")).encode()
        + b"\n"
    )
    roots = {
        om.SOURCE_GATE: (str(gate_root),),
        om.SOURCE_HOST: (str(host_root),),
    }

    def resolver(_receipts, _stable_bytes, _config):
        return (
            om.OutcomeEvidence(
                nonce="corpus-mutated-current",
                session_id="sanitized-current-session",
                observable=True,
                evidence_available=True,
                adapter="codex",
                project_fingerprint=_proof()["fingerprint"],
                progress_inserts=1,
            ),
        )

    state = om.measure(
        roots,
        _config(roots=roots),
        project_proof_context=_proof_context(),
        now=T0 + timedelta(days=2),
        evidence_resolver=resolver,
    )
    historical_markers = [
        marker
        for marker in state.loss_markers
        if marker.reason == "schema_invalid"
    ]
    assert len(historical_markers) == 2
    assert all(not marker.in_scope for marker in historical_markers)
    assert all(row.malformed_regions == 0 for row in state.source_health)
    assert all(row.clean for row in state.source_health)
    assert all(row.complete for row in state.candidate_completeness)
    assert om.compute_oracles(state).o2 == "pass"


@pytest.mark.parametrize(
    "malformed_name", ["gate-2026-08-02.log", "gate-undated.log"]
)
def test_in_window_or_unplaceable_malformed_file_blocks_measurement(
    tmp_path, malformed_name
):
    gate_root = tmp_path / "gate"
    host_root = tmp_path / "host"
    gate_root.mkdir()
    host_root.mkdir()
    gate_bytes, host_bytes = _current_measurement_bytes()
    (gate_root / "gate-2026-08-01.log").write_bytes(gate_bytes)
    (host_root / "rollout-2026-08-01.jsonl").write_bytes(host_bytes)
    (gate_root / malformed_name).write_bytes(b"not-json\n")
    roots = {
        om.SOURCE_GATE: (str(gate_root),),
        om.SOURCE_HOST: (str(host_root),),
    }
    state = om.measure(
        roots,
        _config(roots=roots),
        project_proof_context=_proof_context(),
        now=T0 + timedelta(days=2),
        evidence_resolver=lambda _receipts, _stable_bytes, _config: (
            om.OutcomeEvidence(
                nonce="corpus-mutated-current",
                session_id="sanitized-current-session",
                observable=True,
                evidence_available=True,
                adapter="codex",
                project_fingerprint=_proof()["fingerprint"],
                progress_inserts=1,
            ),
        ),
    )
    gate_health = next(
        row for row in state.source_health if row.source == om.SOURCE_GATE
    )
    gate_completeness = next(
        row
        for row in state.candidate_completeness
        if row.source == om.SOURCE_GATE
    )
    assert gate_health.malformed_regions == 1
    assert gate_completeness.complete is False
    assert any(
        marker.reason == "schema_invalid" and marker.in_scope
        for marker in state.loss_markers
    )
    assert om.compute_oracles(state).o2 == "indeterminate"


def test_long_lived_s2_malformed_tail_is_conservatively_in_scope(tmp_path):
    gate_root = tmp_path / "gate"
    host_root = tmp_path / "host"
    gate_root.mkdir()
    host_root.mkdir()
    gate_bytes, host_bytes = _current_measurement_bytes()
    (gate_root / "gate-2026-08-01.log").write_bytes(gate_bytes)
    (host_root / "rollout-2026-08-01.jsonl").write_bytes(host_bytes)
    historical_meta = json.loads(
        (
            FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl"
        ).read_text().splitlines()[0]
    )
    historical_meta["timestamp"] = "2020-01-01T00:00:00Z"
    historical_meta["payload"]["timestamp"] = "2020-01-01T00:00:00Z"
    (host_root / "rollout-2020-01-01.jsonl").write_bytes(
        json.dumps(historical_meta, separators=(",", ":")).encode()
        + b"\nnot-json\n"
    )
    roots = {
        om.SOURCE_GATE: (str(gate_root),),
        om.SOURCE_HOST: (str(host_root),),
    }
    state = om.measure(
        roots,
        _config(roots=roots),
        project_proof_context=_proof_context(),
        now=T0 + timedelta(days=2),
        evidence_resolver=lambda _receipts, _stable_bytes, _config: (
            om.OutcomeEvidence(
                nonce="corpus-mutated-current",
                session_id="sanitized-current-session",
                observable=True,
                evidence_available=True,
                adapter="codex",
                project_fingerprint=_proof()["fingerprint"],
                progress_inserts=1,
            ),
        ),
    )
    host_health = next(
        row for row in state.source_health if row.source == om.SOURCE_HOST
    )
    host_completeness = next(
        row
        for row in state.candidate_completeness
        if row.source == om.SOURCE_HOST
    )
    assert host_health.malformed_regions == 1
    assert host_completeness.complete is False
    assert any(
        marker.source == om.SOURCE_HOST
        and marker.reason == "schema_invalid"
        and marker.in_scope
        for marker in state.loss_markers
    )
    assert om.compute_oracles(state).o2 == "indeterminate"


def test_report_redacts_all_paths_hashes_marker_details_and_raw_sessions():
    secret_root = "/private/sensitive/project-root"
    secret_session = "raw-session-should-never-render"
    secret_files = {
        om.SOURCE_GATE: f"{secret_root}/gate-2026-08-04.log",
        om.SOURCE_HOST: f"{secret_root}/rollout-2026-08-04.jsonl",
    }
    secret_marker_file = f"{secret_root}/private-loss-region.jsonl"
    secret_hash = "b" * 64
    base = _state(_pair("report-redact", 1, session=secret_session))
    receipt = replace(
        base.receipts[0],
        observations=tuple(
            replace(row, file=secret_files[row.source])
            for row in base.receipts[0].observations
        ),
        session_id=secret_session,
    )
    health = tuple(
        om.SourceHealth(
            source=source,
            roots=(secret_root,),
            files_seen=1,
            files_parsed=1,
        )
        for source in om.SOURCES
    )
    completeness = tuple(
        om.CandidateCompletenessReceipt(
            source=source,
            roots=(secret_root,),
            enumerated_files=(secret_files[source],),
            stable_file_hashes=((secret_files[source], secret_hash),),
            complete=True,
        )
        for source in om.SOURCES
    )
    marker = om.LossMarker(
        "schema_invalid",
        source=om.SOURCE_GATE,
        file=secret_marker_file,
        session_id=secret_session,
        detail=f"path={secret_marker_file};session={secret_session}",
    )
    state = replace(
        base,
        receipts=(receipt,),
        source_health=health,
        candidate_completeness=completeness,
        loss_markers=(marker,),
    )
    rendered = om.render_report(state, om.compute_oracles(state))
    for secret in (
        secret_root,
        *secret_files.values(),
        secret_marker_file,
        secret_hash,
        secret_session,
    ):
        assert secret not in rendered
    payload = json.loads(rendered)
    assert payload["receipts"][0]["session_tokens"] == {
        om.SOURCE_GATE: "S1-session-0001",
        om.SOURCE_HOST: "S2-session-0001",
    }
    assert payload["loss_markers"][0]["file_token"].startswith("S1-file-")
    assert payload["loss_markers"][0]["session_token"] == "S1-session-0001"
    assert payload["loss_markers"][0]["detail_present"] is True


def test_frozen_fixture_pack_requires_exact_membership_protocol_and_provenance():
    fixture_bytes = {
        name: (FIXTURES / name).read_bytes()
        for name in om.FROZEN_FIXTURE_PACK_SHA256
    }
    assert {
        name: hashlib.sha256(data).hexdigest()
        for name, data in fixture_bytes.items()
    } == om.FROZEN_FIXTURE_PACK_SHA256
    config = _config()

    def manifest_for(protocol):
        manifest = om.MeasurementManifest(
            contract_sha256=om.CONTRACT_SHA256,
            ratification_node_ids=om.RATIFICATION_NODE_IDS,
            implementation_commit=config.implementation_commit,
            measurement_protocol_version=protocol,
            source_roots=config.source_roots,
            fixture_hashes=om.FROZEN_FIXTURE_PACK_SHA256,
            composite_sha256="",
        )
        return replace(
            manifest, composite_sha256=manifest.expected_composite()
        )

    errors = om.verify_pins(
        contract_bytes=b"tampered contract checked independently",
        capture=om.CapturePin(om.CAPTURE_NODE_ID, om.CONTRACT_SHA256),
        manifest=manifest_for(om.MEASUREMENT_PROTOCOL_VERSION),
        config=config,
        fixture_bytes=fixture_bytes,
    )
    assert not {
        "manifest_fixture_pack_mismatch",
        "fixture_pack_membership_mismatch",
        "fixture_provenance_invalid",
        "manifest_protocol_not_frozen",
    }.intersection(errors)

    bumped_config = _config(protocol="outcome-v2.6.1")
    bumped_errors = om.verify_pins(
        contract_bytes=b"tampered contract checked independently",
        capture=om.CapturePin(om.CAPTURE_NODE_ID, om.CONTRACT_SHA256),
        manifest=manifest_for("outcome-v2.6.1"),
        config=bumped_config,
        fixture_bytes=fixture_bytes,
    )
    assert "manifest_protocol_not_frozen" in bumped_errors

    missing_member = dict(fixture_bytes)
    missing_member.pop("golden-report-v2.6.json")
    missing_errors = om.verify_pins(
        contract_bytes=b"tampered contract checked independently",
        capture=om.CapturePin(om.CAPTURE_NODE_ID, om.CONTRACT_SHA256),
        manifest=manifest_for(om.MEASUREMENT_PROTOCOL_VERSION),
        config=config,
        fixture_bytes=missing_member,
    )
    assert "fixture_pack_membership_mismatch" in missing_errors

    bad_provenance = dict(fixture_bytes)
    provenance = json.loads(bad_provenance["manifest.json"])
    first = next(iter(provenance["fixtures"].values()))
    first["sha256"] = "0" * 64
    bad_provenance["manifest.json"] = json.dumps(provenance).encode()
    provenance_errors = om.verify_pins(
        contract_bytes=b"tampered contract checked independently",
        capture=om.CapturePin(om.CAPTURE_NODE_ID, om.CONTRACT_SHA256),
        manifest=manifest_for(om.MEASUREMENT_PROTOCOL_VERSION),
        config=config,
        fixture_bytes=bad_provenance,
    )
    assert "fixture_provenance_invalid" in provenance_errors


def test_lineage_order_key_and_project_proof_version_fail_closed_at_construction():
    with pytest.raises(ValueError, match="lineage_order_key is required"):
        replace(_receipt("missing-order", 1), lineage_order_key=None)
    with pytest.raises(ValueError, match="lineage_order_key is required"):
        om.ReceiptLineage(
            nonce="missing-lineage-order",
            admitted=True,
            lineage_order_key=None,
            measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
        )
    with pytest.raises(ValueError, match="frozen version"):
        replace(_config(), project_proof_version="project-proof-v999")


def test_local_unsanitized_corpus_floor_or_skip_only_when_roots_absent():
    if os.environ.get("LATCH_RUN_LIVE_OUTCOME_CONFORMANCE") != "1":
        pytest.skip("opt-in unsanitized production-parser conformance")

    gate_root = Path(
        os.environ.get("LATCH_LIVE_GATE_ROOT", Path.home() / "repos" / "latch-vault")
    )
    codex_root = Path(
        os.environ.get(
            "LATCH_LIVE_CODEX_ROOT",
            os.environ.get(
                "LATCH_LIVE_HOST_ROOT", Path.home() / ".codex" / "sessions"
            ),
        )
    )
    claude_root = Path(
        os.environ.get(
            "LATCH_LIVE_CLAUDE_ROOT", Path.home() / ".claude" / "projects"
        )
    )
    assert gate_root.exists() and codex_root.exists() and claude_root.exists()
    gate_files, gate_errors = om.discover_source_files(
        (gate_root,), om.SOURCE_GATE
    )
    codex_files, codex_errors = om.discover_source_files(
        (codex_root,), om.SOURCE_HOST
    )
    claude_files, claude_errors = om.discover_source_files(
        (claude_root,), om.SOURCE_HOST
    )
    assert gate_errors == codex_errors == claude_errors == 0
    assert len(gate_files) >= 1
    assert len(codex_files) >= 1
    assert len(claude_files) >= 1

    config = _config()
    max_files = 256
    max_total_bytes = 64 * 1024 * 1024
    max_file_bytes = 16 * 1024 * 1024

    gate_parsed_files = gate_records = gate_bytes_seen = 0
    for path in sorted(
        gate_files, key=lambda item: item.stat().st_mtime, reverse=True
    )[:max_files]:
        size = path.stat().st_size
        if size > max_file_bytes or gate_bytes_seen + size > max_total_bytes:
            continue
        data = path.read_bytes()
        gate_bytes_seen += len(data)
        rows, _markers, checks = om.parse_gate_log_bytes(
            data, file=path.name, config=config
        )
        gate_parsed_files += 1
        gate_records += max(len(rows), len(checks))
        if gate_records:
            break

    def host_floor(files):
        parsed_files = calls = bytes_seen = 0
        for path in sorted(
            files, key=lambda item: item.stat().st_mtime, reverse=True
        )[:max_files]:
            size = path.stat().st_size
            if size > max_file_bytes or bytes_seen + size > max_total_bytes:
                continue
            data = path.read_bytes()
            bytes_seen += len(data)
            if b"latch_gate" not in data and b"kb_gate" not in data:
                continue
            rows, _markers = om.parse_host_record_bytes(
                data,
                file=path.name,
                config=config,
            )
            parsed_files += 1
            calls += len(rows)
            if calls:
                break
        return parsed_files, calls

    codex_parsed_files, codex_calls = host_floor(codex_files)
    claude_parsed_files, claude_calls = host_floor(claude_files)
    assert gate_parsed_files >= 1 and gate_records >= 1
    assert codex_parsed_files >= 1 and codex_calls >= 1
    assert claude_parsed_files >= 1 and claude_calls >= 1
