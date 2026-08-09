#!/usr/bin/env python3
"""Read-only report over recent kb_gate structural logs."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import log_utils  # noqa: E402
import outcome_measurement  # noqa: E402


DEFAULT_DAYS = 14
DEFAULT_LIMIT = 10
MEASUREMENT_PROTOCOL_VERSION = outcome_measurement.MEASUREMENT_PROTOCOL_VERSION


def assemble_gate_report(
    conn,
    *,
    project_path: str | os.PathLike | None = None,
    start: date | None = None,
    end: date | None = None,
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Assemble a read-only report from gate/adversary/decision/outcome logs."""
    start, end = _date_window(start=start, end=end, days=days)
    gates = _read("gate", start, end, project_path)
    adversary = _read("adversary", start, end, project_path)
    decisions = _read("decision", start, end, project_path)
    outcomes = _read("gate_outcome", start, end, project_path)
    measurement_outcomes = _latest_version_only(
        outcomes,
        measurement_protocol_version=MEASUREMENT_PROTOCOL_VERSION,
    )

    evidence_counts = Counter(
        int(node_id)
        for row in gates
        for node_id in row.get("evidence_ids") or []
        if _is_intish(node_id)
    )
    chain_counts = Counter(
        int(node_id)
        for row in gates
        for node_id in row.get("decision_chain") or []
        if _is_intish(node_id)
    )
    node_ids = set(evidence_counts) | set(chain_counts)
    nodes = _node_map(conn, node_ids)
    top_evidence = _ranked_nodes(nodes, evidence_counts, limit=limit)
    top_chain = _ranked_nodes(nodes, chain_counts, limit=limit)
    priorities = [
        row for row in top_evidence
        if row.get("kind") == "priority" and row.get("status") != "missing"
    ][:limit]

    verdict_counts = _label_counts(row.get("recommendation") for row in gates)
    outcome_counts = _label_counts(row.get("outcome_category") for row in outcomes)
    outcome_by_verdict_counts = _nested_label_counts(
        outcomes,
        outer_key="verdict",
        inner_key="outcome_category",
    )
    adversary_delta_counts = _label_counts(row.get("verdict_delta") for row in adversary)
    human_action_counts = _label_counts(row.get("human_action") for row in decisions)

    evidence_type_counts = _sum_nested_counts(gates, "evidence_type_counts")
    gap_type_counts = _sum_nested_counts(gates, "gap_type_counts")
    claim_signals = {
        "load_bearing_claims": sum(_int(row.get("load_bearing_claim_count")) for row in gates),
        "uncovered_claims": sum(_int(row.get("uncovered_claim_count")) for row in gates),
        "evidence_type_counts": dict(evidence_type_counts),
        "gap_type_counts": dict(gap_type_counts),
    }
    coverage = _coverage(gates, outcomes)
    measurement = _measurement_quality(
        gates,
        measurement_outcomes,
        start=start,
        end=end,
        measurement_protocol_version=MEASUREMENT_PROTOCOL_VERSION,
    )
    used = {
        "gate_rows": len(gates),
        "adversary_rows": len(adversary),
        "decision_rows": len(decisions),
        "gate_outcome_rows": len(outcomes),
        "top_evidence_nodes": len(top_evidence),
        "priority_nodes": len(priorities),
    }
    summary = (
        f"Latch read {len(gates)} gate call(s), {len(adversary)} adversary row(s), "
        f"{len(outcomes)} outcome row(s), and {len(decisions)} decision signal(s) "
        f"from {start.isoformat()} through {end.isoformat()}."
    )
    return {
        "label": "Latch gate report",
        "source": "gate_report",
        "must_display_to_user": True,
        "summary": summary,
        "why_it_matters": (
            "This shows how latch has been applying project judgment over recent "
            "work using structural gate logs and current KB node authority, without "
            "reading raw prompts or writing new decisions."
        ),
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": (end - start).days + 1,
        },
        "used": used,
        "structural_only": True,
        "verdict_counts": verdict_counts,
        "coverage": coverage,
        "measurement": measurement,
        "outcome_counts": outcome_counts,
        "outcome_by_verdict_counts": outcome_by_verdict_counts,
        "adversary_delta_counts": adversary_delta_counts,
        "human_action_counts": human_action_counts,
        "claim_signals": claim_signals,
        "top_evidence_nodes": top_evidence,
        "top_decision_chain_nodes": top_chain,
        "priority_evidence": priorities,
    }


def format_text(report: dict[str, Any]) -> str:
    lines = [
        "# Latch Gate Report",
        "",
        _opening_sentence(report),
        "",
        *_course_correction_lines(report),
        "",
        *_claim_story_lines(report),
        "",
        (
            f"Window: {report['window']['start']} to {report['window']['end']} "
            f"UTC ({report['window']['days']} day(s))"
        ),
        f"Why this matters: {report['why_it_matters']}",
        "",
        "## Gate Activity Snapshot",
    ]
    _append_counts(lines, "Verdicts", report.get("verdict_counts") or {})
    _append_counts(lines, "Outcomes", report.get("outcome_counts") or {})
    _append_counts(lines, "Adversary deltas", report.get("adversary_delta_counts") or {})
    _append_counts(lines, "Human actions", report.get("human_action_counts") or {})

    claim_signals = report.get("claim_signals") or {}
    lines.extend([
        "",
        "## Claim Grounding",
        f"- Load-bearing claims observed: {claim_signals.get('load_bearing_claims', 0)}",
        f"- Uncovered claims observed: {claim_signals.get('uncovered_claims', 0)}",
    ])
    _append_counts(lines, "Evidence types", claim_signals.get("evidence_type_counts") or {})
    _append_counts(lines, "Gap types", claim_signals.get("gap_type_counts") or {})
    _append_coverage(lines, report.get("coverage") or {})
    _append_measurement(lines, report.get("measurement") or {})

    _append_nodes(
        lines,
        "Latch Evidence Leaderboard",
        report.get("top_evidence_nodes") or [],
        count_label="gate cite",
        include_commentary=True,
    )
    _append_nodes(
        lines,
        "What Latch Kept You Focused On",
        report.get("priority_evidence") or [],
        count_label="gate cite",
    )
    _append_nodes(
        lines,
        "Top Decision-Chain Anchors",
        report.get("top_decision_chain_nodes") or [],
        count_label="chain cite",
    )
    if not report.get("top_evidence_nodes"):
        lines.extend([
            "",
            "No gate log rows were found in this window.",
        ])
    lines.extend([
        "",
        "## Report Boundary",
        "Source: structural gate/adversary/decision/outcome logs plus current KB node metadata.",
        "Privacy boundary: no raw prompts, no node bodies, and no new KB writes.",
        "Use this as a proof receipt for project judgment, not as analytics, RL, or a dashboard.",
    ])
    return "\n".join(lines) + "\n"


def _opening_sentence(report: dict[str, Any]) -> str:
    gates = _int((report.get("used") or {}).get("gate_rows"))
    days = _int((report.get("window") or {}).get("days"))
    window = f"{days}-day window" if days else "reporting window"
    return (
        f"In this {window}, structural logs recorded {gates} "
        f"{_plural(gates, 'gate call')}."
    )


def _course_correction_lines(report: dict[str, Any]) -> list[str]:
    verdicts = report.get("verdict_counts") or {}
    outcomes = report.get("outcome_counts") or {}
    by_verdict = report.get("outcome_by_verdict_counts") or {}
    modify = _int(verdicts.get("MODIFY"))
    accepted = _int(outcomes.get("ACCEPTED"))
    accepted_modify = _int((by_verdict.get("MODIFY") or {}).get("ACCEPTED"))
    overridden = _int(outcomes.get("OVERRIDDEN"))
    ambiguous = _int(outcomes.get("AMBIGUOUS"))
    if not any((modify, accepted, overridden, ambiguous)):
        return ["No course-correction outcomes were recorded in this window."]

    lines = [
        (
            f"Latch returned MODIFY on {modify} "
            f"{_plural(modify, 'gate call')}."
        )
    ]
    outcome_bits = []
    if accepted:
        accepted_phrase = f"{accepted} accepted {_plural(accepted, 'outcome')}"
        if accepted_modify:
            accepted_phrase += (
                f", including {accepted_modify} MODIFY "
                f"{_plural(accepted_modify, 'course correction')}"
            )
        outcome_bits.append(accepted_phrase)
    if overridden:
        outcome_bits.append(f"{overridden} overridden")
    if ambiguous:
        outcome_bits.append(f"{ambiguous} left ambiguous enough to keep watching")
    if outcome_bits:
        lines.append(f"Recent gate outcomes: {_join_human(outcome_bits)}.")
    return lines


def _claim_story_lines(report: dict[str, Any]) -> list[str]:
    claim_signals = report.get("claim_signals") or {}
    evidence_types = claim_signals.get("evidence_type_counts") or {}
    claims = _int(claim_signals.get("load_bearing_claims"))
    uncovered = _int(claim_signals.get("uncovered_claims"))
    kb_nodes = _int(evidence_types.get("kb_node"))
    user_input = _int(evidence_types.get("user_input"))
    code_trace = _int(evidence_types.get("code_trace"))
    grounded_bits = []
    if kb_nodes:
        grounded_bits.append(f"{kb_nodes} tied back to KB evidence")
    if user_input:
        grounded_bits.append(f"{user_input} grounded in direct user input")
    if code_trace:
        grounded_bits.append(f"{code_trace} backed by code trace evidence")

    lines = [
        (
            f"Latch checked {claims} {_plural(claims, 'load-bearing claim')} "
            "across those gate calls."
        )
    ]
    if grounded_bits:
        lines.append(f"Grounding found: {_join_human(grounded_bits)}.")
    if uncovered:
        lines.append(
            f"{uncovered} {_plural(uncovered, 'claim')} had no backing and stayed visible "
            "as uncovered instead of becoming silent assumptions."
        )
    return lines


def _date_window(
    *, start: date | None, end: date | None, days: int,
) -> tuple[date, date]:
    if days < 1:
        raise ValueError("days must be >= 1")
    if end is None:
        end = datetime.now(timezone.utc).date()
    if start is None:
        start = end - timedelta(days=days - 1)
    if start > end:
        raise ValueError("start must be before or equal to end")
    return start, end


def _read(
    stream: str,
    start: date,
    end: date,
    project_path: str | os.PathLike | None,
) -> list[dict[str, Any]]:
    return list(log_utils.read_log_range(stream, start, end, project_path))


def _node_map(conn, node_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    ids = sorted({int(node_id) for node_id in node_ids})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, kind, title, status, workstream_id FROM nodes WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    out = {int(row["id"]): dict(row) for row in rows}
    for node_id in ids:
        out.setdefault(
            node_id,
            {
                "id": node_id,
                "kind": "missing",
                "title": "(missing node)",
                "status": "missing",
                "workstream_id": None,
            },
        )
    _attach_rejected_path_counts(conn, out, ids)
    return out


def _attach_rejected_path_counts(
    conn, nodes: dict[int, dict[str, Any]], ids: list[int]
) -> None:
    """Stamp each node with how many typed rejections it records (id=3948 V2).

    Replaces the keyword test this report used to run on node titles. Measured
    on the live vault, that test was 50% precision / 50% recall against the
    pre-registered rubric (docs/v2_rejection_rubric.md): it missed rejections
    phrased "X instead of Y" and fired on any node whose title merely discussed
    rejection — including Latch's own roadmap nodes describing this feature.

    Degrades to 0 rather than raising when the table is absent, so a report run
    against a vault an older engine created still works.
    """
    for node in nodes.values():
        node.setdefault("rejected_path_count", 0)
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"SELECT node_id, COUNT(*) AS n FROM rejected_path "
            f"WHERE node_id IN ({placeholders}) GROUP BY node_id",
            ids,
        ).fetchall()
    except sqlite3.Error:
        return
    for row in rows:
        node = nodes.get(int(row["node_id"]))
        if node is not None:
            node["rejected_path_count"] = int(row["n"])


def _ranked_nodes(
    nodes: dict[int, dict[str, Any]],
    counts: Counter[int],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for node_id, count in counts.most_common(max(0, limit)):
        node = dict(nodes.get(node_id) or {})
        if not node:
            continue
        node["count"] = int(count)
        ranked.append(node)
    return ranked


def _coverage(
    gates: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    """How much of the gate record actually carries an outcome label.

    The denominator is ALL gate rows, not just the labelable ones — a coverage
    number computed over rows that already have a session id would read near
    100% while ignoring most of the traffic. ``unlabelable_no_session_id`` is
    published alongside so the gap is visible in the same breath as the rate,
    and ``coverage_pct_of_labelable`` shows how the machinery performs on the
    rows it can actually see. Counts are always present; percentages are None
    rather than 0 on an empty window, so "no data" never reads as "0%".
    """
    total = len(gates)
    labeled = len(outcomes)
    # A gate row the host left session-less may still have been labeled, via
    # attribution recovered from the host's transcript. Counting those as
    # unlabelable would shrink the labelable denominator below the labeled
    # count and print an impossible rate — "200% of labelable rows are
    # labeled" — in the one block whose whole job is to be honest about
    # coverage. Identify the rows that actually produced an outcome and take
    # them out of every unlabelable bucket.
    labeled_ids = {
        key for key in (_gate_call_identity(row) for row in outcomes)
        if key is not None
    }

    def _was_labeled(row: dict[str, Any]) -> bool:
        key = _gate_call_identity({
            "gate_call_id": row.get("gate_call_id"),
            "gate_query_hash": row.get("query_hash"),
            "gate_ts": row.get("ts"),
        })
        return key is not None and key in labeled_ids

    # Classify each UNLABELED row into exactly one unlabelable bucket. The
    # buckets must be disjoint: a row that is both skipped and session-less
    # would otherwise be subtracted twice, which can drive `labelable` negative
    # and render a rate above 100% — in the one block whose whole job is to be
    # honest about coverage. Labeled rows are labelable by definition and are
    # never bucketed, which is what keeps a transcript-recovered row from being
    # counted as unlabelable.
    no_session = 0
    skipped = 0
    bad_ts = 0
    for row in gates:
        if _was_labeled(row):
            continue
        if row.get("skipped"):
            skipped += 1
        elif not row.get("session_id"):
            no_session += 1
        elif _parse_gate_ts(row.get("ts")) is None:
            bad_ts += 1
    labelable = total - no_session - skipped - bad_ts
    ambiguous = sum(
        1 for row in outcomes if row.get("outcome_category") == "AMBIGUOUS"
    )
    return {
        "gate_rows": total,
        "labeled_rows": labeled,
        "unlabelable_no_session_id": no_session,
        "unlabelable_skipped_verdict": skipped,
        "unlabelable_unparseable_ts": bad_ts,
        "labelable_rows": labelable,
        "coverage_pct": round(100 * labeled / total, 1) if total else None,
        "coverage_pct_of_labelable": (
            round(100 * labeled / labelable, 1) if labelable > 0 else None
        ),
        "ambiguous_rows": ambiguous,
        "ambiguous_rows_raw": ambiguous,
        "ambiguous_pct": (
            round(100 * ambiguous / labeled, 1) if labeled else None
        ),
        "ambiguous_pct_raw": (
            round(100 * ambiguous / labeled, 1) if labeled else None
        ),
        # Identity provenance of the labeled rows. Rows recovered from a host
        # transcript are real thread ids joined by content, not guesses — but
        # they are a different confidence class from host-supplied identity, so
        # a coverage number must be splittable rather than blended (id=4018).
        "labeled_by_session_source": _label_counts(
            row.get("session_source") for row in outcomes
        ),
        # Rows whose window contained a gate that could not be attributed, so
        # the boundary was unknowable. Surfaced as a count because a quality
        # rate computed over these is measuring a degraded window, not the
        # thing it claims to measure.
        "uncertain_boundary_rows": sum(
            1 for row in outcomes if row.get("window_boundary_uncertain")
        ),
    }


def _parse_gate_ts(value: Any) -> datetime | None:
    """Parse a gate row's ISO-8601 ``ts``, mirroring the correlator's parser so
    the report's unlabelable count matches what the correlator actually skips."""
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _gate_call_identity(row: dict[str, Any]) -> tuple[Any, ...] | None:
    """Which gate call an outcome row belongs to, or None when unknowable.

    The nonce is the invocation identity. A pre-nonce observation is a loss
    marker under v2.6, not a hash-promoted invocation.
    """
    call_id = row.get("gate_call_id")
    if isinstance(call_id, str) and call_id:
        return ("call", call_id)
    return None


def _latest_version_only(
    rows: list[dict[str, Any]],
    *,
    measurement_protocol_version: str = MEASUREMENT_PROTOCOL_VERSION,
) -> list[dict[str, Any]]:
    """Read exactly one pinned measurement generation.

    Correlator implementation semver is not a measurement protocol.  Selecting
    the numerically newest correlator row can mix or silently replace protocol
    generations, so v2.6 filters the exact pin and keys receipts by
    ``(invocation, measurement_protocol_version)``.
    """
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        if row.get("measurement_protocol_version") != measurement_protocol_version:
            continue
        key = _gate_call_identity(row)
        if key is None:
            continue
        prior = best.get(key)
        if prior is None:
            best[key] = dict(row)
        elif prior != row:
            # Impossible under immutable receipts. Keep one structural row for
            # rendering but force the oracle adapter to invalidate the audit.
            best[key]["generation_conflict"] = True
    return list(best.values())


def _version_key(value: Any) -> tuple[int, ...]:
    """Sortable tuple for a dotted version string; unparseable sorts lowest."""
    if not isinstance(value, str):
        return (-1,)
    parts: list[int] = []
    for chunk in value.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            return (-1,)
    return tuple(parts) or (-1,)


def _measurement_quality(
    gates: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    *,
    start: date,
    end: date,
    measurement_protocol_version: str = MEASUREMENT_PROTOCOL_VERSION,
    loss_markers: Iterable[dict[str, Any] | outcome_measurement.LossMarker] = (),
    source_health: Iterable[dict[str, Any] | outcome_measurement.SourceHealth] = (),
    candidate_completeness: Iterable[
        dict[str, Any] | outcome_measurement.CandidateCompletenessReceipt
    ] = (),
) -> dict[str, Any]:
    """Run the frozen v2.6 oracle engine over one exact receipt generation."""
    source_health_rows = tuple(source_health)
    completeness_rows = tuple(candidate_completeness)
    configured_roots: dict[str, tuple[str, ...]] = {}
    for row in source_health_rows:
        source = row.source if isinstance(
            row, outcome_measurement.SourceHealth
        ) else row.get("source")
        roots = row.roots if isinstance(
            row, outcome_measurement.SourceHealth
        ) else row.get("roots", ())
        if source in outcome_measurement.SOURCES and roots:
            configured_roots[str(source)] = tuple(str(root) for root in roots)
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    cap_date = end + timedelta(days=1)
    requested_cap_dt = datetime(
        cap_date.year, cap_date.month, cap_date.day, tzinfo=timezone.utc
    )
    hard_invalidations: list[str] = []
    if requested_cap_dt - start_dt > timedelta(days=21):
        hard_invalidations.append("report_window_exceeds_21_day_measurement_cap")
    # Report selection already applied ``end``. MeasurementConfig retains the
    # frozen exact cap independently of that diagnostic selection window.
    cap_dt = start_dt + timedelta(days=21)

    # The report adapter does not persist or compare a project path. The opaque
    # placeholder only satisfies MeasurementConfig's type boundary; receipt
    # project proof was already classified before this arithmetic layer.
    report_proof = {
        "version": outcome_measurement.PROJECT_PROOF_VERSION,
        "key_epoch": "report-adapter",
        "fingerprint": "0" * 64,
    }
    config = outcome_measurement.MeasurementConfig(
        t0=start_dt,
        cap=cap_dt,
        target_project_proof=report_proof,
        key_epoch="report-adapter",
        pinned_runtime_version="report-adapter",
        measurement_protocol_version=measurement_protocol_version,
        source_roots=configured_roots,
    )

    selected = _latest_version_only(
        outcomes,
        measurement_protocol_version=measurement_protocol_version,
    )
    receipt_rows: list[dict[str, Any]] = []
    derived_markers: list[dict[str, Any] | outcome_measurement.LossMarker] = list(
        loss_markers
    )
    for row in selected:
        nonce = str(row.get("gate_call_id") or "unknown")
        if row.get("generation_conflict"):
            hard_invalidations.append(f"conflicting_duplicate_receipt:{nonce}")
        raw_order = row.get("lineage_order_key") or row.get("gate_ts")
        order_key = _parse_gate_ts(raw_order)
        if order_key is None:
            issue = "missing" if raw_order in (None, "") else "invalid"
            hard_invalidations.append(
                f"canonical_receipt_lineage_order_{issue}:{nonce}"
            )
            derived_markers.append({
                "reason": "schema_invalid",
                "nonce": nonce if nonce != "unknown" else None,
                "in_scope": True,
                "detail": f"receipt_lineage_order_{issue}",
            })
            continue
        disposition = row.get("measurement_disposition") or row.get("disposition")
        if disposition not in outcome_measurement.DISPOSITIONS:
            disposition = "loss_signal"
        receipt_rows.append({
            **row,
            "nonce": row.get("gate_call_id"),
            "disposition": disposition,
            "outcome": row.get("outcome_category"),
            # Missing canonical receipt state is never inferred from a legacy
            # diagnostic row. Core v2.6 receipts must assert all three flags.
            "admitted": row.get("admitted") is True,
            "lineage_order_key": order_key,
            "fresh_ts": row.get("fresh_ts") or row.get("gate_ts"),
            "finalized": row.get("finalized") is True,
            "prefix_member": row.get("prefix_member") is True,
        })

    gate_checks: list[dict[str, Any]] = []
    for index, row in enumerate(gates):
        missing = []
        for field in outcome_measurement.REQUIRED_ID_LIST_FIELDS:
            value = row.get(field)
            if not isinstance(value, list) or any(
                isinstance(item, bool) or not isinstance(item, int) for item in value
            ):
                missing.append(field)
        parsed_ts = _parse_gate_ts(row.get("ts"))
        gate_checks.append({
            "obs_id": (outcome_measurement.SOURCE_GATE, "gate_report", index),
            "ts": parsed_ts,
            "in_scope": True,
            "id_lists_valid": not missing,
            "missing_fields": missing,
        })
        if not row.get("gate_call_id"):
            derived_markers.append({
                "reason": "identity_missing",
                "source": outcome_measurement.SOURCE_GATE,
                "ts": parsed_ts,
                "in_scope": True,
            })
        elif parsed_ts is None:
            derived_markers.append({
                "reason": "schema_invalid",
                "source": outcome_measurement.SOURCE_GATE,
                "in_scope": True,
            })

    result = outcome_measurement.audit_rows(
        receipt_rows,
        config,
        gate_rows=gate_checks,
        loss_markers=derived_markers,
        source_health=source_health_rows,
        candidate_completeness=completeness_rows,
        hard_invalidations=hard_invalidations,
        # The report is a noncanonical diagnostic projection over a closed,
        # inclusive date range.  Its deterministic as-of coordinate is the
        # first UTC instant after that range; never ask the canonical oracle to
        # infer wall-clock provenance from process time.
        measurement_taken_at=requested_cap_dt,
    )
    if result.o3_pass is None:
        o3_status = "unevaluated"
    elif result.o2 != "pass":
        o3_status = "diagnostic-pass" if result.o3_pass else "diagnostic-fail"
    else:
        o3_status = "pass" if result.o3_pass else "fail"
    raw_total = sum(result.raw_label_counts.values())
    raw_ambiguous = result.raw_label_counts.get("AMBIGUOUS", 0)
    return {
        "diagnostic": True,
        "canonical": False,
        "envelope_verified": False,
        "diagnostic_status": "invalidated" if result.invalidated else "noncanonical",
        "measurement_protocol_version": measurement_protocol_version,
        "o1": result.o1_pass,
        "o2": result.o2,
        "o2_reasons": list(result.o2_reasons),
        "o3": result.o3_pass,
        "o3_status": o3_status,
        "o3_subordinated_to_o2": result.o2 != "pass",
        "eligible_n": result.eligible_n,
        "d_min": result.d_min,
        "raw_label_counts": dict(result.raw_label_counts),
        "clean_label_counts": dict(result.clean_label_counts),
        "raw_ambiguous_pct": (
            round(100.0 * raw_ambiguous / raw_total, 6) if raw_total else None
        ),
        "clean_ambiguous_pct": result.ambiguous_rate,
        "disposition_counts": dict(result.disposition_counts),
        "loss_marker_count": result.marker_count,
        "source_health_clean": result.source_health_clean,
        "v1_green": result.v1_green,
        "verdict": result.verdict,
        "quality_summary": result.quality_summary,
        "invalidated": result.invalidated,
        "invalidation_reasons": list(result.invalidation_reasons),
    }


def _label_counts(labels: Iterable[Any]) -> dict[str, int]:
    counts = Counter(_label(value) for value in labels)
    return {key: counts[key] for key in sorted(counts)}


def _sum_nested_counts(rows: list[dict[str, Any]], key: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        nested = row.get(key) or {}
        if not isinstance(nested, dict):
            continue
        for label, value in nested.items():
            counts[_label(label)] += _int(value)
    return counts


def _nested_label_counts(
    rows: Iterable[dict[str, Any]],
    *,
    outer_key: str,
    inner_key: str,
) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter[str]] = {}
    for row in rows:
        outer = _label(row.get(outer_key))
        inner = _label(row.get(inner_key))
        counts.setdefault(outer, Counter())[inner] += 1
    return {
        outer: {inner: counter[inner] for inner in sorted(counter)}
        for outer, counter in sorted(counts.items())
    }


def _append_counts(lines: list[str], label: str, counts: dict[str, int]) -> None:
    if not counts:
        lines.append(f"{label}: none")
        return
    rendered = ", ".join(f"{key}={value}" for key, value in counts.items())
    lines.append(f"{label}: {rendered}")


def _append_coverage(lines: list[str], coverage: dict[str, Any]) -> None:
    """Render the outcome-coverage block in the default text report.

    Kept in the normal report rather than JSON-only: a coverage number that only
    appears when someone asks for `--json` is a number nobody sees, and the
    unlabelable breakdown is the part that stops a low rate from being misread
    as the gate not working."""
    if not coverage:
        return
    total = _int(coverage.get("gate_rows"))
    if not total:
        return
    labeled = _int(coverage.get("labeled_rows"))
    labelable = _int(coverage.get("labelable_rows"))
    pct = coverage.get("coverage_pct")
    pct_labelable = coverage.get("coverage_pct_of_labelable")
    amb = coverage.get("ambiguous_pct_raw", coverage.get("ambiguous_pct"))
    lines.extend([
        "",
        "### Outcome Coverage",
        f"- Labeled: {labeled} of {total} gate "
        f"{_plural(total, 'row')}"
        + (f" ({pct}%)" if pct is not None else ""),
        f"- Labelable: {labelable} "
        f"{_plural(labelable, 'row')}"
        + (f"; {pct_labelable}% of those are labeled"
           if pct_labelable is not None else ""),
    ])
    unlabelable = [
        ("no session id", _int(coverage.get("unlabelable_no_session_id"))),
        ("skipped verdict", _int(coverage.get("unlabelable_skipped_verdict"))),
        ("unparseable timestamp",
         _int(coverage.get("unlabelable_unparseable_ts"))),
    ]
    present = [f"{label}: {count}" for label, count in unlabelable if count]
    if present:
        lines.append(f"- Not labelable — {', '.join(present)}")
    if amb is not None:
        lines.append(
            f"- Raw diagnostic AMBIGUOUS: "
            f"{_int(coverage.get('ambiguous_rows_raw', coverage.get('ambiguous_rows')))} "
            f"of {labeled} labeled ({amb}%); not a clean quality rate"
        )
    by_source = coverage.get("labeled_by_session_source") or {}
    # UNKNOWN is what _label_counts renders for a row with no session_source —
    # i.e. one written before this field existed. Those are almost certainly
    # host-supplied, but the row does not say so, and lumping them under
    # "recovered from transcript" made a vault where recovery never ran report
    # itself as 100% recovered. Unknown provenance is its own line.
    unknown = _int(by_source.get("UNKNOWN"))
    recovered = {
        k: v for k, v in by_source.items()
        if k not in ("host_supplied", "UNKNOWN")
    }
    if recovered or unknown:
        parts = [f"{_int(by_source.get('host_supplied'))} host-supplied"]
        if recovered:
            parts.append(
                "recovered from transcript — "
                + ", ".join(f"{k}={v}" for k, v in sorted(recovered.items()))
            )
        if unknown:
            parts.append(
                f"{unknown} unknown provenance (written before this was recorded)"
            )
        lines.append("- Identity: " + "; ".join(parts))
    uncertain = _int(coverage.get("uncertain_boundary_rows"))
    if uncertain:
        lines.append(
            f"- Degraded windows: {uncertain} labeled "
            f"{_plural(uncertain, 'row')} had an unattributable gate inside the "
            "window, so the boundary is unknown; exclude these from quality rates"
        )
    lines.append(
        "- A low rate here measures how much gate traffic carries a "
        "correlatable identity, not whether the gate works."
    )


def _append_measurement(lines: list[str], measurement: dict[str, Any]) -> None:
    """Render noncanonical arithmetic without implying audit authority."""
    if not measurement:
        return
    invalidated = measurement.get("invalidated") is True
    status = "INVALIDATED" if invalidated else "NONCANONICAL DIAGNOSTIC"
    lines.extend([
        "",
        "### Outcome Measurement v2.6 — Diagnostic (Noncanonical)",
        (
            f"- Status: {status} — gate-report arithmetic only; this view is "
            "not the frozen envelope audit and carries no canonical authority"
        ),
        (
            "- Receipt generation: "
            f"{measurement.get('measurement_protocol_version')} "
            "(diagnostic generation filter)"
        ),
        (
            f"- O1 field presence: {measurement.get('o1')}; "
            f"O2 coverage: {measurement.get('o2')} "
            f"(E={_int(measurement.get('eligible_n'))}, "
            f"D_min={_int(measurement.get('d_min'))})"
        ),
        (
            f"- O3 AMBIGUOUS: {measurement.get('o3_status')}"
            + (
                " — diagnostic only until O2 passes"
                if measurement.get("o3_subordinated_to_o2") else ""
            )
        ),
        (
            "- Quality counts — raw: "
            f"{measurement.get('raw_label_counts') or {}}; clean eligible: "
            f"{measurement.get('clean_label_counts') or {}}"
        ),
    ])
    invalidation_reasons = measurement.get("invalidation_reasons") or []
    if invalidated:
        lines.append(
            "- Invalidation reasons: "
            + (
                ", ".join(str(item) for item in invalidation_reasons)
                if invalidation_reasons
                else "unspecified"
            )
        )
    reasons = measurement.get("o2_reasons") or []
    if reasons:
        lines.append("- O2 reasons: " + ", ".join(str(item) for item in reasons))
    lines.append(
        "- Censored, uncertain, pilot, loss, and conflict rows never enter the "
        "clean quality cohort."
    )


def _append_nodes(
    lines: list[str],
    label: str,
    nodes: list[dict[str, Any]],
    *,
    count_label: str,
    include_commentary: bool = False,
) -> None:
    if not nodes:
        return
    lines.extend(["", f"## {label}"])
    for node in nodes:
        plural = "" if node.get("count") == 1 else "s"
        lines.append(
            f"- id={node['id']} [{node['kind']}/{node['status']}] "
            f"{node['title']} ({node['count']} {count_label}{plural})"
        )
        if include_commentary:
            lines.append(f"  Why it mattered: {_node_commentary(node)}")


def _label(value: Any) -> str:
    if value is None or value == "":
        return "UNKNOWN"
    return str(value)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return singular
    return plural or f"{singular}s"


def _join_human(parts: list[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def _node_commentary(node: dict[str, Any]) -> str:
    title = str(node.get("title") or "")
    lowered = title.lower()
    if "neutral" in lowered or "compile outward" in lowered or "cross-vendor" in lowered or "cursor" in lowered or "codex" in lowered:
        return (
            "This kept Latch pointed at a cross-tool judgment layer instead of a "
            "single-agent feature."
        )
    if "install" in lowered or "seed" in lowered or "first-value" in lowered or "first value" in lowered:
        return (
            "Recent gates kept pulling work back toward the first moment where a "
            "new user can feel Latch's value."
        )
    if "oss" in lowered or "first oss" in lowered:
        return (
            "This kept the work honest about the first public wedge and the boundary "
            "around launch scope."
        )
    if "wedge" in lowered or "proof-honest" in lowered or "p0" in lowered:
        return (
            "This kept the near-term surface narrow, evidence-backed, and hard to "
            "inflate into dashboard or platform sprawl."
        )
    if "decision" in lowered or "binding" in lowered:
        return (
            "This evidence kept the report anchored in explicit project judgment, "
            "not fuzzy remembered context."
        )
    rejections = _int(node.get("rejected_path_count"))
    if rejections:
        return (
            f"This node records {rejections} typed "
            f"{_plural(rejections, 'rejected path')}, which is the part generic "
            "memory systems usually lose."
        )
    if "roadmap" in lowered or "ordering" in lowered or "next-step" in lowered:
        return (
            "This tied recent gate activity back to sequencing, so current work "
            "stayed connected to what should happen next."
        )
    if "workstream" in lowered:
        return (
            "This connected the report to the active lane of work, not just isolated "
            "gate calls."
        )
    return (
        "This node was repeatedly cited as current authority in recent gate calls."
    )


def _is_intish(value: Any) -> bool:
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Show a read-only report over recent latch gate activity."
    )
    ap.add_argument("--project", default=os.getcwd(),
                    help="project directory whose KB/logs should be read (default: cwd)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"days to include when --start is omitted (default: {DEFAULT_DAYS})")
    ap.add_argument("--start", type=_parse_date,
                    help="inclusive start date YYYY-MM-DD")
    ap.add_argument("--end", type=_parse_date,
                    help="inclusive end date YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"number of top nodes to show (default: {DEFAULT_LIMIT})")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    conn = db.connect(args.project)
    try:
        report = assemble_gate_report(
            conn,
            project_path=args.project,
            start=args.start,
            end=args.end,
            days=args.days,
            limit=args.limit,
        )
    finally:
        conn.close()
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(format_text(report), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
