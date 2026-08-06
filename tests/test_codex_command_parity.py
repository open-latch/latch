from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
REVIEW_SKILL_TEMPLATE = (
    ROOT / "templates" / "codex" / "source-command-latch-review" / "SKILL.md"
)


def _frontmatter_field(path: Path, field: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(field)}:\s*(.+)$", text, re.MULTILINE)
    assert match, f"{path} is missing {field!r} frontmatter"
    return match.group(1).strip().strip('"')


def _review_target_contract(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        r"<!-- latch-review-target-grammar:start -->\n(.*?)\n"
        r"<!-- latch-review-target-grammar:end -->",
        text,
        re.DOTALL,
    )
    assert match, f"{path} is missing the managed review target grammar"
    return match.group(1)


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
    if REVIEW_SKILL_TEMPLATE.exists():
        codex_skills.add("latch-review")

    assert codex_skills == claude_commands

    for command in sorted(claude_commands):
        skill_dir = (
            REVIEW_SKILL_TEMPLATE.parent
            if command == "latch-review"
            else ROOT / ".agents" / "skills" / f"source-command-{command}"
        )
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
        + [
            ROOT / ".agents" / "skills" / "source-command-unlatch" / "SKILL.md",
            REVIEW_SKILL_TEMPLATE,
        ]
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
        "latch-review",
        "latch-tree",
        "unlatch",
    }

    for command in shell_backed_commands:
        skill = (
            REVIEW_SKILL_TEMPLATE
            if command == "latch-review"
            else ROOT
            / ".agents"
            / "skills"
            / f"source-command-{command}"
            / "SKILL.md"
        )
        text = skill.read_text(encoding="utf-8")

        if command == "latch-review":
            assert "latch_runner=<LATCH_REVIEW_POSIX_LITERAL>" in text
            assert "<KB_HOME_POSIX_LITERAL>" not in text
            assert "<LATCH_REVIEW_POWERSHELL_LITERAL>" in text
            assert "${LATCH_HOME" not in text
            assert "${CLAUDE_KB_HOME" not in text
            bash_block = re.search(r"```bash\n(.*?)\n```", text, re.DOTALL)
            powershell_block = re.search(
                r"```powershell\n(.*?)\n```", text, re.DOTALL
            )
            assert bash_block and powershell_block
            assert "LATCH_HOME" not in bash_block.group(1)
            assert "CLAUDE_KB_HOME" not in bash_block.group(1)
            assert "LATCH_HOME" not in powershell_block.group(1)
            assert "CLAUDE_KB_HOME" not in powershell_block.group(1)
            assert "rerun bin/install_codex from the trusted Latch checkout" in text
            assert "AGENTS.md" not in text
            assert "src/mcp_server.py" not in text
            continue
        assert "CLAUDE_KB_HOME" in text
        assert "AGENTS.md" in text
        assert "src/mcp_server.py" in text
        assert "Could not find latch checkout" in text
        assert 'latch_home="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"' not in text


def test_latch_review_host_entrypoints_share_strict_target_grammar():
    command = ROOT / "commands" / "latch-review.md"
    skill = (
        REVIEW_SKILL_TEMPLATE
    )
    command_contract = _review_target_contract(command)
    skill_contract = _review_target_contract(skill)

    assert command_contract == skill_contract
    assert command_contract == "<LATCH_REVIEW_TARGET_CONTRACT>"
    canonical_contract = (
        ROOT / "templates" / "review-target-contract.md"
    ).read_text(encoding="utf-8")
    for required in (
        "zero arguments",
        "auto-detect the current",
        "Empty target text must produce exactly zero",
        "--pr N",
        "--range OID...OID",
        "--range OID..OID",
        "--commit OID",
        "[1-9][0-9]*",
        "[0-9a-f]{40}",
        "--end-of-options",
        "shell operators",
        "redirection",
        "command substitution",
        "any extra flag",
        "ask the user for a valid target",
    ):
        assert required in canonical_contract

    assert "--post-pr" in canonical_contract
    assert "only to `--pr N`" in canonical_contract


def test_latch_review_source_template_is_not_host_discoverable():
    discoverable = (
        ROOT
        / ".agents"
        / "skills"
        / "source-command-latch-review"
        / "SKILL.md"
    )
    assert not discoverable.exists()
    assert REVIEW_SKILL_TEMPLATE.is_file()
    text = REVIEW_SKILL_TEMPLATE.read_text(encoding="utf-8")
    assert _frontmatter_field(REVIEW_SKILL_TEMPLATE, "name") == (
        "source-command-latch-review"
    )
    assert text.count("<LATCH_REVIEW_POSIX_LITERAL>") == 1
    assert text.count("<LATCH_REVIEW_POWERSHELL_LITERAL>") == 1
    assert "outside the filesystem sandbox from the first attempt" in " ".join(
        text.split()
    )


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
