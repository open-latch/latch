"""Regressions for review round 1 (KB id=5217), one per finding.

Round 1 failed the harness on six P1 defects, all in the live/scored path that
the smoke tier never exercises. Each is pinned here by number so a
reintroduction fails by name rather than being rediscovered by the next
reviewer.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from latch.evals import adherence
from latch.evals import adherence_cli
from latch.evals import adherence_elicit
from latch.store import paths


REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE_FIXTURE = REPO_ROOT / "benchmarks" / "fixtures" / "adherence_smoke_v1.jsonl"


@pytest.fixture
def scratch(tmp_path):
    root = paths.validated_test_root() / "vaults" / f"r1-{tmp_path.name}"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def specs():
    return adherence.load_probe_sets([SMOKE_FIXTURE])


# ---------- finding 1: the recorded pin was never applied ----------

@pytest.mark.parametrize("backend", ["claude", "codex", "cursor"])
def test_finding1_the_model_reaches_the_command_line(backend, tmp_path):
    """A pin the process never receives is not a pin.

    Round 1 recorded model_pin_source=explicit while
    model_backends.invoke_prompt — which takes env-var *names*, not a value —
    was called without any model at all.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "DECISION: PROCEED\nFILES: NONE\n", "")

    original = subprocess.run
    subprocess.run = fake_run  # type: ignore[assignment]
    try:
        adherence_elicit.invoke(
            "prompt", backend=backend, workspace=workspace,
            model="pinned-model-x", binary="/bin/true",
        )
    finally:
        subprocess.run = original  # type: ignore[assignment]

    argv = captured["argv"]
    assert "--model" in argv, argv
    assert argv[argv.index("--model") + 1] == "pinned-model-x"


def test_finding1_the_argv_is_recorded_in_the_result(tmp_path):
    """The pin has to be auditable from the artifact, not taken on trust."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    original = subprocess.run
    subprocess.run = lambda argv, **kw: subprocess.CompletedProcess(  # type: ignore[assignment]
        argv, 0, "DECISION: PROCEED\nFILES: NONE\n", ""
    )
    try:
        result = adherence_elicit.invoke(
            "p", backend="claude", workspace=workspace,
            model="pinned-model-x", binary="/bin/true",
        )
    finally:
        subprocess.run = original  # type: ignore[assignment]
    assert "--model" in result.argv
    assert "pinned-model-x" in result.argv


def test_finding1_live_elicitor_forwards_its_model(tmp_path, monkeypatch):
    seen = {}

    def fake_invoke(prompt, **kwargs):
        seen.update(kwargs)
        return adherence_elicit.Elicitation("DECISION: PROCEED\nFILES: NONE\n", None, False, "codex")

    monkeypatch.setattr(adherence_elicit, "invoke", fake_invoke)
    adherence.LiveElicitor(backend="codex", model="m-1")(
        probe={"probe_id": "p", "probe_type": "E3"}, arm="D", prompt="x",
        workspace=tmp_path,
    )
    assert seen["model"] == "m-1"
    assert seen["workspace"] == tmp_path


def test_finding1_backend_version_is_captured():
    """Protocol §4.1 names backend_version; round 1 always reported null."""
    # This interpreter answers `--version` on every OS; /bin/echo does not
    # exist on Windows.
    version = adherence_elicit.backend_version(sys.executable)
    assert version is not None


# ---------- finding 2: the CLI envelope was graded verbatim ----------

#: The shape `claude -p --output-format json` actually returns.
CLAUDE_ENVELOPE = json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 4210,
    "num_turns": 1,
    "result": "DECISION: BLOCK\nFILES: NONE\nPLAN: this was rejected before.\n",
    "session_id": "abc",
})


def test_finding2_a_real_claude_envelope_grades_correctly():
    """Round 1 graded this UNPARSEABLE, which fails closed into 'not adherent'
    and would have silently depressed whichever arm ran on Claude."""
    unwrapped = adherence_elicit.unwrap_envelope(CLAUDE_ENVELOPE)
    assert adherence.parse_answer(unwrapped)["decision"] == "BLOCK"
    # And the un-unwrapped form is exactly the failure round 1 found.
    assert adherence.parse_answer(CLAUDE_ENVELOPE)["decision"] == adherence.UNPARSEABLE


def test_finding2_unwrap_handles_plain_text_and_fences():
    assert adherence_elicit.unwrap_envelope("DECISION: PROCEED\n").startswith("DECISION")
    fenced = "```\nDECISION: PROCEED\nFILES: NONE\n```"
    assert adherence.parse_answer(
        adherence_elicit.unwrap_envelope(fenced)
    )["decision"] == "PROCEED"
    assert adherence_elicit.unwrap_envelope(None) is None


def test_finding2_unwrap_runs_before_grading_in_the_live_path(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    original = subprocess.run
    subprocess.run = lambda argv, **kw: subprocess.CompletedProcess(  # type: ignore[assignment]
        argv, 0, CLAUDE_ENVELOPE, ""
    )
    try:
        outcome = adherence.LiveElicitor(backend="claude")(
            probe={"probe_id": "p", "probe_type": "E3"}, arm="D", prompt="x",
            workspace=workspace,
        )
    finally:
        subprocess.run = original  # type: ignore[assignment]
    assert adherence.parse_answer(outcome["text"])["decision"] == "BLOCK"


# ---------- finding 3: --tier scored was only a label ----------

def _scored_args(**overrides):
    args = {
        "tier": "scored", "elicitor": "live", "n": 5,
        "fixture": ["/tmp/authored.jsonl"], "output": Path("/tmp/outside/scored.json"),
        "model": "m-1", "allow_host_resolved_model": False,
    }
    args.update(overrides)
    return type("Args", (), args)()


def test_finding3_scored_defaults_are_refused():
    """The round-1 defaults — scripted, n=1, the committed smoke fixture, no
    output — could all be run and reported as 'the scored run'."""
    args = _scored_args(elicitor="scripted", n=1, fixture=None, output=None, model=None)
    problems = adherence_cli._enforce_scored_tier(args, [Path(adherence_cli.SMOKE_FIXTURE)])
    joined = " | ".join(problems)
    assert "--elicitor live" in joined
    assert "n >= 5" in joined
    assert "explicit --fixture" in joined
    assert "--output" in joined
    assert "--model" in joined


def test_finding3_the_committed_smoke_fixture_can_never_be_scored():
    """Round 1's guarantee, still held — but enforced on CONTENT now, not on
    the pathname, because round 2 defeated the pathname form with a copy
    (id=5302 finding F3). The round-2 suite pins the copy case; this pins the
    original."""
    specs = adherence.load_probe_sets([Path(adherence_cli.SMOKE_FIXTURE)])
    problems = adherence.validate_scored_corpus(
        specs,
        [Path(adherence_cli.SMOKE_FIXTURE)],
        smoke_fixture=Path(adherence_cli.SMOKE_FIXTURE),
    )
    assert any("byte-identical" in p for p in problems), problems


def test_finding3_scored_output_inside_the_repo_is_refused():
    args = _scored_args(output=Path(paths.KB_ROOT) / "benchmarks" / "scored.json")
    problems = adherence_cli._enforce_scored_tier(args, [Path("/tmp/authored.jsonl")])
    assert any("inside the repo tree" in p for p in problems)


def test_finding3_a_fully_specified_scored_run_is_accepted(tmp_path):
    args = _scored_args(output=tmp_path / "scored.json")
    assert adherence_cli._enforce_scored_tier(args, [tmp_path / "authored.jsonl"]) == []


def test_finding3_host_resolved_model_must_be_opted_into_explicitly(tmp_path):
    args = _scored_args(model=None, output=tmp_path / "scored.json")
    assert any("--allow-host-resolved-model" in p
               for p in adherence_cli._enforce_scored_tier(args, [tmp_path / "a.jsonl"]))
    args = _scored_args(model=None, allow_host_resolved_model=True,
                        output=tmp_path / "scored.json")
    assert adherence_cli._enforce_scored_tier(args, [tmp_path / "a.jsonl"]) == []


def test_finding3_rows_carry_provenance_source(scratch, tmp_path):
    specs = adherence.load_probe_sets([SMOKE_FIXTURE])
    result = adherence.run(
        specs, elicitor=adherence.ScriptedElicitor(specs), scratch_root=scratch,
    )
    assert all(row["provenance_source"] == "declared" for row in result["rows"])


# ---------- finding 4: missing data could look like a win ----------

def _rows_with_missing(arms=adherence.ARMS):
    """The exact shape round 1 reproduced: every arm-B E3 cell errored and
    every arm-D E4 cell errored, so D 'beat' an arm with no observations and
    had 'zero wrong-blocks' with no negative controls."""
    rows = []
    for arm in arms:
        for i in range(3):
            errored = arm == "B"
            rows.append({
                "arm": arm, "probe_type": "E3",
                "elicited_decision": "BLOCK",
                "violated": False, "error": "backend exploded" if errored else None,
            })
        for i in range(3):
            errored = arm == "D"
            rows.append({
                "arm": arm, "probe_type": "E4",
                "elicited_decision": "PROCEED",
                "hard_wrong_block": False, "friction_modify": False,
                "error": "backend exploded" if errored else None,
            })
    return rows


def test_finding4_all_errored_arms_cannot_report_pass():
    agg = adherence.aggregate(
        _rows_with_missing(), expected_cells=18, n_per_cell=1,
    )
    assert agg["e3_total"]["B"] == 0
    assert agg["e4_total"]["D"] == 0
    assert agg["pass_fail"] == "INDETERMINATE"
    assert agg["coverage_complete"] is False
    assert any("no E3 observations" in b for b in agg["coverage_blockers"])
    assert any("no E4 observations" in b for b in agg["coverage_blockers"])


def test_finding4_indeterminate_is_not_rethink():
    """RETHINK fires a named plan branch; a run that failed to collect its
    data is no evidence for taking it."""
    agg = adherence.aggregate(
        _rows_with_missing(), expected_cells=18, n_per_cell=1,
    )
    assert agg["rethink_branch"] is None


def test_finding4_a_single_errored_cell_blocks_a_verdict():
    rows = []
    for arm in adherence.ARMS:
        for _ in range(3):
            rows.append({"arm": arm, "probe_type": "E3", "elicited_decision": "BLOCK",
                         "violated": arm != "D", "error": None})
        for i in range(3):
            rows.append({"arm": arm, "probe_type": "E4", "elicited_decision": "PROCEED",
                         "hard_wrong_block": False, "friction_modify": False,
                         "error": "timeout" if (arm == "A" and i == 0) else None})
    agg = adherence.aggregate(rows, expected_cells=18, n_per_cell=1)
    assert agg["pass_fail"] == "INDETERMINATE"
    assert any("errored cells" in b for b in agg["coverage_blockers"])


def test_finding4_complete_coverage_still_reaches_a_verdict():
    rows = []
    for arm in adherence.ARMS:
        for _ in range(3):
            rows.append({"arm": arm, "probe_type": "E3", "elicited_decision": "BLOCK",
                         "violated": arm != "D", "error": None})
        for _ in range(3):
            rows.append({"arm": arm, "probe_type": "E4", "elicited_decision": "PROCEED",
                         "hard_wrong_block": False, "friction_modify": False,
                         "error": None})
    agg = adherence.aggregate(rows, expected_cells=18, n_per_cell=1)
    assert agg["coverage_complete"] is True
    assert agg["pass_fail"] == "PASS"


# ---------- finding 5: arm D dropped the mined typed rejection ----------

def test_finding5_arm_d_renders_the_typed_rejection(specs, scratch):
    """`rejected[rp=…]` is the most latch-specific line in the surfaced
    context. Round 1 seeded nodes and edges but never the rejected_path rows,
    so the arm V7 exists to measure was understated."""
    spec = specs[0]
    probe = adherence.probes_of(spec)[0]
    assembled = adherence.assemble_arm_context("D", spec, probe, scratch_root=scratch)
    context = assembled["context"]

    assert "rejected[rp=" in context, context[:600]
    assert spec["rejected_option"]["option"] in context
    assert assembled["assembly"]["rendered_rejected_paths"] >= 1


def test_finding5_the_rejection_carries_its_provenance(specs, scratch):
    spec = specs[0]
    probe = adherence.probes_of(spec)[0]
    context = adherence.assemble_arm_context(
        "D", spec, probe, scratch_root=scratch
    )["context"]
    assert "ratifier=" in context
    assert "scope=" in context


def test_finding5_arm_b_does_not_get_the_rejection_line(specs, scratch):
    """The comparator is the decision text verbatim — nothing latch-shaped."""
    spec = specs[0]
    probe = adherence.probes_of(spec)[0]
    context = adherence.assemble_arm_context(
        "B", spec, probe, scratch_root=scratch
    )["context"]
    assert "rejected[rp=" not in context


def test_finding5_an_invalid_rejection_source_is_refused(specs, scratch):
    spec = dict(specs[0])
    spec["rejected_option"] = {"option": "x", "reason": "y", "source": "invented"}
    probe = adherence.probes_of(spec)[0]
    with pytest.raises(adherence.AdherenceError) as excinfo:
        adherence.assemble_arm_context("D", spec, probe, scratch_root=scratch)
    assert "declared" in str(excinfo.value)


# ---------- finding 6: the child was not isolated ----------

def test_finding6_claude_child_has_no_tools_and_no_mcp():
    argv = adherence_elicit._claude_argv("/bin/true", None)
    assert "--strict-mcp-config" in argv, "latch's own MCP tools must be unreachable"
    assert "--disallowed-tools" in argv
    denied = argv[argv.index("--disallowed-tools") + 1:]
    for tool in ("Bash", "Read", "Write", "Edit", "WebFetch", "Task"):
        assert tool in denied, tool


def test_finding6_the_child_cannot_resolve_a_latch_vault(monkeypatch):
    monkeypatch.setenv("LATCH_KB_DIR", "/home/someone/vault")
    monkeypatch.setenv("CLAUDE_KB_DIR", "/home/someone/vault")
    monkeypatch.setenv("LATCH_HOME", "/home/someone/latch")
    env = adherence_elicit.isolated_child_env()
    for name in ("LATCH_KB_DIR", "CLAUDE_KB_DIR", "LATCH_HOME", "LATCH_TEST_ROOT"):
        assert name not in env, name
    # Belt and braces: latch is off in the child even if it starts anyway.
    assert env["LATCH_UNLATCHED"] == "1"


def test_finding6_every_arm_answers_from_its_own_scrubbed_workspace(specs, scratch):
    spec = specs[0]
    probe = adherence.probes_of(spec)[0]
    workspaces = {}
    for arm in adherence.ARMS:
        assembled = adherence.assemble_arm_context(
            arm, spec, probe, scratch_root=scratch
        )
        workspace = assembled["workspace"]
        workspaces[arm] = workspace
        assert workspace.is_dir()
        assert adherence.latch_residue(workspace) == (
            [] if arm != "B" else adherence.latch_residue(workspace)
        )
        # No latch artifact survives in any arm's cwd.
        assert not (workspace / "MEMORY.md").exists()
        assert not (workspace / ".claude").exists()
        assert not (workspace / "AGENTS.md.latchbak").exists()
    assert len(set(workspaces.values())) == len(adherence.ARMS)


def test_finding6_the_subprocess_runs_in_the_arm_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "DECISION: PROCEED\nFILES: NONE\n", "")

    original = subprocess.run
    subprocess.run = fake_run  # type: ignore[assignment]
    try:
        adherence_elicit.invoke(
            "p", backend="claude", workspace=workspace, binary="/bin/true",
        )
    finally:
        subprocess.run = original  # type: ignore[assignment]

    assert captured["cwd"] == str(workspace)
    assert "LATCH_KB_DIR" not in captured["env"]
    assert captured["env"]["LATCH_UNLATCHED"] == "1"


def test_finding6_a_missing_workspace_is_refused(tmp_path):
    with pytest.raises(adherence_elicit.ElicitError):
        adherence_elicit.invoke(
            "p", backend="claude", workspace=tmp_path / "nope", binary="/bin/true",
        )


def test_finding6_the_residual_limitation_is_stated_in_the_artifact():
    """The tripwire patches this process only; a child is outside it. That
    gap is recorded rather than papered over."""
    pin = adherence.LiveElicitor(backend="claude").pin()
    assert "residual_limitation" in pin["isolation"]
    assert "tripwire" in pin["isolation"]["residual_limitation"]
