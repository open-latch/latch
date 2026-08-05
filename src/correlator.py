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

This legacy correlator emits diagnostic outcomes only. Canonical v2.6 receipts
are minted exclusively by ``outcome_measurement.measure/fold_observations``
after the required snapshot and freshness drain. Diagnostic rows are idempotent
per ``(gate_call_id, correlator_version)`` so corrected 0.5.0 rows backfill over
bad 0.4.0 output without masquerading as finalized protocol receipts.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import artifacts          # noqa: E402
import codex_attribution  # noqa: E402
import db                 # noqa: E402
import log_utils          # noqa: E402
import outcome_measurement  # noqa: E402
import project_proof      # noqa: E402


CORRELATOR_VERSION_DEFAULT = "0.5.0"  # diagnostic boundary/classification repair
MEASUREMENT_PROTOCOL_VERSION_DEFAULT = outcome_measurement.MEASUREMENT_PROTOCOL_VERSION
PROJECT_KEY_EPOCH_DEFAULT = "outcome-v2.6-key-1"
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

def _dedup_key(
    row: dict,
    *,
    version_key: str = "correlator_version",
) -> tuple | None:
    """Diagnostic dedup identity for one outcome row, or None if unkeyed.

    ``gate_call_id`` is the invocation identity, but this stream is not a
    canonical measurement generation. Hash-plus-time remains only a legacy
    diagnostic idempotency fallback for historical pre-nonce rows."""
    version = row.get(version_key)
    if version is None:
        return None
    call_id = row.get("gate_call_id")
    if isinstance(call_id, str) and call_id:
        return ("diagnostic_call", call_id, version)
    return None


def _legacy_diagnostic_key(row: dict) -> tuple | None:
    """Idempotency key for non-measurement, pre-nonce diagnostic outcomes.

    These rows remain useful for the historical gate report, but are explicitly
    outside the v2.6 receipt generation and never enter measurement arithmetic.
    """
    query_hash = row.get("gate_query_hash")
    gate_ts = row.get("gate_ts")
    version = row.get("correlator_version")
    if query_hash is None or gate_ts is None or version is None:
        return None
    return ("legacy_diagnostic", query_hash, gate_ts, version)


def _load_existing_keys(
    project_path,
    start_date: date,
    end_date: date,
    correlator_version: str,
) -> set[tuple]:
    """Walk existing gate_outcome.log rows in the range and build the dedup set.
    Bounds the range out by a few days on each side to catch session-crossing
    overlap, but the set is small enough to load fully into memory."""
    seen: set[tuple] = set()
    for R in log_utils.read_log_range(
        "gate_outcome", start_date, end_date, project_path,
    ):
        if R.get("correlator_version") == correlator_version:
            key = _dedup_key(R) or _legacy_diagnostic_key(R)
        else:
            key = _legacy_diagnostic_key(R)
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
) -> int | None:
    """Count distinct files this session edited inside [t0, t_end] — the
    "shipped diff" signal (id=3948 V1). Grounds a verdict's outcome in code that
    actually moved rather than in a nearby KB write. Failure-isolated: any
    A missing/unreadable/malformed/changing transcript returns ``None`` so the
    caller censors with ``evidence_unavailable``.  A valid transcript containing
    no successful edits returns the distinct observed value ``0``.

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
            return None
        if not row:
            return None
        tpath = row["transcript_path"] if "transcript_path" in row.keys() else row[0]
    if not tpath:
        return None
    path = Path(tpath)
    try:
        before = path.stat()
        first = path.read_bytes()
        decoded = first.decode("utf-8")
        for line in decoded.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                return None
        count = len(artifacts.observe_session_artifacts_in_window(
            tpath, project_path, t0, t_end,
        ))
        second = path.read_bytes()
        after = path.stat()
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None
    if (
        first != second
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or getattr(before, "st_ino", None) != getattr(after, "st_ino", None)
    ):
        return None
    return count


def _boundary_censor_reason(
    loss_markers_by_session: dict[str, list[datetime | None]],
    session_id: str,
    t0: datetime,
    t_end: datetime,
) -> str | None:
    """Return a censor reason for exact-session boundary loss, if any.

    Unknown-session and merely same-project records are deliberately absent:
    they cannot prove membership in this session and therefore can neither
    truncate nor censor its window.  A loss marker carrying exact session
    evidence *can* make the boundary unknowable.  Unplaceable markers censor
    conservatively; timestamped markers censor only windows they straddle.
    """
    for ts in loss_markers_by_session.get(session_id) or []:
        if ts is None:
            return "boundary_uncertain"
        if t0 < ts <= t_end:
            return "boundary_uncertain"
    return None


def _transcript_calls_by_session(
    index: dict,
    *,
    target_project_proof: dict | None = None,
) -> dict[str, list[dict]]:
    """Structural transcript gate calls grouped by their exact session.

    Attribution indexes must expose the structural ``by_session`` stream
    directly. There is no hash-index compatibility path in v2.6. No request
    text or path is emitted; this map exists only to establish exact-session
    order.
    """
    def _normalized(item: dict) -> dict:
        copied = dict(item)
        if not isinstance(copied.get("ts"), datetime):
            copied["ts"] = _parse_iso_ms(copied.get("ts"))
        if isinstance(copied.get("source_order"), list):
            copied["source_order"] = tuple(copied["source_order"])
        return copied

    def _order(item: dict) -> tuple:
        return (
            item.get("ts") or datetime.max.replace(tzinfo=timezone.utc),
            item.get("source_order") or (2**31, 2**63),
            str(item.get("gate_call_id") or ""),
        )

    def _belongs_to_target(item: dict) -> bool:
        if target_project_proof is None:
            return True
        if item.get("project_check") == project_proof.PROJECT_MATCH:
            return True
        return project_proof.compare_project_proofs(
            item.get("project_proof"), target_project_proof,
        ) == project_proof.PROJECT_MATCH

    direct = (
        index.get("by_session")
        or index.get("session_calls")
        or index.get("by_session_calls")
    )
    if isinstance(direct, dict):
        return {
            str(session_id): sorted(
                [
                    _normalized(item)
                    for item in calls
                    if isinstance(item, dict) and _belongs_to_target(item)
                ],
                key=_order,
            )
            for session_id, calls in direct.items()
            if isinstance(calls, list)
        }
    # v2.6 has no hash-index fallback. Without the explicit structural stream,
    # boundary completeness is unproven and the caller censors.
    return {}


def _next_transcript_gate_ts(
    calls_by_session: dict[str, list[dict]],
    *,
    session_id: str,
    current_ts: datetime,
    current_call_id: str | None,
    current_source_order: tuple | None = None,
) -> datetime | None:
    """Next gate recorded by this exact Codex session's transcript.

    The transcript stream is authoritative for recovered rows and includes gate
    calls that never matched a gate-log row, including skipped/degraded calls.
    """
    calls = calls_by_session.get(session_id) or []
    current_key: tuple = (
        current_ts,
        current_source_order or (-1, -1),
        str(current_call_id or ""),
    )
    if current_call_id:
        matching = [
            item for item in calls
            if item.get("gate_call_id") == current_call_id
        ]
        if matching:
            current_item = min(
                matching,
                key=lambda item: (
                    item.get("ts") or datetime.max.replace(tzinfo=timezone.utc),
                    item.get("source_order") or (2**31, 2**63),
                ),
            )
            current_key = (
                current_item.get("ts") or current_ts,
                current_item.get("source_order") or (-1, -1),
                str(current_call_id),
            )

    for item in calls:
        ts = item.get("ts")
        if not isinstance(ts, datetime):
            continue
        call_id = item.get("gate_call_id")
        if current_call_id and call_id == current_call_id:
            continue
        item_key = (
            ts,
            item.get("source_order") or (2**31, 2**63),
            str(call_id or ""),
        )
        if item_key > current_key:
            return ts
    return None


def _candidate_index_complete(index: dict) -> bool:
    receipt = index.get("candidate_completeness")
    if isinstance(receipt, dict):
        return receipt.get("complete") is True
    return bool(getattr(receipt, "complete", False))


def _host_observation_payload(row: outcome_measurement.Observation) -> dict:
    # Use the same complete semantic projection as recovered Codex candidates;
    # host-supplied session identity must not hide a same-nonce S2 conflict.
    payload = codex_attribution._observation_metadata(row)
    payload["source_order"] = (0, row.byte_offset)
    return payload


def _host_parser_config(
    start_date: date,
    end_date: date,
    *,
    target_project_proof: dict | None,
    project_key_epoch: str | None,
    pinned_runtime_version: str | None,
    measurement_protocol_version: str,
) -> outcome_measurement.MeasurementConfig:
    t0 = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    # This config is used only to parse already date-filtered diagnostic input.
    # Preserve the canonical fixed 21-day measurement cap; the caller's
    # requested report end remains a separate selection boundary.
    cap = t0 + timedelta(days=21)
    proof = target_project_proof or {
        "version": project_proof.PROJECT_PROOF_VERSION,
        "key_epoch": "correlator-unscoped",
        "fingerprint": "0" * 64,
    }
    return outcome_measurement.MeasurementConfig(
        t0=t0,
        cap=cap,
        target_project_proof=proof,
        key_epoch=project_key_epoch or "correlator-unscoped",
        pinned_runtime_version=pinned_runtime_version or "correlator-unpinned",
        measurement_protocol_version=measurement_protocol_version,
        require_fresh_snapshots=False,
    )


def _host_session_stream(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    config: outcome_measurement.MeasurementConfig,
    proof_context: project_proof.ProjectProofContext | None,
    target_project_proof: dict | None,
) -> dict:
    """Parse one host session's S2 stream from one stable transcript snapshot."""
    try:
        row = conn.execute(
            "SELECT project_path, transcript_path FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is None or not row["transcript_path"]:
        return {
            "transcript_path": None,
            "calls": [],
            "by_nonce": {},
            "complete": False,
            "loss_markers": [],
        }
    transcript_path = str(row["transcript_path"])
    segment_proof = None
    if proof_context is not None and row["project_path"]:
        try:
            segment_proof = proof_context.prove(str(row["project_path"]))
        except (TypeError, ValueError, OSError):
            segment_proof = None
    if target_project_proof is not None and project_proof.compare_project_proofs(
        segment_proof, target_project_proof,
    ) != project_proof.PROJECT_MATCH:
        # A foreign or unknown-project segment with a colliding session id is
        # never in the target session stream and cannot bound its windows.
        return {
            "transcript_path": transcript_path,
            "calls": [],
            "by_nonce": {},
            "complete": segment_proof is not None,
            "loss_markers": [],
        }
    path = Path(transcript_path)
    try:
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
    except OSError:
        return {
            "transcript_path": transcript_path,
            "calls": [],
            "by_nonce": {},
            "complete": False,
            "loss_markers": [],
        }
    stable = (
        before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and getattr(before, "st_ino", None) == getattr(after, "st_ino", None)
    )
    try:
        observations, markers = outcome_measurement.parse_host_record_bytes(
            data,
            file=transcript_path,
            config=config,
            project_proof_context=proof_context,
            vault_key=None,
        )
    except (TypeError, ValueError):
        observations, markers = [], []
        stable = False

    calls: list[dict] = []
    by_nonce: dict[str, list[dict]] = {}
    for observation in observations:
        if observation.session_id and observation.session_id != session_id:
            continue
        payload = _host_observation_payload(observation)
        item = {
            "session_id": session_id,
            "ts": observation.ts,
            "gate_call_id": observation.nonce,
            "skipped": observation.skipped,
            "adapter": observation.adapter,
            "project_proof": segment_proof,
            "host_observation": payload,
            "source_order": payload["source_order"],
        }
        calls.append(item)
        if observation.nonce:
            by_nonce.setdefault(observation.nonce, []).append(item)
    for marker in markers:
        if (
            marker.reason == "host_call_output_missing"
            and marker.session_id == session_id
        ):
            calls.append({
                "session_id": session_id,
                "ts": marker.ts,
                "gate_call_id": marker.nonce,
                "skipped": None,
                "adapter": "host",
                "project_proof": segment_proof,
                "host_observation": None,
                "source_order": (0, marker.byte_offset or 0),
            })
    calls.sort(key=lambda item: (
        item.get("ts") or datetime.max.replace(tzinfo=timezone.utc),
        item.get("source_order") or (2**31, 2**63),
        str(item.get("gate_call_id") or ""),
    ))
    malformed = any(marker.reason == "schema_invalid" for marker in markers)
    return {
        "transcript_path": transcript_path,
        "calls": calls,
        "by_nonce": by_nonce,
        "complete": stable and not malformed,
        "loss_markers": markers,
    }


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
        # v2.6 deliberately removed global update count: another concurrent
        # session updating any node cannot make this session look compliant.
        # Accept only exact-session inserts/cited-edge activity.
        cited_edge_activity = _modify_links_to_cited(
            conn, session_id, cited_ids, t0, t_end,
        )
        if inserts_total > 0 or cited_edge_activity:
            return "ACCEPTED"
        return "UNRESOLVED"

    return "AMBIGUOUS"


def _measurement_classification(
    gate_row: dict,
    attribution: dict,
    *,
    measurement_protocol_version: str,
    pinned_runtime_version: str | None,
) -> tuple[str | None, tuple[str, ...]]:
    """Strict v2.6 disposition for one nonce-proven gate invocation.

    A session id alone is not confirmatory evidence.  Confirmatory requires an
    exact dual-source join, matching project proof/epoch, attestation, and the
    pinned source protocol/runtime.  Historical hash joins and non-pinned
    versions are pilots; missing required proof is loss, never a clean upgrade.
    """
    call_id = gate_row.get("gate_call_id")
    if not isinstance(call_id, str) or not call_id:
        return None, ("identity_missing",)

    if attribution.get("conflict"):
        return "conflict", tuple(
            attribution.get("conflict_reasons") or ("nonce_candidate_conflict",)
        )
    if attribution.get("s2_stream_complete") is not True:
        return "loss_signal", ("candidate_completeness_unproven",)

    project_check = attribution.get("project_check") or gate_row.get("project_check")
    if project_check == "foreign_project":
        return "foreign_project", ()
    if project_check in {
        "key_epoch_mismatch",
        "project_proof_missing",
        "project_proof_invalid",
    }:
        return "loss_signal", (str(project_check),)
    if project_check != "match":
        return "loss_signal", ("project_proof_missing",)

    host = attribution.get("host_observation")
    if not isinstance(host, dict):
        return "loss_signal", ("gate_only",)

    conflicts: set[str] = set(host.get("embedded_conflict_reasons") or ())
    gate_ts = _parse_iso_ms(gate_row.get("ts"))
    host_ts = host.get("ts")
    if not isinstance(host_ts, datetime):
        host_ts = _parse_iso_ms(host_ts)
    if (
        gate_ts is not None
        and host_ts is not None
        and abs((host_ts - gate_ts).total_seconds()) > 300
    ):
        conflicts.add("timestamp_mismatch")

    gate_proof = gate_row.get("project_proof")
    host_proof = host.get("project_proof")
    host_scope_proof = host.get("host_scope_project_proof")
    if isinstance(host_proof, dict) and isinstance(host_scope_proof, dict):
        scope_comparison = project_proof.compare_project_proofs(
            host_proof, host_scope_proof,
        )
        if scope_comparison == project_proof.PROJECT_KEY_EPOCH_MISMATCH:
            return "loss_signal", ("key_epoch_mismatch",)
        if scope_comparison == project_proof.PROJECT_FOREIGN:
            conflicts.add("project_scope_mismatch")
        elif scope_comparison in {
            project_proof.PROJECT_PROOF_MISSING,
            project_proof.PROJECT_PROOF_INVALID,
        }:
            return "loss_signal", (str(scope_comparison),)
    if isinstance(gate_proof, dict) and isinstance(host_proof, dict):
        proof_comparison = project_proof.compare_project_proofs(
            gate_proof, host_proof,
        )
        if proof_comparison == project_proof.PROJECT_KEY_EPOCH_MISMATCH:
            return "loss_signal", ("key_epoch_mismatch",)
        if proof_comparison in {
            project_proof.PROJECT_PROOF_MISSING,
            project_proof.PROJECT_PROOF_INVALID,
        }:
            return "loss_signal", (str(proof_comparison),)
        if proof_comparison != project_proof.PROJECT_MATCH:
            conflicts.add("project_proof_mismatch")
    elif gate_proof is None or host_proof is None:
        return "loss_signal", ("project_proof_missing",)

    gate_epoch = gate_row.get("key_epoch")
    host_epoch = host.get("key_epoch")
    if gate_epoch and host_epoch and gate_epoch != host_epoch:
        return "loss_signal", ("key_epoch_mismatch",)
    elif not gate_epoch or not host_epoch:
        return "loss_signal", ("project_proof_missing",)

    gate_protocol = (
        gate_row.get("measurement_protocol_version")
        or gate_row.get("protocol_version")
    )
    host_protocol = host.get("measurement_protocol_version")
    if gate_protocol is None or host_protocol is None:
        return "loss_signal", ("version_missing",)
    if gate_protocol != host_protocol:
        conflicts.add("measurement_protocol_mismatch")
    elif gate_protocol != measurement_protocol_version:
        return "pilot", ("version_mismatch",)

    gate_runtime = gate_row.get("runtime_version")
    host_runtime = host.get("runtime_version")
    if gate_runtime and host_runtime and gate_runtime != host_runtime:
        conflicts.add("runtime_version_mismatch")
    elif (
        pinned_runtime_version is None
        or gate_runtime != pinned_runtime_version
        or host_runtime != pinned_runtime_version
    ):
        return "pilot", ("version_mismatch",)

    gate_attestations = {
        str(value)
        for value in (
            gate_row.get("attestation"),
            gate_row.get("runtime_attestation"),
        )
        if value is not None and str(value)
    }
    host_attestation = host.get("attestation")
    if not gate_attestations or not host_attestation:
        return "loss_signal", ("attestation_missing",)
    if len(gate_attestations) != 1 or host_attestation not in gate_attestations:
        conflicts.add("attestation_mismatch")
    if (
        pinned_runtime_version is None
        or gate_attestations != {pinned_runtime_version}
        or host_attestation != pinned_runtime_version
    ):
        conflicts.add("attestation_not_pinned")

    gate_skipped = gate_row.get("skipped")
    host_skipped = host.get("skipped")
    if host_skipped is not None and bool(gate_skipped) != bool(host_skipped):
        conflicts.add("skipped_mismatch")

    if conflicts:
        return "conflict", tuple(sorted(conflicts))

    dual_source = (
        attribution.get("session_source") == "codex_transcript_nonce"
        or attribution.get("dual_source_joined") is True
    )
    if not dual_source:
        return "loss_signal", ("gate_only",)
    return "confirmatory", ()


# ---------- public entry point ----------

def correlate(
    project_path: str | None,
    start_date: date,
    end_date: date,
    *,
    window_seconds: int = WINDOW_SECONDS_DEFAULT,
    correlator_version: str = CORRELATOR_VERSION_DEFAULT,
    measurement_protocol_version: str = MEASUREMENT_PROTOCOL_VERSION_DEFAULT,
    pinned_runtime_version: str | None = None,
    project_key_epoch: str | None = PROJECT_KEY_EPOCH_DEFAULT,
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
        # Emitted rows whose window contained an unattributable gate. Reported
        # so a degraded window is visible as a count, never silently folded into
        # a clean-looking rate.
        "rows_with_uncertain_boundary": 0,
        "rows_skipped_dedup": 0,
        "rows_skipped_skipped_verdict": 0,
        # Skipped calls never emit outcomes, but a skipped call with exact
        # session identity and a timestamp is still a boundary.  Split the
        # audit count so a caller can prove those calls were not silently lost.
        "rows_skipped_boundary_capable": 0,
        "rows_skipped_boundary_unresolved": 0,
        "rows_skipped_attributed_from_transcript": 0,
        # A nonce-less observation is not an invocation under v2.6.  It remains
        # an auditable loss marker and, with exact session evidence, can censor
        # a measured window.
        "rows_loss_identity_missing": 0,
        "rows_gate_only_boundary_loss": 0,
        "rows_attribution_conflict": 0,
        "rows_unknown_session_ignored_for_boundary": 0,
        "correlator_version": correlator_version,
        "validation_protocol_version": measurement_protocol_version,
    }
    conn = db.connect(project_path or "")
    try:
        proof_context = None
        target_project_proof = None
        if project_path and project_key_epoch:
            identity = getattr(conn, "_kb_vault_identity", None)
            if identity is not None:
                try:
                    proof_context = project_proof.ProjectProofContext.from_vault_identity(
                        identity,
                        key_epoch=project_key_epoch,
                    )
                    target_project_proof = proof_context.prove(project_path)
                except ValueError:
                    proof_context = None
                    target_project_proof = None
        seen = _load_existing_keys(
            project_path,
            start_date,
            end_date,
            correlator_version,
        )

        host_config = _host_parser_config(
            start_date,
            end_date,
            target_project_proof=target_project_proof,
            project_key_epoch=project_key_epoch,
            pinned_runtime_version=pinned_runtime_version,
            measurement_protocol_version=measurement_protocol_version,
        )
        host_stream_cache: dict[str, dict] = {}

        def _host_stream(session_id: str) -> dict:
            if session_id not in host_stream_cache:
                host_stream_cache[session_id] = _host_session_stream(
                    conn,
                    session_id,
                    config=host_config,
                    proof_context=proof_context,
                    target_project_proof=target_project_proof,
                )
            return host_stream_cache[session_id]

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
                        None,
                        start_date,
                        end_date,
                        proof_context=proof_context,
                        target_project_path=project_path,
                    )
                except Exception:
                    attribution_index = {
                        "by_nonce": {},
                        "session_calls": {},
                        "candidate_completeness": {"complete": False},
                        "target_project_proof": target_project_proof,
                    }
            return attribution_index

        all_rows = list(log_utils.read_log_range(
            "gate", start_date, end_date, project_path,
        ))

        def _attribute_row(row: dict) -> dict | None:
            return codex_attribution.attribute(
                row,
                _attribution(),
                target_project_proof=target_project_proof,
            )

        # Resolve identity for EVERY row before building the next-gate map.
        # The map truncates a verdict's window at the next gate in the same
        # session; keying it on the raw row would leave every recovered row
        # session-less at map-build time, so two consecutive recovered gates in
        # one thread would both get a full 30-minute window and the earlier
        # verdict would absorb the later one's activity — misclassifying it.
        resolved: dict[int, dict] = {}
        for idx, R in enumerate(all_rows):
            if R.get("session_id"):
                session_id = str(R["session_id"])
                candidate_proof = R.get("project_proof")
                project_check = project_proof.compare_project_proofs(
                    candidate_proof,
                    target_project_proof,
                )
                stream = _host_stream(session_id)
                candidates = list(
                    (stream.get("by_nonce") or {}).get(R.get("gate_call_id")) or []
                )
                conflict_reasons = codex_attribution._candidate_set_conflicts(candidates)
                secondary = (
                    min(candidates, key=lambda item: item.get("source_order") or (0, 0))
                    if candidates else None
                )
                host_observation = (
                    secondary.get("host_observation")
                    if isinstance(secondary, dict) else None
                )
                dual_source_joined = isinstance(host_observation, dict)
                resolved[idx] = {
                    "session_id": session_id,
                    "session_source": "host_supplied",
                    "transcript_path": stream.get("transcript_path"),
                    "project_check": project_check,
                    "dual_source_joined": dual_source_joined,
                    "host_observation": host_observation,
                    "source_order": (
                        secondary.get("source_order")
                        if isinstance(secondary, dict) else None
                    ),
                    "conflict": bool(conflict_reasons),
                    "conflict_reasons": conflict_reasons,
                    "s2_stream_complete": stream.get("complete") is True,
                }
                continue
            # The host gave no identity. Recover it from its own transcript by
            # content join rather than writing the row off — otherwise every
            # measured number is silently single-host (id=4018). Declines on an
            # ambiguous match; see codex_attribution.
            hit = _attribute_row(R)
            if hit is None:
                continue
            if hit.get("conflict") and not hit.get("session_id"):
                counts["rows_attribution_conflict"] += 1
                continue
            resolved[idx] = {
                "session_id": hit["session_id"],
                "session_source": hit["source"],
                "transcript_path": hit.get("transcript_path"),
                "project_check": hit.get("project_check") or "match",
                "dual_source_joined": hit.get("source") == "codex_transcript_nonce",
                "host_observation": hit.get("host_observation"),
                "source_order": hit.get("source_order"),
                "conflict": hit.get("conflict") is True,
                "conflict_reasons": tuple(hit.get("conflict_reasons") or ()),
                "s2_stream_complete": True,
            }

        # Exact-session evidence only.  Unknown-session rows are neither
        # boundaries nor censoring evidence, even if their lossy display project
        # happens to match.  Conversely, a marker carrying an exact session can
        # make that session's boundary unknowable.
        loss_markers_by_session: dict[str, list[datetime | None]] = {}
        for idx, R in enumerate(all_rows):
            hit = resolved.get(idx)
            if hit is None:
                counts["rows_unknown_session_ignored_for_boundary"] += 1
                continue
            call_id = R.get("gate_call_id")
            if not isinstance(call_id, str) or not call_id:
                loss_markers_by_session.setdefault(
                    str(hit["session_id"]), []
                ).append(_parse_iso_ms(R.get("ts")))
            elif not isinstance(hit.get("host_observation"), dict):
                # B11: a nonce'd S1 invocation with exact session evidence but
                # no matching S2 tool-result row is gate_only. It remains a
                # boundary, and makes any measured window crossing it unknowable.
                loss_markers_by_session.setdefault(
                    str(hit["session_id"]), []
                ).append(_parse_iso_ms(R.get("ts")))
                counts["rows_gate_only_boundary_loss"] += 1

        # Host-attributed rows are ordered by exact same-session gate-log rows.
        # Recovered Codex rows use the exact session's transcript stream below;
        # that stream also contains skipped or otherwise unmatched gate calls.
        next_ts_by_idx: dict[int, datetime | None] = _build_next_in_session_map(
            all_rows,
            resolved,
            sources={"host_supplied"},
        )
        has_recovered = any(
            hit.get("session_source") != "host_supplied" for hit in resolved.values()
        )
        if has_recovered:
            current_index = _attribution()
            transcript_calls = _transcript_calls_by_session(
                current_index,
                target_project_proof=target_project_proof,
            )
            candidate_index_complete = _candidate_index_complete(current_index)
        else:
            transcript_calls = {}
            candidate_index_complete = None

        # Exact-session S2 loss markers participate in boundary censoring too.
        for session_id, stream in host_stream_cache.items():
            for marker in stream.get("loss_markers") or []:
                if marker.session_id == session_id and marker.reason in {
                    "host_call_output_missing",
                    "schema_invalid",
                }:
                    loss_markers_by_session.setdefault(session_id, []).append(marker.ts)
        for session_id, calls in transcript_calls.items():
            for item in calls:
                if item.get("host_observation") is None:
                    loss_markers_by_session.setdefault(session_id, []).append(
                        item.get("ts")
                    )

        for idx, R in enumerate(all_rows):
            hit = resolved.get(idx)
            if R.get("skipped"):
                counts["rows_skipped_skipped_verdict"] += 1
                skipped_ts = _parse_iso_ms(R.get("ts"))
                if hit is not None and skipped_ts is not None:
                    counts["rows_skipped_boundary_capable"] += 1
                    if hit.get("session_source") != "host_supplied":
                        counts["rows_skipped_attributed_from_transcript"] += 1
                else:
                    counts["rows_skipped_boundary_unresolved"] += 1
                continue
            if hit is None:
                counts["rows_skipped_no_session_id"] += 1
                continue
            session_id = hit["session_id"]
            session_source = hit["session_source"]
            transcript_path = hit["transcript_path"]
            if session_source != "host_supplied":
                counts["rows_attributed_from_transcript"] += 1
            call_id = R.get("gate_call_id")
            has_nonce = isinstance(call_id, str) and bool(call_id)
            if not has_nonce:
                counts["rows_loss_identity_missing"] += 1
            # Build the dedup identity from the SOURCE gate row, mapped onto the
            # same field names the emitted outcome row uses, so both sides of
            # the comparison key on the nonce whenever it exists.
            if has_nonce:
                key = _dedup_key({
                    "gate_call_id": call_id,
                    "correlator_version": correlator_version,
                })
            else:
                key = _legacy_diagnostic_key({
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
            next_s1_ts = next_ts_by_idx.get(idx)
            if session_source == "host_supplied":
                stream = _host_stream(session_id)
                s2_calls = {session_id: stream.get("calls") or []}
                s2_complete = stream.get("complete") is True
            else:
                s2_calls = transcript_calls
                s2_complete = candidate_index_complete is True
            next_s2_ts = _next_transcript_gate_ts(
                s2_calls,
                session_id=session_id,
                current_ts=t0,
                current_call_id=call_id,
                current_source_order=hit.get("source_order"),
            )
            boundary_candidates = [
                ts for ts in (next_s1_ts, next_s2_ts) if ts is not None
            ]
            next_gate_ts = min(boundary_candidates) if boundary_candidates else None
            boundary_source = "same_session_s1_s2_union"
            boundary_source_missing = not s2_complete or session_id not in s2_calls
            t_end = _compute_window_end(
                conn, session_id, t0, window_seconds, next_gate_ts,
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
            boundary_censor_reason = _boundary_censor_reason(
                loss_markers_by_session, session_id, t0, t_end,
            )
            if boundary_source_missing:
                boundary_censor_reason = (
                    boundary_censor_reason or "boundary_source_unavailable"
                )
            boundary_uncertain = boundary_censor_reason is not None
            if boundary_uncertain:
                counts["rows_with_uncertain_boundary"] += 1

            file_touches = _count_file_touches(
                conn, session_id, project_path, t0, t_end,
                transcript_path=transcript_path,
            )
            evidence_unavailable = file_touches is None
            if evidence_unavailable:
                outcome = "CENSORED"
            else:
                outcome = _classify(
                    conn, R, session_id, t0, t_end,
                    project_path=project_path, file_touches=file_touches,
                )
            if boundary_uncertain:
                outcome = "CENSORED"
            censor_reason = (
                boundary_censor_reason
                or ("evidence_unavailable" if evidence_unavailable else None)
            )

            if has_nonce:
                disposition, loss_reasons = _measurement_classification(
                    R,
                    hit,
                    measurement_protocol_version=measurement_protocol_version,
                    pinned_runtime_version=pinned_runtime_version,
                )
            else:
                disposition, loss_reasons = None, ("identity_missing",)

            log_utils.emit_event(
                "gate_outcome",
                {
                    "gate_ts": R.get("ts"),
                    "gate_query_hash": R.get("query_hash"),
                    # The exact join key (id=3310). query_hash collides across
                    # a repeated request and its retry; the nonce does not.
                    "gate_call_id": call_id,
                    # Closed-set label: host_supplied / codex_transcript_nonce.
                    # Makes a diagnostic coverage number splittable
                    # by identity provenance instead of blended.
                    "session_source": session_source,
                    "boundary_source": boundary_source,
                    "window_boundary_uncertain": boundary_uncertain,
                    "censor_reason": censor_reason,
                    "diagnostic_disposition": disposition,
                    "diagnostic_loss_reasons": list(loss_reasons),
                    "project_check": hit.get("project_check"),
                    "candidate_index_complete": s2_complete,
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
            if key is not None:
                seen.add(key)
    finally:
        conn.close()
    return counts


def _build_next_in_session_map(
    gate_rows: list[dict],
    resolved: dict[int, dict] | None = None,
    *,
    sources: set[str] | None = None,
) -> dict[int, datetime | None]:
    """For each gate row index, look up the timestamp of the NEXT gate
    in the same session (or None if it's the session's last gate in the
    scan window). Used to truncate the attribution window so a later
    gate's outcome isn't attributed to the earlier verdict.

    ONLY a gate known to be in the same session truncates. An unattributed gate
    is NOT a boundary: same-project is not same-session, concurrent Codex threads
    are normal, and truncating on one deletes real observed activity — measured
    on live data it cut a PROCEED window from 1800s to 391s and turned 6 observed
    file touches into 0.

    `resolved` supplies the session id for rows the host left blank and
    attribution recovered. It must be passed whenever attribution is in play:
    keying on the raw row would treat every recovered row as session-less, so
    consecutive recovered gates in one thread would each get a full window and
    the earlier verdict would absorb the later one's activity."""
    by_session: dict[str, list[tuple[int, datetime]]] = {}
    for idx, R in enumerate(gate_rows):
        hit = resolved.get(idx) if resolved else None
        if sources is not None:
            if hit is None or hit.get("session_source") not in sources:
                continue
        sid = hit.get("session_id") if hit else R.get("session_id")
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
