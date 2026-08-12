#!/usr/bin/env python3
"""Codex SessionStart hook: silent attribution and AGENTS.md sync.

Codex support intentionally does not mirror Claude Code's Stop/SessionEnd
automatic compaction. Healthy startup emits no model context.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure src/ and src/hooks/ are importable when Codex launches this script
# directly from hooks.json.
SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

from _common import (  # noqa: E402
    fence_inactive_session,
    hook_field,
    log,
    read_hook_input,
    record_session_binding,
    session_start_transition,
    transcript_path,
)

import codex_session  # noqa: E402
import project_config  # noqa: E402
from project_config import ProjectConfigError  # noqa: E402
from paths import is_disabled, is_in_compact, is_unlatched_mode  # noqa: E402
from session_start import (  # noqa: E402
    _SESSION_SETUP_NOTICE,
    _build_locked_notice,
    _build_locked_system_message,
    _build_unlatched_notice,
    _build_unlatched_system_message,
    _emit_session_start_context,
    _join_startup_notices,
    _managed_doc_wiring_notice,
)


def codex_project_cwd(payload: dict) -> str:
    return hook_field(
        payload,
        "cwd",
        "workingDirectory",
        "workspaceRoot",
        "workdir",
        default=os.getcwd(),
    )


def codex_session_id(payload: dict) -> str | None:
    return (
        hook_field(payload, "session_id", "sessionId", "thread_id", "threadId", "id")
        or os.environ.get("CODEX_THREAD_ID")
    )


def main() -> int:
    if is_in_compact():
        return 0
    payload = read_hook_input()
    cwd = codex_project_cwd(payload)
    try:
        with session_start_transition(cwd):
            return _run_session_start(payload, cwd)
    except (OSError, ProjectConfigError) as exc:
        log(
            f"codex_session_start transition coordination failed: {exc}",
            cwd,
            expected_revision="stale-session",
        )
        _emit_session_start_context(_SESSION_SETUP_NOTICE)
        return 0


def _run_session_start(payload: dict, cwd: str) -> int:
    sid = codex_session_id(payload)
    tpath = transcript_path(payload)

    setup_degraded = not bool(sid)
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
        # A resumed task can fail here after another fresh task has re-pinned
        # the project and written its marker.  Tombstone that shared workspace
        # handoff so the stale task cannot inherit the fresh task's identity.
        _invalidate_workspace_marker(cwd)
        log(
            f"codex_session_start binding snapshot failed: {exc}",
            cwd,
            expected_revision="stale-session",
        )
        _emit_session_start_context(_SESSION_SETUP_NOTICE)
        return 0
    if binding_revision is None:
        _invalidate_workspace_marker(cwd)
        log(
            "codex_session_start could not verify a session id for this project binding",
            cwd,
            expected_revision="stale-session",
        )
        _emit_session_start_context(_SESSION_SETUP_NOTICE)
        return 0
    if is_disabled(cwd):
        return 0
    if sid:
        # Session attribution is independent of KB setup.  In particular, a
        # readable external vault may be outside this hook's writable sandbox;
        # codex_session supplies a private runtime fallback for that case.
        try:
            codex_session.write_marker(
                cwd, sid, transcript_path=tpath,
            )
        except Exception as e:
            setup_degraded = True
            _invalidate_workspace_marker(cwd)
            log(
                f"codex_session_start marker write failed: {e}",
                cwd,
                expected_revision=binding_revision,
            )
    else:
        # A missing current id must invalidate any prior workspace marker;
        # otherwise an MCP process without CODEX_THREAD_ID can inherit the
        # previous task's attribution.
        try:
            codex_session.invalidate_marker(cwd)
        except Exception as e:
            log(
                f"codex_session_start marker invalidation failed: {e}",
                cwd,
                expected_revision=binding_revision,
            )

    agents_md_action = _auto_sync_agents_md(
        cwd,
        expected_revision=binding_revision,
    )
    wiring_notice = _managed_doc_wiring_notice(
        agents_md_action,
        doc_name="AGENTS.md",
        manual_command=f"{SRC.parent}/bin/install_agents_md.sh --yes",
    )

    notice = _join_startup_notices(
        _SESSION_SETUP_NOTICE if setup_degraded else None,
        wiring_notice,
    )
    if notice:
        _emit_session_start_context(notice)

    return 0


def _invalidate_workspace_marker(cwd: str) -> bool:
    """Best-effort fail-closed invalidation without selecting a KB for logs."""
    try:
        codex_session.invalidate_marker(cwd)
    except Exception:
        return False
    return True


def _auto_sync_agents_md(
    cwd: str,
    *,
    expected_revision: str | None = None,
) -> str | None:
    """Re-sync this project's AGENTS.md managed region when already wired.

    Mirrors Claude's CLAUDE.md hot-path behavior: ``create=False`` means a
    fresh or unmanaged project is never auto-wired, but an existing managed
    region is kept current after latch upgrades. Wrapped so sync failures never
    break Codex session startup.
    """
    try:
        import agents_md_sync
        target = Path(cwd) / "AGENTS.md"
        action = agents_md_sync.sync_if_outdated(target)
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
