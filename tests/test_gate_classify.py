"""Step 9 / step 4b — gate classifier prompt + parser + run_gate.

The actual `claude -p` call cannot run in unit tests (cost + latency), so
we exercise:
- prompt construction: required system text, all four labels named,
  few-shot examples present, chain assembly serialized correctly,
  request echoed
- output parsing: well-formed verdict, JSON-envelope unwrap, markdown-fence
  unwrap, missing recommendation → error, invalid label → error,
  malformed JSON → error
- skip paths: use_llm=False, kill switch, in-compact, budget cap hit
- run_gate wiring: assemble + classify glued together; cited evidence
  hydrated from cited node ids; chains returned for drill-in
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import budget       # noqa: E402
import db           # noqa: E402
import embeddings   # noqa: E402
import gate         # noqa: E402
import log_utils    # noqa: E402
import paths        # noqa: E402

# This file exercises the CLASSIFIER path. The adversary layer is now default-ON
# (id=1343) and would fire a real second `claude -p` on the use_llm=True +
# PROCEED tests below — force it OFF here; the adversary is covered explicitly
# in test_gate_adversary.py.
gate.ADVERSARY_ENABLED = False


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_classify_")
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


# ---------- prompt construction ----------

def test_prompt_includes_all_four_labels():
    chain = {"query": "anything", "seeds": [], "chains": []}
    p = gate.build_classifier_prompt(chain)
    for label in gate.CLASSIFIER_LABELS:
        _assert(label in p, f"label {label!r} missing from prompt")
    print("PASS prompt_includes_all_four_labels")


def test_prompt_includes_few_shot_section():
    chain = {"query": "x", "seeds": [], "chains": []}
    p = gate.build_classifier_prompt(chain)
    _assert("EXAMPLE 1 (PROCEED)" in p, "PROCEED few-shot missing")
    _assert("EXAMPLE 2 (MODIFY)" in p, "MODIFY few-shot missing")
    _assert("EXAMPLE 3 (DO_NOT_PROCEED)" in p, "DO_NOT_PROCEED few-shot missing")
    _assert("--- END EXAMPLES ---" in p, "few-shot terminator missing")
    print("PASS prompt_includes_few_shot_section")


def test_prompt_anti_hedge_rule_present():
    """Open question 473 — the prompt must explicitly counter MODIFY hedge bias."""
    chain = {"query": "x", "seeds": [], "chains": []}
    p = gate.build_classifier_prompt(chain)
    _assert("Anti-hedge" in p, "anti-hedge rule missing from system prompt")
    print("PASS prompt_anti_hedge_rule_present")


def test_prompt_echoes_request():
    chain = {"query": "extend the Redis cache to the admin API", "seeds": [], "chains": []}
    p = gate.build_classifier_prompt(chain)
    _assert("REQUEST: extend the Redis cache to the admin API" in p,
            "actual request line missing from prompt")
    print("PASS prompt_echoes_request")


def test_prompt_renders_seeds_and_evidence():
    chain = {
        "query": "do a thing",
        "seeds": [{
            "id": 100, "kind": "decision", "title": "the seed title",
            "body_excerpt": "the seed body excerpt", "status": "canonical",
            "workstream_id": None, "source": "hybrid", "score": 0.9,
        }],
        "chains": [{
            "seed_id": 100,
            "evidence": [{
                "id": 200, "kind": "fact", "title": "evidence title",
                "body_excerpt": "evidence body", "status": "stale",
                "workstream_id": None,
                "via_relation": "supersedes", "direction": "out",
                "hop": 1, "path": [200],
            }],
        }],
    }
    p = gate.build_classifier_prompt(chain)
    _assert("seed [id=100, decision, status=canonical, source=hybrid] the seed title" in p,
            f"seed line malformed: see prompt")
    _assert("[id=200, hop=1, via=supersedes(out), status=stale, fact] evidence title" in p,
            "evidence line malformed")
    _assert("the seed body excerpt" in p, "seed body missing")
    _assert("evidence body" in p, "evidence body missing")
    print("PASS prompt_renders_seeds_and_evidence")


def test_prompt_handles_empty_chain():
    p = gate.build_classifier_prompt(
        {"query": "request", "seeds": [], "chains": []}
    )
    _assert("(no seeds — KB context is empty for this query)" in p,
            "empty-chain note missing")
    print("PASS prompt_handles_empty_chain")


def test_prompt_caps_chain_assembly_to_max_chains():
    seeds = [
        {
            "id": 100 + i, "kind": "decision", "title": f"seed-{i}",
            "body_excerpt": "", "status": "canonical", "workstream_id": None,
            "source": "hybrid", "score": 0.9 - i * 0.01,
        }
        for i in range(8)
    ]
    chain = {
        "query": "x",
        "seeds": seeds,
        "chains": [{"seed_id": s["id"], "evidence": []} for s in seeds],
    }
    p = gate.build_classifier_prompt(chain, max_chains=3)
    # Seeds 0,1,2 should appear; seeds 3+ should not.
    _assert("seed-0" in p and "seed-1" in p and "seed-2" in p,
            "first 3 seeds should render")
    for i in range(3, 8):
        _assert(f"seed-{i}" not in p,
                f"seed-{i} should be truncated by max_chains=3")
    print("PASS prompt_caps_chain_assembly_to_max_chains")


# ---------- output parsing ----------

_GOOD_VERDICT = {
    "recommendation": "PROCEED",
    "summary": "looks fine",
    "decision_chain": [1, 2],
    "abandoned_paths": [],
    "active_constraints": [3],
    "current_direction": [1],
    "risk_if_proceed": "minor",
    "better_next_action": "",
    "evidence_nodes": [1, 2, 3],
}


def test_parse_well_formed_json():
    out = gate.parse_classifier_output(json.dumps(_GOOD_VERDICT))
    _assert(out["recommendation"] == "PROCEED", f"recommendation: {out}")
    _assert(out["decision_chain"] == [1, 2], f"chain: {out}")
    _assert(out["error"] is None, f"error should be None: {out}")
    print("PASS parse_well_formed_json")


def test_parse_unwraps_claude_p_envelope():
    """`claude -p --output-format json` returns {"result": "<text>"}."""
    envelope = {"result": json.dumps(_GOOD_VERDICT)}
    out = gate.parse_classifier_output(json.dumps(envelope))
    _assert(out["recommendation"] == "PROCEED", f"unwrap failed: {out}")
    print("PASS parse_unwraps_claude_p_envelope")


def test_parse_unwraps_markdown_fence():
    raw = "```json\n" + json.dumps(_GOOD_VERDICT) + "\n```"
    out = gate.parse_classifier_output(raw)
    _assert(out["recommendation"] == "PROCEED", f"fence unwrap: {out}")
    print("PASS parse_unwraps_markdown_fence")


def test_parse_handles_invalid_label():
    bad = {**_GOOD_VERDICT, "recommendation": "MAYBE"}
    out = gate.parse_classifier_output(json.dumps(bad))
    _assert(out["recommendation"] is None, f"should reject invalid: {out}")
    _assert(out["error"] and "invalid recommendation" in out["error"],
            f"error message: {out}")
    print("PASS parse_handles_invalid_label")


def test_parse_handles_missing_recommendation():
    bad = {k: v for k, v in _GOOD_VERDICT.items() if k != "recommendation"}
    out = gate.parse_classifier_output(json.dumps(bad))
    _assert(out["recommendation"] is None, "missing rec should error")
    _assert(out["error"] is not None, f"error expected: {out}")
    print("PASS parse_handles_missing_recommendation")


def test_parse_handles_malformed_json():
    out = gate.parse_classifier_output("not json at all { broken")
    _assert(out["recommendation"] is None, "malformed should error")
    _assert(out["error"] is not None, f"error expected: {out}")
    print("PASS parse_handles_malformed_json")


def test_parse_handles_empty_output():
    for raw in (None, "", "   ", "\n\n"):
        out = gate.parse_classifier_output(raw)
        _assert(out["recommendation"] is None, f"empty {raw!r} should error")
        _assert(out["error"] is not None, f"error expected for {raw!r}: {out}")
    print("PASS parse_handles_empty_output")


def test_parse_coerces_id_lists_to_int():
    raw = json.dumps({**_GOOD_VERDICT, "decision_chain": ["10", "20", "abc", True, 30]})
    out = gate.parse_classifier_output(raw)
    _assert(out["decision_chain"] == [10, 20, 30],
            f"should drop non-int and coerce strings, got {out['decision_chain']}")
    print("PASS parse_coerces_id_lists_to_int")


def test_parse_extracts_json_buried_in_text():
    """The model sometimes adds chatter despite instructions."""
    raw = "Sure, here's the verdict:\n\n" + json.dumps(_GOOD_VERDICT) + "\n\nLet me know if you need more."
    out = gate.parse_classifier_output(raw)
    _assert(out["recommendation"] == "PROCEED",
            f"should extract embedded JSON: {out}")
    print("PASS parse_extracts_json_buried_in_text")


# ---------- skip paths ----------

def test_classify_skipped_when_use_llm_false():
    out = gate.classify_gate(
        {"query": "x", "seeds": [], "chains": []},
        project_path=None, use_llm=False,
    )
    _assert(out["recommendation"] is None, f"use_llm=False should skip: {out}")
    _assert(out.get("skipped") is True, f"skipped flag missing: {out}")
    _assert("use_llm=False" in (out.get("error") or ""),
            f"reason should mention use_llm=False: {out}")
    print("PASS classify_skipped_when_use_llm_false")


def test_classify_skipped_when_in_compact_env():
    import os
    prev = os.environ.get("CLAUDE_KB_IN_COMPACT")
    os.environ["CLAUDE_KB_IN_COMPACT"] = "1"
    try:
        out = gate.classify_gate(
            {"query": "x", "seeds": [], "chains": []},
            project_path=None, use_llm=True,
        )
        _assert(out.get("skipped") is True,
                f"in-compact reentrancy should skip: {out}")
        _assert("disabled/in-compact" in (out.get("error") or ""),
                f"reason should mention in-compact: {out}")
    finally:
        if prev is None:
            del os.environ["CLAUDE_KB_IN_COMPACT"]
        else:
            os.environ["CLAUDE_KB_IN_COMPACT"] = prev
    print("PASS classify_skipped_when_in_compact_env")


def test_classify_skipped_when_budget_cap_hit():
    """Pre-fill the budget to the cap so check_and_record returns False."""
    tmp = tempfile.mkdtemp(prefix="kb_classify_budget_")
    try:
        # Drive the count to exactly the cap so the next check_and_record
        # call (inside classify_gate) refuses.
        for _ in range(budget.DEFAULT_NONHEAL_DAILY_CAP):
            budget.record_invocation(tmp)
        out = gate.classify_gate(
            {"query": "x", "seeds": [], "chains": []},
            project_path=tmp, use_llm=True,
        )
        _assert(out.get("skipped") is True,
                f"budget cap should skip: {out}")
        _assert("budget" in (out.get("error") or "").lower(),
                f"reason should mention budget: {out}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS classify_skipped_when_budget_cap_hit")


# ---------- run_gate (4c wiring) ----------

def test_run_gate_with_use_llm_false_assembles_chain_and_skips_classify():
    """Verifies the wrapper glues assemble + classify and propagates the
    skipped verdict without touching the LLM."""
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        target = _ins(conn, "decision", "in-process LRU hypothesis",
                      "in-process LRU hypothesis body", status="stale")
        db.add_edge(conn, src=seed, dst=target, relation="supersedes")
        out = gate.run_gate(
            conn, "Redis session cache", project_path=tmp, use_llm=False,
        )
        _assert(out["request"] == "Redis session cache", f"request echoed: {out}")
        _assert(out["verdict"]["skipped"] is True,
                f"verdict should be skipped: {out['verdict']}")
        _assert(seed in {s["id"] for s in out["chains"]["seeds"]},
                f"chain assembly should include seed: {out['chains']}")
        # No verdict citations → no hydrated evidence.
        _assert(out["evidence"] == [], f"no cited evidence yet: {out['evidence']}")
        print("PASS run_gate_with_use_llm_false_assembles_chain_and_skips_classify")
    finally:
        _cleanup(tmp, conn)


def test_run_gate_hydrates_cited_evidence_when_verdict_returns():
    """Force a verdict by stubbing classify_gate to return cited ids;
    verify run_gate hydrates them into the compact evidence list."""
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        ev1 = _ins(conn, "fact", "alpha", "alpha body")
        ev2 = _ins(conn, "fact", "beta", "beta body")
        db.add_edge(conn, src=seed, dst=ev1, relation="related_to")
        db.add_edge(conn, src=seed, dst=ev2, relation="related_to")

        original = gate.classify_gate
        def stub(chain_assembly, **kwargs):
            return {
                "recommendation": "PROCEED",
                "summary": "stubbed",
                "decision_chain": [seed],
                "abandoned_paths": [],
                "active_constraints": [],
                "current_direction": [seed],
                "risk_if_proceed": "",
                "better_next_action": "",
                "evidence_nodes": [seed, ev1, ev2],
                "error": None,
            }
        gate.classify_gate = stub
        try:
            out = gate.run_gate(
                conn, "Redis session cache", project_path=tmp, use_llm=True,
            )
        finally:
            gate.classify_gate = original

        _assert(out["verdict"]["recommendation"] == "PROCEED",
                f"verdict should pass through: {out['verdict']}")
        ev_ids = {e["id"] for e in out["evidence"]}
        _assert(ev_ids == {seed, ev1, ev2},
                f"evidence hydration mismatch: {ev_ids}")
        # Compact form, not full body — only id/kind/title/status/workstream_id.
        for e in out["evidence"]:
            for k in ("id", "kind", "title", "status", "workstream_id"):
                _assert(k in e, f"compact field {k} missing: {e}")
            _assert("body" not in e and "body_excerpt" not in e,
                    f"hydration should be compact (no body): {e}")
        print("PASS run_gate_hydrates_cited_evidence_when_verdict_returns")
    finally:
        _cleanup(tmp, conn)


def test_run_gate_appends_jsonl_log_line():
    """Every run_gate() call must drop one JSONL row in gate.log
    with the fields needed for empirical tuning (timestamp, recommendation,
    evidence_ids, elapsed_ms, budget_count, seed_ids, query_hash). Query is
    hashed; the raw excerpt is omitted by default (structural-only invariant,
    id=1108 §3 / id=1225)."""
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        # use_llm=False → skipped verdict path; logging must still fire.
        gate.run_gate(
            conn, "Redis session cache", project_path=tmp, use_llm=False,
        )
        log_path = log_utils.today_log_path(gate.LOG_STREAM, tmp)
        _assert(log_path.exists(), f"log file should exist at {log_path}")
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        _assert(len(lines) == 1, f"exactly one log line: got {len(lines)}")
        entry = json.loads(lines[0])
        for k in (
            "ts", "project", "query_hash", "query_chars",
            "recommendation", "skipped", "evidence_ids", "decision_chain",
            "seed_count", "seed_ids", "reachable_count", "elapsed_ms",
            "budget_count",
        ):
            _assert(k in entry, f"required field {k!r} missing: {entry}")
        _assert(entry["skipped"] is True, f"skipped path should log True: {entry}")
        # Structural-only: no raw query text by default, anywhere in the row.
        _assert("query_excerpt" not in entry,
                f"raw query_excerpt must be omitted by default: {entry}")
        _assert("Redis session cache" not in lines[0],
                f"raw query text must not appear in the row: {lines[0]}")
        _assert(len(entry["query_hash"]) == 12,
                f"query_hash should be 12 chars: {entry}")
        _assert(seed in entry["seed_ids"],
                f"seed should appear in seed_ids: {entry}")
        _assert(isinstance(entry["elapsed_ms"], (int, float))
                and entry["elapsed_ms"] >= 0,
                f"elapsed_ms numeric and non-negative: {entry}")
        # Second call → second line, same file.
        gate.run_gate(
            conn, "Redis session cache", project_path=tmp, use_llm=False,
        )
        lines2 = log_path.read_text(encoding="utf-8").strip().splitlines()
        _assert(len(lines2) == 2, f"second call appends: got {len(lines2)}")
        print("PASS run_gate_appends_jsonl_log_line")
    finally:
        _cleanup(tmp, conn)


def test_run_gate_log_truncates_long_query():
    """When raw-query logging is opted in (CLAUDE_KB_LOG_RAW_QUERY), a long
    prompt must not bloat the line: query_excerpt is capped at
    LOG_QUERY_EXCERPT_CHARS; query_chars records the original length either
    way."""
    tmp, conn = _fresh_db()
    _prev = gate.LOG_RAW_QUERY
    gate.LOG_RAW_QUERY = True
    try:
        long_q = "x" * 5000
        gate.run_gate(
            conn, long_q, project_path=tmp, use_llm=False,
        )
        entry = json.loads(
            log_utils.today_log_path(gate.LOG_STREAM, tmp)
            .read_text(encoding="utf-8")
            .strip().splitlines()[-1]
        )
        _assert(len(entry["query_excerpt"]) == gate.LOG_QUERY_EXCERPT_CHARS,
                f"excerpt capped at {gate.LOG_QUERY_EXCERPT_CHARS}: "
                f"got {len(entry['query_excerpt'])}")
        _assert(entry["query_chars"] == 5000,
                f"original length recorded: {entry['query_chars']}")
        print("PASS run_gate_log_truncates_long_query")
    finally:
        gate.LOG_RAW_QUERY = _prev
        _cleanup(tmp, conn)


def test_run_gate_log_raw_query_opt_in():
    """query_excerpt is opt-in: absent by default, present (capped) only when
    CLAUDE_KB_LOG_RAW_QUERY is set. query_hash + query_chars are always
    present either way. Structural-only invariant id=1108 §3 / id=1225."""
    tmp, conn = _fresh_db()
    _prev = gate.LOG_RAW_QUERY
    try:
        # Default: off — no raw text in the row.
        gate.LOG_RAW_QUERY = False
        gate.run_gate(conn, "secret prompt body", project_path=tmp, use_llm=False)
        off = json.loads(
            log_utils.today_log_path(gate.LOG_STREAM, tmp)
            .read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        _assert("query_excerpt" not in off,
                f"excerpt must be off by default: {off}")
        _assert(len(off["query_hash"]) == 12
                and off["query_chars"] == len("secret prompt body"),
                f"hash + chars always present: {off}")
        # Opt-in: on — excerpt restored for local debugging.
        gate.LOG_RAW_QUERY = True
        gate.run_gate(conn, "secret prompt body", project_path=tmp, use_llm=False)
        on = json.loads(
            log_utils.today_log_path(gate.LOG_STREAM, tmp)
            .read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        _assert(on.get("query_excerpt") == "secret prompt body",
                f"excerpt present when opted in: {on}")
        print("PASS run_gate_log_raw_query_opt_in")
    finally:
        gate.LOG_RAW_QUERY = _prev
        _cleanup(tmp, conn)


def test_run_gate_log_propagates_session_id():
    """session_id passed to run_gate must land in the emitted gate.log row.
    Unblocks the Gap A+D correlator (id=1098) — gate.log session_id was
    previously hardcoded None."""
    tmp, conn = _fresh_db()
    try:
        sid = "c9122d49-test-fixture-sid"
        gate.run_gate(
            conn, "Redis session cache", project_path=tmp,
            session_id=sid, use_llm=False,
        )
        entry = json.loads(
            log_utils.today_log_path(gate.LOG_STREAM, tmp)
            .read_text(encoding="utf-8")
            .strip().splitlines()[-1]
        )
        _assert(entry.get("session_id") == sid,
                f"session_id must propagate to gate.log: got {entry.get('session_id')!r}")
        print("PASS run_gate_log_propagates_session_id")
    finally:
        _cleanup(tmp, conn)


def test_run_gate_log_session_id_defaults_to_none():
    """Calls that omit session_id continue to emit null (no regression for
    direct gate.run_gate callers that don't have a session context)."""
    tmp, conn = _fresh_db()
    try:
        gate.run_gate(
            conn, "Redis session cache", project_path=tmp, use_llm=False,
        )
        entry = json.loads(
            log_utils.today_log_path(gate.LOG_STREAM, tmp)
            .read_text(encoding="utf-8")
            .strip().splitlines()[-1]
        )
        _assert(entry.get("session_id") is None,
                f"missing session_id should emit null: {entry.get('session_id')!r}")
        print("PASS run_gate_log_session_id_defaults_to_none")
    finally:
        _cleanup(tmp, conn)


# ---------- citation-gap sufficiency check (id=1220 / id=1253) ----------

def test_prompt_includes_citation_gap_schema():
    """The classifier prompt must teach the load_bearing_claims schema + the
    gap_type vocabulary so the model emits the citation-gap fields."""
    p = gate.build_classifier_prompt({"query": "x", "seeds": [], "chains": []})
    _assert("load_bearing_claims" in p, "claims field missing from schema")
    _assert("Citation-gap rule" in p, "citation-gap instruction missing")
    for tok in ("kb_node", "user_input", "code_trace",
                "decision_or_history", "current_value_or_code", "unknowable"):
        _assert(tok in p, f"vocab token {tok!r} missing from prompt")
    print("PASS prompt_includes_citation_gap_schema")


def test_parse_load_bearing_claims_wellformed():
    claims = [
        {"claim": "A is the chosen store", "evidence_type": "kb_node",
         "evidence_ref": 200, "gap_type": None},
        {"claim": "user wants B", "evidence_type": "user_input",
         "evidence_ref": None, "gap_type": None},
        {"claim": "current value of X", "evidence_type": "code_trace",
         "evidence_ref": "foo.py:42", "gap_type": None},
        {"claim": "Y is the bottleneck", "evidence_type": "none",
         "evidence_ref": None, "gap_type": "current_value_or_code"},
    ]
    out = gate.parse_classifier_output(
        json.dumps({**_GOOD_VERDICT, "load_bearing_claims": claims})
    )
    _assert(len(out["load_bearing_claims"]) == 4, f"all 4 kept: {out['load_bearing_claims']}")
    _assert(out["load_bearing_claims"][0]["evidence_ref"] == 200,
            "int evidence_ref preserved")
    _assert(out["load_bearing_claims"][2]["evidence_ref"] == "foo.py:42",
            "string evidence_ref preserved")
    # Only the evidence_type='none' claim becomes an uncovered gap.
    _assert(len(out["uncovered_claims"]) == 1, f"one gap derived: {out['uncovered_claims']}")
    u = out["uncovered_claims"][0]
    _assert(u["claim"] == "Y is the bottleneck", f"gap claim text: {u}")
    _assert(u["suggested_remedy"] == "code_trace",
            f"current_value_or_code → code_trace: {u}")
    print("PASS parse_load_bearing_claims_wellformed")


def test_parse_load_bearing_claims_absent_defaults_empty():
    """Back-compat: a verdict with no load_bearing_claims field parses to empty
    lists, not an error — old classifier outputs keep working."""
    out = gate.parse_classifier_output(json.dumps(_GOOD_VERDICT))
    _assert(out["recommendation"] == "PROCEED", f"verdict still parses: {out}")
    _assert(out["load_bearing_claims"] == [], f"claims default empty: {out}")
    _assert(out["uncovered_claims"] == [], f"uncovered default empty: {out}")
    # A non-list value is tolerated → empty, never raises.
    out2 = gate.parse_classifier_output(
        json.dumps({**_GOOD_VERDICT, "load_bearing_claims": "not a list"})
    )
    _assert(out2["load_bearing_claims"] == [] and out2["uncovered_claims"] == [],
            f"non-list tolerated: {out2}")
    print("PASS parse_load_bearing_claims_absent_defaults_empty")


def test_parse_uncovered_remedy_mapping():
    """gap_type → suggested_remedy is the deterministic engine mapping
    (id=1156/id=1203). Unknown/missing gap_type falls to the safe default
    (flag_to_user — ask, don't assume)."""
    claims = [
        {"claim": "why decision", "evidence_type": "none", "evidence_ref": None,
         "gap_type": "decision_or_history"},
        {"claim": "exact value", "evidence_type": "none", "evidence_ref": None,
         "gap_type": "current_value_or_code"},
        {"claim": "cannot know", "evidence_type": "none", "evidence_ref": None,
         "gap_type": "unknowable"},
        {"claim": "gap no type", "evidence_type": "none", "evidence_ref": None},
        {"claim": "bad gap type", "evidence_type": "none", "evidence_ref": None,
         "gap_type": "bogus"},
    ]
    out = gate.parse_classifier_output(
        json.dumps({**_GOOD_VERDICT, "load_bearing_claims": claims})
    )
    remedy = {u["claim"]: u["suggested_remedy"] for u in out["uncovered_claims"]}
    _assert(remedy["why decision"] == "hop_deeper", f"map: {remedy}")
    _assert(remedy["exact value"] == "code_trace", f"map: {remedy}")
    _assert(remedy["cannot know"] == "flag_to_user", f"map: {remedy}")
    _assert(remedy["gap no type"] == "flag_to_user", f"missing → default: {remedy}")
    _assert(remedy["bad gap type"] == "flag_to_user", f"unknown → default: {remedy}")
    print("PASS parse_uncovered_remedy_mapping")


def test_parse_claims_drops_malformed():
    """Defensive parsing: non-dict entries and empty-claim entries are dropped;
    an unrecognized evidence_type is coerced to 'none' (treated as a gap, never
    as covered) so a mistagged claim can't silently pass as backed."""
    claims = [
        "not a dict",
        42,
        {"claim": "", "evidence_type": "kb_node"},          # empty text → dropped
        {"evidence_type": "kb_node", "evidence_ref": 1},     # no claim → dropped
        {"claim": "weird tag", "evidence_type": "banana", "evidence_ref": 5},
    ]
    out = gate.parse_classifier_output(
        json.dumps({**_GOOD_VERDICT, "load_bearing_claims": claims})
    )
    _assert(len(out["load_bearing_claims"]) == 1,
            f"only the one salvageable entry kept: {out['load_bearing_claims']}")
    kept = out["load_bearing_claims"][0]
    _assert(kept["claim"] == "weird tag", f"kept claim: {kept}")
    _assert(kept["evidence_type"] == "none",
            f"unrecognized tag coerced to none: {kept}")
    _assert(len(out["uncovered_claims"]) == 1
            and out["uncovered_claims"][0]["suggested_remedy"] == "flag_to_user",
            f"coerced-none becomes a default-remedy gap: {out['uncovered_claims']}")
    print("PASS parse_claims_drops_malformed")


def test_error_results_carry_claim_keys():
    """Error/skip results must still carry the claim keys (empty) so callers
    render uniformly without KeyErrors."""
    out = gate.parse_classifier_output("not json at all { broken")
    _assert(out["recommendation"] is None and out["error"], f"is an error: {out}")
    _assert(out["load_bearing_claims"] == [] and out["uncovered_claims"] == [],
            f"error result carries empty claim keys: {out}")
    skip = gate.classify_gate(
        {"query": "x", "seeds": [], "chains": []},
        project_path=None, use_llm=False,
    )
    _assert(skip["load_bearing_claims"] == [] and skip["uncovered_claims"] == [],
            f"skip result carries empty claim keys: {skip}")
    print("PASS error_results_carry_claim_keys")


def _stub_verdict_with_claims(seed):
    return {
        "recommendation": "PROCEED", "summary": "stub",
        "decision_chain": [seed], "abandoned_paths": [], "active_constraints": [],
        "current_direction": [seed], "risk_if_proceed": "", "better_next_action": "",
        "evidence_nodes": [seed],
        "load_bearing_claims": [
            {"claim": "claim_zzz_one", "evidence_type": "kb_node",
             "evidence_ref": seed, "gap_type": None},
            {"claim": "claim_zzz_two", "evidence_type": "none",
             "evidence_ref": None, "gap_type": "unknowable"},
        ],
        "uncovered_claims": [
            {"claim": "claim_zzz_two", "gap_type": "unknowable",
             "suggested_remedy": "flag_to_user"},
        ],
        "error": None,
    }


def test_run_gate_log_structural_claim_counts():
    """gate.log gains structural citation-gap signal: counts + evidence_type /
    gap_type histograms — and NO claim text by default (id=1108 §3 / id=1220)."""
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "seed", "seed body")
        original = gate.classify_gate
        gate.classify_gate = lambda chain_assembly, **kw: _stub_verdict_with_claims(seed)
        try:
            gate.run_gate(conn, "request text", project_path=tmp, use_llm=True)
        finally:
            gate.classify_gate = original
        line = (log_utils.today_log_path(gate.LOG_STREAM, tmp)
                .read_text(encoding="utf-8").strip().splitlines()[-1])
        entry = json.loads(line)
        _assert(entry["load_bearing_claim_count"] == 2, f"claim count: {entry}")
        _assert(entry["uncovered_claim_count"] == 1, f"uncovered count: {entry}")
        _assert(entry["evidence_type_counts"]["kb_node"] == 1
                and entry["evidence_type_counts"]["none"] == 1,
                f"evidence_type histogram: {entry['evidence_type_counts']}")
        _assert(entry["gap_type_counts"]["unknowable"] == 1,
                f"gap_type histogram: {entry['gap_type_counts']}")
        # Structural-only: claim text must not leak into the row by default.
        _assert("claim_zzz_one" not in line and "claim_zzz_two" not in line,
                f"claim text must not appear in the log row: {line}")
        _assert("uncovered_claim_texts" not in entry,
                f"claim texts opt-in only: {entry}")
        print("PASS run_gate_log_structural_claim_counts")
    finally:
        _cleanup(tmp, conn)


def test_run_gate_log_claim_texts_opt_in():
    """Claim text is content — emitted only when CLAUDE_KB_LOG_RAW_QUERY is on,
    same opt-in as query_excerpt."""
    tmp, conn = _fresh_db()
    _prev = gate.LOG_RAW_QUERY
    original = gate.classify_gate
    try:
        seed = _ins(conn, "decision", "seed", "seed body")
        gate.classify_gate = lambda chain_assembly, **kw: _stub_verdict_with_claims(seed)
        gate.LOG_RAW_QUERY = True
        gate.run_gate(conn, "request text", project_path=tmp, use_llm=True)
        entry = json.loads(
            log_utils.today_log_path(gate.LOG_STREAM, tmp)
            .read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        _assert(entry.get("uncovered_claim_texts") == ["claim_zzz_two"],
                f"uncovered claim texts present when opted in: {entry}")
        print("PASS run_gate_log_claim_texts_opt_in")
    finally:
        gate.classify_gate = original
        gate.LOG_RAW_QUERY = _prev
        _cleanup(tmp, conn)


if __name__ == "__main__":
    test_prompt_includes_all_four_labels()
    test_prompt_includes_few_shot_section()
    test_prompt_anti_hedge_rule_present()
    test_prompt_echoes_request()
    test_prompt_renders_seeds_and_evidence()
    test_prompt_handles_empty_chain()
    test_prompt_caps_chain_assembly_to_max_chains()
    test_parse_well_formed_json()
    test_parse_unwraps_claude_p_envelope()
    test_parse_unwraps_markdown_fence()
    test_parse_handles_invalid_label()
    test_parse_handles_missing_recommendation()
    test_parse_handles_malformed_json()
    test_parse_handles_empty_output()
    test_parse_coerces_id_lists_to_int()
    test_parse_extracts_json_buried_in_text()
    test_classify_skipped_when_use_llm_false()
    test_classify_skipped_when_in_compact_env()
    test_classify_skipped_when_budget_cap_hit()
    test_run_gate_with_use_llm_false_assembles_chain_and_skips_classify()
    test_run_gate_hydrates_cited_evidence_when_verdict_returns()
    test_run_gate_appends_jsonl_log_line()
    test_run_gate_log_truncates_long_query()
    test_run_gate_log_propagates_session_id()
    test_run_gate_log_session_id_defaults_to_none()
    test_prompt_includes_citation_gap_schema()
    test_parse_load_bearing_claims_wellformed()
    test_parse_load_bearing_claims_absent_defaults_empty()
    test_parse_uncovered_remedy_mapping()
    test_parse_claims_drops_malformed()
    test_error_results_carry_claim_keys()
    test_run_gate_log_structural_claim_counts()
    test_run_gate_log_claim_texts_opt_in()
    print("\nAll gate classifier tests pass.")
