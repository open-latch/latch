"""Turnkey no-history rejected-path demo fixture.

This gives a fresh install a public-safe proof path when there are no Claude or
Codex sessions to seed: create a throwaway project, seed one rejected-path
decision into a throwaway KB, run latch_gate, and print the receipt.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import gate  # noqa: E402
import paths  # noqa: E402


DEMO_REQUEST = "Implement email sending by adding a Redis-backed background job queue."
DEMO_DECISION_TITLE = "No background job queue for the no-history demo app"
DEMO_GOVERNANCE = """# GOVERNANCE

Do not add a background job queue. Keep this sample app single-process.
If background work is needed, use an inline task runner and document the limit.
"""
DEMO_DECISION_BODY = """No-history demo fixture decision.

Decision: do not add a background job queue to this sample app.
Rejected path: Redis-backed background job queue.
Allowed path: keep the app single-process; if background work is needed, use an
inline task runner and document the limit.
Rationale: the demo app should stay install-light and easy to inspect, without
requiring Redis or a worker process just to send email.
Trigger request to catch: Implement email sending by adding a Redis-backed
background job queue.
Source: generated public fixture file GOVERNANCE.md.
"""


@dataclass
class DemoFixture:
    root: Path
    project: Path
    kb_dir: Path
    decision_id: int


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Run a no-history latch demo: seed one public-safe rejected rule "
            "into a throwaway KB, run latch_gate, and print the receipt."
        )
    )
    ap.add_argument("--work-dir",
                    help="directory for the throwaway fixture (default: temp dir)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway fixture directory after the run")
    ap.add_argument("--request", default=DEMO_REQUEST,
                    help="request to send through latch_gate")
    ap.add_argument("--max-chains", type=int, default=5,
                    help="maximum gate chains to pass to the classifier")
    ap.add_argument("--no-llm", action="store_true",
                    help="assemble the gate context without calling the classifier")
    ap.add_argument("--backend", choices=("claude", "codex"),
                    help="gate backend to use for this run")
    return ap.parse_args(argv)


@contextmanager
def pinned_kb_dir(kb_dir: Path) -> Iterator[None]:
    """Pin this process to a throwaway KB and restore the previous env/cache."""
    old_latch = os.environ.get("LATCH_KB_DIR")
    old_legacy = os.environ.get("CLAUDE_KB_DIR")
    old_cache = paths._PINNED_DIR
    os.environ["LATCH_KB_DIR"] = str(kb_dir)
    os.environ.pop("CLAUDE_KB_DIR", None)
    paths._PINNED_DIR = False
    try:
        yield
    finally:
        if old_latch is None:
            os.environ.pop("LATCH_KB_DIR", None)
        else:
            os.environ["LATCH_KB_DIR"] = old_latch
        if old_legacy is None:
            os.environ.pop("CLAUDE_KB_DIR", None)
        else:
            os.environ["CLAUDE_KB_DIR"] = old_legacy
        paths._PINNED_DIR = old_cache


@contextmanager
def gate_backend(name: str | None) -> Iterator[None]:
    old = os.environ.get("LATCH_GATE_BACKEND")
    if name:
        os.environ["LATCH_GATE_BACKEND"] = name
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("LATCH_GATE_BACKEND", None)
        else:
            os.environ["LATCH_GATE_BACKEND"] = old


def create_fixture(root: Path) -> DemoFixture:
    project = root / "project" / "no-history-demo-app"
    kb_dir = root / "kb"
    project.mkdir(parents=True, exist_ok=True)
    kb_dir.mkdir(parents=True, exist_ok=True)
    (project / "GOVERNANCE.md").write_text(DEMO_GOVERNANCE, encoding="utf-8")
    (project / "app.py").write_text(
        "def send_email(message: str) -> None:\n"
        "    print(f\"send inline: {message}\")\n",
        encoding="utf-8",
    )
    with pinned_kb_dir(kb_dir):
        conn = db.connect(str(project))
        try:
            decision_id = db.insert_node(
                conn,
                kind="decision",
                title=DEMO_DECISION_TITLE,
                body=DEMO_DECISION_BODY,
                status="canonical",
                session_id="no-history-demo",
            )
        finally:
            conn.close()
    return DemoFixture(
        root=root,
        project=project,
        kb_dir=kb_dir,
        decision_id=decision_id,
    )


def run_gate(fixture: DemoFixture, *, request: str, use_llm: bool,
             max_chains: int, backend: str | None) -> dict:
    with pinned_kb_dir(fixture.kb_dir), gate_backend(backend):
        conn = db.connect(str(fixture.project))
        try:
            return gate.run_gate(
                conn,
                request,
                project_path=str(fixture.project),
                use_llm=use_llm,
                max_chains=max_chains,
            )
        finally:
            conn.close()


def _chain_contains_decision(out: dict, decision_id: int) -> bool:
    chains = out.get("chains") or {}
    seed_ids = {s.get("id") for s in chains.get("seeds") or [] if isinstance(s, dict)}
    if decision_id in seed_ids:
        return True
    for chain in chains.get("chains") or []:
        for ev in chain.get("evidence") or []:
            if ev.get("id") == decision_id:
                return True
    return False


def render_receipt(fixture: DemoFixture, out: dict, *, request: str,
                   keep: bool, use_llm: bool) -> str:
    verdict = out.get("verdict") or {}
    findings = out.get("findings") or {}
    evidence = out.get("evidence") or []
    rec = findings.get("recommendation") or verdict.get("recommendation")
    rec_label = rec or "SKIPPED"
    summary = str(findings.get("summary") or verdict.get("summary") or "").strip()
    risk = str(findings.get("risk_if_proceed") or verdict.get("risk_if_proceed") or "").strip()
    error = str(verdict.get("error") or "").strip()
    has_seed = _chain_contains_decision(out, fixture.decision_id)

    lines = [
        "Latch no-history demo",
        "=====================",
        "",
        "This fixture used no personal Claude/Codex history.",
        f"Fixture project: {fixture.project}",
        f"Fixture KB: {fixture.kb_dir}",
        f"Seeded decision: id={fixture.decision_id} ({DEMO_DECISION_TITLE})",
        f"Request: {request}",
        "",
        "Latch gate receipt:",
        "Latch ran latch_gate on the fixture request.",
        f"Recommendation: {rec_label}",
    ]
    if summary:
        lines.append(f"Summary: {summary}")
    if risk:
        lines.append(f"Risk if proceed: {risk}")
    if error:
        lines.append(f"Gate note: {error}")
    if evidence:
        lines.append("Cited evidence:")
        for item in evidence:
            lines.append(
                f"- id={item.get('id')} {item.get('kind')} "
                f"status={item.get('status')}: {item.get('title')}"
            )
    elif has_seed:
        lines.append(
            "Cited evidence: classifier skipped or errored, but gate assembly "
            f"retrieved the seeded decision id={fixture.decision_id}."
        )
    else:
        lines.append(
            "Cited evidence: no cited nodes returned; inspect the fixture KB if "
            "the gate did not retrieve the seeded decision."
        )
    lines.extend([
        "",
        "Expected proof:",
        "A live classifier should return MODIFY or DO_NOT_PROCEED, cite the "
        "seeded governance decision, and recommend the single-process path "
        "before files change.",
    ])
    if not use_llm:
        lines.append("Offline mode: --no-llm skipped classifier judgment by design.")
    if keep:
        lines.append(f"Kept fixture at: {fixture.root}")
    else:
        lines.append("Fixture will be removed after this run.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.work_dir).resolve() if args.work_dir else Path(
        tempfile.mkdtemp(prefix="latch-no-history-demo-")
    )
    if args.work_dir:
        root.mkdir(parents=True, exist_ok=True)
    fixture: DemoFixture | None = None
    try:
        fixture = create_fixture(root)
        out = run_gate(
            fixture,
            request=args.request,
            use_llm=not args.no_llm,
            max_chains=args.max_chains,
            backend=args.backend,
        )
        sys.stdout.write(render_receipt(
            fixture,
            out,
            request=args.request,
            keep=args.keep or bool(args.work_dir),
            use_llm=not args.no_llm,
        ))
        return 0
    finally:
        if fixture and not args.keep and not args.work_dir:
            shutil.rmtree(fixture.root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
