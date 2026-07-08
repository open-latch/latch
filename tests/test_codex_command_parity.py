from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _frontmatter_field(path: Path, field: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(field)}:\s*(.+)$", text, re.MULTILINE)
    assert match, f"{path} is missing {field!r} frontmatter"
    return match.group(1).strip().strip('"')


def test_codex_source_commands_match_claude_latch_commands():
    prefix = "source-command-"
    claude_commands = {
        path.stem
        for path in (ROOT / "commands").glob("latch-*.md")
    }
    unlatch_command = ROOT / "commands" / "unlatch.md"
    if unlatch_command.exists():
        claude_commands.add("unlatch")
    codex_skills = {
        path.parent.name[len(prefix):]
        for path in (ROOT / ".agents" / "skills").glob("source-command-latch-*/SKILL.md")
        if path.parent.name.startswith(prefix)
    }
    unlatch_skill = ROOT / ".agents" / "skills" / "source-command-unlatch" / "SKILL.md"
    if unlatch_skill.exists():
        codex_skills.add("unlatch")

    assert codex_skills == claude_commands

    for command in sorted(claude_commands):
        skill_dir = ROOT / ".agents" / "skills" / f"source-command-{command}"
        skill = skill_dir / "SKILL.md"
        metadata = skill_dir / "agents" / "openai.yaml"

        assert skill.exists(), f"missing Codex skill for /{command}"
        assert metadata.exists(), f"missing Codex app metadata for /{command}"
        assert _frontmatter_field(skill, "name") == f"source-command-{command}"

        description = _frontmatter_field(skill, "description")
        assert command in description
        assert f"/{command}" in description


def test_repo_does_not_ship_legacy_kb_source_commands():
    legacy = sorted(
        path.parent.name
        for path in (ROOT / ".agents" / "skills").glob("source-command-kb-*/SKILL.md")
    )
    assert legacy == []


def test_codex_source_commands_do_not_use_claude_argument_placeholder():
    offenders = [
        path
        for path in list((ROOT / ".agents" / "skills").glob("source-command-latch-*/SKILL.md"))
        + [ROOT / ".agents" / "skills" / "source-command-unlatch" / "SKILL.md"]
        if path.exists()
        if "$ARGUMENTS" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_latch_decay_codex_skill_invokes_weekly_maintenance():
    text = (
        ROOT
        / ".agents"
        / "skills"
        / "source-command-latch-decay"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert 'python "$latch_home/src/maintenance.py" weekly "$(pwd)"' in text


def test_latch_gate_codex_skill_uses_explicit_request_fallback():
    text = (
        ROOT
        / ".agents"
        / "skills"
        / "source-command-latch-gate"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "latch_gate" in text
    assert "request=\"$(cat <<'EOF'" in text
    assert 'bash "$latch_home/bin/run_latch_gate.sh" "$request"' in text


def test_latch_gate_report_codex_skill_uses_explicit_filter_array():
    text = (
        ROOT
        / ".agents"
        / "skills"
        / "source-command-latch-gate-report"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "filters=()" in text
    assert 'bash "$latch_home/bin/latch_gate_report.sh" "${filters[@]}"' in text


def test_shell_backed_codex_skills_do_not_treat_project_root_as_latch_home():
    shell_backed_commands = {
        "latch-budget-approve",
        "latch-compact",
        "latch-decay",
        "latch-gate",
        "latch-gate-report",
        "latch-heal",
        "latch-tree",
        "unlatch",
    }

    for command in shell_backed_commands:
        text = (
            ROOT
            / ".agents"
            / "skills"
            / f"source-command-{command}"
            / "SKILL.md"
        ).read_text(encoding="utf-8")

        assert "CLAUDE_KB_HOME" in text
        assert "AGENTS.md" in text
        assert "src/mcp_server.py" in text
        assert "Could not find latch checkout" in text
        assert 'latch_home="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"' not in text


def test_model_backed_codex_shell_fallbacks_default_to_codex():
    gate = (
        ROOT
        / ".agents"
        / "skills"
        / "source-command-latch-gate"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    heal = (
        ROOT
        / ".agents"
        / "skills"
        / "source-command-latch-heal"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    tree = (
        ROOT
        / ".agents"
        / "skills"
        / "source-command-latch-tree"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "export LATCH_GATE_BACKEND=codex" in gate
    assert "export LATCH_MAINTENANCE_BACKEND=codex" in heal
    assert "export LATCH_MAINTENANCE_BACKEND=codex" in tree
