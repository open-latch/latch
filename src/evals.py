"""Local benchmark runner for latch's decision-judgment wedge.

This is deliberately small: fixtures seed a temporary KB, run the deterministic
gate assembly path, and grade whether latch surfaced the decision evidence that
a generic memory system would blur, stale-merge, or miss.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import db
import embeddings
import gate
import paths
import search


DEFAULT_FIXTURE = paths.KB_ROOT / "benchmarks" / "fixtures" / "wedge_v1.jsonl"
DEFAULT_MODES = (
    "latch_full",
    "active_seed_graph",
    "stale_search",
    "memory_like",
)
MODE_DESCRIPTIONS = {
    "latch_full": (
        "full latch evidence assembly: stale-aware hybrid seeds plus graph "
        "traversal over decision relations"
    ),
    "active_seed_graph": (
        "ablation: active-only hybrid seeds plus graph traversal; tests whether "
        "the right decision chain is reachable without stale/foundational nodes "
        "being directly searchable"
    ),
    "stale_search": (
        "ablation: stale-aware hybrid search only, with no graph traversal; "
        "tests whether direct retrieval is enough without decision edges"
    ),
    "memory_like": (
        "memory-like baseline: active hybrid search only, no stale nodes and "
        "no graph traversal"
    ),
}
WEDGE_THESIS = (
    "Latch is not a memory benchmark. These evals test whether latch surfaces "
    "binding project judgment: rejected paths, current authority, stale status, "
    "the real reasons behind decisions, and visible gate receipts."
)


class EvalError(Exception):
    pass


def load_cases(fixture_paths: list[Path]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in fixture_paths:
        if not path.exists():
            raise EvalError(f"fixture not found: {path}")
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                case = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise EvalError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            case["_fixture_path"] = str(path)
            case["_fixture_lineno"] = lineno
            _validate_case(case, f"{path}:{lineno}")
            cases.append(case)
    if not cases:
        raise EvalError("no benchmark cases found")
    return cases


def _validate_case(case: dict[str, Any], where: str) -> None:
    required = ("id", "suite", "kind", "query", "nodes", "expect")
    missing = [k for k in required if k not in case]
    if missing:
        raise EvalError(f"{where}: missing required field(s): {', '.join(missing)}")
    refs = set()
    for i, node in enumerate(case["nodes"]):
        for field in ("ref", "kind", "title", "body"):
            if field not in node:
                raise EvalError(f"{where}: nodes[{i}] missing {field!r}")
        if node["ref"] in refs:
            raise EvalError(f"{where}: duplicate node ref {node['ref']!r}")
        refs.add(node["ref"])
    for i, edge in enumerate(case.get("edges", [])):
        for field in ("src", "dst", "relation"):
            if field not in edge:
                raise EvalError(f"{where}: edges[{i}] missing {field!r}")
        if edge["src"] not in refs or edge["dst"] not in refs:
            raise EvalError(f"{where}: edge references unknown node: {edge}")
    expect = case["expect"]
    for ref in expect.get("must_retrieve", []):
        if ref not in refs:
            raise EvalError(f"{where}: expect.must_retrieve unknown ref {ref!r}")
    for ref in expect.get("must_not_retrieve", []):
        if ref not in refs:
            raise EvalError(f"{where}: expect.must_not_retrieve unknown ref {ref!r}")
    for i, check in enumerate(expect.get("supporting_phrases", [])):
        if check.get("ref") not in refs:
            raise EvalError(f"{where}: supporting_phrases[{i}] unknown ref")
        if not check.get("phrase"):
            raise EvalError(f"{where}: supporting_phrases[{i}] missing phrase")


def run_cases(
    cases: list[dict[str, Any]],
    *,
    seed_top_k: int = gate.DEFAULT_SEED_TOP_K,
    max_hops: int = gate.DEFAULT_MAX_HOPS,
    modes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    selected_modes = tuple(modes or DEFAULT_MODES)
    _validate_modes(selected_modes)
    case_results = [
        _run_case(
            case,
            seed_top_k=seed_top_k,
            max_hops=max_hops,
            modes=selected_modes,
        )
        for case in cases
    ]
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return _summarize(
        case_results, elapsed_ms=elapsed_ms, modes=selected_modes,
    )


def _validate_modes(modes: tuple[str, ...]) -> None:
    unknown = [m for m in modes if m not in MODE_DESCRIPTIONS]
    if unknown:
        raise EvalError(f"unknown eval mode(s): {', '.join(unknown)}")


def _run_case(
    case: dict[str, Any],
    *,
    seed_top_k: int,
    max_hops: int,
    modes: tuple[str, ...],
) -> dict[str, Any]:
    temp_root = tempfile.mkdtemp(prefix="latch-eval-")
    conn = None
    try:
        conn = _connect_temp_kb(Path(temp_root))
        ref_to_id = _seed_nodes(conn, case["nodes"])
        _seed_edges(conn, case.get("edges", []), ref_to_id)
        id_to_ref = {nid: ref for ref, nid in ref_to_id.items()}
        mode_results = {}
        for mode in modes:
            retrieved_ids, seed_refs, evidence_refs = _run_mode(
                conn,
                mode,
                case["query"],
                id_to_ref=id_to_ref,
                seed_top_k=seed_top_k,
                max_hops=max_hops,
            )
            mode_results[mode] = _grade_mode(
                conn,
                case,
                ref_to_id=ref_to_id,
                id_to_ref=id_to_ref,
                retrieved_ids=retrieved_ids,
                seed_refs=seed_refs,
                evidence_refs=evidence_refs,
            )

        primary = mode_results[modes[0]]
        return {
            "id": case["id"],
            "suite": case["suite"],
            "kind": case["kind"],
            "difficulty": case.get("difficulty", "smoke"),
            "query": case["query"],
            "passed": primary["passed"],
            "required_ref_count": primary["required_ref_count"],
            "supporting_phrase_count": primary["supporting_phrase_count"],
            "memory_trap": case.get("memory_trap", ""),
            "why_memory_fails": case.get("why_memory_fails", ""),
            "source_note": case.get("source_note", ""),
            "retrieved_refs": primary["retrieved_refs"],
            "missing_refs": primary["missing_refs"],
            "unexpected_refs": primary["unexpected_refs"],
            "missing_supporting_phrases": primary["missing_supporting_phrases"],
            "seed_refs": primary["seed_refs"],
            "evidence_refs": primary["evidence_refs"],
            "mode_results": mode_results,
            "fixture_path": case.get("_fixture_path"),
            "fixture_lineno": case.get("_fixture_lineno"),
        }
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def _run_mode(
    conn,
    mode: str,
    query: str,
    *,
    id_to_ref: dict[int, str],
    seed_top_k: int,
    max_hops: int,
) -> tuple[set[int], list[str], list[str]]:
    if mode == "latch_full":
        assembly = gate.assemble_gate(
            conn,
            query,
            seed_top_k=seed_top_k,
            include_stale=True,
            focus_seed=False,
            max_hops=max_hops,
        )
        return _assembly_refs(assembly, id_to_ref)
    if mode == "active_seed_graph":
        assembly = gate.assemble_gate(
            conn,
            query,
            seed_top_k=seed_top_k,
            include_stale=False,
            focus_seed=False,
            max_hops=max_hops,
        )
        return _assembly_refs(assembly, id_to_ref)
    if mode == "stale_search":
        hits = search.hybrid_search(
            conn,
            query,
            limit=seed_top_k,
            include_stale=True,
            track_access=False,
        )
        return _search_refs(hits, id_to_ref)
    if mode == "memory_like":
        hits = search.hybrid_search(
            conn,
            query,
            limit=seed_top_k,
            include_stale=False,
            track_access=False,
        )
        return _search_refs(hits, id_to_ref)
    raise EvalError(f"unsupported eval mode: {mode}")


def _assembly_refs(
    assembly: dict[str, Any], id_to_ref: dict[int, str],
) -> tuple[set[int], list[str], list[str]]:
    retrieved_ids = _retrieved_ids(assembly)
    seed_refs = [
        id_to_ref[s["id"]]
        for s in assembly.get("seeds", [])
        if s["id"] in id_to_ref
    ]
    evidence_refs = [
        id_to_ref[eid]
        for eid in assembly.get("evidence_node_ids", [])
        if eid in id_to_ref
    ]
    return retrieved_ids, seed_refs, evidence_refs


def _search_refs(
    hits: list[dict[str, Any]], id_to_ref: dict[int, str],
) -> tuple[set[int], list[str], list[str]]:
    retrieved_ids = {h["id"] for h in hits}
    seed_refs = [id_to_ref[h["id"]] for h in hits if h["id"] in id_to_ref]
    return retrieved_ids, seed_refs, []


def _grade_mode(
    conn,
    case: dict[str, Any],
    *,
    ref_to_id: dict[str, int],
    id_to_ref: dict[int, str],
    retrieved_ids: set[int],
    seed_refs: list[str],
    evidence_refs: list[str],
) -> dict[str, Any]:
    expect = case["expect"]
    retrieved_refs = [id_to_ref[nid] for nid in retrieved_ids if nid in id_to_ref]
    missing_refs = [
        ref for ref in expect.get("must_retrieve", [])
        if ref_to_id[ref] not in retrieved_ids
    ]
    unexpected_refs = [
        ref for ref in expect.get("must_not_retrieve", [])
        if ref_to_id[ref] in retrieved_ids
    ]
    missing_phrases = _missing_supporting_phrases(
        conn, expect.get("supporting_phrases", []), ref_to_id, retrieved_ids
    )
    passed = not missing_refs and not unexpected_refs and not missing_phrases
    return {
        "passed": passed,
        "required_ref_count": len(expect.get("must_retrieve", [])),
        "supporting_phrase_count": len(expect.get("supporting_phrases", [])),
        "retrieved_refs": retrieved_refs,
        "missing_refs": missing_refs,
        "unexpected_refs": unexpected_refs,
        "missing_supporting_phrases": missing_phrases,
        "seed_refs": seed_refs,
        "evidence_refs": evidence_refs,
    }


def _connect_temp_kb(temp_root: Path):
    """Connect to a throwaway KB even on machines with a pinned install."""
    kb_dir = temp_root / "kb"
    old_latch_env = os.environ.pop("LATCH_KB_DIR", None)
    old_env = os.environ.pop("CLAUDE_KB_DIR", None)
    old_pin = paths._PINNED_DIR
    try:
        paths._PINNED_DIR = kb_dir
        return db.connect(str(temp_root))
    finally:
        paths._PINNED_DIR = old_pin
        if old_latch_env is not None:
            os.environ["LATCH_KB_DIR"] = old_latch_env
        if old_env is not None:
            os.environ["CLAUDE_KB_DIR"] = old_env


def _seed_nodes(conn, nodes: list[dict[str, Any]]) -> dict[str, int]:
    ref_to_id: dict[str, int] = {}
    for node in nodes:
        text = f"{node['title']}\n\n{node['body']}"
        vec = embeddings.embed(text)
        nid = db.insert_node(
            conn,
            kind=node["kind"],
            title=node["title"],
            body=node["body"],
            status=node.get("status", "canonical"),
            embedding=embeddings.to_blob(vec),
        )
        ref_to_id[node["ref"]] = nid
    return ref_to_id


def _seed_edges(conn, edges: list[dict[str, Any]], ref_to_id: dict[str, int]) -> None:
    for edge in edges:
        db.add_edge(
            conn,
            src=ref_to_id[edge["src"]],
            dst=ref_to_id[edge["dst"]],
            relation=edge["relation"],
        )


def _retrieved_ids(assembly: dict[str, Any]) -> set[int]:
    ids = {s["id"] for s in assembly.get("seeds", [])}
    ids.update(assembly.get("evidence_node_ids", []))
    return ids


def _missing_supporting_phrases(
    conn,
    checks: list[dict[str, str]],
    ref_to_id: dict[str, int],
    retrieved_ids: set[int],
) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for check in checks:
        nid = ref_to_id[check["ref"]]
        phrase = check["phrase"]
        if nid not in retrieved_ids:
            missing.append({
                "ref": check["ref"],
                "phrase": phrase,
                "reason": "node_not_retrieved",
            })
            continue
        node = db.get_node(conn, nid) or {}
        haystack = f"{node.get('title', '')}\n{node.get('body', '')}".lower()
        if phrase.lower() not in haystack:
            missing.append({
                "ref": check["ref"],
                "phrase": phrase,
                "reason": "phrase_not_in_retrieved_node",
            })
    return missing


def _summarize(
    case_results: list[dict[str, Any]],
    *,
    elapsed_ms: int,
    modes: tuple[str, ...],
) -> dict[str, Any]:
    primary = modes[0]
    total = len(case_results)
    passed = sum(1 for r in case_results if r["passed"])
    required_total = sum(r["required_ref_count"] for r in case_results)
    required_missing = sum(len(r["missing_refs"]) for r in case_results)
    phrase_total = sum(r["supporting_phrase_count"] for r in case_results)
    phrase_missing = sum(
        len(r["missing_supporting_phrases"])
        for r in case_results
    )
    by_kind: dict[str, dict[str, int]] = {}
    by_difficulty: dict[str, dict[str, int]] = {}
    for result in case_results:
        row = by_kind.setdefault(result["kind"], {"cases": 0, "passed": 0})
        row["cases"] += 1
        row["passed"] += 1 if result["passed"] else 0
        diff_row = by_difficulty.setdefault(
            result["difficulty"], {"cases": 0, "passed": 0}
        )
        diff_row["cases"] += 1
        diff_row["passed"] += 1 if result["passed"] else 0
    mode_summaries = {
        mode: _summarize_mode(case_results, mode)
        for mode in modes
    }
    comparisons = _mode_comparisons(case_results, modes)
    latch_wins = 0
    if "latch_full" in modes and "memory_like" in modes:
        latch_wins = comparisons["latch_full_vs_memory_like"]["primary_only_wins"]
    return {
        "ok": passed == total,
        "thesis": WEDGE_THESIS,
        "primary_mode": primary,
        "mode_descriptions": {
            mode: MODE_DESCRIPTIONS[mode]
            for mode in modes
        },
        "summary": {
            "cases": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": passed / total if total else 0.0,
            "required_retrieval_rate": (
                (required_total - required_missing) / required_total
                if required_total else 1.0
            ),
            "supporting_phrase_rate": (
                (phrase_total - phrase_missing) / phrase_total
                if phrase_total else 1.0
            ),
            "missing_supporting_phrase_count": phrase_missing,
            "elapsed_ms": elapsed_ms,
            "by_kind": by_kind,
            "by_difficulty": by_difficulty,
            "latch_only_wins": latch_wins,
            "comparisons": comparisons,
        },
        "modes": mode_summaries,
        "cases": case_results,
    }


def _mode_comparisons(
    case_results: list[dict[str, Any]], modes: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    if len(modes) < 2:
        return {}
    primary = modes[0]
    comparisons: dict[str, dict[str, Any]] = {}
    for baseline in modes[1:]:
        primary_only = 0
        baseline_only = 0
        both_pass = 0
        both_fail = 0
        for result in case_results:
            primary_pass = result["mode_results"][primary]["passed"]
            baseline_pass = result["mode_results"][baseline]["passed"]
            if primary_pass and baseline_pass:
                both_pass += 1
            elif primary_pass and not baseline_pass:
                primary_only += 1
            elif not primary_pass and baseline_pass:
                baseline_only += 1
            else:
                both_fail += 1
        total = len(case_results)
        comparisons[f"{primary}_vs_{baseline}"] = {
            "primary_mode": primary,
            "baseline_mode": baseline,
            "primary_only_wins": primary_only,
            "baseline_only_wins": baseline_only,
            "both_pass": both_pass,
            "both_fail": both_fail,
            "net_wins": primary_only - baseline_only,
            "pass_rate_delta": (
                (primary_only - baseline_only) / total if total else 0.0
            ),
        }
    return comparisons


def _summarize_mode(
    case_results: list[dict[str, Any]], mode: str,
) -> dict[str, Any]:
    total = len(case_results)
    mode_results = [r["mode_results"][mode] for r in case_results]
    passed = sum(1 for r in mode_results if r["passed"])
    required_total = sum(r["required_ref_count"] for r in mode_results)
    required_missing = sum(len(r["missing_refs"]) for r in mode_results)
    phrase_total = sum(r["supporting_phrase_count"] for r in mode_results)
    phrase_missing = sum(len(r["missing_supporting_phrases"]) for r in mode_results)
    by_difficulty: dict[str, dict[str, int]] = {}
    for case, result in zip(case_results, mode_results):
        row = by_difficulty.setdefault(
            case["difficulty"], {"cases": 0, "passed": 0}
        )
        row["cases"] += 1
        row["passed"] += 1 if result["passed"] else 0
    return {
        "cases": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": passed / total if total else 0.0,
        "required_retrieval_rate": (
            (required_total - required_missing) / required_total
            if required_total else 1.0
        ),
        "supporting_phrase_rate": (
            (phrase_total - phrase_missing) / phrase_total
            if phrase_total else 1.0
        ),
        "missing_supporting_phrase_count": phrase_missing,
        "by_difficulty": by_difficulty,
    }


def render_markdown(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        "# Latch Wedge Benchmark",
        "",
        result["thesis"],
        "",
        "## Summary",
        "",
        f"- Cases: {summary['cases']}",
        f"- Passed: {summary['passed']}",
        f"- Failed: {summary['failed']}",
        f"- Pass rate: {summary['pass_rate']:.0%}",
        f"- Required retrieval rate: {summary['required_retrieval_rate']:.0%}",
        f"- Supporting phrase rate: {summary['supporting_phrase_rate']:.0%}",
        f"- Missing supporting phrases: {summary['missing_supporting_phrase_count']}",
        f"- Latch-only wins vs memory-like baseline: {summary['latch_only_wins']}",
        f"- Elapsed: {summary['elapsed_ms']} ms",
        "",
        "## Comparisons",
        "",
    ]
    if summary["comparisons"]:
        for label, comparison in summary["comparisons"].items():
            lines.append(
                f"- {label}: primary-only wins "
                f"{comparison['primary_only_wins']}; baseline-only wins "
                f"{comparison['baseline_only_wins']}; both pass "
                f"{comparison['both_pass']}; both fail "
                f"{comparison['both_fail']}; net wins "
                f"{comparison['net_wins']}"
            )
    else:
        lines.append("(none)")
    lines.extend([
        "",
        "## Modes",
        "",
    ])
    for mode, mode_summary in result["modes"].items():
        lines.extend([
            f"### {mode}",
            "",
            MODE_DESCRIPTIONS[mode],
            "",
            f"- Passed: {mode_summary['passed']}/{mode_summary['cases']}",
            f"- Pass rate: {mode_summary['pass_rate']:.0%}",
            f"- Required retrieval rate: {mode_summary['required_retrieval_rate']:.0%}",
            f"- Supporting phrase rate: {mode_summary['supporting_phrase_rate']:.0%}",
            "- Difficulty: "
            + _render_breakdown(mode_summary["by_difficulty"]),
            "",
        ])
    lines.extend([
        "## Difficulty",
        "",
        _render_breakdown(summary["by_difficulty"]),
        "",
        "## Cases",
        "",
    ])
    for case in result["cases"]:
        status = "PASS" if case["passed"] else "FAIL"
        lines.extend([
            f"### {status} {case['id']}",
            "",
            f"- Kind: {case['kind']}",
            f"- Difficulty: {case['difficulty']}",
            f"- Query: {case['query']}",
            f"- Retrieved refs: {', '.join(case['retrieved_refs']) or '(none)'}",
        ])
        if "memory_like" in case["mode_results"]:
            baseline = case["mode_results"]["memory_like"]
            baseline_status = "PASS" if baseline["passed"] else "FAIL"
            lines.append(
                f"- Memory-like baseline: {baseline_status}; retrieved "
                f"{', '.join(baseline['retrieved_refs']) or '(none)'}"
            )
        if case.get("memory_trap"):
            lines.append(f"- Memory trap: {case['memory_trap']}")
        if case.get("why_memory_fails"):
            lines.append(f"- Why memory fails: {case['why_memory_fails']}")
        if case.get("source_note"):
            lines.append(f"- Source note: {case['source_note']}")
        if not case["passed"]:
            if case["missing_refs"]:
                lines.append(f"- Missing refs: {', '.join(case['missing_refs'])}")
            if case["unexpected_refs"]:
                lines.append(f"- Unexpected refs: {', '.join(case['unexpected_refs'])}")
            if case["missing_supporting_phrases"]:
                lines.append(
                    "- Missing supporting phrases: "
                    + json.dumps(case["missing_supporting_phrases"], sort_keys=True)
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_breakdown(rows: dict[str, dict[str, int]]) -> str:
    if not rows:
        return "(none)"
    parts = []
    for label in sorted(rows):
        row = rows[label]
        parts.append(f"{label} {row['passed']}/{row['cases']}")
    return ", ".join(parts)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run latch's local decision-judgment benchmark fixtures."
    )
    parser.add_argument(
        "--fixture",
        action="append",
        type=Path,
        default=None,
        help="JSONL fixture path. May be passed more than once.",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format.",
    )
    parser.add_argument("--output", type=Path, help="Write report to this path.")
    parser.add_argument("--seed-top-k", type=int, default=gate.DEFAULT_SEED_TOP_K)
    parser.add_argument("--max-hops", type=int, default=gate.DEFAULT_MAX_HOPS)
    parser.add_argument(
        "--mode",
        action="append",
        choices=tuple(MODE_DESCRIPTIONS),
        default=None,
        help=(
            "Evaluation mode to run. Defaults to latch_full plus memory_like. "
            "May be passed more than once; the first mode is the pass/fail gate."
        ),
    )
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="Always exit 0 even when benchmark cases fail.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    fixture_paths = args.fixture or [DEFAULT_FIXTURE]
    try:
        cases = load_cases(fixture_paths)
        result = run_cases(
            cases,
            seed_top_k=args.seed_top_k,
            max_hops=args.max_hops,
            modes=args.mode,
        )
    except EvalError as exc:
        print(f"latch_eval: {exc}", file=sys.stderr)
        return 2
    output = (
        json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.format == "json"
        else render_markdown(result)
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0 if result["ok"] or args.no_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
