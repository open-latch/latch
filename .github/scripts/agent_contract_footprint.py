#!/usr/bin/env python3
"""Fail CI when the always-loaded agent contract grows without review.

The committed obligation fixture provides hard ceilings.  This check adds the
missing relative signal: compare the rendered Claude, Codex, and Cursor
surfaces with the pull request base and require explicit maintainer review for
any line, word, or byte growth (or for loosening a ceiling).

The renderer below intentionally reads the marker and compaction constants
from each git snapshot.  That keeps the comparison tied to the contract that
actually existed at the base revision instead of today's implementation.
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = "tests/fixtures/agent_contract_obligations.json"
METRICS = ("lines", "words", "bytes")


@dataclass(frozen=True)
class Snapshot:
    source_snippet: str
    cursor_rule_source: str
    wiring_version: str
    wiring_marker_prefix: str
    claude_begin: str
    claude_end: str
    agents_begin: str
    agents_end: str
    claude_compaction: str
    agents_compaction: str


def metric(text: str) -> dict[str, int]:
    return {
        "lines": len(text.splitlines()),
        "words": len(text.split()),
        "bytes": len(text.encode("utf-8")),
    }


def _constant(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        value = ast.literal_eval(node.value)
        if isinstance(value, str):
            return value
        break
    raise ValueError(f"could not read string constant {name}")


def _read_first(read_text: Callable[[str], str], *paths: str) -> str:
    last_error: OSError | ValueError | None = None
    for path in paths:
        try:
            return read_text(path)
        except (OSError, ValueError) as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def load_snapshot(read_text: Callable[[str], str]) -> Snapshot:
    managed = _read_first(
        read_text, "src/latch/hosts/managed_doc_sync.py", "src/managed_doc_sync.py"
    )
    claude = _read_first(
        read_text, "src/latch/hosts/claude_md_sync.py", "src/claude_md_sync.py"
    )
    agents = _read_first(
        read_text, "src/latch/hosts/agents_md_sync.py", "src/agents_md_sync.py"
    )
    return Snapshot(
        source_snippet=read_text("claude_md_snippet.md"),
        cursor_rule_source=read_text("cursor_rule_snippet.mdc"),
        wiring_version=read_text("WIRING_VERSION").strip(),
        wiring_marker_prefix=_constant(managed, "WIRING_MARKER_PREFIX"),
        claude_begin=_constant(claude, "BEGIN_MARK"),
        claude_end=_constant(claude, "END_MARK"),
        agents_begin=_constant(agents, "BEGIN_MARK"),
        agents_end=_constant(agents, "END_MARK"),
        claude_compaction=_constant(managed, "CLAUDE_COMPACTION_TEXT"),
        agents_compaction=_constant(managed, "AGENTS_COMPACTION_TEXT"),
    )


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _render_contract(
    snapshot: Snapshot,
    *,
    target_name: str,
    installer_name: str,
    compaction_text: str,
) -> str:
    text = snapshot.source_snippet.replace("{{KB_HOME}}", "/opt/latch")
    text = text.replace("{{LATCH_COMPACTION_TEXT}}", compaction_text)
    if target_name != "CLAUDE.md":
        text = text.replace("CLAUDE.md", target_name)
    if installer_name != "install_claude_md":
        text = text.replace("install_claude_md", installer_name)
    rendered = _norm(text).strip("\n")
    return (
        f"{snapshot.wiring_marker_prefix} {snapshot.wiring_version} -->\n"
        f"{rendered}"
    )


def _block(begin: str, rendered: str, end: str) -> str:
    return f"{begin}\n{rendered}\n{end}\n"


def measure_snapshot(snapshot: Snapshot) -> dict[str, dict[str, int]]:
    claude = _block(
        snapshot.claude_begin,
        _render_contract(
            snapshot,
            target_name="CLAUDE.md",
            installer_name="install_claude_md",
            compaction_text=snapshot.claude_compaction,
        ),
        snapshot.claude_end,
    )
    agents = _block(
        snapshot.agents_begin,
        _render_contract(
            snapshot,
            target_name="AGENTS.md",
            installer_name="install_agents_md",
            compaction_text=snapshot.agents_compaction,
        ),
        snapshot.agents_end,
    )
    cursor_rule = (
        _norm(snapshot.cursor_rule_source)
        .replace("{{KB_HOME}}", "/opt/latch")
        .replace("{{WIRING_VERSION}}", snapshot.wiring_version)
        .strip("\n")
        + "\n"
    )
    return {
        "source_snippet": metric(snapshot.source_snippet),
        "claude_managed": metric(claude),
        "agents_managed": metric(agents),
        "cursor_rule": metric(cursor_rule),
        "cursor_always_loaded": metric(agents + "\n" + cursor_rule),
    }


def growth_findings(
    base: dict[str, dict[str, int]],
    current: dict[str, dict[str, int]],
) -> list[str]:
    findings: list[str] = []
    for surface, current_metrics in current.items():
        if surface not in base:
            continue
        for name in METRICS:
            delta = current_metrics[name] - base[surface][name]
            if delta > 0:
                findings.append(f"{surface}.{name} grew by {delta}")
    return findings


def absolute_budget_findings(
    current: dict[str, dict[str, int]],
    budgets: dict[str, dict[str, int]],
) -> list[str]:
    findings: list[str] = []
    for surface, limits in budgets.items():
        if surface not in current:
            findings.append(f"budget references unknown surface {surface}")
            continue
        for key, limit in limits.items():
            if not key.startswith("max_"):
                continue
            name = key.removeprefix("max_")
            if name not in current[surface]:
                findings.append(f"budget references unknown metric {surface}.{name}")
            elif current[surface][name] > limit:
                findings.append(
                    f"{surface}.{name} is {current[surface][name]}, above {key}={limit}"
                )
    return findings


def budget_loosening_findings(
    base: dict[str, dict[str, int]],
    current: dict[str, dict[str, int]],
) -> list[str]:
    findings: list[str] = []
    for surface, limits in current.items():
        old_limits = base.get(surface, {})
        for key, limit in limits.items():
            if key in old_limits and limit > old_limits[key]:
                findings.append(
                    f"{surface}.{key} increased from {old_limits[key]} to {limit}"
                )
    return findings


def _git_text(root: Path, revision: str, path: str) -> str:
    proc = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        cwd=root,
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"cannot read {path} at {revision}: {detail}")
    return proc.stdout.decode("utf-8")


def _policy(read_text: Callable[[str], str], *, require_label: bool = True) -> dict:
    data = json.loads(read_text(POLICY_PATH))
    if not isinstance(data.get("budgets"), dict):
        raise ValueError(f"{POLICY_PATH} has no budgets object")
    if require_label and not isinstance(data.get("growth_review_label"), str):
        raise ValueError(f"{POLICY_PATH} has no growth_review_label")
    return data


def _print_table(
    base: dict[str, dict[str, int]],
    current: dict[str, dict[str, int]],
) -> None:
    print("agent contract footprint (PR base -> current):")
    print(f"{'surface':<23} {'metric':<7} {'base':>7} {'current':>8} {'delta':>7}")
    for surface, values in current.items():
        for name in METRICS:
            old = base.get(surface, values)[name]
            new = values[name]
            print(f"{surface:<23} {name:<7} {old:>7} {new:>8} {new - old:>+7}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="require review for growth in always-loaded agent contracts"
    )
    ap.add_argument("--base", required=True, help="git revision used as the footprint baseline")
    ap.add_argument("--repo", type=Path, default=ROOT, help="repository root")
    ap.add_argument(
        "--allow-growth",
        action="store_true",
        help="acknowledge the configured maintainer review label",
    )
    args = ap.parse_args(argv)
    root = args.repo.resolve()

    try:
        current_read = lambda path: (root / path).read_text(encoding="utf-8")
        base_read = lambda path: _git_text(root, args.base, path)
        current_policy = _policy(current_read)
        base_policy = _policy(base_read, require_label=False)
        current = measure_snapshot(load_snapshot(current_read))
        base = measure_snapshot(load_snapshot(base_read))
    except (OSError, ValueError, json.JSONDecodeError, SyntaxError) as exc:
        print(f"agent contract footprint check could not run: {exc}", file=sys.stderr)
        return 2

    _print_table(base, current)
    hard_failures = absolute_budget_findings(current, current_policy["budgets"])
    review_findings = growth_findings(base, current)
    review_findings.extend(
        budget_loosening_findings(base_policy["budgets"], current_policy["budgets"])
    )

    if hard_failures:
        print("\nabsolute contract budgets failed:", file=sys.stderr)
        for finding in hard_failures:
            print(f"- {finding}", file=sys.stderr)
        return 1

    if review_findings and not args.allow_growth:
        label = current_policy["growth_review_label"]
        print("\nagent contract growth requires explicit maintainer review:", file=sys.stderr)
        for finding in review_findings:
            print(f"- {finding}", file=sys.stderr)
        print(
            "Trim or relocate equivalent detail, or apply the "
            f"{label!r} label after reviewing why the always-loaded cost is necessary.",
            file=sys.stderr,
        )
        return 1

    if review_findings:
        print("\nrelative growth approved; absolute budgets still pass")
        for finding in review_findings:
            print(f"- {finding}")
    else:
        print("\nno relative contract growth; absolute budgets pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
