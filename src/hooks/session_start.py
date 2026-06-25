"""SessionStart hook: reconcile orphaned sessions, then brief the new one.

1. For any prior session in this project where ended_at IS NULL but turns
   advanced past last_compact_turn, fire a final compact (synchronously, so
   the brief includes its summary).
2. Print a short briefing to stdout (latest canonical session summary +
   unreviewed staging facts) — Claude Code prepends this to the new session.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from _common import log, project_cwd, read_hook_input, session_id, transcript_path

import budget
import db
import priorities
from paths import is_disabled, is_in_compact


MAX_WORKSTREAMS = 5
MAX_OPEN_QUESTIONS = 3
MAX_BRIEFING_IDEAS = 5

# Below this many (non-stale) nodes the KB is treated as new, and the brief
# leads with a short getting-started block so a first-time user gets value
# before learning to operate latch. The block self-removes once the KB has a
# little history — a couple of real working sessions clear the threshold.
NEW_USER_NODE_THRESHOLD = 8

_GETTING_STARTED_BLOCK = (
    "## Getting started with latch\n\n"
    "latch is building a memory for this project as you work — the *why* behind "
    "decisions, what got ruled out, and where things stand — so a fresh session "
    "resumes with full context instead of starting cold.\n\n"
    "- **It fills automatically as we work** — I capture decisions and durable "
    "findings into the KB without you having to ask.\n"
    "- **At the end of a working session, ask me to `/kb-compact`** — it "
    "summarizes the session into the KB so the reasoning isn't lost (this is "
    "latch's command — *not* Claude Code's built-in `/compact`, which only trims "
    "the conversation and saves nothing to the KB). Budget-gated and quick.\n"
    "- **New project? Seed one decision** — capture one approach you've already "
    "ruled out and why, then ask for it later and latch can surface the prior "
    "decision before the agent repeats old work.\n"
    "- This note disappears once your KB has a little history.\n"
)


def main() -> int:
    if is_disabled() or is_in_compact():
        return 0
    payload = read_hook_input()
    cwd = project_cwd(payload)
    sid = session_id(payload)
    tpath = transcript_path(payload)

    surfaced_ids: list[int] = []
    try:
        conn = db.connect(cwd)
        try:
            if sid:
                db.upsert_session(conn, sid, cwd, tpath)
            orphans = db.orphaned_sessions(conn, cwd)
        finally:
            conn.close()
    except Exception as e:
        log(f"session_start db error: {e}")
        orphans = []

    # Note: orphan reconciliation is intentionally manual-only. Synchronous
    # auto-reconciliation was the primary amplifier in the 2026-04-23 fan-out
    # incident — every new summarizer session triggered by the compactor would
    # re-enter here and spawn compactions for every orphan, recursively.
    # Orphans are now surfaced in the briefing only; run /kb-compact to process.
    orphan_count = len(orphans)

    try:
        budget_line = budget.brief_line(cwd)
    except Exception as e:
        log(f"budget brief_line failed: {e}")
        budget_line = None

    # Auto-sync CLAUDE.md BEFORE building the brief so a re-sync this session is
    # visible in-session (it is otherwise silent — log-only). Behavior of the
    # sync itself is unchanged; it was previously called at the end of main().
    claude_md_action = _auto_sync_claude_md(cwd)

    briefing = _build_briefing(
        cwd, orphan_count=orphan_count, budget_line=budget_line,
        surfaced_ids=surfaced_ids,
        claude_md_synced=(claude_md_action == "synced"),
    )

    # Seed the active set with what the brief just put in front of the model,
    # so UserPromptSubmit dedupe sees them on turn 1.
    if sid and surfaced_ids:
        try:
            conn = db.connect(cwd)
            try:
                db.record_retrievals(
                    conn, session_id=sid, turn=0,
                    items=[(nid, None) for nid in surfaced_ids],
                    source="session_start",
                )
            finally:
                conn.close()
        except Exception as e:
            log(f"session_start record_retrievals failed: {e}")

    if briefing:
        # Hook stdout becomes additionalContext for the session.
        # JSON form is the spec; if Claude Code ignores the envelope it falls
        # back to treating stdout as plain text — both yield a usable brief.
        out = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": briefing,
            }
        }
        print(json.dumps(out))

    return 0


def _auto_sync_claude_md(cwd: str) -> str | None:
    """Re-sync this project's CLAUDE.md latch-contract region IF it has already
    been wired (markers present) and the snippet changed upstream. Opt-in by the
    markers: ``create=False`` never auto-wires a fresh project. Silent — writes
    to disk + hooks.log only, NOT stdout, so it never pollutes additionalContext
    or costs context tokens. Dependency-free (no numpy/db/embeddings import), so
    it is safe on the hot SessionStart path. Wrapped — a sync failure must never
    break the session.

    Returns the sync action ('synced' | 'unchanged' | 'skipped' | ...) so the
    caller can surface a one-time in-session note when the region was actually
    rewritten; None if the sync errored (already logged).
    """
    try:
        import claude_md_sync
        target = Path(cwd) / "CLAUDE.md"
        action = claude_md_sync.sync(target, create=False)
        if action == "synced":
            log(f"claude_md auto-sync: re-synced managed region in {target} "
                f"(backup: {target}.latchbak)")
        return action
    except Exception as e:
        log(f"claude_md auto-sync skipped: {e}")
        return None


def _build_briefing(
    cwd: str,
    orphan_count: int = 0,
    budget_line: str | None = None,
    surfaced_ids: list[int] | None = None,
    claude_md_synced: bool = False,
    synced_doc_name: str = "CLAUDE.md",
) -> str:
    """Build the SessionStart additionalContext brief.

    If `surfaced_ids` is provided, the ids of every node included in the brief
    are appended to it — main() uses this to seed session_retrievals so the
    per-prompt hook can dedupe from turn 1.

    `claude_md_synced` True means a managed instruction-file auto-sync rewrote
    the managed region this session — surface a one-time notice so the
    otherwise-silent re-sync is visible to the user. `synced_doc_name` keeps
    Codex's AGENTS.md notice honest while preserving Claude's default wording.
    """
    try:
        conn = db.connect(cwd)
        try:
            focus_rows = db.get_focus(conn, limit=MAX_WORKSTREAMS)
            # Fallback: focus table empty (fresh DB, freshly evicted, or before
            # any auto-bump activity). Recent-canonical workstreams keep the
            # brief useful instead of going silent.
            if focus_rows:
                workstreams = focus_rows
                workstreams_from_focus = True
            else:
                workstreams = db.recent_nodes(
                    conn, kind="workstream", status="canonical",
                    limit=MAX_WORKSTREAMS,
                )
                workstreams_from_focus = False
            # status=canonical on an open_question means "resolved" — drop those
            # from the brief so they stop bugging the user. Over-fetch since the
            # API only filters TO a single status; we want everything EXCEPT
            # canonical (and stale, which recent_nodes already excludes by default).
            open_qs = [
                n for n in db.recent_nodes(
                    conn, kind="open_question", limit=MAX_OPEN_QUESTIONS * 3,
                )
                if n.get("status") != "canonical"
            ][:MAX_OPEN_QUESTIONS]
            ideas = db.recent_nodes(conn, kind="idea", limit=MAX_BRIEFING_IDEAS)
            latest_progress = db.recent_nodes(
                conn, kind="progress", status="canonical", limit=1,
            )
            prio = priorities.list_priorities(conn)
            workstream_prio: dict[int, list[dict]] = {}
            for ws in workstreams:
                wid = ws.get("workstream_id") or ws.get("id")
                if wid is None:
                    continue
                workstream_prio[int(wid)] = priorities.list_priorities(
                    conn, workstream_id=int(wid),
                )
            # New-user detection: cheap COUNT(*), same connection. Drives the
            # getting-started block below.
            show_getting_started = db.node_count(conn) < NEW_USER_NODE_THRESHOLD
        finally:
            conn.close()
    except Exception as e:
        log(f"briefing build failed: {e}")
        return ""

    # Pending body-edge / state drift from the last nightly sweep (id=1149
    # Part 3). Lightweight log read — drift.latest_pending pulls no DB and no
    # heal/numpy import, so it's safe on the hot SessionStart path.
    try:
        import drift
        n_drift, _ = drift.latest_pending(cwd)
    except Exception as e:
        log(f"drift pending count failed: {e}")
        n_drift = 0

    if surfaced_ids is not None:
        scoped_prio = [
            p for rows in workstream_prio.values() for p in rows
        ]
        for collection in (
            workstreams, open_qs, ideas, latest_progress, prio, scoped_prio,
        ):
            surfaced_ids.extend(n["id"] for n in collection)

    if (not workstreams and not open_qs and not ideas and not latest_progress
            and not prio and not orphan_count and not budget_line and not n_drift
            and not show_getting_started and not claude_md_synced):
        return ""

    parts = ["# latch — session brief\n"]
    # New-user onboarding leads the brief (and is the one thing a brand-new,
    # otherwise-empty KB has to show). Self-removes once the KB fills.
    if show_getting_started:
        parts.append(_GETTING_STARTED_BLOCK)
    if orphan_count:
        parts.append(
            f"_{orphan_count} prior session(s) have unreviewed transcripts. "
            f"Run `/kb-compact` to summarize them on demand._\n"
        )
    if budget_line:
        parts.append(f"_{budget_line}_\n")
    if n_drift:
        parts.append(
            f"_⚠ {n_drift} body-edge/state drift item(s) flagged by the last "
            f"nightly sweep — run `bash bin/run_kb_drift.sh` (or read the latest "
            f"`drift-*.log`) to review and `kb_link`/fix._\n"
        )
    if claude_md_synced:
        parts.append(
            f"_↻ latch {synced_doc_name} was re-synced from an updated snippet this "
            "session (managed region only — content outside the markers is "
            f"untouched; prior version backed up to `{synced_doc_name}.latchbak`)._\n"
        )

    # Top of mind: standing priorities lead the brief so they colour the whole
    # session, not just gate calls. Empty list → section omitted entirely.
    parts.extend(priorities.render_for_brief(prio))

    # Workstreams come first — stable topic pointers, not the latest task body.
    # The body itself is kept short by convention; render in full so the search
    # hints + key-node ids are visible without forcing a follow-up kb_get.
    if workstreams:
        if workstreams_from_focus:
            parts.append("## Focus (active workstreams)\n")
        else:
            parts.append("## Active workstreams\n")
        for ws in workstreams:
            marker = " (pinned)" if ws.get("pinned") else ""
            parts.append(f"- (id={ws['id']}){marker} **{ws['title']}**")
            parts.append(f"  {_one_line(ws['body'], n=320)}")
            wid = int(ws.get("workstream_id") or ws["id"])
            parts.extend(
                priorities.render_workstream_for_brief(
                    workstream_prio.get(wid, []),
                )
            )

    if open_qs:
        parts.append("\n## Open questions (most recent)\n")
        for q in open_qs:
            parts.append(f"- (id={q['id']}{_by(q)}) {q['title']}")

    if ideas:
        parts.append("\n## Parked ideas (future / hypothetical)\n")
        for n in ideas:
            parts.append(
                f"- (id={n['id']}{_by(n)}) **{n['title']}** — {_one_line(n['body'])}"
            )

    # Latest progress kept as a single one-liner pointer (not a full body dump).
    # Prevents the brief from anchoring the agent on yesterday's task.
    if latest_progress:
        s = latest_progress[0]
        parts.append(
            f"\n_Latest session note: (id={s['id']}, {s['updated_at']}{_by(s)}) "
            f"**{s['title']}** — `kb_get({s['id']})` for body._\n"
        )

    parts.append(
        "\n_Use `kb_search` / `kb_get` / `kb_recent` MCP tools to drill in. "
        "Workstream bodies are intentionally terse; search before acting._\n"
    )
    return "\n".join(parts)


def _one_line(s: str, n: int = 160) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _by(node: dict) -> str:
    """Render `, by=<user>` if attribution is present, else empty.
    Pre-migration nodes have NULL created_by — silently skip those."""
    user = node.get("created_by")
    return f", by={user}" if user else ""


if __name__ == "__main__":
    sys.exit(main())
