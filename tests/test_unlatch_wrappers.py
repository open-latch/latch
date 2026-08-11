"""latch_enable/latch_status wrapper behavior over the global Unlatch receipts.

The new Global Shared unlatch writes the KB_HOME/UNLATCHED sentinel (plus a
per-scope machine-local receipt) and never the legacy UNLATCH_STATE.json.
latch_status advertises `bash bin/latch_enable.sh` as the resume command, so
that wrapper must recover both receipt generations: the legacy JSON receipt via
`unlatch.py on --legacy-state`, and the new sentinel by delegating to
`project_mode.py latch --confirm latch`.
"""
from __future__ import annotations

from pathlib import Path

# Reuse the scope harness so wrapper behavior is asserted against the same
# Global Shared install model as the command-lifecycle suite.
from test_unlatch import (  # noqa: F401  (scope_harness is a fixture import)
    ScopeHarness,
    _run,
    scope_harness,
)

import project_config


CLAUDE_MD = b"# project instructions\nkeep me byte-exact\n"


def _globally_unlatch(harness: ScopeHarness) -> None:
    (harness.root / "CLAUDE.md").write_bytes(CLAUDE_MD)
    _run("unlatch.sh", harness, harness.root, "--confirm", "unlatch")
    assert (harness.home / "UNLATCHED").is_file()
    assert (harness.home / "DISABLE").is_file()
    assert not (harness.home / "UNLATCH_STATE.json").exists()
    assert (harness.root / "CLAUDE.md").read_bytes() != CLAUDE_MD


def test_status_advertised_resume_command_recovers_new_unlatched_sentinel(
    scope_harness: ScopeHarness,
) -> None:
    _globally_unlatch(scope_harness)

    status = _run("latch_status.sh", scope_harness, scope_harness.root, check=False)
    assert "[UNLATCHED-GLOBAL]" in status.stdout
    assert "legacy" not in status.stdout.splitlines()[1]
    assert "resume: bash bin/latch_enable.sh" in status.stdout

    enable = _run("latch_enable.sh", scope_harness, scope_harness.root)
    assert not (scope_harness.home / "UNLATCHED").exists()
    assert not (scope_harness.home / "DISABLE").exists()
    assert (scope_harness.root / "CLAUDE.md").read_bytes() == CLAUDE_MD
    assert "latch ENABLED" in enable.stdout
    assert project_config.resolve(scope_harness.root).state == (
        project_config.MODE_LATCHED
    )

    after = _run("latch_status.sh", scope_harness, scope_harness.root, check=False)
    assert after.returncode == 0
    assert "[GLOBAL-CLEAR]" in after.stdout


def test_latch_enable_recovers_from_receipt_root_not_cwd(
    scope_harness: ScopeHarness,
) -> None:
    _globally_unlatch(scope_harness)
    elsewhere = scope_harness.home  # any directory that is not the project root

    _run("latch_enable.sh", scope_harness, elsewhere)

    assert not (scope_harness.home / "UNLATCHED").exists()
    assert not (scope_harness.home / "DISABLE").exists()
    assert (scope_harness.root / "CLAUDE.md").read_bytes() == CLAUDE_MD


def test_latch_enable_without_all_preserves_disable_write(
    scope_harness: ScopeHarness,
) -> None:
    _globally_unlatch(scope_harness)
    disable_write = scope_harness.home / "DISABLE_WRITE"
    disable_write.write_text("write-side off - exact bytes\n", encoding="utf-8")

    result = _run("latch_enable.sh", scope_harness, scope_harness.root)

    assert not (scope_harness.home / "UNLATCHED").exists()
    assert not (scope_harness.home / "DISABLE").exists()
    assert disable_write.read_text(encoding="utf-8") == (
        "write-side off - exact bytes\n"
    )
    assert not disable_write.with_name(
        "DISABLE_WRITE.latch-enable-keep"
    ).exists()
    assert "DISABLE_WRITE still present" in result.stdout
    assert (scope_harness.root / "CLAUDE.md").read_bytes() == CLAUDE_MD


def test_latch_enable_all_also_removes_disable_write(
    scope_harness: ScopeHarness,
) -> None:
    _globally_unlatch(scope_harness)
    disable_write = scope_harness.home / "DISABLE_WRITE"
    disable_write.write_text("write-side off\n", encoding="utf-8")

    _run("latch_enable.sh", scope_harness, scope_harness.root, "--all")

    assert not (scope_harness.home / "UNLATCHED").exists()
    assert not (scope_harness.home / "DISABLE").exists()
    assert not disable_write.exists()


def test_legacy_unlatch_state_receipt_still_routes_to_legacy_recovery(
    scope_harness: ScopeHarness,
) -> None:
    # A genuine legacy install carries UNLATCH_STATE.json. The wrapper must
    # keep routing that generation through the verified legacy path (which
    # rejects this malformed receipt) instead of the new-sentinel delegation.
    (scope_harness.home / "UNLATCHED").write_text("legacy\n", encoding="utf-8")
    (scope_harness.home / "UNLATCH_STATE.json").write_text(
        "{not json", encoding="utf-8"
    )

    result = _run(
        "latch_enable.sh",
        scope_harness,
        scope_harness.root,
        check=False,
    )

    assert result.returncode != 0
    assert "legacy" in (result.stderr + result.stdout).lower()
    # Fail-closed: the sentinel and receipt stay until legacy recovery works.
    assert (scope_harness.home / "UNLATCHED").exists()
    assert (scope_harness.home / "UNLATCH_STATE.json").exists()
