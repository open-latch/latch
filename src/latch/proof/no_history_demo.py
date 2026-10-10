"""Turnkey no-history rejected-path demo fixture.

This gives a fresh install a public-safe proof path when there are no Claude or
Codex sessions to seed: create a throwaway project, seed one rejected-path
decision into a throwaway KB, run latch_gate, and print the receipt.
"""
from __future__ import annotations
if __package__ in (None, ""):
    import sys, pathlib
    sys.path.insert(0, str(next(p for p in pathlib.Path(__file__).resolve().parents if p.name == "src")))

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from latch.store import db  # noqa: E402
from latch.gate import gate  # noqa: E402
from latch.store import paths  # noqa: E402
from latch.store import vault_identity  # noqa: E402


DEMO_REQUEST = (
    "Add multi-user accounts by moving the datastore from local SQLite to a "
    "hosted Postgres service."
)
DEMO_DECISION_TITLE = "Keep the demo app local-first on SQLite — no hosted database"
DEMO_GOVERNANCE = """# GOVERNANCE

Keep this app local-first: one embedded SQLite file, no server to run and no
account to sign up for. Do not move the datastore to a hosted or client-server
database. If data ever needs to move between machines, add an explicit
export/import step and document its limits — do not add a network database.
"""
DEMO_DECISION_BODY = """No-history demo fixture decision.

Decision: keep the demo app local-first on a single embedded SQLite file.
Rejected path: moving the datastore to a hosted or client-server database (for
example a managed Postgres service) to add multi-user accounts or sync.
Allowed path: stay on local SQLite; if data must move between machines, add an
explicit export/import step and document its limits.
Rationale: local-first keeps the app installable with no server to operate and
no account to create; a hosted database adds an account, a network dependency,
and an operational surface this project deliberately avoids.
Trigger request to catch: Add multi-user accounts by moving the datastore from
local SQLite to a hosted Postgres service.
Source: generated public fixture file GOVERNANCE.md.
"""


@dataclass
class DemoFixture:
    root: Path
    project: Path
    kb_dir: Path
    decision_id: int
    test_root: Path
    test_capability: str
    owns_test_root: bool


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
def _test_runtime(test_root: Path, capability: str) -> Iterator[None]:
    """Activate an authenticated disposable root and restore ambient state."""
    saved = {
        name: os.environ.get(name)
        for name in (
            paths.TEST_ROOT_ENV,
            paths.TEST_CAPABILITY_ENV,
            "LATCH_KB_DIR",
            "CLAUDE_KB_DIR",
        )
    }
    os.environ[paths.TEST_ROOT_ENV] = str(test_root)
    os.environ[paths.TEST_CAPABILITY_ENV] = capability
    os.environ.pop("LATCH_KB_DIR", None)
    os.environ.pop("CLAUDE_KB_DIR", None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _demo_test_identity(root: Path) -> tuple[Path, str, bool]:
    existing = paths.validated_test_root()
    if existing is not None:
        return existing, os.environ[paths.TEST_CAPABILITY_ENV], False
    test_root = root / ".latch-test-runtime"
    test_root.mkdir(parents=True, exist_ok=False)
    capability = secrets.token_hex(32)
    (test_root / paths.TEST_SENTINEL).write_text(
        json.dumps({
            "format": 1,
            "root_uuid": str(uuid.uuid4()),
            "capability_sha256": hashlib.sha256(
                capability.encode("utf-8")
            ).hexdigest(),
        }, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return test_root.resolve(), capability, True


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
    project.mkdir(parents=True, exist_ok=True)
    (project / "GOVERNANCE.md").write_text(DEMO_GOVERNANCE, encoding="utf-8")
    (project / "app.py").write_text(
        "import sqlite3\n\n\n"
        "def get_db() -> sqlite3.Connection:\n"
        "    # Local-first: one embedded SQLite file, no server.\n"
        "    return sqlite3.connect(\"app.db\")\n",
        encoding="utf-8",
    )
    test_root, capability, owns_test_root = _demo_test_identity(root)
    with _test_runtime(test_root, capability):
        kb_dir = paths.project_dir(str(root / "kb"))
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
        test_root=test_root,
        test_capability=capability,
        owns_test_root=owns_test_root,
    )


def run_gate(fixture: DemoFixture, *, request: str, use_llm: bool,
             max_chains: int, backend: str | None) -> dict:
    with _test_runtime(fixture.test_root, fixture.test_capability), \
            pinned_kb_dir(fixture.kb_dir), gate_backend(backend):
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


def cleanup_fixture(fixture: DemoFixture) -> None:
    """Delete the identified test vault before removing other fixture files."""
    with _test_runtime(fixture.test_root, fixture.test_capability):
        identity = vault_identity.read_identity(fixture.kb_dir / "kb.db")
        if identity is not None:
            vault_identity.safe_delete_test_vault(
                fixture.kb_dir,
                expected_uuid=identity.vault_uuid,
                capability=fixture.test_capability,
            )
    shutil.rmtree(fixture.root, ignore_errors=True)


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
        "seeded governance decision, and recommend the local-first SQLite path "
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
            cleanup_fixture(fixture)


if __name__ == "__main__":
    raise SystemExit(main())
