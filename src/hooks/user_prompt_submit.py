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

from _common import (
    hook_field, log, project_cwd, read_hook_input, session_id, transcript_path,
)

import mcp_broker
from paths import UNLATCHED_MESSAGE, is_disabled, is_in_compact, is_unlatched_mode


_PROCESS_STARTED = time.perf_counter()

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
# Windows process/DLL startup is materially slower than POSIX on the supported
# hosts. Reserve it from the user-visible wall instead of letting the in-hook
# retrieval deadline consume the entire 250 ms contract.
SUBPROCESS_BUDGET_RESERVE_MS = 125 if os.name == "nt" else 25
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

# The existing correction nudge above is deliberately broad because a false
# positive is cheap. Automatic incident collection needs a narrower predicate:
# direct corrections / repeated-breakage language only, with diagnostic
# questions excluded. This predicate is evaluated only under the dev flag.
_DETECTOR_CORRECTION_SIGNAL = re.compile(
    r"(?:\b(?:that|this|your\s+(?:answer|claim|summary|result|implementation))\s+"
    r"(?:is|was|'s)\s+(?:wrong|incorrect|inaccurate|outdated|stale|broken)\b|"
    r"\b(?:you\s+are|you'?re)\s+(?:wrong|incorrect|mistaken)\b|"
    r"\bi\s+already\s+told\s+you\b|"
    r"\bstill\s+(?:broken|wrong|failing|not\s+working)\b|"
    r"\byou\s+(?:missed|ignored|forgot)\s+(?:that|the|my)\b)",
    re.IGNORECASE,
)
_DETECTOR_DIAGNOSTIC_QUESTION = re.compile(
    r"(?:^\s*(?:what\s+is\s+wrong\s+with|is\s+this\s+wrong|"
    r"could\s+this\s+be\s+wrong|can\s+this\s+be\s+wrong|"
    r"do\s+you\s+think\b|can\s+you\s+(?:check|tell|verify)\b|"
    r"tell\s+me\b|i\s+wonder\b)|"
    r"\b(?:whether|if)\s+(?:this|that|it)\s+(?:is|was)\s+"
    r"(?:wrong|incorrect|broken)\b)",
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
    tpath = transcript_path(payload)
    prompt = (hook_field(payload, "prompt", "user_prompt") or "").strip()

    correction_signal = bool(CORRECTION_SIGNAL.search(prompt))
    detector_enabled = _detector_auto_enabled()
    detector_correction_signal = (
        _is_detector_correction(prompt) if detector_enabled else False
    )
    guideline_signal = (
        bool(GUIDELINE_SIGNAL.search(prompt))
        and "kb_priority" not in prompt.lower()
        and "latch_priority" not in prompt.lower()
    )

    # On the ordinary post-idle path, return before DB/profile resolution. A
    # Windows sqlite-vec DLL load can exceed the entire visible hook budget,
    # while there is no vector to retrieve with until the owner is warm anyway.
    if (
        sid
        and prompt
        and len(prompt.split()) >= MIN_PROMPT_WORDS
        and mcp_broker.read_discovery() is None
    ):
        log_entry = {
            "mission_control": False,
            "ts": _now(),
            "sid": sid,
            "prompt_hash": _phash(prompt),
            "prompt_words": len(prompt.split()),
            "cwd": cwd,
            "correction_signal": correction_signal,
            "guideline_signal": guideline_signal,
            "cite_nudge": 0,
            "skip": "embed_daemon_unavailable",
        }
        if detector_enabled:
            log_entry["detector_correction_signal"] = detector_correction_signal
            _set_detector_receipt_status(log_entry, [])
        log_entry["daemon_wake_requested"] = mcp_broker.request_daemon_start(cwd)
        mcp_broker.emit_lifecycle(
            "prompt_retrieval_degraded",
            reason="embed_daemon_unavailable",
            wake_requested=bool(log_entry["daemon_wake_requested"]),
        )
        _write_log(cwd, log_entry)
        _queue_detector(cwd, sid, tpath, log_entry)
        context = (
            _format_detector_retrieval_context([], log_entry)
            if detector_enabled
            else _format_runtime_unavailable()
        )
        nudge = _extra_nudges(correction_signal, guideline_signal)
        if nudge:
            context = nudge + "\n\n" + context
        _print_context(context)
        return 0

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
    if detector_enabled:
        log_entry["detector_correction_signal"] = detector_correction_signal

    # Cheap early-outs that need no DB or model. The correction nudge is
    # independent of retrieval — emit it even when retrieval is skipped
    # (short prompts like "that's wrong" are exactly the case to catch).
    if not sid:
        log_entry["skip"] = "no_session_id"
        if detector_enabled:
            _set_detector_receipt_status(log_entry, [])
        _write_log(cwd, log_entry)
        nudge = _extra_nudges(correction_signal, guideline_signal, mc_directive, cite_directive)
        if nudge:
            _print_context(nudge)
        return 0
    if not prompt or len(prompt.split()) < MIN_PROMPT_WORDS:
        log_entry["skip"] = "prompt_too_short"
        if detector_enabled:
            _set_detector_receipt_status(log_entry, [])
        _write_log(cwd, log_entry)
        _queue_detector(cwd, sid, tpath, log_entry)
        nudge = _extra_nudges(correction_signal, guideline_signal, mc_directive, cite_directive)
        if nudge:
            _print_context(nudge)
        return 0

    t0 = time.perf_counter()
    try:
        injected = _retrieve_and_inject(
            cwd, sid, prompt, log_entry,
            deadline=_PROCESS_STARTED + (
                HARD_BUDGET_MS - SUBPROCESS_BUDGET_RESERVE_MS
            ) / 1000.0,
        )
    except Exception as e:
        log_entry["error"] = f"{type(e).__name__}: {e}"
        if detector_enabled:
            _set_detector_receipt_status(log_entry, [])
        _write_log(cwd, log_entry)
        _queue_detector(cwd, sid, tpath, log_entry)
        log(f"user_prompt_submit error: {e}")
        return 0
    elapsed_ms = (time.perf_counter() - t0) * 1000
    log_entry["elapsed_ms"] = round(elapsed_ms, 1)

    if elapsed_ms > HARD_BUDGET_MS:
        log_entry["overran_budget"] = True
        # Already past budget — still emit the result we computed, since the
        # damage (latency) is already done. Future tuning can decide whether
        # to drop the result instead.

    if detector_enabled:
        _set_detector_receipt_status(log_entry, injected)
    _write_log(cwd, log_entry)
    _queue_detector(cwd, sid, tpath, log_entry)

    if detector_enabled:
        context = _format_detector_retrieval_context(injected, log_entry)
    elif log_entry.get("skip") == "embed_daemon_unavailable":
        context = _format_runtime_unavailable()
    else:
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


def _retrieve_and_inject(
    cwd: str, sid: str, prompt: str, log_entry: dict, *, deadline: float | None = None,
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

        active_set = db.get_active_set(conn, session_id=sid, current_turn=turn)
        log_entry["active_set_size"] = len(active_set)
        if _detector_auto_enabled():
            log_entry["active_ids"] = sorted(int(n) for n in active_set)

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

        if _detector_auto_enabled():
            _freeze_detector_node_snapshots(conn, log_entry)

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
    scope_repo: str | None = None,
) -> list[dict]:
    _load_runtime()
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
        "## KB hits — none auto-retrieved (sim below floor)\n\n"
        "**Auto-retrieval found nothing above SIM_FLOOR.** That doesn't mean "
        "the KB has nothing — it means similarity scoring missed. Actively "
        "query latch (`latch_search` / `latch_get` / `latch_recent`) before "
        "responding — every prompt, no exception."
    )


def _format_runtime_unavailable() -> str:
    return (
        "## KB auto-retrieval temporarily unavailable\n\n"
        "The shared latch runtime was idle, starting, or unreachable, so this "
        "prompt was **not similarity-scored**. This is not a below-threshold "
        "result. Latch requested a background wake; actively query "
        "(`latch_search` / `latch_get` / `latch_recent`) before responding."
    )


def _format_detector_retrieval_context(items: list[dict], log_entry: dict) -> str:
    """Truthful dev-detector receipt surface; never conflates no-run with no-hit."""
    status = log_entry.get("retrieval_status")
    if status in {"unavailable", "error"}:
        reason = log_entry.get("skip") or str(log_entry.get("error") or "runtime error").split(":", 1)[0]
        return (
            "## KB retrieval unavailable\n\n"
            f"**Latch retrieval did not produce a result ({reason}).** This is "
            "distinct from finding no relevant node. Actively query Latch and "
            "treat the runtime state as degraded."
        )
    if status == "not_executed":
        return (
            "## KB retrieval not executed\n\n"
            f"The per-prompt retrieval path did not run ({log_entry.get('skip') or 'unknown reason'}). "
            "This is not evidence that the KB has no relevant result."
        )
    if status == "over_budget":
        detail = _format_injection(items) if items else (
            "No context was injected from the recorded top 10."
        )
        return (
            "## KB retrieval completed over budget\n\n"
            "The retrieval result below was produced, but the runtime exceeded "
            f"the {HARD_BUDGET_MS} ms hook budget and is recorded as degraded.\n\n"
            f"{detail}"
        )
    if items:
        return _format_injection(items)
    return (
        "## KB hits — no context injected from the recorded top 10\n\n"
        "Retrieval executed, but no candidate survived the current kind, active-set, "
        "and similarity filters. This is a top-10 receipt boundary, not proof that "
        "the KB has no relevant information. Actively query Latch before responding."
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
    _load_light_runtime()
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
    _load_light_runtime()
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


def _is_detector_correction(prompt: str) -> bool:
    """High-confidence dev-detector predicate, narrower than the nudge."""
    # Ignore fenced code and quoted log/output lines so pasted failures do not
    # become user-correction incidents.
    scrubbed = re.sub(r"```.*?```", " ", prompt or "", flags=re.DOTALL)
    scrubbed = "\n".join(
        line for line in scrubbed.splitlines() if not line.lstrip().startswith(">")
    ).strip()
    if not scrubbed or _DETECTOR_DIAGNOSTIC_QUESTION.search(scrubbed):
        return False
    return bool(
        _DETECTOR_CORRECTION_SIGNAL.search(scrubbed)
        or re.search(
            r"\b(?:that'?s|this\s+is)\s+(?:wrong|incorrect|broken|outdated)\b",
            scrubbed,
            re.IGNORECASE,
        )
    )


def _set_detector_receipt_status(log_entry: dict, injected: list[dict]) -> str:
    if log_entry.get("error"):
        status = "error"
    elif log_entry.get("skip") == "embed_daemon_unavailable":
        status = "unavailable"
    elif log_entry.get("skip"):
        status = "not_executed"
    elif log_entry.get("overran_budget"):
        status = "over_budget"
    elif injected:
        status = "ok"
    else:
        status = "no_injection"
    log_entry["retrieval_status"] = status
    return status


def _freeze_detector_node_snapshots(conn, log_entry: dict) -> None:
    """Freeze ranked, injected, then bounded active nodes at event time."""
    if not _detector_auto_enabled():
        return
    try:
        import detector_snapshot

        scores: dict[int, float] = {}
        ids: list[int] = []
        seen: set[int] = set()
        for item in list(log_entry.get("raw_hits") or []) + list(log_entry.get("injected") or []):
            if not isinstance(item, (list, tuple)) or not item:
                continue
            node_id = int(item[0])
            if node_id not in seen:
                seen.add(node_id)
                ids.append(node_id)
            if len(item) > 1 and item[1] is not None:
                scores[node_id] = float(item[1])
        for raw_id in log_entry.get("active_ids") or []:
            node_id = int(raw_id)
            if node_id not in seen:
                seen.add(node_id)
                ids.append(node_id)
        log_entry["node_snapshots"] = detector_snapshot.snapshot_nodes(
            conn,
            ids,
            scores=scores,
            limit=32,
        )
        log_entry["node_snapshot_omitted_count"] = max(0, len(ids) - 32)
    except Exception as exc:
        # Truthful partial state: preserve the reason without breaking the hook.
        log_entry["snapshot_status"] = f"unavailable:{type(exc).__name__}"


def _queue_detector(
    cwd: str, sid: str | None, tpath: str | None, log_entry: dict,
) -> None:
    """Queue one background trace only after the retrieval receipt is durable."""
    if not _detector_auto_enabled() or not sid:
        return
    triggers: list[str] = []
    if log_entry.get("detector_correction_signal"):
        triggers.append("explicit_correction")
    if log_entry.get("retrieval_status") in {"unavailable", "error", "over_budget"}:
        triggers.append("runtime_degraded")
    if not triggers:
        return
    try:
        import detector_trigger

        detector_trigger.queue(
            project_path=cwd,
            session_id=sid,
            transcript_path=tpath,
            prompt_hash=log_entry.get("prompt_hash"),
            event_ts=log_entry.get("detector_event_ts") or log_entry.get("ts"),
            trigger_types=triggers,
            node_ids=[
                int(s["id"])
                for s in log_entry.get("node_snapshots") or []
                if isinstance(s, dict) and s.get("id") is not None
            ],
            turn=log_entry.get("turn"),
        )
    except Exception as exc:
        log(f"detector queue failed: {exc}")


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
        if _detector_auto_enabled():
            # Durable event coordinate: this exact value is persisted in the
            # receipt and handed to the detached worker after the append.
            entry.setdefault("detector_event_ts", _detector_now_iso())
        log_utils.emit_event(
            LOG_STREAM, entry,
            project_path=cwd,
            session_id=entry.get("sid"),
        )
    except Exception as e:
        log(f"retrieve.log write failed: {e}")


def _detector_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _detector_auto_enabled() -> bool:
    adapter = os.environ.get("LATCH_ADAPTER", "").strip().lower()
    return (
        os.environ.get("LATCH_DEV_DETECTOR") == "1"
        and adapter in {"", "claude-code", "claude_code"}
    )


if __name__ == "__main__":
    sys.exit(main())
