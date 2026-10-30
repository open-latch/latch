"""Regressions for review round 2 (KB id=5302), one per surviving finding.

Round 2 passed F1, F2 and F5 and failed F3, F4 and F6. The review budget
(id=4562) is now spent, so these are the last three defects the harness gets to
be wrong about; each is pinned by number.
"""
import json
import subprocess
from pathlib import Path

import pytest

from latch.evals import adherence
from latch.evals import adherence_cli
from latch.evals import adherence_elicit
from latch.store import paths


REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE_FIXTURE = REPO_ROOT / "benchmarks" / "fixtures" / "adherence_smoke_v1.jsonl"


@pytest.fixture
def specs():
    return adherence.load_probe_sets([SMOKE_FIXTURE])


# ---------- F3: the scored corpus check was pathname-based ----------

def test_f3_a_byte_identical_copy_elsewhere_is_still_refused(tmp_path, specs):
    """Round 2's exact bypass: copy the smoke fixture and score the copy."""
    copy = tmp_path / "totally-not-the-smoke-fixture.jsonl"
    copy.write_bytes(SMOKE_FIXTURE.read_bytes())
    assert copy.resolve() != SMOKE_FIXTURE.resolve(), "the paths must differ"

    problems = adherence.validate_scored_corpus(
        specs, [copy], smoke_fixture=SMOKE_FIXTURE,
    )
    assert any("byte-identical" in p for p in problems), problems


def test_f3_a_probe_set_without_provenance_source_is_refused(tmp_path):
    """Protocol §8.1: a set that cannot state its provenance cannot be scored."""
    spec = {
        "set_id": "no-provenance",
        "decision": {"title": "t", "text": "b"},
        "e3": {"probe_id": "x-e3", "request": "r", "forbidden_paths": ["a.py"]},
        "e4": {"probe_id": "x-e4", "request": "r"},
    }
    problems = adherence.validate_scored_corpus(
        [spec], [], smoke_fixture=SMOKE_FIXTURE,
    )
    assert any("provenance.source" in p for p in problems), problems


def test_f3_an_invalid_provenance_source_is_refused():
    spec = {
        "set_id": "bad-provenance",
        "provenance": {"source": "invented"},
        "decision": {"title": "t", "text": "b"},
        "e3": {"probe_id": "x-e3", "request": "r", "forbidden_paths": ["a.py"]},
        "e4": {"probe_id": "x-e4", "request": "r"},
    }
    problems = adherence.validate_scored_corpus(
        [spec], [], smoke_fixture=SMOKE_FIXTURE,
    )
    assert any("provenance.source" in p for p in problems), problems


def test_f3_scripted_responses_cannot_be_scored(specs):
    """Structural: only a synthetic tier carries canned answers."""
    problems = adherence.validate_scored_corpus(
        specs, [], smoke_fixture=SMOKE_FIXTURE,
    )
    assert any("scripted_responses" in p for p in problems), problems


def test_f3_a_genuine_mined_corpus_passes(tmp_path):
    mined = {
        "set_id": "mined-42",
        "provenance": {"source": "backfill", "rejected_path_id": 42, "node_id": 7},
        "decision": {"title": "t", "text": "b"},
        "e3": {"probe_id": "m-e3", "request": "r", "forbidden_paths": ["a.py"]},
        "e4": {"probe_id": "m-e4", "request": "r"},
    }
    fixture = tmp_path / "mined.jsonl"
    fixture.write_text(json.dumps(mined) + "\n", encoding="utf-8")
    assert adherence.validate_scored_corpus(
        [mined], [fixture], smoke_fixture=SMOKE_FIXTURE,
    ) == []


# ---------- F4: falsifiers fired on incomplete data ----------

def _rows(*, b_e3_timeouts=0, d_wrong_blocks=0):
    rows = []
    for arm in adherence.ARMS:
        for i in range(3):
            errored = arm == "B" and i < b_e3_timeouts
            rows.append({
                "arm": arm, "probe_type": "E3", "elicited_decision": "BLOCK",
                # Every observed B E3 cell adheres, which is what makes F1
                # fire on a shrunken denominator.
                "violated": False,
                "error": "timeout" if errored else None,
            })
        for i in range(3):
            block = arm == "D" and i < d_wrong_blocks
            rows.append({
                "arm": arm, "probe_type": "E4",
                "elicited_decision": "BLOCK" if block else "PROCEED",
                "hard_wrong_block": block, "friction_modify": False, "error": None,
            })
    return rows


def test_f4_one_timeout_does_not_fire_the_decisive_stop():
    """Round 2's reproduction: a single B/E3 timeout fired F1's
    "STOP — there is no problem to solve", which would end the V7 line on one
    dropped observation."""
    agg = adherence.aggregate(
        _rows(b_e3_timeouts=1), expected_cells=18, n_per_cell=1,
    )
    assert agg["pass_fail"] == "INDETERMINATE"
    assert [f["id"] for f in agg["falsifiers_fired"]] == []
    deferred = {f["id"] for f in agg["falsifiers_deferred"]}
    assert deferred == {"F1", "F2"}


def test_f4_deferred_falsifiers_say_why():
    agg = adherence.aggregate(
        _rows(b_e3_timeouts=1), expected_cells=18, n_per_cell=1,
    )
    for entry in agg["falsifiers_deferred"]:
        assert "complete coverage" in entry["reason"]


def test_f4_an_observed_wrong_block_still_fires_on_incomplete_data():
    """F3 fires on evidence that HAPPENED, so missing cells elsewhere cannot
    manufacture it, and 3865 C3 is zero-tolerance."""
    agg = adherence.aggregate(
        _rows(b_e3_timeouts=1, d_wrong_blocks=1), expected_cells=18, n_per_cell=1,
    )
    assert agg["pass_fail"] == "INDETERMINATE"
    assert "F3" in {f["id"] for f in agg["falsifiers_fired"]}


def test_f4_complete_coverage_still_evaluates_every_falsifier():
    agg = adherence.aggregate(_rows(), expected_cells=18, n_per_cell=1)
    assert agg["falsifiers_deferred"] == []
    # All three arms adhere on every E3 cell here, so F1 and F2 both hold.
    fired = {f["id"] for f in agg["falsifiers_fired"]}
    assert {"F1", "F2"} <= fired


# ---------- F6: the child inherited ambient customizations ----------

def test_f6_claude_child_disables_all_customizations():
    """--safe-mode is the load-bearing flag: it disables hooks, MCP, CLAUDE.md,
    plugins and commands while preserving auth (unlike --bare, which forces
    API-key auth and would break an OAuth host)."""
    argv = adherence_elicit._claude_argv("/bin/true", None)
    assert "--safe-mode" in argv
    assert "--setting-sources" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv


def test_f6_context_tokens_are_read_from_the_envelope():
    """The objective signal. Marker matching missed round 2's contamination
    because the child never quoted the injected text — it just ingested 218k
    tokens of it and answered something else."""
    raw = json.dumps({"usage": {
        "input_tokens": 18,
        "cache_read_input_tokens": 218108,
        "cache_creation_input_tokens": 9854,
    }})
    assert adherence_elicit.context_tokens(raw) == 227980
    assert adherence_elicit.context_tokens(None) is None
    assert adherence_elicit.context_tokens("not json") is None


def test_f6_an_over_budget_child_is_not_clean(monkeypatch, tmp_path):
    contaminated = json.dumps({
        "result": adherence_elicit.CANARY_TOKEN + "\nNONE",
        "usage": {"input_tokens": 18, "cache_read_input_tokens": 218108},
    })
    monkeypatch.setattr(
        adherence_elicit, "invoke",
        lambda *a, **kw: adherence_elicit.Elicitation(
            adherence_elicit.CANARY_TOKEN + "\nNONE", None, False, "claude",
            "v", [], contaminated,
        ),
    )
    result = adherence_elicit.contamination_probe(
        backend="claude", workspace=tmp_path,
    )
    assert result["clean"] is False
    assert result["context_tokens"] == 218126
    assert any("ambient customizations" in r for r in result["reasons"])


def test_f6_a_clean_child_passes(monkeypatch, tmp_path):
    clean = json.dumps({
        "result": adherence_elicit.CANARY_TOKEN + "\nNONE",
        "usage": {"input_tokens": 2, "cache_read_input_tokens": 16084,
                  "cache_creation_input_tokens": 6873},
    })
    monkeypatch.setattr(
        adherence_elicit, "invoke",
        lambda *a, **kw: adherence_elicit.Elicitation(
            adherence_elicit.CANARY_TOKEN + "\nNONE", None, False, "claude",
            "v", [], clean,
        ),
    )
    result = adherence_elicit.contamination_probe(
        backend="claude", workspace=tmp_path,
    )
    assert result["clean"] is True, result["reasons"]
    assert result["context_tokens"] == 22959


def test_f6_the_unlatched_banner_is_treated_as_contamination():
    """LATCH_UNLATCHED=1 did not silence the hook — it made it emit this
    instead of KB hits. Still latch text in the child's prompt."""
    markers = adherence_elicit.detect_contamination(
        "CANARY-7F3A\nLatch is currently UNLATCHED. Run /unlatch to re-latch."
    )
    assert "Latch is currently UNLATCHED" in markers


def test_f6_kb_hit_injection_is_treated_as_contamination():
    markers = adherence_elicit.detect_contamination(
        "CANARY-7F3A\n## KB hits (similarity sample)\n- (decision, id=1) ..."
    )
    assert "KB hits" in markers


def test_f6_a_child_that_ignores_the_canary_is_not_clean(monkeypatch, tmp_path):
    """Round 2's contaminated child answered "Standing by." — no markers, but
    plainly not answering the prompt it was given."""
    monkeypatch.setattr(
        adherence_elicit, "invoke",
        lambda *a, **kw: adherence_elicit.Elicitation(
            "Standing by.", None, False, "claude", "v", [],
            json.dumps({"usage": {"input_tokens": 10}}),
        ),
    )
    result = adherence_elicit.contamination_probe(
        backend="claude", workspace=tmp_path,
    )
    assert result["clean"] is False
    assert any("canary" in r for r in result["reasons"])


def test_f6_a_scored_run_may_not_skip_the_contamination_check(tmp_path):
    args = type("Args", (), {
        "skip_contamination_check": True, "backend": "claude",
        "model": "m", "timeout_s": 10,
    })()
    problems = adherence_cli._enforce_scored_contamination(args, tmp_path)
    assert any("may not skip" in p for p in problems)
