"""kb_gate chain assembly (step 9 / step 4a).

Pure-Python traversal engine. No LLM call here — that's step 4b. This module
collects the inputs the classifier prompt will need: hybrid-search seeds,
focus-seeded workstream subgraphs, and 1–2 hop traversal results over the
canonical relation set, with stale nodes deliberately included so abandoned
paths surface in the chain.

Output schema (locked 2026-04-30, deliberately minimal + forward-compatible —
4b can render whatever subset of fields it needs):

    {
        "query":             <str, the input>,
        "seeds":             [<seed_node>, ...],   # ordered: hybrid hits, then focus
        "chains":            [<chain>, ...],
        "evidence_node_ids": [<int>, ...],         # deduped, sorted, all unique
                                                   # ids reached via traversal
    }

    seed_node = {
        "id", "kind", "title", "body_excerpt", "status", "workstream_id",
        "source": "hybrid" | "focus",
        "score":  <float>,                         # hybrid RRF score or focus
                                                   # effective_score
    }

    chain = {
        "seed_id":  <int>,
        "evidence": [<evidence_node>, ...],
    }

    evidence_node = {
        "id", "kind", "title", "body_excerpt", "status", "workstream_id",
        "via_relation":  <str>,            # canonical or free-form
        "direction":     "out" | "in",     # "out" = seed/predecessor was src
        "hop":           1 | 2,
        "path":          [<int>, ...],     # node ids from seed (exclusive)
                                           # to here (inclusive)
    }

The full set of traversed relations is `CANONICAL_TRAVERSAL_RELATIONS` (the 6
direction-aware ones from step 9) plus `related_to` — the latter accounts for
~47% of all edges and skipping it would miss too much project context.
Direction is recorded but NOT filtered: 4a walks both in + out edges; the
classifier prompt (4b) decides how to interpret direction per relation.
Including both directions costs little at 1–2 hops and avoids encoding
direction conventions twice (once here, once in the prompt).

Stale nodes are surfaced both as hybrid-search hits (`include_stale=True`,
opposite of `kb_search`'s default) and as traversal targets (no stale filter
on edge walks). The whole point of the tool is to find what was abandoned.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Iterable

import budget
import capture_streams
import db
import log_utils
import paths
import priorities
import profiles
import search


# Canonical 6 + related_to + reconciled_by. Keep this union explicit rather than
# importing CANONICAL_TRAVERSAL_RELATIONS and adding — the set is the public
# contract of what 4a actually walks. `reconciled_by` (id=1415 #4) is walked so
# the gate sees latch's own staleness mechanism: when a seed's framing was
# partially updated by a newer decision, the reconciling node enters the chain
# instead of the gate reasoning from stale framing. It is sparse (~4% of edges)
# so it adds negligible fan-out; `related_to` (the dense ~72%) is the relation
# pruned past hop-1 in _traverse_from.
TRAVERSAL_RELATIONS: frozenset[str] = frozenset(
    db.CANONICAL_TRAVERSAL_RELATIONS | {"related_to", "reconciled_by"}
)

# Relations carrying decision/staleness signal — ranked ABOVE the dense,
# low-signal `related_to` when capping a chain's evidence for the prompt
# (id=1415). reconciled_by joins the canonical 6 here so a reconciling node is
# never crowded out of the prompt by topical related_to neighbours.
_HIGH_SIGNAL_RELATIONS: frozenset[str] = frozenset(
    db.CANONICAL_TRAVERSAL_RELATIONS | {"reconciled_by"}
)

DEFAULT_SEED_TOP_K = 5
DEFAULT_MAX_HOPS = 2
DEFAULT_BODY_EXCERPT = 400


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive-int tuning knob from the environment, falling back to
    `default` on absent/blank/malformed/out-of-range values."""
    try:
        v = int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default
    return v if v >= minimum else default


def _env_int_any(names: Iterable[str], default: int, *, minimum: int = 1) -> int:
    """Read the first set positive-int tuning knob from a list of env names."""
    for name in names:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            v = int(raw)
        except (TypeError, ValueError):
            continue
        if v >= minimum:
            return v
    return default


# Prompt-bound caps (KB id=1415: gate prompt explosion → classifier 90s timeout).
# The classifier/adversary prompt is bounded at RENDER time, NOT in traversal:
# assemble_gate still returns the full reachable set so `reachable_count`
# telemetry and `blast_radius` (kb_correct) stay truthful — but
# _render_chain_for_prompt emits only the top-ranked evidence per chain plus a
# global ceiling. Measured on the live SAv2 KB 2026-06-07 (985 active nodes,
# related_to = 72% of edges): pre-cap prompts hit 34–54k tokens and a worst-case
# chain carried 358 evidence nodes; these caps bring that to ≤ MAX_TOTAL. Defaults
# are conservative; override via env. Shrinking the prompt strictly LOWERS latency
# and tokens (priority id=1329) — a tightening, not a regression.
GATE_MAX_EVIDENCE_PER_CHAIN = _env_int("CLAUDE_KB_GATE_MAX_EVIDENCE_PER_CHAIN", 12)
GATE_MAX_TOTAL_EVIDENCE = _env_int("CLAUDE_KB_GATE_MAX_TOTAL_EVIDENCE", 60)
# Reserved slots so the abandoned-path signal (stale nodes — the whole point of
# the gate) survives the cap even when active nodes would otherwise crowd it out.
GATE_STALE_BUDGET = _env_int("CLAUDE_KB_GATE_STALE_BUDGET", 3, minimum=0)

# Max hop depth at which the dense `related_to` relation is traversed (id=1415
# #3). related_to is ~72% of edges and its 2-hop neighbourhood explodes
# combinatorially with low-signal nodes; canonical + reconciled_by relations
# (sparse, decision-bearing) still traverse to full `max_hops`. This caps the
# work assemble_gate does, not just the rendered prompt. Gate-only — blast_radius
# (kb_correct) passes related_to_max_hop=None to keep the full neighbourhood.
GATE_RELATED_TO_MAX_HOP = _env_int("CLAUDE_KB_GATE_RELATED_TO_MAX_HOP", 1)

# Personal-layer kinds that must never seed the gate similarity chain. They are
# surfaced through dedicated channels instead — priorities via the ACTIVE
# PROJECT PRIORITIES block (priorities.render_for_gate) + the SessionStart
# brief. Priorities carry no embedding, but the FTS trigger still indexes them,
# so without this filter an unembedded priority could surface as a keyword seed
# and pollute the decision chain. Verification profiles (profiles.PROFILE_KIND)
# are excluded for the same reason — surface-only per-user config, not evidence.
EXCLUDED_SEED_KINDS: frozenset[str] = frozenset(
    {priorities.PRIORITY_KIND, profiles.PROFILE_KIND}
)

# Per-invocation JSONL telemetry. One line per run_gate() call. Mirrors
# the retrieve.log pattern used by the UserPromptSubmit hook so empirical
# tuning (verdict distribution, MODIFY hedge rate, latency, budget burn) has
# the data it needs after ~100 prompts.
LOG_STREAM = "gate"
LOG_QUERY_EXCERPT_CHARS = 200

# Structural-only invariant (id=1108 §3): gate.log must not carry raw prompt
# text by default. query_hash is the correlation key the Gap A+D correlator
# joins on, and the raw query is never a learning feature — so the human-
# readable query_excerpt is opt-in for local debugging only, default off,
# mirroring the CLAUDE_KB_GIT_SNAPSHOT opt-in. Set CLAUDE_KB_LOG_RAW_QUERY=1
# to restore it. (Resolves id=1225; reconciles the id=613 hash+excerpt
# default, which predates the id=1108 structural-only lock.)
LOG_RAW_QUERY = os.environ.get("CLAUDE_KB_LOG_RAW_QUERY") == "1"


def assemble_gate(
    conn: sqlite3.Connection,
    query: str,
    *,
    seed_top_k: int = DEFAULT_SEED_TOP_K,
    include_stale: bool = True,
    focus_seed: bool = True,
    max_hops: int = DEFAULT_MAX_HOPS,
    body_excerpt_chars: int = DEFAULT_BODY_EXCERPT,
) -> dict:
    """Build the chain assembly for one kb_gate call.

    `query` is the user's coding/build prompt verbatim. Hybrid seeds use this
    directly; focus seeding ignores it (focus is set by activity, not query).

    Bumping `seed_top_k`, `max_hops`, or `body_excerpt_chars` widens the
    classifier's context at the cost of prompt-token budget. Defaults are
    tuned for "enough chain to judge, not so much that it floods the prompt."
    """
    seeds, seed_ids = _collect_seeds(
        conn, query,
        seed_top_k=seed_top_k,
        include_stale=include_stale,
        focus_seed=focus_seed,
        body_excerpt_chars=body_excerpt_chars,
    )

    chains: list[dict] = []
    all_evidence_ids: set[int] = set()
    for seed in seeds:
        evidence = _traverse_from(
            conn, seed["id"],
            seed_ids=seed_ids,
            max_hops=max_hops,
            body_excerpt_chars=body_excerpt_chars,
            related_to_max_hop=GATE_RELATED_TO_MAX_HOP,
        )
        chains.append({"seed_id": seed["id"], "evidence": evidence})
        all_evidence_ids.update(e["id"] for e in evidence)

    active_workstream_ids = _workstream_ids_from_seeds(seeds)
    prio = priorities.list_for_context(conn, active_workstream_ids)
    return {
        "query": query,
        "seeds": seeds,
        "chains": chains,
        "evidence_node_ids": sorted(all_evidence_ids),
        "priorities": [_priority_for_prompt(p) for p in prio],
    }


def blast_radius(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    body_excerpt_chars: int = DEFAULT_BODY_EXCERPT,
) -> list[dict]:
    """Public wrapper over the traversal engine, seeded from a single node.

    Reused by kb_correct (`verify.correct_plan`, KB id=1151) to compute the
    side-effect set of a node about to be corrected: every node reachable
    from `node_id` over `TRAVERSAL_RELATIONS` (canonical 6 + related_to +
    reconciled_by), both in and out edges, up to `max_hops`. Unlike the gate,
    this keeps the FULL `related_to` depth (no `related_to_max_hop`) — a
    correction wants the wide net of everything that may carry the bad
    framing. Each returned node carries
    `via_relation` / `direction` / `hop` / `path` so the caller can judge
    which neighbors actually carried the bad framing forward (direction='in'
    over a canonical relation = a likely framing-carrier).

    Unlike `assemble_gate`, there is no hybrid-search or focus seeding — the
    blast radius is exactly the graph neighborhood of the one node.
    """
    return _traverse_from(
        conn, node_id,
        seed_ids=set(),
        max_hops=max_hops,
        body_excerpt_chars=body_excerpt_chars,
    )


# ---------- seed collection ----------

def _collect_seeds(
    conn: sqlite3.Connection,
    query: str,
    *,
    seed_top_k: int,
    include_stale: bool,
    focus_seed: bool,
    body_excerpt_chars: int,
) -> tuple[list[dict], set[int]]:
    """Hybrid-search hits first, then focus workstreams that aren't already
    in the seed set. `track_access=False` so gate retrieval doesn't
    pollute the ref_count promotion signal — that's reserved for organic
    `kb_search` use."""
    seeds: list[dict] = []
    seed_ids: set[int] = set()

    if query.strip():
        hits = search.hybrid_search(
            conn, query,
            limit=seed_top_k,
            include_stale=include_stale,
            track_access=False,
        )
        for h in hits:
            if h["id"] in seed_ids:
                continue
            if h["kind"] in EXCLUDED_SEED_KINDS:
                continue
            seeds.append(_seed_from_hit(h, body_excerpt_chars))
            seed_ids.add(h["id"])

    if focus_seed:
        for f in db.get_focus(conn, limit=db.FOCUS_CAP):
            wid = f["workstream_id"]
            if wid in seed_ids:
                continue
            seeds.append(_seed_from_focus(f, body_excerpt_chars))
            seed_ids.add(wid)

    return seeds, seed_ids


def _seed_from_hit(h: dict, excerpt_chars: int) -> dict:
    return {
        "id": h["id"],
        "kind": h["kind"],
        "title": h["title"],
        "body_excerpt": _excerpt(h.get("body"), excerpt_chars),
        "status": h["status"],
        "workstream_id": h.get("workstream_id"),
        "source": "hybrid",
        "score": float(h.get("score") or 0.0),
    }


def _seed_from_focus(f: dict, excerpt_chars: int) -> dict:
    # f comes from db.get_focus — joined with the workstream node's fields.
    return {
        "id": f["workstream_id"],
        "kind": f["kind"],
        "title": f["title"],
        "body_excerpt": _excerpt(f.get("body"), excerpt_chars),
        "status": f["status"],
        "workstream_id": f["workstream_id"],
        "source": "focus",
        "score": float(f.get("effective_score") or 0.0),
    }


def _workstream_ids_from_seeds(seeds: list[dict]) -> list[int]:
    """Resolve workstreams that are in scope for this gate assembly.

    Hybrid hits carry their owning `workstream_id`; a workstream hit from the
    current request carries its own id. Focus seeds remain traversal context
    only: being recently active is not enough to make a workstream priority
    apply to an unrelated request.
    """
    out: list[int] = []
    seen: set[int] = set()
    for seed in seeds:
        if seed.get("source") == "focus":
            continue
        wid = seed.get("workstream_id")
        if wid is None and seed.get("kind") == "workstream":
            wid = seed.get("id")
        if wid is None:
            continue
        try:
            wid_int = int(wid)
        except (TypeError, ValueError):
            continue
        if wid_int not in seen:
            seen.add(wid_int)
            out.append(wid_int)
    return out


def _priority_for_prompt(p: dict) -> dict:
    return {
        "id": p["id"],
        "title": p["title"],
        "scope": p.get("scope") or (
            "overall" if p.get("workstream_id") is None else "workstream"
        ),
        "workstream_id": p.get("workstream_id"),
        "workstream_title": p.get("workstream_title"),
    }


# ---------- traversal ----------

def _traverse_from(
    conn: sqlite3.Connection,
    seed_id: int,
    *,
    seed_ids: set[int],
    max_hops: int,
    body_excerpt_chars: int,
    related_to_max_hop: int | None = None,
) -> list[dict]:
    """BFS from seed up to `max_hops` over TRAVERSAL_RELATIONS.

    `related_to_max_hop` (id=1415 #3): when set, `related_to` edges are only
    followed up to that hop depth; canonical + `reconciled_by` relations still
    traverse to `max_hops`. None (the default, used by blast_radius) walks
    `related_to` to full depth. assemble_gate passes GATE_RELATED_TO_MAX_HOP.

    Returns evidence nodes in stable order (sorted by hop, then via_relation,
    then id) so the classifier prompt sees deterministic context. Seeds are
    excluded from evidence — they're rendered separately."""
    visited: set[int] = {seed_id}
    by_id: dict[int, dict] = {}

    frontier: list[tuple[int, list[int]]] = [(seed_id, [])]  # (node_id, path)
    for hop in range(1, max_hops + 1):
        next_frontier: list[tuple[int, list[int]]] = []
        for current_id, path_so_far in frontier:
            for edge in _outgoing_and_incoming(conn, current_id):
                # Prune dense `related_to` past its hop budget (id=1415 #3).
                # Skip WITHOUT marking visited so the same node can still be
                # reached this hop via a canonical / reconciled_by edge (and
                # gets attributed to that higher-signal relation instead).
                if (related_to_max_hop is not None
                        and hop > related_to_max_hop
                        and edge["relation"] == "related_to"):
                    continue
                if edge["other_id"] in visited:
                    continue
                visited.add(edge["other_id"])
                new_path = path_so_far + [edge["other_id"]]
                node = _evidence_node(
                    conn, edge["other_id"],
                    via_relation=edge["relation"],
                    direction=edge["direction"],
                    hop=hop,
                    path=new_path,
                    body_excerpt_chars=body_excerpt_chars,
                )
                if node is None:
                    continue
                by_id[edge["other_id"]] = node
                next_frontier.append((edge["other_id"], new_path))
        frontier = next_frontier
        if not frontier:
            break

    # Drop any node that's also a seed (different seed reaches the same node
    # via traversal — render it as a seed there, not an evidence here).
    evidence = [n for nid, n in by_id.items() if nid not in seed_ids]
    evidence.sort(key=lambda n: (n["hop"], n["via_relation"], n["id"]))
    return evidence


def _outgoing_and_incoming(
    conn: sqlite3.Connection, node_id: int,
) -> Iterable[dict]:
    """Yield {other_id, relation, direction} for every edge touching `node_id`
    whose canonicalized relation is in TRAVERSAL_RELATIONS. Direction is from
    the perspective of `node_id`: 'out' = node_id is src; 'in' = node_id is dst.
    """
    rows = conn.execute(
        """
        SELECT src, dst, relation FROM edges
        WHERE (src = ? OR dst = ?) AND status = 'active'
        """,
        (node_id, node_id),
    ).fetchall()
    for r in rows:
        canonical = db.canonicalize_relation(r["relation"])
        if canonical not in TRAVERSAL_RELATIONS:
            continue
        if r["src"] == node_id:
            yield {"other_id": r["dst"], "relation": canonical, "direction": "out"}
        else:
            yield {"other_id": r["src"], "relation": canonical, "direction": "in"}


def _evidence_node(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    via_relation: str,
    direction: str,
    hop: int,
    path: list[int],
    body_excerpt_chars: int,
) -> dict | None:
    """Materialize an evidence node row. Returns None if the node id no
    longer exists (FK CASCADE should make this rare, but defensive)."""
    row = conn.execute(
        "SELECT id, kind, title, body, status, workstream_id, updated_at "
        "FROM nodes WHERE id = ?",
        (node_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "body_excerpt": _excerpt(row["body"], body_excerpt_chars),
        "status": row["status"],
        "workstream_id": row["workstream_id"],
        "updated_at": row["updated_at"],
        "via_relation": via_relation,
        "direction": direction,
        "hop": hop,
        "path": list(path),
    }


# ---------- helpers ----------

def _excerpt(body: str | None, n: int) -> str:
    if not body:
        return ""
    body = body.strip()
    if len(body) <= n:
        return body
    return body[: n - 1].rstrip() + "…"


# ---------- step 4b: classifier ----------

# 300s, raised from 150s (which was raised from 90s): a *successful* classify on a
# mature KB legitimately takes ~70–150s of thoughtful reasoning + unavoidable backend
# startup — and on 2026-06-15 a 150s ceiling cut off a NORMAL ~10k-token classify
# (id=1732). Correctness > speed (decision id=1462, reconciled by id=1807): we tolerate
# the latency rather than downgrade the model or cut a live call. 300 is INTERIM MARGIN,
# not the fix — the residual latency is backend startup + genuine reasoning, attacked at
# the source by a warm subprocess (id=1463) + a streaming-liveness watchdog (id=1732
# items 3-4), which should make even 150s rarely hit. A None verdict (timeout) silently
# reads as PROCEED, so the ceiling must sit above genuine reasoning, not at its p50.
# Env-overridable via LATCH_GATE_CLASSIFIER_TIMEOUT_S / CLAUDE_KB_GATE_CLASSIFIER_TIMEOUT_S.
# SECOND CEILING (id=1483): the MCP client enforces its own per-tool-call timeout
# (default effectively unbounded), so 300 (and 300+ADVERSARY on PROCEED) does not hit it
# on a default install — but any install that sets an explicit MCP timeout MUST keep it
# above CLASSIFIER_TIMEOUT_S + ADVERSARY_TIMEOUT_S.
CLASSIFIER_TIMEOUT_S = _env_int_any(
    ("LATCH_GATE_CLASSIFIER_TIMEOUT_S", "CLAUDE_KB_GATE_CLASSIFIER_TIMEOUT_S"),
    300,
)
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"
SUPPORTED_CLASSIFIER_BACKENDS = {"claude", "codex"}
# CREATE_NO_WINDOW: don't flash a console window per claude.cmd call when the
# parent has no console. 0 on POSIX (no-op). See heal.py for the full rationale.
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Locked v1 stance per design doc §4.1 + open_question 474:
# - Output is a structured **side-note**: surface the recommendation, the
#   user/agent decides whether to follow it. NOT active-redirect (which would
#   rewrite the agent's next action). Revisit after a week of real usage.
# - Few-shot examples cover PROCEED / MODIFY / DO_NOT_PROCEED to mitigate the
#   MODIFY hedge bias flagged in open_question 473. NEEDS_HUMAN_JUDGMENT is
#   demonstrated by absence — pick it only when KB context is genuinely thin.
CLASSIFIER_LABELS = ("PROCEED", "MODIFY", "DO_NOT_PROCEED", "NEEDS_HUMAN_JUDGMENT")

# Citation-gap sufficiency check (KB id=1220 / decision id=1253). The classifier
# enumerates the load-bearing claims its verdict rests on and tags each with its
# evidence; claims with evidence_type="none" are gaps. The remedy ROUTING is a
# deterministic engine mapping — not CLAUDE.md prose, per id=1156 / id=1203: the
# LLM observes the gap_type, the engine maps gap_type → suggested_remedy here.
# Ordered tuple (not a set) so the evidence_type histogram in gate.log has a
# stable key order. Membership tests (`x in EVIDENCE_TYPES`) work on the tuple.
EVIDENCE_TYPES = ("kb_node", "user_input", "code_trace", "none")
GAP_TYPES = ("decision_or_history", "current_value_or_code", "unknowable")
GAP_REMEDY = {
    "decision_or_history": "hop_deeper",      # walk the supersede/reconcile chain; cite a node
    "current_value_or_code": "code_trace",    # trace file:line; cite the source
    "unknowable": "flag_to_user",             # surface as an explicit assumption
}
# Safest remedy when gap_type is missing/unknown: ask the user, never assume.
DEFAULT_REMEDY = "flag_to_user"

# ---------- step 4b-adversary: adversarial verdict layer (scope KB id=1343) ----------
# A SECOND model-backed call, fired only on PROCEED verdicts (the over-permissive
# case where a skeptic adds the most — scope §7 cost lever). It attacks the
# plan (mis-weighted retrieval: a stronger counter-node the classifier
# under-weighted) and surfaces the genuine forks that are the user's call. It
# is advisory/side-note v1 (id=529): it never auto-flips the verdict — the
# delta + objection ride along in verdict["adversary"] for the agent to surface.
#
# Default ON. This overrides the conservative-default-OFF posture because the
# adversarial layer is part of the gate's value: it looks for a stronger
# counter-node on permissive verdicts. NOTE: the actual per-gate token/latency
# delta is still unmeasured. Opt out with CLAUDE_KB_ADVERSARY=0
# (mcpServers.env is inherited here; this runs in the MCP process, not a hook).
# 120s (was 60s): the adversary is a second model call on a comparable prompt, so
# under the same startup overhead 60s timed out on most PROCEED gates and silently
# dropped the default-ON adversarial layer (id=1365). Correctness over speed —
# same rationale as the classifier ceiling (decision id=1462); kept under the
# classifier's 300s since the adversary prompt is narrower.
ADVERSARY_TIMEOUT_S = _env_int_any(
    ("LATCH_GATE_ADVERSARY_TIMEOUT_S", "CLAUDE_KB_GATE_ADVERSARY_TIMEOUT_S"),
    120,
)
ADVERSARY_ENABLED = os.environ.get("CLAUDE_KB_ADVERSARY", "1") != "0"
# Closed set the adversary may flip the verdict TO (cite-or-PROCEED bounds it).
# Mirror in capture_streams.VERDICT_DELTAS.
ADVERSARY_DELTAS = ("none", "MODIFY", "DO_NOT_PROCEED")

CLASSIFIER_SYSTEM = """You are the gate judgment layer for a project knowledge base.

Your job: given a user's coding/build/implement request and a KB chain
assembly (decisions, facts, progress, constraints, abandoned paths
surfaced via canonical-relation traversal), recommend one of four actions
for the agent that's about to execute the request:

  PROCEED              — the request aligns with prior decisions and
                         constraints; nothing in the KB blocks it.
  MODIFY               — the request can succeed but needs adjustment to
                         account for prior decisions, abandoned paths, or
                         active constraints. Be specific about WHAT to change.
  DO_NOT_PROCEED       — prior decisions explicitly ruled this out, or the
                         abandoned-path evidence shows the request would
                         repeat a known dead end. Cite the specific node ids.
  NEEDS_HUMAN_JUDGMENT — KB context is genuinely thin or contradictory; the
                         human should decide. Pick this ONLY when you cannot
                         credibly choose one of the other three. (LLMs hedge
                         here under uncertainty — do not.)

Anti-hedge rule: if the chain has clear evidence pointing one way, commit to
PROCEED / MODIFY / DO_NOT_PROCEED with cited node ids. NEEDS_HUMAN_JUDGMENT
is for chains with ambiguous, missing, or contradictory signals — not for
"I'm not sure." Same for MODIFY: only use it when there's a specific
modification you can name. If you'd recommend MODIFY but can't say what
to change, the answer is PROCEED with caveats.

Project priorities: you may also be given a list of ACTIVE PROJECT PRIORITIES.
Overall priorities are standing directives the user wants weighed on every
build (e.g. security review, cross-platform installability). Workstream
priorities are additive directives for the named in-scope workstream only. For
each priority shown, check whether the proposed work would honour or neglect
it. If a priority is clearly neglected or would be violated, prefer MODIFY and
name the priority (P1/P2/…) and the concrete adjustment; you may cite its node
id in evidence_nodes. Priorities are guidance to WEIGH, not hard gates — a
priority alone is not grounds for DO_NOT_PROCEED unless the request directly
contradicts it.

Citation-gap rule: after choosing a label, enumerate the load-bearing claims
your recommendation actually rests on — the assertions that, if false, would
change the verdict. For each, point at its backing:
  - "kb_node"    — a node in the chain above; put its id in evidence_ref.
  - "user_input" — something the request itself states; evidence_ref null.
  - "code_trace" — a fact about current code/values; evidence_ref a "file:line"
                   if you can name it, else null.
  - "none"       — you are relying on it but nothing above backs it. This is a
                   GAP; surfacing it is the point, so do NOT invent backing.
                   Add a gap_type so the agent knows how to resolve it:
       "decision_or_history"   — a why/decision the graph should hold but that
                                 wasn't walked (resolve by hopping deeper);
       "current_value_or_code" — an exact value or code behavior the graph
                                 structurally lacks (resolve by a code trace);
       "unknowable"            — not determinable from graph or code (must be
                                 confirmed by the user).
gap_type is required when evidence_type is "none", and null otherwise.

Output a single JSON object, nothing else:

{
  "recommendation": "PROCEED" | "MODIFY" | "DO_NOT_PROCEED" | "NEEDS_HUMAN_JUDGMENT",
  "summary": "<2-4 sentence rationale>",
  "decision_chain":     [<node_id>, ...],   # nodes that anchor the recommendation
  "abandoned_paths":    [<node_id>, ...],   # stale nodes in the chain that bear on this
  "active_constraints": [<node_id>, ...],   # constraint/fact nodes that bound the action
  "current_direction":  [<node_id>, ...],   # decisions/progress showing where work is heading
  "risk_if_proceed":    "<one sentence>",
  "better_next_action": "<one sentence — concrete, actionable; or empty if PROCEED>",
  "evidence_nodes":     [<node_id>, ...],   # all node ids cited above, deduped
  "load_bearing_claims": [                  # claims the recommendation rests on
    {"claim": "<assertion the plan depends on>",
     "evidence_type": "kb_node" | "user_input" | "code_trace" | "none",
     "evidence_ref": <node_id> | "<file:line or short note>" | null,
     "gap_type": "decision_or_history" | "current_value_or_code" | "unknowable" | null},
    ...
  ]
}

No markdown fences. No commentary outside the JSON.
"""

# Three synthetic exemplars teaching the *pattern* of citing node ids and
# committing to a label. They are illustrative only — the actual chain
# assembly the model sees at runtime replaces these placeholders. The
# domain is intentionally generic (a web service) so the patterns generalize.
CLASSIFIER_FEW_SHOT = """\
--- EXAMPLE 1 (PROCEED) ---
REQUEST: extend the Redis-backed session cache to the admin API

CHAIN ASSEMBLY:
seed [id=200, decision, status=canonical] Redis chosen for session cache after 4-way bake-off
  body: Bake-off of Redis vs Memcached vs in-process LRU vs Postgres-backed sessions. Redis wins on latency + cross-instance consistency...
  evidence:
    [id=201, hop=1, via=related_to(out), status=canonical] Redis ops baseline (p99 latency, connection-pool sizing)
    [id=202, hop=1, via=tested_against(out), status=stale] in-process LRU prototype
      body: In-process LRU evaluated and abandoned — cache misses on horizontal-scale instance flap; consistency model insufficient...
    [id=203, hop=1, via=related_to(in), status=canonical] session-key naming convention (tenant:user:scope)

OUTPUT:
{"recommendation":"PROCEED","summary":"Redis session cache already won the 4-way bake-off (id=200) and the key-naming convention (id=203) generalizes to the admin API. The in-process LRU prototype (id=202) is in the chain only as the abandoned alternative and does not bear on the admin-API extension.","decision_chain":[200,203],"abandoned_paths":[202],"active_constraints":[203],"current_direction":[200],"risk_if_proceed":"Admin-API key cardinality may push connection-pool sizing beyond the current baseline.","better_next_action":"","evidence_nodes":[200,201,202,203],"load_bearing_claims":[{"claim":"Redis is the chosen session cache","evidence_type":"kb_node","evidence_ref":200,"gap_type":null},{"claim":"the tenant:user:scope key convention generalizes to the admin API","evidence_type":"kb_node","evidence_ref":203,"gap_type":null},{"claim":"the admin API needs the same session semantics as the main API","evidence_type":"user_input","evidence_ref":null,"gap_type":null}]}

--- EXAMPLE 2 (MODIFY) ---
REQUEST: re-run the in-process job-queue prototype with a larger worker pool to fix throughput

CHAIN ASSEMBLY:
seed [id=300, decision, status=stale] in-process job queue prototype
  body: In-process queue tested 2026-04-23; throughput capped at 1/4 of target. Worker model couldn't survive worker-process restarts. Abandoned.
  evidence:
    [id=301, hop=1, via=supersedes(in), status=canonical] Redis Streams pipeline replaces in-process queue
      body: Switched to Redis Streams + consumer groups; durable across restarts, scales horizontally.
    [id=302, hop=2, via=related_to(out), status=canonical] worker-pool tuning landed in the Streams pipeline
      body: Worker-pool size now configurable per consumer group; addresses throughput shortfall in the live path.
    [id=303, hop=1, via=motivates(in), status=canonical] worker-process restart storm during deploy
      body: Original abandonment driver — in-process state lost on every rolling deploy.

OUTPUT:
{"recommendation":"MODIFY","summary":"The in-process queue was abandoned (id=300) because the worker-process model loses state across restarts (id=303), not because of pool size. Redis Streams (id=301) is the live path, and worker-pool tuning already landed there (id=302). Re-running the in-process prototype with a larger pool will not fix the abandonment reason.","decision_chain":[300,301,302],"abandoned_paths":[300],"active_constraints":[303],"current_direction":[301],"risk_if_proceed":"Same restart-storm failure returns; pool-size fix is wasted on the wrong layer.","better_next_action":"Apply any throughput tuning to the Redis Streams pipeline (id=301), which is the live path.","evidence_nodes":[300,301,302,303],"load_bearing_claims":[{"claim":"the in-process queue was abandoned for restart survival, not pool size","evidence_type":"kb_node","evidence_ref":303,"gap_type":null},{"claim":"Redis Streams is the live job-queue path","evidence_type":"kb_node","evidence_ref":301,"gap_type":null},{"claim":"the current Streams worker-pool size is the actual throughput bottleneck today","evidence_type":"none","evidence_ref":null,"gap_type":"current_value_or_code"}]}

--- EXAMPLE 3 (DO_NOT_PROCEED) ---
REQUEST: switch the storage layer to a NoSQL document store

CHAIN ASSEMBLY:
seed [id=431, idea, status=staging] NoSQL document-store migration
  body: Considered moving primary storage to a document DB for schema flexibility. Verdict: wrong tradeoff. Audit-log query patterns require relational joins; schema validation at write-time is a non-negotiable for compliance.
  evidence:
    [id=400, hop=1, via=related_to(out), status=canonical] Postgres + strict schema migrations chosen as primary store
    [id=401, hop=1, via=related_to(out), status=canonical] audit-log query patterns require relational joins (compliance constraint)

OUTPUT:
{"recommendation":"DO_NOT_PROCEED","summary":"The NoSQL migration (id=431) is an explicitly-parked idea with verdict 'wrong tradeoff' — audit-log queries require relational joins (id=401) and Postgres with strict schemas is the locked decision (id=400). Implementing the NoSQL switch would unwind the deliberate compliance-driven architectural choice.","decision_chain":[431,400,401],"abandoned_paths":[431],"active_constraints":[400,401],"current_direction":[400],"risk_if_proceed":"Audit-log queries break; compliance constraint violated.","better_next_action":"If the goal is schema flexibility, see the locked Postgres approach (id=400) — JSONB columns there cover the flexibility need without losing relational guarantees.","evidence_nodes":[400,401,431],"load_bearing_claims":[{"claim":"audit-log queries require relational joins","evidence_type":"kb_node","evidence_ref":401,"gap_type":null},{"claim":"Postgres with strict schema is the locked primary store","evidence_type":"kb_node","evidence_ref":400,"gap_type":null},{"claim":"write-time schema validation is a compliance non-negotiable","evidence_type":"kb_node","evidence_ref":401,"gap_type":null}]}

--- END EXAMPLES ---
"""


def _evidence_sort_for_relevance(evidence: list[dict]) -> list[dict]:
    """Order evidence best-first for prompt inclusion (KB id=1415).

    Stable multi-key sort, most→least significant applied last (Python sort is
    stable, so we sort by the weakest key first):
      hop ascending → canonical relation before `related_to` → active before
      stale → recency descending.
    The recency tilt also addresses id=525 (4a had no per-node recency)."""
    ev = list(evidence)
    ev.sort(key=lambda n: n.get("updated_at") or "", reverse=True)          # recent first
    ev.sort(key=lambda n: 0 if (n.get("status") != "stale") else 1)         # active first
    ev.sort(key=lambda n: 0 if n.get("via_relation") in _HIGH_SIGNAL_RELATIONS else 1)
    ev.sort(key=lambda n: n.get("hop", 99))                                 # nearer first
    return ev


def _select_chain_evidence(
    evidence: list[dict], *, max_evidence: int | None, stale_budget: int,
) -> list[dict]:
    """Pick the top `max_evidence` evidence nodes for one chain's prompt slice,
    reserving up to `stale_budget` slots for stale nodes so the abandoned-path
    signal the gate exists to surface (see test_evidence_includes_stale_targets)
    cannot be entirely crowded out by fresher active nodes.

    Returns the selected nodes back in presentation order (hop, via_relation,
    id) — the same order the renderer used pre-cap. Output size never exceeds
    `max_evidence`."""
    if max_evidence is None or len(evidence) <= max_evidence:
        return list(evidence)
    if max_evidence <= 0:
        return []
    ranked = _evidence_sort_for_relevance(evidence)
    chosen = ranked[:max_evidence]
    chosen_ids = {n["id"] for n in chosen}
    n_stale = sum(1 for n in chosen if n.get("status") == "stale")
    if stale_budget and n_stale < stale_budget:
        extra_stale = [
            n for n in ranked
            if n.get("status") == "stale" and n["id"] not in chosen_ids
        ]
        non_stale = [n for n in chosen if n.get("status") != "stale"]
        # Swap weakest non-stale for strongest excluded stale, 1-for-1, so the
        # total stays == max_evidence. Bounded by both available pools.
        need = min(stale_budget - n_stale, len(extra_stale), len(non_stale))
        if need:
            evict = {n["id"] for n in non_stale[-need:]}
            chosen = [n for n in chosen if n["id"] not in evict] + extra_stale[:need]
    chosen.sort(key=lambda n: (n["hop"], n["via_relation"], n["id"]))
    return chosen


def _render_chain_for_prompt(
    chain_assembly: dict,
    *,
    max_chains: int = 5,
    max_evidence_per_chain: int = GATE_MAX_EVIDENCE_PER_CHAIN,
    max_total_evidence: int = GATE_MAX_TOTAL_EVIDENCE,
    stale_budget: int = GATE_STALE_BUDGET,
) -> str:
    """Serialize the assemble_gate() output into the human-readable form the
    classifier prompt consumes. Bounded on three axes (KB id=1415): at most
    `max_chains` seeds, at most `max_evidence_per_chain` ranked evidence nodes
    per seed, and at most `max_total_evidence` evidence nodes across the whole
    prompt. Omitted (lower-signal) nodes are noted inline so the classifier
    knows the chain was truncated rather than exhausted."""
    lines: list[str] = []
    seeds = chain_assembly.get("seeds") or []
    chains = chain_assembly.get("chains") or []
    chains_by_seed = {c["seed_id"]: c for c in chains}

    if not seeds:
        return "(no seeds — KB context is empty for this query)"

    total_rendered = 0
    for seed in seeds[:max_chains]:
        sid = seed["id"]
        lines.append(
            f"seed [id={sid}, {seed['kind']}, status={seed['status']}, "
            f"source={seed['source']}] {seed['title']}"
        )
        body = seed.get("body_excerpt", "")
        if body:
            lines.append(f"  body: {body}")
        chain = chains_by_seed.get(sid)
        full_ev = (chain or {}).get("evidence") or []
        if full_ev:
            remaining = (max_total_evidence - total_rendered
                         if max_total_evidence else max_evidence_per_chain)
            per_chain_cap = min(max_evidence_per_chain, max(0, remaining))
            shown = _select_chain_evidence(
                full_ev, max_evidence=per_chain_cap, stale_budget=stale_budget,
            )
            total_rendered += len(shown)
            if shown:
                lines.append("  evidence:")
                for ev in shown:
                    lines.append(
                        f"    [id={ev['id']}, hop={ev['hop']}, "
                        f"via={ev['via_relation']}({ev['direction']}), "
                        f"status={ev['status']}, {ev['kind']}] {ev['title']}"
                    )
                    eb = ev.get("body_excerpt", "")
                    if eb:
                        lines.append(f"      body: {eb}")
            omitted = len(full_ev) - len(shown)
            if omitted > 0:
                lines.append(
                    f"    … +{omitted} lower-signal evidence node(s) omitted "
                    f"(chain capped; highest-signal shown)"
                )
        lines.append("")
    return "\n".join(lines).rstrip()


def build_classifier_prompt(chain_assembly: dict, *, max_chains: int = 5) -> str:
    """Compose the full prompt: system + few-shot + actual chain + request."""
    request = chain_assembly.get("query", "").strip() or "(empty request)"
    rendered = _render_chain_for_prompt(chain_assembly, max_chains=max_chains)
    prio_block = priorities.render_for_gate(chain_assembly.get("priorities") or [])
    prio_section = (
        f"\n--- ACTIVE PROJECT PRIORITIES ---\n{prio_block}\n" if prio_block else ""
    )
    return (
        CLASSIFIER_SYSTEM
        + "\n"
        + CLASSIFIER_FEW_SHOT
        + prio_section
        + "\n--- ACTUAL REQUEST ---\n"
        + f"REQUEST: {request}\n\n"
        + "CHAIN ASSEMBLY:\n"
        + rendered
        + "\n\nOUTPUT:\n"
    )


def parse_classifier_output(raw: str | None) -> dict:
    """Extract the verdict JSON from a gate backend output. Mirrors the unwrap
    pattern in heal._parse_arbitrate_output: handle the JSON envelope, strip
    markdown fences, fall back to a labeled error result on any failure.

    Returns a dict with all expected keys; missing fields default to safe
    empties so the caller can render uniformly. Returns
    `recommendation=None` and `error=<reason>` on parse failure — that's
    different from a NEEDS_HUMAN_JUDGMENT verdict (which is a real choice)."""
    if not raw or not str(raw).strip():
        return _classifier_error("empty output")
    text = raw.strip()
    try:
        env = json.loads(text)
        if isinstance(env, dict):
            text = env.get("result") or env.get("response") or text
            if isinstance(text, dict):
                obj = text
                return _normalize_verdict(obj)
    except (json.JSONDecodeError, TypeError):
        pass
    if isinstance(text, str):
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:].strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return _classifier_error("no JSON object in output")
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            return _classifier_error(f"JSON parse failed: {e}")
        return _normalize_verdict(obj)
    return _classifier_error("unrecognized output shape")


def _normalize_verdict(obj: dict) -> dict:
    rec = obj.get("recommendation")
    if rec not in CLASSIFIER_LABELS:
        return _classifier_error(f"invalid recommendation {rec!r}")
    claims, uncovered = _normalize_claims(obj.get("load_bearing_claims"))
    return {
        "recommendation": rec,
        "summary": str(obj.get("summary", "")).strip(),
        "decision_chain": [int(x) for x in obj.get("decision_chain") or [] if _is_intish(x)],
        "abandoned_paths": [int(x) for x in obj.get("abandoned_paths") or [] if _is_intish(x)],
        "active_constraints": [int(x) for x in obj.get("active_constraints") or [] if _is_intish(x)],
        "current_direction": [int(x) for x in obj.get("current_direction") or [] if _is_intish(x)],
        "risk_if_proceed": str(obj.get("risk_if_proceed", "")).strip(),
        "better_next_action": str(obj.get("better_next_action", "")).strip(),
        "evidence_nodes": [int(x) for x in obj.get("evidence_nodes") or [] if _is_intish(x)],
        "load_bearing_claims": claims,
        "uncovered_claims": uncovered,
        "error": None,
    }


def _normalize_claims(raw) -> tuple[list[dict], list[dict]]:
    """Parse the classifier's `load_bearing_claims` into a clean list and derive
    the uncovered subset (evidence_type='none') with an engine-routed
    `suggested_remedy`. The trigger (an uncovered claim) is mechanical; the
    gap_type is the LLM's semantic call; the gap_type → remedy mapping is a
    deterministic engine constant (GAP_REMEDY), NOT CLAUDE.md prose
    (id=1156 / id=1203). KB id=1220 / decision id=1253.

    Defensive like `_is_intish` / the id-list coercion: drops malformed entries,
    coerces unrecognized evidence_type to 'none' (an unparseable tag is treated
    as a gap, never as covered), and never raises."""
    claims: list[dict] = []
    if not isinstance(raw, list):
        return claims, []
    for item in raw:
        if not isinstance(item, dict):
            continue
        claim_text = str(item.get("claim", "")).strip()
        if not claim_text:
            continue
        etype = item.get("evidence_type")
        if etype not in EVIDENCE_TYPES:
            etype = "none"  # unrecognized tag → treat as a gap, never as covered
        eref = item.get("evidence_ref")
        if isinstance(eref, bool):
            eref = None
        elif _is_intish(eref):
            eref = int(eref)
        elif eref is not None:
            eref = str(eref).strip() or None
        gap_type = item.get("gap_type")
        if etype != "none" or gap_type not in GAP_TYPES:
            gap_type = None  # only meaningful for gaps; unknown/missing → safe default below
        claims.append({
            "claim": claim_text,
            "evidence_type": etype,
            "evidence_ref": eref,
            "gap_type": gap_type,
        })
    uncovered = [
        {
            "claim": c["claim"],
            "gap_type": c["gap_type"],
            "suggested_remedy": GAP_REMEDY.get(c["gap_type"], DEFAULT_REMEDY),
        }
        for c in claims
        if c["evidence_type"] == "none"
    ]
    return claims, uncovered


def _is_intish(x) -> bool:
    if isinstance(x, bool):
        return False  # bool is a subclass of int in Python — exclude explicitly
    if isinstance(x, int):
        return True
    if isinstance(x, str):
        return x.strip().lstrip("-").isdigit()
    return False


def _classifier_error(reason: str) -> dict:
    reason = str(reason or "unknown gate error").strip().rstrip(".")
    return {
        "recommendation": None,
        "summary": f"Gate did not produce a recommendation: {reason}.",
        "decision_chain": [],
        "abandoned_paths": [],
        "active_constraints": [],
        "current_direction": [],
        "risk_if_proceed": "",
        "better_next_action": "",
        "evidence_nodes": [],
        "load_bearing_claims": [],
        "uncovered_claims": [],
        "error": reason,
    }


def _classifier_backend(name: str | None = None, *, default: str = "claude") -> str:
    """Resolve the gate model backend.

    ``claude`` is the legacy/default backend for Claude Code installs. Codex
    installs set ``LATCH_GATE_BACKEND=codex`` in their MCP environment so the
    engine does not quietly route Codex gate judgment through Claude.
    ``CLAUDE_KB_GATE_BACKEND`` remains as a compatibility alias for older local
    setups that already use the CLAUDE_KB_* env namespace.
    """
    raw = (
        name
        or os.environ.get("LATCH_GATE_BACKEND")
        or os.environ.get("CLAUDE_KB_GATE_BACKEND")
        or os.environ.get("LATCH_MODEL_BACKEND")
        or default
    )
    backend = str(raw).strip().lower()
    if backend not in SUPPORTED_CLASSIFIER_BACKENDS:
        raise ValueError(
            f"unsupported gate backend {raw!r}; "
            f"expected one of {sorted(SUPPORTED_CLASSIFIER_BACKENDS)}"
        )
    return backend


def _invoke_classifier_backend_once(
    prompt: str,
    *,
    backend: str,
    timeout_s: int,
    purpose: str = "classifier",
) -> tuple[str | None, str | None, bool]:
    """Invoke the selected model backend once.

    Returns (stdout_or_final_message, error_reason, timed_out). Parsing is kept
    by the caller so Claude's JSON envelope and Codex's final-message text share
    the same parse path.
    """
    backend = _classifier_backend(backend)
    if backend == "codex":
        return _invoke_codex_classifier_once(
            prompt, timeout_s=timeout_s, purpose=purpose,
        )
    return _invoke_claude_classifier_once(
        prompt, timeout_s=timeout_s, purpose=purpose,
    )


def _invoke_claude_classifier_once(
    prompt: str,
    *,
    timeout_s: int,
    claude_bin: str | None = None,
    purpose: str = "classifier",
) -> tuple[str | None, str | None, bool]:
    bin_path = claude_bin or CLAUDE_BIN
    env = os.environ.copy()
    # Reentrancy guard: classifier's own model call must not trigger hooks /
    # nested compactions. Same convention as heal.arbitrate.
    env["CLAUDE_KB_IN_COMPACT"] = "1"
    try:
        proc = subprocess.run(
            [bin_path, "-p", "--no-session-persistence", "--output-format", "json"],
            input=prompt,
            capture_output=True, text=True, encoding="utf-8",
            timeout=timeout_s,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return None, f"{purpose} timed out after {timeout_s}s", True
    except FileNotFoundError as e:
        return None, f"subprocess failed: {type(e).__name__}: {e}", False
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return None, f"claude backend exit {proc.returncode}: {detail[:300]}", False
    return proc.stdout, None, False


def _invoke_codex_classifier_once(
    prompt: str,
    *,
    timeout_s: int,
    codex_bin: str | None = None,
    purpose: str = "classifier",
) -> tuple[str | None, str | None, bool]:
    """Run Codex non-interactively as a gate classifier.

    The invocation mirrors the Codex compactor backend: temporary empty cwd,
    ignored user config/rules, ephemeral session, and read-only sandbox. That
    keeps the backend from loading project AGENTS.md or re-entering latch hooks
    while it is only producing a classifier JSON object.
    """
    bin_path = codex_bin or CODEX_BIN
    env = os.environ.copy()
    env["CLAUDE_KB_IN_COMPACT"] = "1"
    model = os.environ.get("LATCH_GATE_CODEX_MODEL") or os.environ.get("CODEX_GATE_MODEL")
    try:
        with tempfile.TemporaryDirectory(prefix="latch-codex-gate-") as tmp:
            out_path = Path(tmp) / "last_message.txt"
            args = [
                bin_path,
                "exec",
                "--ignore-user-config",
                "--ignore-rules",
                "--cd", tmp,
                "--skip-git-repo-check",
                "--ephemeral",
                "--sandbox", "read-only",
                "--output-last-message", str(out_path),
            ]
            if model:
                args.extend(["--model", model])
            args.append("-")
            proc = subprocess.run(
                args,
                input=prompt,
                capture_output=True, text=True, encoding="utf-8",
                timeout=timeout_s,
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            final_text = ""
            if out_path.exists():
                final_text = out_path.read_text(encoding="utf-8", errors="replace")
            if not final_text.strip():
                final_text = proc.stdout
    except subprocess.TimeoutExpired:
        return None, f"{purpose} timed out after {timeout_s}s", True
    except FileNotFoundError as e:
        return None, f"subprocess failed: {type(e).__name__}: {e}", False

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return None, f"codex backend exit {proc.returncode}: {detail[-1000:]}", False
    if not final_text.strip():
        return None, "codex backend returned empty final message", False
    return final_text, None, False


def classify_gate(
    chain_assembly: dict,
    *,
    project_path: str | None,
    use_llm: bool = True,
    max_chains: int = 5,
    timeout_s: int = CLASSIFIER_TIMEOUT_S,
    backend: str | None = None,
) -> dict:
    """Run the classifier LLM call against an assembled chain.

    Skipped paths (returns `recommendation=None` with a `skipped` flag):
    - kill switch active (`paths.is_disabled` / `is_in_compact`)
    - `use_llm=False` (used by tests + offline contexts)
    - daily budget cap hit (same gate the compactor + nightly heal use)

    On LLM error / parse failure, returns `recommendation=None` with an
    `error` string. Callers should treat None as "no judgment available"
    rather than a fourth label.
    """
    if paths.is_disabled() or paths.is_in_compact():
        return {**_classifier_error("disabled/in-compact"), "skipped": True}
    if not use_llm:
        return {**_classifier_error("use_llm=False"), "skipped": True}

    try:
        resolved_backend = _classifier_backend(backend)
    except ValueError as e:
        return {**_classifier_error(str(e)), "prompt_chars": 0}

    allowed, _ = budget.check_and_record(project_path, category="nonheal")
    if not allowed:
        return {**_classifier_error("daily budget cap hit"), "skipped": True}

    prompt = build_classifier_prompt(chain_assembly, max_chains=max_chains)
    prompt_chars = len(prompt)
    raw, err, timed_out = _invoke_classifier_backend_once(
        prompt, backend=resolved_backend, timeout_s=timeout_s,
        purpose="classifier",
    )
    if timed_out:
        # The failure mode this prompt-bounding change targets (id=1415). Flag it
        # distinctly so the surface layer can say "timed out" rather than emit a
        # silent no-op — a guard that fails invisibly is worst-case.
        return {**_classifier_error(err or f"classifier timed out after {timeout_s}s"),
                "prompt_chars": prompt_chars, "timed_out": True,
                "backend": resolved_backend}
    if err is not None or raw is None:
        return {**_classifier_error(err or "classifier subprocess failed"),
                "prompt_chars": prompt_chars, "backend": resolved_backend}
    result = parse_classifier_output(raw)
    result["prompt_chars"] = prompt_chars
    result["backend"] = resolved_backend
    return result


ADVERSARY_SYSTEM = """You are the adversarial reviewer for a project knowledge-base gate.

The classifier has proposed PROCEED on a coding/build request. Your job is NOT
to agree. Given the SAME chain assembly the classifier saw and its proposed
verdict, do two things:

1. ATTACK THE PLAN. Find the single strongest node in the chain that — taken at
   what its title/body says — refutes the plan, re-scopes it, or shows it
   repeats an abandoned path. This is about MIS-WEIGHTED evidence: a decision
   the classifier read but under-weighted, an abandoned path it ignored, a
   constraint it skipped. (The classifier already checks its OWN claims for
   missing backing — do not duplicate that; you look for backing that points
   the OTHER way.)
   CITE-OR-PROCEED (hard rule): a flip MUST cite a specific node id present in
   the chain above. If you cannot name one, set counter_node_id=null and
   verdict_delta="none" — the verdict stands. Never invent an objection; an
   uncited objection is worse than none.

2. SURFACE GENUINE FORKS. List only the decisions that are genuinely the
   USER'S call — turning on the user's domain expertise or preference, or
   stakeful / hard to reverse. NOT every ambiguity: if a sensible default
   exists and the choice is inside the agent's competence, do not ask. An empty
   list is the correct, common answer.

Output a single JSON object, nothing else. No markdown fences. No commentary.

{
  "objection": "<one sentence, or empty if none>",
  "counter_node_id": <node_id> | null,
  "verdict_delta": "none" | "MODIFY" | "DO_NOT_PROCEED",
  "design_decision_questions": [
    {"question": "<the fork>",
     "stake": "<why this is the user's call>",
     "options_hint": ["<option a>", "<option b>"]}
  ]
}
"""


# EXPERIMENTAL — mission-control / verification profiles. NOT recommended for use;
# planned to be unshipped to a separate branch later (observed unhelpful on
# pmeyer's workspace, 2026-06-10). See KB decision id=1550. Don't rely on / extend.
# (The default counter-node adversary below is unaffected and stays in service.)
ADVERSARY_SYSTEM_ASSUMPTION_HUNTER = """You are the adversarial reviewer for a project knowledge-base gate, in ASSUMPTION-HUNTER mode.

The classifier has proposed PROCEED on a request from a user who CANNOT verify
the agent's claims themselves — they rely on the agent to ground every assertion.
Your job is NOT to agree. Given the same chain assembly and proposed verdict, do
two things:

1. HUNT THE UNVERIFIED ASSUMPTION. Find the single load-bearing thing the plan
   TREATS AS TRUE without having verified it — above all a claim about what a
   config, parameter, flag, or code path CURRENTLY does, asserted from memory or
   from the KB rather than from reading the source. State that assumption as the
   objection and name the EXACT artifact that would settle it (e.g. "read
   config.toml model_tag", "grep EnableBidAskClamp in Skew20CurveFitter.cs").
   This is the failure class where a fluent guess substitutes for a checked fact.
   CITE-OR-PROCEED (hard rule): you may FLIP the verdict only if you can cite a
   specific node id in the chain that already contradicts or settles the
   assumption — set counter_node_id to it. If the assumption simply has not been
   checked (no node settles it), set counter_node_id=null and
   verdict_delta="none": the verdict stands, but your objection still names the
   assumption and the artifact that would verify it. Never invent a node id.

2. SURFACE GENUINE FORKS. List only the decisions that are genuinely the USER'S
   call — their domain expertise or preference, or stakeful / hard to reverse.
   NOT every ambiguity: if a sensible default exists and the choice is inside the
   agent's competence, do not ask. An empty list is the correct, common answer.

Output a single JSON object, nothing else. No markdown fences. No commentary.

{
  "objection": "<the unverified assumption + what would settle it, or empty>",
  "counter_node_id": <node_id> | null,
  "verdict_delta": "none" | "MODIFY" | "DO_NOT_PROCEED",
  "design_decision_questions": [
    {"question": "<the fork>",
     "stake": "<why this is the user's call>",
     "options_hint": ["<option a>", "<option b>"]}
  ]
}
"""


# Adversary system prompt by mode. Profile-selected (KB id=1420 / id=1428):
# 'counter_node' is the shipped default (attack MIS-WEIGHTED KB evidence);
# 'assumption_hunter' (mission-control profiles) attacks UNVERIFIED assumptions —
# the PBE-111 failure class. Same output contract, so _normalize_adversary /
# parse_adversary_output are unchanged. Unknown/missing mode → counter_node, so
# the default path stays byte-identical for unbound / trust-and-go actors.
ADVERSARY_SYSTEMS = {
    "counter_node": ADVERSARY_SYSTEM,
    "assumption_hunter": ADVERSARY_SYSTEM_ASSUMPTION_HUNTER,
}


def build_adversary_prompt(
    chain_assembly: dict, verdict: dict, *, max_chains: int = 5,
    mode: str = "counter_node",
) -> str:
    """Compose the adversary prompt: system (selected by `mode`) + the proposed
    verdict to attack + the same chain assembly the classifier saw + the request.
    `mode` defaults to the shipped counter-node reviewer; an unknown mode falls
    back to it so the default adversary path never changes."""
    request = chain_assembly.get("query", "").strip() or "(empty request)"
    rendered = _render_chain_for_prompt(chain_assembly, max_chains=max_chains)
    rec = verdict.get("recommendation") or "PROCEED"
    summary = (verdict.get("summary") or "").strip()
    cited = list(verdict.get("evidence_nodes") or [])
    return (
        ADVERSARY_SYSTEMS.get(mode, ADVERSARY_SYSTEM)
        + "\n--- PROPOSED VERDICT (attack this) ---\n"
        + f"recommendation: {rec}\n"
        + f"summary: {summary}\n"
        + f"cited_nodes: {cited}\n"
        + "\n--- ACTUAL REQUEST ---\n"
        + f"REQUEST: {request}\n\n"
        + "CHAIN ASSEMBLY:\n"
        + rendered
        + "\n\nOUTPUT:\n"
    )


def _normalize_decision_questions(raw) -> list[dict]:
    """Coerce the adversary's design_decision_questions into clean dicts.
    Defensive like _normalize_claims: drops malformed entries, never raises."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        q = str(item.get("question", "")).strip()
        if not q:
            continue
        opts = item.get("options_hint")
        if isinstance(opts, list):
            opts = [str(o).strip() for o in opts if str(o).strip()]
        else:
            opts = []
        out.append({
            "question": q,
            "stake": str(item.get("stake", "")).strip(),
            "options_hint": opts,
        })
    return out


def _normalize_adversary(obj: dict) -> dict:
    """Normalize the adversary JSON. Enforces the cite-or-PROCEED guard:
    a verdict flip with no cited counter node is downgraded to no-flip."""
    delta = obj.get("verdict_delta")
    if delta not in ADVERSARY_DELTAS:
        delta = "none"
    cnid = obj.get("counter_node_id")
    if isinstance(cnid, bool):
        cnid = None
    elif _is_intish(cnid):
        cnid = int(cnid)
    else:
        cnid = None
    # CITE-OR-PROCEED: no citable counter node → the verdict stands.
    if cnid is None:
        delta = "none"
    return {
        "objection": str(obj.get("objection", "")).strip(),
        "counter_node_id": cnid,
        "verdict_delta": delta,
        "design_decision_questions": _normalize_decision_questions(
            obj.get("design_decision_questions")
        ),
        "error": None,
    }


def _adversary_error(reason: str) -> dict:
    return {
        "objection": "",
        "counter_node_id": None,
        "verdict_delta": "none",
        "design_decision_questions": [],
        "error": reason,
    }


def parse_adversary_output(raw: str | None) -> dict:
    """Extract the adversary JSON from a gate backend output. Mirrors
    parse_classifier_output's envelope/fence unwrap, then normalizes."""
    if not raw or not str(raw).strip():
        return _adversary_error("empty output")
    text = raw.strip()
    try:
        env = json.loads(text)
        if isinstance(env, dict):
            inner = env.get("result") or env.get("response") or text
            if isinstance(inner, dict):
                return _normalize_adversary(inner)
            if isinstance(inner, str):
                text = inner
    except (json.JSONDecodeError, TypeError):
        pass
    if isinstance(text, str):
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:].strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return _adversary_error("no JSON object in output")
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            return _adversary_error(f"JSON parse failed: {e}")
        if not isinstance(obj, dict):
            return _adversary_error("parsed JSON is not an object")
        return _normalize_adversary(obj)
    return _adversary_error("unrecognized output shape")


def adversary_classify(
    chain_assembly: dict,
    verdict: dict,
    *,
    project_path: str | None,
    use_llm: bool = True,
    max_chains: int = 5,
    timeout_s: int = ADVERSARY_TIMEOUT_S,
    mode: str = "counter_node",
    backend: str | None = None,
) -> dict:
    """Run the adversary LLM call against the assembled chain + proposed
    verdict. Same skip/budget/subprocess shape as classify_gate; returns an
    `_adversary_error` (objection="", verdict_delta="none") on any skip/error
    so the caller can attach it uniformly."""
    if paths.is_disabled() or paths.is_in_compact():
        return {**_adversary_error("disabled/in-compact"), "skipped": True}
    if not use_llm:
        return {**_adversary_error("use_llm=False"), "skipped": True}

    try:
        resolved_backend = _classifier_backend(backend)
    except ValueError as e:
        return _adversary_error(str(e))

    # A real second LLM call — counts toward the same daily cap as the
    # classifier (scope §7 acknowledges the doubled per-gate cost on PROCEED).
    allowed, _ = budget.check_and_record(project_path, category="nonheal")
    if not allowed:
        return {**_adversary_error("daily budget cap hit"), "skipped": True}

    prompt = build_adversary_prompt(
        chain_assembly, verdict, max_chains=max_chains, mode=mode,
    )
    raw, err, _timed_out = _invoke_classifier_backend_once(
        prompt, backend=resolved_backend, timeout_s=timeout_s,
        purpose="adversary",
    )
    if err is not None or raw is None:
        adv = _adversary_error(err or "adversary subprocess failed")
        adv["backend"] = resolved_backend
        return adv
    adv = parse_adversary_output(raw)
    adv["backend"] = resolved_backend
    return adv


def _should_fire_adversary(verdict: dict) -> bool:
    """PROCEED-only firing lever (scope §7). The adversary adds the most value
    on the over-permissive case; MODIFY/DO_NOT_PROCEED are already
    non-permissive and skipped/errored verdicts carry no judgment to attack.
    Gated behind the conservative-default OFF flag (ADVERSARY_ENABLED)."""
    if not ADVERSARY_ENABLED:
        return False
    return verdict.get("recommendation") == "PROCEED"


def _counted(n: int, singular: str, plural: str | None = None) -> str:
    word = singular if n == 1 else (plural or f"{singular}s")
    return f"{n} {word}"


def _gate_receipt_summary(verdict: dict, evidence: list[dict]) -> str:
    """Short user-facing provenance line for a gate result."""
    counts = []
    if evidence:
        counts.append(_counted(len(evidence), "cited KB node"))
    decision_chain = verdict.get("decision_chain") or []
    if decision_chain:
        counts.append(_counted(len(decision_chain), "decision-chain id"))
    abandoned_paths = verdict.get("abandoned_paths") or []
    if abandoned_paths:
        counts.append(_counted(len(abandoned_paths), "abandoned-path id"))
    active_constraints = verdict.get("active_constraints") or []
    if active_constraints:
        counts.append(_counted(len(active_constraints), "active constraint"))
    current_direction = verdict.get("current_direction") or []
    if current_direction:
        counts.append(_counted(len(current_direction), "current-direction id"))

    basis = ", ".join(counts) if counts else "the assembled KB context"
    return (
        "Latch ran kb_gate on this request and used "
        f"{basis} to produce the verdict; cited node status carries current "
        "authority."
    )


def format_gate_findings(
    verdict: dict,
    evidence: list[dict],
    *,
    gate_status: str | None = None,
) -> dict:
    """User-visible compact gate surface.

    The full verdict remains machine-readable, but Codex often paraphrases tool
    results into native narration. This object is intentionally shaped as the
    chat artifact an agent should display before implementation so users can
    see latch's recommendation, cited KB nodes, and any unresolved gaps.
    """
    receipt_summary = _gate_receipt_summary(verdict, evidence)
    summary = str(verdict.get("summary") or "").strip()
    if not summary:
        error = str(verdict.get("error") or "").strip().rstrip(".")
        summary = (
            f"Gate did not produce a recommendation: {error}."
            if error else
            "Gate did not produce a recommendation."
        )
    out = {
        "label": "Latch gate findings",
        "must_display_to_user": True,
        "source": "kb_gate",
        "recommendation": verdict.get("recommendation"),
        "summary": summary,
        "risk_if_proceed": str(verdict.get("risk_if_proceed") or "").strip(),
        "better_next_action": str(verdict.get("better_next_action") or "").strip(),
        "decision_chain": list(verdict.get("decision_chain") or []),
        "abandoned_paths": list(verdict.get("abandoned_paths") or []),
        "active_constraints": list(verdict.get("active_constraints") or []),
        "current_direction": list(verdict.get("current_direction") or []),
        "evidence_nodes": [
            {
                "id": e.get("id"),
                "kind": e.get("kind"),
                "title": e.get("title"),
                "status": e.get("status"),
            }
            for e in evidence
        ],
        "load_bearing_claims": list(verdict.get("load_bearing_claims") or []),
        "uncovered_claims": list(verdict.get("uncovered_claims") or []),
        "receipt": {
            "summary": receipt_summary,
            "source": "kb_gate",
            "used": {
                "decision_chain": len(verdict.get("decision_chain") or []),
                "abandoned_paths": len(verdict.get("abandoned_paths") or []),
                "active_constraints": len(verdict.get("active_constraints") or []),
                "current_direction": len(verdict.get("current_direction") or []),
                "evidence_nodes": len(evidence),
                "load_bearing_claims": len(verdict.get("load_bearing_claims") or []),
                "uncovered_claims": len(verdict.get("uncovered_claims") or []),
            },
            "authority": (
                "Use evidence_nodes[].status as the visible current-authority "
                "surface; decision_chain, abandoned_paths, current_direction, "
                "and load_bearing_claims explain the rationale and source basis."
            ),
        },
        "why_it_matters": receipt_summary,
        "display_guidance": (
            "Show this as an explicit Latch gate block before acting: say Latch "
            "ran kb_gate on the request, then show verdict, summary/rationale, "
            "cited KB evidence nodes with status/current authority, source/basis, "
            "next action when present, and uncovered claims/gaps when present."
        ),
    }
    if gate_status is not None:
        out["gate_status"] = gate_status
    adversary = verdict.get("adversary")
    if isinstance(adversary, dict) and adversary:
        out["adversary"] = {
            "objection": adversary.get("objection"),
            "counter_node_id": adversary.get("counter_node_id"),
            "verdict_delta": adversary.get("verdict_delta"),
            "design_decision_questions": adversary.get("design_decision_questions") or [],
        }
    return out


# ---------- step 4c: top-level entry point ----------

def run_gate(
    conn: sqlite3.Connection,
    request: str,
    *,
    project_path: str | None,
    session_id: str | None = None,
    use_llm: bool = True,
    seed_top_k: int = DEFAULT_SEED_TOP_K,
    max_hops: int = DEFAULT_MAX_HOPS,
    max_chains: int = 5,
    body_excerpt_chars: int = DEFAULT_BODY_EXCERPT,
) -> dict:
    """End-to-end gate: assemble chain, classify, return combined result.

    The MCP `kb_gate` tool and the `/kb-gate` slash command both
    delegate here. Output:

        {
            "request":   <str>,
            "verdict":   <classifier output>,   # see parse_classifier_output
            "findings":  <user-visible compact findings>,
            "chains":    <chain assembly>,      # raw, for the agent to drill in
            "evidence":  [<compact_node>, ...], # flat, deduped, cited-only
        }

    `findings` is the side-note surface agents should show in chat. `evidence`
    stays as the raw compact cited-node list; the agent can `kb_get(<id>)` for
    full bodies.
    """
    t0 = time.perf_counter()
    chain_assembly = assemble_gate(
        conn, request,
        seed_top_k=seed_top_k,
        max_hops=max_hops,
        body_excerpt_chars=body_excerpt_chars,
    )
    verdict = classify_gate(
        chain_assembly,
        project_path=project_path,
        use_llm=use_llm,
        max_chains=max_chains,
    )

    cited = set(verdict.get("evidence_nodes") or [])
    # Hydrate cited nodes into a compact form. A cited id may be a seed or
    # a deeper evidence node — look up directly so we hit them all.
    evidence: list[dict] = []
    if cited:
        placeholders = ",".join("?" for _ in cited)
        rows = conn.execute(
            f"SELECT id, kind, title, status, workstream_id "
            f"FROM nodes WHERE id IN ({placeholders})",
            list(cited),
        ).fetchall()
        evidence = [dict(r) for r in rows]
        evidence.sort(key=lambda d: d["id"])

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    _log_invocation(
        project_path=project_path,
        session_id=session_id,
        request=request,
        verdict=verdict,
        chain_assembly=chain_assembly,
        evidence=evidence,
        elapsed_ms=elapsed_ms,
    )

    # Adversarial verdict layer (scope KB id=1343). PROCEED-only, default-off.
    # Advisory/side-note v1: rides along in verdict["adversary"]; the verdict
    # itself is NOT auto-flipped — the agent surfaces the objection/forks and
    # the user decides. Its own latency lands in adversary.log; the classifier
    # path's elapsed_ms (gate.log) above is unaffected.
    if use_llm and _should_fire_adversary(verdict):
        # Profile-selected adversary mode (KB id=1420): counter-node by default
        # (byte-identical to the shipped path), assumption-hunter for
        # mission-control profiles. Lightweight binding lookup, no writes.
        adv_mode = profiles.active_adversary_mode(conn)
        adv_t0 = time.perf_counter()
        adv = adversary_classify(
            chain_assembly, verdict,
            project_path=project_path, use_llm=use_llm, max_chains=max_chains,
            mode=adv_mode, backend=verdict.get("backend"),
        )
        adv_ms = round((time.perf_counter() - adv_t0) * 1000, 1)
        adv["mode"] = adv_mode
        verdict["adversary"] = adv
        _log_adversary(
            project_path=project_path, session_id=session_id, request=request,
            verdict_before=verdict.get("recommendation"), adv=adv,
            elapsed_ms=adv_ms, mode=adv_mode,
        )

    return {
        "request": request,
        "verdict": verdict,
        "findings": format_gate_findings(verdict, evidence),
        "chains": chain_assembly,
        "evidence": evidence,
    }


# ---------- invocation telemetry (gate.log) ----------

def _log_invocation(
    *,
    project_path: str | None,
    session_id: str | None,
    request: str,
    verdict: dict,
    chain_assembly: dict,
    evidence: list[dict],
    elapsed_ms: float,
) -> None:
    """Append one JSONL line per run_gate() call to the daily gate log
    (KB id=1091 conventions). Best-effort: any error is swallowed so
    logging never breaks the verdict path.

    `session_id` is the load-bearing correlation key for the Gap A+D
    correlator (KB id=1098). When run via the MCP `kb_gate` tool,
    mcp_server passes `PROJECT_SESSION_ID` (captured from
    `CLAUDE_CODE_SESSION_ID` at module load).
    """
    try:
        seeds = chain_assembly.get("seeds") or []
        entry = {
            "query_hash": _query_hash(request),
            "query_chars": len(request),
            "recommendation": verdict.get("recommendation"),
            "skipped": bool(verdict.get("skipped", False)),
            "error": verdict.get("error"),
            "evidence_ids": sorted(e["id"] for e in evidence),
            "decision_chain": list(verdict.get("decision_chain") or []),
            "seed_count": len(seeds),
            "seed_ids": [s["id"] for s in seeds],
            "reachable_count": len(chain_assembly.get("evidence_node_ids") or []),
            # Assembled classifier-prompt size — the direct driver of the id=1415
            # timeout, now observable per call alongside the reachable-set count
            # it derives from (capped at render time, so this is post-cap).
            "prompt_chars": verdict.get("prompt_chars"),
            "backend": verdict.get("backend"),
            "timed_out": bool(verdict.get("timed_out", False)),
            "elapsed_ms": elapsed_ms,
            "budget_count": _budget_count_snapshot(project_path),
            # Citation-gap structural signal (id=1220 / id=1253). Counts +
            # histograms only — claim TEXT stays in the in-context tool return,
            # never the structural log (id=1108 §3 / id=1225). Gives the Gap A+D
            # correlator a scorable obligation even on PROCEED verdicts (id=1232).
            "load_bearing_claim_count": len(verdict.get("load_bearing_claims") or []),
            "uncovered_claim_count": len(verdict.get("uncovered_claims") or []),
            "evidence_type_counts": _evidence_type_histogram(verdict),
            "gap_type_counts": _gap_type_histogram(verdict),
        }
        # Raw query text is opt-in only (structural-only invariant, id=1108
        # §3): query_hash above is the correlation key, query_excerpt is a
        # local human-debug affordance. Default off; CLAUDE_KB_LOG_RAW_QUERY=1
        # restores it.
        if LOG_RAW_QUERY:
            entry["query_excerpt"] = request[:LOG_QUERY_EXCERPT_CHARS]
            # Claim text is content, gated behind the same opt-in as query text.
            entry["uncovered_claim_texts"] = [
                str(u.get("claim", ""))[:LOG_QUERY_EXCERPT_CHARS]
                for u in (verdict.get("uncovered_claims") or [])
            ]
        log_utils.emit_event(
            LOG_STREAM, entry,
            project_path=project_path,
            session_id=session_id,
        )
    except Exception:
        pass


def _query_hash(request: str) -> str:
    """sha1[:12] of the request — the correlation key shared by gate.log and
    adversary.log so the offline correlator can join an adversary row back to
    its originating gate verdict/prompt. Structural-only (a hash, never text)."""
    return hashlib.sha1(
        request.encode("utf-8", errors="replace")
    ).hexdigest()[:12]


def _log_adversary(
    *,
    project_path: str | None,
    session_id: str | None,
    request: str,
    verdict_before: str | None,
    adv: dict,
    elapsed_ms: float,
    mode: str = "counter_node",
) -> None:
    """Emit one adversary.log row (structural-only, KB id=1343 / id=1108).
    Best-effort: any error is swallowed so the adversary path never breaks the
    verdict return. Token accounting from backend envelopes is a later
    refinement (Phase 5 correlator); tokens=None for now."""
    try:
        capture_streams.emit_adversary_event(
            verdict_before=verdict_before or "PROCEED",
            verdict_delta=adv.get("verdict_delta", "none"),
            counter_node_id=adv.get("counter_node_id"),
            n_forks_raised=len(adv.get("design_decision_questions") or []),
            latency_ms=int(elapsed_ms),
            query_hash=_query_hash(request),
            tokens=None,
            mode=mode,
            backend=adv.get("backend"),
            project_path=project_path,
            session_id=session_id,
        )
    except Exception:
        pass


def _budget_count_snapshot(project_path: str | None) -> int | None:
    """Read today's non-heal budget count without bumping it. Returns None on
    failure. Non-heal is the bucket gate itself spends from, so that's
    what the snapshot reports."""
    try:
        return budget.status(project_path)["nonheal"]["count"]
    except Exception:
        return None


def _evidence_type_histogram(verdict: dict) -> dict:
    """Structural-only count of claim evidence_types (no claim text). Stable key
    order from EVIDENCE_TYPES. id=1108 §3."""
    counts = {t: 0 for t in EVIDENCE_TYPES}
    for c in verdict.get("load_bearing_claims") or []:
        t = c.get("evidence_type")
        if t in counts:
            counts[t] += 1
    return counts


def _gap_type_histogram(verdict: dict) -> dict:
    """Structural-only count of uncovered-claim gap_types (no claim text)."""
    counts: dict[str, int] = {}
    for u in verdict.get("uncovered_claims") or []:
        g = u.get("gap_type") or "unspecified"
        counts[g] = counts.get(g, 0) + 1
    return counts
