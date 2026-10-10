"""Production evidence resolution over canonical windows and stable S2 bytes."""
from __future__ import annotations

import gc
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tracemalloc

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from latch.store import artifacts  # noqa: E402
from latch.store import db  # noqa: E402
from latch.evals import outcome_evidence  # noqa: E402
from latch.evals import outcome_measurement as om  # noqa: E402


UTC = timezone.utc
START = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
END = START + timedelta(minutes=30)
SESSION = "evidence-session"
NONCE = "aaaaaaaaaaaa"
S2_FILE = "claude-session.sanitized.jsonl"
FIXTURES = Path(__file__).parent / "fixtures" / "outcome_measurement"
KEY_EPOCH = "evidence-test-epoch"
RUNTIME_VERSION = "evidence-test-runtime"
PROOF_KEY = b"evidence-test-project-proof-key!" * 2


def _proof_context() -> om.ProjectProofContext:
    return om.ProjectProofContext.from_vault_key(
        PROOF_KEY,
        key_epoch=KEY_EPOCH,
        vault_id="evidence-test-vault",
    )


def _config(project: Path) -> om.MeasurementConfig:
    return om.MeasurementConfig(
        t0=START - timedelta(days=1),
        cap=START - timedelta(days=1) + timedelta(days=21),
        target_project_proof=_proof_context().prove(project),
        key_epoch=KEY_EPOCH,
        pinned_runtime_version=RUNTIME_VERSION,
    )


def _observation(
    source: str,
    file: str,
    project: Path,
    *,
    ids=None,
    adapter: str | None = None,
) -> om.Observation:
    return om.Observation(
        source=source,
        file=file,
        byte_offset=0,
        nonce=NONCE,
        ts=START,
        session_id=SESSION,
        adapter=adapter or ("gate" if source == om.SOURCE_GATE else "claude"),
        project_proof=_proof_context().prove(project),
        verdict="MODIFY" if source == om.SOURCE_GATE else None,
        verdict_id_lists=(
            {name: list(ids or []) for name in om.REQUIRED_ID_LIST_FIELDS}
            if source == om.SOURCE_GATE
            else None
        ),
    )


def _receipt(
    project: Path,
    *,
    evidence_ids=(),
    session: str | None = SESSION,
    adapter: str = "claude",
) -> om.InvocationReceipt:
    return om.InvocationReceipt(
        nonce=NONCE,
        measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
        observations=(
            _observation(om.SOURCE_GATE, "gate.log", project, ids=evidence_ids),
            _observation(
                om.SOURCE_HOST, S2_FILE, project, adapter=adapter,
            ),
        ),
        disposition="confirmatory",
        admitted=True,
        lineage_order_key=START,
        fresh_ts=START,
        session_id=session,
        verdict="MODIFY",
        outcome=None,
        finalized=False,
        window_start=START,
        window_end=END,
    )


def _censor_reason(evidence: om.OutcomeEvidence, project: Path) -> str | None:
    observations = tuple(
        replace(
            row,
            observable=evidence.observable,
            evidence_available=evidence.evidence_available,
        )
        for row in _receipt(project).observations
    )
    return om._classify_outcome(observations)[1]


def _resolve(conn, project: Path, receipts, stable):
    return outcome_evidence.resolve_receipt_evidence(
        receipts,
        stable,
        _config(project),
        conn=conn,
        project_path=str(project),
        project_proof_context=_proof_context(),
    )


def _claude_bytes(project: Path) -> bytes:
    rows = [
        {
            "timestamp": "2026-08-04T12:05:00.000Z",
            "sessionId": SESSION,
            "cwd": str(project),
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use",
                "id": "edit-ok",
                "name": "Edit",
                "input": {"file_path": str(project / "src/a.py")},
            }]},
        },
        {
            "timestamp": "2026-08-04T12:05:01.000Z",
            "sessionId": SESSION,
            "cwd": str(project),
            "type": "user",
            "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": "edit-ok",
                "content": "updated",
            }]},
        },
        # Same successful path is distinct evidence once, not twice.
        {
            "timestamp": "2026-08-04T12:06:00.000Z",
            "sessionId": SESSION,
            "cwd": str(project),
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use",
                "id": "edit-dup",
                "name": "Write",
                "input": {"file_path": str(project / "src/a.py")},
            }]},
        },
        {
            "timestamp": "2026-08-04T12:06:01.000Z",
            "sessionId": SESSION,
            "cwd": str(project),
            "type": "user",
            "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": "edit-dup",
                "content": "written",
            }]},
        },
        # Explicitly failed edits never become touches.
        {
            "timestamp": "2026-08-04T12:07:00.000Z",
            "sessionId": SESSION,
            "cwd": str(project),
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use",
                "id": "edit-failed",
                "name": "Edit",
                "input": {"file_path": str(project / "src/failed.py")},
            }]},
        },
        {
            "timestamp": "2026-08-04T12:07:01.000Z",
            "sessionId": SESSION,
            "cwd": str(project),
            "type": "user",
            "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": "edit-failed",
                "is_error": True,
                "content": "failed",
            }]},
        },
    ]
    return "".join(json.dumps(row) + "\n" for row in rows).encode()


def _codex_outer_exec_bytes(
    project: Path,
    script: str,
    *,
    timestamp: str = "2026-08-04T12:08:00.000Z",
    output: object = None,
    output_call_id: str = "outer-exec",
) -> bytes:
    """Mutate the sanitized corpus envelope without changing its record shape."""
    rows = [
        json.loads(line)
        for line in (
            FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl"
        ).read_text().splitlines()
    ]
    rows[0]["timestamp"] = timestamp
    rows[0]["payload"].update({
        "id": SESSION,
        "session_id": SESSION,
        "timestamp": timestamp,
        "cwd": str(project),
    })
    rows[1] = {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": "outer-exec",
            "input": script,
        },
    }
    rows[2] = {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": output_call_id,
            "output": json.dumps(
                {"exit_code": 0} if output is None else output,
                separators=(",", ":"),
            ),
        },
    }
    return "".join(json.dumps(row) + "\n" for row in rows).encode()


def _insert_node(
    conn,
    *,
    kind: str,
    session_id: str | None,
    created_at: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO nodes "
        "(kind, title, body, status, session_id, created_at, updated_at) "
        "VALUES (?, 't', 'b', 'staging', ?, ?, ?)",
        (kind, session_id, created_at, created_at),
    )
    conn.commit()
    return int(cursor.lastrowid)


def test_resolver_uses_exact_window_session_ids_and_snapshotted_s2(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    conn = db.connect(str(tmp_path))
    try:
        cited = _insert_node(
            conn, kind="fact", session_id=None, created_at="2026-08-04 11:00:00",
        )
        progress = _insert_node(
            conn,
            kind="progress",
            session_id=SESSION,
            created_at="2026-08-04 12:10:00",
        )
        _insert_node(
            conn,
            kind="progress",
            session_id="concurrent-session",
            created_at="2026-08-04 12:11:00",
        )
        conn.execute(
            "INSERT INTO edges (src, dst, relation, created_at, status) "
            "VALUES (?, ?, 'related_to', '2026-08-04 12:12:00', 'active')",
            (progress, cited),
        )
        conn.commit()
        stable = {om.SOURCE_HOST: ((S2_FILE, _claude_bytes(tmp_path)),)}
        receipt = _receipt(tmp_path, evidence_ids=(cited,))

        result = _resolve(conn, tmp_path, (receipt,), stable)[0]
        assert result.observable is True
        assert result.evidence_available is True
        assert (result.inserts, result.progress_inserts) == (1, 1)
        assert result.linked_cited_insert is True
        assert result.cited_edge_activity is True
        assert result.touches == 1

        resolver = outcome_evidence.make_receipt_evidence_resolver(
            conn,
            str(tmp_path),
            project_proof_context=_proof_context(),
        )
        assert tuple(resolver((receipt,), stable, _config(tmp_path))) == (result,)
    finally:
        conn.close()


def test_db_evidence_preserves_fractional_window_bounds(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    conn = db.connect(str(tmp_path))
    try:
        _insert_node(
            conn,
            kind="progress",
            session_id=SESSION,
            created_at="2026-08-04 12:00:00",
        )
        _insert_node(
            conn,
            kind="progress",
            session_id=SESSION,
            created_at="2026-08-04 12:30:00.900000",
        )
        receipt = replace(
            _receipt(tmp_path),
            window_start=START + timedelta(milliseconds=900),
            window_end=END + timedelta(milliseconds=100),
        )
        stable = {om.SOURCE_HOST: ((S2_FILE, _claude_bytes(tmp_path)),)}
        result = _resolve(conn, tmp_path, (receipt,), stable)[0]
        assert result.evidence_available is True
        assert result.inserts == 0
        assert result.progress_inserts == 0
    finally:
        conn.close()


@pytest.mark.parametrize("failure", ["missing_bytes", "malformed", "missing_session"])
def test_receipt_evidence_failures_never_become_clean_zero(
    tmp_path: Path,
    failure: str,
):
    conn = db.connect(str(tmp_path))
    try:
        receipt = _receipt(tmp_path)
        stable = {om.SOURCE_HOST: ((S2_FILE, _claude_bytes(tmp_path)),)}
        if failure == "missing_bytes":
            stable = {om.SOURCE_HOST: ()}
        elif failure == "malformed":
            stable = {om.SOURCE_HOST: ((S2_FILE, b"{malformed\n"),)}
        else:
            receipt = replace(receipt, session_id=None)
        result = _resolve(conn, tmp_path, (receipt,), stable)[0]
        assert result.nonce == NONCE
        assert result.observable is False
        assert result.evidence_available is False
        assert (result.inserts, result.progress_inserts, result.touches) == (0, 0, 0)
        assert _censor_reason(result, tmp_path) == "instrument_unavailable"
    finally:
        conn.close()


def test_sql_failure_isolated_to_unavailable_evidence(tmp_path: Path):
    conn = db.connect(str(tmp_path))
    receipt = _receipt(tmp_path)
    stable = {om.SOURCE_HOST: ((S2_FILE, _claude_bytes(tmp_path)),)}
    conn.execute("DROP TABLE nodes")
    conn.commit()
    try:
        result = _resolve(conn, tmp_path, (receipt,), stable)[0]
        assert result.observable is True
        assert result.evidence_available is False
        assert _censor_reason(result, tmp_path) == "evidence_unavailable"
    finally:
        conn.close()


def test_resolver_unions_resumed_exact_session_segments_only(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    foreign_project = tmp_path / "foreign-project"
    foreign_project.mkdir()
    conn = db.connect(str(tmp_path))
    call = {
        "timestamp": "2026-08-04T12:08:00.000Z",
        "sessionId": SESSION,
        "cwd": str(tmp_path),
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "id": "resumed-edit",
            "name": "Edit",
            "input": {"file_path": str(tmp_path / "src/resumed.py")},
        }]},
    }
    result = {
        "timestamp": "2026-08-04T12:08:01.000Z",
        "sessionId": SESSION,
        "cwd": str(tmp_path),
        "type": "user",
        "message": {"content": [{
            "type": "tool_result",
            "tool_use_id": "resumed-edit",
            "content": "updated",
        }]},
    }
    foreign = {
        "timestamp": "2026-08-04T12:09:00.000Z",
        # Deliberately reuse the target session. Project proof, not the raw
        # session string, must keep this segment out of the evidence union.
        "sessionId": SESSION,
        "cwd": str(foreign_project),
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "id": "foreign-edit",
            "name": "Edit",
            "input": {"file_path": str(tmp_path / "src/foreign.py")},
        }]},
    }
    stable = {
        om.SOURCE_HOST: (
            (S2_FILE, _claude_bytes(tmp_path)),
            ("resumed-1.jsonl", (json.dumps(call) + "\n").encode()),
            ("resumed-2.jsonl", (json.dumps(result) + "\n").encode()),
            # A malformed foreign segment and a wholly unknown malformed
            # segment are outside this exact-session evidence union.
            (
                "foreign.jsonl",
                (json.dumps(foreign) + "\n{malformed\n").encode(),
            ),
            (
                "foreign-adapter.jsonl",
                (
                    json.dumps({
                        "timestamp": "2026-08-04T12:09:30.000Z",
                        "type": "session_meta",
                        "payload": {"id": SESSION, "cwd": str(tmp_path)},
                    })
                    + "\n{malformed\n"
                ).encode(),
            ),
            ("unknown.jsonl", b"{malformed\n"),
        )
    }
    try:
        resolved = _resolve(
            conn, tmp_path, (_receipt(tmp_path),), stable,
        )[0]
        assert resolved.observable is True
        assert resolved.evidence_available is False
        assert resolved.touches == 2
    finally:
        conn.close()


def test_foreign_project_raw_session_collision_censors_db_counts(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    foreign = tmp_path / "foreign-project"
    foreign.mkdir()
    conn = db.connect(str(tmp_path))
    try:
        _insert_node(
            conn,
            kind="progress",
            session_id=SESSION,
            created_at="2026-08-04 12:10:00",
        )
        foreign_row = {
            "timestamp": "2026-08-04T12:09:00.000Z",
            "sessionId": SESSION,
            "cwd": str(foreign),
            "type": "user",
            "message": {"content": []},
        }
        stable = {
            om.SOURCE_HOST: (
                (S2_FILE, _claude_bytes(tmp_path)),
                (
                    "foreign-session.jsonl",
                    (json.dumps(foreign_row) + "\n").encode(),
                ),
            )
        }
        result = _resolve(
            conn, tmp_path, (_receipt(tmp_path),), stable,
        )[0]
        assert result.observable is True
        assert result.evidence_available is False
        assert result.touches == 1
        assert result.progress_inserts == 0
        assert _censor_reason(result, tmp_path) == "evidence_unavailable"
    finally:
        conn.close()


@pytest.mark.parametrize("candidate", ["call", "result"])
def test_nonidentical_artifact_candidates_with_same_tool_id_fail_closed(
    tmp_path: Path, candidate: str,
):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    rows = [json.loads(line) for line in _claude_bytes(tmp_path).splitlines()]
    source_index = 0 if candidate == "call" else 1
    duplicate = json.loads(json.dumps(rows[source_index]))
    if candidate == "call":
        duplicate["message"]["content"][0]["input"]["file_path"] = str(
            tmp_path / "src/different.py"
        )
    else:
        duplicate["message"]["content"][0]["content"] = "different success"
    rows.insert(source_index + 1, duplicate)
    data = "".join(json.dumps(row) + "\n" for row in rows).encode()
    conn = db.connect(str(tmp_path))
    try:
        result = _resolve(
            conn,
            tmp_path,
            (_receipt(tmp_path),),
            {om.SOURCE_HOST: ((S2_FILE, data),)},
        )[0]
        assert result.observable is False
        assert result.evidence_available is False
        assert result.touches == 0
    finally:
        conn.close()


@pytest.mark.parametrize("adapter", ["claude", "codex"])
@pytest.mark.parametrize(
    ("shared_id", "unrelated_timestamp", "fails_closed"),
    [
        (False, "2026-08-04T12:05:00.500Z", False),
        (True, "2026-08-04T12:05:00.500Z", True),
        (True, "2026-08-04T11:59:30.000Z", False),
    ],
)
def test_cross_tool_call_id_collision_is_scoped_to_the_receipt_window(
    tmp_path: Path,
    adapter: str,
    shared_id: bool,
    unrelated_timestamp: str,
    fails_closed: bool,
):
    (tmp_path / ".git").mkdir()
    edit_id = "edit-call"
    unrelated_id = edit_id if shared_id else "read-call"
    if adapter == "claude":
        rows = [
            {
                "timestamp": "2026-08-04T12:05:00.000Z",
                "sessionId": SESSION,
                "cwd": str(tmp_path),
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": edit_id,
                    "name": "Edit",
                    "input": {"file_path": str(tmp_path / "src/a.py")},
                }]},
            },
            {
                "timestamp": unrelated_timestamp,
                "sessionId": SESSION,
                "cwd": str(tmp_path),
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": unrelated_id,
                    "name": "Read",
                    "input": {"file_path": str(tmp_path / "src/a.py")},
                }]},
            },
            {
                "timestamp": "2026-08-04T12:05:01.000Z",
                "sessionId": SESSION,
                "cwd": str(tmp_path),
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": edit_id,
                    "content": "updated",
                }]},
            },
        ]
        if (
            shared_id
            and unrelated_timestamp == "2026-08-04T11:59:30.000Z"
        ):
            # Complete the historical occurrence before the in-window edit.
            # The separate ordered-occurrence regression below covers the
            # indistinguishable unresolved-overlap case and requires censoring.
            rows.insert(2, {
                "timestamp": "2026-08-04T11:59:31.000Z",
                "sessionId": SESSION,
                "cwd": str(tmp_path),
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": edit_id,
                    "content": "historical read",
                }]},
            })
    else:
        rows = [
            {
                "timestamp": "2026-08-04T11:59:00.000Z",
                "type": "session_meta",
                "payload": {"id": SESSION, "cwd": str(tmp_path)},
            },
            {
                "timestamp": "2026-08-04T12:05:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    # Codex call identity accepts either structural alias; the
                    # collision index and edit index must select the same one.
                    "call_id": "",
                    "id": edit_id,
                    "status": "completed",
                    "input": (
                        "*** Begin Patch\n"
                        f"*** Update File: {tmp_path / 'src/a.py'}\n"
                        "@@\n-a\n+b\n*** End Patch"
                    ),
                },
            },
            {
                "timestamp": unrelated_timestamp,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "read_file",
                    "call_id": unrelated_id,
                    "arguments": "{}",
                },
            },
        ]
    data = "".join(json.dumps(row) + "\n" for row in rows).encode()

    if fails_closed:
        with pytest.raises(
            artifacts.ArtifactEvidenceError,
            match="identity is shared by conflicting tool calls",
        ):
            artifacts.observe_session_artifacts_in_window_bytes(
                data, str(tmp_path), START, END, session_id=SESSION,
            )
    else:
        assert artifacts.observe_session_artifacts_in_window_bytes(
            data, str(tmp_path), START, END, session_id=SESSION,
        ) == [{"repo": str(tmp_path), "path": "src/a.py"}]


@pytest.mark.parametrize("adapter", ["claude", "codex"])
@pytest.mark.parametrize(
    (
        "old_failed",
        "include_new_result",
        "old_result_timestamp",
        "error_match",
        "second_old_call",
    ),
    [
        (False, False, "2026-08-04T11:58:01.000Z", "no tool result", False),
        (True, True, "2026-08-04T11:58:01.000Z", None, False),
        (False, False, "2026-08-04T12:05:01.000Z", "ambiguous", False),
        (False, True, "2026-08-04T11:58:01.000Z", "ambiguous", True),
    ],
    ids=[
        "old-success-cannot-supply",
        "old-failure-cannot-poison",
        "unresolved-overlap-fails-closed",
        "ambiguity-debt-propagates",
    ],
)
def test_artifact_results_join_the_ordered_same_id_call_occurrence(
    tmp_path: Path,
    adapter: str,
    old_failed: bool,
    include_new_result: bool,
    old_result_timestamp: str,
    error_match: str | None,
    second_old_call: bool,
):
    (tmp_path / ".git").mkdir()
    shared_id = "reused-call-id"
    expected = [{"repo": str(tmp_path), "path": "src/inside.py"}]
    if adapter == "claude":
        rows = [
            {
                "timestamp": "2026-08-04T11:58:00.000Z",
                "sessionId": SESSION,
                "cwd": str(tmp_path),
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": shared_id,
                    "name": "Read",
                    "input": {"file_path": str(tmp_path / "src/old.py")},
                }]},
            },
            {
                "timestamp": old_result_timestamp,
                "sessionId": SESSION,
                "cwd": str(tmp_path),
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": shared_id,
                    "is_error": old_failed,
                    "content": "old result",
                }]},
            },
            {
                "timestamp": "2026-08-04T12:05:00.000Z",
                "sessionId": SESSION,
                "cwd": str(tmp_path),
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": shared_id,
                    "name": "Edit",
                    "input": {"file_path": str(tmp_path / "src/inside.py")},
                }]},
            },
        ]
        if second_old_call:
            rows.insert(1, {
                "timestamp": "2026-08-04T11:58:00.500Z",
                "sessionId": SESSION,
                "cwd": str(tmp_path),
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": shared_id,
                    "name": "Read",
                    "input": {"file_path": str(tmp_path / "src/old-two.py")},
                }]},
            })
        if include_new_result:
            rows.append({
                "timestamp": "2026-08-04T12:05:01.000Z",
                "sessionId": SESSION,
                "cwd": str(tmp_path),
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": shared_id,
                    "content": "new success",
                }]},
            })
    else:
        patch = (
            "*** Begin Patch\n"
            f"*** Update File: {tmp_path / 'src/inside.py'}\n"
            "@@\n-a\n+b\n*** End Patch"
        )
        rows = [
            {
                "timestamp": "2026-08-04T11:57:00.000Z",
                "type": "session_meta",
                "payload": {"id": SESSION, "cwd": str(tmp_path)},
            },
            {
                "timestamp": "2026-08-04T11:58:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "read_file",
                    "call_id": shared_id,
                    "input": "{}",
                },
            },
            {
                "timestamp": old_result_timestamp,
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": shared_id,
                    "output": json.dumps({
                        "exit_code": 7 if old_failed else 0,
                    }),
                },
            },
            {
                "timestamp": "2026-08-04T12:05:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": shared_id,
                    "input": f"await tools.apply_patch({json.dumps(patch)});",
                },
            },
        ]
        if second_old_call:
            rows.insert(2, {
                "timestamp": "2026-08-04T11:58:00.500Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "read_file",
                    "call_id": shared_id,
                    "input": "{}",
                },
            })
        if include_new_result:
            rows.append({
                "timestamp": "2026-08-04T12:05:01.000Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": shared_id,
                    "output": json.dumps({"exit_code": 0}),
                },
            })

    data = "".join(json.dumps(row) + "\n" for row in rows).encode()
    if error_match is None:
        assert artifacts.observe_session_artifacts_in_window_bytes(
            data,
            str(tmp_path),
            START,
            END,
            session_id=SESSION,
        ) == expected
    else:
        with pytest.raises(artifacts.ArtifactEvidenceError, match=error_match):
            artifacts.observe_session_artifacts_in_window_bytes(
                data,
                str(tmp_path),
                START,
                END,
                session_id=SESSION,
            )


def test_call_local_codex_completion_does_not_create_result_debt(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    shared_id = "call-local-completion"
    nested_patch = (
        "*** Begin Patch\n"
        f"*** Update File: {tmp_path / 'src/inside.py'}\n"
        "@@\n-a\n+b\n*** End Patch"
    )
    rows = [
        {
            "timestamp": "2026-08-04T11:57:00.000Z",
            "type": "session_meta",
            "payload": {"id": SESSION, "cwd": str(tmp_path)},
        },
        {
            "timestamp": "2026-08-04T11:58:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": shared_id,
                "status": "completed",
                "input": (
                    "*** Begin Patch\n"
                    f"*** Update File: {tmp_path / 'src/old.py'}\n"
                    "@@\n-a\n+b\n*** End Patch"
                ),
            },
        },
        {
            "timestamp": "2026-08-04T12:05:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": shared_id,
                "input": (
                    f"await tools.apply_patch({json.dumps(nested_patch)});"
                ),
            },
        },
        {
            "timestamp": "2026-08-04T12:05:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": shared_id,
                "output": json.dumps({"exit_code": 0}),
            },
        },
    ]
    data = "".join(json.dumps(row) + "\n" for row in rows).encode()
    assert artifacts.observe_session_artifacts_in_window_bytes(
        data,
        str(tmp_path),
        START,
        END,
        session_id=SESSION,
    ) == [{"repo": str(tmp_path), "path": "src/inside.py"}]


def test_call_local_codex_completion_ignores_ambiguous_old_output(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    shared_id = "call-local-isolation"
    rows = [
        {
            "timestamp": "2026-08-04T11:57:00.000Z",
            "type": "session_meta",
            "payload": {"id": SESSION, "cwd": str(tmp_path)},
        },
        {
            "timestamp": "2026-08-04T11:58:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "read_file",
                "call_id": shared_id,
                "arguments": "{}",
            },
        },
        {
            "timestamp": "2026-08-04T12:05:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": shared_id,
                "status": "completed",
                "input": (
                    "*** Begin Patch\n"
                    f"*** Update File: {tmp_path / 'src/inside.py'}\n"
                    "@@\n-a\n+b\n*** End Patch"
                ),
            },
        },
        {
            "timestamp": "2026-08-04T12:05:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": shared_id,
                "output": json.dumps({"exit_code": 7}),
            },
        },
    ]
    data = "".join(json.dumps(row) + "\n" for row in rows).encode()
    assert artifacts.observe_session_artifacts_in_window_bytes(
        data,
        str(tmp_path),
        START,
        END,
        session_id=SESSION,
    ) == [{"repo": str(tmp_path), "path": "src/inside.py"}]


def test_codex_result_envelopes_realign_matching_call_families(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    shared_id = "codex-result-family"
    nested_patch = (
        "*** Begin Patch\n"
        f"*** Update File: {tmp_path / 'src/inside.py'}\n"
        "@@\n-a\n+b\n*** End Patch"
    )
    rows = [
        {
            "timestamp": "2026-08-04T11:57:00.000Z",
            "type": "session_meta",
            "payload": {"id": SESSION, "cwd": str(tmp_path)},
        },
        {
            "timestamp": "2026-08-04T11:58:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "read_file",
                "call_id": shared_id,
                "arguments": "{}",
            },
        },
        {
            "timestamp": "2026-08-04T11:59:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": shared_id,
                "status": "completed",
                "input": (
                    "*** Begin Patch\n"
                    f"*** Update File: {tmp_path / 'src/old.py'}\n"
                    "@@\n-a\n+b\n*** End Patch"
                ),
            },
        },
        {
            "timestamp": "2026-08-04T11:59:30.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": shared_id,
                "output": json.dumps({"exit_code": 7}),
            },
        },
        {
            "timestamp": "2026-08-04T12:05:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": shared_id,
                "input": (
                    f"await tools.apply_patch({json.dumps(nested_patch)});"
                ),
            },
        },
        {
            "timestamp": "2026-08-04T12:05:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": shared_id,
                "output": json.dumps({"exit_code": 0}),
            },
        },
    ]
    data = "".join(json.dumps(row) + "\n" for row in rows).encode()
    assert artifacts.observe_session_artifacts_in_window_bytes(
        data,
        str(tmp_path),
        START,
        END,
        session_id=SESSION,
    ) == [{"repo": str(tmp_path), "path": "src/inside.py"}]


def test_artifact_result_just_after_window_still_joins_its_call(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    rows = [
        {
            "timestamp": "2026-08-04T12:29:59.000Z",
            "sessionId": SESSION,
            "cwd": str(tmp_path),
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use",
                "id": "edge-edit",
                "name": "Edit",
                "input": {"file_path": str(tmp_path / "src/edge.py")},
            }]},
        },
        {
            "timestamp": "2026-08-04T12:30:01.000Z",
            "sessionId": SESSION,
            "cwd": str(tmp_path),
            "type": "user",
            "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": "edge-edit",
                "content": "updated",
            }]},
        },
    ]
    data = "".join(json.dumps(row) + "\n" for row in rows).encode()
    assert artifacts.observe_session_artifacts_in_window_bytes(
        data,
        str(tmp_path),
        START,
        END,
        session_id=SESSION,
    ) == [{"repo": str(tmp_path), "path": "src/edge.py"}]


def test_resolver_builds_stable_segment_index_once_per_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    conn = db.connect(str(tmp_path))
    first = _receipt(tmp_path)
    second_nonce = "bbbbbbbbbbbb"
    second = replace(
        first,
        nonce=second_nonce,
        observations=tuple(
            replace(row, nonce=second_nonce) for row in first.observations
        ),
    )
    stable = {om.SOURCE_HOST: ((S2_FILE, _claude_bytes(tmp_path)),)}
    original = artifacts.build_artifact_evidence_index
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(artifacts, "build_artifact_evidence_index", counted)
    try:
        resolved = _resolve(conn, tmp_path, (first, second), stable)
        assert len(resolved) == 2
        assert all(row.observable for row in resolved)
        assert calls == 1
    finally:
        conn.close()


def test_bytes_helper_is_strict_but_legacy_path_wrapper_stays_lenient(tmp_path: Path):
    malformed = b'{"timestamp":"bad"}\n{not-json\n'
    with pytest.raises(artifacts.ArtifactEvidenceError):
        artifacts.observe_session_artifacts_in_window_bytes(
            malformed,
            str(tmp_path),
            START,
            END,
            session_id=SESSION,
        )

    path = tmp_path / "legacy.jsonl"
    path.write_bytes(malformed)
    assert artifacts.observe_session_artifacts_in_window(
        str(path), str(tmp_path), START, END,
    ) == []


def test_bytes_helper_reads_successful_codex_patch_from_exact_session(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    rows = [
        {
            "timestamp": "2026-08-04T11:59:00.000Z",
            "type": "session_meta",
            "payload": {"id": SESSION, "cwd": str(tmp_path)},
        },
        {
            "timestamp": "2026-08-04T12:08:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": "patch-ok",
                "status": "completed",
                "input": (
                    "*** Begin Patch\n"
                    f"*** Update File: {tmp_path / 'src/codex.py'}\n"
                    "@@\n-a\n+b\n*** End Patch"
                ),
            },
        },
    ]
    data = "".join(json.dumps(row) + "\n" for row in rows).encode()
    assert artifacts.observe_session_artifacts_in_window_bytes(
        data,
        str(tmp_path),
        START,
        END,
        session_id=SESSION,
    ) == [{"repo": str(tmp_path), "path": "src/codex.py"}]


def test_corpus_shaped_outer_exec_apply_patch_matches_direct_and_resolver(
    tmp_path: Path,
):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {tmp_path / 'src/outer.py'}\n"
        "@@\n-a\n+b\n*** End Patch"
    )
    script = (
        f"const patch = {json.dumps(patch)};\n"
        "const result = await tools.apply_patch(patch);\n"
        "text(result);"
    )
    data = _codex_outer_exec_bytes(tmp_path, script)
    expected = [{"repo": str(tmp_path), "path": "src/outer.py"}]
    assert artifacts.observe_session_artifacts_in_window_bytes(
        data,
        str(tmp_path),
        START,
        END,
        session_id=SESSION,
    ) == expected

    conn = db.connect(str(tmp_path))
    try:
        resolved = _resolve(
            conn,
            tmp_path,
            (_receipt(tmp_path, adapter="codex"),),
            {om.SOURCE_HOST: ((S2_FILE, data),)},
        )[0]
        assert resolved.observable is True
        assert resolved.evidence_available is True
        assert resolved.touches == 1
    finally:
        conn.close()


def test_outer_exec_failure_and_result_identity_do_not_manufacture_touch(
    tmp_path: Path,
):
    (tmp_path / ".git").mkdir()
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {tmp_path / 'failed.py'}\n"
        "*** End Patch"
    )
    script = f"await tools.apply_patch({json.dumps(patch)});"
    failed = _codex_outer_exec_bytes(
        tmp_path, script, output={"exit_code": 7},
    )
    assert artifacts.observe_session_artifacts_in_window_bytes(
        failed,
        str(tmp_path),
        START,
        END,
        session_id=SESSION,
    ) == []

    mismatched = _codex_outer_exec_bytes(
        tmp_path, script, output_call_id="another-outer-call",
    )
    with pytest.raises(artifacts.ArtifactEvidenceError, match="tool result"):
        artifacts.observe_session_artifacts_in_window_bytes(
            mismatched,
            str(tmp_path),
            START,
            END,
            session_id=SESSION,
        )


def test_outer_exec_ignores_quoted_and_commented_apply_patch_decoys(
    tmp_path: Path,
):
    (tmp_path / ".git").mkdir()
    decoy = (
        "*** Begin Patch\n"
        f"*** Update File: {tmp_path / 'decoy.py'}\n"
        "*** End Patch"
    )
    actual = (
        "*** Begin Patch\n"
        f"*** Update File: {tmp_path / 'actual.py'}\n"
        "*** End Patch"
    )
    second_actual = (
        "*** Begin Patch\n"
        f"*** Update File: {tmp_path / 'actual-two.py'}\n"
        "*** End Patch"
    )
    quoted = f"tools.apply_patch({json.dumps(decoy)})"
    script = (
        f"const note = {json.dumps(quoted)};\n"
        f"// tools.apply_patch({json.dumps(decoy)});\n"
        f"/* tools.apply_patch({json.dumps(decoy)}); */\n"
        f"let actual = {json.dumps(actual)};\n"
        "await tools.apply_patch(actual);\n"
        f"await tools.apply_patch({json.dumps(second_actual)});"
    )
    data = _codex_outer_exec_bytes(tmp_path, script)
    observed = artifacts.observe_session_artifacts_in_window_bytes(
        data,
        str(tmp_path),
        START,
        END,
        session_id=SESSION,
    )
    assert {item["path"] for item in observed} == {"actual.py", "actual-two.py"}


def test_outer_exec_dynamic_patch_fails_closed_only_inside_window(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    script = (
        "const patch = buildPatch();\n"
        "await tools.apply_patch(patch);"
    )
    outside = _codex_outer_exec_bytes(
        tmp_path,
        script,
        timestamp="2026-08-04T11:59:00.000Z",
    )
    assert artifacts.observe_session_artifacts_in_window_bytes(
        outside,
        str(tmp_path),
        START,
        END,
        session_id=SESSION,
    ) == []

    inside = _codex_outer_exec_bytes(tmp_path, script)
    with pytest.raises(artifacts.ArtifactEvidenceError, match="exactly resolvable"):
        artifacts.observe_session_artifacts_in_window_bytes(
            inside,
            str(tmp_path),
            START,
            END,
            session_id=SESSION,
        )


def test_bytes_helper_ignores_broken_edit_outside_exact_window(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    rows = [
        {
            "timestamp": "2026-08-04T11:59:00.000Z",
            "sessionId": SESSION,
            "cwd": str(tmp_path),
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use",
                "name": "Edit",
                "input": {},
            }]},
        },
        {
            "timestamp": "2026-08-04T12:10:00.000Z",
            "sessionId": SESSION,
            "cwd": str(tmp_path),
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use",
                "id": "inside",
                "name": "Edit",
                "input": {"file_path": str(tmp_path / "src/inside.py")},
            }]},
        },
        {
            "timestamp": "2026-08-04T12:10:01.000Z",
            "sessionId": SESSION,
            "cwd": str(tmp_path),
            "type": "user",
            "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": "inside",
                "content": "updated",
            }]},
        },
    ]
    data = "".join(json.dumps(row) + "\n" for row in rows).encode()
    assert artifacts.observe_session_artifacts_in_window_bytes(
        data,
        str(tmp_path),
        START,
        END,
        session_id=SESSION,
    ) == [{"repo": str(tmp_path), "path": "src/inside.py"}]


@pytest.mark.parametrize(
    ("adapter", "expected_family"),
    [("claude", "claude-tool"), ("codex", "codex-custom")],
)
def test_artifact_result_index_retains_digest_not_raw_payload(
    tmp_path: Path,
    adapter: str,
    expected_family: str,
):
    identity = (adapter, "target-proof", SESSION)

    def resolve_identity(row_adapter, session_id, _cwd, _proof):
        if row_adapter == adapter and session_id == SESSION:
            return identity
        return None

    large_output = "unique-result-output:" + ("x" * 4_000_000)
    if adapter == "claude":
        rows = [{
            "timestamp": "2026-08-04T12:05:01.000Z",
            "sessionId": SESSION,
            "cwd": str(tmp_path),
            "type": "user",
            "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": "large-result",
                "content": large_output,
            }]},
        }]
    else:
        rows = [
            {
                "timestamp": "2026-08-04T12:05:00.000Z",
                "type": "session_meta",
                "payload": {"id": SESSION, "cwd": str(tmp_path)},
            },
            {
                "timestamp": "2026-08-04T12:05:01.000Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "large-result",
                    "output": large_output,
                },
            },
        ]
    data = "".join(
        json.dumps(row, separators=(",", ":")) + "\n" for row in rows
    ).encode()

    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    try:
        gc.collect()
        retained_before, _peak_before = tracemalloc.get_traced_memory()
        index = artifacts.build_artifact_evidence_index(
            ((S2_FILE, data),),
            resolve_identity=resolve_identity,
        )
        gc.collect()
        retained_after, _peak_after = tracemalloc.get_traced_memory()
    finally:
        if not was_tracing:
            tracemalloc.stop()

    indexed = index.results[(identity, "large-result")][0]
    assert indexed.family == expected_family
    assert len(indexed.fingerprint_sha256) == 64
    assert not hasattr(indexed, "payload")
    assert large_output not in repr(index)
    assert retained_after - retained_before < 2_000_000
