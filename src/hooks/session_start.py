"""Claude Code SessionStart hook.

Healthy startup is deliberately silent.  The hook keeps managed CLAUDE.md
wiring current without reading or writing the KB, and emits only exceptional
state that the user or agent must know about.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from _common import log, project_cwd, read_hook_input

from paths import KB_ROOT, is_disabled, is_in_compact, is_unlatched_mode


_SESSION_SETUP_NOTICE = (
    "_⚠ Latch could not complete silent session setup. Latch will continue, "
    "but current-session attribution or transcript-aware workflows may be "
    "degraded; run `latch doctor` and inspect the hook log._"
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
    cwd = project_cwd(payload)

    claude_md_action = _auto_sync_claude_md(cwd)
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


def _build_unlatched_system_message() -> str:
    return (
        "LATCH UNLATCHED MODE ACTIVE: Latch is OFF for this install. "
        "This is the agent without Latch's project judgment layer. Automatic "
        "retrieval, gate guidance, compaction, self-heal, maintenance, and "
        "automatic Latch writes are disabled until the user runs /unlatch and "
        "confirms Latch. If LATCH_UNLATCHED is set, unset it too."
    )


def _build_unlatched_notice() -> str:
    """Static exceptional receipt for Unlatched mode; performs no KB work."""
    return "\n".join([
        "# latch is unlatched",
        "",
        "Latch is currently UNLATCHED.",
        "This is the agent without Latch's project judgment layer.",
        "Scope: this Latch install stays unlatched until you re-latch, even if "
        "you change repos.",
        "",
        "- Disabled: automatic retrieval, gate guidance, Stop/SessionEnd "
        "compaction, self-heal, maintenance, and automatic Latch writes.",
        "- Still true: your KB is local and unchanged, Latch remains installed, "
        "/unlatch remains available, MCP registration remains present, and "
        "non-Latch tools/hooks are unaffected.",
        "- Run `/unlatch` to re-latch. If `LATCH_UNLATCHED` is set, unset it too.",
        f"- Latch home: `{KB_ROOT}`.",
    ])


def _auto_sync_claude_md(cwd: str) -> str | None:
    """Re-sync an already-managed CLAUDE.md region without first-wiring a repo."""
    try:
        import claude_md_sync

        target = Path(cwd) / "CLAUDE.md"
        action = claude_md_sync.sync_if_outdated(target)
        if action == "synced":
            log(
                f"claude_md auto-sync: re-synced managed region in {target} "
                f"(backup: {target}.latchbak)"
            )
        return action
    except Exception as exc:
        log(f"claude_md auto-sync skipped: {exc}")
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
