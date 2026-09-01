"""Item-2 regression for the V4 build (KB id=4626): gate-time consumption of
typed rejected_path rows (the V2 table, id=4369).

Acceptance being pinned here:
- a gate call over a fixture vault whose decision chain carries a
  rejected_path row receives that typed rejection in the classifier context;
- with the row absent, the rendered context is byte-identical apart from the
  rejection lines themselves (additive-only: stripping the rejection lines
  from the with-row prompt reproduces the without-row prompt exactly);
- rendering is bounded per node (id=1415 prompt-budget discipline) with an
  explicit omitted-count note, never silent truncation (priority 4114 spirit);
- the renderer reports which rejected_path row ids were actually rendered
  (post-cap) so item 3 can log the surfaced set truthfully.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

from latch.store import db          # noqa: E402
from latch.retrieval import embeddings  # noqa: E402
from latch.gate import gate        # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_gate_rejected_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    try:
        conn.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def _ins(conn, kind, title, body, *, status="staging", workstream_id=None):
    vec = embeddings.embed(f"{title}\n\n{body}")
    return db.insert_node(
        conn, kind=kind, title=title, body=body, status=status,
        embedding=embeddings.to_blob(vec), workstream_id=workstream_id,
    )


def _force_seed(conn, node_id):
    """Stub hybrid_search to return exactly this node, deterministic."""
    row = conn.execute(
        "SELECT id, kind, title, body, status, workstream_id FROM nodes "
        "WHERE id = ?", (node_id,),
    ).fetchone()
    hit = dict(row)
    hit["score"] = 1.0
    return lambda *a, **kw: [hit]


def _prompt_for(conn, query):
    assembly = gate.assemble_gate(conn, query)
    return gate.build_classifier_prompt(assembly)


def _call_context(prompt: str) -> str:
    """The per-call slice of the prompt: everything after the static
    instruction + few-shot block. Item 2's byte-identity acceptance is about
    THIS section — the static block also mentions rejected[rp=...] (it
    teaches the citation field, item 3) but is version-constant and identical
    across calls."""
    return prompt.split("--- ACTUAL REQUEST ---", 1)[1]


def _strip_rejection_lines(prompt: str) -> str:
    kept = [
        line
        for line in prompt.split("\n")
        if not line.lstrip().startswith("rejected[rp=")
        and "rejected option(s) omitted" not in line
    ]
    return "\n".join(kept)


def test_rejected_row_on_seed_surfaces_in_prompt():
    tmp, conn = _fresh_db()
    orig = gate.search.hybrid_search
    try:
        nid = _ins(
            conn, "decision", "Queue engine chosen",
            "Redis Streams adopted for the job queue.", status="canonical",
        )
        rid = db.insert_rejected_path(
            conn, nid,
            option="in-process queue",
            reason="loses state across worker restarts",
            ratifier="founder",
            decided_at="2026-04-23",
            scope_predicate="repo:open-latch",
        )
        gate.search.hybrid_search = _force_seed(conn, nid)
        prompt = _prompt_for(conn, "re-try the in-process queue")
        marker = f"rejected[rp={rid}]:"
        _assert(marker in prompt, f"prompt missing {marker!r}:\n{prompt}")
        _assert("in-process queue" in prompt, "option text missing from prompt")
        _assert(
            "loses state across worker restarts" in prompt,
            "reason text missing from prompt",
        )
        _assert("ratifier=founder" in prompt, "ratifier missing from prompt")
        _assert("decided=2026-04-23" in prompt, "decided_at missing from prompt")
        _assert("scope=repo:open-latch" in prompt, "scope predicate missing")
        print("PASS rejected_row_on_seed_surfaces_in_prompt")
    finally:
        gate.search.hybrid_search = orig
        _cleanup(tmp, conn)


def test_rejected_row_on_evidence_node_surfaces_in_prompt():
    tmp, conn = _fresh_db()
    orig = gate.search.hybrid_search
    try:
        seed_id = _ins(
            conn, "decision", "Storage layer settled",
            "Postgres stays primary.", status="canonical",
        )
        ev_id = _ins(
            conn, "idea", "NoSQL migration parked",
            "Considered and parked: wrong tradeoff.", status="stale",
        )
        db.add_edge(conn, src=ev_id, dst=seed_id, relation="related_to")
        rid = db.insert_rejected_path(
            conn, ev_id,
            option="NoSQL document store",
            reason="audit-log queries require relational joins",
        )
        gate.search.hybrid_search = _force_seed(conn, seed_id)
        prompt = _prompt_for(conn, "switch storage to a document store")
        marker = f"rejected[rp={rid}]:"
        _assert(marker in prompt, f"prompt missing {marker!r}:\n{prompt}")
        # Evidence-level rejection lines sit under the evidence body indent.
        line = next(l for l in prompt.split("\n") if marker in l)
        _assert(
            line.startswith("      rejected[rp="),
            f"evidence rejection not at evidence indent: {line!r}",
        )
        print("PASS rejected_row_on_evidence_node_surfaces_in_prompt")
    finally:
        gate.search.hybrid_search = orig
        _cleanup(tmp, conn)


def test_context_is_additive_only_without_row():
    tmp, conn = _fresh_db()
    orig = gate.search.hybrid_search
    try:
        nid = _ins(
            conn, "decision", "Queue engine chosen",
            "Redis Streams adopted for the job queue.", status="canonical",
        )
        gate.search.hybrid_search = _force_seed(conn, nid)
        before = _call_context(_prompt_for(conn, "re-try the in-process queue"))
        _assert(
            "rejected[rp=" not in before,
            "no-row call context must carry no rejection marker",
        )

        db.insert_rejected_path(
            conn, nid,
            option="in-process queue",
            reason="loses state across worker restarts",
        )
        with_row = _call_context(
            _prompt_for(conn, "re-try the in-process queue")
        )
        _assert("rejected[rp=" in with_row, "row present but not rendered")
        _assert(
            _strip_rejection_lines(with_row) == before,
            "with-row call context is not byte-identical to no-row call "
            "context after stripping the rejection lines — the change is "
            "not additive-only",
        )
        print("PASS context_is_additive_only_without_row")
    finally:
        gate.search.hybrid_search = orig
        _cleanup(tmp, conn)


def test_per_node_cap_with_omitted_note():
    tmp, conn = _fresh_db()
    orig = gate.search.hybrid_search
    try:
        nid = _ins(
            conn, "decision", "Runtime surface settled",
            "Shared MCP runtime adopted.", status="canonical",
        )
        for i in range(5):
            db.insert_rejected_path(
                conn, nid,
                option=f"alternative {i}",
                reason=f"ruled out for reason {i}",
            )
        gate.search.hybrid_search = _force_seed(conn, nid)
        prompt = _call_context(_prompt_for(conn, "revisit the runtime surface"))
        rendered = [
            l for l in prompt.split("\n") if l.lstrip().startswith("rejected[rp=")
        ]
        cap = gate.GATE_MAX_REJECTED_PER_NODE
        _assert(cap == 3, f"default per-node rejection cap should be 3, got {cap}")
        _assert(
            len(rendered) == cap,
            f"expected {cap} rendered rejection lines, got {len(rendered)}",
        )
        _assert(
            "+2 rejected option(s) omitted" in prompt,
            "omitted-count note missing — silent truncation is forbidden",
        )
        print("PASS per_node_cap_with_omitted_note")
    finally:
        gate.search.hybrid_search = orig
        _cleanup(tmp, conn)


def test_renderer_reports_surfaced_row_ids():
    # DB-free synthetic assembly: renderer must report exactly the rendered
    # (post-cap) row ids into the caller's accumulator, in render order.
    seed = {
        "id": 100, "kind": "decision", "title": "seed", "body_excerpt": "b",
        "status": "canonical", "workstream_id": None, "source": "hybrid",
        "score": 1.0,
        "rejected_paths": [
            {"id": 11, "node_id": 100, "option": "opt-a", "reason": "r-a",
             "ratifier": None, "decided_at": None, "scope_predicate": None,
             "source": "declared", "created_at": "2026-08-01 00:00:00"},
        ],
    }
    ev = {
        "id": 200, "kind": "fact", "title": "ev", "body_excerpt": "eb",
        "status": "canonical", "workstream_id": None, "via_relation":
        "related_to", "direction": "out", "hop": 1, "path": [100, 200],
        "rejected_paths": [
            {"id": 21, "node_id": 200, "option": "opt-b", "reason": "r-b",
             "ratifier": None, "decided_at": None, "scope_predicate": None,
             "source": "backfill", "created_at": "2026-08-01 00:00:00"},
            {"id": 22, "node_id": 200, "option": "opt-c", "reason": "r-c",
             "ratifier": None, "decided_at": None, "scope_predicate": None,
             "source": "declared", "created_at": "2026-08-01 00:00:00"},
        ],
    }
    assembly = {
        "query": "q",
        "seeds": [seed],
        "chains": [{"seed_id": 100, "lane_group_id": None, "evidence": [ev]}],
        "evidence_node_ids": [200],
        "priorities": [],
        "lane_groups": [],
    }
    surfaced: list[int] = []
    rendered = gate._render_chain_for_prompt(
        assembly, surfaced_rejected_paths=surfaced,
    )
    _assert("rejected[rp=11]:" in rendered, rendered)
    _assert("rejected[rp=21]:" in rendered, rendered)
    _assert("rejected[rp=22]:" in rendered, rendered)
    _assert(surfaced == [11, 21, 22], f"surfaced ids wrong: {surfaced}")
    print("PASS renderer_reports_surfaced_row_ids")


if __name__ == "__main__":
    test_rejected_row_on_seed_surfaces_in_prompt()
    test_rejected_row_on_evidence_node_surfaces_in_prompt()
    test_context_is_additive_only_without_row()
    test_per_node_cap_with_omitted_note()
    test_renderer_reports_surfaced_row_ids()
