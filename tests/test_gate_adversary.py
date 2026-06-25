"""Step 4b-adversary — adversarial verdict layer (scope KB id=1343).

The actual `claude -p` call can't run in unit tests, so we exercise:
- adversary prompt construction (attacks the proposed verdict, cite-or-PROCEED)
- output parsing + normalization (envelope/fence unwrap, cite-or-PROCEED guard,
  invalid-delta coercion, malformed → safe default, question cleanup)
- the PROCEED-only + default-off firing lever
- run_gate wiring: fires only on PROCEED when enabled, attaches
  verdict["adversary"], emits one adversary.log row; never fires on
  MODIFY / when disabled / when use_llm=False.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import db           # noqa: E402
import embeddings   # noqa: E402
import gate         # noqa: E402
import log_utils    # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_adversary_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    try:
        conn.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def _ins(conn, kind, title, body, *, status="staging"):
    vec = embeddings.embed(f"{title}\n\n{body}")
    return db.insert_node(
        conn, kind=kind, title=title, body=body, status=status,
        embedding=embeddings.to_blob(vec),
    )


def _read_adversary(tmp):
    today = datetime.now(timezone.utc).date()
    return list(log_utils.read_log_range("adversary", today, today, tmp))


# ---------- prompt construction ----------

def _chain(query="Redis session cache"):
    return {
        "query": query,
        "seeds": [{
            "id": 200, "kind": "decision", "status": "canonical",
            "source": "hybrid", "title": "Redis chosen for session cache",
            "body_excerpt": "bake-off body",
        }],
        "chains": [{"seed_id": 200, "evidence": []}],
    }


def test_adversary_prompt_includes_verdict_request_and_cite_rule():
    verdict = {"recommendation": "PROCEED", "summary": "looks aligned",
               "evidence_nodes": [200]}
    p = gate.build_adversary_prompt(_chain(), verdict, max_chains=5)
    _assert("PROCEED" in p, "proposed recommendation should appear")
    _assert("Redis session cache" in p, "request should be echoed")
    _assert("CITE-OR-PROCEED" in p, "cite-or-PROCEED rule should be present")
    _assert("id=200" in p, "chain seed should render")
    print("PASS adversary_prompt_includes_verdict_request_and_cite_rule")


# ---------- parsing / normalization ----------

def test_parse_adversary_well_formed():
    raw = json.dumps({
        "objection": "ignores the abandoned LRU path",
        "counter_node_id": 202,
        "verdict_delta": "MODIFY",
        "design_decision_questions": [
            {"question": "shard by tenant?", "stake": "your infra call",
             "options_hint": ["yes", "no"]},
        ],
    })
    out = gate.parse_adversary_output(raw)
    _assert(out["counter_node_id"] == 202, f"counter id: {out}")
    _assert(out["verdict_delta"] == "MODIFY", f"delta: {out}")
    _assert(len(out["design_decision_questions"]) == 1, f"questions: {out}")
    _assert(out["error"] is None, f"no error expected: {out}")
    print("PASS parse_adversary_well_formed")


def test_parse_adversary_cite_or_proceed_guard():
    # A flip with no cited counter node must be downgraded to no-flip.
    raw = json.dumps({
        "objection": "I feel uneasy",
        "counter_node_id": None,
        "verdict_delta": "DO_NOT_PROCEED",
        "design_decision_questions": [],
    })
    out = gate.parse_adversary_output(raw)
    _assert(out["counter_node_id"] is None, f"counter stays None: {out}")
    _assert(out["verdict_delta"] == "none",
            f"uncited flip must downgrade to none: {out}")
    print("PASS parse_adversary_cite_or_proceed_guard")


def test_parse_adversary_coerces_invalid_delta():
    raw = json.dumps({"counter_node_id": 5, "verdict_delta": "REJECT"})
    out = gate.parse_adversary_output(raw)
    _assert(out["verdict_delta"] == "none", f"invalid delta → none: {out}")
    print("PASS parse_adversary_coerces_invalid_delta")


def test_parse_adversary_unwraps_envelope():
    inner = {"objection": "", "counter_node_id": None,
             "verdict_delta": "none", "design_decision_questions": []}
    env = {"type": "result", "result": json.dumps(inner)}
    out = gate.parse_adversary_output(json.dumps(env))
    _assert(out["verdict_delta"] == "none", f"envelope unwrap failed: {out}")
    _assert(out["error"] is None, f"no error: {out}")
    print("PASS parse_adversary_unwraps_envelope")


def test_parse_adversary_malformed_returns_safe_default():
    out = gate.parse_adversary_output("not json at all { broken")
    _assert(out["verdict_delta"] == "none", f"safe delta: {out}")
    _assert(out["counter_node_id"] is None, f"safe counter: {out}")
    _assert(out["design_decision_questions"] == [], f"safe questions: {out}")
    _assert(out["error"], f"error should be set: {out}")
    print("PASS parse_adversary_malformed_returns_safe_default")


def test_parse_adversary_drops_malformed_questions():
    raw = json.dumps({
        "counter_node_id": 9, "verdict_delta": "MODIFY",
        "design_decision_questions": [
            {"question": "", "stake": "x"},          # empty question → dropped
            "not a dict",                            # non-dict → dropped
            {"question": "keep this", "options_hint": "not-a-list"},
        ],
    })
    out = gate.parse_adversary_output(raw)
    qs = out["design_decision_questions"]
    _assert(len(qs) == 1, f"only the valid question survives: {qs}")
    _assert(qs[0]["question"] == "keep this", f"wrong question kept: {qs}")
    _assert(qs[0]["options_hint"] == [], f"bad options_hint → []: {qs}")
    print("PASS parse_adversary_drops_malformed_questions")


# ---------- firing lever ----------

def test_should_fire_only_proceed_and_enabled():
    orig = gate.ADVERSARY_ENABLED
    try:
        gate.ADVERSARY_ENABLED = True
        _assert(gate._should_fire_adversary({"recommendation": "PROCEED"}),
                "enabled + PROCEED should fire")
        _assert(not gate._should_fire_adversary({"recommendation": "MODIFY"}),
                "MODIFY should not fire")
        _assert(not gate._should_fire_adversary({"recommendation": None}),
                "skipped/None should not fire")
        gate.ADVERSARY_ENABLED = False
        _assert(not gate._should_fire_adversary({"recommendation": "PROCEED"}),
                "disabled should never fire")
        print("PASS should_fire_only_proceed_and_enabled")
    finally:
        gate.ADVERSARY_ENABLED = orig


# ---------- run_gate wiring ----------

def _proceed_stub(seed):
    def stub(chain_assembly, **kwargs):
        return {
            "recommendation": "PROCEED", "summary": "stubbed",
            "decision_chain": [seed], "abandoned_paths": [],
            "active_constraints": [], "current_direction": [seed],
            "risk_if_proceed": "", "better_next_action": "",
            "evidence_nodes": [seed], "error": None,
        }
    return stub


def _modify_stub(seed):
    def stub(chain_assembly, **kwargs):
        out = _proceed_stub(seed)(chain_assembly)
        out["recommendation"] = "MODIFY"
        return out
    return stub


def _adv_stub(seed):
    def stub(chain_assembly, verdict, **kwargs):
        return {
            "objection": "repeats abandoned path", "counter_node_id": seed,
            "verdict_delta": "MODIFY",
            "design_decision_questions": [
                {"question": "q1", "stake": "s1", "options_hint": []},
                {"question": "q2", "stake": "s2", "options_hint": []},
            ],
            "error": None,
        }
    return stub


def _run_with_stubs(conn, tmp, *, classify_stub, enabled, use_llm=True,
                    adv_stub=None):
    o_classify, o_adv, o_enabled = (
        gate.classify_gate, gate.adversary_classify, gate.ADVERSARY_ENABLED,
    )
    gate.classify_gate = classify_stub
    if adv_stub is not None:
        gate.adversary_classify = adv_stub
    gate.ADVERSARY_ENABLED = enabled
    try:
        return gate.run_gate(conn, "Redis session cache",
                             project_path=tmp, use_llm=use_llm)
    finally:
        gate.classify_gate = o_classify
        gate.adversary_classify = o_adv
        gate.ADVERSARY_ENABLED = o_enabled


def test_run_gate_fires_adversary_on_proceed_when_enabled():
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "body")
        out = _run_with_stubs(conn, tmp, classify_stub=_proceed_stub(seed),
                              enabled=True, adv_stub=_adv_stub(seed))
        adv = out["verdict"].get("adversary")
        _assert(adv is not None, "adversary should be attached on PROCEED")
        _assert(adv["counter_node_id"] == seed, f"counter id: {adv}")
        _assert(adv["verdict_delta"] == "MODIFY", f"delta: {adv}")
        # verdict itself is NOT auto-flipped (side-note v1).
        _assert(out["verdict"]["recommendation"] == "PROCEED",
                "verdict must not be auto-flipped")
        rows = _read_adversary(tmp)
        _assert(len(rows) == 1, f"one adversary.log row expected: {rows}")
        r = rows[0]
        _assert(r["verdict_before"] == "PROCEED", f"verdict_before: {r}")
        _assert(r["verdict_delta"] == "MODIFY", f"row delta: {r}")
        _assert(r["counter_node_id"] == seed, f"row counter: {r}")
        _assert(r["n_forks_raised"] == 2, f"row n_forks_raised: {r}")
        print("PASS run_gate_fires_adversary_on_proceed_when_enabled")
    finally:
        _cleanup(tmp, conn)


def test_run_gate_no_adversary_on_modify():
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "body")
        out = _run_with_stubs(conn, tmp, classify_stub=_modify_stub(seed),
                              enabled=True, adv_stub=_adv_stub(seed))
        _assert("adversary" not in out["verdict"],
                "MODIFY verdict must not trigger the adversary")
        _assert(_read_adversary(tmp) == [], "no adversary.log row on MODIFY")
        print("PASS run_gate_no_adversary_on_modify")
    finally:
        _cleanup(tmp, conn)


def test_run_gate_no_adversary_when_disabled():
    tmp, conn = _fresh_db()
    try:
        seed = _ins(conn, "decision", "Redis session cache", "body")
        out = _run_with_stubs(conn, tmp, classify_stub=_proceed_stub(seed),
                              enabled=False, adv_stub=_adv_stub(seed))
        _assert("adversary" not in out["verdict"],
                "disabled adversary must not attach")
        _assert(_read_adversary(tmp) == [], "no adversary.log row when disabled")
        print("PASS run_gate_no_adversary_when_disabled")
    finally:
        _cleanup(tmp, conn)


def test_run_gate_no_adversary_when_use_llm_false():
    # use_llm=False → skipped verdict (recommendation None) → never fires, even
    # if somehow enabled. Guards the offline/test path.
    tmp, conn = _fresh_db()
    try:
        _ins(conn, "decision", "Redis session cache", "body")
        o_enabled = gate.ADVERSARY_ENABLED
        gate.ADVERSARY_ENABLED = True
        try:
            out = gate.run_gate(conn, "Redis session cache",
                                project_path=tmp, use_llm=False)
        finally:
            gate.ADVERSARY_ENABLED = o_enabled
        _assert("adversary" not in out["verdict"],
                "use_llm=False must not fire the adversary")
        _assert(_read_adversary(tmp) == [], "no adversary.log row offline")
        print("PASS run_gate_no_adversary_when_use_llm_false")
    finally:
        _cleanup(tmp, conn)


if __name__ == "__main__":
    test_adversary_prompt_includes_verdict_request_and_cite_rule()
    test_parse_adversary_well_formed()
    test_parse_adversary_cite_or_proceed_guard()
    test_parse_adversary_coerces_invalid_delta()
    test_parse_adversary_unwraps_envelope()
    test_parse_adversary_malformed_returns_safe_default()
    test_parse_adversary_drops_malformed_questions()
    test_should_fire_only_proceed_and_enabled()
    test_run_gate_fires_adversary_on_proceed_when_enabled()
    test_run_gate_no_adversary_on_modify()
    test_run_gate_no_adversary_when_disabled()
    test_run_gate_no_adversary_when_use_llm_false()
    print("ALL gate_adversary tests passed")
