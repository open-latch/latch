"""Compaction: summarize a session transcript into KB nodes via a model backend.

Called by hooks (Stop / SessionEnd / SessionStart-reconcile) and by the
/latch-compact slash command. Produces one session_summary node per session
(UPSERTed) plus extracted facts/decisions/entities (stacked).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).parent))

import artifacts  # noqa: E402
import budget  # noqa: E402
import codex_transcript  # noqa: E402
import cursor_backend  # noqa: E402
import db  # noqa: E402
import embeddings  # noqa: E402
import feeders  # noqa: E402
import heal  # noqa: E402
import lifecycle_signals  # noqa: E402
import lockfile  # noqa: E402
import log_utils  # noqa: E402
import paths  # noqa: E402
import workstreams  # noqa: E402

# On Windows, subprocess.run([...]) with shell=False calls CreateProcess, which
# does not consult PATHEXT — a bare "claude" argv0 won't find claude.cmd. Resolve
# the full path once via shutil.which so it works on Windows (.cmd) and Unix alike.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"
CLAUDE_COMPACTOR_DISALLOWED_TOOLS = "Bash,Edit,Write,NotebookEdit"
# CREATE_NO_WINDOW: don't flash a console window per claude.cmd call when the
# parent has no console. 0 on POSIX (no-op). See heal.py for the full rationale.
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
MAX_TRANSCRIPT_CHARS = 120_000  # truncate from the head; prefer recent turns
REPAIR_RAW_OUTPUT_CHARS = 20_000
REPAIR_TRANSCRIPT_CHARS = 40_000
SUPPORTED_SUMMARIZER_BACKENDS = {"claude", "codex", "cursor"}
COMPACTOR_ATTESTATION_CANDIDATE_LIMIT = 5
COMPACTOR_OPEN_CANDIDATE_LIMIT = 3
_FEEDER_RELATIONS = frozenset({"advances", "motivates", "depends_on"})
_EXTRACTED_NODE_KINDS = frozenset({
    "fact",
    "decision",
    "progress",
    "entity",
    "preference",
    "open_question",
    "idea",
})
_LIFECYCLE_OWNED_EDGE_RELATIONS = frozenset({
    "merged_into",
    "closed_in_favor_of",
    "branched_from",
})

COMPACT_PROMPT = """You are summarizing a coding-agent session for a per-project knowledge base.

The KB stores nodes (facts, decisions, progress, entities, preferences, open_questions,
ideas) and typed edges between them. The goal is so a *future* agent session can pick
up where this one left off without the user having to re-explain.

You are given:
  1. Any prior summary for this same session (which you should COMBINE with new content,
     not duplicate). If this is the first compact, prior summary will be empty.
  2. Recent transcript content.
  3. A small sample of related KB nodes already known. Rows carrying role
     "open_feeder" are unresolved building blocks of the currently active
     workstreams — see Temporal stance and Closure duty below.

Produce ONE JSON object with this exact shape, and nothing else:

{
  "session_summary": {
    "title": "<short title, ~6-10 words>",
    "body": "<markdown body covering: what we worked on, key decisions, current state, what's next>"
  },
  "extracted_nodes": [
    {"kind": "fact|decision|progress|entity|preference|open_question|idea",
     "title": "<short>", "body": "<markdown>",
     "workstream_id": <int|null — see workstream guidance below>}
  ],
  "links": [
    {"src_title": "<title from extracted_nodes or session_summary>",
     "dst_id": <existing node id from related KB nodes>,
     "relation": "<verb — see relation vocabulary below>"}
  ],
  "workstream_proposals": [
    {"proposal_key": "<stable short key>",
     "candidate_key": "<candidate_key from an open_candidate row>",
     "title": "<short workstream title>",
     "charter_body": "Objective: ...\nDone when: ...\nScope boundary: ...\nNext step: ...",
     "seed_member_ids": [<stored node id>, ...],
     "member_titles": ["<exact stored/extracted node title>", ...],
     "recurrence_evidence": {"session_ids": ["<session id>", ...]}}
  ],
  "attestations": [
    {"candidate_key": "<candidate_key from a merge_candidate/close_candidate row>",
     "verdict": "agree|disagree|unsure",
     "evidence_ids": [<node id from that candidate row>, ...]}
  ]
}

Kind semantics (pick the best fit; when in doubt, `fact`):
- fact: a verified piece of information about the code, system, or domain.
- decision: a choice made (architecture, trade-off, scope) with rationale.
- progress: what was done this session; what remains.
- entity: a named thing (file, service, person, API) worth remembering.
- preference: a user-stated way to work (style, tool, convention).
- open_question: something unresolved that needs later attention.
- idea: a hypothetical/future item — something the user has floated but not
  committed to. Parked future items, experimental directions, "maybe someday"
  thoughts. Ideas are surfaced to future sessions so they are not lost.

Temporal stance (decide it for every extracted node):
- SETTLED — records something that is true or finished. Write it matter-of-fact.
- FORWARD-LOOKING — an idea, open question, research finding, or partial
  progress that exists to serve work that has not happened yet. Write it as a
  building block toward its end state, and include in the body:
    (a) the end state or decision it serves,
    (b) "Done when: <condition that makes it resolved or moot>",
    (c) the next concrete step.
  When that end state is a workstream or decision visible in related_kb_nodes,
  also emit a links entry to it using advances / motivates / depends_on.
  A forward-looking node with no target link is a capture smell; a settled
  fact with no links is fine.

Closure duty:
- If the transcript shows that a related_kb_nodes row (especially one with
  role "open_feeder") was completed, made moot, or abandoned this session,
  emit a links entry from the extracted node recording that outcome to the
  old node's id, using `resolves` (completed or moot) or `supersedes`
  (replaced by a new direction). Declaring the transition now is cheaper and
  more accurate than a nightly sweep reconstructing it later.

Workstream guidance:
- If any related_kb_nodes have kind='workstream' and a new node clearly
  belongs to one of them, set `workstream_id` to that workstream's id.
- If unsure or the connection is weak, leave `workstream_id` as null.
  Orphan nodes are tolerated; over-tagging is worse than under-tagging.
- Never invent a workstream_id that is not in related_kb_nodes.
- Never tag a node into a closed/stale workstream. If a workstream row is not
  explicitly present and active in related_kb_nodes, leave workstream_id null.

Workstream lifecycle judgment (candidate events only — NEVER mutate a lane):
- Rows with role='merge_candidate' or role='close_candidate' come only from the
  latest lifecycle derivation. If this session supplies useful judgment, emit
  one attestation using that exact candidate_key and only evidence_ids shown by
  the row. Do not attest an older, absent, OPEN, or ADOPT candidate.
- A row with role='open_candidate' may support one workstream_proposal. A
  proposal is a draft/corroboration event, NOT permission to open a workstream.
  Use only stored/extracted members named by that candidate and cite genuine
  recurrence from at least two contamination-free sessions (or a validated
  shared feeder/decision target). Never invent member ids, titles, sessions,
  evidence, candidate keys, or a force field.
- Every proposed charter must contain four non-empty labeled lines:
  Objective:, Done when:, Scope boundary:, and Next step:.
- These arrays may be empty. Never claim a proposal or attestation was applied;
  deterministic lifecycle tooling decides later whether any operation occurs.

Relation vocabulary:
- The system has a canonical traversal set used for chain reasoning:
    `supersedes` (newer kills older),
    `replaces` (current direction over abandoned),
    `constrains` (constraint -> decision/workstream),
    `motivates` (problem/feedback -> decision),
    `tested_against` (decision -> benchmark),
    `depends_on` (X requires Y first).
  When an edge fits one of these cleanly, use that exact name.
- Otherwise use a free-form verb (`related_to`, `implements`, `answers`,
  `resolves`, `confirms`, `contrasts_with`, `explains_failure_of`, etc.).
  Free-form is fine when no canonical fits.
- Never emit `merged_into`, `closed_in_favor_of`, or `branched_from`; those
  identity relations are written only by the governed lifecycle service.
- The system canonicalizes known synonyms on insert — `relates_to` becomes
  `related_to`, `requires` becomes `depends_on`. Don't worry about exact form.

Guidelines:
- Be specific. Prefer concrete facts over generalities.
- The session_summary REPLACES the prior summary — include everything still relevant
  from the prior summary plus the new content. Do not lose state.
- Only extract a node if it is reusable knowledge (would help a future session).
  Skip per-turn chatter.
- Skip links if you are unsure — empty list is fine for settled nodes.
  Forward-looking nodes should carry their target link whenever the target
  is visible in related_kb_nodes.
- Output JSON only. No markdown fences, no commentary.
"""


def read_transcript(path: str | Path) -> str:
    """Flatten a Claude Code or Codex JSONL transcript to readable text."""
    if codex_transcript.is_codex_transcript(path):
        joined = codex_transcript.read_transcript(path)
        if len(joined) > MAX_TRANSCRIPT_CHARS:
            joined = "...[earlier turns truncated]...\n\n" + joined[-MAX_TRANSCRIPT_CHARS:]
        return joined

    # Claude Code transcripts are JSONL; we flatten to a readable form.
    p = Path(path)
    if not p.exists():
        return ""
    lines = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = obj.get("type") or obj.get("role") or "?"
        msg = obj.get("message") or obj
        content = msg.get("content") if isinstance(msg, dict) else None
        text = _flatten_content(content) if content is not None else json.dumps(obj)[:500]
        if text:
            lines.append(f"[{role}] {text}")
    joined = "\n\n".join(lines)
    if len(joined) > MAX_TRANSCRIPT_CHARS:
        joined = "...[earlier turns truncated]...\n\n" + joined[-MAX_TRANSCRIPT_CHARS:]
    return joined


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and "text" in item:
                    parts.append(item["text"])
                elif item.get("type") == "tool_use":
                    parts.append(f"[tool_use {item.get('name','?')}]")
                elif item.get("type") == "tool_result":
                    r = item.get("content", "")
                    if isinstance(r, list):
                        r = " ".join(str(x.get("text", "")) for x in r if isinstance(x, dict))
                    parts.append(f"[tool_result {str(r)[:300]}]")
        return "\n".join(parts)
    return str(content) if content else ""


def _related_nodes_brief(conn, query: str, limit: int = 8) -> list[dict]:
    import search
    # track_access=False: the compactor fetches these as context for a summarizer,
    # not as user-driven retrieval. Counting these as references would inflate
    # ref_count on whichever nodes happen to cluster near recent transcripts.
    rows = search.hybrid_search(conn, query, limit=limit, track_access=False)
    return [{"id": r["id"], "kind": r["kind"], "title": r["title"]} for r in rows]


def _done_when_line(body: str) -> str | None:
    for line in (body or "").splitlines():
        if line.strip().lower().startswith("done when:"):
            return line.strip()[:400]
    return None


def _merge_focus_workstreams(conn, related: list[dict]) -> list[dict]:
    """Offer the current focus lanes explicitly to the compactor.

    Search samples frequently contain only member nodes, which left the model
    no legal workstream id to emit. Focus is the bounded set the session could
    plausibly have worked on; duplicate ids keep the search row's position but
    gain the explicit lane role and charter fields.
    """
    merged = [dict(row) for row in related]
    positions = {
        int(row["id"]): index
        for index, row in enumerate(merged)
        if row.get("id") is not None
    }
    for lane in db.get_focus(conn, limit=db.FOCUS_CAP):
        if lane.get("status") == "stale":
            continue
        wid = int(lane.get("workstream_id") or lane["id"])
        row = {
            "id": wid,
            "kind": "workstream",
            "title": lane["title"],
            "role": "focus_workstream",
            "charter_excerpt": " ".join((lane.get("body") or "").split())[:800],
            "done_when": _done_when_line(lane.get("body") or ""),
        }
        if wid in positions:
            merged[positions[wid]].update(row)
        else:
            positions[wid] = len(merged)
            merged.append(row)
    return merged


_LANE_ID_KEYS = frozenset({
    "lane_ids", "workstream_ids", "src_workstream_id", "dst_workstream_id",
    "workstream_id", "source_workstream_id", "target_workstream_id", "left", "right",
})
_MEMBER_ID_KEYS = frozenset({
    "member_ids", "seed_member_ids", "node_ids", "candidate_member_ids", "members",
})
_EVIDENCE_ID_KEYS = frozenset({
    "evidence_ids", "shared_target_ids", "decision_ids", "feeder_ids",
    "target_ids", "retrieval_event_node_ids",
})
_SESSION_ID_KEYS = frozenset({
    "session_ids", "contact_sessions", "eligible_session_ids", "sessions",
    "cross_path_sessions",
})
_MEMBER_TITLE_KEYS = frozenset({"member_titles", "seed_member_titles"})


def _coerce_int_values(value: Any) -> set[int]:
    values = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    out: set[int] = set()
    for item in values:
        if isinstance(item, Mapping):
            item = item.get("id", item.get("node_id"))
        if isinstance(item, bool):
            continue
        try:
            out.add(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _signal_ids(value: Any, keys: frozenset[str]) -> set[int]:
    out: set[int] = set()
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).lower()
            if key in keys:
                out.update(_coerce_int_values(child))
            if isinstance(child, (Mapping, list, tuple)):
                out.update(_signal_ids(child, keys))
    elif isinstance(value, (list, tuple)):
        for child in value:
            if isinstance(child, (Mapping, list, tuple)):
                out.update(_signal_ids(child, keys))
    return out


def _signal_texts(value: Any, keys: frozenset[str]) -> set[str]:
    out: set[str] = set()
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).lower()
            if key in keys:
                values = child if isinstance(child, (list, tuple, set)) else [child]
                for item in values:
                    if isinstance(item, str) and item.strip():
                        out.add(item.strip())
            if isinstance(child, (Mapping, list, tuple)):
                out.update(_signal_texts(child, keys))
    elif isinstance(value, (list, tuple)):
        for child in value:
            if isinstance(child, (Mapping, list, tuple)):
                out.update(_signal_texts(child, keys))
    return out


def _candidate_evidence_ids(signal: Mapping[str, Any]) -> set[int]:
    return (
        _signal_ids(signal, _LANE_ID_KEYS)
        | _signal_ids(signal, _MEMBER_ID_KEYS)
        | _signal_ids(signal, _EVIDENCE_ID_KEYS)
    )


def _candidate_member_ids(conn, signal: Mapping[str, Any]) -> set[int]:
    out = _signal_ids(signal, _MEMBER_ID_KEYS)
    if out:
        placeholders = ",".join("?" for _ in out)
        rows = conn.execute(
            f"SELECT id FROM nodes WHERE id IN ({placeholders}) "
            "AND kind IN ('workstream','priority')",
            sorted(out),
        ).fetchall()
        out.difference_update(int(row["id"]) for row in rows)
    for title in _signal_texts(signal, _MEMBER_TITLE_KEYS):
        rows = conn.execute(
            "SELECT id FROM nodes WHERE title = ? AND status != 'stale' "
            "AND kind NOT IN ('workstream','priority') ORDER BY id",
            (title,),
        ).fetchall()
        if len(rows) == 1:
            out.add(int(rows[0]["id"]))
    return out


def _member_titles_by_id(conn, member_ids: list[int]) -> list[str]:
    if not member_ids:
        return []
    placeholders = ",".join("?" for _ in member_ids)
    rows = conn.execute(
        f"SELECT id, title FROM nodes WHERE id IN ({placeholders})",
        member_ids,
    ).fetchall()
    by_id = {int(row["id"]): str(row["title"]) for row in rows}
    return [by_id[node_id] for node_id in member_ids if node_id in by_id]


def _merge_lifecycle_candidate_rows(conn, related: list[dict]) -> list[dict]:
    """Append bounded latest-derivation rows for proposal/attestation duty.

    The model receives only structural ids and a one-line signal summary, not
    the detector's arbitrary JSON payload. MERGE/CLOSE rows are attestable;
    OPEN rows are proposal corroboration only.
    """
    merged = [dict(row) for row in related]
    candidates = db.latest_workstream_derivation_candidates(conn)
    attestation_rows = 0
    open_rows = 0
    for candidate in candidates:
        op = str(candidate.get("op") or "").upper()
        if op in {"MERGE", "CLOSE"}:
            if attestation_rows >= COMPACTOR_ATTESTATION_CANDIDATE_LIMIT:
                continue
            attestation_rows += 1
            role = f"{op.lower()}_candidate"
        elif op == "OPEN":
            if open_rows >= COMPACTOR_OPEN_CANDIDATE_LIMIT:
                continue
            open_rows += 1
            role = "open_candidate"
        else:
            continue
        signal = candidate.get("signal") if isinstance(candidate.get("signal"), Mapping) else {}
        lane_ids = sorted(_signal_ids(signal, _LANE_ID_KEYS))
        member_ids = sorted(_candidate_member_ids(conn, signal))
        member_titles = sorted(
            set(_member_titles_by_id(conn, member_ids))
            | _signal_texts(signal, _MEMBER_TITLE_KEYS)
        )
        evidence_ids = sorted(_candidate_evidence_ids(signal))
        session_ids = sorted(_signal_texts(signal, _SESSION_ID_KEYS))
        merged.append({
            "role": role,
            "candidate_key": candidate["candidate_key"],
            "derivation_key": candidate["derivation_key"],
            "rank": int(candidate.get("rank") or 0),
            "workstream_ids": lane_ids,
            "member_ids": member_ids,
            "member_titles": member_titles,
            "evidence_ids": evidence_ids,
            "session_ids": session_ids,
            "evidence": _candidate_evidence_line(
                signal=signal,
                lane_ids=lane_ids,
                member_ids=member_ids,
                evidence_ids=evidence_ids,
                session_ids=session_ids,
            ),
        })
    return merged


def _candidate_evidence_line(
    *,
    signal: Mapping[str, Any],
    lane_ids: list[int],
    member_ids: list[int],
    evidence_ids: list[int],
    session_ids: list[str],
) -> str:
    parts = []
    if lane_ids:
        parts.append("workstreams=" + ",".join(str(value) for value in lane_ids[:8]))
    if member_ids:
        parts.append("members=" + ",".join(str(value) for value in member_ids[:12]))
    if evidence_ids:
        parts.append("evidence=" + ",".join(str(value) for value in evidence_ids[:12]))
    if session_ids:
        parts.append(f"eligible_sessions={len(session_ids)}")
    for key in (
        "co_contact_sessions", "jaccard", "contact_session_count", "span_hours",
        "observation_days", "tier1", "reason",
    ):
        value = signal.get(key)
        if value is not None and not isinstance(value, (Mapping, list, tuple)):
            parts.append(f"{key}={value}")
    tier2 = signal.get("tier2_inputs")
    if isinstance(tier2, list) and tier2:
        parts.append("tier2=" + ",".join(str(value) for value in tier2[:4]))
    return "; ".join(parts) or "latest derivation candidate (no node ids supplied)"


@contextlib.contextmanager
def _project_lock(project_path: str):
    """Backwards-compat shim — the lock primitive moved to `lockfile.py` so
    MCP write tools can also consult it via `wait_for_compaction`. Behavior
    unchanged: acquire-or-skip, yielding True/False."""
    with lockfile.compactor_lock(project_path) as acquired:
        if not acquired:
            _log(f"compactor lock held at {lockfile._lock_path(project_path)} — skipping")
        yield acquired


def run_compaction(
    session_id: str,
    project_path: str,
    transcript_path: str | None,
    *,
    final: bool = False,
    summarizer_backend: str | None = None,
) -> dict:
    """Run one compaction pass. Returns a small status dict."""
    if paths.is_unlatched_mode():
        return {
            "ok": False,
            "reason": "unlatched",
            "message": paths.UNLATCHED_MESSAGE,
            "session_id": session_id,
        }
    if paths.is_disabled():
        return {"ok": False, "reason": "disabled", "session_id": session_id}
    if paths.is_in_compact():
        # Should never happen in practice — hooks guard this path — but if the
        # compactor is ever invoked inside a compactor-spawned summarizer
        # session, we refuse to recurse.
        return {"ok": False, "reason": "reentrant", "session_id": session_id}
    try:
        backend = _summarizer_backend(summarizer_backend, default="claude")
    except ValueError as e:
        return {
            "ok": False,
            "reason": "unsupported_summarizer_backend",
            "error": str(e),
            "session_id": session_id,
        }
    with _project_lock(project_path) as acquired:
        if not acquired:
            return {"ok": False, "reason": "locked", "session_id": session_id}
        # Budget gate — the backstop against auto-hook runaways. Check AND
        # reserve in one shot so the count is accurate even if the compaction
        # itself fails afterward (tokens were still spent).
        try:
            allowed, state = budget.check_and_record(project_path, category="nonheal")
        except OSError as exc:
            _log(f"budget state unavailable for {project_path}: {exc} — "
                 f"compaction blocked without spend")
            return {
                "ok": False,
                "reason": "budget_state_error",
                "session_id": session_id,
            }
        if not allowed:
            _log(f"budget cap hit for {project_path}: "
                 f"{state['count_nonheal']}/day non-heal — "
                 f"run /latch-budget-approve to unlock")
            return {
                "ok": False,
                "reason": "budget_cap",
                "count": state["count_nonheal"],
                "cap": budget.DEFAULT_NONHEAL_DAILY_CAP,
                "category": "nonheal",
                "session_id": session_id,
            }
        return _run_compaction_locked(
            session_id, project_path, transcript_path, final=final,
            summarizer_backend=backend,
        )


def _run_compaction_locked(
    session_id: str,
    project_path: str,
    transcript_path: str | None,
    *,
    final: bool = False,
    summarizer_backend: str = "claude",
) -> dict:
    conn = db.connect(project_path)
    try:
        sess = db.get_session(conn, session_id)
        if sess is None:
            db.upsert_session(conn, session_id, project_path, transcript_path)
            sess = db.get_session(conn, session_id)
        transcript_path = transcript_path or sess.get("transcript_path")
        transcript_text = read_transcript(transcript_path) if transcript_path else ""

        prior_summary = ""
        prior_node_id = sess.get("summary_node_id")
        if prior_node_id:
            prior = db.get_node(conn, prior_node_id)
            if prior:
                prior_summary = prior["body"]

        related = _related_nodes_brief(conn, transcript_text[-4000:] or "project work")
        related = _merge_focus_workstreams(conn, related)
        # Focus-workstream feeders ride along so the summarizer can target
        # forward-looking links and honor the closure duty (KB 2299).
        related = feeders.merge_feeder_rows(conn, related)
        related = _merge_lifecycle_candidate_rows(conn, related)
        offered_workstream_ids = {
            int(row["id"])
            for row in related
            if row.get("kind") == "workstream" and row.get("id") is not None
        }

        prompt_payload = {
            "project_path": project_path,
            "session_id": session_id,
            "prior_summary": prior_summary,
            "transcript": transcript_text,
            "related_kb_nodes": related,
        }

        result_json = _invoke_summarizer(prompt_payload, backend=summarizer_backend)
        if result_json is None:
            return {
                "ok": False,
                "reason": f"{summarizer_backend}_invocation_failed",
                "summarizer_backend": summarizer_backend,
                "session_id": session_id,
            }

        apply_result = _apply_compaction(
            conn, session_id, result_json, final=final, prior_summary_id=prior_node_id,
            project_path=project_path,
            offered_workstream_ids=offered_workstream_ids,
        )
        summary_node_id = apply_result["summary_node_id"]
        write_count = (
            int(apply_result["summary_written"])
            + apply_result["inserted_nodes"]
            + apply_result["linked_edges"]
            + apply_result["lifecycle_events"]
        )
        if write_count == 0:
            _log(
                "compactor produced no summary body, extracted nodes, or links; "
                f"leaving session {session_id} uncompacted"
            )
            return {
                "ok": False,
                "reason": "empty_compaction_result",
                "session_id": session_id,
                "summary_node_id": summary_node_id,
                "summary_written": False,
                "inserted_nodes": 0,
                "linked_edges": 0,
                "lifecycle_events": 0,
                "final": final,
                "summarizer_backend": summarizer_backend,
            }
        # Slice 2: auto-observe the files this session actually edited (parsed from
        # the raw transcript) and attach them as provenance to the session's nodes
        # — superseding Slice 1's coarse repo=project_cwd fallback in signal value.
        # Non-fatal: an enrichment, never allowed to break compaction.
        try:
            n_enriched = artifacts.attach_observed_artifacts(
                conn, session_id, transcript_path, project_path,
            )
            if n_enriched:
                _log(f"artifact auto-observe: enriched {n_enriched} node(s) "
                     f"for session {session_id}")
        except Exception as e:  # noqa: BLE001
            _log(f"artifact auto-observe failed (non-fatal): {e}")
        db.mark_compacted(conn, session_id, sess["turn_count"], summary_node_id)
        if final:
            db.mark_ended(conn, session_id)
        return {
            "ok": True,
            "session_id": session_id,
            "summary_node_id": summary_node_id,
            "summary_written": apply_result["summary_written"],
            "inserted_nodes": apply_result["inserted_nodes"],
            "linked_edges": apply_result["linked_edges"],
            "lifecycle_events": apply_result["lifecycle_events"],
            "attestations_recorded": apply_result["attestations_recorded"],
            "proposals_accepted": apply_result["proposals_accepted"],
            "proposals_rejected": apply_result["proposals_rejected"],
            "final": final,
            "summarizer_backend": summarizer_backend,
        }
    finally:
        conn.close()


def _summarizer_backend(name: str | None, *, default: str = "claude") -> str:
    raw = (
        name
        or os.environ.get("CLAUDE_KB_COMPACTOR_BACKEND")
        or os.environ.get("LATCH_COMPACTOR_BACKEND")
        or os.environ.get("LATCH_MODEL_BACKEND")
        or default
    )
    backend = raw.strip().lower()
    if backend not in SUPPORTED_SUMMARIZER_BACKENDS:
        supported = ", ".join(sorted(SUPPORTED_SUMMARIZER_BACKENDS))
        raise ValueError(f"unsupported summarizer backend {raw!r}; expected one of: {supported}")
    return backend


def _invoke_claude(payload: dict) -> dict | None:
    """Compatibility wrapper for the production Claude Code compactor path."""
    return _invoke_summarizer(payload, backend="claude")


def _invoke_summarizer(payload: dict, *, backend: str = "claude") -> dict | None:
    """First attempt + one repair retry. Returns parsed dict or None.

    Worst case: 2 backend invocations per compaction (first attempt +
    repair). The daily budget cap (step 6) counts compactions, not invocations,
    so retry cost is bounded per compaction.
    """
    backend = _summarizer_backend(backend, default="claude")
    user_msg = (
        COMPACT_PROMPT
        + "\n\n--- PRIOR SUMMARY ---\n"
        + (payload["prior_summary"] or "(none)")
        + "\n\n--- RELATED KB NODES ---\n"
        + json.dumps(payload["related_kb_nodes"], indent=2)
        + "\n\n--- TRANSCRIPT ---\n"
        + payload["transcript"]
    )

    stdout, err = _invoke_summarizer_once(user_msg, backend=backend)
    if stdout is None:
        _log(f"compactor first-attempt {backend} subprocess failed: {err}")
        return None

    obj, parse_err = _parse_json_envelope(stdout)
    if obj is not None and _has_compaction_content(obj):
        return obj
    if obj is not None:
        parse_err = "parsed JSON had no summary body, extracted nodes, or links"

    _log(f"compactor first-attempt parse failed ({parse_err}); attempting repair")
    repair_msg = _repair_prompt(
        payload=payload,
        parse_err=parse_err,
        raw_output=stdout,
    )
    stdout2, err2 = _invoke_summarizer_once(repair_msg, backend=backend)
    if stdout2 is None:
        _log(f"compactor repair {backend} subprocess failed: {err2}")
        _save_failed_compact(payload, stdout, None,
                             reason=f"first:{parse_err};repair_subprocess:{err2}")
        return None

    obj2, parse_err2 = _parse_json_envelope(stdout2)
    if obj2 is not None and _has_compaction_content(obj2):
        _log("compactor repair succeeded")
        return obj2
    if obj2 is not None:
        _log("compactor repair parsed JSON but result was empty")
        _save_failed_compact(payload, stdout, stdout2,
                             reason=f"first:{parse_err};repair_empty")
        return obj2

    _log(f"compactor repair parse also failed: {parse_err2}")
    _save_failed_compact(payload, stdout, stdout2,
                         reason=f"first:{parse_err};repair:{parse_err2}")
    return None


def _repair_prompt(*, payload: dict, parse_err: str, raw_output: str) -> str:
    """Build a self-contained repair prompt.

    Repair calls are separate Claude/Codex/Cursor processes with no session
    memory, so references to "the original request" are not enough.
    Include the schema and a bounded slice of the original context so the
    repair model can either convert useful prose output into JSON or regenerate
    a valid compact when the first output was structurally empty.
    """
    transcript = payload.get("transcript") or ""
    if len(transcript) > REPAIR_TRANSCRIPT_CHARS:
        transcript = (
            "...[earlier transcript omitted for repair]...\n\n"
            + transcript[-REPAIR_TRANSCRIPT_CHARS:]
        )
    return (
        COMPACT_PROMPT
        + "\n\nThe previous output failed compaction validation with this error:\n"
        + parse_err
        + "\n\nHere is the raw output produced by the previous attempt:\n\n"
        + (raw_output or "")[:REPAIR_RAW_OUTPUT_CHARS]
        + "\n\n--- ORIGINAL PRIOR SUMMARY ---\n"
        + (payload.get("prior_summary") or "(none)")
        + "\n\n--- ORIGINAL RELATED KB NODES ---\n"
        + json.dumps(payload.get("related_kb_nodes") or [], indent=2)
        + "\n\n--- ORIGINAL TRANSCRIPT EXCERPT ---\n"
        + transcript
        + "\n\nReturn ONLY a single valid JSON object matching the schema above. "
        + "No markdown fences, no prose, no commentary."
    )


def _has_compaction_content(obj: dict) -> bool:
    summary = obj.get("session_summary") or {}
    if isinstance(summary, dict) and (summary.get("body") or "").strip():
        return True
    for node in obj.get("extracted_nodes", []) or []:
        if (
            isinstance(node, dict)
            and isinstance(node.get("kind"), str)
            and node.get("kind") in _EXTRACTED_NODE_KINDS
            and isinstance(node.get("body"), str)
            and node.get("body", "").strip()
        ):
            return True
    for link in obj.get("links", []) or []:
        if (
            isinstance(link, dict)
            and link.get("src_title")
            and link.get("dst_id") is not None
            and link.get("relation")
        ):
            return True
    for field in ("workstream_proposals", "attestations"):
        value = obj.get(field)
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            return True
    return False

def _invoke_summarizer_once(
    user_msg: str,
    *,
    backend: str = "claude",
    timeout_s: float | None = None,
) -> tuple[str | None, str | None]:
    backend = _summarizer_backend(backend, default="claude")
    if backend == "codex":
        return _invoke_codex_once(user_msg, timeout_s=timeout_s or 600)
    if backend == "cursor":
        return _invoke_cursor_once(user_msg, timeout_s=timeout_s or 600)
    return _invoke_claude_once(user_msg, timeout_s=timeout_s or 180)


def _invoke_claude_once(
    user_msg: str,
    *,
    timeout_s: float = 180,
    claude_bin: str | None = None,
) -> tuple[str | None, str | None]:
    """Runs `claude -p --output-format json` once. Returns (stdout, error_reason).
    stdout is None on subprocess failure (not on parse failure)."""
    bin_path = claude_bin or CLAUDE_BIN
    env = os.environ.copy()
    # Set CLAUDE_KB_IN_COMPACT on the child so its own hooks (Stop / SessionStart
    # / SessionEnd) no-op and cannot recursively trigger more compactions.
    env["CLAUDE_KB_IN_COMPACT"] = "1"
    try:
        # Pass the prompt via stdin, not argv — large transcripts exceed Windows'
        # ~8KB CreateProcess/CMD command-line limit when using claude.cmd shim.
        proc = subprocess.run(
            [
                bin_path,
                "-p",
                "--no-session-persistence",
                "--output-format",
                "json",
                "--disallowedTools",
                CLAUDE_COMPACTOR_DISALLOWED_TOOLS,
            ],
            input=user_msg,
            capture_output=True, text=True, encoding="utf-8", timeout=timeout_s,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return None, f"{type(e).__name__}: {e}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return None, f"exit {proc.returncode}: {detail[:500]}"
    return proc.stdout, None


def _invoke_codex_once(
    user_msg: str,
    *,
    timeout_s: float = 600,
    codex_bin: str | None = None,
) -> tuple[str | None, str | None]:
    """Run `codex exec` once and return its final message text.

    The Codex backend intentionally runs in a temporary empty cwd, with an
    ephemeral read-only session and ignored user config. That keeps compaction
    from loading project AGENTS.md or re-entering latch hooks while it is merely
    acting as a summarizer.
    """
    bin_path = codex_bin or CODEX_BIN
    env = os.environ.copy()
    env["CLAUDE_KB_IN_COMPACT"] = "1"
    model = os.environ.get("CODEX_COMPACTOR_MODEL")
    try:
        with tempfile.TemporaryDirectory(prefix="latch-codex-compact-") as tmp:
            out_path = Path(tmp) / "last_message.txt"
            args = [
                bin_path,
                "exec",
                "--ignore-user-config",
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
                input=user_msg,
                capture_output=True, text=True, encoding="utf-8", timeout=timeout_s,
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            final_text = ""
            if out_path.exists():
                final_text = out_path.read_text(encoding="utf-8", errors="replace")
            if not final_text.strip():
                final_text = proc.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return None, f"{type(e).__name__}: {e}"

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return None, f"exit {proc.returncode}: {detail[-1000:]}"
    if not final_text.strip():
        return None, "empty codex final message"
    return final_text, None


def _invoke_cursor_once(
    user_msg: str,
    *,
    timeout_s: float = 600,
    cursor_bin: str | None = None,
) -> tuple[str | None, str | None]:
    """Run Cursor Agent in an isolated Ask-mode headless invocation."""
    model = os.environ.get("LATCH_COMPACTOR_CURSOR_MODEL") or os.environ.get("CURSOR_COMPACTOR_MODEL")
    text, error, _timed_out = cursor_backend.invoke_prompt(
        user_msg,
        timeout_s=timeout_s,
        purpose="compactor",
        agent_bin=cursor_bin,
        model=model,
    )
    return text, error


def _parse_json_envelope(raw: str) -> tuple[dict | None, str]:
    """Unwrap known CLI envelopes, then extract the inner JSON object.

    Claude's `--output-format json` wraps text in a `result` field; Codex's
    `--output-last-message` writes the final response directly. Returns
    (obj, error_description). obj is None iff parse failed.
    """
    raw = (raw or "").strip()
    if not raw:
        return None, "empty output"
    try:
        envelope = json.loads(raw)
        text = envelope.get("result") or envelope.get("response") or raw
    except json.JSONDecodeError:
        text = raw
    return _extract_json_object(text)


def _extract_json_object(text: str) -> tuple[dict | None, str]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None, "no JSON object delimiters found"
    try:
        return json.loads(text[start : end + 1]), ""
    except json.JSONDecodeError as e:
        return None, f"JSONDecodeError: {e}"


def _save_failed_compact(payload: dict, raw1: str | None, raw2: str | None, reason: str) -> None:
    """Archive the raw model output(s) and reason to
    projects/<cwd>/failed_compact/<timestamp>.txt for post-hoc inspection."""
    from datetime import datetime
    try:
        project_path = payload.get("project_path")
        project = paths.project_dir(project_path) if project_path else paths.KB_ROOT
        fail_dir = project / "failed_compact"
        fail_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        parts = [
            f"session_id: {payload.get('session_id')}",
            f"reason: {reason}",
            "",
            "--- first attempt raw output ---",
            raw1 if raw1 is not None else "(subprocess failed; no stdout)",
        ]
        if raw2 is not None:
            parts += ["", "--- repair attempt raw output ---", raw2]
        (fail_dir / f"{ts}.txt").write_text("\n".join(parts), encoding="utf-8")
    except Exception as e:
        _log(f"failed to archive failed_compact: {e}")


def _lifecycle_event_key(kind: str, candidate_key: str, session_id: str | None) -> str:
    material = json.dumps(
        [kind, str(candidate_key), str(session_id or "")],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"compact:{kind}:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _strict_id_list(value: Any) -> tuple[list[int], bool]:
    if value is None:
        return [], False
    if not isinstance(value, list):
        return [], True
    out: list[int] = []
    seen: set[int] = set()
    invalid = False
    for item in value:
        if isinstance(item, bool):
            invalid = True
            continue
        try:
            node_id = int(item)
        except (TypeError, ValueError):
            invalid = True
            continue
        if node_id not in seen:
            seen.add(node_id)
            out.append(node_id)
    return out, invalid


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _proposal_member_refs(proposal: Mapping[str, Any]) -> tuple[list[int], list[str], list[str]]:
    reasons: list[str] = []
    ids: list[int] = []
    seen_ids: set[int] = set()
    for key in ("seed_member_ids", "member_ids"):
        if key not in proposal:
            continue
        values, invalid = _strict_id_list(proposal.get(key))
        if invalid:
            reasons.append(f"invalid_{key}")
        for value in values:
            if value not in seen_ids:
                seen_ids.add(value)
                ids.append(value)

    titles: list[str] = []
    seen_titles: set[str] = set()
    raw_titles = proposal.get("member_titles")
    if raw_titles is not None:
        if not isinstance(raw_titles, list):
            reasons.append("invalid_member_titles")
        else:
            for item in raw_titles:
                title = item.strip() if isinstance(item, str) else ""
                if not title:
                    reasons.append("invalid_member_title")
                elif title not in seen_titles:
                    seen_titles.add(title)
                    titles.append(title)

    raw_members = proposal.get("members")
    if raw_members is not None:
        if not isinstance(raw_members, list):
            reasons.append("invalid_members")
        else:
            for item in raw_members:
                if isinstance(item, str):
                    title = item.strip()
                    if title and title not in seen_titles:
                        seen_titles.add(title)
                        titles.append(title)
                    elif not title:
                        reasons.append("invalid_member_title")
                    continue
                if not isinstance(item, Mapping):
                    reasons.append("invalid_member_ref")
                    continue
                value = item.get("id", item.get("node_id"))
                if value is not None:
                    parsed, invalid = _strict_id_list([value])
                    if invalid:
                        reasons.append("invalid_member_id")
                    for node_id in parsed:
                        if node_id not in seen_ids:
                            seen_ids.add(node_id)
                            ids.append(node_id)
                title = item.get("title")
                if title is not None:
                    clean = title.strip() if isinstance(title, str) else ""
                    if clean and clean not in seen_titles:
                        seen_titles.add(clean)
                        titles.append(clean)
                    elif not clean:
                        reasons.append("invalid_member_title")
    return ids, titles, reasons


def _resolve_proposal_members(
    conn,
    proposal: Mapping[str, Any],
    *,
    title_to_id: Mapping[str, int],
) -> tuple[list[int], list[str]]:
    member_ids, member_titles, reasons = _proposal_member_refs(proposal)
    title_ids: set[int] = set()
    for title in member_titles:
        mapped = title_to_id.get(title)
        if mapped is not None:
            title_ids.add(int(mapped))
            continue
        rows = conn.execute(
            "SELECT id FROM nodes WHERE title = ? AND status != 'stale' ORDER BY id",
            (title,),
        ).fetchall()
        if len(rows) != 1:
            reasons.append("unknown_or_ambiguous_member_title")
            continue
        title_ids.add(int(rows[0]["id"]))

    explicit_ids = set(member_ids)
    if explicit_ids and title_ids and explicit_ids != title_ids:
        reasons.append("member_title_id_mismatch")
    resolved = explicit_ids or title_ids
    if not resolved:
        reasons.append("missing_members")
        return [], sorted(set(reasons))

    placeholders = ",".join("?" for _ in resolved)
    rows = conn.execute(
        f"SELECT id, kind, status FROM nodes WHERE id IN ({placeholders})",
        sorted(resolved),
    ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    for node_id in sorted(resolved):
        row = by_id.get(node_id)
        if row is None:
            reasons.append("unknown_member_id")
        elif row["status"] == "stale":
            reasons.append("stale_member")
        elif row["kind"] in {"workstream", "priority"}:
            reasons.append(f"{row['kind']}_cannot_be_seed_member")
    return sorted(resolved), sorted(set(reasons))


def _charter_sections(body: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip(" \t-*#")
        label, separator, value = line.partition(":")
        normalized = label.strip().lower().replace("_", " ")
        if separator and value.strip() and normalized in {
            "objective", "done when", "scope boundary", "next step",
        }:
            found[normalized] = value.strip()
    return found


def _missing_charter_sections(body: str) -> list[str]:
    found = _charter_sections(body)
    required = ("objective", "done when", "scope boundary", "next step")
    return [label for label in required if label not in found]


def _eligible_contact_sessions(conn, member_ids: list[int]) -> set[str]:
    if not member_ids:
        return set()
    placeholders = ",".join("?" for _ in member_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT session_id FROM retrieval_events
        WHERE node_id IN ({placeholders})
          AND workstream_id_at_event IS NULL
          AND {lifecycle_signals.eligible_contact_sql()}
        """,
        member_ids,
    ).fetchall()
    return {str(row["session_id"]) for row in rows}


def _eligible_retrieval_event_sessions(
    conn, event_ids: list[int], member_ids: list[int],
) -> tuple[set[str], bool]:
    if not event_ids:
        return set(), True
    placeholders = ",".join("?" for _ in event_ids)
    rows = conn.execute(
        f"SELECT id, session_id, turn, node_id, source, "
        f"workstream_id_at_event FROM retrieval_events "
        f"WHERE id IN ({placeholders})",
        event_ids,
    ).fetchall()
    if len(rows) != len(set(event_ids)):
        return set(), False
    members = set(member_ids)
    sessions: set[str] = set()
    for row in rows:
        eligible = (
            int(row["node_id"]) in members
            and row["workstream_id_at_event"] is None
            and lifecycle_signals.is_eligible_contact_event(dict(row))
        )
        if not eligible:
            return set(), False
        sessions.add(str(row["session_id"]))
    return sessions, True


def _validated_shared_targets(
    conn, member_ids: list[int], target_ids: list[int],
) -> set[int]:
    if len(member_ids) < 2 or not target_ids:
        return set()
    member_marks = ",".join("?" for _ in member_ids)
    target_marks = ",".join("?" for _ in target_ids)
    relation_marks = ",".join("?" for _ in _FEEDER_RELATIONS)
    rows = conn.execute(
        f"""
        SELECT e.dst, COUNT(DISTINCT e.src) AS source_count
        FROM edges e
        JOIN nodes target ON target.id = e.dst
        WHERE e.src IN ({member_marks})
          AND e.dst IN ({target_marks})
          AND e.relation IN ({relation_marks})
          AND e.status = 'active'
          AND target.status != 'stale'
          AND target.kind IN ('decision', 'workstream')
        GROUP BY e.dst
        HAVING COUNT(DISTINCT e.src) >= 2
        """,
        [*member_ids, *target_ids, *sorted(_FEEDER_RELATIONS)],
    ).fetchall()
    return {int(row["dst"]) for row in rows}


def _validate_recurrence_evidence(
    conn,
    recurrence: Any,
    *,
    member_ids: list[int],
    candidate_signal: Mapping[str, Any],
) -> tuple[dict, list[str]]:
    if not isinstance(recurrence, Mapping):
        return {}, ["missing_recurrence_evidence"]
    reasons: list[str] = []
    actual_sessions = _eligible_contact_sessions(conn, member_ids)
    claimed_sessions = _signal_texts(recurrence, _SESSION_ID_KEYS)

    event_ids, invalid_events = _strict_id_list(
        recurrence.get("retrieval_event_ids", recurrence.get("event_ids")),
    )
    if invalid_events:
        reasons.append("invalid_retrieval_event_ids")
    event_sessions, events_valid = _eligible_retrieval_event_sessions(
        conn, event_ids, member_ids,
    )
    if not events_valid:
        reasons.append("invalid_retrieval_event_evidence")
    cited_sessions = set(claimed_sessions) | event_sessions
    if claimed_sessions.difference(actual_sessions):
        reasons.append("unverified_recurrence_session")

    target_ids = sorted(_signal_ids(recurrence, frozenset({"shared_target_ids"})))
    valid_targets = _validated_shared_targets(conn, member_ids, target_ids)
    if set(target_ids).difference(valid_targets):
        reasons.append("unverified_shared_target")
    candidate_evidence = _candidate_evidence_ids(candidate_signal)
    if target_ids and candidate_evidence and not set(target_ids).issubset(candidate_evidence):
        reasons.append("shared_target_not_in_open_candidate")

    if len(cited_sessions) < 2 and not valid_targets:
        reasons.append("insufficient_tier1_recurrence")
    return {
        "session_ids": sorted(cited_sessions),
        "retrieval_event_ids": event_ids,
        "shared_target_ids": sorted(valid_targets),
    }, sorted(set(reasons))


def _validate_workstream_proposal(
    conn,
    proposal: Mapping[str, Any],
    *,
    latest_open: Mapping[str, Any] | None,
    title_to_id: Mapping[str, int],
) -> tuple[dict | None, list[str]]:
    reasons: list[str] = []
    if "force" in proposal:
        reasons.append("force_not_allowed")
    proposal_key = str(proposal.get("proposal_key") or "").strip()
    candidate_key = str(proposal.get("candidate_key") or "").strip()
    title = str(proposal.get("title") or "").strip()
    charter_body = str(proposal.get("charter_body") or "").strip()
    if not proposal_key:
        reasons.append("missing_proposal_key")
    if not candidate_key:
        reasons.append("missing_candidate_key")
    if latest_open is None or candidate_key != latest_open.get("candidate_key"):
        reasons.append("not_latest_open_candidate")
    if not title:
        reasons.append("missing_title")
    if not charter_body:
        reasons.append("missing_charter_body")
    else:
        reasons.extend(
            f"missing_charter_{label.replace(' ', '_')}"
            for label in _missing_charter_sections(charter_body)
        )

    member_ids, member_reasons = _resolve_proposal_members(
        conn, proposal, title_to_id=title_to_id,
    )
    reasons.extend(member_reasons)
    signal = (
        latest_open.get("signal")
        if latest_open is not None and isinstance(latest_open.get("signal"), Mapping)
        else {}
    )
    candidate_members = _candidate_member_ids(conn, signal)
    if not candidate_members:
        reasons.append("open_candidate_missing_members")
    elif not set(member_ids).issubset(candidate_members):
        reasons.append("members_not_in_open_candidate")

    recurrence, recurrence_reasons = _validate_recurrence_evidence(
        conn,
        proposal.get("recurrence_evidence"),
        member_ids=member_ids,
        candidate_signal=signal,
    )
    reasons.extend(recurrence_reasons)
    if reasons:
        return None, sorted(set(reasons))
    charter = _charter_sections(charter_body)
    return {
        "proposal_key": proposal_key,
        "candidate_key": candidate_key,
        "title": title,
        "charter_body": charter_body,
        "objective": charter["objective"],
        "done_when": charter["done when"],
        "scope_boundary": charter["scope boundary"],
        "next_step": charter["next step"],
        "member_ids": member_ids,
        "recurrence_evidence": recurrence,
        "recurrence": {
            **recurrence,
            "session_count": len(recurrence["session_ids"]),
        },
        "proposal_validated": True,
        "proposal_source": "compactor",
    }, []


def _record_proposal_rejection(
    conn,
    *,
    session_id: str,
    proposal: Mapping[str, Any],
    reasons: list[str],
    latest_by_key: Mapping[str, Mapping[str, Any]],
    index: int,
) -> bool:
    supplied = str(proposal.get("candidate_key") or "").strip()
    candidate_key = supplied or (
        "invalid-proposal:"
        + hashlib.sha256(
            f"{session_id}:{index}:{proposal.get('proposal_key', '')}".encode("utf-8")
        ).hexdigest()
    )
    latest = latest_by_key.get(candidate_key)
    try:
        db.append_workstream_op_event(
            conn,
            event_key=_lifecycle_event_key("proposal", candidate_key, session_id),
            candidate_key=candidate_key,
            event_type="proposal_rejected",
            payload={
                "proposal_key": str(proposal.get("proposal_key") or "")[:200],
                "reasons": sorted(set(reasons)),
            },
            derivation_key=latest.get("derivation_key") if latest else None,
            session_id=session_id,
            require_latest_candidate=bool(latest),
        )
        return True
    except Exception as exc:
        _log(f"workstream proposal rejection event failed: {type(exc).__name__}")
        return False


def _apply_lifecycle_judgments(
    conn,
    session_id: str,
    result: Mapping[str, Any],
    *,
    title_to_id: Mapping[str, int],
) -> dict:
    """Validate model judgments and append events; never mutate workstreams."""
    latest = db.latest_workstream_derivation_candidates(conn)
    latest_by_key = {str(row["candidate_key"]): row for row in latest}
    lifecycle_events = 0
    attestations_recorded = 0
    proposals_accepted = 0
    proposals_rejected = 0

    for attestation in _mapping_list(result.get("attestations")):
        candidate_key = str(attestation.get("candidate_key") or "").strip()
        candidate = latest_by_key.get(candidate_key)
        verdict = str(attestation.get("verdict") or "").strip().lower()
        evidence_ids, invalid_ids = _strict_id_list(attestation.get("evidence_ids", []))
        if (
            candidate is None
            or str(candidate.get("op") or "").upper() not in {"MERGE", "CLOSE"}
            or verdict not in db.WORKSTREAM_EVENT_VERDICTS
            or invalid_ids
            or not evidence_ids
        ):
            continue
        signal = candidate.get("signal") if isinstance(candidate.get("signal"), Mapping) else {}
        allowed_ids = _candidate_evidence_ids(signal)
        if evidence_ids and not set(evidence_ids).issubset(allowed_ids):
            continue
        if evidence_ids:
            placeholders = ",".join("?" for _ in evidence_ids)
            found = conn.execute(
                f"SELECT COUNT(*) AS n FROM nodes WHERE id IN ({placeholders})",
                evidence_ids,
            ).fetchone()["n"]
            if int(found) != len(evidence_ids):
                continue
        try:
            db.append_workstream_op_event(
                conn,
                event_key=_lifecycle_event_key("attestation", candidate_key, session_id),
                candidate_key=candidate_key,
                event_type="attestation",
                verdict=verdict,
                payload={"evidence_ids": evidence_ids},
                derivation_key=candidate["derivation_key"],
                session_id=session_id,
                require_latest_candidate=True,
            )
        except Exception as exc:
            _log(f"workstream attestation event failed: {type(exc).__name__}")
            continue
        lifecycle_events += 1
        attestations_recorded += 1

    open_by_key = {
        key: row for key, row in latest_by_key.items()
        if str(row.get("op") or "").upper() == "OPEN"
    }
    for index, proposal in enumerate(
        _mapping_list(result.get("workstream_proposals")), start=1,
    ):
        candidate_key = str(proposal.get("candidate_key") or "").strip()
        validated, reasons = _validate_workstream_proposal(
            conn,
            proposal,
            latest_open=open_by_key.get(candidate_key),
            title_to_id=title_to_id,
        )
        if validated is None:
            if _record_proposal_rejection(
                conn,
                session_id=session_id,
                proposal=proposal,
                reasons=reasons,
                latest_by_key=latest_by_key,
                index=index,
            ):
                lifecycle_events += 1
                proposals_rejected += 1
            continue
        candidate = open_by_key[candidate_key]
        try:
            db.append_workstream_op_event(
                conn,
                event_key=_lifecycle_event_key("proposal", candidate_key, session_id),
                candidate_key=candidate_key,
                event_type="proposal_accepted",
                payload=validated,
                derivation_key=candidate["derivation_key"],
                session_id=session_id,
                require_latest_candidate=True,
            )
        except Exception as exc:
            _log(f"workstream proposal event failed: {type(exc).__name__}")
            continue
        lifecycle_events += 1
        proposals_accepted += 1

    return {
        "lifecycle_events": lifecycle_events,
        "attestations_recorded": attestations_recorded,
        "proposals_accepted": proposals_accepted,
        "proposals_rejected": proposals_rejected,
    }


def _apply_compaction(
    conn,
    session_id: str,
    result: dict,
    *,
    final: bool,
    prior_summary_id: int | None,
    project_path: str | None = None,
    offered_workstream_ids: set[int] | None = None,
) -> dict:
    summary = result.get("session_summary") or {}
    title = summary.get("title") or "Session summary"
    body = summary.get("body") or ""
    summary_status = "canonical" if final else "staging"

    summary_node_id = prior_summary_id
    summary_written = False
    if summary_node_id and body:
        vec = embeddings.to_blob(embeddings.embed(f"{title}\n\n{body}"))
        db.update_node(conn, summary_node_id, title=title, body=body, status=summary_status, embedding=vec)
        summary_written = True
    elif body:
        vec = embeddings.to_blob(embeddings.embed(f"{title}\n\n{body}"))
        summary_node_id = db.insert_node(
            conn, kind="progress", title=title, body=body,
            status=summary_status, session_id=session_id, embedding=vec,
        )
        summary_written = True

    title_to_id: dict[str, int] = {}
    written_node_ids: list[int] = []
    if summary_node_id and title:
        title_to_id[title] = summary_node_id
        if summary_written:
            written_node_ids.append(int(summary_node_id))

    inserted_nodes = 0
    for n in result.get("extracted_nodes", []) or []:
        if not isinstance(n, Mapping):
            continue
        kind = n.get("kind")
        if not isinstance(kind, str) or kind not in _EXTRACTED_NODE_KINDS:
            continue
        ntitle = n.get("title", "(untitled)")
        nbody = n.get("body", "")
        if not nbody:
            continue
        # workstream_id is optional and capability-scoped: the model may only
        # select an active workstream that was explicitly offered in its
        # related_kb_nodes context. Invalid ids are dropped and logged without
        # preserving transcript or node content.
        raw_ws_id = n.get("workstream_id")
        ws_id = raw_ws_id
        try:
            ws_id = (
                int(ws_id)
                if ws_id is not None and not isinstance(ws_id, bool)
                else None
            )
        except (TypeError, ValueError):
            ws_id = None
        reject_reason = None
        offered = offered_workstream_ids or set()
        if raw_ws_id is not None and ws_id is None:
            reject_reason = "not_integer"
        elif ws_id is not None and ws_id not in offered:
            reject_reason = "not_offered"
            ws_id = None
        elif ws_id is not None:
            resolution = workstreams.resolve_membership_target(conn, ws_id)
            if not resolution["ok"]:
                reject_reason = str(resolution.get("state") or "inactive")
                ws_id = None
            else:
                resolved_id = int(resolution["resolved_workstream_id"])
                if resolved_id != ws_id:
                    log_utils.emit_event(
                        "compactor",
                        {
                            "event": "workstream_tag_redirected",
                            "requested_workstream_id": ws_id,
                            "resolved_workstream_id": resolved_id,
                        },
                        project_path=project_path,
                        session_id=session_id,
                    )
                ws_id = resolved_id
        if reject_reason:
            log_utils.emit_event(
                "compactor",
                {
                    "event": "workstream_tag_rejected",
                    "reason": reject_reason,
                    "workstream_id": (
                        raw_ws_id if isinstance(raw_ws_id, int) and not isinstance(raw_ws_id, bool)
                        else None
                    ),
                },
                project_path=project_path,
                session_id=session_id,
            )
        # use_llm=False: compactor already spent one summarizer call; near-dups
        # get conservative keep_both here and are arbitrated by nightly heal.
        heal_result = heal.insert_with_heal(
            conn, kind=kind, title=ntitle, body=nbody, status="staging",
            session_id=session_id, use_llm=False, workstream_id=ws_id,
        )
        title_to_id[ntitle] = heal_result["id"]
        written_node_ids.append(int(heal_result["id"]))
        inserted_nodes += 1

    linked_edges = 0
    for link in result.get("links", []) or []:
        src_title = link.get("src_title")
        dst_id = link.get("dst_id")
        relation = link.get("relation")
        if not (src_title and dst_id and relation):
            continue
        canonical_relation = db.canonicalize_relation(str(relation))
        if canonical_relation in _LIFECYCLE_OWNED_EDGE_RELATIONS:
            continue
        src_id = title_to_id.get(src_title)
        if src_id is None:
            continue
        try:
            db.add_edge(
                conn, src=int(src_id), dst=int(dst_id), relation=str(relation),
                project_path=project_path, session_id=session_id,
            )
            linked_edges += 1
        except Exception as e:
            _log(f"edge insert failed: {e}")

    if written_node_ids:
        db.record_retrieval_events(
            conn,
            source="write",
            items=[(node_id, None) for node_id in written_node_ids],
            session_id=session_id,
        )

    lifecycle = _apply_lifecycle_judgments(
        conn,
        session_id,
        result,
        title_to_id=title_to_id,
    )

    return {
        "summary_node_id": summary_node_id,
        "summary_written": summary_written,
        "inserted_nodes": inserted_nodes,
        "linked_edges": linked_edges,
        **lifecycle,
    }


def _log(msg: str) -> None:
    log_path = paths.KB_ROOT / "compactor.log"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            from datetime import datetime
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


if __name__ == "__main__":
    # Manual invocation: python compactor.py <session_id> <project_path> [transcript_path] [--final]
    args = sys.argv[1:]
    final = "--final" in args
    args = [a for a in args if a != "--final"]
    if len(args) < 2:
        print("usage: compactor.py <session_id> <project_path> [transcript_path] [--final]")
        sys.exit(2)
    session_id = args[0]
    project_path = args[1]
    transcript_path = args[2] if len(args) >= 3 else None
    print(json.dumps(run_compaction(session_id, project_path, transcript_path, final=final)))
