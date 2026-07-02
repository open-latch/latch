"""Unit tests for src/claude_md_sync.py — the single-source-of-truth sync for a
project's CLAUDE.md latch-contract managed region.

Covers: render/extract, evaluate (ok/drift/missing/absent), and sync in every
mode — create / append (preserving outside content) / replace-on-drift /
idempotent / backup written / create=False gating (never auto-wires).
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import claude_md_sync as cms  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp():
    return Path(tempfile.mkdtemp(prefix="kb_cms_test_"))


def test_render_substitutes_placeholder():
    out = cms.render_contract(kb_home="/some/where")
    _assert("{{KB_HOME}}" not in out, "placeholder must be substituted")
    _assert(out.strip() != "", "rendered contract must be non-empty")
    _assert('ToolSearch(query="mcp__latch latch_search latch_get latch_recent latch_gate")' in out,
            "contract should prefer latch-named MCP tools")
    _assert("select:mcp__latch__kb_search" not in out,
            "contract should not use brittle exact-select legacy discovery")
    _assert("/latch-compact" in out and "/kb-compact" not in out,
            "contract should prefer latch compact command")
    print("PASS render_substitutes_placeholder")


def test_extract_region_none_without_markers():
    _assert(cms.extract_region("# hi\n\nno markers here") is None,
            "no markers -> None")
    print("PASS extract_region_none_without_markers")


def test_evaluate_absent_then_create_then_ok():
    d = _tmp()
    try:
        t = d / "CLAUDE.md"
        _assert(cms.evaluate(t) == cms.ABSENT, "missing file -> ABSENT")
        action = cms.sync(t)
        _assert(action == "created", action)
        _assert(t.is_file(), "file created")
        _assert(cms.BEGIN_MARK in t.read_text(encoding="utf-8"), "markers written")
        _assert(cms.evaluate(t) == cms.OK, "post-create -> OK")
        print("PASS evaluate_absent_then_create_then_ok")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_append_preserves_outside_content():
    d = _tmp()
    try:
        t = d / "CLAUDE.md"
        t.write_text("# My Project\n\nProject-specific rules.\n", encoding="utf-8")
        _assert(cms.evaluate(t) == cms.MISSING, "file w/o markers -> MISSING")
        action = cms.sync(t)
        _assert(action == "appended", action)
        body = t.read_text(encoding="utf-8")
        _assert("# My Project" in body and "Project-specific rules." in body,
                "outside content must be preserved")
        _assert(cms.evaluate(t) == cms.OK, "post-append -> OK")
        _assert((d / "CLAUDE.md.latchbak").is_file(), "backup written")
        print("PASS append_preserves_outside_content")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_idempotent():
    d = _tmp()
    try:
        t = d / "CLAUDE.md"
        cms.sync(t)
        before = t.read_text(encoding="utf-8")
        action = cms.sync(t)
        _assert(action == "unchanged", action)
        _assert(t.read_text(encoding="utf-8") == before, "byte-identical re-sync")
        print("PASS idempotent")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_drift_detected_and_resynced_preserving_outside():
    d = _tmp()
    try:
        t = d / "CLAUDE.md"
        t.write_text("# Header before\n", encoding="utf-8")
        cms.sync(t)
        t.write_text(
            t.read_text(encoding="utf-8").replace("KB usage", "KB TAMPERED")
            + "\n## trailing project section\n",
            encoding="utf-8",
        )
        _assert(cms.evaluate(t) == cms.DRIFT, "tampered region -> DRIFT")
        action = cms.sync(t)
        _assert(action == "synced", action)
        _assert(cms.evaluate(t) == cms.OK, "post-resync -> OK")
        body = t.read_text(encoding="utf-8")
        _assert("KB TAMPERED" not in body, "tamper removed")
        _assert("# Header before" in body, "leading content preserved")
        _assert("## trailing project section" in body, "trailing content preserved")
        print("PASS drift_detected_and_resynced_preserving_outside")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_create_false_never_auto_wires():
    d = _tmp()
    try:
        # ABSENT + create=False -> skipped, no file
        t = d / "CLAUDE.md"
        _assert(cms.sync(t, create=False) == "skipped", "ABSENT create=False -> skipped")
        _assert(not t.is_file(), "must NOT create a file")
        # MISSING + create=False -> skipped, no markers added
        t.write_text("# project only\n", encoding="utf-8")
        _assert(cms.sync(t, create=False) == "skipped", "MISSING create=False -> skipped")
        _assert(cms.extract_region(t.read_text(encoding="utf-8")) is None,
                "must NOT add markers to an un-wired file")
        print("PASS create_false_never_auto_wires")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_create_false_resyncs_wired_drift():
    d = _tmp()
    try:
        t = d / "CLAUDE.md"
        cms.sync(t)  # wire it
        t.write_text(t.read_text(encoding="utf-8").replace("KB usage", "KB X"),
                     encoding="utf-8")
        _assert(cms.evaluate(t) == cms.DRIFT, "should read DRIFT")
        _assert(cms.sync(t, create=False) == "synced",
                "wired+drift create=False -> synced")
        _assert(cms.evaluate(t) == cms.OK, "post-resync OK")
        print("PASS create_false_resyncs_wired_drift")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _stub(tty, prompt):
    """Swap in test doubles for the first-wiring confirm seam; returns a restore
    callable. prompt may be a bool (the answer) or a callable(question)->bool."""
    orig_tty, orig_prompt = cms._stdin_is_tty, cms._prompt_yes_no
    cms._stdin_is_tty = (lambda: tty)
    cms._prompt_yes_no = prompt if callable(prompt) else (lambda _q: prompt)

    def restore():
        cms._stdin_is_tty, cms._prompt_yes_no = orig_tty, orig_prompt
    return restore


def test_main_first_wire_confirm_yes_writes():
    d = _tmp()
    restore = _stub(tty=True, prompt=True)
    try:
        t = d / "CLAUDE.md"
        rc = cms.main([str(t)])
        _assert(rc == 0, f"confirmed wire -> rc 0, got {rc}")
        _assert(t.is_file() and cms.evaluate(t) == cms.OK, "confirmed -> region written")
        print("PASS main_first_wire_confirm_yes_writes")
    finally:
        restore()
        shutil.rmtree(d, ignore_errors=True)


def test_main_first_wire_decline_writes_nothing():
    d = _tmp()
    restore = _stub(tty=True, prompt=False)
    try:
        t = d / "CLAUDE.md"
        rc = cms.main([str(t)])
        _assert(rc == 0, f"declined wire -> rc 0 (deliberate), got {rc}")
        _assert(not t.is_file(), "declined -> file must NOT be created")
        print("PASS main_first_wire_decline_writes_nothing")
    finally:
        restore()
        shutil.rmtree(d, ignore_errors=True)


def test_main_yes_flag_bypasses_prompt():
    d = _tmp()
    # prompt would DECLINE if consulted — --yes must write anyway.
    restore = _stub(tty=True, prompt=False)
    try:
        t = d / "CLAUDE.md"
        rc = cms.main(["--yes", str(t)])
        _assert(rc == 0 and cms.evaluate(t) == cms.OK,
                "--yes must wire without consulting the prompt")
        print("PASS main_yes_flag_bypasses_prompt")
    finally:
        restore()
        shutil.rmtree(d, ignore_errors=True)


def test_main_noninteractive_without_yes_aborts():
    d = _tmp()
    # No TTY and no --yes: must not hang, must not write, must signal non-zero.
    def _boom(_q):
        raise AssertionError("must not prompt when stdin is not a TTY")
    restore = _stub(tty=False, prompt=_boom)
    try:
        t = d / "CLAUDE.md"
        rc = cms.main([str(t)])
        _assert(rc == 1, f"non-interactive first-wire -> rc 1, got {rc}")
        _assert(not t.is_file(), "non-interactive -> file must NOT be created")
        print("PASS main_noninteractive_without_yes_aborts")
    finally:
        restore()
        shutil.rmtree(d, ignore_errors=True)


def test_main_drift_resync_does_not_prompt():
    d = _tmp()
    # Wire directly (no prompt), tamper, then re-sync via main with a prompt that
    # raises if consulted — drift re-sync is region-only and must never prompt.
    def _boom(_q):
        raise AssertionError("must not prompt on a drift re-sync")
    restore = _stub(tty=True, prompt=_boom)
    try:
        t = d / "CLAUDE.md"
        cms.sync(t)  # first wiring, bypassing main()
        t.write_text(t.read_text(encoding="utf-8").replace("KB usage", "KB X"),
                     encoding="utf-8")
        _assert(cms.evaluate(t) == cms.DRIFT, "tampered region -> DRIFT")
        rc = cms.main([str(t)])
        _assert(rc == 0 and cms.evaluate(t) == cms.OK,
                "drift re-sync must succeed without prompting")
        print("PASS main_drift_resync_does_not_prompt")
    finally:
        restore()
        shutil.rmtree(d, ignore_errors=True)


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\nAll {len(fns)} claude_md_sync tests passed.")


if __name__ == "__main__":
    _run_all()
