"""Claude Code SessionStart hook.

Healthy startup is deliberately silent.  The hook keeps managed CLAUDE.md
wiring current without reading or writing the KB, and emits only exceptional
state that the user or agent must know about.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from _common import (
    fence_inactive_session,
    log,
    project_cwd,
    read_hook_input,
    record_session_binding,
    session_id,
    session_start_transition,
)

import project_config
from project_config import ProjectConfigError
from paths import (
    KB_ROOT,
    is_disabled,
    is_in_compact,
    is_unlatched_mode,
    unlatch_scope,
)


_SESSION_SETUP_NOTICE = (
    "_⚠ Latch could not safely bind this agent task to the project's current "
    "KB. Start a fresh agent task in this project (do not resume the old one); "
    "run `latch doctor` if this repeats._"
)


def main() -> int:
    if is_in_compact():
        return 0
    payload = read_hook_input()
    cwd = project_cwd(payload)
    try:
        with session_start_transition(cwd):
            return _run_session_start(payload, cwd)
    except (OSError, ProjectConfigError) as exc:
        log(
            f"session_start transition coordination failed: {exc}",
            cwd,
            expected_revision="stale-session",
        )
        _emit_session_start_context(_SESSION_SETUP_NOTICE)
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
            f"session_start binding snapshot failed: {exc}",
            cwd,
            expected_revision="stale-session",
        )
        _emit_session_start_context(_SESSION_SETUP_NOTICE)
        return 0
    if binding_revision is None:
        log(
            "session_start could not verify a session id for this project binding",
            cwd,
            expected_revision="stale-session",
        )
        _emit_session_start_context(_SESSION_SETUP_NOTICE)
        return 0
    if is_disabled(cwd):
        return 0

    claude_md_action = _auto_sync_claude_md(cwd, binding_revision)
    wiring_notice = _managed_doc_wiring_notice(
        claude_md_action,
        doc_name="CLAUDE.md",
        manual_command=f"{KB_ROOT}/bin/install_claude_md.sh --yes",
    )
    notice = _join_startup_notices(
        wiring_notice,
    )
    if notice:
        _emit_session_start_context(notice)
    return 0


def _join_startup_notices(*notices: str | None) -> str:
    """Join exceptional startup notices without manufacturing routine output."""
    return "\n\n".join(
        notice.strip() for notice in notices if notice and notice.strip()
    )


def _emit_session_start_context(
    context: str,
    system_message: str | None = None,
) -> None:
    """Emit a Claude-compatible SessionStart additionalContext envelope."""
    if not context:
        return
    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    if system_message:
        out["systemMessage"] = system_message
    print(json.dumps(out))


def _scope_receipt(cwd: str) -> str:
    try:
        scope = unlatch_scope(cwd)
    except (OSError, ProjectConfigError):
        scope = None
    if scope == "project":
        return "Only this project is unlatched; other projects and KBs are unchanged."
    if scope == "global":
        return "A legacy/global override is active, so other projects may also be unlatched."
    return "Run /latch status before assuming whether other projects are affected."


def _build_unlatched_system_message(cwd: str) -> str:
    return (
        "LATCH UNLATCHED MODE ACTIVE: Latch is OFF for this scope. "
        "This is the agent without Latch's judgment layer. Automatic "
        "retrieval, gate guidance, compaction, self-heal, maintenance, and "
        "automatic Latch writes are disabled here until the user runs /latch. "
        + _scope_receipt(cwd)
    )


def _build_unlatched_notice(cwd: str) -> str:
    """Static exceptional receipt for Unlatched mode; performs no KB work."""
    return "\n".join([
        "# latch is unlatched",
        "",
        "Latch is currently UNLATCHED.",
        "This is the agent without Latch's judgment layer.",
        f"Scope: {_scope_receipt(cwd)}",
        "",
        "- Disabled: automatic retrieval, gate guidance, Stop/SessionEnd "
        "compaction, self-heal, maintenance, and automatic Latch writes.",
        "- Still true: your KB is local and unchanged, Latch remains installed, "
        "/latch and /unlatch remain available, MCP registration remains present, and "
        "non-Latch tools/hooks are unaffected.",
        "- Run `/latch` to re-latch. If `LATCH_UNLATCHED` is set, unset it too.",
        f"- Latch home: `{KB_ROOT}`.",
    ])


def _build_locked_system_message(target: project_config.ResolvedScope) -> str:
    return (
        "LATCH LOCKED: no KB access is allowed for this filesystem scope. "
        f"Root: {target.project_root}. Policy: {target.policy or 'unknown'}. "
        f"Reason: {target.reason or 'no safe KB target'}. Run /latch status "
        "and explicitly repair or authorize this root before using Latch."
    )


def _build_locked_notice(target: project_config.ResolvedScope) -> str:
    """Visible fail-closed receipt; never opens or creates a KB."""
    return "\n".join([
        "# latch is locked",
        "",
        "Latch is currently LOCKED. No KB was opened.",
        f"Root: `{target.project_root}`",
        f"Policy: `{target.policy or 'unknown'}`",
        f"Scope: `{target.scope_id or target.source}`",
        f"Last known KB: `{target.remembered_kb_dir or 'none'}`",
        f"Reason: {target.reason or 'no safe KB target'}",
        "",
        "Run `/latch status` and explicitly repair or authorize this root. "
        "Other project scopes remain unchanged.",
    ])


def _auto_sync_claude_md(
    cwd: str,
    expected_revision: str | None = None,
) -> str | None:
    """Re-sync an already-managed CLAUDE.md region without first-wiring a repo."""
    try:
        import claude_md_sync

        target = Path(cwd) / "CLAUDE.md"
        action = claude_md_sync.sync_if_outdated(target)
        if action == "synced":
            log(
                f"claude_md auto-sync: re-synced managed region in {target} "
                f"(backup: {target}.latchbak)",
                cwd,
                expected_revision=expected_revision,
            )
        return action
    except Exception as exc:
        log(
            f"claude_md auto-sync skipped: {exc}",
            cwd,
            expected_revision=expected_revision,
        )
        return "error"


def _managed_doc_wiring_notice(
    action: str | None,
    *,
    doc_name: str,
    manual_command: str,
    restart_required: bool = False,
) -> str | None:
    """Describe one-time repair or exceptional managed-document states."""
    if action == "synced":
        restart = (
            " Restart or open a new task to reload host wiring."
            if restart_required
            else ""
        )
        return (
            f"_↻ Latch repaired older {doc_name} project wiring once (managed "
            f"region only; user content was preserved; backup: "
            f"`{doc_name}.latchbak`).{restart}_"
        )
    if action == "newer":
        return (
            f"_⚠ {doc_name} has newer Latch project wiring than this engine. "
            f"Latch did not downgrade it; update the engine or inspect with "
            f"`{manual_command} --check`._"
        )
    if action in ("invalid", "error"):
        return (
            f"_⚠ Latch could not safely repair {doc_name} project wiring. The "
            f"session will continue; run `{manual_command}` manually._"
        )
    return None


if __name__ == "__main__":
    sys.exit(main())
