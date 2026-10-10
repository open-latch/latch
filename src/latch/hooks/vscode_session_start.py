#!/usr/bin/env python3
"""VS Code/Copilot SessionStart hook: silent AGENTS.md sync.

This is intentionally thinner than Claude Code's lifecycle hook and separate
from Codex's hook. VS Code's hook support is still preview, and VS Code can
also discover Claude Code hook files. It does not spawn transcript compaction
or write Codex session markers. Healthy startup emits no model context.
"""
from __future__ import annotations
if __package__ in (None, ""):
    import sys, pathlib
    sys.path.insert(0, str(next(p for p in pathlib.Path(__file__).resolve().parents if p.name == "src")))

import os
import sys
from pathlib import Path

from latch.hooks._common import hook_field, log, read_hook_input  # noqa: E402

from latch.store.paths import is_disabled, is_in_compact, is_unlatched_mode  # noqa: E402
from latch.hooks.session_start import (  # noqa: E402
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
    if is_unlatched_mode():
        _emit_session_start_context(
            _build_unlatched_notice(),
            system_message=_build_unlatched_system_message(),
        )
        return 0
    if is_disabled():
        return 0

    payload = read_hook_input()
    cwd = vscode_project_cwd(payload)

    agents_md_action = _auto_sync_agents_md(cwd)
    wiring_notice = _managed_doc_wiring_notice(
        agents_md_action,
        doc_name="AGENTS.md",
        manual_command=f"{next(p for p in Path(__file__).resolve().parents if p.name == 'src').parent}/bin/install_agents_md.sh --yes",
    )
    notice = _join_startup_notices(
        wiring_notice,
    )
    if notice:
        _emit_session_start_context(notice)

    return 0


def _auto_sync_agents_md(cwd: str) -> str | None:
    """Re-sync this project's AGENTS.md managed region when already wired."""
    try:
        from latch.hosts import agents_md_sync

        target = Path(cwd) / "AGENTS.md"
        action = agents_md_sync.sync(target, create=False)
        if action == "synced":
            log(f"agents_md auto-sync: re-synced managed region in {target} "
                f"(backup: {target}.latchbak)")
        return action
    except Exception as e:
        log(f"agents_md auto-sync skipped: {e}")
        return "error"


if __name__ == "__main__":
    sys.exit(main())
