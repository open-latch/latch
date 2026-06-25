"""kb_verify + kb_correct — the lightweight tier of the two-tier validation
model (KB id=888), plus the structured human-confirmed correction mechanism
(spec KB id=1151, resolves the open guardrail question id=886).

Three surfaces, all deterministic except where the agent supplies judgment:

* ``verify(node_id)`` — per-node deterministic audit, no LLM. Returns
  ``OK / RECONCILED / STALE / NOT_FOUND`` from ``nodes.status`` + outbound
  ``reconciled_by`` edges + incoming ``supersedes`` / ``replaces`` edges.
  Sub-millisecond; this is the "detect" half of the tier.

* ``correct_plan(bad_node_id)`` — read-only. Captures the pre-mutation
  snapshot and reuses the gate BFS (``gate.blast_radius``) to enumerate the
  side-effect set, then surfaces a supersede-vs-reconcile recommendation for
  the agent + human to confirm. NO mutation here.

* ``correct_apply(bad_node_id, mode, ...)`` — the "apply" half. Atomic,
  ordered, capture-before-mutation (KB id=1121): inserts the corrected node,
  wires the supersede-or-reconcile edge, adds ``reconciled_by`` edges from a
  judged subset of blast-radius framing-carriers, and emits one structural
  ``correction.log`` row (KB id=1091 / id=1108) as a human-labeled
  "the KB was wrong" RL reward signal.

Mutation is HUMAN-CONFIRMED, never auto-fired: detection is automatic (the
agent-inline classifier + the deterministic keyword-nudge in the
UserPromptSubmit hook), but ``correct_apply`` only runs on explicit user
confirmation. A misclassification must not be able to cascade stale-marks
across the graph on its own (nmeyer 2026-05-29, spec id=1151).

The supersede-vs-reconcile fork is the load-bearing distinction (KB id=864 /
id=271):

* ``mode="supersede"`` — the bad node is wholly wrong / hallucinated. New
  node ``supersedes`` it; the bad node goes ``status='stale'`` with its body
  LEFT UNTOUCHED (id=271 — staling, not editing, is what preserves the
  decision-change history). Edge is wired BEFORE the status flip so the
  reconciliation.log capture in ``db.add_edge`` sees the pre-stale status
  (the capture-before-mutation invariant, id=1121).
* ``mode="reconcile"`` — the bad node is true in its own scope but its
  framing was over-applied. New node is linked ``reconciled_by`` (both stay
  canonical, the banner surfaces the newer one); the bad node is NOT staled.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from typing import Iterable

import numpy as np

import db
import embeddings
import gate
import heal
import log_utils


VERIFY_OK = "OK"
VERIFY_RECONCILED = "RECONCILED"
VERIFY_STALE = "STALE"
VERIFY_NOT_FOUND = "NOT_FOUND"

CORRECT_MODES = ("supersede", "reconcile")

# Structural label for the correction.log `trigger` field — what surfaced the
# wrongness. Free-form None is allowed; these are the conventional values.
TRIGGER_USER_ASSERTION = "user_assertion"
TRIGGER_AGENT_SELF_CONTRADICTION = "agent_self_contradiction"

LOG_STREAM = "correction"

# ---------- kb_update claim-change guard (spec KB id=1175; enforces id=1174) ----------
#
# A NUDGE, not a block: in-place ``kb_update`` on a canonical fact/decision can
# silently rewrite the CLAIM and destroy the decision-change transition (the
# 2026-05-18 staleness chain id=856; id=954 id=888-overstatement). Policy
# id=1174 routes claim changes through ``kb_correct`` (supersede/reconcile) so
# the transition stays auditable. This guard makes that observable AT WRITE
# TIME — closing the same id=886 noticing-failure on the write path that
# ``kb_correct`` closed on the correction path. Posture mirrors id=825 (A1
# structured nudge, not auto-mutation) and the orphan_hint / plan_freshness_hint
# return-payload convention: never block the write, surface a hint, the human
# decides. System paths (nightly heal / compactor) call ``db.update_node``
# directly and never reach this — scope is strictly the MCP ``kb_update`` tool.

# Start at heal's dedup boundary — a known-meaningful cut in this MiniLM-384
# normalized space. Tune from claim_change.log telemetry (spec open question).
CLAIM_CHANGE_COSINE_THRESHOLD = 0.85

LOG_STREAM_CLAIM_CHANGE = "claim_change"


def _claim_change_cosine(
    old_embedding_blob: bytes | None, new_vec
) -> float | None:
    """Cosine between the stored (old) embedding and the freshly-embedded new
    text. Embeddings are L2-normalized (see embeddings.embed_batch), so the dot
    product IS the cosine — same idiom as hooks/user_prompt_submit.py. Returns
    None when either vector is missing or the shapes disagree (defensive: a
    dimension mismatch must never raise inside the kb_update hot path)."""
    old_vec = embeddings.from_blob(old_embedding_blob)
    if old_vec is None or new_vec is None:
        return None
    try:
        old_arr = np.asarray(old_vec, dtype=np.float32)
        new_arr = np.asarray(new_vec, dtype=np.float32)
    except (TypeError, ValueError):
        return None
    if old_arr.shape != new_arr.shape or old_arr.size == 0:
        return None
    return float(np.dot(old_arr, new_arr))


def compute_claim_change_hint(
    *,
    node_id: int,
    kind: str,
    status: str,
    old_embedding_blob: bytes | None,
    old_body: str | None,
    new_body: str | None,
    new_vec,
) -> dict | None:
    """Pure predicate. Returns a ``claim_change_hint`` dict when an in-place
    body edit looks like a CLAIM change on a canonical fact/decision, else None.

    Fires only when ALL hold (spec id=1175):
      1. body actually changed (title-only / status-only edits exempt);
      2. ``kind in {fact, decision}`` (claim-bearing kinds);
      3. ``status == 'canonical'`` (highest stakes; staging still being shaped);
      4. NOT a pure addition — the old body is not preserved (substring) in the
         new body. Appending a resolution/reconciliation banner keeps the old
         claim intact → exempt (kills the dominant false-positive: a banner
         append on a short node drops cosine without being a claim change);
      5. material embedding shift: ``cosine < CLAIM_CHANGE_COSINE_THRESHOLD``.

    Condition 4 is checked before the cosine so a pure addition short-circuits
    without needing the vectors at all.
    """
    if new_body is None or old_body is None or new_body == old_body:
        return None
    if kind not in ("fact", "decision"):
        return None
    if status != "canonical":
        return None
    if old_body in new_body:  # pure addition (banner/append) — claim preserved
        return None
    cos = _claim_change_cosine(old_embedding_blob, new_vec)
    if cos is None or cos >= CLAIM_CHANGE_COSINE_THRESHOLD:
        return None
    return {
        "node_id": node_id,
        "kind": kind,
        "cosine": round(cos, 4),
        "threshold": CLAIM_CHANGE_COSINE_THRESHOLD,
        "suggestion": "kb_correct_plan",
        "note": (
            f"This looks like a claim change on a canonical {kind} (id={node_id}, "
            f"cosine={round(cos, 4)}). If you're changing what this node asserts, "
            f"prefer kb_correct_plan({node_id}) so the transition is auditable "
            f"(policy id=1174). If this is a non-claim edit (banner/typo/cross-ref), "
            f"ignore."
        ),
    }


def record_claim_change(
    *,
    node_id: int,
    kind: str,
    status: str,
    old_embedding_blob: bytes | None,
    old_body: str | None,
    new_body: str | None,
    new_vec,
    project_path: str | None = None,
    session_id: str | None = None,
) -> dict | None:
    """Compute the claim-change hint AND emit one structural ``claim_change.log``
    row (id=1091 / id=1108 conventions — NO titles, bodies, or raw text). Returns
    the hint payload (dict) or None. Never raises: the actual kb_update proceeds
    regardless (nudge, not block). The telemetry row is emitted for every
    body-changing update (any kind) so the cosine distribution carries a baseline
    of legitimate non-claim edits for threshold calibration; ``hint_fired``
    flags the firing subset (fact/decision canonical claim changes)."""
    try:
        hint = compute_claim_change_hint(
            node_id=node_id, kind=kind, status=status,
            old_embedding_blob=old_embedding_blob,
            old_body=old_body, new_body=new_body, new_vec=new_vec,
        )
        cos = _claim_change_cosine(old_embedding_blob, new_vec)
        old_text_preserved = bool(
            old_body is not None and new_body is not None and old_body in new_body
        )
        log_utils.emit_event(
            LOG_STREAM_CLAIM_CHANGE,
            {
                "node_id": node_id,
                "kind": kind,
                "status": status,
                "cosine": round(cos, 4) if cos is not None else None,
                "body_len_before": len(old_body) if old_body is not None else None,
                "body_len_after": len(new_body) if new_body is not None else None,
                "old_text_preserved": old_text_preserved,
                "hint_fired": hint is not None,
            },
            project_path=project_path,
            session_id=session_id,
        )
        return hint
    except Exception:
        # Telemetry/guard must never break the write it observes.
        return None


# ---------- kb_verify: deterministic per-node audit ----------

def verify(conn: sqlite3.Connection, node_id: int) -> dict:
    """Deterministic, no-LLM verdict on a single node's current authority.

    Precedence: NOT_FOUND > STALE > RECONCILED > OK.

    * NOT_FOUND  — no such node.
    * STALE      — ``status='stale'`` OR an active incoming supersedes/replaces
                   edge points at it (it lost a replacement). Do not cite.
    * RECONCILED — still canonical/staging, but has an active outbound
                   ``reconciled_by`` edge: factually true in its own scope, but
                   a newer node constrains its framing. Read both before acting.
    * OK         — current and unconstrained.
    """
    node = db.get_node(conn, node_id)
    if node is None:
        return {"node_id": node_id, "verdict": VERIFY_NOT_FOUND}

    superseded_by = [
        r["src"] for r in conn.execute(
            "SELECT src FROM edges "
            "WHERE dst = ? AND status = 'active' "
            "AND relation IN ('supersedes', 'replaces') "
            "ORDER BY src ASC",
            (node_id,),
        ).fetchall()
    ]
    if node["status"] == "stale" or superseded_by:
        return {
            "node_id": node_id,
            "verdict": VERIFY_STALE,
            "status": node["status"],
            "superseded_by": superseded_by,
        }

    reconciled_by = [b["linked_id"] for b in db.reconciliation_banner(conn, node_id)]
    if reconciled_by:
        return {
            "node_id": node_id,
            "verdict": VERIFY_RECONCILED,
            "status": node["status"],
            "reconciled_by": reconciled_by,
        }

    return {"node_id": node_id, "verdict": VERIFY_OK, "status": node["status"]}


# ---------- kb_correct_plan: read-only blast radius + recommendation ----------

def correct_plan(
    conn: sqlite3.Connection,
    bad_node_id: int,
    *,
    max_hops: int = gate.DEFAULT_MAX_HOPS,
    body_excerpt_chars: int = gate.DEFAULT_BODY_EXCERPT,
) -> dict:
    """Read-only phase 1 of a correction. NO mutation.

    Returns the pre-mutation snapshot of the bad node, the blast radius
    (graph neighborhood via ``gate.blast_radius``), the framing-carrier
    candidates (inbound canonical-relation neighbors — the ones most likely
    to have propagated the bad framing), and a heuristic supersede-vs-reconcile
    recommendation. The agent + human make the final call at apply time.

    Also returns ``report_format``: a presentation directive telling the
    consuming agent to LEAD its user-facing report with a plain-English summary
    (what was wrong / how it was identified / the proposed fix) before any
    node/edge/mode detail. The structured fields are unchanged and remain
    available underneath — this only steers how the plan is narrated, since
    most users don't read graph mechanics.
    """
    node = db.get_node(conn, bad_node_id)
    if node is None:
        return {"error": f"node {bad_node_id} not found", "bad_node_id": bad_node_id}

    snapshot = {
        "id": bad_node_id,
        "kind": node["kind"],
        "title": node["title"],
        "status": node["status"],
        "ref_count": int(node.get("ref_count") or 0),
        "age_days": db._days_since(node.get("created_at")),
    }

    radius = gate.blast_radius(
        conn, bad_node_id,
        max_hops=max_hops, body_excerpt_chars=body_excerpt_chars,
    )

    # Framing-carrier candidates: neighbors that point AT the bad node
    # (direction='in') over a canonical traversal relation. These are the
    # nodes whose own framing most plausibly leans on the bad node, so they
    # are the prime candidates for a `reconciled_by` edge to the correction.
    # related_to inbound is surfaced too but flagged lower-confidence.
    carriers = [
        {
            "id": e["id"], "kind": e["kind"], "title": e["title"],
            "status": e["status"], "via_relation": e["via_relation"], "hop": e["hop"],
            "confidence": "high" if db.is_traversal_relation(e["via_relation"]) else "low",
        }
        for e in radius
        if e["direction"] == "in" and e["hop"] == 1
    ]

    return {
        "bad_node_id": bad_node_id,
        "snapshot": snapshot,
        "blast_radius": radius,
        "blast_radius_size": len(radius),
        "framing_carrier_candidates": carriers,
        "recommended_mode": "supersede",
        "recommendation_note": (
            "Heuristic default 'supersede' (wholly-wrong is the common case). "
            "Choose 'reconcile' instead when the bad node is TRUE in its own "
            "scope but its framing was over-applied (id=864 test). Pass the "
            "subset of framing_carrier_candidates that actually carried the "
            "bad framing forward as reconcile_ids — not every neighbor "
            "(avoids id=1118 graph pollution)."
        ),
        "report_format": (
            "When you surface this plan to the user, LEAD with a 2-4 sentence "
            "plain-English summary in this order: (1) what the stored knowledge "
            "got wrong, (2) how you identified it was wrong, (3) the fix you "
            "propose, in one sentence. Put it BEFORE any node ids, bodies, edges, "
            "modes, or reconcile_ids. Most users don't know what graph edges or "
            "supersede/reconcile mean — the structured detail above is for the "
            "reviewer and belongs AFTER the summary."
        ),
    }


# ---------- kb_correct_apply: atomic, ordered, capture-before-mutation ----------

def correct_apply(
    conn: sqlite3.Connection,
    bad_node_id: int,
    *,
    mode: str,
    title: str,
    body: str,
    kind: str = "decision",
    corrected_status: str = "canonical",
    reconcile_ids: Iterable[int] | None = None,
    workstream_id: int | None = None,
    links: list[dict] | None = None,
    trigger: str | None = None,
    prompt_hash: str | None = None,
    session_id: str | None = None,
    project_path: str | None = None,
) -> dict:
    """Phase 2 — apply a confirmed correction atomically.

    Ordering is load-bearing (capture-before-mutation, id=1121):
      1. snapshot the bad node's pre-mutation scalars;
      2. insert the corrected node (embedded, controlled — no heal arbitration);
      3. wire the primary edge:
         * supersede: ``corrected --supersedes--> bad`` THEN stale `bad`
           (edge first so db.add_edge's reconciliation.log capture sees the
           pre-stale status);
         * reconcile: ``bad --reconciled_by--> corrected`` (bad NOT staled);
      4. ``reconciled_by`` edges from each judged framing-carrier to corrected;
      5. apply any caller-supplied ``links`` from the corrected node;
      6. emit one structural ``correction.log`` row.

    Returns a summary including ``corrected_node_id`` and an ``orphan_hint``
    for the corrected body (same hygiene surface as kb_insert / kb_update).
    """
    if mode not in CORRECT_MODES:
        return {"error": f"mode must be one of {CORRECT_MODES}, got {mode!r}"}

    bad = db.get_node(conn, bad_node_id)
    if bad is None:
        return {"error": f"node {bad_node_id} not found", "bad_node_id": bad_node_id}

    t0 = time.perf_counter()

    # (1) capture-before-mutation — read pre-state into locals BEFORE any UPDATE.
    bad_status_before = bad["status"]
    bad_kind = bad["kind"]
    bad_ref_count = int(bad.get("ref_count") or 0)
    bad_age_days = db._days_since(bad.get("created_at"))
    inbound_edge_count = int(conn.execute(
        "SELECT COUNT(*) FROM edges WHERE dst = ? AND status = 'active'",
        (bad_node_id,),
    ).fetchone()[0])

    reconcile_list = [int(x) for x in (reconcile_ids or []) if int(x) != bad_node_id]

    # (2) insert the corrected node — controlled embed, no on-insert heal.
    blob = embeddings.to_blob(embeddings.embed(f"{title}\n\n{body}"))
    corrected_id = db.insert_node(
        conn, kind=kind, title=title, body=body, status=corrected_status,
        session_id=session_id, embedding=blob, workstream_id=workstream_id,
    )

    # (3) primary edge — capture-before-mutation ordering for supersede.
    staled = False
    if mode == "supersede":
        db.add_edge(
            conn, src=corrected_id, dst=bad_node_id, relation="supersedes",
            project_path=project_path, session_id=session_id,
        )
        db.update_node(conn, bad_node_id, status="stale")  # AFTER add_edge
        staled = True
    else:  # reconcile — both stay canonical, bad surfaces corrected via banner
        db.add_edge(
            conn, src=bad_node_id, dst=corrected_id, relation="reconciled_by",
            project_path=project_path, session_id=session_id,
        )

    # (4) reconciled_by edges from judged framing-carriers to the correction.
    reconciled_applied: list[int] = []
    for rid in reconcile_list:
        if db.get_node(conn, rid) is None:
            continue
        db.add_edge(
            conn, src=rid, dst=corrected_id, relation="reconciled_by",
            project_path=project_path, session_id=session_id,
        )
        reconciled_applied.append(rid)

    # (5) caller-supplied links from the corrected node (e.g. advances workstream).
    for link in links or []:
        dst = link.get("dst")
        rel = link.get("relation")
        if dst is None or not rel:
            continue
        db.add_edge(
            conn, src=corrected_id, dst=int(dst), relation=str(rel),
            project_path=project_path, session_id=session_id,
        )

    # (6) structural-only correction.log row (id=1091 / id=1108 — NO titles,
    # bodies, or raw prompt text; the trigger prompt is hashed by the caller).
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    log_utils.emit_event(
        LOG_STREAM,
        {
            "bad_node_id": bad_node_id,
            "bad_node_kind": bad_kind,
            "bad_node_status_before": bad_status_before,
            "bad_node_ref_count": bad_ref_count,
            "bad_node_age_days": bad_age_days,
            "bad_node_inbound_edges": inbound_edge_count,
            "mode": mode,
            "staled": staled,
            "corrected_node_id": corrected_id,
            "corrected_node_kind": kind,
            "reconcile_ids": reconciled_applied,
            "reconcile_count": len(reconciled_applied),
            "trigger": trigger,
            "prompt_hash": prompt_hash,
            "elapsed_ms": elapsed_ms,
        },
        project_path=project_path,
        session_id=session_id,
    )

    orphan_hint = heal.compute_orphan_hint(conn, corrected_id, body)
    return {
        "ok": True,
        "mode": mode,
        "bad_node_id": bad_node_id,
        "bad_node_status_before": bad_status_before,
        "staled": staled,
        "corrected_node_id": corrected_id,
        "reconcile_ids_applied": reconciled_applied,
        "orphan_hint": orphan_hint,
    }


def hash_prompt(text: str | None) -> str | None:
    """sha1[:12] of a triggering prompt for the correction.log `prompt_hash`
    field (id=611 hashing convention). Returns None for empty input."""
    if not text:
        return None
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12]
