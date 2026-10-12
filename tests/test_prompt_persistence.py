"""The active set must describe KB hits actually returned to the agent."""
import json
import sqlite3
import time
from types import SimpleNamespace

import numpy as np
import pytest

from latch.hooks import user_prompt_submit as ups
from latch.store import db, paths
from test_prompt_hook_context import _stub_main


def _ready_prompt(monkeypatch, tmp_path):
    logs = _stub_main(monkeypatch, tmp_path, prompt="retrieve the stored database decision")
    vector = np.zeros(db.VEC_DIM, dtype=np.float32)
    vector[0] = 1
    conn = db.connect(str(tmp_path))
    node_id = db.insert_node(
        conn, kind="fact", title="Database writes preserve returned context",
        body="Only returned KB hits belong in the session active set.",
        status="canonical", embedding=vector.tobytes(),
    )
    conn.close()
    ups._load_runtime()
    clock = [time.perf_counter()]
    monkeypatch.setattr(ups, "_PROCESS_STARTED", clock[0])
    monkeypatch.setattr(ups, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    monkeypatch.setattr(
        ups.mcp_broker, "inspect_live_embed_discovery",
        lambda **kw: SimpleNamespace(status="ready"),
    )
    monkeypatch.setattr(ups.mcp_broker, "emit_lifecycle", lambda *a, **kw: None)
    monkeypatch.setattr(ups, "_embed_with_bounded_wake", lambda *a, **kw: vector)
    return node_id, clock, logs


def _recorded_ids(tmp_path):
    conn = sqlite3.connect(paths.db_path(str(tmp_path)))
    try:
        return [row[0] for row in conn.execute(
            "SELECT node_id FROM session_retrievals WHERE session_id='session-1'"
        )]
    finally:
        conn.close()


@pytest.mark.parametrize("failed_update", ["upsert_session", "update_last_prompt_embedding"])
def test_session_interruption_does_not_suppress_unseen_hits(
    monkeypatch, tmp_path, capsys, failed_update,
):
    node_id, clock, logs = _ready_prompt(monkeypatch, tmp_path)
    original = getattr(db, failed_update)

    def interrupted(*args, **kwargs):
        clock[0] += 10
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(db, failed_update, interrupted)
    assert ups.main() == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "local retrieval budget" in context
    assert logs[-1]["skip"] == "local_budget_exhausted"
    assert not logs[-1].get("injected")
    assert _recorded_ids(tmp_path) == []

    # A subsequent healthy prompt must still be able to receive the same hit.
    monkeypatch.setattr(db, failed_update, original)
    clock[0] = time.perf_counter()
    monkeypatch.setattr(ups, "_PROCESS_STARTED", clock[0])
    assert ups.main() == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert f"id={node_id}," in context
    assert _recorded_ids(tmp_path) == [node_id]


def test_committed_hit_is_returned_even_when_final_write_exceeds_deadline(
    monkeypatch, tmp_path, capsys,
):
    node_id, clock, logs = _ready_prompt(monkeypatch, tmp_path)
    committed = [False]
    original_record = db.record_retrievals
    original_update = db.update_last_prompt_embedding
    original_upsert = db.upsert_session

    def record_then_expire(*args, **kwargs):
        result = original_record(*args, **kwargs)
        committed[0] = True
        clock[0] += 10
        return result

    def check_no_late_write(original):
        def checked(*args, **kwargs):
            assert not committed[0], "session write occurred after committed injection markers"
            return original(*args, **kwargs)
        return checked

    monkeypatch.setattr(db, "record_retrievals", record_then_expire)
    monkeypatch.setattr(db, "upsert_session", check_no_late_write(original_upsert))
    monkeypatch.setattr(db, "update_last_prompt_embedding", check_no_late_write(original_update))
    assert ups.main() == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert committed[0]
    assert f"id={node_id}," in context
    assert "temporarily unavailable" not in context
    assert logs[-1]["overran_budget"] is True
    assert not logs[-1].get("skip")
    assert _recorded_ids(tmp_path) == [node_id]
