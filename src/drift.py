"""Deterministic nightly drift sweep — body-edge / state drift (id=1149 Part 3).

No LLM. Walks `staging` idea / open_question / decision nodes and flags two
drift kinds, then emits one `drift-<date>.log` JSONL row per finding:

  * ``orphan_mention`` — body has an ``id=X`` mention with no active edge
    to/from the node. Part 2's ``orphan_hint`` catches this prospectively at
    write time (heal.py:616); this catches nodes that predate Part 2 or whose
    hint was ignored, and re-surfaces it nightly until fixed.
  * ``stale_prereq`` — body references ``id=X`` in a *dependency* framing
    ("depends on id=X", "blocked by id=X", ...) where X is now ``canonical``
    AND no acknowledgement token (✓ / done / shipped / ...) sits near the
    mention. The prereq settled elsewhere but this body still reads as
    pending — the id=1111 / id=1097 axis from the id=1150 audit that a
    write-time hint structurally cannot catch.

Surface-only — NEVER mutates the DB (id=534 / id=825 surface-don't-redirect).
Structural-only rows — no titles, bodies, or excerpts (id=1091 §3 forbidden
list); a row pins the exact (node_id, referenced_id) so the agent kb_gets the
node to see context. Mirrors ``correlator.correlate()``'s shape:
``sweep(conn, project_path)`` -> counts dict.

Conventions: id=1091 / id=1108. Spec: id=1149 Part 3 / id=1157.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import log_utils  # noqa: E402


# Node kinds whose bodies act as long-lived "where are we" surfaces — the ones
# prone to body-edge / state drift over time. progress nodes are excluded: they
# are point-in-time ship records, not living specs.
DRIFT_SCAN_KINDS = ("idea", "open_question", "decision")

# Dependency-framing cues near a mention that mark it as a *pending* prereq
# reference (vs. a mere citation). Matched case-insensitively. Trailing spaces
# on short words ("once ", "after ") avoid matching inside other tokens.
_DEP_CUES = (
    "depends on", "depend on", "blocked by", "blocked on", "waiting on",
    "waiting for", "prereq", "prerequisite", "gated on", "unblocks",
    "once ", "after ", "before ", "pending ", "requires ", "needs ",
)
# Acknowledgement cues meaning the prereq is already settled — suppress the
# stale_prereq finding when any appears near the mention.
_ACK_CUES = (
    "✓", "✔", "done", "shipped", "complete", "landed", "merged",
    "resolved", "closed",
)
# Half-width (chars) of the text window scanned around a mention for cues.
_CUE_WINDOW = 100


def _has_cue(haystack: str, cues) -> bool:
    return any(c in haystack for c in cues)


def sweep(conn: sqlite3.Connection, project_path: str | None) -> dict:
    """Scan staging idea/open_question/decision nodes for body-edge + state
    drift and emit one drift.log row per finding. Read-only on the DB.

    `orphan_mention` takes precedence over `stale_prereq` for the same
    mention — an un-edged mention is reported as an orphan first; once edged,
    a later sweep can surface it as a stale prereq if it still applies.

    Returns counts: ``nodes_scanned``, ``orphan_mention``, ``stale_prereq``,
    ``rows_emitted``.
    """
    # Lazy import: heal pulls numpy/embeddings; keep this module (and the
    # SessionStart brief + CLI that import it) light until a sweep actually
    # runs. Reuse heal's exact code-span stripping + mention regex so the
    # exclusion mirrors orphan_hint precisely (kb_gate risk note, id=1149).
    from heal import _strip_code_spans, _ID_MENTION_RE

    counts = {
        "nodes_scanned": 0,
        "orphan_mention": 0,
        "stale_prereq": 0,
        "rows_emitted": 0,
    }
    placeholders = ",".join("?" for _ in DRIFT_SCAN_KINDS)
    nodes = conn.execute(
        f"SELECT id, kind, body FROM nodes "
        f"WHERE status = 'staging' AND kind IN ({placeholders})",
        DRIFT_SCAN_KINDS,
    ).fetchall()

    for node in nodes:
        counts["nodes_scanned"] += 1
        node_id = node["id"]
        scannable = _strip_code_spans(node["body"] or "")
        seen: set[int] = set()
        for m in _ID_MENTION_RE.finditer(scannable):
            rid = int(m.group(1))
            if rid == node_id or rid in seen:
                continue
            seen.add(rid)

            edged = conn.execute(
                "SELECT 1 FROM edges WHERE status = 'active' "
                "AND ((src = ? AND dst = ?) OR (src = ? AND dst = ?)) LIMIT 1",
                (node_id, rid, rid, node_id),
            ).fetchone()

            ref = conn.execute(
                "SELECT status FROM nodes WHERE id = ?", (rid,),
            ).fetchone()
            ref_status = ref["status"] if ref else None

            window = scannable[
                max(0, m.start() - _CUE_WINDOW):m.end() + _CUE_WINDOW
            ].lower()

            if edged is None:
                finding = "orphan_mention"
            elif (ref_status == "canonical"
                  and _has_cue(window, _DEP_CUES)
                  and not _has_cue(window, _ACK_CUES)):
                finding = "stale_prereq"
            else:
                continue

            counts[finding] += 1
            log_utils.emit_event(
                "drift",
                {
                    "node_id": node_id,
                    "node_kind": node["kind"],
                    "finding": finding,
                    "referenced_id": rid,
                    "referenced_status": ref_status,
                },
                project_path=project_path,
                session_id=None,
            )
            counts["rows_emitted"] += 1

    return counts


def latest_pending(
    project_path: str | None, lookback_days: int = 7,
) -> tuple[int, str | None]:
    """Distinct node_ids flagged by the MOST RECENT drift sweep within the
    last ``lookback_days``. Returns ``(count, date_str | None)``.

    Lightweight — reads only drift.log files (no DB, no heal import), so the
    SessionStart brief can call it on every session start without pulling the
    heavy heal/numpy import chain. The count is self-clearing: once the agent
    fixes the flagged nodes, the next nightly sweep stops emitting their rows
    and the count drops.
    """
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=lookback_days)
    by_date: dict[str, set[int]] = {}
    for R in log_utils.read_log_range("drift", start, today, project_path):
        nid = R.get("node_id")
        ds = (R.get("ts") or "")[:10]
        if nid is None or len(ds) != 10:
            continue
        by_date.setdefault(ds, set()).add(nid)
    if not by_date:
        return 0, None
    latest = max(by_date)
    return len(by_date[latest]), latest
