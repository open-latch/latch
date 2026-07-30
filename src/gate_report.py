#!/usr/bin/env python3
"""Read-only report over recent kb_gate structural logs."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import log_utils  # noqa: E402


DEFAULT_DAYS = 14
DEFAULT_LIMIT = 10


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
    outcomes = _latest_version_only(_read("gate_outcome", start, end, project_path))

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
    return out


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
    no_session = sum(1 for row in gates if not row.get("session_id"))
    # The correlator refuses three row shapes outright, so none of them can ever
    # be labeled and none belongs in the labelable denominator: a missing
    # session id, a skipped verdict, and an unparseable timestamp. Counting them
    # would understate how the machinery performs on the rows it can actually
    # see — the opposite of the honesty this block exists for. Each is reported
    # separately so the gap is attributable, not just subtracted.
    skipped = sum(
        1 for row in gates if row.get("skipped") and row.get("session_id")
    )
    bad_ts = sum(
        1 for row in gates
        if row.get("session_id")
        and not row.get("skipped")
        and _parse_gate_ts(row.get("ts")) is None
    )
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
        "ambiguous_pct": (
            round(100 * ambiguous / labeled, 1) if labeled else None
        ),
    }


def _parse_gate_ts(value: Any) -> datetime | None:
    """Parse a gate row's ISO-8601 ``ts``, mirroring the correlator's parser so
    the report's unlabelable count matches what the correlator actually skips."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _gate_call_identity(row: dict[str, Any]) -> tuple[Any, ...] | None:
    """Which gate call an outcome row belongs to, or None when unknowable.

    Prefers the ``gate_call_id`` nonce. Hash-plus-timestamp is a fallback for
    pre-nonce rows only: a repeated identical request shares its ``query_hash``,
    so keying on the hash collapses a MODIFY and its retry into one row and
    undercounts real gate calls (KB id=3310). This must stay consistent with
    ``correlator._dedup_key`` — the writer and the reader have to agree on what
    "one gate call" means, or the report silently disagrees with the log.
    """
    call_id = row.get("gate_call_id")
    if isinstance(call_id, str) and call_id:
        return ("call", call_id)
    query_hash = row.get("gate_query_hash")
    gate_ts = row.get("gate_ts")
    if query_hash is None or gate_ts is None:
        return None
    return ("hash", query_hash, gate_ts)


def _latest_version_only(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one outcome row per gate call — the highest ``correlator_version``.

    A correlator version bump re-emits rows into the SAME daily files under the
    new classification, and dedup keys on the version, so both generations
    coexist on disk by design. Reading them all would double-count every
    outcome and inflate the very numbers this report exists to measure.
    """
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    for row in rows:
        key = _gate_call_identity(row)
        if key is None:
            unkeyed.append(row)
            continue
        prior = best.get(key)
        if prior is None or _version_key(
            row.get("correlator_version"),
        ) >= _version_key(prior.get("correlator_version")):
            best[key] = row
    return list(best.values()) + unkeyed


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
    amb = coverage.get("ambiguous_pct")
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
            f"- AMBIGUOUS: {_int(coverage.get('ambiguous_rows'))} of "
            f"{labeled} labeled ({amb}%)"
        )
    lines.append(
        "- A low rate here measures how much gate traffic carries a "
        "correlatable identity, not whether the gate works."
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
    if "rejected" in lowered or "discarded" in lowered or "ruled" in lowered:
        return (
            "This kept abandoned paths visible, which is the part generic memory "
            "systems usually lose."
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
