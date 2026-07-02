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
import sys
from pathlib import Path

from _common import (
    hook_field, log, project_cwd, read_hook_input, session_id,
    spawn_compactor_detached, transcript_path,
)

import capture_streams
import cite_detector
import db
import profiles
from paths import is_in_compact, is_write_disabled

COMPACT_EVERY_N_TURNS = 5


def main() -> int:
    # is_write_disabled() implies is_disabled(); covers both kill-switches.
    if is_write_disabled() or is_in_compact():
        return 0
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


if __name__ == "__main__":
    sys.exit(main())
