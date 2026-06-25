"""Heal — on-insert dedup + nightly integrity/contradiction sweep.

On-insert entry point: `insert_with_heal(conn, kind, title, body, ...)`.
Nightly entry point: `nightly_heal(conn, project_path, ...)`.

Supersede semantics (per healing invariants, applies to both paths):
  * winner stays; loser marked status='stale' (never deleted — audit trail)
  * a `supersedes` edge is added from winner -> loser

Keep_both semantics (both paths): both stay; a `related_to` edge is added.

Three-pass arbitration (nightly only — on-insert just does pass C when use_llm):
  * Pass A: recency — if age_diff > 30d AND newer is still fresh, newer wins.
  * Pass B: ref_count — if dominant side has ref_count ratio >= 3 AND both are
    referenced (min >= 1), dominant wins.
  * Pass C: LLM arbitrator — use the selected model backend for a
    supersede/keep_both call.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np

import artifacts as artifact_store  # noqa: E402
import budget  # noqa: E402
import correlator  # noqa: E402
import db  # noqa: E402
import embeddings  # noqa: E402
import log_utils  # noqa: E402
import model_backends  # noqa: E402
import paths  # noqa: E402


# ≥0.85 triggers heal at insert time; 0.70-0.85 is deferred to nightly (Step 7).
SIMILARITY_THRESHOLD = 0.85
NEAR_DUP_TOP_K = 5
# was 60; raised to 150 (id=1570) — model CLI cold-start routinely exceeds 60s
# on slower boxes, so arbitration spuriously timed out (matches the kb_gate
# 90->150 bump; the compactor uses 180). Both arbitrate() and
# _arbitrate_nightly() share this.
ARBITRATE_TIMEOUT_S = 150
# Arbitrator circuit breaker (id=1570). A nightly pass attempts up to ~50 LLM
# arbitrations; if the selected model backend hangs, each burns the full
# ARBITRATE_TIMEOUT_S while the shared project lock is held — turning a slow pass into a tens-of-
# minutes stuck lock that blocks compaction and MCP writes (observed 2026-06-11:
# selfheal held the lock 11+ min). After this many consecutive backend
# timeouts, the arbitrator short-circuits to the safe keep_both default without
# spawning the subprocess. Any successful call resets the counter. Per-process
# (selfheal spawns a fresh process per pass), so each pass starts clean.
ARBITRATE_TIMEOUT_BREAKER = 2
_consecutive_arbitrate_timeouts = 0


def _arbitrator_circuit_open() -> bool:
    """True once ARBITRATE_TIMEOUT_BREAKER consecutive backend timeouts have
    accumulated — callers should skip the subprocess and return keep_both."""
    return _consecutive_arbitrate_timeouts >= ARBITRATE_TIMEOUT_BREAKER


def _note_arbitrate_timeout() -> None:
    global _consecutive_arbitrate_timeouts
    _consecutive_arbitrate_timeouts += 1


def _note_arbitrate_success() -> None:
    global _consecutive_arbitrate_timeouts
    _consecutive_arbitrate_timeouts = 0

# Nightly heal thresholds — two-tier (id=871):
#   high tier (sim >= NIGHTLY_SIMILARITY_THRESHOLD): near-duplicate detection,
#     full three-pass arbitration → {supersede, keep_both} (+ reconciled_by if
#     the LLM proposes it).
#   low tier (LOW_TIER_SIMILARITY_THRESHOLD <= sim < NIGHTLY_SIMILARITY_THRESHOLD):
#     topical-overlap detection, LLM-only → {reconciled_by, keep_both}; surfaces
#     framing drift the supersede/keep_both vocabulary can't express.
NIGHTLY_SIMILARITY_THRESHOLD = 0.70
LOW_TIER_SIMILARITY_THRESHOLD = 0.50
NIGHTLY_TOP_K = 5
RECENCY_AGE_DIFF_DAYS = 30
RECENCY_FRESH_WINDOW_DAYS = 30
REF_COUNT_RATIO_THRESHOLD = 3.0
REF_COUNT_MIN_BOTH = 1

HEAL_CODEX_MODEL_ENV = (
    "LATCH_HEAL_CODEX_MODEL",
    "CODEX_HEAL_MODEL",
    "LATCH_MAINTENANCE_CODEX_MODEL",
    "CODEX_MAINTENANCE_MODEL",
)


# ---------- artifact evidence (the evidence contract) ----------
#
# Artifacts are EVIDENCE, not law. When a heal arbitration carries artifact
# coordinates, the prompt must frame them as provenance (where the knowledge was
# observed), NOT as proof of where the claim applies — otherwise the LLM would
# wrongly treat a different file/repo as grounds to keep two genuinely-conflicting
# claims apart, or (worse) the reverse. Semantic content still owns the decision.
ARTIFACT_EVIDENCE_FRAMING = (
    "Artifact coordinates below are PROVENANCE EVIDENCE — where the knowledge was "
    "observed or which files were touched. They are NOT proof of where the claim "
    "applies. A global directive or broad architectural decision can legitimately "
    "supersede or reconcile across different artifact scopes. When the two nodes "
    "appear to belong to different repos/worlds, PREFER keep_both or reconciled_by "
    "over a destructive supersede unless the content clearly warrants it."
)


def _artifact_evidence_block(
    a_repos, b_repos, *, label_a: str = "A", label_b: str = "B",
) -> str:
    """A short evidence block for an arbitration prompt, or '' when NEITHER node
    has artifact evidence — keeping the prompt byte-identical for the scopeless
    majority (the evidence contract: no behavior change without evidence)."""
    a_repos = a_repos or frozenset()
    b_repos = b_repos or frozenset()
    if not a_repos and not b_repos:
        return ""
    a = ", ".join(sorted(a_repos)) or "(none)"
    b = ", ".join(sorted(b_repos)) or "(none)"
    return (
        "\n\n--- ARTIFACT EVIDENCE ---\n"
        + ARTIFACT_EVIDENCE_FRAMING
        + f"\nNODE {label_a} repos: {a}\nNODE {label_b} repos: {b}"
    )


# ---------- near-duplicate search ----------

def find_near_duplicates(
    conn,
    vec: np.ndarray,
    *,
    kind: str | None = None,
    exclude_id: int | None = None,
    threshold: float = SIMILARITY_THRESHOLD,
    top_k: int = NEAR_DUP_TOP_K,
) -> list[dict]:
    """Return nodes within cosine-similarity `threshold` of `vec`, strongest first.

    Uses the sqlite-vec virtual table when loaded, brute-force cosine otherwise.
    Stale nodes are excluded (supersede chains shouldn't cascade).
    """
    if db.vec_loaded(conn):
        try:
            candidates = _vec_candidates(conn, vec, top_k=top_k)
        except Exception:
            candidates = _brute_candidates(conn, vec, top_k=top_k)
    else:
        candidates = _brute_candidates(conn, vec, top_k=top_k)

    out = []
    for c in candidates:
        if c["id"] == exclude_id:
            continue
        if c["status"] == "stale":
            continue
        if kind is not None and c["kind"] != kind:
            continue
        if c["similarity"] < threshold:
            continue
        out.append(c)
    return out


def _vec_candidates(conn, vec: np.ndarray, top_k: int) -> list[dict]:
    qblob = embeddings.to_blob(vec)
    rows = conn.execute(
        "SELECT rowid, distance FROM vec_nodes "
        "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (qblob, top_k),
    ).fetchall()
    if not rows:
        return []
    ids = [r["rowid"] for r in rows]
    placeholders = ",".join("?" for _ in ids)
    node_rows = conn.execute(
        f"SELECT id, kind, title, body, status, session_id, created_at, updated_at "
        f"FROM nodes WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    nodes_by_id = {r["id"]: dict(r) for r in node_rows}
    out = []
    for r in rows:
        node = nodes_by_id.get(r["rowid"])
        if node is None:
            continue
        # cosine distance -> similarity
        node["similarity"] = 1.0 - float(r["distance"])
        out.append(node)
    return out


def _brute_candidates(conn, vec: np.ndarray, top_k: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, kind, title, body, status, session_id, created_at, updated_at, embedding "
        "FROM nodes WHERE embedding IS NOT NULL"
    ).fetchall()
    if not rows:
        return []
    mat = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    idx, scores = embeddings.cosine_topk(vec, mat, k=top_k)
    out = []
    for i, s in zip(idx, scores):
        d = dict(rows[i])
        d.pop("embedding", None)
        d["similarity"] = float(s)
        out.append(d)
    return out


# ---------- arbitration ----------

ARBITRATE_PROMPT = """You are the arbitrator for a project knowledge-base on-insert heal.

A new node is about to be inserted. It is highly similar to an existing node.
Decide whether they should be merged (new supersedes old) or both kept.

Return ONE JSON object, and nothing else:

  {"decision": "supersede" | "keep_both", "reason": "<one short sentence>"}

Guidance:
  * supersede: the new node contains the same or strictly-better information
    about the same thing — the old node is redundant or outdated.
  * keep_both: the nodes are related but cover distinct angles, contexts, or
    facets worth preserving side-by-side.
  * When in doubt, prefer keep_both — nightly heal can revisit with more context.

Output JSON only. No markdown fences, no commentary.
"""


def arbitrate(
    new: dict, old: dict, similarity: float,
    *, new_repos=frozenset(), old_repos=frozenset(),
) -> dict:
    """Ask the selected model backend to decide supersede vs keep_both.

    Returns a dict with `decision` and `reason`. On any failure, defaults to
    keep_both.

    `new_repos` / `old_repos` are the nodes' artifact repo scopes (the evidence
    contract): when present they are added to the prompt as provenance evidence
    so the arbitrator prefers keep_both/reconciled across disjoint scopes. They
    are evidence only — inline heal never blocks a collision merely because
    provenance differs."""
    if paths.is_disabled() or paths.is_in_compact():
        return {"decision": "keep_both", "reason": "arbitrator skipped (disabled/in-compact)"}
    if _arbitrator_circuit_open():
        return {"decision": "keep_both", "reason": "arbitrator circuit open (repeated model backend timeouts; id=1570)"}

    payload = (
        ARBITRATE_PROMPT
        + f"\n\n--- NEW NODE ---\nkind: {new.get('kind')}\n"
        + f"title: {new.get('title')}\n\n{new.get('body', '')}"
        + f"\n\n--- EXISTING NODE (id={old.get('id')}, similarity={similarity:.3f}) ---\n"
        + f"kind: {old.get('kind')}\ntitle: {old.get('title')}\n"
        + f"created_at: {old.get('created_at')}\nupdated_at: {old.get('updated_at')}\n\n"
        + (old.get("body") or "")
        + _artifact_evidence_block(new_repos, old_repos, label_a="NEW", label_b="EXISTING")
    )

    result = model_backends.invoke_prompt(
        payload,
        env_names=model_backends.MAINTENANCE_BACKEND_ENV,
        timeout_s=ARBITRATE_TIMEOUT_S,
        purpose="arbitrate",
        codex_model_env=HEAL_CODEX_MODEL_ENV,
    )
    if result.error is not None or result.text is None:
        if result.timed_out:
            _note_arbitrate_timeout()
        _log(f"arbitrate {result.backend} subprocess failed: {result.error}")
        return {
            "decision": "keep_both",
            "reason": f"arbitrator failed ({result.backend}): {result.error}",
        }

    _note_arbitrate_success()
    verdict = _parse_arbitrate_output(result.text)
    verdict["backend"] = result.backend
    return verdict


def _parse_arbitrate_output(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        return {"decision": "keep_both", "reason": "arbitrator returned empty output"}
    # Unwrap --output-format json envelope if present.
    try:
        env = json.loads(raw)
        text = env.get("result") or env.get("response") or raw
    except json.JSONDecodeError:
        text = raw
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return {"decision": "keep_both", "reason": "arbitrator output had no JSON object"}
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        return {"decision": "keep_both", "reason": f"arbitrator JSON parse failed: {e}"}
    decision = obj.get("decision")
    if decision not in ("supersede", "keep_both"):
        return {"decision": "keep_both", "reason": f"arbitrator returned unknown decision {decision!r}"}
    return {"decision": decision, "reason": str(obj.get("reason", ""))[:300]}


# ---------- nightly arbitration (symmetric A/B, four-verb) ----------

# Symmetric A/B prompt for nightly heal. Distinct from the asymmetric on-insert
# prompt above because:
#   1. Both nightly nodes are "existing" — NEW/EXISTING framing is misleading
#      (id=443).
#   2. Extended verb space includes `reconciled_by` for the framing-drift class
#      (id=871) — older node remains canonical, newer constrains its scope.
# Convention: callers pre-sort A=older, B=newer by updated_at (see _order_by_age),
# so the supersede_a / supersede_b / reconciled_by verbs have unambiguous direction.
NIGHTLY_ARBITRATE_PROMPT = """You are the arbitrator for a project knowledge-base nightly heal pass.

Two similar nodes (A and B) have been paired. Decide their relationship.
Convention: A is the OLDER node (lower updated_at), B is the NEWER node.

Return ONE JSON object, and nothing else:

  {"decision": "<verb>", "reason": "<one short sentence>"}

Decision verbs:

  * "supersede_a": A is strictly better — keep A canonical, mark B stale.
    Use when the older node already says everything the newer one says (and
    more), or the newer is redundant.
  * "supersede_b": B is strictly better — keep B canonical, mark A stale.
    Use when the newer node fully replaces the older (reversed decision,
    corrected data, updated parameters that invalidate the old framing
    entirely).
  * "reconciled_by": A remains factually correct in its own scope, but B
    constrains, narrows, re-scopes, or re-parameterises that framing. BOTH
    stay canonical; an edge A->B is added so future readers see B as a
    cross-reference whenever they look at A. Use when the older fact still
    describes something accurately (mechanism, plan, parameter, decision
    rationale) but a newer canonical decision constrains scope, time-scale,
    or hyperparameters without replacing the underlying claim.
  * "keep_both": A and B are related but cover distinct angles, contexts, or
    facets worth preserving side-by-side without a directional cross-reference.

Tie-breakers when in doubt:
  * Between supersede_* and reconciled_by: prefer reconciled_by (non-destructive).
  * Between reconciled_by and keep_both: prefer reconciled_by when one node's
    framing visibly depends on or is constrained by the other.
  * Between any two verbs: prefer keep_both (lowest-stakes default).

Output JSON only. No markdown fences, no commentary.
"""


NIGHTLY_VALID_DECISIONS = ("supersede_a", "supersede_b", "keep_both", "reconciled_by")


def _arbitrate_nightly(
    older: dict, newer: dict, similarity: float,
    *, a_repos=frozenset(), b_repos=frozenset(),
) -> dict:
    """Symmetric A/B arbitrator for nightly heal. Caller must pre-sort:
    `older` is the lower-updated_at node, `newer` is the higher. The prompt
    relies on this convention. `a_repos`/`b_repos` are the older/newer artifact
    repo scopes (evidence contract): when present they are added to the prompt
    as provenance evidence, biasing toward keep_both/reconciled across disjoint
    scopes — but content still owns the decision.

    Returns {"decision": <verb>, "reason": <str>}. On any failure, defaults
    to keep_both."""
    if paths.is_disabled() or paths.is_in_compact():
        return {"decision": "keep_both", "reason": "arbitrator skipped (disabled/in-compact)"}
    if _arbitrator_circuit_open():
        return {"decision": "keep_both", "reason": "arbitrator circuit open (repeated model backend timeouts; id=1570)"}

    payload = (
        NIGHTLY_ARBITRATE_PROMPT
        + f"\n\n--- NODE A (older, id={older.get('id')}, similarity={similarity:.3f}) ---\n"
        + f"kind: {older.get('kind')}\ntitle: {older.get('title')}\n"
        + f"created_at: {older.get('created_at', 'unknown')}\n"
        + f"updated_at: {older.get('updated_at', 'unknown')}\n\n"
        + (older.get("body") or "")
        + f"\n\n--- NODE B (newer, id={newer.get('id')}) ---\n"
        + f"kind: {newer.get('kind')}\ntitle: {newer.get('title')}\n"
        + f"created_at: {newer.get('created_at', 'unknown')}\n"
        + f"updated_at: {newer.get('updated_at', 'unknown')}\n\n"
        + (newer.get("body") or "")
        + _artifact_evidence_block(a_repos, b_repos, label_a="A", label_b="B")
    )

    result = model_backends.invoke_prompt(
        payload,
        env_names=model_backends.MAINTENANCE_BACKEND_ENV,
        timeout_s=ARBITRATE_TIMEOUT_S,
        purpose="arbitrate_nightly",
        codex_model_env=HEAL_CODEX_MODEL_ENV,
    )
    if result.error is not None or result.text is None:
        if result.timed_out:
            _note_arbitrate_timeout()
        _log(f"arbitrate_nightly {result.backend} subprocess failed: {result.error}")
        return {
            "decision": "keep_both",
            "reason": f"arbitrator failed ({result.backend}): {result.error}",
        }

    _note_arbitrate_success()
    verdict = _parse_arbitrate_nightly_output(result.text)
    verdict["backend"] = result.backend
    return verdict


def _parse_arbitrate_nightly_output(raw: str) -> dict:
    """Parse the four-verb nightly arbitrator response.

    Accepts the legacy single-verb `supersede` (from the on-insert prompt) and
    maps it to `supersede_b` so transitional outputs don't drop to keep_both
    silently. Everything else unknown defaults to keep_both."""
    raw = (raw or "").strip()
    if not raw:
        return {"decision": "keep_both", "reason": "arbitrator returned empty output"}
    try:
        env = json.loads(raw)
        text = env.get("result") or env.get("response") or raw
    except json.JSONDecodeError:
        text = raw
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return {"decision": "keep_both", "reason": "arbitrator output had no JSON object"}
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        return {"decision": "keep_both", "reason": f"arbitrator JSON parse failed: {e}"}
    decision = obj.get("decision")
    if decision == "supersede":
        decision = "supersede_b"
    if decision not in NIGHTLY_VALID_DECISIONS:
        return {"decision": "keep_both", "reason": f"arbitrator returned unknown decision {decision!r}"}
    return {"decision": decision, "reason": str(obj.get("reason", ""))[:300]}


# ---------- nightly arbitration ----------

def _parse_ts(s: str | None) -> datetime | None:
    """Parse DB timestamp (UTC naive). Returns None for nulls/unparseable."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _pick_by_recency(a: dict, b: dict) -> dict | None:
    """Returns {'winner': winner_node, 'loser': loser_node, 'reason': str} if
    recency pass fires, else None. Conditions: age diff > RECENCY_AGE_DIFF_DAYS
    AND the newer node was last updated within RECENCY_FRESH_WINDOW_DAYS.

    Both conditions matter: the second prevents picking "less-stale of two stale."
    """
    ta = _parse_ts(a.get("updated_at"))
    tb = _parse_ts(b.get("updated_at"))
    if ta is None or tb is None:
        return None
    now = datetime.now(timezone.utc)
    diff = abs((ta - tb).days)
    if diff <= RECENCY_AGE_DIFF_DAYS:
        return None
    newer, older = (a, b) if ta > tb else (b, a)
    newer_ts = ta if ta > tb else tb
    if (now - newer_ts).days > RECENCY_FRESH_WINDOW_DAYS:
        return None
    return {"winner": newer, "loser": older,
            "reason": f"newer by {diff}d and still fresh"}


def _order_by_age(a: dict, b: dict) -> tuple[dict, dict]:
    """Return (older, newer) by updated_at, falling back to created_at, then id.
    Used to give the symmetric nightly arbitrator a stable A/B convention so
    the supersede_a / supersede_b / reconciled_by verbs are unambiguous."""
    ta = _parse_ts(a.get("updated_at")) or _parse_ts(a.get("created_at"))
    tb = _parse_ts(b.get("updated_at")) or _parse_ts(b.get("created_at"))
    if ta is not None and tb is not None and ta != tb:
        return (a, b) if ta < tb else (b, a)
    ida, idb = a.get("id"), b.get("id")
    if ida is not None and idb is not None and ida != idb:
        return (a, b) if ida < idb else (b, a)
    return a, b


def _pick_by_ref_count(a: dict, b: dict) -> dict | None:
    """Returns a pick if one side dominates by ref_count, else None. Requires
    ratio >= REF_COUNT_RATIO_THRESHOLD AND both sides referenced at least
    REF_COUNT_MIN_BOTH times (a 0-reference loser is a cold-start signal, not
    a dominance signal). Restricted to same-kind pairs: cross-kind pairs
    (entity-vs-fact, decision-vs-progress, etc.) are usually complementary
    facets, not duplicates — defer those to the LLM."""
    if a.get("kind") != b.get("kind"):
        return None
    ra = int(a.get("ref_count") or 0)
    rb = int(b.get("ref_count") or 0)
    lo, hi = min(ra, rb), max(ra, rb)
    if lo < REF_COUNT_MIN_BOTH:
        return None
    if hi < lo * REF_COUNT_RATIO_THRESHOLD:
        return None
    winner, loser = (a, b) if ra > rb else (b, a)
    return {"winner": winner, "loser": loser,
            "reason": f"ref_count dominance {hi} vs {lo}"}


def three_pass_arbitrate(
    a: dict, b: dict, *, similarity: float = 0.0, use_llm: bool = True,
    tier: str = "high", a_repos=frozenset(), b_repos=frozenset(),
) -> dict:
    """Nightly arbitration. Two tiers (id=871):

    Artifact Evidence Contract: if `a_repos` and `b_repos` are both non-empty and
    DISJOINT, the deterministic recency/ref_count passes are skipped and the pair
    is routed to the LLM (which sees the repo evidence and may still supersede or
    reconcile). This prevents a silent, destructive cross-scope supersede; it
    does NOT hard-partition heal — same/overlapping/either-scopeless pairs and
    candidate discovery are unchanged.

    `tier="high"` (similarity >= NIGHTLY_SIMILARITY_THRESHOLD): full three-pass
      arbitration — Pass A recency → Pass B ref_count → Pass C LLM. LLM returns
      any of {supersede_a, supersede_b, keep_both, reconciled_by}.

    `tier="low"` (LOW_TIER_SIMILARITY_THRESHOLD <= similarity < ...): skip
      Pass A and Pass B (recency-of-fact and ref_count dominance are duplicate
      signals, not reconciliation signals) and go straight to the LLM. Expected
      verbs are reconciled_by or keep_both; supersede_* is still accepted if
      the LLM proposes it.

    Returns:
      {
        "decision": "supersede" | "keep_both" | "reconciled_by",
        "winner_id": int | None,    # set when decision=supersede
        "loser_id":  int | None,    # set when decision=supersede
        "older_id":  int | None,    # set when decision=reconciled_by
        "newer_id":  int | None,    # set when decision=reconciled_by
        "path":      "recency" | "ref_count" | "llm" | "skip",
        "tier":      "high" | "low",
        "reason":    str,
      }
    """
    # Cross-scope guard: both nodes carry artifact evidence and their repo sets
    # don't intersect → treat deterministic supersede as unsafe, defer to LLM.
    cross_scope_disjoint = bool(a_repos and b_repos and not (a_repos & b_repos))
    if tier == "high" and not cross_scope_disjoint:
        pick = _pick_by_recency(a, b)
        if pick is not None:
            return {
                "decision": "supersede",
                "winner_id": pick["winner"]["id"],
                "loser_id": pick["loser"]["id"],
                "older_id": None, "newer_id": None,
                "path": "recency", "tier": "high",
                "reason": pick["reason"],
            }
        pick = _pick_by_ref_count(a, b)
        if pick is not None:
            return {
                "decision": "supersede",
                "winner_id": pick["winner"]["id"],
                "loser_id": pick["loser"]["id"],
                "older_id": None, "newer_id": None,
                "path": "ref_count", "tier": "high",
                "reason": pick["reason"],
            }

    if not use_llm:
        return {
            "decision": "keep_both",
            "winner_id": None, "loser_id": None,
            "older_id": None, "newer_id": None,
            "path": "skip", "tier": tier,
            "reason": "deterministic passes inconclusive; LLM disabled",
        }

    older, newer = _order_by_age(a, b)
    # Map per-input repo scopes onto the older/newer ordering for the prompt.
    older_repos, newer_repos = (
        (a_repos, b_repos) if older.get("id") == a.get("id") else (b_repos, a_repos)
    )
    verdict = _arbitrate_nightly(
        older, newer, similarity, a_repos=older_repos, b_repos=newer_repos,
    )
    decision = verdict["decision"]
    reason = verdict.get("reason", "")

    if decision == "supersede_a":
        return {
            "decision": "supersede",
            "winner_id": older["id"], "loser_id": newer["id"],
            "older_id": None, "newer_id": None,
            "path": "llm", "tier": tier, "reason": reason,
        }
    if decision == "supersede_b":
        return {
            "decision": "supersede",
            "winner_id": newer["id"], "loser_id": older["id"],
            "older_id": None, "newer_id": None,
            "path": "llm", "tier": tier, "reason": reason,
        }
    if decision == "reconciled_by":
        return {
            "decision": "reconciled_by",
            "winner_id": None, "loser_id": None,
            "older_id": older["id"], "newer_id": newer["id"],
            "path": "llm", "tier": tier, "reason": reason,
        }
    return {
        "decision": "keep_both",
        "winner_id": None, "loser_id": None,
        "older_id": None, "newer_id": None,
        "path": "llm", "tier": tier, "reason": reason,
    }


def edge_exists_between(conn, x: int, y: int) -> bool:
    """True if any active edge exists between x and y in either direction, any
    relation. Tombstoned edges are treated as absent so heal will re-create the
    edge (add_edge re-activates the tombstoned row in place)."""
    row = conn.execute(
        "SELECT 1 FROM edges "
        "WHERE ((src = ? AND dst = ?) OR (src = ? AND dst = ?)) "
        "  AND status = 'active' "
        "LIMIT 1",
        (x, y, y, x),
    ).fetchone()
    return row is not None


# Supersede/replace lineage verbs: audit edges that must stay anchored on the
# (now stale) loser. Everything else is structural and inherits to the winner.
_LINEAGE_RELATIONS = {"supersedes", "replaces"}


def _inherit_edges(
    conn, winner_id: int, loser_id: int,
    *, project_path: str | None = None, session_id: str | None = None,
) -> int:
    """Re-point the loser's structural edges onto the winner so a supersede
    doesn't orphan them on a node that drops out of default reads (KB id=1118).

    For each active edge incident to the loser EXCEPT supersede/replace lineage
    edges (which stay on the loser for audit): add the equivalent edge anchored
    on the winner (idempotent) and tombstone the loser's copy. Self-loops and
    edges whose other endpoint is the winner are skipped (the just-added
    `supersedes` winner->loser edge is one such — and is lineage anyway).

    Call AFTER the `supersedes` edge is recorded (so reconciliation.log captures
    the supersede with the loser still non-stale) and BEFORE the loser is staled
    — the capture-before-mutation order (KB id=1121). Returns the count migrated.
    """
    migrated = 0
    for e in db.neighbors(conn, loser_id):
        rel = e["relation"]
        if rel in _LINEAGE_RELATIONS:
            continue  # leave supersede/replace lineage on the loser (audit)
        src, dst = e["src"], e["dst"]
        other = dst if src == loser_id else src
        if other == winner_id or other == loser_id:
            # A loser<->winner non-lineage link or a loser self-loop: re-pointing
            # would self-loop on the winner, and it's redundant once the loser is
            # subsumed. Retire it rather than leave it active on a stale node.
            db.tombstone_edge(conn, src=src, dst=dst, relation=rel)
            continue
        new_src = winner_id if src == loser_id else src
        new_dst = winner_id if dst == loser_id else dst
        db.add_edge(
            conn, src=new_src, dst=new_dst, relation=rel,
            project_path=project_path, session_id=session_id,
        )
        db.tombstone_edge(conn, src=src, dst=dst, relation=rel)
        migrated += 1
    return migrated


def apply_nightly_supersede(
    conn, winner_id: int, loser_id: int,
    *, project_path: str | None = None, session_id: str | None = None,
) -> None:
    """Winner stays as-is; loser's structural edges inherit to the winner; loser
    marked stale; supersedes edge winner -> loser.

    `add_edge` runs BEFORE `_inherit_edges` and `update_node` so reconciliation.log
    captures the loser's pre-stale status (KB id=1097/id=1121 capture-before-
    mutation rule). Edge inheritance (KB id=1118) runs before the stale mutation
    so the loser's structural edges are migrated to the winner, not orphaned.
    """
    db.add_edge(
        conn, src=winner_id, dst=loser_id, relation="supersedes",
        project_path=project_path, session_id=session_id,
    )
    _inherit_edges(
        conn, winner_id, loser_id,
        project_path=project_path, session_id=session_id,
    )
    db.update_node(conn, loser_id, status="stale")


def apply_nightly_reconciled_by(
    conn, older_id: int, newer_id: int,
    *, project_path: str | None = None, session_id: str | None = None,
) -> None:
    """Both nodes stay canonical; `reconciled_by` edge older -> newer.

    Distinct from apply_nightly_supersede: the older node remains factually
    correct in its own scope. The edge makes the newer node surface via
    `db.reconciliation_banner(conn, older_id)` whenever an agent kb_get's
    the older — read-time guidance, not enforcement (id=534 / id=862)."""
    db.add_edge(
        conn, src=older_id, dst=newer_id, relation="reconciled_by",
        project_path=project_path, session_id=session_id,
    )


# ---------- apply a decision ----------

def apply_supersede(
    conn, new_id: int, old_id: int,
    *, project_path: str | None = None, session_id: str | None = None,
) -> None:
    """Mark old stale and add a supersedes edge new -> old. Audit trail kept.
    The old node's structural edges inherit to the new node (KB id=1118).

    `add_edge` runs BEFORE `_inherit_edges` and `update_node` so reconciliation.log
    captures the old node's pre-stale status (KB id=1097/id=1121 capture-before-
    mutation rule).
    """
    db.add_edge(
        conn, src=new_id, dst=old_id, relation="supersedes",
        project_path=project_path, session_id=session_id,
    )
    _inherit_edges(
        conn, new_id, old_id,
        project_path=project_path, session_id=session_id,
    )
    db.update_node(conn, old_id, status="stale")


def apply_keep_both(conn, new_id: int, old_id: int) -> None:
    """Add a related_to edge new -> old."""
    db.add_edge(conn, src=new_id, dst=old_id, relation="related_to")


# ---------- plan-freshness hint ----------

# Edges from a ship-progress node to a plan-shaped node that signal the plan's
# body may now be out-of-date and should be kb_update'd by the agent.
PLAN_LINK_RELATIONS = ("implements", "advances", "depends_on")

# Plan-shaped kinds — nodes whose body acts as a "where are we" surface for
# downstream ship-progress to update. idea / open_question added per id=1194 §3:
# a parked idea's body is a living spec, so a ship node implementing it should
# nudge a body refresh too. This folds axis-3 self-state drift (a shipped node
# still calling itself a forward plan, e.g. id=871) into the existing axis-2
# plan-freshness mechanism rather than building a separate detector. The trigger
# is the incoming ship edge, NOT body language — an un-shipped parked spec has
# no such edge and stays silent until something actually ships against it.
PLAN_KINDS = ("progress", "decision", "workstream", "idea", "open_question")


def compute_plan_freshness_hint(
    conn, new_id: int, new_kind: str,
) -> list[dict]:
    """Return a structured nudge listing plan-shaped neighbors that may now
    be stale because of this ship-progress insert.

    Triggers only when `new_kind == "progress"` and the new node has at least
    one outbound edge whose relation is in PLAN_LINK_RELATIONS pointing at a
    non-stale node whose kind is in PLAN_KINDS. The agent is expected to
    follow up with `kb_update` on each listed `linked_id`.

    Empty list when no nudge applies. Cheap — single SQL join, no LLM call.
    """
    if new_kind != "progress":
        return []
    placeholders = ",".join("?" for _ in PLAN_LINK_RELATIONS)
    kind_placeholders = ",".join("?" for _ in PLAN_KINDS)
    rows = conn.execute(
        f"SELECT e.dst AS linked_id, e.relation AS relation, "
        f"       n.kind AS kind, n.title AS title "
        f"FROM edges e JOIN nodes n ON n.id = e.dst "
        f"WHERE e.src = ? "
        f"  AND e.status = 'active' "
        f"  AND e.relation IN ({placeholders}) "
        f"  AND n.kind IN ({kind_placeholders}) "
        f"  AND COALESCE(n.status, '') != 'stale' "
        f"ORDER BY e.dst ASC",
        (new_id, *PLAN_LINK_RELATIONS, *PLAN_KINDS),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- orphan hint (body-id mentions must be edges) ----------

# Matches `id=123` (the project's canonical way of naming a node in prose).
# `\b` anchors avoid matching things like `uuid=123` or `grid=4`.
_ID_MENTION_RE = re.compile(r"\bid=(\d+)\b")
# Fenced code blocks and inline-code spans are stripped before scanning so
# `id=X` inside a code example or quoted snippet doesn't trip a false orphan
# (kb_gate risk note on id=1149 Part 2).
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _strip_code_spans(text: str) -> str:
    """Blank out fenced + inline code so id=X inside code/quoted snippets is
    not scanned. Replaces with spaces (not "") to preserve excerpt offsets."""
    text = _FENCED_CODE_RE.sub(lambda m: " " * len(m.group(0)), text)
    text = _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), text)
    return text


def compute_orphan_hint(
    conn, node_id: int, body: str, kind: str | None = None,
) -> list[dict]:
    """Return body `id=X` mentions that lack an active edge to/from node_id.

    Mirrors `compute_plan_freshness_hint`: cheap, single SQL probe per unique
    mention, no LLM. A nudge in the A1 mould (KB id=825) — the agent should
    `kb_link` each (or drop the mention), but it does NOT block the write.

    Kind-scope (id=1194 §1/§2): when `kind` is supplied, only spec kinds
    (idea / open_question / decision) are scanned — their bodies make
    structural / dependency refs that SHOULD be edges. Index/summary kinds
    (workstream / progress / fact / entity) make curated citation refs that
    legitimately stay un-edged; scanning them over-fires (id=338: 27 flagged /
    2 real). The exempt set mirrors the nightly sweep's `DRIFT_SCAN_KINDS` so
    the write-time and nightly tiers can never disagree (the id=1158
    duplication lesson). `kind=None` preserves the pure-scanner contract for
    direct callers / unit tests.

    Self-references (`id=<node_id>`) are ignored. Code spans are stripped
    first (see `_strip_code_spans`) to avoid false positives on id=X inside
    examples. Edge existence is checked permissively — an active edge in
    EITHER direction satisfies the mention.

    Returns a list of `{"referenced_id": int, "body_excerpt": str}`, empty
    when every mention is edged (or there are none). Implements id=1149 Part 2.
    """
    if kind is not None:
        from drift import DRIFT_SCAN_KINDS
        if kind not in DRIFT_SCAN_KINDS:
            return []
    scannable = _strip_code_spans(body or "")
    hints: list[dict] = []
    seen: set[int] = set()
    for m in _ID_MENTION_RE.finditer(scannable):
        rid = int(m.group(1))
        if rid == node_id or rid in seen:
            continue
        seen.add(rid)
        edged = conn.execute(
            "SELECT 1 FROM edges "
            "WHERE status = 'active' "
            "  AND ((src = ? AND dst = ?) OR (src = ? AND dst = ?)) "
            "LIMIT 1",
            (node_id, rid, rid, node_id),
        ).fetchone()
        if edged is None:
            start = max(0, m.start() - 40)
            end = min(len(scannable), m.end() + 40)
            hints.append({
                "referenced_id": rid,
                "body_excerpt": scannable[start:end].strip(),
            })
    return hints


# ---------- ship-edge relation hint (mis-typed related_to on a ship node) ----------

def compute_ship_edge_hint(conn, new_id: int, new_kind: str) -> list[dict]:
    """Return spec neighbors a progress node links to via `related_to` — almost
    certainly mis-typed ship edges that should be implements/advances/depends_on.

    Deterministic, structural A1 nudge (id=825), keying ONLY on
    (kind(src) == progress, relation == related_to, kind(dst) in spec kinds).
    Scoped to src=progress so legitimate idea<->idea sibling `related_to`
    (e.g. id=1172 <-> id=1149) is never flagged. Closes the chicken-and-egg
    that let id=871's `related_to` ship edge slip past plan_freshness: the
    wrong relation now triggers a correction, and once upgraded the right
    relation unlocks the plan_freshness body-refresh nudge. (id=1194 §4.)

    Empty list when no nudge applies. Cheap — single SQL join, no LLM. The
    spec-kind set is `drift.DRIFT_SCAN_KINDS` (single source of truth, shared
    with orphan_hint + the nightly sweep).
    """
    if new_kind != "progress":
        return []
    from drift import DRIFT_SCAN_KINDS
    kind_placeholders = ",".join("?" for _ in DRIFT_SCAN_KINDS)
    rows = conn.execute(
        f"SELECT e.dst AS linked_id, n.kind AS kind, n.title AS title "
        f"FROM edges e JOIN nodes n ON n.id = e.dst "
        f"WHERE e.src = ? "
        f"  AND e.status = 'active' "
        f"  AND e.relation = 'related_to' "
        f"  AND n.kind IN ({kind_placeholders}) "
        f"  AND COALESCE(n.status, '') != 'stale' "
        f"ORDER BY e.dst ASC",
        (new_id, *DRIFT_SCAN_KINDS),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- single insert entry point ----------

def _new_node_repo_scope(artifacts, project_path) -> frozenset:
    """Repo-scope EVIDENCE for a not-yet-inserted node: the repos named by
    explicit `artifacts` if any, else the coarse `project_path` stamp — mirrors
    artifact_store.capture_for_node so inline heal sees the SAME scope that will
    be attached right after insert. Evidence only; never blocks the insert."""
    repos = set()
    for a in artifacts or []:
        repo, _ = artifact_store._coerce(a)
        if repo and str(repo).strip():
            repos.add(artifact_store.canonicalize_repo(repo))
    if not repos and project_path and str(project_path).strip():
        repos.add(artifact_store.canonicalize_repo(project_path))
    return frozenset(repos)


def insert_with_heal(
    conn,
    *,
    kind: str,
    title: str,
    body: str,
    status: str = "staging",
    session_id: str | None = None,
    links: list[dict] | None = None,
    use_llm: bool = True,
    threshold: float = SIMILARITY_THRESHOLD,
    workstream_id: int | None = None,
    project_path: str | None = None,
    artifacts=None,
) -> dict:
    """Insert a new node, running the on-insert heal against near-duplicates.

    Returns:
      {
        "id": <new node id>,
        "heal": "none" | "keep_both" | "supersede",
        "matched_id": <old id if heal != "none", else None>,
        "similarity": <float if heal != "none", else None>,
        "arbitrator": <arbitrator reason if LLM was consulted, else None>,
        "plan_freshness_hint": [<{linked_id, relation, kind, title}>, ...]
            — non-empty when this is a `progress` node linking to a plan-shaped
            node via implements/advances/depends_on. The agent should follow
            up with kb_update on each listed linked_id. Empty list otherwise.
        "orphan_hint": [<{referenced_id, body_excerpt}>, ...]
            — body `id=X` mentions with no active edge to/from the new node.
            The agent should kb_link each (or drop the mention). Empty
            otherwise. See compute_orphan_hint (id=1149 Part 2). Kind-scoped to
            spec kinds (idea/open_question/decision) per id=1194 §1/§2.
        "ship_edge_hint": [<{linked_id, kind, title}>, ...]
            — non-empty when this is a `progress` node linking to a spec node
            (idea/open_question/decision) via `related_to`: a likely mis-typed
            ship edge that should be implements/advances/depends_on so
            plan-freshness can track it. See compute_ship_edge_hint (id=1194 §4).
      }
    """
    t0 = time.perf_counter()
    vec = embeddings.embed(f"{title}\n\n{body}")

    candidates = find_near_duplicates(
        conn, vec, kind=kind, threshold=threshold,
    )

    new_id = db.insert_node(
        conn, kind=kind, title=title, body=body, status=status,
        session_id=session_id, embedding=embeddings.to_blob(vec),
        workstream_id=workstream_id,
    )
    for link in links or []:
        try:
            db.add_edge(
                conn, src=new_id, dst=int(link["dst"]),
                relation=str(link["relation"]),
                project_path=project_path, session_id=session_id,
            )
        except (KeyError, ValueError, TypeError):
            continue

    plan_hint = compute_plan_freshness_hint(conn, new_id, kind)
    orphan_hint = compute_orphan_hint(conn, new_id, body, kind)
    ship_edge_hint = compute_ship_edge_hint(conn, new_id, kind)

    if not candidates:
        # No emission — heal did not fire (KB id=1095).
        return {"id": new_id, "heal": "none", "matched_id": None,
                "similarity": None, "arbitrator": None,
                "plan_freshness_hint": plan_hint, "orphan_hint": orphan_hint, "ship_edge_hint": ship_edge_hint}

    top = candidates[0]
    top_sim = top["similarity"]
    # POINT-IN-TIME capture (KB id=1091 §4 + id=1095 capture-before-mutation
    # rule). apply_supersede mutates top.status -> 'stale', so we MUST snapshot
    # the pre-arbitration status before any of the apply_* branches runs.
    matched_status_before = top["status"]
    matched_kind = top["kind"]
    matched_id = top["id"]

    def _emit(decision: str) -> None:
        log_utils.emit_event(
            "heal",
            {
                "inserted_node_id": new_id,
                "inserted_kind": kind,
                "matched_id": matched_id,
                "matched_kind": matched_kind,
                "matched_status_before": matched_status_before,
                "similarity": top_sim,
                "arbitrator_decision": decision,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            },
            project_path=project_path,
            session_id=session_id,
        )

    if not use_llm:
        # Conservative path (compactor, offline ingest): don't pay a second
        # LLM call; just link and defer to nightly heal.
        apply_keep_both(conn, new_id, matched_id)
        _emit("keep_both")
        return {"id": new_id, "heal": "keep_both", "matched_id": matched_id,
                "similarity": top_sim, "arbitrator": None,
                "plan_freshness_hint": plan_hint, "orphan_hint": orphan_hint, "ship_edge_hint": ship_edge_hint}

    # Budget gate for the on-insert LLM arbitrator. Charged to the non-heal
    # bucket — kb_insert is user-driven and shouldn't compete with the nightly
    # heal fan-out cap. project_path=None skips the gate (tests, back-compat).
    if project_path is not None:
        allowed, _ = budget.check_and_record(project_path, category="nonheal")
        if not allowed:
            apply_keep_both(conn, new_id, matched_id)
            _emit("keep_both")
            return {"id": new_id, "heal": "keep_both", "matched_id": matched_id,
                    "similarity": top_sim,
                    "arbitrator": "budget cap hit (non-heal); deferred to nightly",
                    "plan_freshness_hint": plan_hint, "orphan_hint": orphan_hint, "ship_edge_hint": ship_edge_hint}

    # Evidence contract: give the arbitrator the new + matched repo scopes so it
    # treats provenance as evidence (prefer keep_both/reconciled across disjoint
    # worlds). Computed BEFORE the call — the new node's provenance is attached by
    # the caller only after this returns, so we derive it from the intended
    # artifacts/project_path here.
    new_repos = _new_node_repo_scope(artifacts, project_path)
    old_repos = artifact_store.node_repo_scope(conn, matched_id)
    verdict = arbitrate(
        {"kind": kind, "title": title, "body": body}, top, top_sim,
        new_repos=new_repos, old_repos=old_repos,
    )
    if verdict["decision"] == "supersede":
        apply_supersede(
            conn, new_id, matched_id,
            project_path=project_path, session_id=session_id,
        )
        _emit("supersede")
        return {"id": new_id, "heal": "supersede", "matched_id": matched_id,
                "similarity": top_sim, "arbitrator": verdict["reason"],
                "plan_freshness_hint": plan_hint, "orphan_hint": orphan_hint, "ship_edge_hint": ship_edge_hint}

    apply_keep_both(conn, new_id, matched_id)
    _emit("keep_both")
    return {"id": new_id, "heal": "keep_both", "matched_id": matched_id,
            "similarity": top_sim, "arbitrator": verdict["reason"],
            "plan_freshness_hint": plan_hint, "orphan_hint": orphan_hint, "ship_edge_hint": ship_edge_hint}


# ---------- nightly integrity pass ----------

def run_integrity_pass(conn) -> dict:
    """Scan for and repair common bitrot:

    * orphan edges — src or dst no longer exist (should be rare with FK CASCADE,
      but catches anything created before FKs were enforced or with FKs off).
    * vec_nodes rows without a matching nodes row (delete).
    * nodes with an embedding blob but no vec_nodes row (backfill, when loaded).
    """
    summary = {"orphan_edges_deleted": 0, "vec_orphans_deleted": 0, "vec_backfilled": 0}

    cur = conn.execute(
        "DELETE FROM edges WHERE src NOT IN (SELECT id FROM nodes) "
        "OR dst NOT IN (SELECT id FROM nodes)"
    )
    summary["orphan_edges_deleted"] = cur.rowcount or 0
    conn.commit()

    if db.vec_loaded(conn):
        try:
            cur = conn.execute(
                "DELETE FROM vec_nodes WHERE rowid NOT IN (SELECT id FROM nodes)"
            )
            summary["vec_orphans_deleted"] = cur.rowcount or 0
            missing = conn.execute(
                "SELECT id, embedding FROM nodes "
                "WHERE embedding IS NOT NULL "
                "AND id NOT IN (SELECT rowid FROM vec_nodes)"
            ).fetchall()
            for row in missing:
                conn.execute(
                    "INSERT INTO vec_nodes(rowid, embedding) VALUES (?, ?)",
                    (row["id"], row["embedding"]),
                )
            summary["vec_backfilled"] = len(missing)
            conn.commit()
        except Exception as e:
            _log(f"integrity vec pass failed (non-fatal): {e}")

    return summary


# ---------- nightly contradiction sweep ----------

def nightly_heal(
    conn,
    project_path: str | None = None,
    *,
    use_llm: bool = True,
    integrity: bool = True,
    contradictions: bool = True,
    high_threshold: float = NIGHTLY_SIMILARITY_THRESHOLD,
    low_threshold: float = LOW_TIER_SIMILARITY_THRESHOLD,
    top_k: int = NIGHTLY_TOP_K,
) -> dict:
    """Nightly healer. Three phases:

    1. Integrity (optional): orphan cleanup + vec_nodes sync.
    2. Contradiction sweep (two-tier, id=871; two-pass dispatch, id=950):
         * Discovery pass: walk all candidates, build pair list filtered for
           stale + edge-exists. Each pair tagged with tier.
         * Arbitration pass: process high-tier pairs first (guaranteed budget
           access for near-duplicates), then low-tier pairs with remaining
           budget. Within a tier, higher similarity first.
         * High tier (sim >= `high_threshold`): full three-pass arbitration
           (recency → ref_count → LLM), decision in {supersede, keep_both,
           reconciled_by}.
         * Low tier (`low_threshold` <= sim < `high_threshold`): LLM-only,
           decision in {reconciled_by, keep_both} (skip deterministic passes —
           recency/ref_count are duplicate signals, not reconciliation
           signals).
       Pairs are skipped when an edge already exists between them OR the
       match is already stale.
    3. Summary.

    LLM calls (the only expensive step) are gated on the same daily budget the
    compactor uses. If the cap is hit mid-sweep, remaining LLM-bound collisions
    fall back to keep_both. `budget_blocked_by_tier` surfaces which tier the
    cap was hit on.

    Idempotent: re-running immediately after should do ~nothing because edges
    from the first run short-circuit the sweep on subsequent collisions.
    """
    if paths.is_disabled():
        return {"ok": False, "reason": "disabled"}

    summary: dict = {
        "ok": True,
        "integrity": None,
        "examined": 0,
        "collisions": 0,
        "skipped_edge_exists": 0,
        "skipped_stale": 0,
        # Summaries are tree-managed roll-ups (near-duplicates of their members by
        # construction); they are NOT contradiction candidates. Excluded on both
        # sides — seed query below + the pair-admission guard in the arbitration
        # loop. See id=1699/id=1797.
        "skipped_summary": 0,
        "superseded": 0,
        "kept_both": 0,
        "reconciled": 0,
        "by_path": {"recency": 0, "ref_count": 0, "llm": 0, "skip": 0},
        "by_tier": {"high": 0, "low": 0},
        "llm_invocations": 0,
        "budget_blocked": 0,
        "budget_blocked_by_tier": {"high": 0, "low": 0},
        # Evidence contract: high-tier pairs whose disjoint artifact scopes caused
        # the deterministic recency/ref_count supersede to be deferred to the LLM.
        "cross_scope_deferred": 0,
    }

    if integrity:
        summary["integrity"] = run_integrity_pass(conn)
        _debug(f"integrity pass: {summary['integrity']}")

    if not contradictions:
        return summary

    candidates = conn.execute(
        "SELECT id, kind, title, body, status, ref_count, created_at, updated_at, "
        "       last_referenced_at, embedding "
        "FROM nodes WHERE status != 'stale' AND embedding IS NOT NULL "
        "  AND kind != 'summary' "
        "ORDER BY id"
    ).fetchall()
    _debug(f"contradiction sweep: {len(candidates)} candidates, "
           f"low_threshold={low_threshold}, high_threshold={high_threshold}, "
           f"top_k={top_k}, use_llm={use_llm}")

    # ---------- Discovery pass: build pair list across all candidates ----------
    seen_pairs: set[tuple[int, int]] = set()
    pair_list: list[tuple[float, str, int, int]] = []  # (sim, tier, a_id, b_id)

    for row in candidates:
        a_id = row["id"]
        a = db.get_node(conn, a_id)
        if not a or a["status"] == "stale":
            continue
        summary["examined"] += 1
        vec = np.frombuffer(row["embedding"], dtype=np.float32)

        near = find_near_duplicates(
            conn, vec, exclude_id=a_id, threshold=low_threshold, top_k=top_k,
        )
        for cand in near:
            b_id = cand["id"]
            pair = (min(a_id, b_id), max(a_id, b_id))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            summary["collisions"] += 1

            b = db.get_node(conn, b_id)
            if not b or b["status"] == "stale":
                summary["skipped_stale"] += 1
                _debug(f"  pair ({a_id},{b_id}) sim={cand['similarity']:.3f} "
                       f"SKIP: b stale")
                continue
            if edge_exists_between(conn, a_id, b_id):
                summary["skipped_edge_exists"] += 1
                _debug(f"  pair ({a_id},{b_id}) sim={cand['similarity']:.3f} "
                       f"SKIP: edge already exists")
                continue

            sim = cand["similarity"]
            tier = "high" if sim >= high_threshold else "low"
            summary["by_tier"][tier] += 1
            pair_list.append((sim, tier, a_id, b_id))

    # ---------- Arbitration pass: high-tier first, then low-tier ----------
    # Sort: high-tier pairs before low-tier; within a tier, higher sim first.
    pair_list.sort(key=lambda p: (0 if p[1] == "high" else 1, -p[0]))

    _debug(f"arbitration pass: {len(pair_list)} pairs queued "
           f"(high={summary['by_tier']['high']}, low={summary['by_tier']['low']})")

    # Per-node repo-scope memo for the evidence-contract guard. Plain dict,
    # populated lazily by artifact_store.node_repo_scope so each node's
    # node_artifact lookup happens at most once across the whole pass.
    scope_cache: dict = {}

    for sim, tier, a_id, b_id in pair_list:
        # Re-fetch — an earlier arbitration in this pass may have marked one
        # of these stale, or added an edge between them, via cascade.
        a = db.get_node(conn, a_id)
        b = db.get_node(conn, b_id)
        if not a or a["status"] == "stale" or not b or b["status"] == "stale":
            summary["skipped_stale"] += 1
            _debug(f"  pair ({a_id},{b_id}) sim={sim:.3f} tier={tier} "
                   f"SKIP: became stale during arbitration")
            continue
        # Load-bearing rail (id=1699/id=1797): a tree summary is a near-duplicate
        # of its own members by construction, so it collides at the sweep
        # threshold and the recency pass would let the fresh summary supersede the
        # older source node. Summaries are tree-managed, never contradiction
        # candidates. The seed query excludes summaries as `a`; this catches a
        # summary returned as `b` by find_near_duplicates(kind=None).
        if a["kind"] == "summary" or b["kind"] == "summary":
            summary["skipped_summary"] += 1
            _debug(f"  pair ({a_id},{b_id}) sim={sim:.3f} tier={tier} "
                   f"SKIP: summary node not eligible for contradiction arbitration")
            continue
        if edge_exists_between(conn, a_id, b_id):
            summary["skipped_edge_exists"] += 1
            _debug(f"  pair ({a_id},{b_id}) sim={sim:.3f} tier={tier} "
                   f"SKIP: edge added during arbitration")
            continue

        _debug(f"  pair ({a_id},{b_id}) sim={sim:.3f} tier={tier} "
               f"a=({a['kind']!r} {a['title']!r} ref={a.get('ref_count',0)}) "
               f"b=({b['kind']!r} {b['title']!r} ref={b.get('ref_count',0)})")

        # Artifact repo-scope evidence for the cross-scope guard (cached). Empty
        # for the scopeless majority → three_pass_arbitrate behaves exactly as
        # before. Evidence, not law: discovery already collided this pair.
        a_repos = artifact_store.node_repo_scope(conn, a_id, cache=scope_cache)
        b_repos = artifact_store.node_repo_scope(conn, b_id, cache=scope_cache)
        if (tier == "high" and a_repos and b_repos and not (a_repos & b_repos)
                and (_pick_by_recency(a, b) is not None
                     or _pick_by_ref_count(a, b) is not None)):
            # Count only ACTUAL deferrals: a deterministic recency/ref_count
            # supersede that the disjoint-scope guard suppressed (→ LLM/keep_both).
            # Without the would-supersede check this over-counts every disjoint
            # high-tier pair, most of which the deterministic passes skip anyway.
            summary["cross_scope_deferred"] += 1
            _debug(f"    cross-scope disjoint {sorted(a_repos)} vs {sorted(b_repos)}"
                   f" — deferring deterministic supersede to LLM")

        # High tier: try deterministic passes first (cheap, no LLM cost).
        if tier == "high":
            tentative = three_pass_arbitrate(
                a, b, similarity=sim, use_llm=False, tier="high",
                a_repos=a_repos, b_repos=b_repos,
            )
            if tentative["path"] != "skip":
                _apply_verdict(conn, summary, tentative, a_id, b_id, project_path=project_path)
                continue

        # LLM path: high-tier inconclusive OR low-tier (always).
        if not use_llm:
            verdict = {
                "decision": "keep_both",
                "winner_id": None, "loser_id": None,
                "older_id": None, "newer_id": None,
                "path": "skip", "tier": tier,
                "reason": "LLM disabled",
            }
            _apply_verdict(conn, summary, verdict, a_id, b_id, project_path=project_path)
            continue

        allowed, _ = budget.check_and_record(project_path, category="heal")
        if not allowed:
            summary["budget_blocked"] += 1
            summary["budget_blocked_by_tier"][tier] += 1
            apply_keep_both(conn, a_id, b_id)
            summary["by_path"]["skip"] += 1
            summary["kept_both"] += 1
            _debug(f"    -> keep_both (heal budget cap hit, tier={tier})")
            continue

        summary["llm_invocations"] += 1
        _debug(f"    invoking LLM arbitrator (tier={tier})")
        verdict = three_pass_arbitrate(
            a, b, similarity=sim, use_llm=True, tier=tier,
            a_repos=a_repos, b_repos=b_repos,
        )
        _apply_verdict(conn, summary, verdict, a_id, b_id)

    try:
        summary["log_retention"] = log_utils.maintain_log_retention(project_path)
    except Exception as e:
        _debug(f"log_retention failed: {e}")
        summary["log_retention"] = {"error": str(e)}

    try:
        today = datetime.now(timezone.utc).date()
        summary["correlator"] = correlator.correlate(
            project_path, today - timedelta(days=1), today,
        )
    except Exception as e:
        _debug(f"correlator failed: {e}")
        summary["correlator"] = {"error": str(e)}

    # Deterministic body-edge / state drift sweep (id=1149 Part 3). No LLM;
    # surface-only. Lazy import avoids a heal<->drift cycle (drift reuses
    # heal's code-span helpers).
    try:
        import drift
        summary["drift"] = drift.sweep(conn, project_path)
    except Exception as e:
        _debug(f"drift sweep failed: {e}")
        summary["drift"] = {"error": str(e)}

    _debug(f"sweep complete: {summary}")
    return summary


def _apply_verdict(
    conn, summary: dict, verdict: dict, a_id: int, b_id: int,
    *, project_path: str | None = None,
) -> None:
    """Apply the verdict's DB mutation and bump the matching counters."""
    summary["by_path"][verdict["path"]] = summary["by_path"].get(verdict["path"], 0) + 1
    if verdict["decision"] == "supersede":
        apply_nightly_supersede(
            conn, verdict["winner_id"], verdict["loser_id"],
            project_path=project_path,
        )
        summary["superseded"] += 1
        _debug(f"    -> supersede via {verdict['path']} (tier={verdict.get('tier')}): "
               f"winner={verdict['winner_id']} loser={verdict['loser_id']} "
               f"reason={verdict.get('reason','')!r}")
    elif verdict["decision"] == "reconciled_by":
        apply_nightly_reconciled_by(
            conn, verdict["older_id"], verdict["newer_id"],
            project_path=project_path,
        )
        summary["reconciled"] += 1
        _debug(f"    -> reconciled_by via {verdict['path']} (tier={verdict.get('tier')}): "
               f"older={verdict['older_id']} newer={verdict['newer_id']} "
               f"reason={verdict.get('reason','')!r}")
    else:
        apply_keep_both(conn, a_id, b_id)
        summary["kept_both"] += 1
        _debug(f"    -> keep_both via {verdict['path']} (tier={verdict.get('tier')}): "
               f"reason={verdict.get('reason','')!r}")


# ---------- logging ----------

def _log(msg: str) -> None:
    log_path = paths.KB_ROOT / "heal.log"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


def _debug(msg: str) -> None:
    """Per-decision debug log. No-op unless CLAUDE_KB_DEBUG_LOG points at a file.
    Set that env var before invoking a maintenance pass (e.g. `python
    src/selfheal.py <project>`) to capture verbose per-decision logging."""
    path = os.environ.get("CLAUDE_KB_DEBUG_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass
