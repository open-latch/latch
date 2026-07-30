"""Offline correlator — joins gate.log verdicts to in-session events.

Walks ``gate-<date>.log`` over a date range, and for each verdict row
builds a ``gate_outcome-<date>.log`` row capturing:

* ``outcome_category``: ``ACCEPTED`` / ``OVERRIDDEN`` / ``AMBIGUOUS`` /
  ``UNRESOLVED`` — derived from in-window kb_insert / kb_update activity
  and whether new edges link back to the verdict's cited ids.
* Follow-up counts: ``followup_count_inserts``, ``followup_count_updates``,
  ``followup_count_file_touches`` (files edited in-window — the shipped-diff signal),
  ``followup_count_reconciliations``, ``followup_count_corrections``.
* ``cited_ids_touched`` (Gap D signal) — how many of the verdict's cited
  nodes the agent referenced after the verdict, via session_retrievals.
* ``cited_ids_corrected`` — how many of the verdict's cited nodes were
  CORRECTED (appeared as a correction.log bad_node_id) in the window. The
  reward-attribution signal that the gate surfaced a node which turned out
  to be wrong (KB id=1151 / id=1159). Distinct cited ids, not event count.
* ``window_seconds`` — the truncated window width actually applied (may
  be < ``window_seconds`` argument when the next gate in the session or
  the session_end falls earlier).

Spec: KB id=1098. Conventions: KB id=1091 / id=1108. Structural-only
invariant — no titles, bodies, or raw prompt text in emitted rows.

Idempotent at the same ``correlator_version``: dedup keys on
``(gate_call_id, correlator_version)`` when the source gate row carries a
``gate_call_id`` nonce, falling back to ``(gate_query_hash, gate_ts,
correlator_version)`` for pre-nonce rows. The nonce is the only exact key:
repeated identical requests share a ``query_hash``, so a hash+timestamp key
collapses distinct calls on the single most common flow — a MODIFY followed by
a retry (KB id=3310). Bumping the version re-emits rows under the new
classification logic without colliding.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import artifacts          # noqa: E402
import codex_attribution  # noqa: E402
import db                 # noqa: E402
import log_utils          # noqa: E402


CORRELATOR_VERSION_DEFAULT = "0.3.0"  # 0.3.0: + shipped-diff file touches (id=3948 V1)
WINDOW_SECONDS_DEFAULT = 1800  # 30 minutes


# ---------- timestamp helpers ----------

def _parse_iso_ms(ts: str | None) -> datetime | None:
    """Parse gate.log's ``2026-05-27T15:23:32.245Z`` format. Returns a
    timezone-aware UTC datetime, or None if the input is unparseable."""
    if not ts:
        return None
    s = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_db_ts(dt: datetime) -> str:
    """Format a UTC datetime in db._now()'s ``%Y-%m-%d %H:%M:%S`` style
    for string comparison in SQL WHERE clauses."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------- dedup ----------

def _dedup_key(row: dict, *, version_key: str = "correlator_version") -> tuple | None:
    """Dedup identity for one outcome row, or None when it cannot be keyed.

    Prefers the ``gate_call_id`` nonce, which is unique per gate invocation.
    Only pre-nonce rows fall back to hash+timestamp, which cannot separate a
    repeated identical request from its retry (KB id=3310)."""
    version = row.get(version_key)
    if version is None:
        return None
    call_id = row.get("gate_call_id")
    if isinstance(call_id, str) and call_id:
        return ("call", call_id, version)
    query_hash = row.get("gate_query_hash")
    gate_ts = row.get("gate_ts")
    if query_hash is None or gate_ts is None:
        return None
    return ("hash", query_hash, gate_ts, version)


def _load_existing_keys(
    project_path, start_date: date, end_date: date,
) -> set[tuple]:
    """Walk existing gate_outcome.log rows in the range and build the dedup set.
    Bounds the range out by a few days on each side to catch session-crossing
    overlap, but the set is small enough to load fully into memory."""
    seen: set[tuple] = set()
    for R in log_utils.read_log_range(
        "gate_outcome", start_date, end_date, project_path,
    ):
        key = _dedup_key(R)
        if key is not None:
            seen.add(key)
    return seen


# ---------- window computation ----------

def _compute_window_end(
    conn: sqlite3.Connection,
    session_id: str,
    t0: datetime,
    window_seconds: int,
    next_gate_ts_in_session: datetime | None,
) -> datetime:
    """Window endpoint = min(t0 + window_seconds, next_gate_in_session, session_end).

    ``next_gate_ts_in_session`` is supplied by the caller because gate
    rows live in flat files; pre-collecting per-session next-ts lookups
    avoids re-walking the logs per row.
    """
    default_end = t0 + timedelta(seconds=window_seconds)
    candidates: list[datetime] = [default_end]
    if next_gate_ts_in_session is not None:
        candidates.append(next_gate_ts_in_session)

    row = conn.execute(
        "SELECT ended_at FROM sessions WHERE id = ?", (session_id,),
    ).fetchone()
    if row and row["ended_at"]:
        ended = db._parse_ts(row["ended_at"])
        if ended is not None:
            candidates.append(ended)

    end = min(candidates)
    if end < t0:
        end = t0
    return end


# ---------- event counts ----------

def _count_inserts(
    conn: sqlite3.Connection,
    session_id: str,
    t0: datetime,
    t_end: datetime,
    *,
    kind: str | None = None,
) -> int:
    """Distinct nodes inserted by ``session_id`` in [t0, t_end]. Optional
    kind filter (used by the classifier to look specifically for
    ``progress`` inserts)."""
    sql = (
        "SELECT COUNT(*) AS c FROM nodes "
        "WHERE session_id = ? AND created_at BETWEEN ? AND ?"
    )
    params: list = [session_id, _fmt_db_ts(t0), _fmt_db_ts(t_end)]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    return conn.execute(sql, params).fetchone()["c"]


def _count_updates(
    conn: sqlite3.Connection, t0: datetime, t_end: datetime,
) -> int:
    """Distinct nodes whose ``updated_at`` falls in [t0, t_end].

    ``nodes.updated_at`` is overwritten on each update (no history) and
    ``nodes.session_id`` is set at insert (not on update), so this counts
    DISTINCT nodes whose latest update landed in the window — NOT
    distinct update events. id=1098 clarification #2."""
    row = conn.execute(
        "SELECT COUNT(DISTINCT id) AS c FROM nodes "
        "WHERE updated_at BETWEEN ? AND ?",
        (_fmt_db_ts(t0), _fmt_db_ts(t_end)),
    ).fetchone()
    return row["c"]


def _count_reconciliations(
    project_path, session_id: str, t0: datetime, t_end: datetime,
) -> int:
    """Reconciliation events in ``session_id``'s window. Reads
    ``reconciliation-<date>.log`` files spanning [t0, t_end]."""
    start_date = t0.date()
    end_date = t_end.date()
    count = 0
    for R in log_utils.read_log_range(
        "reconciliation", start_date, end_date, project_path,
    ):
        if R.get("session_id") != session_id:
            continue
        ts = _parse_iso_ms(R.get("ts"))
        if ts is None:
            continue
        if t0 <= ts <= t_end:
            count += 1
    return count


def _correction_signals(
    project_path, session_id: str, cited_ids: list[int],
    t0: datetime, t_end: datetime,
) -> tuple[int, int]:
    """Return ``(corrections_total, cited_ids_corrected)`` for ``session_id``'s
    window. Reads ``correction-<date>.log`` files spanning [t0, t_end] in a
    single pass.

    * ``corrections_total`` — count of correction events (any bad node) the
      session fired in the window.
    * ``cited_ids_corrected`` — distinct verdict-cited ids that appeared as a
      correction's ``bad_node_id``; the reward-attribution signal that the
      gate surfaced a node which turned out to be wrong (KB id=1151 / id=1159).

    A correction is a human-labeled "the KB was wrong" event — the strongest
    negative reward signal in the four-stream substrate."""
    cited = set(cited_ids)
    total = 0
    corrected: set[int] = set()
    for R in log_utils.read_log_range(
        "correction", t0.date(), t_end.date(), project_path,
    ):
        if R.get("session_id") != session_id:
            continue
        ts = _parse_iso_ms(R.get("ts"))
        if ts is None or not (t0 <= ts <= t_end):
            continue
        total += 1
        bad = R.get("bad_node_id")
        if bad in cited:
            corrected.add(bad)
    return total, len(corrected)


def _count_cited_touches(
    conn: sqlite3.Connection,
    session_id: str,
    cited_ids: list[int],
    t0: datetime,
) -> int:
    """Distinct cited node ids ``session_id`` touched (via kb_get bump or
    UserPromptSubmit injection) on or after ``t0``. Uses
    ``session_retrievals.last_injected_at`` as the timestamp signal.
    id=1098 clarification #3."""
    if not cited_ids:
        return 0
    placeholders = ",".join("?" for _ in cited_ids)
    sql = (
        "SELECT COUNT(DISTINCT node_id) AS c FROM session_retrievals "
        f"WHERE session_id = ? AND node_id IN ({placeholders}) "
        "AND last_injected_at >= ?"
    )
    params: list = [session_id, *cited_ids, _fmt_db_ts(t0)]
    return conn.execute(sql, params).fetchone()["c"]


def _modify_links_to_cited(
    conn: sqlite3.Connection,
    session_id: str,
    cited_ids: list[int],
    t0: datetime,
    t_end: datetime,
) -> bool:
    """True iff at least one node inserted by ``session_id`` in window
    has an active outbound edge (also created in window) to any
    ``cited_id``. id=1098 clarification #4."""
    if not cited_ids:
        return False
    placeholders = ",".join("?" for _ in cited_ids)
    sql = (
        "SELECT 1 FROM edges e "
        "INNER JOIN nodes n ON e.src = n.id "
        "WHERE n.session_id = ? "
        "  AND n.created_at BETWEEN ? AND ? "
        f"  AND e.dst IN ({placeholders}) "
        "  AND e.status = 'active' "
        "  AND e.created_at BETWEEN ? AND ? "
        "LIMIT 1"
    )
    params: list = [
        session_id, _fmt_db_ts(t0), _fmt_db_ts(t_end),
        *cited_ids,
        _fmt_db_ts(t0), _fmt_db_ts(t_end),
    ]
    return conn.execute(sql, params).fetchone() is not None


def _count_file_touches(
    conn: sqlite3.Connection,
    session_id: str,
    project_path: str | None,
    t0: datetime,
    t_end: datetime,
    transcript_path: str | None = None,
) -> int:
    """Count distinct files this session edited inside [t0, t_end] — the
    "shipped diff" signal (id=3948 V1). Grounds a verdict's outcome in code that
    actually moved rather than in a nearby KB write. Failure-isolated: any
    missing transcript or parse error yields 0, matching this module's posture of
    never letting observation break a label.

    `transcript_path` lets a caller supply the transcript directly. Attribution
    recovered from a Codex rollout already knows the file, and most Codex threads
    have no `sessions` row to look it up in — the only Codex row-creator is a
    manual `/latch-compact`. Without this the signal would stay blind on exactly
    the sessions attribution just recovered."""
    tpath = transcript_path
    if not tpath:
        try:
            row = conn.execute(
                "SELECT transcript_path FROM sessions WHERE id = ?", [session_id],
            ).fetchone()
        except sqlite3.Error:
            return 0
        if not row:
            return 0
        tpath = row["transcript_path"] if "transcript_path" in row.keys() else row[0]
    if not tpath:
        return 0
    try:
        return len(artifacts.observe_session_artifacts_in_window(
            tpath, project_path, t0, t_end,
        ))
    except Exception:
        return 0


# ---------- outcome classification ----------

def _classify(
    conn: sqlite3.Connection,
    gate_row: dict,
    session_id: str,
    t0: datetime,
    t_end: datetime,
    project_path: str | None = None,
    file_touches: int | None = None,
) -> str:
    """Map (verdict, in-window activity) → outcome label. Closed set:
    ACCEPTED / OVERRIDDEN / AMBIGUOUS / UNRESOLVED."""
    verdict = gate_row.get("recommendation")
    cited_ids = [int(i) for i in (gate_row.get("evidence_ids") or [])]
    inserts_total = _count_inserts(conn, session_id, t0, t_end)
    progress_inserts = _count_inserts(
        conn, session_id, t0, t_end, kind="progress",
    )
    if file_touches is None:
        file_touches = _count_file_touches(
            conn, session_id, project_path, t0, t_end,
        )

    if verdict == "PROCEED":
        return "ACCEPTED" if progress_inserts > 0 else "AMBIGUOUS"

    if verdict == "MODIFY":
        if inserts_total == 0:
            # No KB write. Shipped code in-window is still evidence the work
            # went on without recording the ruling, so it reads as OVERRIDDEN
            # rather than unknowable. With no code and no write there is
            # genuinely nothing to infer from, and that stays AMBIGUOUS by
            # founder ruling (id=3985) — relabelling it UNRESOLVED would have
            # satisfied the ≤20% bar by renaming rather than by inferring.
            if file_touches > 0:
                return "OVERRIDDEN"
            return "AMBIGUOUS"
        if _modify_links_to_cited(conn, session_id, cited_ids, t0, t_end):
            return "ACCEPTED"
        return "OVERRIDDEN"

    if verdict == "DO_NOT_PROCEED":
        return "OVERRIDDEN" if progress_inserts > 0 else "ACCEPTED"

    if verdict == "NEEDS_HUMAN_JUDGMENT":
        updates_in_window = _count_updates(conn, t0, t_end)
        if inserts_total > 0 or updates_in_window > 0:
            return "ACCEPTED"
        return "UNRESOLVED"

    return "AMBIGUOUS"


# ---------- public entry point ----------

def correlate(
    project_path: str | None,
    start_date: date,
    end_date: date,
    *,
    window_seconds: int = WINDOW_SECONDS_DEFAULT,
    correlator_version: str = CORRELATOR_VERSION_DEFAULT,
) -> dict:
    """Walk gate.log in [start_date, end_date] and emit one
    gate_outcome.log row per non-skipped, session-tagged, not-yet-seen
    verdict.

    Returns counts dict: ``rows_emitted``, ``rows_skipped_no_session_id``,
    ``rows_skipped_dedup``, ``rows_skipped_skipped_verdict``.
    """
    counts = {
        "rows_emitted": 0,
        "rows_skipped_no_session_id": 0,
        # Split out from rows_skipped_no_session_id, which was incremented at
        # two unrelated sites and so could not serve as a clean denominator
        # component for the coverage metric.
        "rows_skipped_unparseable_ts": 0,
        # Rows the host left session-less that were recovered from a Codex
        # rollout by content join. Reported separately so coverage can always
        # be split into host-supplied vs recovered identity.
        "rows_attributed_from_transcript": 0,
        "rows_skipped_dedup": 0,
        "rows_skipped_skipped_verdict": 0,
    }
    conn = db.connect(project_path or "")
    try:
        seen = _load_existing_keys(project_path, start_date, end_date)

        # Built at most once per run, and only if a session-less row actually
        # turns up: the rollout directory holds hundreds of multi-megabyte files,
        # so an unconditional scan would tax every correlation of an
        # all-attributed range. Bounded to the correlated dates.
        attribution_index: dict | None = None

        def _attribution() -> dict:
            nonlocal attribution_index
            if attribution_index is None:
                try:
                    attribution_index = codex_attribution.build_index(
                        None, start_date, end_date,
                    )
                except Exception:
                    attribution_index = {"by_nonce": {}, "by_hash": {}}
            return attribution_index

        all_rows = list(log_utils.read_log_range(
            "gate", start_date, end_date, project_path,
        ))
        next_ts_by_idx: dict[int, datetime | None] = _build_next_in_session_map(
            all_rows,
        )

        for idx, R in enumerate(all_rows):
            if R.get("skipped"):
                counts["rows_skipped_skipped_verdict"] += 1
                continue
            session_id = R.get("session_id")
            session_source = "host_supplied"
            transcript_path = None
            if not session_id:
                # The host gave no identity. Recover it from its own transcript
                # by content join rather than writing the row off — otherwise
                # every measured number is silently single-host (id=4018).
                # Declines on an ambiguous match; see codex_attribution.
                hit = codex_attribution.attribute(R, _attribution())
                if hit is None:
                    counts["rows_skipped_no_session_id"] += 1
                    continue
                session_id = hit["session_id"]
                transcript_path = hit["transcript_path"]
                session_source = hit["source"]
                counts["rows_attributed_from_transcript"] += 1
            # Build the dedup identity from the SOURCE gate row, mapped onto the
            # same field names the emitted outcome row uses, so both sides of
            # the comparison key on the nonce whenever it exists.
            key = _dedup_key({
                "gate_call_id": R.get("gate_call_id"),
                "gate_query_hash": R.get("query_hash"),
                "gate_ts": R.get("ts"),
                "correlator_version": correlator_version,
            })
            if key is not None and key in seen:
                counts["rows_skipped_dedup"] += 1
                continue
            t0 = _parse_iso_ms(R.get("ts"))
            if t0 is None:
                counts["rows_skipped_unparseable_ts"] += 1
                continue
            t_end = _compute_window_end(
                conn, session_id, t0, window_seconds, next_ts_by_idx.get(idx),
            )

            followup_inserts = _count_inserts(conn, session_id, t0, t_end)
            followup_updates = _count_updates(conn, t0, t_end)
            followup_recons = _count_reconciliations(
                project_path, session_id, t0, t_end,
            )
            cited_ids = [int(i) for i in (R.get("evidence_ids") or [])]
            cited_touched = _count_cited_touches(
                conn, session_id, cited_ids, t0,
            )
            followup_corrections, cited_corrected = _correction_signals(
                project_path, session_id, cited_ids, t0, t_end,
            )
            file_touches = _count_file_touches(
                conn, session_id, project_path, t0, t_end,
                transcript_path=transcript_path,
            )
            outcome = _classify(
                conn, R, session_id, t0, t_end,
                project_path=project_path, file_touches=file_touches,
            )

            log_utils.emit_event(
                "gate_outcome",
                {
                    "gate_ts": R.get("ts"),
                    "gate_query_hash": R.get("query_hash"),
                    # The exact join key (id=3310). query_hash collides across
                    # a repeated request and its retry; the nonce does not.
                    "gate_call_id": R.get("gate_call_id"),
                    # Closed-set label: host_supplied / codex_transcript_nonce /
                    # codex_transcript_hash. Makes a coverage number splittable
                    # by identity provenance instead of blended.
                    "session_source": session_source,
                    "verdict": R.get("recommendation"),
                    "outcome_category": outcome,
                    "followup_count_inserts": followup_inserts,
                    "followup_count_updates": followup_updates,
                    "followup_count_reconciliations": followup_recons,
                    "followup_count_corrections": followup_corrections,
                    # Shipped-diff signal: a COUNT of files edited in-window,
                    # never the paths. Paths are user content (they leak repo
                    # layout and project subject matter) and have no place in a
                    # structural stream.
                    "followup_count_file_touches": file_touches,
                    "cited_ids_total": len(cited_ids),
                    "cited_ids_touched": cited_touched,
                    "cited_ids_corrected": cited_corrected,
                    "window_seconds": int((t_end - t0).total_seconds()),
                    "correlator_version": correlator_version,
                },
                project_path=project_path,
                session_id=session_id,
                log_date=t0.date(),
            )
            counts["rows_emitted"] += 1
            seen.add(key)  # type: ignore[arg-type]
    finally:
        conn.close()
    return counts


def _build_next_in_session_map(
    gate_rows: list[dict],
) -> dict[int, datetime | None]:
    """For each gate row index, look up the timestamp of the NEXT gate
    in the same session (or None if it's the session's last gate in the
    scan window). Used to truncate the attribution window so a later
    gate's outcome isn't attributed to the earlier verdict."""
    by_session: dict[str, list[tuple[int, datetime]]] = {}
    for idx, R in enumerate(gate_rows):
        sid = R.get("session_id")
        if not sid:
            continue
        ts = _parse_iso_ms(R.get("ts"))
        if ts is None:
            continue
        by_session.setdefault(sid, []).append((idx, ts))

    next_ts: dict[int, datetime | None] = {}
    for rows in by_session.values():
        rows.sort(key=lambda pair: pair[1])
        for i, (idx, _) in enumerate(rows):
            next_ts[idx] = rows[i + 1][1] if i + 1 < len(rows) else None
    return next_ts
