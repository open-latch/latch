"""UserPromptSubmit hook: inject relevant KB nodes as additionalContext.

C1 — dedupe path:
  Embed prompt -> top-K vector hits -> filter excluded kinds -> remove ids
  already in this session's active set -> inject up to MAX_INJECT new nodes
  whose similarity >= SIM_FLOOR. Record everything injected back into
  session_retrievals so the next prompt sees it.

C2 — drill-down path:
  Detect drill-down vs topic-shift via (a) regex on depth keywords and
  (b) cosine(new_prompt_emb, last_prompt_emb) >= TOPIC_SAME_THRESHOLD.
  When drill-down fires, instead of re-retrieving, graph-traverse `edges`
  from the active set's most recently-injected node and surface neighbors
  not yet in the active set. Falls back to C1 if traversal yields nothing.

Hard wall HARD_BUDGET_MS — emits empty stdout + log line on overrun.
JSONL retrieval log lives at projects/<sanitized_cwd>/retrieve.log; every
prompt writes one line so SIM_FLOOR / TOPIC_SAME_THRESHOLD / depth regex
can be empirically tuned after ~100 prompts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from _common import hook_field, log, project_cwd, read_hook_input, session_id

import db
import embeddings
import log_utils
import profiles
import search
from paths import is_disabled, is_in_compact, project_dir


TOP_K = 5
MAX_INJECT = 5
SIM_FLOOR = 0.55
MIN_PROMPT_WORDS = 3
# 'priority' joins the surface-only kinds: priorities are injected via the
# SessionStart brief + the kb_gate ACTIVE PROJECT PRIORITIES block, never as a
# per-prompt similarity teaser (they carry no embedding anyway — defensive).
EXCLUDED_KINDS = {"workstream", "idea", "open_question", "priority", "profile"}
TOPIC_SAME_THRESHOLD = 0.70
DEPTH_KEYWORDS = re.compile(
    r"\b(why|more|detail|details|how|continue|deeper|explain|show|further|elaborate|tell\s+me\s+more)\b",
    re.IGNORECASE,
)
HARD_BUDGET_MS = 250
LOG_STREAM = "retrieve"

# Deterministic correction-signal scan (no LLM, sub-millisecond). When a
# prompt looks like the user is flagging stored KB info as wrong/stale, we
# prepend a nudge toward the structured kb_correct procedure. This is the
# enforcement backstop for the "agent must notice" failure mode (KB id=886);
# the agent-inline classifier is the primary path, this catches the misses.
# False positives are cheap — a dismissible reminder. Spec: KB id=1151.
CORRECTION_SIGNAL = re.compile(
    r"\b(wrong|incorrect|inaccurate|outdated|out[\s-]?of[\s-]?date|stale|"
    r"hallucinat\w*|mistaken|not\s+(?:true|right|correct|accurate)|"
    r"no\s+longer\s+(?:true|right|correct|accurate|valid)|"
    r"isn'?t\s+(?:true|right|correct)|that'?s\s+(?:false|wrong))\b",
    re.IGNORECASE,
)

# Deterministic standing-guideline scan (no LLM, sub-millisecond). When a prompt
# reads like a sweeping/standing directive ("always …", "from now on …"), we
# prepend a nudge offering to capture it as an overall or workstream priority
# (kb_priority_add) so the gate weighs it on future in-scope builds. Same
# cheap-regex-backstop pattern as CORRECTION_SIGNAL; the offer is the agent's,
# capture is user-confirmed.
GUIDELINE_SIGNAL = re.compile(
    r"\b(from now on|going forward(?:s)?|always|never|make sure(?:\s+to|\s+that)?|"
    r"be sure to|as a rule|in general|by default|every time|"
    r"whenever\s+(?:you|we)|don'?t ever|top of mind|standing (?:rule|guideline))\b",
    re.IGNORECASE,
)


def main() -> int:
    if is_disabled() or is_in_compact():
        return 0
    payload = read_hook_input()
    sid = session_id(payload)
    cwd = project_cwd(payload)
    prompt = (hook_field(payload, "prompt", "user_prompt") or "").strip()

    correction_signal = bool(CORRECTION_SIGNAL.search(prompt))
    guideline_signal = (
        bool(GUIDELINE_SIGNAL.search(prompt)) and "kb_priority" not in prompt.lower()
    )
    mc_directive = _mission_control_directive(cwd, prompt)
    # Slice 3-B: surface the advisory cite-correction nudge queued by last turn's
    # Stop-hook detector (mission-control actors only; marker is 0 for everyone
    # else). Consumed (read + reset) here regardless of the current prompt — it
    # is about the PRIOR turn, so it fires even on short prompts like "ok thanks".
    cite_count = _take_cite_nudge(cwd, sid) if sid else 0
    cite_directive = (
        profiles.render_cite_correction_directive(cite_count) if cite_count else ""
    )

    log_entry: dict = {
        "mission_control": bool(mc_directive),
        "ts": _now(),
        "sid": sid,
        "prompt_hash": _phash(prompt),
        "prompt_words": len(prompt.split()),
        "cwd": cwd,
        "correction_signal": correction_signal,
        "guideline_signal": guideline_signal,
        "cite_nudge": cite_count,
    }

    # Cheap early-outs that need no DB or model. The correction nudge is
    # independent of retrieval — emit it even when retrieval is skipped
    # (short prompts like "that's wrong" are exactly the case to catch).
    if not sid:
        log_entry["skip"] = "no_session_id"
        _write_log(cwd, log_entry)
        nudge = _extra_nudges(correction_signal, guideline_signal, mc_directive, cite_directive)
        if nudge:
            _print_context(nudge)
        return 0
    if not prompt or len(prompt.split()) < MIN_PROMPT_WORDS:
        log_entry["skip"] = "prompt_too_short"
        _write_log(cwd, log_entry)
        nudge = _extra_nudges(correction_signal, guideline_signal, mc_directive, cite_directive)
        if nudge:
            _print_context(nudge)
        return 0

    t0 = time.perf_counter()
    try:
        injected = _retrieve_and_inject(cwd, sid, prompt, log_entry)
    except Exception as e:
        log_entry["error"] = f"{type(e).__name__}: {e}"
        _write_log(cwd, log_entry)
        log(f"user_prompt_submit error: {e}")
        return 0
    elapsed_ms = (time.perf_counter() - t0) * 1000
    log_entry["elapsed_ms"] = round(elapsed_ms, 1)

    if elapsed_ms > HARD_BUDGET_MS:
        log_entry["overran_budget"] = True
        # Already past budget — still emit the result we computed, since the
        # damage (latency) is already done. Future tuning can decide whether
        # to drop the result instead.

    _write_log(cwd, log_entry)

    context = _format_injection(injected) if injected else _format_no_hits()
    # Include mc_directive + cite_directive on the main path too — previously
    # dropped here, so the mission-control standing contract only surfaced on the
    # short-prompt / no-session early-outs. Both are '' for non-mission-control.
    nudge = _extra_nudges(correction_signal, guideline_signal, mc_directive, cite_directive)
    if nudge:
        context = nudge + "\n\n" + context
    _print_context(context)
    return 0


def _print_context(context: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    print(json.dumps(out))


def _retrieve_and_inject(cwd: str, sid: str, prompt: str, log_entry: dict) -> list[dict]:
    conn = db.connect(cwd)
    try:
        # Determine current turn — sessions row may not yet exist if the Stop
        # hook is gated off. Default to 0; turns are still meaningful for TTL
        # because TTL just compares to last_injected_turn (also 0).
        sess = db.get_session(conn, sid)
        turn = sess["turn_count"] if sess else 0
        log_entry["turn"] = turn

        # Embed once; reused for retrieval AND topic-shift detection.
        # Talk to the per-session MCP server's embed listener — the local
        # `embeddings.embed()` path costs ~15s of torch cold-load per
        # subprocess, so falling through to it would blow HARD_BUDGET_MS
        # by ~80x. If the daemon is unreachable or still warming, skip
        # retrieval for this turn rather than block the user's prompt.
        qvec = embeddings.embed_remote(prompt, cwd)
        if qvec is None:
            log_entry["skip"] = "embed_daemon_unavailable"
            return []
        qblob = embeddings.to_blob(qvec)

        last_blob = db.get_last_prompt_embedding(conn, sid)
        topic_sim = None
        if last_blob is not None:
            last_vec = np.frombuffer(last_blob, dtype=np.float32)
            if last_vec.shape == qvec.shape:
                topic_sim = float(np.dot(qvec, last_vec))
        depth_match = bool(DEPTH_KEYWORDS.search(prompt))
        is_drill = (
            depth_match
            and topic_sim is not None
            and topic_sim >= TOPIC_SAME_THRESHOLD
        )
        log_entry["topic_sim"] = round(topic_sim, 3) if topic_sim is not None else None
        log_entry["depth_match"] = depth_match
        log_entry["is_drill"] = is_drill

        active_set = db.get_active_set(conn, session_id=sid, current_turn=turn)
        log_entry["active_set_size"] = len(active_set)

        injected: list[dict] = []
        if is_drill:
            injected = _graph_path(conn, sid, turn, active_set, qvec, log_entry)

        if not injected:
            # C1 path (or C2 fallback when traversal returned nothing).
            injected = _vector_path(conn, sid, turn, active_set, qvec, log_entry, scope_repo=cwd)

        # Stash this prompt's embedding for next-turn topic-shift detection.
        # No-op if upsert_session is needed first.
        db.upsert_session(conn, sid, cwd, None)
        db.update_last_prompt_embedding(conn, sid, qblob)

        return injected
    finally:
        conn.close()


def _vector_path(
    conn, sid: str, turn: int, active_set: set[int], qvec, log_entry: dict,
    scope_repo: str | None = None,
) -> list[dict]:
    raw = search.vector_search(conn, qvec=qvec, limit=TOP_K * 3, scope_repo=scope_repo)
    log_entry["raw_hits"] = [(r["id"], round(r["score"], 3), r["kind"]) for r in raw[:10]]
    candidates = [
        r for r in raw
        if r["kind"] not in EXCLUDED_KINDS
        and r["id"] not in active_set
        and r["score"] >= SIM_FLOOR
    ]
    chosen = candidates[:MAX_INJECT]
    log_entry["path"] = "vector"
    log_entry["filtered_out_kind"] = sum(
        1 for r in raw if r["kind"] in EXCLUDED_KINDS
    )
    log_entry["filtered_out_active"] = sum(
        1 for r in raw if r["id"] in active_set
    )
    log_entry["filtered_out_floor"] = sum(
        1 for r in raw
        if r["kind"] not in EXCLUDED_KINDS
        and r["id"] not in active_set
        and r["score"] < SIM_FLOOR
    )
    log_entry["injected"] = [(r["id"], round(r["score"], 3)) for r in chosen]
    if chosen:
        db.record_retrievals(
            conn, session_id=sid, turn=turn,
            items=[(r["id"], r["score"]) for r in chosen],
            source="prompt",
        )
    return chosen


def _graph_path(
    conn, sid: str, turn: int, active_set: set[int], qvec, log_entry: dict
) -> list[dict]:
    """Surface neighbors of the active node most relevant to the new prompt.

    Re-rank active-set members by similarity to qvec, then pull edges from the
    top one. Yields nodes the agent has likely been about to ask about next."""
    if not active_set:
        log_entry["graph_skip"] = "empty_active"
        return []
    placeholders = ",".join("?" for _ in active_set)
    rows = conn.execute(
        f"SELECT id, embedding FROM nodes WHERE id IN ({placeholders}) "
        f"AND embedding IS NOT NULL",
        list(active_set),
    ).fetchall()
    if not rows:
        log_entry["graph_skip"] = "no_embeddings_in_active"
        return []
    mat = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    sims = mat @ qvec
    pivot_idx = int(np.argmax(sims))
    pivot_id = rows[pivot_idx]["id"]
    pivot_sim = float(sims[pivot_idx])
    log_entry["graph_pivot"] = pivot_id
    log_entry["graph_pivot_sim"] = round(pivot_sim, 3)

    neighbor_rows = conn.execute(
        """
        SELECT DISTINCT n.id, n.kind, n.title, n.body, n.status
        FROM edges e
        JOIN nodes n ON n.id = CASE WHEN e.src = ? THEN e.dst ELSE e.src END
        WHERE (e.src = ? OR e.dst = ?)
          AND e.status = 'active'
          AND n.status != 'stale'
        """,
        (pivot_id, pivot_id, pivot_id),
    ).fetchall()
    new_neighbors = [
        dict(r) for r in neighbor_rows
        if r["id"] not in active_set and r["kind"] not in EXCLUDED_KINDS
    ]
    log_entry["graph_neighbors_total"] = len(neighbor_rows)
    log_entry["graph_neighbors_new"] = len(new_neighbors)
    if not new_neighbors:
        return []

    # Re-rank neighbors by similarity to qvec for stable ordering.
    neighbor_ids = [n["id"] for n in new_neighbors]
    nrows = conn.execute(
        f"SELECT id, embedding FROM nodes WHERE id IN ({','.join('?' for _ in neighbor_ids)}) "
        f"AND embedding IS NOT NULL",
        neighbor_ids,
    ).fetchall()
    score_by_id = {}
    if nrows:
        nmat = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in nrows])
        nscore = nmat @ qvec
        for r, s in zip(nrows, nscore):
            score_by_id[r["id"]] = float(s)
    for n in new_neighbors:
        n["score"] = score_by_id.get(n["id"], 0.0)
    new_neighbors.sort(key=lambda n: -n["score"])
    chosen = new_neighbors[:MAX_INJECT]

    log_entry["path"] = "graph"
    log_entry["injected"] = [(n["id"], round(n["score"], 3)) for n in chosen]
    db.record_retrievals(
        conn, session_id=sid, turn=turn,
        items=[(n["id"], n["score"]) for n in chosen],
        source="graph",
    )
    return chosen


def _format_injection(items: list[dict]) -> str:
    lines = ["## KB hits (similarity sample — not a result)"]
    for r in items:
        sim = r.get("score", 0.0)
        title = r.get("title", "")
        kind = r.get("kind", "")
        lines.append(f"- ({kind}, id={r['id']}, sim={sim:.2f}) {title}")
    lines.append(
        "\n**These are teasers, not an answer.** Actively query the KB "
        "(`kb_search` / `kb_get` / `kb_recent`) before responding — every "
        "prompt, no exception. Auto-injection samples relevance; it doesn't "
        "substitute for reading the node."
    )
    lines.append(
        "_Workstreams, ideas, open_questions are surfaced via the SessionStart brief._"
    )
    return "\n".join(lines)


def _format_no_hits() -> str:
    return (
        "## KB hits — none auto-retrieved (sim below floor)\n\n"
        "**Auto-retrieval found nothing above SIM_FLOOR.** That doesn't mean "
        "the KB has nothing — it means similarity scoring missed. Actively "
        "query the KB (`kb_search` / `kb_get` / `kb_recent`) before "
        "responding — every prompt, no exception."
    )


def _format_correction_nudge() -> str:
    return (
        "## ⚠ Possible KB correction signal\n\n"
        "Your message may be flagging that stored KB info is wrong / stale / "
        "outdated / hallucinated. If so, do NOT freeform-edit node bodies — "
        "follow the structured correction so the decision-change history is "
        "preserved:\n"
        "1. `kb_verify(<id>)` to confirm the suspect node is STALE / RECONCILED / OK.\n"
        "2. `kb_correct_plan(<bad_id>)` for the blast radius + supersede/reconcile recommendation.\n"
        "3. Surface the plan and get explicit user confirmation.\n"
        "4. `kb_correct_apply(...)` — mutation is human-confirmed, never auto-fired.\n\n"
        "If this was not a KB correction, ignore this notice."
    )


def _format_guideline_nudge() -> str:
    return (
        "## Standing-guideline signal (deterministic — not a classifier)\n\n"
        "This prompt reads like a directive meant to shape future work, not just "
        "the current task. If that's the user's intent, **offer** to capture it "
        "as an overall **priority** (`kb_priority_add`) or, when it clearly "
        "belongs only to the active workstream, a workstream **priority** "
        "(`kb_priority_add(..., workstream_id=<id>)`) so latch weighs it in "
        "future in-scope `kb_gate` calls. Capture only with the user's go-ahead; "
        "skip if it's task-local."
    )


# EXPERIMENTAL — mission-control / verification profiles. NOT recommended for use;
# planned to be unshipped to a separate branch later (observed unhelpful on
# pmeyer's workspace, 2026-06-10). See KB decision id=1550. Don't rely on / extend.
def _mission_control_directive(cwd: str, prompt: str) -> str:
    """Standing mission-control verification contract, injected when the resolved
    actor is bound to a profile with gate_surface='all_moves'. Tailored to the
    deterministic move-type of `prompt`; empty for everyone else (unbound actors
    / trust-and-go). Fail-open: any error -> '' so the hook never breaks the
    user's prompt. The Tier-2 enforcement surface for 'blocking by contract' —
    latch has no interceptor (KB id=1398)."""
    try:
        conn = db.connect(cwd)
        try:
            return profiles.mission_control_directive(conn, prompt)
        finally:
            conn.close()
    except Exception as e:
        log(f"mission_control_directive error: {e}")
        return ""


def _take_cite_nudge(cwd: str, sid: str) -> int:
    """Read + reset the pending cite-nudge marker for this session (Slice 3-B).
    Fail-open: any error -> 0 so the hook never breaks the user's prompt. Cheap:
    a single indexed read, and a write only when a nudge was actually queued."""
    try:
        conn = db.connect(cwd)
        try:
            return db.take_pending_cite_nudge(conn, sid)
        finally:
            conn.close()
    except Exception as e:
        log(f"take_pending_cite_nudge error: {e}")
        return 0


def _extra_nudges(
    correction_signal: bool, guideline_signal: bool,
    mc_directive: str = "", cite_directive: str = "",
) -> str:
    """Concatenate deterministic prompt-signal nudges. The mission-control
    directive leads (it is the standing verification contract), then the
    cite-presence correction (a verification follow-up on the prior turn), then
    correction, then standing-guideline. Empty string when none fire."""
    parts = []
    if mc_directive:
        parts.append(mc_directive)
    if cite_directive:
        parts.append(cite_directive)
    if correction_signal:
        parts.append(_format_correction_nudge())
    if guideline_signal:
        parts.append(_format_guideline_nudge())
    return "\n\n".join(parts)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _phash(prompt: str) -> str:
    return hashlib.sha1(prompt.encode("utf-8", errors="replace")).hexdigest()[:12]


def _write_log(cwd: str, entry: dict) -> None:
    """Emit one JSONL row to the daily retrieve log (KB id=1091 conventions).

    The legacy `sid` field on the entry is left in place for back-compat;
    `emit_event` also adds the canonical `session_id` header field from the
    explicit kwarg. Both keys end up in the row — readers should prefer
    `session_id` going forward.
    """
    try:
        log_utils.emit_event(
            LOG_STREAM, entry,
            project_path=cwd,
            session_id=entry.get("sid"),
        )
    except Exception as e:
        log(f"retrieve.log write failed: {e}")


if __name__ == "__main__":
    sys.exit(main())
