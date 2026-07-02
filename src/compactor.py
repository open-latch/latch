"""Compaction: summarize a session transcript into KB nodes via a model backend.

Called by hooks (Stop / SessionEnd / SessionStart-reconcile) and by the
/latch-compact slash command. Produces one session_summary node per session
(UPSERTed) plus extracted facts/decisions/entities (stacked).
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import artifacts  # noqa: E402
import budget  # noqa: E402
import codex_transcript  # noqa: E402
import db  # noqa: E402
import embeddings  # noqa: E402
import heal  # noqa: E402
import lockfile  # noqa: E402
import paths  # noqa: E402

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
SUPPORTED_SUMMARIZER_BACKENDS = {"claude", "codex"}

COMPACT_PROMPT = """You are summarizing a coding-agent session for a per-project knowledge base.

The KB stores nodes (facts, decisions, progress, entities, preferences, open_questions,
ideas) and typed edges between them. The goal is so a *future* agent session can pick
up where this one left off without the user having to re-explain.

You are given:
  1. Any prior summary for this same session (which you should COMBINE with new content,
     not duplicate). If this is the first compact, prior summary will be empty.
  2. Recent transcript content.
  3. A small sample of related KB nodes already known.

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

Workstream guidance:
- If any related_kb_nodes have kind='workstream' and a new node clearly
  belongs to one of them, set `workstream_id` to that workstream's id.
- If unsure or the connection is weak, leave `workstream_id` as null.
  Orphan nodes are tolerated; over-tagging is worse than under-tagging.
- Never invent a workstream_id that is not in related_kb_nodes.

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
- The system canonicalizes known synonyms on insert — `relates_to` becomes
  `related_to`, `requires` becomes `depends_on`. Don't worry about exact form.

Guidelines:
- Be specific. Prefer concrete facts over generalities.
- The session_summary REPLACES the prior summary — include everything still relevant
  from the prior summary plus the new content. Do not lose state.
- Only extract a node if it is reusable knowledge (would help a future session).
  Skip per-turn chatter.
- Skip links if you are unsure — empty list is fine.
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
        allowed, state = budget.check_and_record(project_path, category="nonheal")
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
        )
        summary_node_id = apply_result["summary_node_id"]
        write_count = (
            int(apply_result["summary_written"])
            + apply_result["inserted_nodes"]
            + apply_result["linked_edges"]
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

    Repair calls are separate `claude -p` / `codex exec` processes with no
    session memory, so references to "the original request" are not enough.
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
        if isinstance(node, dict) and (node.get("body") or "").strip():
            return True
    for link in obj.get("links", []) or []:
        if (
            isinstance(link, dict)
            and link.get("src_title")
            and link.get("dst_id") is not None
            and link.get("relation")
        ):
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


def _apply_compaction(
    conn,
    session_id: str,
    result: dict,
    *,
    final: bool,
    prior_summary_id: int | None,
    project_path: str | None = None,
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
    if summary_node_id and title:
        title_to_id[title] = summary_node_id

    inserted_nodes = 0
    for n in result.get("extracted_nodes", []) or []:
        kind = n.get("kind", "fact")
        ntitle = n.get("title", "(untitled)")
        nbody = n.get("body", "")
        if not nbody:
            continue
        # workstream_id is optional — LLM sets it when the new node clearly
        # belongs to a workstream visible in related_kb_nodes. Defensively
        # coerce to int / None in case the LLM emits a string or other type.
        ws_id = n.get("workstream_id")
        try:
            ws_id = int(ws_id) if ws_id is not None else None
        except (TypeError, ValueError):
            ws_id = None
        # use_llm=False: compactor already spent one summarizer call; near-dups
        # get conservative keep_both here and are arbitrated by nightly heal.
        heal_result = heal.insert_with_heal(
            conn, kind=kind, title=ntitle, body=nbody, status="staging",
            session_id=session_id, use_llm=False, workstream_id=ws_id,
        )
        title_to_id[ntitle] = heal_result["id"]
        inserted_nodes += 1

    linked_edges = 0
    for link in result.get("links", []) or []:
        src_title = link.get("src_title")
        dst_id = link.get("dst_id")
        relation = link.get("relation")
        if not (src_title and dst_id and relation):
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

    return {
        "summary_node_id": summary_node_id,
        "summary_written": summary_written,
        "inserted_nodes": inserted_nodes,
        "linked_edges": linked_edges,
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
