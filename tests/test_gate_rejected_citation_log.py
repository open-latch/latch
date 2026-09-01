"""Item-3 regression for the V4 build (KB id=4626): citation persistence,
id lists only.

Pins:
- the classifier contract teaches `cited_rejected_paths` and the
  rejected[rp=<id>] notation (the citation mechanism item 2 made possible);
- parse_classifier_output coerces the field like every other id list and
  error verdicts carry it as [];
- a REAL fixture run_gate call (real classify_gate through the fake claude
  binary seam, real prompt build, real parse, real _log_invocation) logs
  `surfaced_rejected_paths` and `cited_rejected_paths` as int lists;
- a cited id that was never surfaced is clamped out (hallucination guard);
- zero new text fields: rejection option/reason prose never reaches the log
  row (canonical 3915 / ratified 3985 / id=1108 §3).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

from latch.store import db          # noqa: E402
from latch.retrieval import embeddings  # noqa: E402
from latch.gate import gate        # noqa: E402
from latch.common import log_utils   # noqa: E402

# Stop the default-ON adversary from firing a second real backend call
# (same module-level convention as test_gate_classify.py).
gate.ADVERSARY_ENABLED = False


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_cite_log_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    try:
        conn.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def _ins(conn, kind, title, body, *, status="staging", workstream_id=None):
    vec = embeddings.embed(f"{title}\n\n{body}")
    return db.insert_node(
        conn, kind=kind, title=title, body=body, status=status,
        embedding=embeddings.to_blob(vec), workstream_id=workstream_id,
    )


def _force_seed(conn, node_id):
    row = conn.execute(
        "SELECT id, kind, title, body, status, workstream_id FROM nodes "
        "WHERE id = ?", (node_id,),
    ).fetchone()
    hit = dict(row)
    hit["score"] = 1.0
    return lambda *a, **kw: [hit]


def _fake_claude(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env bash\n"
        "cat >/dev/null\n"
        "printf '%s\\n' \"$FAKE_GATE_RESPONSE\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _verdict_json(recommendation, cited, chain_id):
    return json.dumps({
        "recommendation": recommendation,
        "summary": "typed rejection bears on the request",
        "decision_chain": [chain_id],
        "abandoned_paths": [],
        "active_constraints": [],
        "current_direction": [chain_id],
        "risk_if_proceed": "revives a rejected option",
        "better_next_action": "follow the surviving path",
        "evidence_nodes": [chain_id],
        "cited_rejected_paths": cited,
        "load_bearing_claims": [],
    })


def _restore_env(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def _run_fixture_call(response_builder):
    """Run one REAL run_gate call over a fixture vault with a rejected row,
    faking only the model binary. Returns (rid, log entry dict, raw line)."""
    tmp, conn = _fresh_db()
    fake_dir = Path(tempfile.mkdtemp(prefix="kb_cite_fake_"))
    orig_search = gate.search.hybrid_search
    old_claude = gate.CLAUDE_BIN
    old_response = os.environ.get("FAKE_GATE_RESPONSE")
    try:
        nid = _ins(
            conn, "decision", "Queue engine chosen",
            "Redis Streams adopted for the job queue.", status="canonical",
        )
        rid = db.insert_rejected_path(
            conn, nid,
            option="in-process queue",
            reason="loses state across worker restarts",
        )
        gate.search.hybrid_search = _force_seed(conn, nid)
        gate.CLAUDE_BIN = str(_fake_claude(fake_dir / "claude"))
        os.environ["FAKE_GATE_RESPONSE"] = response_builder(rid, nid)
        gate.run_gate(
            conn, "re-try the in-process queue",
            project_path=tmp, use_llm=True,
        )
        line = (
            log_utils.today_log_path(gate.LOG_STREAM, tmp)
            .read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        return rid, json.loads(line), line
    finally:
        gate.search.hybrid_search = orig_search
        gate.CLAUDE_BIN = old_claude
        _restore_env("FAKE_GATE_RESPONSE", old_response)
        shutil.rmtree(fake_dir, ignore_errors=True)
        _cleanup(tmp, conn)


def test_spec_and_examples_teach_cited_rejected_paths():
    _assert(
        '"cited_rejected_paths"' in gate.CLASSIFIER_SYSTEM,
        "verdict contract must include cited_rejected_paths",
    )
    _assert(
        "rejected[rp=" in gate.CLASSIFIER_SYSTEM,
        "contract must document the rejected[rp=<id>] notation",
    )
    _assert(
        '"cited_rejected_paths":[]' in gate.CLASSIFIER_FEW_SHOT,
        "few-shot must show the empty-citation case",
    )
    _assert(
        "rejected[rp=7]" in gate.CLASSIFIER_FEW_SHOT
        and '"cited_rejected_paths":[7]' in gate.CLASSIFIER_FEW_SHOT,
        "few-shot must show a surfaced rejection actually being cited",
    )
    print("PASS spec_and_examples_teach_cited_rejected_paths")


def test_parse_coerces_cited_rejected_paths():
    raw = _verdict_json("MODIFY", ["3", 4, "x", True, None], 300)
    out = gate.parse_classifier_output(raw)
    _assert(
        out["cited_rejected_paths"] == [3, 4],
        f"coercion wrong: {out.get('cited_rejected_paths')!r}",
    )
    absent = json.loads(_verdict_json("PROCEED", [], 300))
    absent.pop("cited_rejected_paths")
    out2 = gate.parse_classifier_output(json.dumps(absent))
    _assert(
        out2["cited_rejected_paths"] == [],
        f"absent field must default to []: {out2.get('cited_rejected_paths')!r}",
    )
    print("PASS parse_coerces_cited_rejected_paths")


def test_error_verdicts_carry_cited_key():
    out = gate.parse_classifier_output("not json at all")
    _assert(
        out["cited_rejected_paths"] == [],
        f"error verdict must carry cited_rejected_paths=[]: {out}",
    )
    unlatched = gate.unlatched_verdict()
    _assert(
        unlatched["cited_rejected_paths"] == [],
        f"unlatched verdict must carry cited_rejected_paths=[]: {unlatched}",
    )
    print("PASS error_verdicts_carry_cited_key")


def test_citation_ids_logged_on_real_fixture_call():
    rid, entry, line = _run_fixture_call(
        lambda rid, nid: _verdict_json("MODIFY", [rid], nid)
    )
    _assert(
        entry.get("surfaced_rejected_paths") == [rid],
        f"surfaced_rejected_paths wrong: {entry.get('surfaced_rejected_paths')!r}",
    )
    _assert(
        entry.get("cited_rejected_paths") == [rid],
        f"cited_rejected_paths wrong: {entry.get('cited_rejected_paths')!r}",
    )
    for key in ("surfaced_rejected_paths", "cited_rejected_paths"):
        v = entry[key]
        _assert(
            isinstance(v, list)
            and all(isinstance(x, int) and not isinstance(x, bool) for x in v),
            f"{key} must be a pure int list: {v!r}",
        )
    _assert(entry["recommendation"] == "MODIFY", entry["recommendation"])
    # Zero new text fields: rejection prose stays in the prompt, never the log.
    _assert(
        "in-process queue" not in line
        and "loses state across worker restarts" not in line,
        f"rejection option/reason text leaked into the log row: {line}",
    )
    print("PASS citation_ids_logged_on_real_fixture_call")


def test_cited_ids_clamped_to_surfaced():
    rid, entry, _ = _run_fixture_call(
        lambda rid, nid: _verdict_json("MODIFY", [rid, 424242], nid)
    )
    _assert(
        entry.get("cited_rejected_paths") == [rid],
        f"unsurfaced id must be clamped out: {entry.get('cited_rejected_paths')!r}",
    )
    print("PASS cited_ids_clamped_to_surfaced")


def test_surfaced_logged_even_without_citation():
    rid, entry, _ = _run_fixture_call(
        lambda rid, nid: _verdict_json("PROCEED", [], nid)
    )
    _assert(
        entry.get("surfaced_rejected_paths") == [rid],
        f"surfaced must be logged without citation: {entry.get('surfaced_rejected_paths')!r}",
    )
    _assert(
        entry.get("cited_rejected_paths") == [],
        f"cited must be [] here: {entry.get('cited_rejected_paths')!r}",
    )
    print("PASS surfaced_logged_even_without_citation")


if __name__ == "__main__":
    test_spec_and_examples_teach_cited_rejected_paths()
    test_parse_coerces_cited_rejected_paths()
    test_error_verdicts_carry_cited_key()
    test_citation_ids_logged_on_real_fixture_call()
    test_cited_ids_clamped_to_surfaced()
    test_surfaced_logged_even_without_citation()
