"""M3 authority test: a ruling a human made outranks newer or nearer agent text.

Twenty synthetic cases encode the authority behaviour latch should have, on the
three surfaces that decide it:

- the gate read path (what the classifier is shown, in what order, with what
  label): a ratified ruling outranks a newer contrary note at a closer hop and
  survives the 12-item chain cap; a superseded ruling never renders as current,
  including the live seam shape of a roadmap reconciled by a hold ruling that a
  later ratified ruling retired (vault ids 3948 -> 4044, shape only); a
  decision captured in the moment and a user's standing rule beat a newer agent
  note; a newer unratified "replacement" surfaces as a contradiction instead of
  retiring the ruling; every ratified ruling the gate reaches is rendered;
- the write path (capture and heal): amended captures bind (decision 7342); a
  newer human ruling replaces the old with no referral, while an overlap the
  human never ruled on is still referred; unattended heal never retires a
  standing rule for a newer agent note;
- the rejected-path policy: a rejected path stays rejected until a human changes
  it (both polarities, plus controls), and an unratified note cannot un-bind a
  declared row.

The governing rule (A1 resolver polarity, carried by M6a): only a ratified
ruling supersedes a ratified ruling; a newer unratified note surfaces as a
contradiction.

Cases latch fails today are strict expected failures (``raises=AssertionError``)
naming the plan item whose PR flips them; that PR removes the marker on purpose.
A harness problem raises ``FixtureError`` instead, so it can never pass itself
off as a predicted failure. ``pytest -m authority -rxX`` lists every case. M11a's
acceptance names "the two decision-7342 cases and the revival case": they are
test_amended_capture_binds_at_capture, test_newer_human_ruling_replaces_old_with_no_referral
and test_amended_capture_rejection_binds. T1's acceptance asks for the un-bind
and revival cases with capture-written rows: its PR switches ``_reject`` to
its capture path.

Everything is deterministic: throwaway vaults under ``tmp_path``, gate assembly
and render only (the classifier is never invoked), heal with ``use_llm=False``,
rejection policy through the zero-model predicate path. Seeding is pinned: only
the seed node carries the query token and the query's own embedding, and the
gate runs with ``seed_top_k=1``, so each case tests ranking and rendering, not
search.
"""
from __future__ import annotations

import hashlib
import inspect
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from latch.gate import gate  # noqa: E402
from latch.gate import predicate  # noqa: E402
from latch.gate import predicate_policy  # noqa: E402
from latch.mcp import mcp_server  # noqa: E402
from latch.pipeline import drift  # noqa: E402
from latch.pipeline import heal  # noqa: E402
from latch.retrieval import embeddings  # noqa: E402
from latch.store import db  # noqa: E402


pytestmark = pytest.mark.authority

M6A = "M6a"
M6B = "M6b"
M11A = "M11a"
T1 = "T1"

QUERY = "zephyrquill"
OLDER = "2026-01-12 09:00:00"
MIDDLE = "2026-02-02 09:00:00"
NEWER = "2026-03-02 09:00:00"
FOUNDER = "session:founder"
DOMAIN = "git_roots:" + hashlib.sha256(b"authority-fixture-repository").hexdigest()
OTHER_DOMAIN = "git_roots:" + hashlib.sha256(b"another-repository").hexdigest()
# Four-digit ids, as in a real vault, so an id named in a render label can
# never be mistaken for a hop count or a date fragment.
ID_FLOOR = 7000
NOTE_WORDS = (
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
    "hotel", "india", "juliett", "kilo", "lima", "mike", "november",
)


class FixtureError(RuntimeError):
    """The fixture did not build the shape a case needs (never a prediction)."""


def _require(condition: object, message: str) -> None:
    if not condition:
        raise FixtureError(message)


def _expected_to_fail(case: str, flips_with: str):
    return pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=f"authority: {case} - flips with {flips_with}",
    )


# ---------- vault fixtures ----------

def _set_id_floor(conn) -> None:
    row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'nodes'"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES ('nodes', ?)",
            (ID_FLOOR,),
        )
    else:
        conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'nodes'",
            (ID_FLOOR,),
        )
    conn.commit()


@pytest.fixture
def vault(tmp_path: Path):
    conn = db.connect(str(tmp_path / "vault"))
    _set_id_floor(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def capture_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A vault the real capture tool writes to; yields a reader connection."""
    project = tmp_path / "project"
    project.mkdir()
    setup = db.connect(str(project))
    _set_id_floor(setup)
    setup.close()

    monkeypatch.setattr(mcp_server, "_conn", lambda: db.connect(str(project)))
    monkeypatch.setattr(mcp_server, "_project_cwd", lambda: str(project))
    monkeypatch.setattr(mcp_server, "_project_session_id", lambda: FOUNDER)
    monkeypatch.setattr(mcp_server.paths, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(
        mcp_server.capture_streams,
        "emit_decision_event",
        lambda **_kwargs: None,
    )
    conn = db.connect(str(project))
    try:
        yield conn
    finally:
        conn.close()


# ---------- node builders ----------

def _stamp(conn, node_id: int, at: str) -> None:
    conn.execute(
        "UPDATE nodes SET created_at = ?, updated_at = ? WHERE id = ?",
        (at, at, node_id),
    )
    conn.commit()


def _insert(conn, kind: str, title: str, *, status: str, at: str,
            embedding: bytes | None = None) -> int:
    node_id = db.insert_node(
        conn, kind=kind, title=title, body=f"{title}.", status=status,
        embedding=embedding,
    )
    _stamp(conn, node_id, at)
    return node_id


def _seed_embedding() -> bytes:
    return embeddings.to_blob(embeddings.embed(QUERY))


def _seed(conn, title: str) -> int:
    """The one node the request hits: it carries the query token and the
    query's own embedding, so it is the single top hybrid hit."""
    return _insert(
        conn, "fact", f"{QUERY} {title}", status="canonical", at=MIDDLE,
        embedding=_seed_embedding(),
    )


def _ratify(conn, node_id: int) -> None:
    db.insert_ratification_nc(
        conn, node_id, ratifier=FOUNDER, action="ratify", source="latch_update",
    )
    db.update_node(conn, node_id, status="canonical")


def _ruling(conn, title: str, *, kind: str = "decision", at: str = OLDER,
            embedding: bytes | None = None) -> int:
    """A human ruling: inserted staging, then ratified through the authority
    table (latest row 'ratify'), as latch_update does."""
    node_id = _insert(conn, kind, title, status="staging", at=at,
                      embedding=embedding)
    _ratify(conn, node_id)
    _stamp(conn, node_id, at)
    _require(db.node_is_ratified(conn, node_id), f"ruling {node_id} not ratified")
    return node_id


def _legacy(conn, kind: str, title: str, *, at: str = MIDDLE,
            embedding: bytes | None = None) -> int:
    """Canonical with no ratification row: a legacy judgment, or an agent fact
    promoted to canonical without any human ratifying it."""
    return _insert(conn, kind, title, status="canonical", at=at,
                   embedding=embedding)


def _note(conn, kind: str, title: str, *, at: str = NEWER) -> int:
    """An agent-written note: staging, never ratified."""
    return _insert(conn, kind, title, status="staging", at=at)


def _link(conn, src: int, dst: int, relation: str) -> None:
    db.add_edge(conn, src, dst, relation)


def _capture(title: str, *, words: str, human_action: str = "approve",
             links: list[dict] | None = None) -> int:
    """Capture through the real MCP tool. Once M11a adds the verbatim-quote
    parameter (fail-closed without it), the human's words ride along."""
    kwargs = {
        "title": title,
        "body": f"{title}.",
        "gate_request": f"Should this govern: {title}?",
        "human_action": human_action,
        "session_id": FOUNDER,
        "links": links,
    }
    if "human_words" in inspect.signature(mcp_server.kb_capture_decision).parameters:
        kwargs["human_words"] = words
    result = mcp_server.kb_capture_decision(**kwargs)
    _require(
        isinstance(result, dict) and result.get("id") is not None,
        f"capture refused: {result!r}",
    )
    return int(result["id"])


# ---------- gate render and its label contract ----------
#
# These helpers are the observable contract M6a and M6b meet. Order in the
# rendered chain is rank. A label is a clause on a node's own lines (header,
# body, rejected options); clauses are split by brackets, ';', ',' and line
# ends:
#   - own authority: a clause with the word "ratified" (or "ratified_by") that
#     is not negated and names no other node, e.g. "[ratified]";
#   - retirement: a clause with "superseded" (or "superseded_by") and the
#     replacement's id, e.g. "[superseded_by=7004]";
#   - contradiction: a clause with "contradict..." and the other node's id,
#     e.g. "[contradicts 7003, unratified]" or "[contradicted by 7005]";
#   - a label about another node names that node's id in the same clause.
# Negations ("not ratified", "non-ratified", "never ratified", "no
# contradiction", "superseded_by=none") are not labels. The PR that flips a
# case may tighten these helpers to its final render format; it must not
# loosen what they mean.

_HEADER_RE = re.compile(r"^\s*(?:seed )?\[id=(\d+)[,\]]")
_ID_RE = re.compile(r"(?<![\d.])\d{4,}(?!\d)")
_NEGATED_PREFIX_RE = re.compile(r"(?i)(?:\bnot(?:\s+yet)?|\bnon|\bnever|\bno)[\s-]*$")
_NEGATED_VALUE_RE = re.compile(
    r"(?i)(?:_by)?\s*[:=]\s*(?:no|false|none|null|pending|0)\b"
)


def _gate(conn, seed_id: int) -> tuple[str, dict[int, int]]:
    """Assemble the gate for QUERY and return the chain exactly as the
    classifier prompt renders it, plus each evidence node's hop."""
    assembly = gate.assemble_gate(conn, QUERY, seed_top_k=1, focus_seed=False)
    seed_ids = [int(seed["id"]) for seed in assembly["seeds"]]
    _require(seed_ids == [seed_id],
             f"seeding drifted: expected [{seed_id}], got {seed_ids}")
    hops = {
        int(node["id"]): int(node["hop"])
        for chain in assembly["chains"]
        for node in chain["evidence"]
    }
    prompt = gate.build_classifier_prompt(assembly)
    _, marker, actual = prompt.partition("--- ACTUAL REQUEST ---")
    _require(marker, "classifier prompt lost its ACTUAL REQUEST section")
    _, marker, chain = actual.partition("CHAIN ASSEMBLY:\n")
    _require(marker, "classifier prompt lost its CHAIN ASSEMBLY section")
    chain, marker, _ = chain.rpartition("\n\nOUTPUT:")
    _require(marker, "classifier prompt lost its OUTPUT marker")
    return chain, hops


def _rendered_ids(chain: str) -> list[int]:
    return [
        int(match.group(1))
        for match in map(_HEADER_RE.match, chain.splitlines())
        if match
    ]


def _block(chain: str, node_id: int) -> str:
    """A node's header line plus its own continuation lines (body, labels,
    rejected options), stopping before the next node or section."""
    lines = chain.splitlines()
    for index, line in enumerate(lines):
        match = _HEADER_RE.match(line)
        if not match or int(match.group(1)) != node_id:
            continue
        block = [line]
        for follow in lines[index + 1:]:
            stripped = follow.strip()
            if (
                not stripped
                or _HEADER_RE.match(follow)
                or stripped == "evidence:"
                or follow.startswith("Lane ")
                or stripped.startswith("\u2026 +")
            ):
                break
            block.append(follow)
        return "\n".join(block)
    return ""


def _named_ids(text: str) -> set[int]:
    return {int(found) for found in _ID_RE.findall(text) if int(found) > ID_FLOOR}


_CLAUSE_BREAKS = "[];,\n"


def _clause_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """The clause a label word sits in: delimited by brackets, ';', ',' or
    the line end, so '[ratified; contradicted by 7003]' yields 'ratified'."""
    opened = max(text.rfind(mark, 0, start) for mark in _CLAUSE_BREAKS) + 1
    closers = [found for found in (text.find(mark, end) for mark in _CLAUSE_BREAKS)
               if found != -1]
    return opened, min(closers) if closers else len(text)


def _positive_labels(block: str, word: str) -> list[str]:
    """Clauses using `word` (a regex; `word_by` too) as a label, skipping
    negations such as 'unratified', 'not yet ratified' or 'superseded_by=none'."""
    clauses = []
    for match in re.finditer(rf"(?i)\b{word}(?=\b|_)", block):
        start, end = match.span()
        opened, closed = _clause_bounds(block, start, end)
        if _NEGATED_PREFIX_RE.search(block[opened:start]):
            continue
        if _NEGATED_VALUE_RE.match(block, end):
            continue
        clauses.append(block[opened:closed])
    return clauses


def _ranked_ahead(chain: str, first: int, second: int) -> bool:
    order = _rendered_ids(chain)
    return (
        first in order and second in order
        and order.index(first) < order.index(second)
    )


def _labelled_ratified(chain: str, node_id: int) -> bool:
    """The node itself is labelled ratified; a label about another node
    ('contradicts ratified 7003') does not count."""
    return any(
        not (_named_ids(segment) - {node_id})
        for segment in _positive_labels(_block(chain, node_id), "ratified")
    )


def _marked_superseded_by(chain: str, node_id: int, superseder: int) -> bool:
    return any(
        superseder in _named_ids(clause)
        for clause in _positive_labels(_block(chain, node_id), "superseded")
    )


def _surfaced_as_contradiction(chain: str, ruling: int, note: int) -> bool:
    return any(
        other in _named_ids(clause)
        for node_id, other in ((ruling, note), (note, ruling))
        for clause in _positive_labels(_block(chain, node_id), r"contradict\w*")
    )


# ---------- heal and policy helpers ----------

def _force_pair(monkeypatch: pytest.MonkeyPatch, left: int, right: int) -> None:
    """Make nightly heal see exactly this pair as near-duplicates."""
    def fake_find(conn, _vec, *, exclude_id=None, **_kwargs):
        other = {left: right, right: left}.get(exclude_id)
        if other is None:
            return []
        return [{**db.get_node(conn, other), "similarity": 0.99}]

    monkeypatch.setattr(heal, "find_near_duplicates", fake_find)


def _quiet_nightly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heal.paths, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(heal.paths, "is_disabled", lambda: False)
    monkeypatch.setattr(heal.log_utils, "maintain_log_retention", lambda _path: {})
    monkeypatch.setattr(heal.correlator, "correlate", lambda *_a, **_k: {})
    monkeypatch.setattr(drift, "sweep", lambda *_a, **_k: {})


def _referrals(conn, left: int, right: int) -> list[int]:
    """'Human review required' referrals parked on this pair."""
    rows = conn.execute(
        """
        SELECT DISTINCT q.id FROM nodes q
        JOIN edges a ON a.src = q.id AND a.dst = ?
                    AND a.relation = 'related_to' AND a.status = 'active'
        JOIN edges b ON b.src = q.id AND b.dst = ?
                    AND b.relation = 'related_to' AND b.status = 'active'
        WHERE q.kind = 'open_question'
        ORDER BY q.id
        """,
        (left, right),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _supersedes(conn, src: int, dst: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM edges WHERE src = ? AND dst = ? "
        "AND relation = 'supersedes' AND status = 'active'",
        (src, dst),
    ).fetchone() is not None


def _heal_embedding() -> bytes:
    return np.ones(db.VEC_DIM, dtype=np.float32).tobytes()


def _days_ago(days: int) -> str:
    """Heal's recency pass only fires on a node touched in the last 30 days,
    so heal cases are clock-relative; gate cases need only an order."""
    return (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%d %H:%M:%S")


def _reject(conn, owner: int, package: str, *,
            domain: str | None = DOMAIN) -> int:
    """A declared rejected option naming a package, as a human ruled it.
    Written directly today; T1's PR routes it through its capture path."""
    row_id = db.insert_rejected_path(
        conn, owner,
        option=f"the {package} package",
        reason="it dropped queued jobs whenever a worker restarted",
        ratifier=FOUNDER,
        decided_at=OLDER,
        scope_predicate=f"package:{package}",
        source="declared",
        policy_domain_id=domain,
    )
    _require(row_id is not None, "rejected_path row not written")
    return int(row_id)


def _revival(conn, *imports: str) -> str:
    """Deterministic revival check for an agent action importing `imports`:
    the binding rows of DOMAIN, compiled, evaluated with zero model calls (the
    same evaluation the snapshot consumer runs over its binding checks)."""
    projection = predicate_policy.project_policy_domain(conn, DOMAIN)
    checks = predicate.compile_predicates(projection.binding_rows)
    verdict = predicate.evaluate(
        checks,
        predicate.ToolCallContext(tool_name="Edit", import_names=tuple(imports)),
    )
    _require(verdict["llm_calls"] == 0, "revival check called a model")
    return str(verdict["decision"])


def _binding_ids(conn) -> set[int]:
    projection = predicate_policy.project_policy_domain(conn, DOMAIN)
    return {row.rejected_path_id for row in projection.binding_rows}


# ========== gate read path ==========

@_expected_to_fail("older ratified ruling vs newer contrary note at a closer hop", M6A)
def test_older_ratified_ruling_outranks_newer_contrary_note_at_closer_hop(vault):
    seed = _seed(vault, "job queue capacity plan")
    plan = _legacy(vault, "fact", "Queue workers scale with nightly batch volume")
    ruling = _ruling(vault, "Job queue stays on Postgres; no Redis, the ops cost is too high")
    note = _note(vault, "decision", "Agent note: move the job queue to Redis streams for throughput")
    _link(vault, seed, plan, "depends_on")
    _link(vault, ruling, plan, "constrains")
    _link(vault, note, seed, "related_to")

    chain, hops = _gate(vault, seed)
    _require(hops.get(ruling) == 2 and hops.get(note) == 1,
             f"expected ruling at hop 2 and note at hop 1, got {hops}")
    assert _ranked_ahead(chain, ruling, note), (
        "the newer, nearer agent note outranks the older ratified ruling"
    )
    assert _labelled_ratified(chain, ruling), "the ruling is not labelled ratified"
    assert not _labelled_ratified(chain, note), "an agent note is labelled ratified"


@_expected_to_fail("ratified ruling in a chain beyond the 12-item evidence cap", M6A)
def test_ratified_ruling_survives_the_twelve_item_chain_cap(vault):
    seed = _seed(vault, "release train cadence")
    plan = _legacy(vault, "fact", "Release trains leave every second Tuesday")
    ruling = _ruling(vault, "No release ships without the full suite green on both operating systems")
    _link(vault, seed, plan, "depends_on")
    _link(vault, ruling, plan, "constrains")
    notes = [
        _note(vault, "fact", f"Agent note {word}: the {word} release could skip the slow test leg")
        for word in NOTE_WORDS[:12]
    ]
    for note in notes:
        _link(vault, note, seed, "related_to")

    chain, hops = _gate(vault, seed)
    _require(len(hops) > gate.GATE_MAX_EVIDENCE_PER_CHAIN,
             f"chain does not overflow the cap: {len(hops)} nodes")
    _require(hops.get(ruling) == 2, f"expected ruling at hop 2, got {hops.get(ruling)}")
    rendered = _rendered_ids(chain)
    assert ruling in rendered, "the chain cap cut the ratified ruling"
    assert all(_ranked_ahead(chain, ruling, note) for note in notes if note in rendered), (
        "an agent note outranks the ratified ruling"
    )
    assert not any(_labelled_ratified(chain, note) for note in notes if note in rendered), (
        "an agent note is labelled ratified"
    )


@_expected_to_fail("a superseded ruling never renders as current", M6A)
def test_superseded_ruling_never_renders_as_current(vault):
    """Both render paths: the request hits a superseded ruling directly (the
    seed line) and the chain reaches another one (an evidence line). Only the
    supersede edges were written; the old rulings' status was not touched, as
    after a capture with a supersedes link or a hand-made link today."""
    old_seed = _ruling(vault, f"{QUERY} raw logs are kept for ninety days",
                       at=OLDER, embedding=_seed_embedding())
    new_seed = _ruling(vault, "Raw logs are kept for thirty days", at=NEWER)
    old_reached = _ruling(vault, "Log exports run nightly at two in the morning UTC", at=OLDER)
    new_reached = _ruling(vault, "Log exports move to four in the morning UTC after the backup window",
                          at=NEWER)
    _link(vault, new_seed, old_seed, "supersedes")
    _link(vault, old_reached, old_seed, "related_to")
    _link(vault, new_reached, old_reached, "supersedes")
    _require(db.get_node(vault, old_reached)["status"] == "canonical", "old ruling status moved")

    chain, hops = _gate(vault, old_seed)
    _require(hops.get(new_seed) == 1 and hops.get(old_reached) == 1 and hops.get(new_reached) == 2,
             f"unexpected hops {hops}")
    assert _marked_superseded_by(chain, old_seed, new_seed), (
        "the superseded ruling seeds the chain labelled as current"
    )
    assert _marked_superseded_by(chain, old_reached, new_reached), (
        "the superseded ruling renders as current evidence, with no superseded-by label"
    )
    assert _ranked_ahead(chain, new_reached, old_reached), (
        "the retired ruling outranks its replacement"
    )


def _seam(conn, *, repaired: bool) -> tuple[int, int, int]:
    """The live roadmap seam, shape only: a legacy-canonical roadmap
    reconciled by a hold ruling that a later ratified closure superseded; the
    hold ruling is itself reconciled by a partial amendment; the closure
    implements the roadmap. The repair (hold marked stale, roadmap reconciled
    by the closure) is the state the live vault is in now."""
    roadmap = _legacy(conn, "decision", f"{QUERY} roadmap: phases gated by binary checkpoints",
                      at=OLDER, embedding=_seed_embedding())
    hold = _legacy(conn, "decision", "Phase two work held until the validation checkpoint closes",
                   at=OLDER)
    amendment = _legacy(conn, "decision", "Phase two early start cleared on a branch only",
                        at=MIDDLE)
    closure = _ruling(conn, "Validation checkpoint closed; phase two unlocked", at=NEWER)
    for title in ("Checkpoint one measurement window closed",
                  "Checkpoint two counting harness merged"):
        _link(conn, roadmap, _legacy(conn, "progress", title), "related_to")
    _link(conn, roadmap, hold, "reconciled_by")
    _link(conn, hold, amendment, "reconciled_by")
    _link(conn, closure, hold, "supersedes")
    _link(conn, closure, roadmap, "implements")
    if repaired:
        db.update_node(conn, hold, status="stale")
        _stamp(conn, hold, OLDER)
        _link(conn, roadmap, closure, "reconciled_by")
    return roadmap, hold, closure


@_expected_to_fail("live seam shape: a retired hold ruling still reconciles the roadmap", M6A)
def test_roadmap_seam_never_renders_retired_hold_ruling_as_current(vault):
    roadmap, hold, closure = _seam(vault, repaired=False)

    chain, hops = _gate(vault, roadmap)
    _require(hops.get(hold) == 1 and hops.get(closure) == 2,
             f"expected hold at hop 1 and closure at hop 2, got {hops}")
    assert _marked_superseded_by(chain, hold, closure), (
        "the retired hold ruling renders as the roadmap's current reconciliation"
    )
    assert _ranked_ahead(chain, closure, hold), "the hold ruling outranks the closure"


def test_roadmap_seam_after_repair_renders_hold_ruling_as_retired(vault):
    """Control on the repaired live shape. It pins that the retired hold
    ruling still reaches the classifier marked retired and that the closure is
    rendered; it does not pin rank, which the pre-repair case owns."""
    roadmap, hold, closure = _seam(vault, repaired=True)

    chain, hops = _gate(vault, roadmap)
    _require(hops.get(hold) == 1 and hops.get(closure) == 1,
             f"expected hold and closure at hop 1, got {hops}")
    hold_block = _block(chain, hold)
    assert hold_block, "the retired hold ruling vanished from the chain"
    assert "status=stale" in hold_block or _marked_superseded_by(chain, hold, closure)
    assert closure in _rendered_ids(chain), "the closure ruling is not rendered"


@_expected_to_fail("a decision captured in the moment beats a newer agent note (decision 7342)", M6A)
def test_decision_captured_in_the_moment_beats_newer_agent_note(capture_vault):
    conn = capture_vault
    seed = _seed(conn, "background export pipeline")
    plan = _legacy(conn, "fact", "Export workers share the primary database connection pool")
    _link(conn, seed, plan, "depends_on")
    ruling = _capture(
        "Exports stay on the primary database; no read replica until load doubles",
        words="keep exports on the primary, no replica yet",
        links=[{"dst": plan, "relation": "constrains"}],
    )
    _require(db.node_is_ratified(conn, ruling), "an approve capture no longer binds")
    _stamp(conn, ruling, OLDER)
    note = _note(conn, "decision", "Agent note: route exports to a read replica now")
    _link(conn, note, seed, "related_to")

    chain, hops = _gate(conn, seed)
    _require(hops.get(ruling) == 2 and hops.get(note) == 1,
             f"expected ruling at hop 2 and note at hop 1, got {hops}")
    assert _ranked_ahead(chain, ruling, note), (
        "a newer agent note outranks the decision the human made in the moment"
    )
    assert _labelled_ratified(chain, ruling), "the captured decision is not labelled ratified"
    assert not _labelled_ratified(chain, note), "an agent note is labelled ratified"


@_expected_to_fail("a user's standing rule against a newer agent note", M6A)
def test_standing_rule_outranks_newer_agent_note(vault):
    seed = _seed(vault, "service dependency review")
    plan = _legacy(vault, "fact", "New services are reviewed before they join the deploy pipeline")
    rule = _ruling(vault, "Always pin third-party dependencies to exact versions",
                   kind="preference")
    note = _note(vault, "decision", "Agent note: float minor dependency versions to pick up fixes")
    _link(vault, seed, plan, "depends_on")
    _link(vault, rule, plan, "constrains")
    _link(vault, note, seed, "related_to")

    chain, hops = _gate(vault, seed)
    _require(hops.get(rule) == 2 and hops.get(note) == 1,
             f"expected rule at hop 2 and note at hop 1, got {hops}")
    assert _ranked_ahead(chain, rule, note), "a newer agent note outranks the standing rule"
    assert _labelled_ratified(chain, rule), "the standing rule is not labelled ratified"
    assert not _labelled_ratified(chain, note), "an agent note is labelled ratified"


@_expected_to_fail("a newer unratified replacement surfaces as a contradiction", M6A)
def test_newer_unratified_replacement_surfaces_as_contradiction(vault):
    """Today the ruling already outranks the note and is not marked retired;
    the case fails on the missing ratified and contradiction labels."""
    seed = _seed(vault, "session token lifetime")
    ruling = _ruling(vault, "Session tokens expire after one hour")
    note = _note(vault, "decision", "Agent note: session tokens now last a full day")
    _link(vault, ruling, seed, "constrains")
    _link(vault, note, seed, "related_to")
    _link(vault, note, ruling, "supersedes")

    chain, hops = _gate(vault, seed)
    _require(hops.get(ruling) == 1 and hops.get(note) == 1,
             f"expected ruling and note at hop 1, got {hops}")
    assert not _marked_superseded_by(chain, ruling, note), (
        "an unratified note retired a ratified ruling"
    )
    assert _ranked_ahead(chain, ruling, note), "the unratified replacement outranks the ruling"
    assert _labelled_ratified(chain, ruling), "the ruling is not labelled ratified"
    assert not _labelled_ratified(chain, note), "an agent note is labelled ratified"
    assert _surfaced_as_contradiction(chain, ruling, note), (
        "the unratified replacement is not surfaced as a contradiction"
    )


@_expected_to_fail("every ratified ruling the gate reaches is rendered", M6B)
def test_every_reached_ratified_ruling_is_rendered(vault):
    seed = _seed(vault, "platform guardrails")
    rulings = [
        _ruling(vault, f"Guardrail {word}: the {word} service keeps its own schema")
        for word in NOTE_WORDS[:13]
    ]
    notes = [
        _note(vault, "fact", f"Agent note {word}: the {word} service could share a schema")
        for word in NOTE_WORDS[13:14] + NOTE_WORDS[:1]
    ]
    for node in rulings + notes:
        _link(vault, node, seed, "related_to")

    chain, hops = _gate(vault, seed)
    _require(len(rulings) > gate.GATE_MAX_EVIDENCE_PER_CHAIN,
             "fixture does not exceed the chain cap with rulings alone")
    _require(all(hops.get(ruling) == 1 for ruling in rulings), f"unexpected hops {hops}")
    missing = [ruling for ruling in rulings if ruling not in _rendered_ids(chain)]
    assert not missing, f"ratified rulings reached but not rendered: {missing}"


# ========== write path: capture and heal ==========

@_expected_to_fail("modify and override captures bind at capture (decision 7342)", M11A)
def test_amended_capture_binds_at_capture(capture_vault):
    conn = capture_vault
    for action, title, words in (
        ("modify", "Cache entries expire after ten minutes, not five",
         "make it ten minutes, five is too short"),
        ("override", "Ship the migration this week despite the open gate question",
         "ship it this week, I accept the risk"),
    ):
        node_id = _capture(title, words=words, human_action=action)
        assert db.node_is_ratified(conn, node_id), (
            f"a {action} capture wrote no ratification"
        )
        assert db.get_node(conn, node_id)["status"] == "canonical", (
            f"a {action} capture stayed staging"
        )


@_expected_to_fail("a newer human ruling replaces the old with no referral (decision 7342)", M11A)
def test_newer_human_ruling_replaces_old_with_no_referral(capture_vault):
    """The human says the new ruling replaces the old one, and the capture
    carries that as an explicit supersedes link. The replacement takes effect
    at capture (the old ruling is retired) and nobody is asked to review it.
    Today the link is written but the old ruling stays current. The no-referral
    half guards the fix: routing this pair to 'Human review required', as heal
    and the compactor do for inferred contradictions, would re-ask the human."""
    conn = capture_vault
    old = _capture("Nightly exports start at two in the morning UTC",
                   words="run exports at two")
    new = _capture(
        "Nightly exports start at four in the morning UTC",
        words="move exports to four, that replaces the two o'clock rule",
        links=[{"dst": old, "relation": "supersedes"}],
    )
    _require(db.node_is_ratified(conn, old) and db.node_is_ratified(conn, new),
             "an approve capture no longer binds")

    assert db.get_node(conn, new)["status"] == "canonical"
    assert db.get_node(conn, old)["status"] == "stale", (
        "the old ruling is still current after the human replaced it"
    )
    assert _supersedes(conn, new, old), "the explicit replacement was not recorded"
    assert _referrals(conn, old, new) == [], "the human was asked to review their own ruling"


def test_only_the_humans_explicit_word_replaces_a_ruling(
    capture_vault, vault, tmp_path, monkeypatch,
):
    """Boundary of the replacement case, on both write paths. At capture: a
    near-identical new ruling linked to the old one only as related, and a
    ruling that constrains a plan fact, retire nothing. In nightly heal: an
    overlap between two human rulings is referred to the human, never
    resolved (the PR #114 guard); both stay current."""
    conn = capture_vault
    plan = _legacy(conn, "fact", "Nightly exports feed the morning reporting job")
    first = _capture("Nightly exports start at two in the morning UTC",
                     words="run exports at two")
    second = _capture(
        "Nightly exports start at two in the morning UTC, before reporting",
        words="exports at two, before the reporting job",
        links=[{"dst": first, "relation": "related_to"},
               {"dst": plan, "relation": "constrains"}],
    )
    _require(db.node_is_ratified(conn, first) and db.node_is_ratified(conn, second),
             "an approve capture no longer binds")
    assert db.get_node(conn, first)["status"] == "canonical", (
        "a capture retired a ruling the human never said it replaced"
    )
    assert db.get_node(conn, plan)["status"] == "canonical", (
        "a capture retired a node it only constrains"
    )
    assert not _supersedes(conn, second, first) and not _supersedes(conn, first, second)

    old = _ruling(vault, "Nightly exports start at two in the morning UTC",
                  at=_days_ago(90), embedding=_heal_embedding())
    new = _ruling(vault, "Nightly exports start at four in the morning UTC",
                  at=_days_ago(1), embedding=_heal_embedding())
    _force_pair(monkeypatch, old, new)
    _quiet_nightly(monkeypatch)

    heal.nightly_heal(vault, project_path=str(tmp_path), use_llm=False, integrity=False)

    assert db.get_node(vault, old)["status"] == "canonical"
    assert db.get_node(vault, new)["status"] == "canonical"
    assert not _supersedes(vault, new, old) and not _supersedes(vault, old, new)
    assert len(_referrals(vault, old, new)) == 1, "the overlap was not referred to the human"


def test_unattended_heal_never_retires_standing_rule_for_newer_agent_note(
    vault, tmp_path, monkeypatch,
):
    """Write-side half of the standing-rule case: the PR #114 guard holds."""
    rule = _ruling(vault, "Always pin third-party dependencies to exact versions",
                   kind="preference", at=_days_ago(90), embedding=_heal_embedding())
    note = _insert(vault, "preference", "Dependencies may float within minor versions",
                   status="staging", at=_days_ago(1), embedding=_heal_embedding())
    _force_pair(monkeypatch, rule, note)
    _quiet_nightly(monkeypatch)

    heal.nightly_heal(vault, project_path=str(tmp_path), use_llm=False, integrity=False)

    assert db.get_node(vault, rule)["status"] == "canonical"
    assert db.node_is_ratified(vault, rule)
    assert db.get_node(vault, note)["status"] == "staging"
    assert not _supersedes(vault, note, rule)
    assert len(_referrals(vault, rule, note)) == 1, (
        "the contradiction was not surfaced to the human"
    )


# ========== rejected path stays rejected ==========

def test_rejected_path_revival_is_blocked(vault):
    """Polarity one: an agent cannot revive a path a human rejected."""
    ruling = _ruling(vault, "Background jobs use the Postgres-backed queue")
    row = _reject(vault, ruling, "oldqueue")
    revival = _note(vault, "decision", "Agent note: bring back oldqueue for throughput")
    _link(vault, revival, ruling, "related_to")

    assert row in _binding_ids(vault)
    assert _revival(vault, "oldqueue.client") == "block"


def test_human_readoption_lifts_the_rejection(vault):
    """Polarity two: a later human ruling that re-adopts the path un-binds it."""
    ruling = _ruling(vault, "Background jobs use the Postgres-backed queue", at=OLDER)
    row = _reject(vault, ruling, "oldqueue")
    readoption = _ruling(vault, "Background jobs move back to oldqueue after its restart fix",
                         at=NEWER)
    _link(vault, readoption, ruling, "supersedes")

    assert row not in _binding_ids(vault)
    assert _revival(vault, "oldqueue.client") == "pass"


def test_rejected_path_controls(vault):
    """Controls: an unrelated package, a row bound to another repository, and
    an unbound legacy row never block."""
    ruling = _ruling(vault, "Background jobs use the Postgres-backed queue")
    _reject(vault, ruling, "oldqueue")
    other = _ruling(vault, "Reports render server-side only")
    _reject(vault, other, "chartkit", domain=OTHER_DOMAIN)
    legacy = _ruling(vault, "Search stays on the built-in index")
    _reject(vault, legacy, "searchbox", domain=None)

    assert _revival(vault, "pgqueue.client") == "pass"
    assert _revival(vault, "chartkit") == "pass"
    assert _revival(vault, "searchbox") == "pass"
    assert _revival(vault, "oldqueue") == "block"


def test_agent_originated_rejection_never_binds(vault):
    """Decision 7342: an agent-inferred ruling stays evidence."""
    inferred = _note(vault, "decision", "Agent inference: avoid oldqueue for background jobs")
    row = _reject(vault, inferred, "oldqueue")

    assert row not in _binding_ids(vault)
    assert _revival(vault, "oldqueue.client") == "pass"


@_expected_to_fail("an amended capture's rejection binds (revival, write side)", M11A)
def test_amended_capture_rejection_binds(capture_vault):
    conn = capture_vault
    ruling = _capture(
        "Background jobs use the Postgres-backed queue; drop oldqueue entirely",
        words="no, drop oldqueue entirely and use the postgres queue",
        human_action="modify",
    )
    _reject(conn, ruling, "oldqueue")

    assert db.node_is_ratified(conn, ruling), "the amended capture did not bind"
    assert _revival(conn, "oldqueue.client") == "block", (
        "a path the human rejected in an amended capture can be revived"
    )


@pytest.mark.parametrize(
    "relation",
    [
        pytest.param("supersedes", marks=_expected_to_fail(
            "an unratified note cannot un-bind a declared row (supersedes)", T1)),
        pytest.param("reconciled_by", marks=_expected_to_fail(
            "an unratified note cannot un-bind a declared row (reconciled_by)", T1)),
    ],
)
def test_unratified_note_cannot_unbind_declared_row(vault, relation):
    """Neither a staging agent note nor an agent fact promoted to canonical
    without a human can un-bind a declared row by claiming to supersede or
    reconcile its ruling. The ratified counterpart is
    test_human_readoption_lifts_the_rejection. The fix keys demotion on
    authority (a source counts only when it is at least as authoritative as
    the row's owner), not on staging status, and covers the reconciler query
    (owner_unresolved_reconciliation) as well as the superseder query."""
    ruling = _ruling(vault, "Background jobs use the Postgres-backed queue")
    row = _reject(vault, ruling, "oldqueue")
    _require(row in _binding_ids(vault), "declared row is not binding to begin with")

    for label, note in (
        ("a staging note", _note(vault, "decision", "Agent note: oldqueue is fine again")),
        ("an unratified canonical fact",
         _legacy(vault, "fact", "Agent finding: oldqueue fixed its restart bug")),
    ):
        if relation == "supersedes":
            _link(vault, note, ruling, "supersedes")
        else:
            _link(vault, ruling, note, "reconciled_by")
        assert row in _binding_ids(vault), f"{label} un-bound the row via {relation}"
        assert _revival(vault, "oldqueue.client") == "block"
