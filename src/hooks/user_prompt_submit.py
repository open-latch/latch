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
JSONL retrieval log lives in the selected KB directory as ``retrieve.log``;
every prompt writes one line so SIM_FLOOR / TOPIC_SAME_THRESHOLD / depth regex
can be empirically tuned after ~100 prompts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

from _common import hook_field, log, project_cwd, read_hook_input, session_id

import mcp_broker
from paths import (
    UNLATCHED_MESSAGE,
    is_disabled,
    is_in_compact,
    is_unlatched_mode,
    latch_intensity,
    db_path,
)


_PROCESS_STARTED = time.perf_counter()

TOP_K = 5
MAX_INJECT = 5
SIM_FLOOR = 0.55
STANDARD_MAX_INJECT = 3
STANDARD_SIM_FLOOR = 0.60
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
# Windows process/DLL startup is materially slower than POSIX on the supported
# hosts. Reserve it from the user-visible wall instead of letting the in-hook
# retrieval deadline consume the entire 250 ms contract.
SUBPROCESS_BUDGET_RESERVE_MS = 125 if os.name == "nt" else 25
# Mission-control and cite-nudge safety reads may each encounter the writer.
# Fifty milliseconds tolerates ordinary short transactions while bounding the
# two-read worst case to 100 ms inside the prompt hook's 250 ms wall.
LIGHT_DB_BUSY_TIMEOUT_SECONDS = 0.05
LOG_STREAM = "retrieve"

np = None
db = None
embeddings = None
log_utils = None
profiles = None
search = None
_LIGHT_RUNTIME_LOADED = False
_RUNTIME_LOADED = False


def _load_log_runtime() -> None:
    global log_utils
    if log_utils is None:
        import log_utils as _log_utils
        log_utils = _log_utils


def _load_light_runtime() -> None:
    global db, log_utils, profiles, _LIGHT_RUNTIME_LOADED
    if _LIGHT_RUNTIME_LOADED:
        return
    import db as _db
    import profiles as _profiles

    _load_log_runtime()
    db = _db
    profiles = _profiles
    _LIGHT_RUNTIME_LOADED = True


def _load_runtime() -> None:
    global np, embeddings, search, _RUNTIME_LOADED
    if _RUNTIME_LOADED:
        return
    _load_light_runtime()
    import numpy as _np
    import embeddings as _embeddings
    import search as _search

    np = _np
    embeddings = _embeddings
    search = _search
    _RUNTIME_LOADED = True

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
# (latch_priority_add) so the gate weighs it on future in-scope builds. Same
# cheap-regex-backstop pattern as CORRECTION_SIGNAL; the offer is the agent's,
# capture is user-confirmed.
GUIDELINE_SIGNAL = re.compile(
    r"\b(from now on|going forward(?:s)?|always|never|make sure(?:\s+to|\s+that)?|"
    r"be sure to|as a rule|in general|by default|every time|"
    r"whenever\s+(?:you|we)|don'?t ever|top of mind|standing (?:rule|guideline))\b",
    re.IGNORECASE,
)


def main() -> int:
    if is_unlatched_mode():
        _print_context(UNLATCHED_MESSAGE)
        return 0
    if is_disabled() or is_in_compact():
        return 0
    payload = read_hook_input()
    sid = session_id(payload)
    cwd = project_cwd(payload)
    prompt = (hook_field(payload, "prompt", "user_prompt") or "").strip()
    intensity = latch_intensity()

    correction_signal = bool(CORRECTION_SIGNAL.search(prompt))
    guideline_signal = (
        intensity == "full"
        and
        bool(GUIDELINE_SIGNAL.search(prompt))
        and "kb_priority" not in prompt.lower()
        and "latch_priority" not in prompt.lower()
    )

    # Safety/profile nudges are independent of similarity retrieval and must
    # survive an unavailable embedding owner. Their helpers use a lightweight
    # direct SQLite connection, so this does not reintroduce sqlite-vec/model
    # startup on the degraded path.
    mc_directive = _mission_control_directive(cwd, prompt)
    cite_count = _take_cite_nudge(cwd, sid) if sid else 0
    cite_directive = (
        profiles.render_cite_correction_directive(cite_count) if cite_count else ""
    )

    # On the ordinary post-idle path, return before retrieval runtime loading. A
    # Windows sqlite-vec DLL load can exceed the entire visible hook budget,
    # while there is no vector to retrieve with until the owner is warm anyway.
    if (
        sid
        and prompt
        and len(prompt.split()) >= MIN_PROMPT_WORDS
        and intensity != "quiet"
        and mcp_broker.read_discovery() is None
    ):
        log_entry = {
            "mission_control": bool(mc_directive),
            "intensity": intensity,
            "ts": _now(),
            "sid": sid,
            "prompt_hash": _phash(prompt),
            "prompt_words": len(prompt.split()),
            "cwd": cwd,
            "correction_signal": correction_signal,
            "guideline_signal": guideline_signal,
            "cite_nudge": cite_count,
            "skip": "embed_daemon_unavailable",
        }
        log_entry["daemon_wake_requested"] = mcp_broker.request_daemon_start(cwd)
        mcp_broker.emit_lifecycle(
            "prompt_retrieval_degraded",
            reason="embed_daemon_unavailable",
            wake_requested=bool(log_entry["daemon_wake_requested"]),
        )
        context = _format_runtime_unavailable(intensity)
        nudge = _extra_nudges(
            correction_signal, guideline_signal, mc_directive, cite_directive,
        )
        if nudge:
            context = nudge + "\n\n" + context
        _emit_and_log(cwd, log_entry, context)
        return 0

    # Slice 3-B: surface the advisory cite-correction nudge queued by last turn's
    # Stop-hook detector (mission-control actors only; marker is 0 for everyone
    # else). Consumed (read + reset) here regardless of the current prompt — it
    # is about the PRIOR turn, so it fires even on short prompts like "ok thanks".
    log_entry: dict = {
        "mission_control": bool(mc_directive),
        "intensity": intensity,
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
        nudge = _extra_nudges(correction_signal, guideline_signal, mc_directive, cite_directive)
        _emit_and_log(cwd, log_entry, nudge)
        return 0
    if not prompt or len(prompt.split()) < MIN_PROMPT_WORDS:
        log_entry["skip"] = "prompt_too_short"
        nudge = _extra_nudges(correction_signal, guideline_signal, mc_directive, cite_directive)
        _emit_and_log(cwd, log_entry, nudge)
        return 0

    # Quiet keeps the correction/profile/citation safety surfaces but performs
    # no per-prompt retrieval and does not wake the embedding runtime.
    if intensity == "quiet":
        log_entry["skip"] = "intensity_quiet"
        nudge = _extra_nudges(
            correction_signal, guideline_signal, mc_directive, cite_directive,
        )
        _emit_and_log(cwd, log_entry, nudge)
        return 0

    t0 = time.perf_counter()
    try:
        injected = _retrieve_and_inject(
            cwd, sid, prompt, log_entry,
            intensity=intensity,
            deadline=_PROCESS_STARTED + (
                HARD_BUDGET_MS - SUBPROCESS_BUDGET_RESERVE_MS
            ) / 1000.0,
        )
    except Exception as e:
        log_entry["error"] = f"{type(e).__name__}: {e}"
        # Similarity is fail-open, but independent correction/profile/citation
        # safety context must not disappear merely because retrieval failed.
        nudge = _extra_nudges(
            correction_signal, guideline_signal, mc_directive, cite_directive,
        )
        _emit_and_log(cwd, log_entry, nudge)
        log(f"user_prompt_submit error: {e}")
        return 0
    elapsed_ms = (time.perf_counter() - t0) * 1000
    log_entry["elapsed_ms"] = round(elapsed_ms, 1)

    if elapsed_ms > HARD_BUDGET_MS:
        log_entry["overran_budget"] = True
        # Already past budget — still emit the result we computed, since the
        # damage (latency) is already done. Future tuning can decide whether
        # to drop the result instead.

    if log_entry.get("skip") == "embed_daemon_unavailable":
        context = _format_runtime_unavailable(intensity)
    elif log_entry.get("skip") == "standard_same_topic":
        context = ""
    else:
        context = _format_injection(injected, intensity=intensity) if injected else (
            _format_no_hits() if intensity == "full" else ""
        )
    # Include mc_directive + cite_directive on the main path too — previously
    # dropped here, so the mission-control standing contract only surfaced on the
    # short-prompt / no-session early-outs. Both are '' for non-mission-control.
    nudge = _extra_nudges(correction_signal, guideline_signal, mc_directive, cite_directive)
    if nudge:
        context = nudge + ("\n\n" + context if context else "")
    _emit_and_log(cwd, log_entry, context)
    return 0


def _print_context(context: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    print(json.dumps(out))


def _emit_and_log(cwd: str, log_entry: dict, context: str) -> None:
    """Record the exact prompt-context cost, then emit only non-empty context."""
    log_entry["context_chars"] = len(context)
    _write_log(cwd, log_entry)
    if context:
        _print_context(context)


def _should_retrieve_for_intensity(intensity: str, topic_sim: float | None) -> bool:
    """Pure policy seam for table-driven tier tests."""
    if intensity == "quiet":
        return False
    if intensity == "standard":
        return topic_sim is None or topic_sim < TOPIC_SAME_THRESHOLD
    return True


def _select_candidates(
    candidates: list[dict],
    active_set: set[int],
    *,
    sim_floor: float,
    max_inject: int,
) -> list[dict]:
    """Apply the prompt hook's deterministic vector-selection policy.

    Keep this pure: the frozen intensity eval calls the same seam as the live
    vector path, so ranking, kind filtering, active-set dedupe, the similarity
    floor, and the tier cap cannot quietly diverge between them. Equal scores
    preserve the candidate source's order.
    """
    eligible = [
        row for row in candidates
        if row["kind"] not in EXCLUDED_KINDS
        and row["id"] not in active_set
        and float(row["score"]) >= sim_floor
    ]
    eligible.sort(key=lambda row: -float(row["score"]))
    return eligible[:max_inject]


def _retrieve_and_inject(
    cwd: str,
    sid: str,
    prompt: str,
    log_entry: dict,
    *,
    intensity: str = "full",
    deadline: float | None = None,
) -> list[dict]:
    _load_runtime()
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
        deadline = deadline or (time.perf_counter() + HARD_BUDGET_MS / 1000.0)
        qvec = _embed_with_bounded_wake(prompt, cwd, deadline, log_entry)
        if qvec is None:
            log_entry["skip"] = "embed_daemon_unavailable"
            mcp_broker.emit_lifecycle(
                "prompt_retrieval_degraded",
                reason="embed_daemon_unavailable",
                wake_requested=bool(log_entry.get("daemon_wake_requested")),
            )
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

        # Standard volunteers context only at the first prompt or a topic
        # shift. Still store this prompt's embedding so the next comparison is
        # against the immediately preceding turn.
        if not _should_retrieve_for_intensity(intensity, topic_sim):
            db.upsert_session(conn, sid, cwd, None)
            db.update_last_prompt_embedding(conn, sid, qblob)
            log_entry["skip"] = "standard_same_topic"
            return []

        active_set = db.get_active_set(conn, session_id=sid, current_turn=turn)
        log_entry["active_set_size"] = len(active_set)

        injected: list[dict] = []
        if is_drill:
            injected = _graph_path(
                conn, sid, turn, active_set, qvec, log_entry,
                max_inject=(STANDARD_MAX_INJECT if intensity == "standard" else MAX_INJECT),
            )

        if not injected:
            # C1 path (or C2 fallback when traversal returned nothing).
            injected = _vector_path(
                conn,
                sid,
                turn,
                active_set,
                qvec,
                log_entry,
                scope_repo=cwd,
                max_inject=(STANDARD_MAX_INJECT if intensity == "standard" else MAX_INJECT),
                sim_floor=(STANDARD_SIM_FLOOR if intensity == "standard" else SIM_FLOOR),
            )

        # Stash this prompt's embedding for next-turn topic-shift detection.
        # No-op if upsert_session is needed first.
        db.upsert_session(conn, sid, cwd, None)
        db.update_last_prompt_embedding(conn, sid, qblob)

        return injected
    finally:
        conn.close()


def _embed_with_bounded_wake(prompt: str, cwd: str, deadline: float, log_entry: dict):
    """Use the warm owner or request one without exceeding the hook wall."""
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        log_entry["daemon_wake_requested"] = mcp_broker.request_daemon_start(cwd)
        return None

    # Only call the embed endpoint when the MCP owner has published readiness;
    # its embed listener appears before model pre-warm completes.
    if mcp_broker.read_discovery() is not None:
        qvec = embeddings.embed_remote(prompt, cwd, timeout=max(0.005, min(0.05, remaining)))
        if qvec is not None:
            return qvec

    log_entry["daemon_wake_requested"] = mcp_broker.request_daemon_start(cwd)
    # Poll the tiny discovery file, not the warming embed endpoint. Reserve a
    # small tail for one bounded embed RPC if startup completes in time.
    while time.perf_counter() < deadline - 0.06:
        if mcp_broker.read_discovery() is not None:
            remaining = deadline - time.perf_counter()
            return embeddings.embed_remote(
                prompt, cwd, timeout=max(0.005, min(0.05, remaining))
            )
        time.sleep(0.01)
    return None


def _vector_path(
    conn, sid: str, turn: int, active_set: set[int], qvec, log_entry: dict,
    scope_repo: str | None = None, *, max_inject: int = MAX_INJECT,
    sim_floor: float = SIM_FLOOR,
) -> list[dict]:
    _load_runtime()
    raw = search.vector_search(conn, qvec=qvec, limit=TOP_K * 3, scope_repo=scope_repo)
    log_entry["raw_hits"] = [(r["id"], round(r["score"], 3), r["kind"]) for r in raw[:10]]
    chosen = _select_candidates(
        raw,
        active_set,
        sim_floor=sim_floor,
        max_inject=max_inject,
    )
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
        and r["score"] < sim_floor
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
    conn, sid: str, turn: int, active_set: set[int], qvec, log_entry: dict,
    *, max_inject: int = MAX_INJECT,
) -> list[dict]:
    """Surface neighbors of the active node most relevant to the new prompt.

    Re-rank active-set members by similarity to qvec, then pull edges from the
    top one. Yields nodes the agent has likely been about to ask about next."""
    _load_runtime()
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
        SELECT DISTINCT n.id, n.kind, n.title, n.body, n.status, n.workstream_id
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
    chosen = new_neighbors[:max_inject]

    log_entry["path"] = "graph"
    log_entry["injected"] = [(n["id"], round(n["score"], 3)) for n in chosen]
    db.record_retrievals(
        conn, session_id=sid, turn=turn,
        items=[(n["id"], n["score"]) for n in chosen],
        source="graph",
        event_details={
            n["id"]: {
                "seed_node_id": pivot_id,
                "reached_node_id": n["id"],
                "workstream_id_at_event": n.get("workstream_id"),
            }
            for n in chosen
        },
    )
    return chosen


def _format_injection(items: list[dict], *, intensity: str = "full") -> str:
    lines = ["## KB hits (similarity sample — not a result)"]
    for r in items:
        sim = r.get("score", 0.0)
        title = r.get("title", "")
        kind = r.get("kind", "")
        lines.append(f"- ({kind}, id={r['id']}, sim={sim:.2f}) {title}")
    if intensity == "standard":
        lines.append(
            "\n**These are teasers, not an answer.** Standard surfaced them because "
            "this is the first prompt or the topic changed. Fetch the full node with `latch_search` / "
            "`latch_get` / `latch_recent` before relying on it."
        )
    else:
        lines.append(
            "\n**These are teasers, not an answer.** Actively query the KB "
            "(`latch_search` / `latch_get` / `latch_recent`) before responding — every "
            "prompt, no exception. Auto-injection samples relevance; it doesn't "
            "substitute for reading the node."
        )
    lines.append(
        "_Workstreams, ideas, open_questions are surfaced via the SessionStart brief._"
    )
    return "\n".join(lines)


def _format_no_hits() -> str:
    return (
        "## KB hits — no new hits injected\n\n"
        "**Automatic surfacing added no new KB hits.** Candidates may already "
        "be active in this session, be excluded from prompt surfacing, fall "
        "below the similarity floor, or be absent. This does not mean the KB "
        "has nothing. Actively query latch (`latch_search` / `latch_get` / "
        "`latch_recent`) before responding — every prompt, no exception."
    )


def _format_runtime_unavailable(intensity: str = "full") -> str:
    if intensity == "standard":
        # Standard opts into a smaller prompt surface, so keep this degraded
        # receipt to one plain paragraph. Full retains the prominent heading
        # because its contract favors explicit per-prompt retrieval receipts.
        return (
            "Latch Standard could not run this prompt's topic-similarity check "
            "because the shared runtime is starting or unavailable, so it could "
            "not determine whether this prompt qualified for injection. Query "
            "`latch_search` / `latch_get` before relying on project history."
        )
    return (
        "## KB auto-retrieval temporarily unavailable\n\n"
        "The shared latch runtime was idle, starting, or unreachable, so this "
        "prompt was **not similarity-scored**. This is not a below-threshold "
        "result. Latch requested a background wake; actively query "
        "(`latch_search` / `latch_get` / `latch_recent`) before responding."
    )


def _format_correction_nudge() -> str:
    return (
        "## ⚠ Possible KB correction signal\n\n"
        "Your message may be flagging that stored KB info is wrong / stale / "
        "outdated / hallucinated. If so, do NOT freeform-edit node bodies — "
        "follow the structured correction so the decision-change history is "
        "preserved:\n"
        "1. `latch_verify(<id>)` to confirm the suspect node is STALE / RECONCILED / OK.\n"
        "2. `latch_correct_plan(<bad_id>)` for the blast radius + supersede/reconcile recommendation.\n"
        "3. Surface the plan and get explicit user confirmation.\n"
        "4. `latch_correct_apply(...)` — mutation is human-confirmed, never auto-fired.\n\n"
        "If this was not a KB correction, ignore this notice."
    )


def _format_guideline_nudge() -> str:
    return (
        "## Standing-guideline signal (deterministic — not a classifier)\n\n"
        "This prompt reads like a directive meant to shape future work, not just "
        "the current task. If that's the user's intent, **offer** to capture it "
        "as an overall **priority** (`latch_priority_add`) or, when it clearly "
        "belongs only to the active workstream, a workstream **priority** "
        "(`latch_priority_add(..., workstream_id=<id>)`) so latch weighs it in "
        "future in-scope `latch_gate` calls. Capture only with the user's go-ahead; "
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
        _load_light_runtime()
        conn = _open_existing_light_connection(cwd)
        if conn is None:
            return ""
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
        _load_light_runtime()
        conn = _open_existing_light_connection(cwd)
        if conn is None:
            return 0
        try:
            return db.take_pending_cite_nudge(conn, sid)
        finally:
            conn.close()
    except Exception as e:
        log(f"take_pending_cite_nudge error: {e}")
        return 0


def _open_existing_light_connection(cwd: str) -> sqlite3.Connection | None:
    """Open the existing KB without schema migration or sqlite-vec loading."""
    path = db_path(cwd)
    if not path.is_file():
        return None
    conn = sqlite3.connect(str(path), timeout=LIGHT_DB_BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    return conn


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
    _load_log_runtime()
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
