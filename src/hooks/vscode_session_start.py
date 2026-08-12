#!/usr/bin/env python3
"""VS Code/Copilot SessionStart hook: silent AGENTS.md sync.

This is intentionally thinner than Claude Code's lifecycle hook and separate
from Codex's hook. VS Code's hook support is still preview, and VS Code can
also discover Claude Code hook files. It does not spawn transcript compaction
or write Codex session markers. Healthy startup emits no model context.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure src/ and src/hooks/ are importable when VS Code launches this script
# directly from .github/hooks/latch.json.
SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

from _common import (  # noqa: E402
    fence_inactive_session,
    hook_field,
    log,
    read_hook_input,
    record_session_binding,
    session_id,
    session_start_transition,
)

import project_config  # noqa: E402
from project_config import ProjectConfigError  # noqa: E402
from paths import is_disabled, is_in_compact, is_unlatched_mode  # noqa: E402
from session_start import (  # noqa: E402
    _build_locked_notice,
    _build_locked_system_message,
    _build_unlatched_notice,
    _build_unlatched_system_message,
    _emit_session_start_context,
    _join_startup_notices,
    _managed_doc_wiring_notice,
)


def vscode_project_cwd(payload: dict) -> str:
    return hook_field(
        payload,
        "cwd",
        "workingDirectory",
        "workspaceRoot",
        "workspaceFolder",
        "workdir",
        default=os.getcwd(),
    )


def main() -> int:
    if is_in_compact():
        return 0
    payload = read_hook_input()
    cwd = vscode_project_cwd(payload)
    try:
        with session_start_transition(cwd):
            return _run_session_start(payload, cwd)
    except (OSError, ProjectConfigError) as exc:
        log(
            f"vscode_session_start transition coordination failed: {exc}",
            cwd,
            expected_revision="stale-session",
        )
        _emit_session_start_context(
            "_⚠ Latch could not safely complete project session setup; restart "
            "the task after any latch/unlatch command finishes._"
        )
        return 0


def _run_session_start(payload: dict, cwd: str) -> int:
    sid = session_id(payload)
    if is_unlatched_mode(cwd):
        fence_inactive_session(cwd, sid)
        _emit_session_start_context(
            _build_unlatched_notice(cwd),
            system_message=_build_unlatched_system_message(cwd),
        )
        return 0
    target = project_config.resolve(cwd)
    if target.state == project_config.MODE_LOCKED:
        fence_inactive_session(cwd, sid)
        _emit_session_start_context(
            _build_locked_notice(target),
            system_message=_build_locked_system_message(target),
        )
        return 0
    try:
        binding_revision = record_session_binding(cwd, sid)
    except (OSError, ProjectConfigError) as exc:
        log(
            f"vscode_session_start binding snapshot failed: {exc}",
            cwd,
            expected_revision="stale-session",
        )
        _emit_session_start_context(
            "_⚠ Latch could not safely bind this agent task to the project's "
            "current KB. Start a fresh agent task in this project (do not "
            "resume the old one)._"
        )
        return 0
    if binding_revision is None:
        log(
            "vscode_session_start could not verify a session id for this binding",
            cwd,
            expected_revision="stale-session",
        )
        _emit_session_start_context(
            "_⚠ Latch could not verify this agent task for the project's "
            "current KB. Start a fresh agent task in this project._"
        )
        return 0
    if is_disabled(cwd):
        return 0

    agents_md_action = _auto_sync_agents_md(cwd, binding_revision)
    wiring_notice = _managed_doc_wiring_notice(
        agents_md_action,
        doc_name="AGENTS.md",
        manual_command=f"{SRC.parent}/bin/install_agents_md.sh --yes",
    )
    notice = _join_startup_notices(
        wiring_notice,
    )
    if notice:
        _emit_session_start_context(notice)

    return 0


def _auto_sync_agents_md(
    cwd: str,
    expected_revision: str | None = None,
) -> str | None:
    """Re-sync this project's AGENTS.md managed region when already wired."""
    try:
        import agents_md_sync

        target = Path(cwd) / "AGENTS.md"
        action = agents_md_sync.sync(target, create=False)
        if action == "synced":
            log(
                f"agents_md auto-sync: re-synced managed region in {target} "
                f"(backup: {target}.latchbak)",
                cwd,
                expected_revision=expected_revision,
            )
        return action
    except Exception as e:
        log(
            f"agents_md auto-sync skipped: {e}",
            cwd,
            expected_revision=expected_revision,
        )
        return "error"


if __name__ == "__main__":
    sys.exit(main())
