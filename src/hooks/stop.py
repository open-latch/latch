"""Stop hook: increments turn counter; auto-compacts every 5 user exchanges;
runs the mission-control cite-presence detector (Slice 3-B).

Runs after every assistant turn. Stays cheap: a single SQLite read/write,
then optionally spawn a detached compactor subprocess. For an actor bound to a
mission-control profile it additionally scans the just-finished assistant turn
for uncited current-value/code claims (KB id=1436) — a pure deterministic
regex pass, no LLM, no network — and queues an advisory next-turn nudge.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from _common import (
    hook_field, log, project_cwd, read_hook_input, session_id,
    spawn_compactor_detached, transcript_path,
)

from paths import UNLATCHED_MESSAGE, is_in_compact, is_unlatched_mode, is_write_disabled

COMPACT_EVERY_N_TURNS = 5

capture_streams = None
cite_detector = None
db = None
profiles = None
_RUNTIME_LOADED = False


def _load_runtime() -> None:
    global capture_streams, cite_detector, db, profiles, _RUNTIME_LOADED
    if _RUNTIME_LOADED:
        return
    import capture_streams as _capture_streams
    import cite_detector as _cite_detector
    import db as _db
    import profiles as _profiles

    capture_streams = _capture_streams
    cite_detector = _cite_detector
    db = _db
    profiles = _profiles
    _RUNTIME_LOADED = True


def main() -> int:
    # is_write_disabled() implies is_disabled(); covers both kill-switches.
    if is_unlatched_mode():
        _print_unlatched_context("Stop")
        return 0
    if is_write_disabled() or is_in_compact():
        return 0
    _load_runtime()
    payload = read_hook_input()
    sid = session_id(payload)
    if not sid:
        return 0
    cwd = project_cwd(payload)
    tpath = transcript_path(payload)

    try:
        conn = db.connect(cwd)
        try:
            db.upsert_session(conn, sid, cwd, tpath)
            turn = db.increment_turn(conn, sid)
            sess = db.get_session(conn, sid)
            last = sess["last_compact_turn"] if sess else 0
            should_compact = (turn - last) >= COMPACT_EVERY_N_TURNS
        finally:
            conn.close()
    except Exception as e:
        log(f"stop hook db error: {e}")
        return 0

    if should_compact:
        log(f"auto-compact: session={sid} turn={turn}")
        spawn_compactor_detached(sid, cwd, tpath, final=False)

    # Slice 3-B: deterministic cite-presence detection over the just-finished
    # turn. Isolated try/except — a detector fault must never break the Stop
    # hook (fail-open, like the rest of the pipeline).
    try:
        _cite_presence_check(sid, cwd, tpath)
    except Exception as e:
        log(f"stop hook cite-check error: {e}")

    # Local dev detector: a bounded, deterministic authority assertion scan.
    # No full trace or model call runs here; evidence is frozen and a detached
    # worker joins the broader receipts after the hook returns.
    if _detector_auto_enabled():
        try:
            _detector_authority_check(sid, cwd, tpath)
        except Exception as e:
            log(f"stop hook detector check error: {e}")

    return 0


# EXPERIMENTAL — mission-control / verification profiles. NOT recommended for use;
# planned to be unshipped to a separate branch later (observed unhelpful on
# pmeyer's workspace, 2026-06-10). See KB decision id=1550. Don't rely on / extend.
def _cite_presence_check(sid: str, cwd: str, tpath: str | None) -> None:
    """Scan the last assistant message for uncited current-value/code claims,
    but ONLY for a mission-control-bound actor (byte-identical no-op otherwise,
    KB id=1436). On a hit: emit a structural detection.log row and stash a
    pending cite-nudge for the next UserPromptSubmit to surface (advisory
    posture — no forced re-turn)."""
    _load_runtime()
    conn = db.connect(cwd)
    try:
        if not profiles.claim_backing_requires_code_trace(conn):
            return  # not mission control → no scan, no writes
        text = _last_assistant_text(tpath)
        if not text.strip():
            capture_streams.emit_detection_event(
                n_claims=0, n_flagged=0, action="none", scanned=False,
                project_path=cwd, session_id=sid,
            )
            return
        result = cite_detector.scan_message(text)
        n_flagged = result["n_flagged"]
        capture_streams.emit_detection_event(
            n_claims=result["n_claims"],
            n_flagged=n_flagged,
            action="nudge_queued" if n_flagged else "none",
            scanned=True,
            transcript_hash=hashlib.sha1(
                text.encode("utf-8", errors="replace")
            ).hexdigest()[:12],
            project_path=cwd,
            session_id=sid,
        )
        if n_flagged:
            db.set_pending_cite_nudge(conn, sid, n_flagged)
    finally:
        conn.close()


# Cap the transcript read: the last assistant message sits at the end of the
# JSONL, so a bounded tail read keeps the Stop hook's cost flat regardless of
# session length (priority id=1329 — no material latency growth). A final
# message larger than this just yields no nudge (fail-open), not a stall.
_TRANSCRIPT_TAIL_BYTES = 512 * 1024


def _last_assistant_text(tpath: str | None) -> str:
    """Concatenated text of the LAST assistant message in the transcript JSONL.

    Reads only the file's tail (`_TRANSCRIPT_TAIL_BYTES`) and discards the first
    (possibly partial) line. Tolerant of schema drift: a line may carry the
    message under `message` (role + a content string or a list of typed blocks)
    or flattened. Tool-use / thinking blocks are ignored; only `text` blocks
    count. Returns '' on any read/parse failure (fail-open)."""
    if not tpath:
        return ""
    p = Path(tpath)
    if not p.exists():
        return ""
    last = ""
    try:
        size = p.stat().st_size
        with p.open("rb") as fb:
            if size > _TRANSCRIPT_TAIL_BYTES:
                fb.seek(size - _TRANSCRIPT_TAIL_BYTES)
            raw = fb.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()
        if size > _TRANSCRIPT_TAIL_BYTES and lines:
            lines = lines[1:]  # drop the partial leading line
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else None
            role = (msg or {}).get("role") or obj.get("role") or obj.get("type")
            if role != "assistant":
                continue
            text = _extract_text((msg or obj).get("content"))
            if text.strip():
                last = text
    except Exception:
        return last
    return last


_CURRENT_NODE_REF = re.compile(
    r"\b(?:(?:latch|kb)\s+node(?:\s+id)?\s*[=:]?\s*|"
    r"node(?:\s+id)?\s*[=:]?\s*)(\d+)\b",
    re.IGNORECASE,
)
_CURRENT_AUTHORITY_WORD = re.compile(
    r"\b(current|canonical|authoritative|governing|live\s+path|active\s+decision)\b",
    re.IGNORECASE,
)
_NONCURRENT_WORD = re.compile(
    r"\b(not\s+current|noncurrent|stale|superseded|historical|old\s+path|"
    r"abandoned|rejected|"
    r"not\s+(?:currently\s+)?(?:the\s+)?(?:canonical|authoritative|governing|"
    r"live\s+path|active\s+decision)|"
    r"no\s+longer\s+(?:the\s+)?(?:current|canonical|authoritative|governing|"
    r"live\s+path|active\s+decision)|"
    r"(?:formerly|previously)\s+(?:the\s+)?(?:current|canonical|authoritative|"
    r"governing|live\s+path|active\s+decision))\b",
    re.IGNORECASE,
)
_NONASSERTIVE_AUTHORITY = re.compile(
    r"(?:^\s*(?:is|are|was|were|can|could|would|should|do|does)\b|"
    r"\b(?:if|whether|might|may|could|perhaps|possibly|maybe|"
    r"cannot\s+tell|can'?t\s+tell|unclear|unknown|check|verify|determine)\b)",
    re.IGNORECASE,
)


def _current_node_assertions(text: str, *, limit: int = 16) -> list[int]:
    """IDs affirmatively asserted as current, excluding history/code/quotes."""
    scrubbed = _scrub_authority_text(text)
    found: list[int] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", scrubbed):
        if (
            not _CURRENT_AUTHORITY_WORD.search(sentence)
            or _NONCURRENT_WORD.search(sentence)
            or _NONASSERTIVE_AUTHORITY.search(sentence)
            or sentence.rstrip().endswith("?")
        ):
            continue
        for match in _CURRENT_NODE_REF.finditer(sentence):
            node_id = int(match.group(1))
            if node_id not in found:
                found.append(node_id)
                if len(found) >= limit:
                    return found
    return found


def _explicit_node_refs(text: str, *, limit: int = 32) -> list[int]:
    """All explicit Latch/KB/node refs outside quoted or fenced material."""
    found: list[int] = []
    for match in _CURRENT_NODE_REF.finditer(_scrub_authority_text(text)):
        node_id = int(match.group(1))
        if node_id not in found:
            found.append(node_id)
            if len(found) >= limit:
                break
    return found


def _scrub_authority_text(text: str) -> str:
    scrubbed = re.sub(r"```.*?```", " ", text or "", flags=re.DOTALL)
    return "\n".join(
        line for line in scrubbed.splitlines() if not line.lstrip().startswith(">")
    )


def _detector_authority_check(sid: str, cwd: str, tpath: str | None) -> None:
    if not _detector_auto_enabled():
        return
    text = _last_assistant_text(tpath)
    node_ids = _current_node_assertions(text)
    if not node_ids:
        return
    _load_runtime()
    import detector_snapshot
    import detector_trigger
    import log_utils

    conn = db.connect(cwd)
    try:
        snapshots = detector_snapshot.snapshot_nodes(conn, node_ids, limit=16)
    finally:
        conn.close()
    cited_ids = set(_explicit_node_refs(text))
    bad: list[dict] = []
    for snap in snapshots:
        if snap.get("authority") == "STALE":
            bad.append(snap)
        elif snap.get("authority") == "RECONCILED":
            successors = {int(n) for n in snap.get("reconciled_by") or []}
            if not successors.intersection(cited_ids):
                bad.append(snap)
    if not bad:
        return
    prompt_hash = _last_user_prompt_hash(tpath)
    log_utils.emit_event(
        "detector_trigger",
        {
            "prompt_hash": prompt_hash,
            "triggers": ["corrected_node_cited_current"],
            "assistant_hash": hashlib.sha1(
                text.encode("utf-8", errors="replace")
            ).hexdigest()[:12],
            "node_snapshots": bad,
        },
        project_path=cwd,
        session_id=sid,
    )
    detector_trigger.queue(
        project_path=cwd,
        session_id=sid,
        transcript_path=tpath,
        prompt_hash=prompt_hash,
        trigger_types=["corrected_node_cited_current"],
        node_ids=[int(s["id"]) for s in bad],
    )


def _detector_auto_enabled() -> bool:
    adapter = os.environ.get("LATCH_ADAPTER", "").strip().lower()
    return (
        os.environ.get("LATCH_DEV_DETECTOR") == "1"
        and adapter in {"", "claude-code", "claude_code"}
    )


def _last_user_prompt_hash(tpath: str | None) -> str | None:
    """Hash the last human prompt from the same bounded transcript tail."""
    if not tpath:
        return None
    p = Path(tpath)
    if not p.exists():
        return None
    last = ""
    try:
        size = p.stat().st_size
        with p.open("rb") as handle:
            if size > _TRANSCRIPT_TAIL_BYTES:
                handle.seek(size - _TRANSCRIPT_TAIL_BYTES)
            raw = handle.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()
        if size > _TRANSCRIPT_TAIL_BYTES and lines:
            lines = lines[1:]
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else None
            role = (msg or {}).get("role") or obj.get("role") or obj.get("type")
            if role != "user":
                continue
            content = (msg or obj).get("content")
            # Tool-result-only user rows are not human prompts.
            if isinstance(content, list) and content and all(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            ):
                continue
            candidate = _extract_text(content)
            if candidate.strip():
                last = candidate
    except Exception:
        return None
    if not last:
        return None
    return hashlib.sha1(last.encode("utf-8", errors="replace")).hexdigest()[:12]


def _extract_text(content) -> str:
    """Pull text out of a message `content` that may be a plain string or a list
    of typed blocks ({'type': 'text', 'text': ...})."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _print_unlatched_context(event: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": UNLATCHED_MESSAGE,
        }
    }))


if __name__ == "__main__":
    sys.exit(main())
