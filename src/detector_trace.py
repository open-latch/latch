"""Deterministic trace engine for the local Latch incident detector.

Phase 0 is read-only: resolve one exact session/turn, join transcript
coordinates to structural receipts, and print a candidate packet. Phase 1's
background worker uses the same trace and appends the already-redacted packet
to a local JSONL stream. No graph, node, priority, or schema mutation occurs.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import detector_snapshot
import log_utils
import paths


SCHEMA_VERSION = "1.0"
DETECTOR_VERSION = "0.1.0"
INCIDENT_STREAM = "detector_incident"
TRIGGER_STREAM = "detector_trigger"
MAX_SNIPPET_CHARS = 320
MAX_NODE_SNAPSHOTS = 32
_INCIDENT_APPEND_LOCK = threading.Lock()
TRIGGER_TYPES = frozenset({
    "manual_trace",
    "explicit_correction",
    "runtime_degraded",
    "direct_authority_conflict",
    "corrected_node_cited_current",
})

FAILURE_CLASSES = {
    "capture_gap",
    "retrieval_gap",
    "filtering_gap",
    "graph_gap",
    "runtime_gap",
    "agent_use_gap",
    "contract_gap",
    "not_a_latch_failure",
}

NOT_LATCH_FAILURE_REASONS = [
    "normal_ambiguity",
    "missing_user_choice",
    "new_information_after_event",
    "changed_user_preference",
    "external_tool_or_host_failure",
    "model_reasoning_unrelated_to_latch_context",
    "insufficient_stored_knowledge",
    "no_actionable_prior_artifact",
]

_NODE_REF_RE = re.compile(
    r"\b(?:id\s*=\s*|node(?:\s+id)?\s*[=:]?\s*)(\d+)\b", re.IGNORECASE
)
_ASSIGNMENT_SECRET_RE = re.compile(
    r'''(?ix)
    (?P<prefix>
        (?<![a-z0-9_.-])
        (?P<key_quote>["']?)
        (?P<key>[a-z0-9_.-]{1,128})
        (?P=key_quote)
        \s*[:=]\s*
    )
    (?:
        "(?P<double>[^"\r\n]*)" |
        '(?P<single>[^'\r\n]*)' |
        (?P<bare>[^\s,;}\]]+)
    )
    ''',
)
_SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|[_.-])(?:api[_-]?key|access[_-]?token|"
    r"secret(?:[_-]?access[_-]?key)?|token|password|passwd|"
    r"authorization|auth)$"
)
_AUTH_HEADER_RE = re.compile(
    r"(?im)(\b(?:proxy-)?authorization\s*:\s*)"
    r"(?:(?:bearer|basic|digest|token)\s+)?[^\s,;]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[opusr]_[A-Za-z0-9_]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{8,}|"
    r"glpat-[A-Za-z0-9_-]{8,}|AKIA[A-Z0-9]{16})\b",
    re.IGNORECASE,
)
_URL_CREDENTIAL_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^@\s/]+@"
)
_PEM_RE = re.compile(
    r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


@dataclass(frozen=True)
class TranscriptEvent:
    line: int
    role: str
    text: str
    timestamp: str | None
    tool_names: tuple[str, ...] = ()

    @property
    def prompt_hash(self) -> str:
        return hash_prompt(self.text)


def hash_prompt(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def open_readonly(
    project_path: str,
) -> tuple[sqlite3.Connection | None, Path, tempfile.TemporaryDirectory | None]:
    """Open an isolated SQLite bundle copy so the source directory is untouched."""
    db_path = paths.db_path(project_path)
    if not db_path.is_file():
        return None, db_path, None
    snapshot_dir = tempfile.TemporaryDirectory(prefix="latch-detector-ro-")
    conn: sqlite3.Connection | None = None
    try:
        snapshot_root = Path(snapshot_dir.name)
        snapshot_db = snapshot_root / db_path.name
        _copy_sqlite_bundle_stable(db_path, snapshot_root)
        # Recovery/checkpointing is allowed only inside the disposable copy.
        # This makes hot rollback-journal and WAL snapshots transactionally
        # readable without touching the source bundle.
        conn = sqlite3.connect(str(snapshot_db))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        check = conn.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise sqlite3.DatabaseError("detector SQLite snapshot failed quick_check")
        return conn, db_path, snapshot_dir
    except Exception:
        if conn is not None:
            conn.close()
        snapshot_dir.cleanup()
        raise


def _copy_sqlite_bundle_stable(db_path: Path, destination: Path) -> None:
    """Copy db+WAL only when their size/mtime state is stable across the copy."""
    sources = (
        db_path,
        Path(str(db_path) + "-wal"),
        Path(str(db_path) + "-journal"),
    )
    for _ in range(3):
        before = _sqlite_bundle_state(sources)
        for source in sources:
            target = destination / source.name
            if source.is_file():
                shutil.copy2(source, target)
            elif target.exists():
                target.unlink()
        if before == _sqlite_bundle_state(sources):
            return
    raise OSError("detector SQLite source changed during snapshot")


def _sqlite_bundle_state(sources: Iterable[Path]) -> tuple[tuple[bool, int, int], ...]:
    state: list[tuple[bool, int, int]] = []
    for source in sources:
        try:
            stat = source.stat()
            state.append((True, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            state.append((False, 0, 0))
    return tuple(state)


def resolve_session_id(explicit: str | None = None) -> str | None:
    for value in (
        explicit,
        os.environ.get("LATCH_SESSION_ID"),
        os.environ.get("CLAUDE_CODE_SESSION_ID"),
        os.environ.get("CODEX_THREAD_ID"),
    ):
        if value and str(value).strip():
            return str(value).strip()
    return None


def resolve_transcript(
    conn: sqlite3.Connection | None,
    session_id: str,
    explicit: str | None,
) -> tuple[Path | None, str, str | None]:
    """Resolve only exact transcript coordinates; never newest-mtime fallback."""
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return path, _host_for_transcript(path), None
        return None, "unknown", "explicit_transcript_missing"

    session_error = None
    if conn is not None:
        row = conn.execute(
            "SELECT transcript_path FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row and row["transcript_path"]:
            path = Path(row["transcript_path"]).expanduser()
            if path.is_file():
                return path, _host_for_transcript(path), None
            session_error = "session_transcript_missing"

    claude_root = Path.home() / ".claude" / "projects"
    if claude_root.is_dir():
        matches = [p for p in claude_root.glob(f"*/{session_id}.jsonl") if p.is_file()]
        if len(matches) == 1:
            return matches[0], "claude_code", None
        if len(matches) > 1:
            return None, "claude_code", "ambiguous_exact_transcript_matches"

    try:
        import codex_transcript

        path = codex_transcript.find_transcript(session_id)
        return path, "codex", "codex_turn_adapter_out_of_scope"
    except Exception:
        return None, "unknown", session_error or "transcript_unavailable"


def _host_for_transcript(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(8):
                line = handle.readline()
                if not line:
                    break
                try:
                    if json.loads(line).get("type") == "session_meta":
                        return "codex"
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return "claude_code"


def parse_claude_transcript(path: Path) -> list[TranscriptEvent]:
    """Parse human/assistant/tool coordinates without retaining tool output."""
    events: list[TranscriptEvent] = []
    for line_no, raw in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        content = message.get("content") if isinstance(message, dict) else None
        text, tools, has_tool_result = _extract_transcript_content(content)
        raw_role = (
            (message.get("role") if isinstance(message, dict) else None)
            or obj.get("role")
            or obj.get("type")
        )
        role = str(raw_role or "unknown")
        if role == "user" and has_tool_result and not text.strip():
            role = "tool_result"
        elif role not in {"user", "assistant"} and tools:
            role = "tool_use"
        if role in {"user", "assistant"} and not text.strip() and tools:
            role = "tool_use"
        if not text.strip() and not tools and role not in {"tool_result"}:
            continue
        events.append(
            TranscriptEvent(
                line=line_no,
                role=role,
                text=text,
                timestamp=obj.get("timestamp") or obj.get("ts"),
                tool_names=tuple(tools),
            )
        )
    return events


def _extract_transcript_content(content: Any) -> tuple[str, list[str], bool]:
    if isinstance(content, str):
        return content, [], False
    if not isinstance(content, list):
        return "", [], False
    texts: list[str] = []
    tools: list[str] = []
    has_tool_result = False
    for block in content:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict):
            typ = block.get("type")
            if typ == "text" and block.get("text"):
                texts.append(str(block["text"]))
            elif typ == "tool_use":
                tools.append(str(block.get("name") or "unknown"))
            elif typ == "tool_result":
                has_tool_result = True
                tools.append(str(block.get("tool_name") or "tool_result"))
    return "\n".join(texts), tools, has_tool_result


def build_trace(
    *,
    project_path: str,
    session_id: str,
    transcript_path: str | None = None,
    prompt_hash: str | None = None,
    trigger_types: Iterable[str] = ("manual_trace",),
    event_ts: str | None = None,
    node_ids: Iterable[int] = (),
    prompt_turn: int | None = None,
    previous: int = 0,
) -> dict:
    """Build one candidate packet without writing files or mutating SQLite."""
    requested_triggers = {str(t) for t in trigger_types if str(t)}
    triggers = sorted(requested_triggers & TRIGGER_TYPES) or ["manual_trace"]
    captured_at = now_iso()
    event_ts = event_ts or captured_at
    conn, db_path, snapshot_dir = open_readonly(project_path)
    try:
        transcript, host, transcript_error = resolve_transcript(
            conn, session_id, transcript_path
        )
        events: list[TranscriptEvent] = []
        if transcript and host == "claude_code":
            events = parse_claude_transcript(transcript)
        elif transcript and host == "codex" and not transcript_error:
            transcript_error = "codex_turn_adapter_out_of_scope"

        selection = _select_exchange(
            events,
            prompt_hash=prompt_hash,
            correction_trigger="explicit_correction" in triggers,
            previous=max(0, int(previous)),
        )
        selection_limitation = selection.get("limitation")
        trigger_event = selection.get("trigger")
        subject_event = selection.get("subject")
        assistant_events = selection.get("assistants") or []
        subject_hash = subject_event.prompt_hash if subject_event else None
        trigger_hash = prompt_hash or (
            trigger_event.prompt_hash if trigger_event else subject_hash
        )
        subject_turn = (
            max(0, int(prompt_turn) - 1)
            if prompt_turn is not None and "explicit_correction" in triggers
            else prompt_turn
        )

        retrieve_rows = _read_stream_rows("retrieve", project_path)
        subject_receipt = _select_receipt(
            retrieve_rows,
            session_id=session_id,
            join_hash=subject_hash,
            turn=subject_turn,
            event_ts=event_ts,
            allow_exact_coordinate="explicit_correction" not in triggers,
            require_exact_coordinate=(
                "runtime_degraded" in triggers
                and "explicit_correction" not in triggers
            ),
        )
        trigger_receipt = _select_receipt(
            retrieve_rows,
            session_id=session_id,
            join_hash=trigger_hash,
            turn=prompt_turn,
            event_ts=event_ts,
            require_exact_coordinate=bool(
                {"explicit_correction", "runtime_degraded"}.intersection(triggers)
            ),
        )
        if (
            subject_receipt is None
            and subject_hash == trigger_hash
            and "explicit_correction" not in triggers
        ):
            subject_receipt = trigger_receipt
        retrieval = _normalize_retrieval(subject_receipt)
        trigger_retrieval = _normalize_retrieval(trigger_receipt)

        gate_receipt = _select_receipt(
            _read_stream_rows("gate", project_path),
            session_id=session_id,
            join_hash=subject_hash or trigger_hash,
            hash_field="query_hash",
            event_ts=event_ts,
        )
        trigger_event_receipt = _select_receipt(
            _read_stream_rows(TRIGGER_STREAM, project_path),
            session_id=session_id,
            join_hash=trigger_hash,
            hash_field="prompt_hash",
            event_ts=event_ts,
        )

        assistant_text = "\n".join(e.text for e in assistant_events if e.text)
        mentioned_ids = extract_node_refs(assistant_text)
        explicit_ids = _ordered_unique(int(n) for n in node_ids)
        candidate_ids = set(explicit_ids)
        receipt_order: list[int] = []
        scores: dict[int, float] = {}
        for normalized in (retrieval, trigger_retrieval):
            for hit in normalized.get("raw_hits", []):
                candidate_ids.add(hit["id"])
                receipt_order.append(hit["id"])
                if hit.get("score") is not None:
                    scores[hit["id"]] = hit["score"]
            for hit in normalized.get("injected", []):
                candidate_ids.add(hit["id"])
                receipt_order.append(hit["id"])
                if hit.get("score") is not None:
                    scores[hit["id"]] = hit["score"]
            for node_id in normalized.get("active_ids", []):
                candidate_ids.add(node_id)
                receipt_order.append(node_id)
        candidate_ids.update(mentioned_ids)
        if gate_receipt:
            candidate_ids.update(int(n) for n in gate_receipt.get("evidence_ids") or [])
        if trigger_event_receipt:
            candidate_ids.update(
                int(s["id"])
                for s in trigger_event_receipt.get("node_snapshots") or []
                if isinstance(s, dict) and s.get("id") is not None
            )

        subject_snapshots = _event_snapshots(
            ("subject_retrieval", subject_receipt),
        )
        trigger_snapshots_by_id = _event_snapshots(
            ("detector_trigger", trigger_event_receipt),
            ("trigger_retrieval", trigger_receipt),
        )
        event_snapshots = dict(subject_snapshots)
        for node_id, snapshot in trigger_snapshots_by_id.items():
            event_snapshots.setdefault(node_id, snapshot)
        snapshots = _merge_snapshots(
            conn,
            candidate_ids,
            event_snapshots,
            priority_ids=[*event_snapshots, *explicit_ids, *receipt_order],
            scores=scores,
            captured_at=captured_at,
            event_ts=event_ts,
        )
        trigger_snapshots = _merge_snapshots(
            conn,
            set(trigger_snapshots_by_id),
            trigger_snapshots_by_id,
            priority_ids=trigger_snapshots_by_id,
            scores=scores,
            captured_at=captured_at,
            event_ts=event_ts,
        )
        relations = _candidate_relations(conn, {s["id"] for s in snapshots})

        classification = _classify(
            triggers=triggers,
            retrieval=retrieval,
            trigger_retrieval=trigger_retrieval,
            snapshots=snapshots,
            trigger_snapshots=trigger_snapshots,
            gate_receipt=gate_receipt,
            trigger_event_receipt=trigger_event_receipt,
        )
        should_emit = _should_emit(triggers, trigger_snapshots)
        packet_seed = {
            "triggers": triggers,
            "session_id": session_id,
            "trigger_hash": trigger_hash,
            "trigger_line": trigger_event.line if trigger_event else None,
        }
        incident_id = "incident-" + _stable_hash(packet_seed)[:16]
        fingerprint = _stable_hash(
            {
                "triggers": triggers,
                "retrieval_state": retrieval["state"],
                "trigger_retrieval_state": trigger_retrieval["state"],
                "primary_failure_class": classification["primary_failure_class"],
                "subject_authority_counts": _authority_counts(snapshots),
                "trigger_authority_counts": _authority_counts(trigger_snapshots),
            }
        )[:20]

        event_coordinate = {
            "project_path": str(Path(project_path).resolve()),
            "resolved_kb_dir": str(db_path.parent.resolve()),
            "host": host,
            "session_id": session_id,
            "prompt_turn": prompt_turn,
            "event_timestamp": event_ts,
            "transcript_path": str(transcript.resolve()) if transcript else None,
            "transcript_status": (
                "available"
                if transcript and events and not selection_limitation
                else "unavailable"
            ),
            "transcript_limitation": transcript_error or selection_limitation,
            "trigger_line": trigger_event.line if trigger_event else None,
            "subject_line": subject_event.line if subject_event else None,
            "assistant_lines": [e.line for e in assistant_events],
        }
        packet = {
            "schema_version": SCHEMA_VERSION,
            "detector_version": DETECTOR_VERSION,
            "incident_id": incident_id,
            "fingerprint": fingerprint,
            "created_at": captured_at,
            "trigger": {
                "types": triggers,
                "confidence": _trigger_confidence(triggers),
                "prompt_hash": trigger_hash,
            },
            "event_coordinate": event_coordinate,
            "expected_behavior": _expected_behavior(triggers),
            "observed_behavior": _observed_behavior(
                triggers,
                retrieval,
                trigger_retrieval,
                snapshots,
                trigger_snapshots,
                gate_receipt,
            ),
            "transcript_evidence": {
                "trigger_snippet": _snippet(trigger_event.text if trigger_event else ""),
                "subject_prompt_snippet": _snippet(subject_event.text if subject_event else ""),
                "assistant_snippet": _snippet(assistant_text),
                "tool_pointers": [
                    {"line": e.line, "names": list(e.tool_names)}
                    for e in [*assistant_events, *(selection.get("tools") or [])]
                    if e.tool_names
                ],
            },
            "receipts": {
                "retrieval": retrieval,
                "trigger_retrieval": trigger_retrieval,
                "gate": _normalize_gate(gate_receipt),
                "detector_trigger": _receipt_pointer(trigger_event_receipt),
                "correction": _joined_pointer(
                    "correction", project_path, session_id, trigger_hash, "prompt_hash", event_ts
                ),
                "decision": _joined_pointer(
                    "decision", project_path, session_id, trigger_hash, "query_hash", event_ts
                ),
            },
            "candidate_node_snapshots": snapshots,
            "trigger_node_snapshots": trigger_snapshots,
            "graph_relations": relations,
            "classification": classification,
            "deterministic_causal_trace": _causal_trace(
                event_coordinate,
                retrieval,
                trigger_retrieval,
                snapshots,
                trigger_snapshots,
                gate_receipt,
            ),
            "inferred_assessment": {
                "status": "not_run",
                "hypothesis": None,
                "note": "Phase 1 is deterministic-only; no LLM assessor ran.",
            },
            "replay_proposal": _replay_proposal(triggers, retrieval, snapshots),
            "human_disposition": {
                "status": "unresolved",
                "resolution_evidence": [],
                "verification_result": None,
            },
            "public_fixture_candidate": False,
            "not_a_latch_failure_reasons_considered": NOT_LATCH_FAILURE_REASONS,
            "should_emit": should_emit,
        }
        packet["sanitized_projection"] = build_sanitized_projection(packet)
        return packet
    finally:
        if conn is not None:
            conn.close()
        if snapshot_dir is not None:
            snapshot_dir.cleanup()


def _select_exchange(
    events: list[TranscriptEvent],
    *,
    prompt_hash: str | None,
    correction_trigger: bool,
    previous: int,
) -> dict[str, Any]:
    users = [e for e in events if e.role == "user" and e.text.strip()]
    if not users:
        return {
            "trigger": None,
            "subject": None,
            "assistants": [],
            "tools": [],
            "limitation": "prompt_hash_not_found" if prompt_hash else None,
        }
    trigger = None
    if prompt_hash:
        matches = [e for e in users if e.prompt_hash == prompt_hash]
        if matches:
            trigger = matches[-1]
        else:
            return {
                "trigger": None,
                "subject": None,
                "assistants": [],
                "tools": [],
                "limitation": "prompt_hash_not_found",
            }
    if trigger is None:
        idx = max(0, len(users) - 1 - previous)
        trigger = users[idx]

    trigger_pos = users.index(trigger)
    subject = trigger
    if correction_trigger and trigger_pos > 0:
        subject = users[trigger_pos - 1]
    elif previous and trigger_pos >= previous:
        subject = users[trigger_pos]

    next_user_line = next(
        (u.line for u in users if u.line > subject.line),
        10**18,
    )
    assistants = [
        e for e in events
        if e.role == "assistant" and subject.line < e.line < next_user_line
    ]
    tools = [
        e for e in events
        if e.role in {"tool_use", "tool_result"} and subject.line < e.line < next_user_line
    ]
    return {
        "trigger": trigger,
        "subject": subject,
        "assistants": assistants,
        "tools": tools,
        "limitation": None,
    }


def _read_stream_rows(stream: str, project_path: str) -> list[dict]:
    log_dir = paths.project_dir(project_path)
    rows: list[dict] = []
    for path in sorted(log_dir.glob(f"{stream}-*.log*")):
        if not path.is_file():
            continue
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, start=1):
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        row["_receipt_path"] = str(path)
                        row["_receipt_line"] = line_no
                        rows.append(row)
        except OSError:
            continue
    rows.sort(key=lambda r: _parse_ts(r.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    return rows


def _select_receipt(
    rows: list[dict],
    *,
    session_id: str,
    join_hash: str | None,
    hash_field: str = "prompt_hash",
    turn: int | None = None,
    event_ts: str | None = None,
    allow_exact_coordinate: bool = True,
    require_exact_coordinate: bool = False,
) -> dict | None:
    if not join_hash:
        return None
    candidates = [
        row for row in rows
        if (row.get("session_id") or row.get("sid")) == session_id
        and row.get(hash_field) == join_hash
    ]
    if not candidates:
        return None
    if turn is not None:
        candidates = [r for r in candidates if r.get("turn") == turn]
        if not candidates:
            return None
    candidates.sort(
        key=lambda r: _parse_ts(r.get("ts"))
        or datetime.min.replace(tzinfo=timezone.utc)
    )
    target = _parse_ts(event_ts)
    if target:
        # UserPromptSubmit stores this event coordinate in the receipt before
        # appending it. The common log header is written milliseconds later, so
        # accept only that exact durable coordinate as the event's own row.
        if allow_exact_coordinate:
            exact_coordinate = [
                row for row in candidates
                if _parse_ts(row.get("detector_event_ts")) == target
            ]
            if exact_coordinate:
                return exact_coordinate[-1]
        if require_exact_coordinate:
            return None
        before = [
            row for row in candidates
            if (row_ts := _parse_ts(row.get("ts"))) is not None and row_ts <= target
        ]
        if before:
            return before[-1]
        # Historical traces must never borrow evidence from a future event.
        return None
    if require_exact_coordinate:
        return None
    return candidates[-1]


def _normalize_retrieval(receipt: dict | None) -> dict:
    if receipt is None:
        return {
            "state": "unavailable",
            "outcome": "receipt_missing_or_rotated",
            "top_k_boundary": 10,
            "raw_hits": [],
            "injected": [],
            "active_ids": [],
            "filters": {},
            "pointer": None,
        }
    raw_hits = _normalize_hits(receipt.get("raw_hits"), with_kind=True)
    injected = _normalize_hits(receipt.get("injected"), with_kind=False)
    skip = receipt.get("skip")
    explicit = receipt.get("retrieval_status")
    if receipt.get("error"):
        state, outcome = "degraded", "error"
    elif explicit in {"unavailable", "error", "over_budget"}:
        state, outcome = "degraded", explicit
    elif skip == "embed_daemon_unavailable":
        state, outcome = "degraded", "embed_daemon_unavailable"
    elif skip:
        state, outcome = "not_executed", str(skip)
    elif receipt.get("overran_budget"):
        state, outcome = "degraded", "over_budget_after_execution"
    elif injected:
        state, outcome = "executed", "injected"
    elif raw_hits:
        state, outcome = "executed", "no_injection_from_top10"
    else:
        state, outcome = "executed", "no_candidates_in_recorded_top10"
    return {
        "state": state,
        "outcome": outcome,
        "top_k_boundary": 10,
        "boundary_note": "Absence means outside the recorded top 10, not absent from the KB.",
        "path": receipt.get("path"),
        "turn": receipt.get("turn"),
        "raw_hits": raw_hits,
        "injected": injected,
        "active_ids": [int(n) for n in receipt.get("active_ids") or []],
        "active_set_size": receipt.get("active_set_size"),
        "filters": {
            "kind": int(receipt.get("filtered_out_kind") or 0),
            "active": int(receipt.get("filtered_out_active") or 0),
            "floor": int(receipt.get("filtered_out_floor") or 0),
        },
        "runtime": {
            "skip": skip,
            "error_type": str(receipt.get("error") or "").split(":", 1)[0] or None,
            "elapsed_ms": receipt.get("elapsed_ms"),
            "overran_budget": bool(receipt.get("overran_budget")),
        },
        "pointer": _receipt_pointer(receipt),
    }


def _normalize_hits(raw: Any, *, with_kind: bool) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:10]:
        if not isinstance(item, (list, tuple)) or not item:
            continue
        try:
            node_id = int(item[0])
        except (TypeError, ValueError):
            continue
        score = None
        if len(item) > 1 and item[1] is not None:
            try:
                score = float(item[1])
            except (TypeError, ValueError):
                pass
        row = {"id": node_id, "score": score}
        if with_kind:
            row["kind"] = str(item[2]) if len(item) > 2 else None
        out.append(row)
    return out


def _event_snapshots(
    *sources: tuple[str, dict | None],
) -> dict[int, dict]:
    snapshots: dict[int, dict] = {}
    for source, receipt in sources:
        if not receipt:
            continue
        for snap in receipt.get("node_snapshots") or []:
            if isinstance(snap, dict) and snap.get("id") is not None:
                copy = dict(snap)
                copy["snapshot_basis"] = "event_time_receipt"
                copy["snapshot_source"] = source
                snapshots.setdefault(int(copy["id"]), copy)
    return snapshots


def _merge_snapshots(
    conn: sqlite3.Connection | None,
    node_ids: set[int],
    event_snapshots: dict[int, dict],
    *,
    priority_ids: Iterable[int],
    scores: dict[int, float],
    captured_at: str,
    event_ts: str,
) -> list[dict]:
    out: list[dict] = []
    ordered_ids = _ordered_unique(
        [*priority_ids, *sorted(node_ids)]
    )[:MAX_NODE_SNAPSHOTS]
    for node_id in ordered_ids:
        if node_id in event_snapshots:
            snap = dict(event_snapshots[node_id])
        elif conn is not None:
            snap = detector_snapshot.snapshot_node(
                conn, node_id, score=scores.get(node_id), snapshot_at=captured_at
            )
            snap["snapshot_basis"] = "current_snapshot_at_trace"
        else:
            snap = {
                "id": node_id,
                "status": None,
                "authority": "UNAVAILABLE",
                "content_hash": None,
                "snapshot_at": captured_at,
                "snapshot_basis": "database_unavailable",
            }
        snap["temporal_fidelity"] = _temporal_fidelity(snap, event_ts)
        out.append(snap)
    return out


def _ordered_unique(values: Iterable[int]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for value in values:
        node_id = int(value)
        if node_id not in seen:
            seen.add(node_id)
            ordered.append(node_id)
    return ordered


def _temporal_fidelity(snapshot: dict, event_ts: str) -> str:
    if snapshot.get("snapshot_basis") == "event_time_receipt":
        return "event_time"
    event = _parse_ts(event_ts)
    created = _parse_ts(snapshot.get("created_at"))
    updated = _parse_ts(snapshot.get("updated_at"))
    captured = _parse_ts(snapshot.get("snapshot_at"))
    if event and created and created > event:
        return "not_available_at_event_created_later"
    if event and updated and updated > event:
        return "historical_unknown_node_changed_after_event"
    if event and captured and abs((captured - event).total_seconds()) <= 120:
        return "near_event_trace_time"
    return "historical_unknown_current_state_only"


def _candidate_relations(
    conn: sqlite3.Connection | None, node_ids: set[int]
) -> list[dict]:
    if conn is None or not node_ids:
        return []
    ids = sorted(node_ids)
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT src, dst, relation FROM edges WHERE status='active' "
        f"AND src IN ({placeholders}) AND dst IN ({placeholders}) "
        "ORDER BY src, dst, relation",
        [*ids, *ids],
    ).fetchall()
    return [
        {
            "src": int(r["src"]),
            "dst": int(r["dst"]),
            "relation": r["relation"],
            "temporal_basis": "current_graph_at_trace",
        }
        for r in rows
    ]


def _classify(
    *,
    triggers: list[str],
    retrieval: dict,
    trigger_retrieval: dict,
    snapshots: list[dict],
    trigger_snapshots: list[dict],
    gate_receipt: dict | None,
    trigger_event_receipt: dict | None,
) -> dict:
    authority_issue = any(
        s.get("authority") in {"STALE", "RECONCILED"}
        and s.get("temporal_fidelity") == "event_time"
        for s in trigger_snapshots
    )
    if "corrected_node_cited_current" in triggers and authority_issue:
        primary, confidence, rationale = (
            "contract_gap",
            "high",
            "A node asserted as current had stale or reconciled authority at capture.",
        )
    elif "runtime_degraded" in triggers or trigger_retrieval["state"] == "degraded":
        primary, confidence, rationale = (
            "runtime_gap",
            "high",
            "The retrieval receipt records an unavailable, error, or over-budget runtime path.",
        )
    elif "direct_authority_conflict" in triggers and gate_receipt and gate_receipt.get(
        "recommendation"
    ) in {"MODIFY", "DO_NOT_PROCEED"}:
        primary, confidence, rationale = (
            "not_a_latch_failure",
            "high",
            "Latch surfaced the authority conflict through the gate; human review should confirm expected behavior.",
        )
    elif retrieval["state"] == "degraded":
        primary, confidence, rationale = (
            "runtime_gap",
            "medium",
            "The subject turn has a degraded retrieval receipt.",
        )
    else:
        primary, confidence, rationale = (
            None,
            "low",
            "The deterministic Phase 1 evidence is insufficient to assign a Latch failure class.",
        )
    assert primary is None or primary in FAILURE_CLASSES
    return {
        "status": "provisional" if primary else "unresolved",
        "primary_failure_class": primary,
        "contributing_classes": [],
        "confidence": confidence,
        "rationale": rationale,
        "alternative_explanations": NOT_LATCH_FAILURE_REASONS,
        "candidate_only": True,
    }


def _should_emit(triggers: list[str], trigger_snapshots: list[dict]) -> bool:
    if triggers == ["manual_trace"]:
        return False
    if {"explicit_correction", "runtime_degraded", "direct_authority_conflict"}.intersection(
        triggers
    ):
        return True
    if "corrected_node_cited_current" in triggers:
        return any(
            s.get("authority") in {"STALE", "RECONCILED"}
            and s.get("temporal_fidelity") == "event_time"
            for s in trigger_snapshots
        )
    return True


def extract_node_refs(text: str) -> set[int]:
    return {int(m.group(1)) for m in _NODE_REF_RE.finditer(text or "")}


def redact_text(text: str) -> str:
    text = _PEM_RE.sub("<redacted-pem>", str(text or ""))
    text = _AUTH_HEADER_RE.sub(r"\1<redacted>", text)
    text = _URL_CREDENTIAL_RE.sub(r"\1<redacted>@", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _TOKEN_RE.sub("<redacted-token>", text)
    text = _ASSIGNMENT_SECRET_RE.sub(_redact_assignment, text)
    text = _EMAIL_RE.sub("<redacted-email>", text)
    return text


def _redact_assignment(match: re.Match) -> str:
    if not _SECRET_KEY_RE.search(match.group("key")):
        return match.group(0)
    prefix = match.group("prefix")
    if match.group("double") is not None:
        return f'{prefix}"<redacted>"'
    if match.group("single") is not None:
        return f"{prefix}'<redacted>'"
    return f"{prefix}<redacted>"


def _snippet(text: str) -> str:
    clean = redact_text(text).strip()
    if len(clean) <= MAX_SNIPPET_CHARS:
        return clean
    return clean[:MAX_SNIPPET_CHARS].rstrip() + "…"


def _receipt_pointer(receipt: dict | None) -> dict | None:
    if not receipt:
        return None
    return {
        "stream": receipt.get("event_type"),
        "path": receipt.get("_receipt_path"),
        "line": receipt.get("_receipt_line"),
        "timestamp": receipt.get("ts"),
    }


def _normalize_gate(receipt: dict | None) -> dict:
    if receipt is None:
        return {"state": "unavailable", "pointer": None}
    return {
        "state": "degraded" if receipt.get("skipped") or receipt.get("error") else "executed",
        "recommendation": receipt.get("recommendation"),
        "evidence_ids": [int(n) for n in receipt.get("evidence_ids") or []],
        "decision_chain": [int(n) for n in receipt.get("decision_chain") or []],
        "pointer": _receipt_pointer(receipt),
    }


def _joined_pointer(
    stream: str,
    project_path: str,
    session_id: str,
    join_hash: str | None,
    hash_field: str,
    event_ts: str,
) -> dict | None:
    receipt = _select_receipt(
        _read_stream_rows(stream, project_path),
        session_id=session_id,
        join_hash=join_hash,
        hash_field=hash_field,
        event_ts=event_ts,
    )
    return _receipt_pointer(receipt)


def _trigger_confidence(triggers: list[str]) -> str:
    if any(t in {"runtime_degraded", "corrected_node_cited_current"} for t in triggers):
        return "high"
    if "explicit_correction" in triggers:
        return "high"
    return "medium" if "direct_authority_conflict" in triggers else "manual"


def _expected_behavior(triggers: list[str]) -> str:
    if "explicit_correction" in triggers:
        return "Relevant pre-event Latch evidence should be surfaced and used, or its absence reported truthfully."
    if "runtime_degraded" in triggers:
        return "Retrieval degradation should remain explicit and must not be reported as no relevant result."
    if "corrected_node_cited_current" in triggers:
        return "Stale or reconciled nodes must not be asserted as current without their authority context."
    if "direct_authority_conflict" in triggers:
        return "An active canonical conflict should be surfaced and resolved before mutation."
    return "Trace the selected turn without mutating project judgment."


def _observed_behavior(
    triggers: list[str],
    retrieval: dict,
    trigger_retrieval: dict,
    snapshots: list[dict],
    trigger_snapshots: list[dict],
    gate_receipt: dict | None,
) -> str:
    subject_authority = _authority_counts(snapshots)
    trigger_authority = _authority_counts(trigger_snapshots)
    return (
        f"Subject retrieval={retrieval['state']}/{retrieval['outcome']}; "
        f"trigger retrieval={trigger_retrieval['state']}/{trigger_retrieval['outcome']}; "
        f"subject authority={subject_authority}; trigger authority={trigger_authority}; gate="
        f"{(gate_receipt or {}).get('recommendation') or 'unavailable'}."
    )


def _causal_trace(
    coordinate: dict,
    retrieval: dict,
    trigger_retrieval: dict,
    snapshots: list[dict],
    trigger_snapshots: list[dict],
    gate_receipt: dict | None,
) -> list[dict]:
    return [
        {
            "step": "freeze_event_coordinate",
            "result": "available" if coordinate.get("session_id") else "unavailable",
        },
        {
            "step": "recover_transcript_turn",
            "result": coordinate.get("transcript_status"),
            "limitation": coordinate.get("transcript_limitation"),
        },
        {
            "step": "join_subject_retrieval_receipt",
            "result": retrieval["state"],
            "outcome": retrieval["outcome"],
        },
        {
            "step": "join_trigger_retrieval_receipt",
            "result": trigger_retrieval["state"],
            "outcome": trigger_retrieval["outcome"],
        },
        {
            "step": "freeze_subject_authority",
            "result": _authority_counts(snapshots),
        },
        {
            "step": "freeze_trigger_authority",
            "result": _authority_counts(trigger_snapshots),
        },
        {
            "step": "join_gate_receipt",
            "result": (gate_receipt or {}).get("recommendation") or "unavailable",
        },
    ]


def _replay_proposal(triggers: list[str], retrieval: dict, snapshots: list[dict]) -> dict:
    return {
        "status": "proposed_not_exported",
        "trigger_types": triggers,
        "fixture_shape": "deterministic transcript plus structural receipt assertions",
        "assertions": [
            f"retrieval_state_is_{retrieval['state']}",
            "authority_state_is_preserved_at_capture",
            "trigger_does_not_auto_confirm_a_latch_failure",
        ],
        "phase_2_exporter_required": True,
    }


def _authority_counts(snapshots: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for snapshot in snapshots:
        key = str(snapshot.get("authority") or "UNAVAILABLE")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def build_sanitized_projection(packet: dict) -> dict:
    """Build a non-linkable public-fixture seed from a strict allowlist."""
    retrieval = packet["receipts"]["retrieval"]
    trigger_retrieval = packet["receipts"]["trigger_retrieval"]
    snapshots = packet.get("candidate_node_snapshots") or []
    trigger_snapshots = packet.get("trigger_node_snapshots") or []
    relation_counts: dict[str, int] = {}
    for relation in packet.get("graph_relations") or []:
        key = _public_relation_bucket(relation.get("relation"))
        relation_counts[key] = relation_counts.get(key, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "detector_version": DETECTOR_VERSION,
        "trigger_types": list(packet["trigger"]["types"]),
        "trigger_confidence": packet["trigger"]["confidence"],
        "retrieval_mechanics": {
            "subject_state": retrieval["state"],
            "subject_outcome": _public_retrieval_outcome(retrieval["outcome"]),
            "trigger_state": trigger_retrieval["state"],
            "trigger_outcome": _public_retrieval_outcome(trigger_retrieval["outcome"]),
            "top_k_boundary": 10,
            "raw_hit_count": len(retrieval.get("raw_hits") or []),
            "injected_count": len(retrieval.get("injected") or []),
            "filter_counts": dict(retrieval.get("filters") or {}),
        },
        "authority_mechanics": {
            "subject": _authority_counts(snapshots),
            "trigger": _authority_counts(trigger_snapshots),
        },
        "graph_relation_counts": dict(sorted(relation_counts.items())),
        "classification": {
            "status": packet["classification"]["status"],
            "primary_failure_class": packet["classification"]["primary_failure_class"],
            "confidence": packet["classification"]["confidence"],
            "candidate_only": True,
        },
        "human_disposition": "unresolved",
        "public_fixture_candidate": False,
    }


def write_incident(packet: dict, project_path: str) -> Path:
    """Append one redacted packet and verify the exact JSONL bytes were stored."""
    safe_packet = _redact_value(json.loads(json.dumps(packet, default=str)))
    safe_packet["transcript_evidence"] = {
        key: _snippet(value) if isinstance(value, str) else value
        for key, value in (safe_packet.get("transcript_evidence") or {}).items()
    }
    safe_packet["public_fixture_candidate"] = False
    safe_packet["sanitized_projection"] = build_sanitized_projection(safe_packet)
    row = {
        **safe_packet,
        "ts": now_iso(),
        "project": paths.sanitize_cwd(project_path),
        "session_id": safe_packet.get("event_coordinate", {}).get("session_id"),
        "event_type": INCIDENT_STREAM,
    }
    path = log_utils.today_log_path(INCIDENT_STREAM, project_path)
    _append_jsonl_checked(path, row)
    return path


def _append_jsonl_checked(path: Path, row: dict) -> None:
    """Serialize, lock, append, fsync, and verify one complete JSONL record."""
    payload = (
        json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str)
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _INCIDENT_APPEND_LOCK:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "r+b", buffering=0) as handle:
            _lock_incident_file(handle)
            try:
                start = os.lseek(handle.fileno(), 0, os.SEEK_END)
                view = memoryview(payload)
                while view:
                    written = os.write(handle.fileno(), view)
                    if written <= 0:
                        raise OSError("detector incident append made no progress")
                    view = view[written:]
                os.fsync(handle.fileno())
                os.lseek(handle.fileno(), start, os.SEEK_SET)
                observed = os.read(handle.fileno(), len(payload))
                if observed != payload:
                    raise OSError("detector incident append verification failed")
            finally:
                _unlock_incident_file(handle)


def _lock_incident_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(handle.fileno(), 0, os.SEEK_SET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_incident_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(handle.fileno(), 0, os.SEEK_SET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_incidents(project_path: str, *, limit: int = 20) -> list[dict]:
    if int(limit) <= 0:
        return []
    rows = _read_stream_rows(INCIDENT_STREAM, project_path)
    for row in rows:
        row.pop("_receipt_path", None)
        row.pop("_receipt_line", None)
    return rows[-int(limit) :]


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if _SECRET_KEY_RE.search(str(key))
                else _redact_value(item)
            )
            for key, item in value.items()
        }
    return value


_PUBLIC_RELATIONS = {
    "supersedes", "replaces", "constrains", "motivates", "tested_against",
    "depends_on", "reconciled_by", "related_to",
}


def _public_relation_bucket(value: Any) -> str:
    relation = str(value or "unknown")
    return relation if relation in _PUBLIC_RELATIONS else "other"


_PUBLIC_RETRIEVAL_OUTCOMES = {
    "receipt_missing_or_rotated",
    "error",
    "unavailable",
    "embed_daemon_unavailable",
    "prompt_too_short",
    "no_session_id",
    "over_budget",
    "over_budget_after_execution",
    "injected",
    "no_injection_from_top10",
    "no_candidates_in_recorded_top10",
}


def _public_retrieval_outcome(value: Any) -> str:
    outcome = str(value or "unknown")
    return outcome if outcome in _PUBLIC_RETRIEVAL_OUTCOMES else "other"
