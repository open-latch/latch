"""Unit tests for AGENTS.md managed-region sync."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import agents_md_sync as ams  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp():
    return Path(tempfile.mkdtemp(prefix="kb-agents-sync-"))


def test_render_contract_targets_agents_md():
    out = ams.render_contract(kb_home="/opt/latch")
    _assert("AGENTS.md" in out, "contract should mention AGENTS.md")
    _assert("CLAUDE.md" not in out, "Codex contract should not mention CLAUDE.md")
    _assert("install_agents_md" in out, "contract should mention AGENTS installer")
    _assert("/opt/latch/README.md" in out, "KB_HOME placeholder should be resolved")
    _assert('ToolSearch(query="mcp__latch latch_search latch_get latch_recent latch_gate")' in out,
            "contract should prefer latch-named MCP tools")
    _assert("select:mcp__latch__kb_search" not in out,
            "contract should not use brittle exact-select legacy discovery")
    _assert("/latch-compact" in out and "/kb-compact" not in out,
            "contract should prefer latch compact command")
    print("PASS render_contract_targets_agents_md")


def test_append_preserves_outside_content():
    d = _tmp()
    try:
        t = d / "AGENTS.md"
        t.write_text("# Project\n\nKeep this.\n", encoding="utf-8")
        action = ams.sync(t, kb_home="/opt/latch")
        _assert(action == "appended", action)
        body = t.read_text(encoding="utf-8")
        _assert("# Project" in body and "Keep this." in body,
                "outside content must survive")
        _assert(ams.BEGIN_MARK in body and ams.END_MARK in body,
                "AGENTS markers should be present")
        _assert((d / "AGENTS.md.latchbak").exists(), "backup should be written")
        _assert(ams.evaluate(t, kb_home="/opt/latch") == ams.OK,
                "post-sync should evaluate OK")
        print("PASS append_preserves_outside_content")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_create_false_never_auto_wires():
    d = _tmp()
    try:
        t = d / "AGENTS.md"
        _assert(ams.sync(t, create=False) == "skipped",
                "absent create=False should skip")
        t.write_text("# Project only\n", encoding="utf-8")
        _assert(ams.sync(t, create=False) == "skipped",
                "missing create=False should skip")
        _assert(ams.extract_region(t.read_text(encoding="utf-8")) is None,
                "missing file must not be wired")
        print("PASS create_false_never_auto_wires")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_sync_migrates_legacy_agents_marker():
    d = _tmp()
    try:
        t = d / "AGENTS.md"
        legacy = (
            "# Project\n\n"
            f"{ams.LEGACY_BEGIN_MARK}\n"
            + ams.render_contract(kb_home="/opt/latch")
            + f"\n{ams.LEGACY_END_MARK}\n\n"
            "Keep this.\n"
        )
        t.write_text(legacy, encoding="utf-8")
        _assert(ams.evaluate(t, kb_home="/opt/latch") == ams.DRIFT,
                "legacy AGENTS marker should be treated as drift")
        action = ams.sync(t, kb_home="/opt/latch")
        _assert(action == "synced", action)
        body = t.read_text(encoding="utf-8")
        _assert(ams.BEGIN_MARK in body and ams.END_MARK in body,
                "new AGENTS markers should be present")
        _assert(ams.LEGACY_BEGIN_MARK not in body,
                "legacy marker should be removed")
        _assert("# Project" in body and "Keep this." in body,
                "outside content must survive")
        _assert((d / "AGENTS.md.latchbak").exists(), "backup should be written")
        print("PASS sync_migrates_legacy_agents_marker")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_main_noninteractive_without_yes_aborts():
    d = _tmp()
    orig_tty = ams._stdin_is_tty
    ams._stdin_is_tty = lambda: False
    try:
        t = d / "AGENTS.md"
        rc = ams.main([str(t)])
        _assert(rc == 1, f"expected rc 1, got {rc}")
        _assert(not t.exists(), "noninteractive first wire should not write")
        print("PASS main_noninteractive_without_yes_aborts")
    finally:
        ams._stdin_is_tty = orig_tty
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_render_contract_targets_agents_md()
    test_append_preserves_outside_content()
    test_create_false_never_auto_wires()
    test_sync_migrates_legacy_agents_marker()
    test_main_noninteractive_without_yes_aborts()
    print("\nAll agents_md_sync tests pass.")
