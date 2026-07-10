"""Unit tests for Cursor project-rule sync."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import cursor_rules_sync as crs  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="latch-cursor-rules-"))


def test_render_rule_has_cursor_frontmatter_and_gate_contract():
    out = crs.render_rule(kb_home="/opt/latch")
    _assert(out.startswith("---\n"), "Cursor .mdc frontmatter should stay first")
    _assert("alwaysApply: true" in out, "rule should be always applied")
    _assert("latch_gate" in out, "rule should name gate tool")
    _assert("findings.must_display_to_user=true" in out,
            "rule should require foreground gate findings")
    _assert("/opt/latch/bin/install_cursor.sh --yes" in out,
            "KB_HOME placeholder should resolve")
    _assert(".cursor/commands" in out and "native Cursor model backend" in out,
            "rule should state Cursor adapter boundaries")
    print("PASS render_rule_has_cursor_frontmatter_and_gate_contract")


def test_sync_creates_and_evaluates_rule():
    d = _tmp()
    try:
        target = d / ".cursor" / "rules" / "latch.mdc"
        action = crs.sync(target, kb_home="/opt/latch")
        _assert(action == "created", action)
        _assert(crs.evaluate(target, kb_home="/opt/latch") == crs.OK,
                "created rule should evaluate OK")
        _assert(target.read_text(encoding="utf-8").startswith("---\n"),
                "created rule should preserve frontmatter")
        print("PASS sync_creates_and_evaluates_rule")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_sync_backs_up_drifted_rule():
    d = _tmp()
    try:
        target = d / ".cursor" / "rules" / "latch.mdc"
        target.parent.mkdir(parents=True)
        target.write_text("custom cursor rule\n", encoding="utf-8")
        action = crs.sync(target, kb_home="/opt/latch")
        _assert(action == "synced", action)
        backup = target.with_name(target.name + ".latchbak")
        _assert(backup.exists(), "drifted rule should be backed up")
        _assert(backup.read_text(encoding="utf-8") == "custom cursor rule\n",
                "backup should hold previous content")
        _assert(crs.evaluate(target, kb_home="/opt/latch") == crs.OK,
                "synced rule should evaluate OK")
        print("PASS sync_backs_up_drifted_rule")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_remove_only_removes_clean_rule():
    d = _tmp()
    try:
        target = d / ".cursor" / "rules" / "latch.mdc"
        target.parent.mkdir(parents=True)
        target.write_text("custom cursor rule\n", encoding="utf-8")
        _assert(crs.remove(target, kb_home="/opt/latch") == crs.DRIFT,
                "drifted rule should not be removed")

        crs.sync(target, kb_home="/opt/latch")
        _assert(crs.remove(target, kb_home="/opt/latch") == "removed",
                "clean rule should be removed")
        _assert(not target.exists(), "rule file should be gone")
        _assert(target.with_name(target.name + ".latchbak").exists(),
                "removed clean rule should be backed up")
        print("PASS remove_only_removes_clean_rule")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_render_rule_has_cursor_frontmatter_and_gate_contract()
    test_sync_creates_and_evaluates_rule()
    test_sync_backs_up_drifted_rule()
    test_remove_only_removes_clean_rule()
    print("\nAll cursor_rules_sync tests pass.")
