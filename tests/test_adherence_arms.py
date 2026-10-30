"""Arm assembly and the scrub (charter 5134 item 3).

The scrub is a named methodology step, not hygiene: latch mirrors its
instructions into more than one host file (id=1036), so a baseline arm that
still carries `MEMORY.md`, a `.latchbak`, or a synced AGENTS.md managed block
is not a baseline at all, and a reviewer will find it (id=3789 / id=1263).
"""
import json
from pathlib import Path

import pytest

from _win_dirlink import link_dir
from latch.evals import adherence
from latch.evals import adherence_vault
from latch.store import paths


REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE_FIXTURE = REPO_ROOT / "benchmarks" / "fixtures" / "adherence_smoke_v1.jsonl"


@pytest.fixture
def scratch(tmp_path):
    """A scratch root pytest's own pin rules will accept for ephemeral vaults."""
    root = paths.validated_test_root() / "vaults" / f"arms-{tmp_path.name}"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def spec():
    return adherence.load_probe_sets([SMOKE_FIXTURE])[0]


# ---------- the scrub ----------

def test_scrub_removes_every_named_latch_artifact(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / ".claude").mkdir(parents=True)
    (workspace / ".cursor").mkdir()
    (workspace / "MEMORY.md").write_text("memory", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("agents", encoding="utf-8")
    (workspace / "CLAUDE.md").write_text("claude", encoding="utf-8")
    (workspace / "AGENTS.md.latchbak").write_text("backup", encoding="utf-8")
    (workspace / "kb_location.json").write_text("{}", encoding="utf-8")
    (workspace / "README.md").write_text("keep me", encoding="utf-8")

    removed = adherence.scrub_workspace(workspace)

    assert "MEMORY.md" in removed
    assert "AGENTS.md" in removed
    assert "CLAUDE.md" in removed
    assert "AGENTS.md.latchbak" in removed
    assert "kb_location.json" in removed
    assert ".claude" in removed and ".cursor" in removed
    assert (workspace / "README.md").exists(), "the scrub must not touch project files"
    assert adherence.latch_residue(workspace) == []


def test_residue_detects_a_managed_block_inside_a_file_the_scrub_kept(tmp_path):
    """The scrub removes files; a managed block can hide in one it keeps."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "CONTRIBUTING.md").write_text(
        "intro\n"
        "<!-- BEGIN LATCH AGENTS SNIPPET : managed region, do not hand-edit -->\n"
        "call latch_gate first\n"
        "<!-- END LATCH AGENTS SNIPPET -->\n",
        encoding="utf-8",
    )
    residue = adherence.latch_residue(workspace)
    assert residue and "CONTRIBUTING.md" in residue[0]


def test_scrub_refuses_a_symlinked_artifact_pointing_outside_the_workspace(tmp_path):
    """A symlinked `.claude` would otherwise delete a real host config."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    real_config = tmp_path / "real-host-config"
    real_config.mkdir()
    (real_config / "settings.json").write_text("{}", encoding="utf-8")
    link_dir(workspace / ".claude", real_config)

    with pytest.raises(adherence.AdherenceError) as excinfo:
        adherence.scrub_workspace(workspace)
    assert "outside the workspace" in str(excinfo.value)
    assert (real_config / "settings.json").exists()


def test_scrub_is_idempotent(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("memory", encoding="utf-8")
    assert adherence.scrub_workspace(workspace) == ["MEMORY.md"]
    assert adherence.scrub_workspace(workspace) == []


# ---------- arm isolation ----------

def test_arm_a_context_is_empty_and_carries_no_latch_bytes(spec, scratch):
    probe = adherence.probes_of(spec)[0]
    assembled = adherence.assemble_arm_context("A", spec, probe, scratch_root=scratch)
    assert assembled["context"] == ""
    assert assembled["workspace_residue"] == []
    assert adherence.contains_latch_bytes(assembled["context"]) == []


def test_arm_b_contains_the_decision_text_and_nothing_latch_derived(spec, scratch):
    probe = adherence.probes_of(spec)[0]
    assembled = adherence.assemble_arm_context("B", spec, probe, scratch_root=scratch)
    context = assembled["context"]

    assert spec["decision"]["text"] in context, "arm B is the same-text comparator"
    assert assembled["workspace_residue"] == []
    assert adherence.contains_latch_bytes(context) == []
    # No latch framing rides along with the decision: no node ids, no citation
    # syntax, no gate vocabulary.
    assert "id=" not in context
    assert "seed [" not in context


def test_arm_b_workspace_is_scrubbed_before_the_constitution_is_written(spec, scratch):
    """Order matters: writing CLAUDE.md first would have it scrubbed away."""
    probe = adherence.probes_of(spec)[0]
    assembled = adherence.assemble_arm_context("B", spec, probe, scratch_root=scratch)
    assert assembled["context"].strip() == spec["decision"]["text"].strip()


def test_arm_d_surfaces_the_decision_through_the_production_renderer(spec, scratch):
    probe = adherence.probes_of(spec)[0]
    assembled = adherence.assemble_arm_context("D", spec, probe, scratch_root=scratch)
    context = assembled["context"]

    assert context.strip(), "arm D must not be empty — a vault without embeddings retrieves nothing (id=2651)"
    assert spec["decision"]["title"] in context
    # These are the production renderer's own markers, so arm D really is the
    # bytes latch puts in front of a classifier rather than a paraphrase.
    assert "seed [" in context and "status=" in context
    assert assembled["assembly"]["seed_ids"]


def test_arm_d_and_arm_b_differ(spec, scratch):
    """If the two arms produced the same string, V7 would measure nothing."""
    probe = adherence.probes_of(spec)[0]
    arm_b = adherence.assemble_arm_context("B", spec, probe, scratch_root=scratch)
    arm_d = adherence.assemble_arm_context("D", spec, probe, scratch_root=scratch)
    assert arm_b["context"] != arm_d["context"]


def test_arm_assembly_is_deterministic(spec, scratch):
    probe = adherence.probes_of(spec)[0]
    first = adherence.assemble_arm_context("D", spec, probe, scratch_root=scratch)
    second = adherence.assemble_arm_context("D", spec, probe, scratch_root=scratch)
    assert first["context"] == second["context"]
    assert first["assembly"] == second["assembly"]


def test_unknown_arm_is_refused(spec, scratch):
    probe = adherence.probes_of(spec)[0]
    with pytest.raises(adherence.AdherenceError):
        adherence.assemble_arm_context("C", spec, probe, scratch_root=scratch)


def test_arm_d_tears_down_its_vault(spec, scratch):
    probe = adherence.probes_of(spec)[0]
    adherence.assemble_arm_context("D", spec, probe, scratch_root=scratch)
    leftover = {p.name for p in scratch.iterdir() if p.name.startswith("arm-d-")}
    assert leftover == set(), "charter item 5(b): ephemeral vaults do not survive"
    # The arm's workspace is a different object with a different prefix; it is
    # reclaimed by the scratch-session teardown, not per-arm.
    assert any(p.name.startswith("ws-arm-d-") for p in scratch.iterdir())


# ---------- the prompt ----------

def test_prompt_omits_the_context_block_entirely_for_arm_a(spec):
    probe = adherence.probes_of(spec)[0]
    prompt = adherence.build_prompt("", probe)
    assert "Project context:" not in prompt
    assert probe["request"] in prompt
    assert "DECISION:" in prompt


def test_prompt_carries_the_context_block_when_there_is_one(spec):
    probe = adherence.probes_of(spec)[0]
    prompt = adherence.build_prompt("some context", probe)
    assert "Project context:\nsome context" in prompt


def test_every_arm_gets_the_identical_response_envelope(spec, scratch):
    """A different envelope per arm would confound the parse rate with the arm."""
    probe = adherence.probes_of(spec)[0]
    envelopes = set()
    for arm in adherence.ARMS:
        assembled = adherence.assemble_arm_context(arm, spec, probe, scratch_root=scratch)
        prompt = adherence.build_prompt(assembled["context"], probe)
        envelopes.add(prompt[prompt.index("Answer in exactly this format"):])
    assert len(envelopes) == 1
