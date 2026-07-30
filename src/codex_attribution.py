"""Recover session attribution for gate calls from Codex's own transcripts.

Codex-hosted gate calls carry no ``session_id``: the host does not expose a
per-request conversation identity to a reused MCP process, and the SessionStart
marker that would supply one is not written on every build (KB id=3152, id=4018).
Without attribution those rows are permanently unlabelable by the correlator, so
every measured number silently becomes single-host.

The recovery does not need the host's cooperation. A Codex rollout transcript
records the ``latch_gate`` tool call itself — request text included — inside the
thread whose id is in the filename and in the ``session_meta`` line. Hashing the
recorded request with the same function the gate log uses joins the two sides
exactly, and the thread id and transcript path fall out of the match.

Two properties make this trustworthy rather than a heuristic:

* **Content join, not proximity.** Matching is on the request hash (and, for
  rows written after the gate started returning it, the ``gate_call_id`` nonce),
  never on "whichever thread was running around then." Four concurrent Codex
  threads in one repo is normal, so a time-window guess would mis-attribute.
* **Unique-match-only.** An identical request issued from two threads, or the
  same request retried, yields more than one candidate. Those resolve to nothing
  rather than to a coin flip: honest absence beats confident wrong attribution
  (canonical id=1716; the Cursor precedent id=1493 → id=1525).

Read-only. Nothing here writes to the KB or to Codex's files.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import codex_transcript  # noqa: E402
import paths            # noqa: E402


GATE_TOOL_NAMES = frozenset({"latch_gate", "kb_gate"})

# Widest gap tolerated between the transcript's record of a gate call and the
# gate log's own timestamp when falling back to a hash join. Generous on
# purpose: the two clocks are the same machine, and the hash already carries the
# identifying weight — this only rejects a same-text call from a different day.
HASH_JOIN_TOLERANCE_SECONDS = 900

# gate_call_id as it appears inside a host-wrapped tool result. Anchored to the
# nonce's shape — sha1[:12], lowercase hex — so prose mentioning the key cannot
# be read as a value.
_NONCE_RE = re.compile(r'\\?"gate_call_id\\?"\s*:\s*\\?"([0-9a-f]{12})\\?"')
_NONCE_SHAPE_RE = re.compile(r'[0-9a-f]{12}')


def query_hash(request: str) -> str:
    """The gate log's ``query_hash``. Must stay byte-identical to
    ``gate._query_hash`` — the two are the join, so any divergence silently
    matches nothing. Kept local rather than imported so an offline attribution
    pass does not pull in the gate module and its model backends; a test pins
    the two against each other.

    Note the normalization: a whitespace-only request hashes as empty, but a
    request with padding is hashed AS-IS, not stripped."""
    normalized = "" if not (request or "").strip() else request
    return hashlib.sha1(
        normalized.encode("utf-8", errors="replace")
    ).hexdigest()[:12]


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _rollout_paths(
    home: Path | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[Path]:
    """Rollout files to scan, restricted to the relevant days when given.

    Codex partitions rollouts as ``sessions/YYYY/MM/DD/``, and a gate call can
    only appear in a rollout from around its own date, so an unbounded walk is
    both slow and pointless — this directory holds hundreds of multi-megabyte
    files. One day of slack on each side absorbs UTC/local skew and threads that
    span midnight.
    """
    root = (home or codex_transcript.codex_home()) / "sessions"
    if not root.exists():
        return []
    if start_date is None or end_date is None:
        return sorted(root.rglob("rollout-*.jsonl"))
    paths: list[Path] = []
    day = start_date - timedelta(days=1)
    last = end_date + timedelta(days=1)
    while day <= last:
        d = root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        if d.is_dir():
            paths.extend(sorted(d.glob("rollout-*.jsonl")))
        day += timedelta(days=1)
    return paths


def _transcript_project(path: Path) -> str | None:
    """The sanitized project key of the workspace a rollout belongs to.

    Read from the ``session_meta`` line's ``cwd`` and sanitized with the same
    function the gate log uses for its own ``project`` field, so the two are
    directly comparable. Without this, a rollout from an unrelated repo could
    satisfy a hash match — every Codex thread on the machine lives in one global
    CODEX_HOME, so the index is machine-wide unless it is scoped.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                cwd = payload.get("cwd") or obj.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    return paths.sanitize_cwd(cwd.strip())
    except OSError:
        return None
    return None


def _gate_calls_in_transcript(path: Path) -> list[dict]:
    """Every gate tool call recorded in one rollout, with its request hash and
    any ``gate_call_id`` the tool returned.

    The call and its output are separate lines joined by ``call_id``, so the
    output is scanned for the nonce and stitched back on. Older rows have no
    nonce — the gate did not return it — and fall back to the hash join.
    """
    out: list[dict] = []
    pending: dict[str, dict] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        if "latch_gate" not in line and "kb_gate" not in line:
            # Cheap prefilter: rollouts are large and almost none of their lines
            # concern the gate.
            if not pending:
                continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")

        if ptype in ("function_call", "custom_tool_call"):
            if payload.get("name") not in GATE_TOOL_NAMES:
                continue
            raw = payload.get("arguments")
            if not isinstance(raw, str):
                raw = payload.get("input")
            request = None
            if isinstance(raw, str) and raw:
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError:
                    decoded = None
                if isinstance(decoded, dict):
                    request = decoded.get("request")
            if not isinstance(request, str) or not request.strip():
                continue
            record = {
                "query_hash": query_hash(request),
                "ts": _parse_ts(obj.get("timestamp")),
                "gate_call_id": None,
            }
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id:
                pending[call_id] = record
            out.append(record)
            continue

        if ptype in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id")
            record = pending.pop(call_id, None) if isinstance(call_id, str) else None
            if record is None:
                continue
            nonce = _gate_call_id_in_output(payload.get("output"))
            if nonce:
                record["gate_call_id"] = nonce
    return out


def _iter_output_texts(output, depth: int = 0):
    """Yield every string buried in a recorded tool result, unwrapping as it goes.

    Real Codex outputs are triple-wrapped and the shape is not obvious from the
    schema: a plain string ``"Wall time: 22.2 seconds\\nOutput:\\n"`` followed by
    a JSON array, whose ``text`` field is *itself* a JSON document with escaped
    quotes. A parser written against the documented shape sees none of it —
    which is exactly how the first attempt at this matched 0 of 261 real
    outputs while its hand-written fixture passed.

    Depth-limited so a pathological nesting cannot spin.
    """
    if depth > 4:
        return
    if isinstance(output, str):
        yield output
        # A host prefix before the payload: keep only what follows the marker.
        marker = "Output:\n"
        idx = output.find(marker)
        candidate = output[idx + len(marker):] if idx >= 0 else output
        candidate = candidate.strip()
        if candidate[:1] in ("[", "{"):
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                return
            yield from _iter_output_texts(decoded, depth + 1)
        return
    if isinstance(output, list):
        for item in output:
            yield from _iter_output_texts(item, depth + 1)
        return
    if isinstance(output, dict):
        for key in ("text", "content", "output"):
            value = output.get(key)
            if value is not None:
                yield from _iter_output_texts(value, depth + 1)
        nonce = output.get("gate_call_id")
        if isinstance(nonce, str):
            yield nonce


def _gate_call_id_in_output(output) -> str | None:
    """Pull ``gate_call_id`` out of a recorded tool result, whatever shape the
    host wrapped it in. Returns None when absent — the normal case for calls
    made before the gate began returning it."""
    for text in _iter_output_texts(output):
        if not isinstance(text, str):
            continue
        if _NONCE_SHAPE_RE.fullmatch(text):
            # Already the bare value, surfaced by the structured unwrap.
            return text
        if "gate_call_id" not in text:
            continue
        # Escape-tolerant: the innermost payload arrives with its quotes
        # backslashed because it is JSON inside a JSON string.
        match = _NONCE_RE.search(text)
        if match:
            return match.group(1)
    return None


def build_index(
    home: Path | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    """Index gate calls recorded in Codex rollouts, bounded to a date range.

    Returns ``{"by_nonce": {...}, "by_hash": {...}}``. BOTH map to a LIST of
    candidates: a hash repeats legitimately, and a nonce can repeat across a
    replayed or resumed thread, so in either case the caller must be able to see
    the ambiguity and decline rather than take the last one written. Each candidate is
    ``{session_id, transcript_path, ts, gate_call_id}``.
    """
    by_nonce: dict[str, list[dict]] = {}
    by_hash: dict[str, list[dict]] = {}
    for path in _rollout_paths(home, start_date, end_date):
        session_id = codex_transcript.transcript_session_id(path)
        if not session_id:
            continue
        project = _transcript_project(path)
        for call in _gate_calls_in_transcript(path):
            candidate = {
                "session_id": session_id,
                "transcript_path": str(path),
                "ts": call["ts"],
                "gate_call_id": call["gate_call_id"],
                "project": project,
            }
            nonce = call["gate_call_id"]
            if nonce:
                # A LIST, not a single value. A replayed or resumed thread can
                # record the same call_id twice; last-write-wins would hand back
                # whichever rollout was scanned last and label it exact. The
                # ambiguity has to survive to the caller so it can decline.
                by_nonce.setdefault(nonce, []).append(candidate)
            by_hash.setdefault(call["query_hash"], []).append(candidate)
    return {"by_nonce": by_nonce, "by_hash": by_hash}


def _project_ok(candidate: dict, project: str | None) -> bool:
    """Whether a candidate may be matched against a gate row from `project`.

    Rejects a cross-project match outright. CODEX_HOME is machine-wide, so
    without this an identical request issued in another repo is a valid hash
    match — and the failure is silent and plausible-looking. A candidate whose
    own project could not be read is also rejected: unknown provenance is not
    the same as matching provenance.
    """
    if project is None:
        # The gate row itself has no project key; nothing to scope against, so
        # fall back to the unscoped behavior rather than rejecting everything.
        return True
    return candidate.get("project") == project


def attribute(
    gate_row: dict,
    index: dict,
    project: str | None = None,
) -> dict | None:
    """Attribute one session-less gate row to a Codex thread, or None.

    Nonce first — that is an exact identity. Hash second, and only when exactly
    one candidate sits inside the tolerance window: an ambiguous hash means a
    repeated request, which is the single most common gate flow (a MODIFY
    followed by a retry, KB id=3310), so guessing there would corrupt precisely
    the rows the measurement cares most about.

    `project` is the gate row's sanitized project key. Matching is scoped to it:
    CODEX_HOME is machine-wide, so an identical request from an unrelated repo
    would otherwise be a valid hash match.

    On success returns ``{session_id, transcript_path, source}`` where source is
    ``codex_transcript_nonce`` or ``codex_transcript_hash``.
    """
    nonce = gate_row.get("gate_call_id")
    if isinstance(nonce, str) and nonce:
        nonce_hits = [
            c for c in ((index.get("by_nonce") or {}).get(nonce) or [])
            if _project_ok(c, project)
        ]
        # Same unique-match rule as the hash path: a nonce seen in two threads
        # is not an exact identity, whatever its name suggests.
        if len({c["session_id"] for c in nonce_hits}) == 1:
            hit = nonce_hits[0]
            return {
                "session_id": hit["session_id"],
                "transcript_path": hit["transcript_path"],
                "source": "codex_transcript_nonce",
            }

    candidates = [
        c for c in ((index.get("by_hash") or {}).get(gate_row.get("query_hash")) or [])
        if _project_ok(c, project)
    ]
    if not candidates:
        return None
    gate_ts = _parse_ts(gate_row.get("ts"))
    if gate_ts is None:
        return None
    near = [
        c for c in candidates
        if c["ts"] is not None
        and abs((c["ts"] - gate_ts).total_seconds()) <= HASH_JOIN_TOLERANCE_SECONDS
    ]
    # Distinct threads, not distinct call records: the same thread recording the
    # same request twice is still unambiguous about WHICH thread it was.
    threads = {c["session_id"] for c in near}
    if len(threads) != 1:
        return None
    hit = min(near, key=lambda c: abs((c["ts"] - gate_ts).total_seconds()))
    return {
        "session_id": hit["session_id"],
        "transcript_path": hit["transcript_path"],
        "source": "codex_transcript_hash",
    }
