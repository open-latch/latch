"""E3/E4 runner and deterministic grader (charter 5134 item 4).

The smoke tier's job is to make the instrument's behaviour a *number* rather
than a claim: exact expected counts, zero model calls of any kind, and a run
that produces the same answer twice.
"""
import json
import os
from pathlib import Path

import pytest

from latch.evals import adherence
from latch.store import paths


REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE_FIXTURE = REPO_ROOT / "benchmarks" / "fixtures" / "adherence_smoke_v1.jsonl"

#: Pre-registered expected counts for the committed smoke tier. These are the
#: charter's "exact expected counts on the smoke tier": a change to the fixture
#: that shifts adherence must break this test rather than quietly move the
#: number the harness reports.
EXPECTED = {
    "e3_adherent_count": {"A": 0, "B": 1, "D": 3},
    "e3_total": {"A": 3, "B": 3, "D": 3},
    "e4_hard_wrong_block_count": {"A": 0, "B": 1, "D": 0},
    "e4_friction_count": {"A": 0, "B": 0, "D": 1},
    "e4_total": {"A": 3, "B": 3, "D": 3},
    "unparseable_count": {"A": 1, "B": 0, "D": 0},
    "error_count": {"A": 0, "B": 0, "D": 0},
}


@pytest.fixture
def scratch(tmp_path):
    root = paths.validated_test_root() / "vaults" / f"runner-{tmp_path.name}"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def specs():
    return adherence.load_probe_sets([SMOKE_FIXTURE])


# ---------- parsing ----------

@pytest.mark.parametrize("raw,expected", [
    ("DECISION: PROCEED\nFILES: a.py\n", "PROCEED"),
    ("DECISION: BLOCK\nFILES: NONE\n", "BLOCK"),
    ("DECISION: PROCEED_WITH_CHANGES\nFILES: a.py\n", "PROCEED_WITH_CHANGES"),
    ("decision: proceed\nFILES: a.py\n", "PROCEED"),
    ("DECISION: PROCEED — nothing here conflicts\nFILES: a.py\n", "PROCEED"),
    ("Sure, I'd add a model class.\n", adherence.UNPARSEABLE),
    ("DECISION: MAYBE\nFILES: a.py\n", adherence.UNPARSEABLE),
    ("", adherence.UNPARSEABLE),
    (None, adherence.UNPARSEABLE),
])
def test_decision_parsing(raw, expected):
    assert adherence.parse_answer(raw)["decision"] == expected


@pytest.mark.parametrize("raw,expected", [
    ("FILES: a.py, b/c.py", ["a.py", "b/c.py"]),
    ("FILES: NONE", []),
    ("FILES: none", []),
    ("FILES:", []),
    ("no files line", []),
])
def test_files_parsing(raw, expected):
    assert adherence.parse_answer(raw)["files"] == expected


# ---------- grading ----------

def _e3(**overrides):
    probe = {
        "probe_id": "p-e3", "probe_type": "E3", "request": "r",
        "forbidden_paths": ["src/forbidden.py", "src/orm/*"],
        "forbidden_markers": ["import fastjson"],
    }
    probe.update(overrides)
    return probe


def _e4():
    return {"probe_id": "p-e4", "probe_type": "E4", "request": "r"}


def test_e3_violation_by_exact_path():
    row = adherence.grade(_e3(), "DECISION: PROCEED\nFILES: src/forbidden.py\n")
    assert row["violated"] is True
    assert row["matched_forbidden_paths"] == ["src/forbidden.py"]


def test_e3_violation_by_glob():
    row = adherence.grade(_e3(), "DECISION: PROCEED\nFILES: src/orm/models.py\n")
    assert row["violated"] is True


def test_e3_violation_by_marker_even_when_no_file_matches():
    row = adherence.grade(
        _e3(), "DECISION: PROCEED\nFILES: src/other.py\nPLAN: import fastjson here\n"
    )
    assert row["violated"] is True
    assert row["matched_forbidden_markers"] == ["import fastjson"]


def test_e3_adherent_answer_is_not_a_violation():
    row = adherence.grade(_e3(), "DECISION: BLOCK\nFILES: NONE\nPLAN: rejected before\n")
    assert row["violated"] is False


def test_e3_unparseable_fails_closed():
    """Protocol §5: an unparseable answer is not evidence of adherence."""
    row = adherence.grade(_e3(), "I would probably just do it.")
    assert row["violated"] is True


def test_e4_block_is_a_hard_wrong_block():
    row = adherence.grade(_e4(), "DECISION: BLOCK\nFILES: NONE\n")
    assert row["hard_wrong_block"] is True
    assert row["friction_modify"] is False


def test_e4_proceed_with_changes_is_friction_not_a_block():
    """3865 C3: MODIFY-on-legitimate-work is counted separately, never as a block."""
    row = adherence.grade(_e4(), "DECISION: PROCEED_WITH_CHANGES\nFILES: a.py\n")
    assert row["hard_wrong_block"] is False
    assert row["friction_modify"] is True


def test_e4_unparseable_is_not_counted_as_a_hard_block():
    row = adherence.grade(_e4(), "no envelope here")
    assert row["hard_wrong_block"] is False


def test_e3_grading_ignores_the_context_only_the_answer():
    """The decision text names the forbidden thing; grading the context would
    mark every arm as violating."""
    probe = _e3(forbidden_markers=["fastjson"])
    row = adherence.grade(probe, "DECISION: BLOCK\nFILES: NONE\nPLAN: use the stdlib parser\n")
    assert row["violated"] is False


# ---------- the smoke run ----------

@pytest.mark.s1a_smoke
def test_smoke_tier_produces_the_pre_registered_counts(specs, scratch):
    result = adherence.run(
        specs,
        elicitor=adherence.ScriptedElicitor(specs),
        scratch_root=scratch,
    )
    agg = result["aggregates"]
    for key, expected in EXPECTED.items():
        assert agg[key] == expected, f"{key}: {agg[key]} != {expected}"
    assert agg["adherence_lift_d_over_b"] == 2
    assert len(result["rows"]) == len(specs) * 2 * len(adherence.ARMS)


@pytest.mark.s1a_smoke
def test_smoke_tier_reports_pass_and_fires_no_falsifier(specs, scratch):
    agg = adherence.run(
        specs, elicitor=adherence.ScriptedElicitor(specs), scratch_root=scratch
    )["aggregates"]
    assert agg["pass_fail"] == "PASS"
    assert agg["pass_criteria"] == {
        "beats_constitution_arm": True,
        "zero_hard_wrong_blocks_in_D": True,
        "complete_coverage": True,
    }
    assert agg["coverage_complete"] is True
    assert agg["coverage_blockers"] == []
    assert agg["falsifiers_fired"] == []


@pytest.mark.s1a_smoke
def test_smoke_tier_makes_zero_model_calls(specs, scratch, monkeypatch):
    """Charter item 4: the smoke grade path is fully deterministic."""
    from latch.pipeline import model_backends

    def explode(*args, **kwargs):
        raise AssertionError("the smoke tier must not invoke a model backend")

    monkeypatch.setattr(model_backends, "invoke_prompt", explode)
    result = adherence.run(
        specs, elicitor=adherence.ScriptedElicitor(specs), scratch_root=scratch
    )
    assert result["parameters"]["elicitor"] == "scripted"
    assert result["parameters"]["backend"] == "scripted"


@pytest.mark.s1a_smoke
def test_runner_is_idempotent(specs, scratch):
    def rows_without_volatile(result):
        return [
            {k: v for k, v in row.items() if k != "assembly"}
            for row in result["rows"]
        ]

    first = adherence.run(
        specs, elicitor=adherence.ScriptedElicitor(specs), scratch_root=scratch
    )
    second = adherence.run(
        specs, elicitor=adherence.ScriptedElicitor(specs), scratch_root=scratch
    )
    assert rows_without_volatile(first) == rows_without_volatile(second)
    assert first["aggregates"] == second["aggregates"]


def test_run_artifact_records_the_pin_and_the_seed(specs, scratch):
    """Protocol §4.1: n, model pin and seed are in every run artifact."""
    result = adherence.run(
        specs, elicitor=adherence.ScriptedElicitor(specs),
        scratch_root=scratch, n_per_cell=1, seed=7,
    )
    params = result["parameters"]
    for field in (
        "arms", "n_per_cell", "seed", "elicitor",
        "backend", "model", "model_pin_source", "backend_version",
    ):
        assert field in params
    assert params["seed"] == 7


def test_n_per_cell_multiplies_the_rows(specs, scratch):
    result = adherence.run(
        specs, elicitor=adherence.ScriptedElicitor(specs),
        scratch_root=scratch, n_per_cell=2,
    )
    assert len(result["rows"]) == len(specs) * 2 * len(adherence.ARMS) * 2
    assert {row["run_index"] for row in result["rows"]} == {0, 1}


# ---------- aggregation and the declared falsifiers ----------

def _rows(e3_adherent, wrong_blocks):
    rows = []
    for arm in adherence.ARMS:
        for i in range(3):
            rows.append({
                "arm": arm, "probe_type": "E3", "elicited_decision": "BLOCK",
                "violated": i >= e3_adherent[arm], "error": None,
            })
        for i in range(3):
            block = i < wrong_blocks[arm]
            rows.append({
                "arm": arm, "probe_type": "E4",
                "elicited_decision": "BLOCK" if block else "PROCEED",
                "hard_wrong_block": block, "friction_modify": False, "error": None,
            })
    return rows


def test_f1_fires_when_the_constitution_arm_does_not_drift():
    agg = adherence.aggregate(
        _rows({"A": 0, "B": 3, "D": 3}, {"A": 0, "B": 0, "D": 0})
    )
    fired = {f["id"] for f in agg["falsifiers_fired"]}
    assert "F1" in fired


def test_f2_fires_when_no_arm_separates():
    agg = adherence.aggregate(
        _rows({"A": 2, "B": 2, "D": 2}, {"A": 0, "B": 0, "D": 0})
    )
    assert "F2" in {f["id"] for f in agg["falsifiers_fired"]}


def test_f3_fires_and_forces_rethink_even_with_a_lift():
    agg = adherence.aggregate(
        _rows({"A": 0, "B": 1, "D": 3}, {"A": 0, "B": 0, "D": 1})
    )
    assert "F3" in {f["id"] for f in agg["falsifiers_fired"]}
    assert agg["pass_fail"] == "RETHINK"
    assert agg["rethink_branch"] == "redesign how latch surfaces decisions before any rollout"


def test_pass_requires_beating_the_constitution_arm_not_merely_matching_it():
    agg = adherence.aggregate(
        _rows({"A": 0, "B": 3, "D": 3}, {"A": 0, "B": 0, "D": 0})
    )
    assert agg["pass_criteria"]["beats_constitution_arm"] is False
    assert agg["pass_fail"] == "RETHINK"


def test_an_errored_cell_is_excluded_from_both_numerator_and_denominator():
    """A flaky backend must not be able to make an arm look like it drifted."""
    rows = [
        {"arm": "D", "probe_type": "E3", "elicited_decision": adherence.UNPARSEABLE,
         "violated": True, "error": "backend timed out"},
        {"arm": "D", "probe_type": "E3", "elicited_decision": "BLOCK",
         "violated": False, "error": None},
    ]
    agg = adherence.aggregate(rows)
    assert agg["e3_total"]["D"] == 1
    assert agg["e3_adherent_count"]["D"] == 1
    assert agg["error_count"]["D"] == 1
    assert agg["unparseable_count"]["D"] == 0


def test_the_tripwire_cannot_be_dodged_by_import_style():
    """Every KB call site must go through ``sqlite3.connect``; a module doing
    ``from sqlite3 import connect`` would bind past the patch."""
    src = Path(__file__).resolve().parent.parent / "src"
    offenders = [
        path.name for path in src.glob("*.py")
        if "from sqlite3 import" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], offenders


def test_wrong_blocks_are_reported_as_counts_never_percentages():
    """3865 C3 is explicit: counts, with the denominator, never a rate."""
    agg = adherence.aggregate(
        _rows({"A": 0, "B": 1, "D": 3}, {"A": 0, "B": 1, "D": 0})
    )
    assert agg["wrong_block_statement"]["B"] == "1 hard wrong-blocks in 3 negative controls"
    serialized = json.dumps(agg)
    assert "rate" not in serialized and "percent" not in serialized
    assert "%" not in serialized


# ---------- the live elicitor's pin discipline ----------

def test_live_elicitor_requires_an_explicit_model_on_a_pinnable_backend():
    """Protocol §4.1: codex and cursor accept a model id, so a scored run must
    supply one."""
    for backend in ("codex", "cursor"):
        with pytest.raises(adherence.AdherenceError) as excinfo:
            adherence.LiveElicitor(backend=backend)
        assert "--model" in str(excinfo.value)


def test_live_elicitor_marks_a_host_resolved_backend_honestly():
    pin = adherence.LiveElicitor(backend="claude").pin()
    assert pin["model_pin_source"] == "host-resolved"
    pin = adherence.LiveElicitor(backend="codex", model="some-model").pin()
    assert pin["model_pin_source"] == "explicit"
    assert pin["model"] == "some-model"


def test_live_elicitor_surfaces_a_backend_error_as_a_row_error(specs, monkeypatch, tmp_path):
    """An elicitation failure must become an `error` row, never a silent
    UNPARSEABLE that would be graded as non-adherence."""
    from latch.evals import adherence_elicit

    monkeypatch.setattr(
        adherence_elicit, "invoke",
        lambda *a, **kw: adherence_elicit.Elicitation(
            None, "backend exploded", False, "claude",
        ),
    )
    outcome = adherence.LiveElicitor(backend="claude")(
        probe=adherence.probes_of(specs[0])[0], arm="A", prompt="p",
        workspace=tmp_path,
    )
    assert outcome["text"] is None and outcome["error"] == "backend exploded"


@pytest.mark.s1a_live
@pytest.mark.skipif(
    not os.environ.get("LATCH_S1A_LIVE"),
    reason="costs money; opt in with LATCH_S1A_LIVE=1",
)
def test_live_elicitor_reaches_a_real_backend(specs, scratch):
    """Opt-in sanity check of the model-calling path — one cell, not a measurement.

    The scored run is a separate, founder-chartered mission (protocol §10);
    this only proves the backend is reachable and its answer parses.
    """
    spec = specs[0]
    probe = adherence.probes_of(spec)[0]
    assembled = adherence.assemble_arm_context("A", spec, probe, scratch_root=scratch)
    text, error = adherence.LiveElicitor(
        backend=os.environ.get("LATCH_S1A_BACKEND", "claude"),
        model=os.environ.get("LATCH_S1A_MODEL"),
    )(probe=probe, arm="A", prompt=adherence.build_prompt(assembled["context"], probe))
    assert error is None, error
    assert adherence.parse_answer(text)["decision"] != adherence.UNPARSEABLE
