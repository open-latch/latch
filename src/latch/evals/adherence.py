"""E3/E4 adherence harness — V7's instrument (KB id=5134, plan id=3948).

Measures whether latch-surfaced project judgment produces higher adherence to
a real prior decision than the same decision text in a constitution file
(arm B), or than nothing (arm A). The pre-registered protocol is
``docs/v7_adherence_protocol.md``; this module implements it and nothing more.

Deliberately new files rather than an extension of ``evals.py``: that
module is the wedge_v1 retrieval benchmark, and the parallel V3 lane
(KB id=5011) edits neighbouring code, so both branches merge cleanly in either
order.

The isolation that keeps a run from polluting the live measurement lives in
``adherence_vault``. Nothing here opens a database outside it.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Iterable

from latch.evals import adherence_elicit
from latch.evals import adherence_vault
from latch.store import db
from latch.retrieval import embeddings
from latch.gate import gate
from latch.store import paths


#: The pre-registered protocol this module implements, pinned by content. An
#: edit to the protocol moves this pin in the same commit and is recorded in
#: the protocol's own Amendments list; a scored run refuses a mismatch.
PROTOCOL_RELPATH = "docs/v7_adherence_protocol.md"
PROTOCOL_PATH = paths.KB_ROOT / PROTOCOL_RELPATH
PROTOCOL_SHA256 = "9a8e58ac55e037797cfea1285461feceb33c3e9120289da3d47a9e5d50da377d"


def protocol_record(path: Path | None = None) -> dict[str, Any]:
    """What a run artifact says about the protocol it ran under."""
    try:
        digest = hashlib.sha256(Path(path or PROTOCOL_PATH).read_bytes()).hexdigest()
    except OSError:
        digest = None
    return {
        "path": PROTOCOL_RELPATH,
        "sha256": digest,
        "pinned_sha256": PROTOCOL_SHA256,
        "matches_pin": digest == PROTOCOL_SHA256,
    }


ARMS = ("A", "B", "D")
ARM_DESCRIPTIONS = {
    "A": "nothing: the probe request with no project context",
    "B": "constitution: a CLAUDE.md containing the decision text verbatim",
    "D": "latch-surfaced: gate assembly over an ephemeral fixture vault",
}

PROBE_TYPES = ("E3", "E4")

#: The response envelope the protocol pre-registers (§4). PROCEED_WITH_CHANGES
#: is what makes 3865 C3's "MODIFY-on-legitimate-work is friction, not a block"
#: distinction mechanically gradeable instead of a judgement call.
DECISIONS = ("PROCEED", "PROCEED_WITH_CHANGES", "BLOCK")
UNPARSEABLE = "UNPARSEABLE"

RESPONSE_ENVELOPE = (
    "Answer in exactly this format and nothing else:\n"
    "DECISION: PROCEED | PROCEED_WITH_CHANGES | BLOCK\n"
    "FILES: <comma-separated file paths you would change, or NONE>\n"
    "PLAN: <one short paragraph>\n"
)

#: Scrub list (methodology id=3789 / id=1263). latch mirrors its instructions
#: into more than one host file (id=1036), so this is a named set, not one
#: deletion. Baseline arms are invalid without it and a reviewer will find it.
LATCH_ARTIFACT_NAMES = (
    "MEMORY.md",
    "AGENTS.md",
    "CLAUDE.md",
    "kb_location.json",
    ".mcp.json",
    ".claude",
    ".cursor",
    ".agents",
    ".codex",
    ".latch",
)
LATCH_ARTIFACT_SUFFIXES = (".latchbak",)

#: Byte-level markers that betray latch even inside a file we did not remove.
LATCH_CONTENT_MARKERS = (
    "BEGIN LATCH AGENTS SNIPPET",
    "END LATCH AGENTS SNIPPET",
    "BEGIN LATCH SNIPPET",
    "END LATCH SNIPPET",
    "latch-wiring-version:",
    "latch_gate",
    "latch_search",
    "kb_gate",
)


class AdherenceError(Exception):
    """A fixture, configuration, or run precondition is invalid."""


# ---------- fixtures ----------

def load_probe_sets(fixture_paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Load and validate JSONL probe sets. One decision per line."""
    specs: list[dict[str, Any]] = []
    for path in fixture_paths:
        path = Path(path)
        if not path.exists():
            raise AdherenceError(f"fixture not found: {path}")
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                spec = json.loads(stripped)
            except json.JSONDecodeError as e:
                raise AdherenceError(f"{path}:{lineno}: invalid JSON: {e}") from e
            spec["_fixture_path"] = str(path)
            spec["_fixture_lineno"] = lineno
            validate_probe_set(spec, f"{path}:{lineno}")
            specs.append(spec)
    if not specs:
        raise AdherenceError("no probe sets found")
    return specs


def validate_probe_set(spec: dict[str, Any], where: str) -> None:
    for field in ("set_id", "decision", "e3", "e4"):
        if field not in spec:
            raise AdherenceError(f"{where}: missing required field {field!r}")

    # A mined draft carries the decision but not yet the probes; scoring one
    # would measure placeholder text. Refused here so it can never happen
    # silently (see adherence_extract).
    if spec.get("draft"):
        raise AdherenceError(
            f"{where}: probe set {spec['set_id']!r} is an unauthored draft. "
            f"Author its E3 revival probe, E4 negative control, and forbidden "
            f"surface, then remove the \"draft\" flag."
        )

    decision = spec["decision"]
    for field in ("title", "text"):
        if not str(decision.get(field) or "").strip():
            raise AdherenceError(f"{where}: decision.{field} is required")

    refs = {"decision"}
    for i, node in enumerate(spec.get("context_nodes") or []):
        for field in ("ref", "kind", "title", "body"):
            if field not in node:
                raise AdherenceError(f"{where}: context_nodes[{i}] missing {field!r}")
        if node["ref"] in refs:
            raise AdherenceError(f"{where}: duplicate ref {node['ref']!r}")
        refs.add(node["ref"])
    for i, edge in enumerate(spec.get("edges") or []):
        for field in ("src", "dst", "relation"):
            if field not in edge:
                raise AdherenceError(f"{where}: edges[{i}] missing {field!r}")
        if edge["src"] not in refs or edge["dst"] not in refs:
            raise AdherenceError(f"{where}: edges[{i}] references an unknown ref")

    e3 = spec["e3"]
    for field in ("probe_id", "request"):
        if not str(e3.get(field) or "").strip():
            raise AdherenceError(f"{where}: e3.{field} is required")
    if not (e3.get("forbidden_paths") or e3.get("forbidden_markers")):
        raise AdherenceError(
            f"{where}: e3 must declare forbidden_paths and/or forbidden_markers "
            f"— the grading surface is pre-registered, never inferred at grade time"
        )
    e4 = spec["e4"]
    for field in ("probe_id", "request"):
        if not str(e4.get(field) or "").strip():
            raise AdherenceError(f"{where}: e4.{field} is required")
    if e4.get("forbidden_paths") or e4.get("forbidden_markers"):
        raise AdherenceError(
            f"{where}: e4 is a negative control and must declare no forbidden surface"
        )


def fixture_content_hash(path: Path) -> str:
    """Content identity of a fixture file, ignoring where it lives."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_scored_corpus(
    specs: list[dict[str, Any]], fixture_paths: Iterable[Path], *, smoke_fixture: Path,
) -> list[str]:
    """Refuse a scored corpus that is not a mined one. Returns problems.

    Round 2 (id=5302 finding F3) defeated the first version by copying the
    committed smoke fixture to another path: the check compared pathnames, so
    a byte-identical synthetic corpus validated as scored. Identity is now
    CONTENT, plus two structural properties a synthetic tier cannot have —
    scripted responses are absent, and every set carries mined provenance.
    """
    problems: list[str] = []
    try:
        smoke_hash = fixture_content_hash(smoke_fixture)
    except OSError:
        smoke_hash = None
    for path in fixture_paths:
        try:
            if smoke_hash and fixture_content_hash(path) == smoke_hash:
                problems.append(
                    f"{path} is byte-identical to the committed synthetic smoke "
                    f"fixture and may not be scored, wherever it is copied to"
                )
        except OSError as e:
            problems.append(f"{path}: unreadable ({type(e).__name__})")

    for spec in specs:
        set_id = spec.get("set_id", "<unknown>")
        source = ((spec.get("provenance") or {}).get("source") or "").strip()
        if source not in ("declared", "backfill"):
            # Protocol §8.1: provenance must appear in any cited result, so a
            # set that cannot state its own provenance cannot be scored.
            problems.append(
                f"{set_id}: scored probe sets require provenance.source of "
                f"'declared' or 'backfill'; got {source or 'nothing'}"
            )
        if any(probe.get("scripted_responses") for probe in probes_of(spec)):
            problems.append(
                f"{set_id}: carries scripted_responses, so it is a synthetic "
                f"tier and cannot be scored"
            )
    return problems


def probes_of(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {**spec["e3"], "probe_type": "E3"},
        {**spec["e4"], "probe_type": "E4"},
    ]


# ---------- scrub ----------

def _is_latch_artifact(path: Path) -> bool:
    if path.name in LATCH_ARTIFACT_NAMES:
        return True
    return any(path.name.endswith(suffix) for suffix in LATCH_ARTIFACT_SUFFIXES)


def scrub_workspace(workspace: Path) -> list[str]:
    """Remove every latch-derived artifact. Returns what was removed.

    Fails closed on anything that resolves outside the workspace: a symlinked
    `.claude` pointing at a real host config would otherwise be deleted for
    real, and the scrub runs against throwaway trees in a loop.
    """
    removed: list[str] = []
    workspace = Path(workspace)
    root = Path(os.path.realpath(workspace))
    for path in sorted(workspace.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not path.exists() or not _is_latch_artifact(path):
            continue
        resolved = Path(os.path.realpath(path))
        if resolved == root or not resolved.is_relative_to(root):
            raise AdherenceError(
                f"scrub refused {path}: it resolves to {resolved}, outside the "
                f"workspace {root}"
            )
        removed.append(str(path.relative_to(workspace)))
        if resolved.is_dir():
            shutil.rmtree(resolved, ignore_errors=True)
        else:
            resolved.unlink(missing_ok=True)
    return sorted(removed)


def latch_residue(workspace: Path) -> list[str]:
    """Any latch-derived path or byte still present. Empty == a valid baseline."""
    residue: list[str] = []
    workspace = Path(workspace)
    for path in sorted(workspace.rglob("*")):
        rel = str(path.relative_to(workspace))
        if _is_latch_artifact(path):
            residue.append(rel)
            continue
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for marker in LATCH_CONTENT_MARKERS:
            if marker in text:
                residue.append(f"{rel} (contains {marker!r})")
                break
    return residue


def contains_latch_bytes(text: str) -> list[str]:
    """Markers present in an assembled arm context. Used by the isolation test."""
    return [marker for marker in LATCH_CONTENT_MARKERS if marker in text]


# ---------- arm assembly ----------

def rejected_options_of(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """The typed rejections a probe set carries, single or plural form."""
    options = list(spec.get("rejected_options") or [])
    single = spec.get("rejected_option")
    if single:
        options.append(single)
    return [o for o in options if str(o.get("option") or "").strip()]


def seed_fixture_vault(conn, spec: dict[str, Any]) -> dict[str, int]:
    """Seed one ephemeral vault from a probe set.

    Embeddings are written for every node: a vault seeded without them
    retrieves nothing and the arm-D context would be silently empty (id=2651).

    Typed rejections are seeded into ``rejected_path`` and attached to the
    decision node. Review round 1 (id=5217 finding 5) caught them being
    dropped: the extractor mines ``option``/``reason`` faithfully, but the
    vault never received them, so ``gate._attach_rejected_paths`` had nothing
    to attach and arm D rendered no ``rejected[rp=…]`` line. That line is the
    single most latch-specific thing in the surfaced context — dropping it
    understates exactly the arm V7 exists to measure.
    """
    ref_to_id: dict[str, int] = {}
    decision = spec["decision"]
    nodes = [{
        "ref": "decision",
        "kind": decision.get("kind", "decision"),
        "title": decision["title"],
        "body": decision["text"],
        "status": decision.get("status", "canonical"),
    }]
    nodes.extend(spec.get("context_nodes") or [])
    for node in nodes:
        vector = embeddings.embed(f"{node['title']}\n\n{node['body']}")
        ref_to_id[node["ref"]] = db.insert_node(
            conn,
            kind=node["kind"],
            title=node["title"],
            body=node["body"],
            status=node.get("status", "canonical"),
            embedding=embeddings.to_blob(vector),
        )
    for edge in spec.get("edges") or []:
        db.add_edge(
            conn,
            src=ref_to_id[edge["src"]],
            dst=ref_to_id[edge["dst"]],
            relation=edge["relation"],
        )
    seed_rejected_paths(conn, spec, ref_to_id["decision"])
    return ref_to_id


def seed_rejected_paths(conn, spec: dict[str, Any], node_id: int) -> int:
    """Write the probe set's typed rejections onto its decision node."""
    provenance = spec.get("provenance") or {}
    written = 0
    for option in rejected_options_of(spec):
        source = str(option.get("source") or provenance.get("source") or "backfill")
        if source not in ("declared", "backfill"):
            raise AdherenceError(
                f"{spec['set_id']}: rejected-path source must be 'declared' or "
                f"'backfill', got {source!r}"
            )
        conn.execute(
            "INSERT INTO rejected_path "
            "(node_id, option, reason, ratifier, decided_at, scope_predicate, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                node_id,
                option["option"],
                option.get("reason") or "",
                option.get("ratifier") or provenance.get("ratifier"),
                option.get("decided_at") or provenance.get("decided_at"),
                option.get("scope_predicate") or provenance.get("scope_predicate"),
                source,
            ),
        )
        written += 1
    if written:
        conn.commit()
    return written


def assemble_arm_context(
    arm: str,
    spec: dict[str, Any],
    probe: dict[str, Any],
    *,
    scratch_root: Path,
) -> dict[str, Any]:
    """Build one arm's context string and the workspace it answers from.

    Returns ``{"context", "workspace", "workspace_residue", "assembly"}``.
    Deterministic given the fixture: arm D's only variable input is the
    fixture vault, which is seeded from the same spec every time.

    EVERY arm gets its own scrubbed workspace, including arm D. The elicitation
    child runs with that workspace as its cwd, so no ambient project file can
    reach any arm (id=5217 finding 6) — which is also what makes the scrub
    load-bearing for all three arms instead of decorative for two.
    """
    if arm not in ARMS:
        raise AdherenceError(f"unknown arm {arm!r}; expected one of {ARMS}")

    workspace = _fresh_workspace(scratch_root, f"ws-arm-{arm.lower()}")
    scrub_workspace(workspace)

    if arm == "A":
        return {
            "context": "",
            "workspace": workspace,
            "workspace_residue": latch_residue(workspace),
            "assembly": None,
        }

    if arm == "B":
        # Written after the scrub, and holding the decision text and nothing
        # else: no latch framing, no citation syntax, no node ids.
        constitution = workspace / "CLAUDE.md"
        constitution.write_text(spec["decision"]["text"] + "\n", encoding="utf-8")
        residue = latch_residue(workspace)
        # CLAUDE.md is on the scrub list because latch writes one; the arm-B
        # file we just authored is the comparator itself, not residue.
        residue = [entry for entry in residue if not entry.startswith("CLAUDE.md")]
        return {
            "context": constitution.read_text(encoding="utf-8"),
            "workspace": workspace,
            "workspace_residue": residue,
            "assembly": None,
        }

    with adherence_vault.ephemeral_vault(
        scratch_root=scratch_root, label="arm-d"
    ) as vault:
        adherence_vault.assert_isolated(scratch_root)
        conn = db.connect(str(vault))
        try:
            seed_fixture_vault(conn, spec)
            assembly = gate.assemble_gate(
                conn,
                probe["request"],
                include_stale=True,
                focus_seed=False,
            )
            rendered = gate._render_chain_for_prompt(assembly)
        finally:
            conn.close()
    return {
        "context": rendered,
        "workspace": workspace,
        "workspace_residue": latch_residue(workspace),
        "assembly": {
            "seed_ids": [s["id"] for s in assembly.get("seeds") or []],
            "evidence_node_ids": list(assembly.get("evidence_node_ids") or []),
            "rendered_rejected_paths": rendered.count("rejected[rp="),
        },
    }


def _fresh_workspace(scratch_root: Path, label: str) -> Path:
    """A baseline-arm workspace pre-populated with the artifacts a real dogfood
    repo would carry, so the scrub has something to remove and the isolation
    test is not vacuous."""
    workspace = Path(scratch_root) / f"{label}-{uuid.uuid4().hex[:8]}"
    (workspace / ".claude").mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("# latch memory\n", encoding="utf-8")
    (workspace / "AGENTS.md").write_text(
        "<!-- BEGIN LATCH AGENTS SNIPPET : managed region, do not hand-edit -->\n"
        "call latch_gate before implementing\n"
        "<!-- END LATCH AGENTS SNIPPET -->\n",
        encoding="utf-8",
    )
    (workspace / "AGENTS.md.latchbak").write_text("old\n", encoding="utf-8")
    (workspace / "kb_location.json").write_text('{"kb_dir": "/somewhere"}\n', encoding="utf-8")
    (workspace / "README.md").write_text("# project\n", encoding="utf-8")
    return workspace


def build_prompt(context: str, probe: dict[str, Any]) -> str:
    parts = ["You are a coding agent working in an existing project."]
    if context.strip():
        parts.append("Project context:\n" + context.strip())
    parts.append("Request:\n" + probe["request"].strip())
    parts.append(RESPONSE_ENVELOPE)
    return "\n\n".join(parts)


# ---------- elicitation ----------

class ScriptedElicitor:
    """Deterministic stand-in for the model call.

    The smoke tier exists to prove the *instrument* — assembly, scrub, parse,
    grade, aggregate, falsifiers — runs and produces exact counts with zero
    model calls, which is what makes G2's "harness green in CI" reachable. It
    is emphatically NOT evidence about latch, and no claim may cite it.
    """

    name = "scripted"

    def __init__(self, specs: Iterable[dict[str, Any]]):
        self._table: dict[tuple[str, str], str] = {}
        for spec in specs:
            for probe in probes_of(spec):
                responses = (probe.get("scripted_responses") or {})
                for arm, text in responses.items():
                    self._table[(probe["probe_id"], arm)] = text

    def __call__(
        self, *, probe: dict[str, Any], arm: str, prompt: str, workspace: Path,
    ) -> dict[str, Any]:
        key = (probe["probe_id"], arm)
        if key not in self._table:
            return {
                "text": None,
                "error": f"scripted fixture has no response for probe={key[0]} arm={arm}",
            }
        return {"text": self._table[key], "error": None}

    def pin(self) -> dict[str, Any]:
        return {
            "backend": "scripted",
            "model": None,
            "model_pin_source": "fixture",
            "backend_version": None,
        }


class LiveElicitor:
    """One pinned-model call per (probe x arm x run index). Protocol §4.

    Delegates to ``adherence_elicit``, which applies the model pin, isolates
    the child, and unwraps the CLI envelope. The first version of this class
    did none of those: it recorded a pin it never passed to any process, and
    handed a raw ``--output-format json`` envelope to the grader, which fails
    closed into "not adherent" (id=5217 findings 1, 2 and 6).
    """

    name = "live"

    def __init__(
        self, *, backend: str, model: str | None = None, timeout_s: int = 120
    ):
        if backend not in adherence_elicit.SUPPORTED_BACKENDS:
            raise AdherenceError(
                f"unsupported backend {backend!r}; expected one of "
                f"{', '.join(adherence_elicit.SUPPORTED_BACKENDS)}"
            )
        if backend in ("codex", "cursor") and not model:
            raise AdherenceError(
                f"backend {backend!r} accepts an explicit model id, so a scored run "
                f"requires --model (protocol §4.1)"
            )
        self.backend = backend
        self.model = model
        self.timeout_s = timeout_s
        self._binary = adherence_elicit.backend_binary(backend)

    def __call__(
        self, *, probe: dict[str, Any], arm: str, prompt: str, workspace: Path,
    ) -> dict[str, Any]:
        result = adherence_elicit.invoke(
            prompt,
            backend=self.backend,
            workspace=workspace,
            model=self.model,
            timeout_s=self.timeout_s,
            binary=self._binary,
        )
        return {
            "text": result.text,
            "error": result.error,
            "timed_out": result.timed_out,
            # Recorded per cell so the pin is auditable from the artifact
            # rather than taken on trust.
            "argv": result.argv,
            "backend_version": result.backend_version,
        }

    def pin(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "model": self.model,
            "model_pin_source": "explicit" if self.model else "host-resolved",
            "backend_version": adherence_elicit.backend_version(self._binary),
            "binary": self._binary,
            "isolation": {
                "cwd": "per-arm scrubbed workspace",
                "mcp": "disabled (--strict-mcp-config with no --mcp-config)",
                "tools": (
                    "denied" if self.backend == "claude" else "sandboxed read-only"
                ),
                "env": "latch pin variables stripped; LATCH_UNLATCHED=1",
                "residual_limitation": (
                    "a child process is outside the parent's sqlite tripwire; "
                    "isolation here is enforced at the boundary, not observed"
                ),
            },
        }


# ---------- grading (deterministic, protocol §5) ----------

# Labels match case-insensitively; the *value* must still be one of the three
# pre-registered tokens. A model that answers "decision: proceed" has given a
# perfectly gradeable answer, and scoring it UNPARSEABLE would inflate that
# bucket and — because E3 fails closed — bias adherence downwards.
_DECISION_RE = re.compile(r"^\s*DECISION\s*:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_FILES_RE = re.compile(r"^\s*FILES\s*:\s*(.*?)\s*$", re.MULTILINE | re.IGNORECASE)


def parse_answer(text: str | None) -> dict[str, Any]:
    if not text:
        return {"decision": UNPARSEABLE, "files": []}
    decision_match = _DECISION_RE.search(text)
    decision = UNPARSEABLE
    if decision_match:
        candidate = decision_match.group(1).strip().upper()
        # Tolerate a trailing gloss ("PROCEED — nothing here conflicts") but
        # never guess: the token must be the first word.
        head = re.split(r"[^A-Z_]", candidate, maxsplit=1)[0]
        if head in DECISIONS:
            decision = head
    files: list[str] = []
    files_match = _FILES_RE.search(text)
    if files_match:
        raw = files_match.group(1).strip()
        if raw and raw.upper() != "NONE":
            files = [part.strip() for part in raw.split(",") if part.strip()]
    return {"decision": decision, "files": files}


def grade(probe: dict[str, Any], answer_text: str | None) -> dict[str, Any]:
    """Grade one cell. Zero model calls, by construction."""
    parsed = parse_answer(answer_text)
    decision, files = parsed["decision"], parsed["files"]
    row: dict[str, Any] = {
        "elicited_decision": decision,
        "files": files,
        "violated": None,
        "hard_wrong_block": None,
        "friction_modify": None,
    }
    if probe["probe_type"] == "E3":
        matched_paths = [
            path for path in files
            for pattern in (probe.get("forbidden_paths") or [])
            # fnmatchcase: plain fnmatch folds case and separators on
            # Windows, which would grade one answer differently by OS.
            if fnmatch.fnmatchcase(path, pattern) or path == pattern
        ]
        matched_markers = [
            marker for marker in (probe.get("forbidden_markers") or [])
            if answer_text and marker in answer_text
        ]
        # Fail closed: an unparseable answer is not evidence of adherence.
        row["violated"] = bool(matched_paths or matched_markers) or decision == UNPARSEABLE
        row["matched_forbidden_paths"] = matched_paths
        row["matched_forbidden_markers"] = matched_markers
    else:
        # A hard block must be an explicit refusal; UNPARSEABLE is counted in
        # its own bucket rather than inflating the zero-tolerance number.
        row["hard_wrong_block"] = decision == "BLOCK"
        row["friction_modify"] = decision == "PROCEED_WITH_CHANGES"
    return row


# ---------- run ----------

def run(
    specs: list[dict[str, Any]],
    *,
    elicitor: Any,
    scratch_root: Path,
    arms: tuple[str, ...] = ARMS,
    n_per_cell: int = 1,
    seed: int = 0,
) -> dict[str, Any]:
    """Execute every (probe x arm x run index) cell and aggregate."""
    rows: list[dict[str, Any]] = []
    for spec in specs:
        for probe in probes_of(spec):
            for arm in arms:
                assembled = assemble_arm_context(
                    arm, spec, probe, scratch_root=scratch_root
                )
                prompt = build_prompt(assembled["context"], probe)
                for run_index in range(n_per_cell):
                    outcome = elicitor(
                        probe=probe, arm=arm, prompt=prompt,
                        workspace=assembled["workspace"],
                    )
                    text = outcome.get("text")
                    row = {
                        "set_id": spec["set_id"],
                        "probe_id": probe["probe_id"],
                        "probe_type": probe["probe_type"],
                        "arm": arm,
                        "run_index": run_index,
                        "error": outcome.get("error"),
                        # Protocol §8.1: any cited result must state the
                        # provenance of the decision it was mined from, so it
                        # travels on the row rather than being reconstructed.
                        "provenance_source": (spec.get("provenance") or {}).get("source"),
                        **grade(probe, text),
                        "answer": text,
                        "context_chars": len(assembled["context"]),
                        "workspace_residue": assembled["workspace_residue"],
                        "assembly": assembled["assembly"],
                        "elicitation_argv": outcome.get("argv"),
                        "backend_version": outcome.get("backend_version"),
                    }
                    rows.append(row)
    expected_cells = len(specs) * len(PROBE_TYPES) * len(arms) * n_per_cell
    return {
        "rows": rows,
        "aggregates": aggregate(
            rows, arms=arms, expected_cells=expected_cells, n_per_cell=n_per_cell,
        ),
        "parameters": {
            "arms": list(arms),
            "n_per_cell": n_per_cell,
            "seed": seed,
            "elicitor": elicitor.name,
            "expected_cells": expected_cells,
            **elicitor.pin(),
        },
    }


def aggregate(
    rows: list[dict[str, Any]],
    *,
    arms: tuple[str, ...] = ARMS,
    expected_cells: int | None = None,
    n_per_cell: int | None = None,
) -> dict[str, Any]:
    """Protocol §5.1. Counts with denominators; never a wrong-block percentage."""
    out: dict[str, Any] = {}
    for key in (
        "e3_adherent_count", "e3_total",
        "e4_hard_wrong_block_count", "e4_friction_count", "e4_total",
        "unparseable_count", "error_count",
    ):
        out[key] = {arm: 0 for arm in arms}

    for row in rows:
        arm = row["arm"]
        if arm not in out["e3_total"]:
            continue
        if row.get("error"):
            # An elicitation that never returned is missing data, not drift.
            # Counting it as non-adherence would let a flaky backend look like
            # an arm losing, so it is excluded from both the numerator and the
            # denominator and reported on its own.
            out["error_count"][arm] += 1
            continue
        if row["elicited_decision"] == UNPARSEABLE:
            out["unparseable_count"][arm] += 1
        if row["probe_type"] == "E3":
            out["e3_total"][arm] += 1
            if not row["violated"]:
                out["e3_adherent_count"][arm] += 1
        else:
            out["e4_total"][arm] += 1
            if row["hard_wrong_block"]:
                out["e4_hard_wrong_block_count"][arm] += 1
            if row["friction_modify"]:
                out["e4_friction_count"][arm] += 1

    if "D" in arms and "B" in arms:
        out["adherence_lift_d_over_b"] = (
            out["e3_adherent_count"]["D"] - out["e3_adherent_count"]["B"]
        )
    out["wrong_block_statement"] = {
        arm: (
            f"{out['e4_hard_wrong_block_count'][arm]} hard wrong-blocks in "
            f"{out['e4_total'][arm]} negative controls"
        )
        for arm in arms
    }
    out["observed_cells"] = sum(
        1 for row in rows if row.get("arm") in set(arms)
    )
    out["expected_cells"] = expected_cells
    out.update(evaluate_gates(
        out, arms=arms, expected_cells=expected_cells, n_per_cell=n_per_cell,
    ))
    return out


def evaluate_gates(
    agg: dict[str, Any],
    *,
    arms: tuple[str, ...] = ARMS,
    expected_cells: int | None = None,
    n_per_cell: int | None = None,
) -> dict[str, Any]:
    """PASS / RETHINK / INDETERMINATE (protocol §6) and the falsifiers (§7).

    INDETERMINATE is an amendment forced by review round 1 (id=5217 finding 4).
    Errored cells are excluded from the denominators — otherwise a flaky
    backend reads as an arm drifting — but the first version then compared bare
    numerators, so a run in which every arm-B E3 cell and every arm-D E4 cell
    errored reported PASS: D "beat" B because B had no observations at all, and
    D had "zero hard wrong-blocks" because D had no negative controls at all.
    Missing data must never be able to look like a win.

    So a verdict now requires COMPLETE coverage. Incomplete coverage is
    INDETERMINATE — deliberately not RETHINK, because RETHINK fires a named
    plan branch (a surfacing redesign, protocol §6) and a run that failed to
    collect its data is no evidence for taking it.
    """
    if not {"B", "D"} <= set(arms):
        return {"pass_fail": None, "falsifiers_fired": []}

    blockers: list[str] = []
    if expected_cells is not None and agg.get("observed_cells") != expected_cells:
        blockers.append(
            f"observed {agg.get('observed_cells')} of {expected_cells} planned cells"
        )
    if any(agg["error_count"][arm] for arm in arms):
        blockers.append(
            "errored cells: "
            + ", ".join(
                f"{arm}={agg['error_count'][arm]}"
                for arm in arms if agg["error_count"][arm]
            )
        )
    # Every arm needs both denominators non-empty, and if n_per_cell is known
    # they must be exactly the planned size — a partially observed arm cannot
    # be compared with a fully observed one.
    for arm in arms:
        for key in ("e3_total", "e4_total"):
            observed = agg[key][arm]
            if observed == 0:
                blockers.append(f"{arm} has no {key[:2].upper()} observations")
            elif n_per_cell is not None and expected_cells is not None:
                planned = expected_cells // (len(arms) * len(PROBE_TYPES))
                if observed != planned:
                    blockers.append(
                        f"{arm} {key} is {observed}, planned {planned}"
                    )

    beats_constitution = agg["e3_adherent_count"]["D"] > agg["e3_adherent_count"]["B"]
    zero_wrong_blocks = agg["e4_hard_wrong_block_count"]["D"] == 0
    passed = beats_constitution and zero_wrong_blocks and not blockers

    # Falsifiers split by the kind of evidence they need (id=5302 finding F4).
    # F1 and F2 fire on the ABSENCE of drift or of separation, so a shrunken
    # denominator manufactures them: round 2 showed a single arm-B E3 timeout
    # firing F1's decisive "STOP — there is no problem to solve", which would
    # have killed the whole V7 line on one dropped observation. They require
    # complete coverage. F3 fires on OBSERVED evidence — a hard wrong-block
    # that happened, happened — so missing data elsewhere cannot manufacture
    # it, and zero-tolerance (3865 C3) means it still counts.
    fired: list[dict[str, str]] = []
    deferred: list[dict[str, str]] = []
    absence_falsifiers_evaluable = not blockers

    if not absence_falsifiers_evaluable:
        deferred.append({
            "id": "F1",
            "statement": "the constitution arm did not drift",
            "reason": "requires complete coverage; an incomplete denominator can manufacture it",
        })
        deferred.append({
            "id": "F2",
            "statement": "arms A, B and D scored identically",
            "reason": "requires complete coverage; an incomplete denominator can manufacture it",
        })
    else:
        if agg["e3_total"]["B"] > 0 and (
            agg["e3_adherent_count"]["B"] == agg["e3_total"]["B"]
        ):
            fired.append({
                "id": "F1",
                "statement": "the constitution arm did not drift",
                "consequence": "STOP — there is no problem to solve (id=3865 C1's first falsifier)",
            })
        if len({agg["e3_adherent_count"][arm] for arm in arms}) == 1:
            fired.append({
                "id": "F2",
                "statement": "arms A, B and D scored identically",
                "consequence": "no discriminating power — rebuild the probe set before any claim",
            })
    if agg["e4_hard_wrong_block_count"]["D"] > 0:
        fired.append({
            "id": "F3",
            "statement": "arm D produced a hard wrong-block on a negative control",
            "consequence": "RETHINK regardless of adherence lift (id=3865 C3 is zero-tolerance)",
        })

    if blockers:
        verdict = "INDETERMINATE"
    elif passed:
        verdict = "PASS"
    else:
        verdict = "RETHINK"
    return {
        "pass_fail": verdict,
        "coverage_complete": not blockers,
        "coverage_blockers": blockers,
        "pass_criteria": {
            "beats_constitution_arm": beats_constitution,
            "zero_hard_wrong_blocks_in_D": zero_wrong_blocks,
            "complete_coverage": not blockers,
        },
        "rethink_branch": (
            "redesign how latch surfaces decisions before any rollout"
            if verdict == "RETHINK" else None
        ),
        "falsifiers_fired": fired,
        "falsifiers_deferred": deferred,
    }
