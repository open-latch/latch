#!/usr/bin/env python3
"""Frozen policy-envelope eval for Quiet, Standard, and Full.

This runner does not claim measured developer time or real-world rebuild
savings. It feeds synthetic, frozen similarity rankings through the shipped
tier policy and reports two bounded things:

* how many scenario-weighted rebuild-risk opportunities receive ambient
  context before an explicit search/gate; and
* the exact prompt-context characters emitted by the formatter for that
  fixture.

It complements, rather than replaces, ``src/evals.py``: the wedge benchmark
tests retrieval quality and graph assembly, while this runner isolates the
intensity policy/cost envelope without reading a live KB or loading a model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import paths


HOOKS = Path(__file__).resolve().parent / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

import session_start  # noqa: E402
import user_prompt_submit as prompt_hook  # noqa: E402


DEFAULT_FIXTURE = paths.KB_ROOT / "benchmarks" / "fixtures" / "intensity_v1.jsonl"
DEFAULT_RECEIPT = paths.KB_ROOT / "benchmarks" / "results" / "intensity_v1_receipt.json"
TIERS = paths.LATCH_INTENSITIES
CLAIM_BOUNDARY = (
    "Frozen synthetic policy scenarios, not measured hours, dollars, universal "
    "catch rates, or a retrieval-quality benchmark. Rebuild-risk units are "
    "relative fixture weights only. This vector/rank envelope omits graph "
    "drill-down and long-session active-set expiry."
)
RECEIPT_CLAIM_BOUNDARY = (
    "Synthetic policy regression contract. Authored scores and relative risk "
    "weights make the result true by construction; this is not measured "
    "developer time, retrieval quality, or empirical rebuild savings."
)
RECEIPT_GATE_INVARIANT = "same gate check and configuration when invoked"
RECEIPT_OMISSIONS = (
    "graph drill-down",
    "long-session active-set expiry",
    "observed task reconstruction time",
)
RECEIPT_TIER_FIELDS = (
    "labeled_reference_opportunities",
    "opportunities_with_expected_reference",
    "prompt_context_chars",
    "relative_rebuild_risk_weight",
    "relative_risk_weight_with_expected_reference",
    "topic_similarity_checks",
    "vector_retrieval_runs",
)


class IntensityEvalError(Exception):
    pass


def load_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise IntensityEvalError(f"fixture not found: {path}")
    events: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    last_turn: dict[str, int] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            event = json.loads(text)
        except json.JSONDecodeError as exc:
            raise IntensityEvalError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        _validate_event(event, f"{path}:{lineno}")
        if event["id"] in seen_ids:
            raise IntensityEvalError(f"{path}:{lineno}: duplicate id {event['id']!r}")
        seen_ids.add(event["id"])
        previous = last_turn.get(event["session"], 0)
        if event["turn"] <= previous:
            raise IntensityEvalError(
                f"{path}:{lineno}: turns must increase within session {event['session']!r}"
            )
        last_turn[event["session"]] = event["turn"]
        events.append(event)
    if not events:
        raise IntensityEvalError("no intensity events found")
    return events


def _validate_event(event: dict[str, Any], where: str) -> None:
    required = (
        "id", "session", "turn", "topic_similarity", "rebuild_risk_units",
        "expected_refs", "candidates",
    )
    missing = [key for key in required if key not in event]
    if missing:
        raise IntensityEvalError(f"{where}: missing {', '.join(missing)}")
    if not isinstance(event["turn"], int) or event["turn"] < 1:
        raise IntensityEvalError(f"{where}: turn must be a positive integer")
    sim = event["topic_similarity"]
    if sim is not None and (not isinstance(sim, (int, float)) or not -1 <= sim <= 1):
        raise IntensityEvalError(f"{where}: topic_similarity must be null or [-1, 1]")
    units = event["rebuild_risk_units"]
    if not isinstance(units, int) or units < 0:
        raise IntensityEvalError(f"{where}: rebuild_risk_units must be >= 0")
    refs: set[str] = set()
    for idx, candidate in enumerate(event["candidates"]):
        for field in ("ref", "kind", "title", "score"):
            if field not in candidate:
                raise IntensityEvalError(
                    f"{where}: candidates[{idx}] missing {field!r}"
                )
        if candidate["ref"] in refs:
            raise IntensityEvalError(
                f"{where}: duplicate candidate ref {candidate['ref']!r}"
            )
        refs.add(candidate["ref"])
    unknown = set(event["expected_refs"]) - refs
    if unknown:
        raise IntensityEvalError(f"{where}: expected refs missing from candidates: {unknown}")


def run(events: list[dict[str, Any]], *, fixture_path: Path | None = None) -> dict:
    ref_ids = {
        ref: idx
        for idx, ref in enumerate(
            sorted({c["ref"] for event in events for c in event["candidates"]}),
            start=1,
        )
    }
    tier_results = {
        tier: _run_tier(events, tier=tier, ref_ids=ref_ids)
        for tier in TIERS
    }
    total_units = sum(event["rebuild_risk_units"] for event in events)
    opportunities = sum(1 for event in events if event["expected_refs"])
    fixture_sha = None
    if fixture_path is not None:
        fixture_sha = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    full = tier_results["full"]
    standard = tier_results["standard"]
    return {
        "schema_version": 1,
        "suite": "intensity_v1",
        "claim_boundary": CLAIM_BOUNDARY,
        "fixture": str(fixture_path) if fixture_path else None,
        "fixture_sha256": fixture_sha,
        "scenario_events": len(events),
        "labeled_reference_opportunities": opportunities,
        "relative_rebuild_risk_weight": total_units,
        "gate_invariant": (
            "All tiers keep the same gate check and configuration when invoked; "
            "this runner grades ambient prompt surfacing only."
        ),
        "brief_caps": _brief_caps(),
        "tiers": tier_results,
        "full_vs_standard": {
            "additional_opportunities_with_reference": (
                full["opportunities_with_expected_reference"]
                - standard["opportunities_with_expected_reference"]
            ),
            "additional_relative_risk_weight_with_reference": (
                full["relative_risk_weight_with_expected_reference"]
                - standard["relative_risk_weight_with_expected_reference"]
            ),
            "additional_prompt_context_chars": (
                full["prompt_context_chars"]
                - standard["prompt_context_chars"]
            ),
        },
    }


def _run_tier(
    events: list[dict[str, Any]], *, tier: str, ref_ids: dict[str, int],
) -> dict:
    active_by_session: dict[str, set[int]] = {}
    event_results: list[dict] = []
    topic_similarity_checks = 0
    vector_retrieval_runs = 0
    injected_items = 0
    context_chars = 0
    opportunities_with_reference = 0
    risk_weight_with_reference = 0

    for event in events:
        active = active_by_session.setdefault(event["session"], set())
        topic_checked = tier != "quiet"
        if topic_checked:
            topic_similarity_checks += 1
        vector_retrieval_run = prompt_hook._should_retrieve_for_intensity(
            tier, event["topic_similarity"]
        )
        chosen: list[dict] = []
        if vector_retrieval_run:
            vector_retrieval_runs += 1
            floor = (
                prompt_hook.STANDARD_SIM_FLOOR
                if tier == "standard" else prompt_hook.SIM_FLOOR
            )
            limit = (
                prompt_hook.STANDARD_MAX_INJECT
                if tier == "standard" else prompt_hook.MAX_INJECT
            )
            candidates = [
                {
                    "id": ref_ids[row["ref"]],
                    "ref": row["ref"],
                    "kind": row["kind"],
                    "title": row["title"],
                    "score": float(row["score"]),
                }
                for row in event["candidates"]
            ]
            chosen = prompt_hook._select_candidates(
                candidates,
                active,
                sim_floor=floor,
                max_inject=limit,
            )
            active.update(row["id"] for row in chosen)

        if chosen:
            context = prompt_hook._format_injection(chosen, intensity=tier)
        elif vector_retrieval_run and tier == "full":
            context = prompt_hook._format_no_hits()
        else:
            context = ""
        context_chars += len(context)
        injected_items += len(chosen)

        expected = {ref_ids[ref] for ref in event["expected_refs"]}
        reference_present = bool(expected) and expected <= active
        if reference_present:
            opportunities_with_reference += 1
            risk_weight_with_reference += event["rebuild_risk_units"]
        event_results.append({
            "id": event["id"],
            "topic_similarity_checked": topic_checked,
            "vector_retrieval_run": vector_retrieval_run,
            "injected_refs": [row["ref"] for row in chosen],
            "context_chars": len(context),
            "expected_reference_present": (
                reference_present if expected else None
            ),
            "relative_rebuild_risk_weight": event["rebuild_risk_units"],
        })

    opportunities = sum(1 for event in events if event["expected_refs"])
    total_units = sum(event["rebuild_risk_units"] for event in events)
    return {
        "labeled_reference_opportunities": opportunities,
        "opportunities_with_expected_reference": opportunities_with_reference,
        "expected_reference_presence_rate": (
            opportunities_with_reference / opportunities if opportunities else 1.0
        ),
        "relative_rebuild_risk_weight": total_units,
        "relative_risk_weight_with_expected_reference": risk_weight_with_reference,
        "relative_risk_weight_reference_rate": (
            risk_weight_with_reference / total_units if total_units else 1.0
        ),
        "topic_similarity_checks": topic_similarity_checks,
        "vector_retrieval_runs": vector_retrieval_runs,
        "injected_items": injected_items,
        "prompt_context_chars": context_chars,
        "prompt_context_chars_per_weight_with_reference": (
            round(context_chars / risk_weight_with_reference, 1)
            if risk_weight_with_reference else None
        ),
        "events": event_results,
    }


def portable_receipt(result: dict) -> dict:
    """Project the full diagnostic result into the checked-in receipt schema.

    The portable form intentionally excludes the machine-specific fixture
    path and per-event diagnostics. Its fixture digest and aggregate policy
    fields are sufficient for the repository's deterministic drift guard.
    """
    return {
        "claim_boundary": RECEIPT_CLAIM_BOUNDARY,
        "fixture_sha256": result["fixture_sha256"],
        "gate_invariant": RECEIPT_GATE_INVARIANT,
        "omissions": list(RECEIPT_OMISSIONS),
        "schema_version": result["schema_version"],
        "suite": result["suite"],
        "tiers": {
            tier: {
                field: result["tiers"][tier][field]
                for field in RECEIPT_TIER_FIELDS
            }
            for tier in TIERS
        },
    }


def _write_text_atomic(path: Path, text: str) -> None:
    """Atomically replace ``path`` with UTF-8 ``text`` in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        target_mode = path.stat().st_mode & 0o777
    except OSError:
        target_mode = 0o644
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        # Pin LF so --write-receipt is byte-for-byte reproducible on Windows
        # as well as POSIX checkouts.
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(tmp, target_mode)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _brief_caps() -> dict[str, dict[str, int]]:
    return {
        tier: {
            "workstreams": policy.max_workstreams,
            "open_questions": policy.max_open_questions,
            "ideas": policy.max_ideas,
            "workstream_chars": policy.workstream_chars,
            "idea_chars": policy.idea_chars,
        }
        for tier, policy in session_start.BRIEF_POLICIES.items()
    }


def render_markdown(result: dict) -> str:
    lines = [
        "# Latch Intensity Policy Envelope",
        "",
        result["claim_boundary"],
        "",
        "| Tier | Expected reference present | Relative risk weight with reference | "
        "Topic checks | Vector retrieval runs | Prompt context chars |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for tier in TIERS:
        row = result["tiers"][tier]
        lines.append(
            f"| {tier.title()} | {row['opportunities_with_expected_reference']}/"
            f"{row['labeled_reference_opportunities']} | "
            f"{row['relative_risk_weight_with_expected_reference']}/"
            f"{row['relative_rebuild_risk_weight']} | "
            f"{row['topic_similarity_checks']} | {row['vector_retrieval_runs']} | "
            f"{row['prompt_context_chars']} |"
        )
    delta = result["full_vs_standard"]
    lines.extend([
        "",
        "Full versus Standard on this frozen fixture:",
        "",
        f"- Additional opportunities with the expected reference present: "
        f"{delta['additional_opportunities_with_reference']}",
        f"- Additional relative risk weight with the reference present: "
        f"{delta['additional_relative_risk_weight_with_reference']}",
        f"- Additional prompt context characters: "
        f"{delta['additional_prompt_context_chars']}",
        "",
        result["gate_invariant"],
    ])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the frozen Quiet/Standard/Full policy-envelope eval."
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--output", type=Path)
    destination.add_argument(
        "--write-receipt",
        nargs="?",
        type=Path,
        const=DEFAULT_RECEIPT,
        metavar="PATH",
        help=(
            "atomically write the trimmed portable JSON receipt; defaults to "
            "benchmarks/results/intensity_v1_receipt.json"
        ),
    )
    args = parser.parse_args(argv)
    try:
        events = load_events(args.fixture)
        result = run(events, fixture_path=args.fixture)
        if args.write_receipt is not None:
            rendered = json.dumps(
                portable_receipt(result), indent=2, sort_keys=True
            ) + "\n"
            _write_text_atomic(args.write_receipt, rendered)
            print(f"Wrote portable receipt: {args.write_receipt}")
            return 0
        rendered = (
            json.dumps(result, indent=2, sort_keys=True) + "\n"
            if args.format == "json" else render_markdown(result)
        )
        if args.output:
            _write_text_atomic(args.output, rendered)
    except (OSError, IntensityEvalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.output:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
