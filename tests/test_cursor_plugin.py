"""Cursor plugin manifest and workflow-skill distribution contract."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import install_cursor as ic  # noqa: E402


OFFICIAL_MANIFEST_KEYS = {
    "name", "displayName", "description", "version", "author", "publisher",
    "homepage", "repository", "license", "logo", "keywords", "category",
    "tags", "commands", "agents", "skills", "rules", "hooks", "mcpServers",
}


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    end = text.find("\n---\n", 4)
    assert end > 0, "SKILL.md frontmatter must close"
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    return fields


def test_cursor_plugin_manifest_matches_official_schema_surface():
    manifest = json.loads(ic.CURSOR_PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["name"] == "latch"
    assert re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", manifest["name"])
    assert set(manifest) <= OFFICIAL_MANIFEST_KEYS
    assert manifest["skills"] == "./cursor_skills/"
    assert "commands" not in manifest, "project commands remain installer-owned; avoid duplicates"
    assert "hooks" not in manifest and "mcpServers" not in manifest, (
        "plugin skills must not duplicate project runtime wiring"
    )


def test_cursor_plugin_skills_are_complete_nonconflicting_and_portable():
    command_stems = {Path(name).stem for name in ic.CURSOR_COMMAND_FILES}
    skill_workflows = {
        name[len("source-command-") :] for name in ic.CURSOR_SKILL_NAMES
    }
    assert skill_workflows == command_stems
    assert not (set(ic.CURSOR_SKILL_NAMES) & command_stems), (
        "skill names must not collide with /latch-* command names"
    )

    for name in ic.CURSOR_SKILL_NAMES:
        path = ic.CURSOR_SKILLS_SRC / name / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        fields = _frontmatter(text)
        assert fields.get("name") == name
        assert fields.get("description")
        assert "Latch Cursor skill boundary:" in text
        assert "${CURSOR_PLUGIN_ROOT}" in text or name == "source-command-latch-pm"
        assert "<KB_HOME>" not in text and "<CURSOR_MODEL_BACKEND>" not in text
        assert "~/.cursor/projects" not in text
        assert "agent-transcripts" not in text


def test_cursor_plugin_status_accepts_checked_in_distribution():
    ok, detail = ic.cursor_plugin_status()
    assert ok, detail
