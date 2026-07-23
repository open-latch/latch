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
from dataclasses import dataclass
from pathlib import Path

from _common import log, project_cwd, read_hook_input, session_id, transcript_path

import budget
import db
import feeders
import lifecycle_receipts
import priorities
from paths import (
    KB_ROOT,
    is_disabled,
    is_in_compact,
    is_unlatched_mode,
    latch_intensity,
    normalize_latch_intensity,
)


MAX_WORKSTREAMS = 5
MAX_OPEN_QUESTIONS = 3
MAX_BRIEFING_IDEAS = 5
MAX_FEEDERS_PER_WORKSTREAM = 3
LIFECYCLE_RECEIPT_LIMITS = {"quiet": 1, "standard": 3, "full": 8}


@dataclass(frozen=True)
class BriefPolicy:
    max_workstreams: int
    max_open_questions: int
    max_ideas: int
    workstream_chars: int
    idea_chars: int
    include_priorities: bool
    include_latest_progress: bool
    max_feeders_per_workstream: int = MAX_FEEDERS_PER_WORKSTREAM
    compact: bool = False


BRIEF_POLICIES = {
    "quiet": BriefPolicy(
        1, 1, 0, 0, 0, False, False,
        max_feeders_per_workstream=1,
        compact=True,
    ),
    "standard": BriefPolicy(
        3, 2, 2, 240, 160, True, True,
        max_feeders_per_workstream=2,
    ),
    # Full preserves the shipped body bounds: 320-char workstream pointers and
    # 160-char idea excerpts. Legacy installs therefore retain their context
    # footprint instead of silently growing when they map to Full.
    "full": BriefPolicy(5, 3, 5, 320, 160, True, True),
}


def _bound_feeder_surface(
    workstream_feeders: dict[int, list[dict]],
    open_question_candidates: list[dict],
    idea_candidates: list[dict],
    policy: BriefPolicy,
) -> tuple[dict[int, list[dict]], list[dict], list[dict]]:
    """Make nested feeders obey the same tier caps as top-level pointers.

    Contextual feeders get first claim on the open-question/idea budgets. The
    remaining budget is filled by the ordinary recent-node sections, with ids
    deduped across workstreams and top-level sections.
    """
    bounded: dict[int, list[dict]] = {}
    seen: set[int] = set()
    question_budget = policy.max_open_questions
    idea_budget = policy.max_ideas

    for workstream_id, rows in workstream_feeders.items():
        selected: list[dict] = []
        for row in rows:
            if len(selected) >= policy.max_feeders_per_workstream:
                break
            node_id = int(row["id"])
            if node_id in seen:
                continue
            kind = str(row.get("kind") or "")
            if kind == "open_question":
                if question_budget <= 0:
                    continue
                question_budget -= 1
            elif kind == "idea":
                if idea_budget <= 0:
                    continue
                idea_budget -= 1
            seen.add(node_id)
            selected.append(row)
        if selected:
            bounded[workstream_id] = selected

    open_questions = [
        row for row in open_question_candidates if int(row["id"]) not in seen
    ][:question_budget]
    ideas = [
        row for row in idea_candidates if int(row["id"]) not in seen
    ][:idea_budget]
    return bounded, open_questions, ideas

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
    "- **At the end of a working session, ask me to `/latch-compact`** — it "
    "summarizes the session into the KB so the reasoning isn't lost (this is "
    "latch's command — *not* Claude Code's built-in `/compact`, which only trims "
    "the conversation and saves nothing to the KB). Budget-gated and quick.\n"
    "- **New project? Run `/latch-pm`** — tell me one approach you've already "
    "ruled out and why, then try asking for it again and watch latch catch the "
    "contradiction. Optional; re-runnable any time.\n"
    "- This note disappears once your KB has a little history.\n"
)

_GETTING_STARTED_QUIET_BLOCK = (
    "## Getting started\n\n"
    "Latch is ready to preserve decisions and rejected paths. Try `/latch-pm` "
    "for a first catch; use `/latch-compact` when you want this session saved.\n"
)


def main() -> int:
    if is_in_compact():
        return 0
    if is_unlatched_mode():
        _emit_session_start_context(
            _build_unlatched_brief(),
            system_message=_build_unlatched_system_message(),
        )
        return 0
    if is_disabled():
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
    # Orphans are now surfaced in the briefing only; run /latch-compact to process.
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
    wiring_notice = _managed_doc_wiring_notice(
        claude_md_action,
        doc_name="CLAUDE.md",
        manual_command=f"{KB_ROOT}/bin/install_claude_md.sh --yes",
    )

    briefing = _build_briefing(
        cwd, orphan_count=orphan_count, budget_line=budget_line,
        surfaced_ids=surfaced_ids,
        claude_md_synced=(claude_md_action == "synced"),
        wiring_notice=wiring_notice,
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
        _emit_session_start_context(briefing)

    return 0


def _emit_session_start_context(context: str, system_message: str | None = None) -> None:
    """Emit a SessionStart additionalContext envelope."""
    # Hook stdout becomes additionalContext for the session. JSON form is the
    # spec; if Claude Code ignores the envelope it falls back to treating stdout
    # as plain text — both yield a usable brief.
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
        "LATCH UNLATCHED MODE ACTIVE: Latch is OFF for this latch install. "
        "This is the agent without latch's project judgment layer. KB brief, prompt injection, gate "
        "guidance, compaction, self-heal, maintenance, and automatic latch "
        "writes are disabled until the user runs /unlatch and confirms latch. "
        "If LATCH_UNLATCHED is set, unset it too."
    )


def _build_unlatched_brief() -> str:
    """Static receipt for Unlatched mode; no KB reads, ranking, or model calls."""
    return "\n".join([
        "# latch is unlatched",
        "",
        "Latch is currently UNLATCHED.",
        "This is the agent without latch's project judgment layer.",
        "Scope: this latch install stays unlatched until you re-latch, even if you change repos.",
        "",
        "- Disabled: SessionStart KB brief, UserPromptSubmit KB injection, gate "
        "guidance, Stop/SessionEnd compaction, self-heal, maintenance, and "
        "automatic latch writes for this latch install.",
        "- Still true: your KB is local and unchanged, latch remains installed, "
        "/unlatch remains available, MCP registration remains present, and "
        "non-latch tools/hooks are unaffected.",
        "- Run `/unlatch` to re-latch. If `LATCH_UNLATCHED` is set, unset it too.",
        f"- Latch home: `{KB_ROOT}`.",
    ])


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
        action = claude_md_sync.sync_if_outdated(target)
        if action == "synced":
            log(f"claude_md auto-sync: re-synced managed region in {target} "
                f"(backup: {target}.latchbak)")
        return action
    except Exception as e:
        log(f"claude_md auto-sync skipped: {e}")
        return "error"


def _managed_doc_wiring_notice(
    action: str | None,
    *,
    doc_name: str,
    manual_command: str,
    restart_required: bool = False,
) -> str | None:
    if action == "synced":
        restart = " Restart or open a new task to reload host wiring." if restart_required else ""
        return (
            f"_↻ latch repaired older {doc_name} project wiring once (managed region "
            f"only; user content was preserved; backup: `{doc_name}.latchbak`).{restart}_"
        )
    if action == "newer":
        return (
            f"_⚠ {doc_name} has newer latch project wiring than this engine. "
            f"Latch did not downgrade it; update the engine or inspect with `{manual_command} --check`._"
        )
    if action in ("invalid", "error"):
        return (
            f"_⚠ latch could not safely repair {doc_name} project wiring. The session "
            f"will continue; run `{manual_command}` manually._"
        )
    return None


def _build_briefing(
    cwd: str,
    orphan_count: int = 0,
    budget_line: str | None = None,
    surfaced_ids: list[int] | None = None,
    claude_md_synced: bool = False,
    synced_doc_name: str = "CLAUDE.md",
    wiring_notice: str | None = None,
    intensity: str | None = None,
    read_only: bool = False,
    startup_write_warning: bool = False,
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
    resolved_intensity = normalize_latch_intensity(intensity) if intensity else None
    resolved_intensity = resolved_intensity or latch_intensity()
    policy = BRIEF_POLICIES[resolved_intensity]

    read_only_mode = bool(read_only)
    try:
        if read_only_mode:
            conn = db.connect_readonly(cwd)
        else:
            try:
                conn = db.connect(cwd)
            except Exception as write_open_error:
                # Rendering the brief is a read-side product surface.  A
                # readable external vault must not disappear merely because
                # this hook cannot run setup or metadata writes there.
                log(
                    "briefing writable connection failed; retrying read-only: "
                    f"{write_open_error}"
                )
                conn = db.connect_readonly(cwd)
                read_only_mode = True
        try:
            focus_rows = db.get_focus(conn, limit=policy.max_workstreams)
            # Fallback: focus table empty (fresh DB, freshly evicted, or before
            # any auto-bump activity). Recent-canonical workstreams keep the
            # brief useful instead of going silent.
            if focus_rows:
                workstreams = focus_rows
                workstreams_from_focus = True
            else:
                workstreams = db.recent_nodes(
                    conn, kind="workstream", status="canonical",
                    limit=policy.max_workstreams,
                )
                workstreams_from_focus = False
            # status=canonical on an open_question means "resolved" — drop those
            # from the brief so they stop bugging the user. Over-fetch since the
            # API only filters TO a single status; we want everything EXCEPT
            # canonical (and stale, which recent_nodes already excludes by default).
            open_question_candidates = [
                n for n in db.recent_nodes(
                    conn, kind="open_question", limit=MAX_OPEN_QUESTIONS * 3,
                    # Over-fetch so resolved canonical questions can be removed.
                )
                if n.get("status") != "canonical"
            ]
            idea_candidates = (
                db.recent_nodes(conn, kind="idea", limit=MAX_BRIEFING_IDEAS * 3)
                if policy.max_ideas else []
            )
            latest_progress = (
                db.recent_nodes(conn, kind="progress", status="canonical", limit=1)
                if policy.include_latest_progress else []
            )
            prio = priorities.list_priorities(conn) if policy.include_priorities else []
            workstream_prio: dict[int, list[dict]] = {}
            for ws in workstreams:
                wid = ws.get("workstream_id") or ws.get("id")
                if wid is None:
                    continue
                workstream_prio[int(wid)] = (
                    priorities.list_priorities(conn, workstream_id=int(wid))
                    if policy.include_priorities else []
                )
            # Open feeders per workstream (KB 2299): the declared building
            # blocks of each active goal, surfaced via intent edges and
            # membership rather than text similarity.
            workstream_feeders: dict[int, list[dict]] = {}
            for ws in workstreams:
                wid = ws.get("workstream_id") or ws.get("id")
                if wid is None:
                    continue
                rows = feeders.open_feeders(
                    conn, int(wid), limit=0,
                )
                if rows:
                    workstream_feeders[int(wid)] = rows
            workstream_feeders, open_qs, ideas = _bound_feeder_surface(
                workstream_feeders,
                open_question_candidates,
                idea_candidates,
                policy,
            )
            # New-user detection: cheap COUNT(*), same connection. Drives the
            # getting-started block below.
            show_getting_started = db.node_count(conn) < NEW_USER_NODE_THRESHOLD
            receipt_items = []
            lifecycle_suggestions = []
            if lifecycle_receipts.RECEIPTS_CHANNEL_LIVE and not read_only_mode:
                try:
                    receipt_items = lifecycle_receipts.surface_pending_items(
                        conn,
                        limit=LIFECYCLE_RECEIPT_LIMITS[resolved_intensity],
                    )
                except Exception as e:
                    log(f"lifecycle receipt surfacing failed: {e}")
                    if db.is_readonly_error(e):
                        read_only_mode = True
                suggestion_budget = max(
                    0,
                    LIFECYCLE_RECEIPT_LIMITS[resolved_intensity] - len(receipt_items),
                )
                try:
                    lifecycle_suggestions = (
                        lifecycle_receipts.surface_pending_suggestions(
                            conn,
                            limit=suggestion_budget,
                        )
                        if suggestion_budget and not read_only_mode else []
                    )
                except Exception as e:
                    log(f"lifecycle suggestion surfacing failed: {e}")
                    if db.is_readonly_error(e):
                        read_only_mode = True
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
        scoped_feeders = [
            f for rows in workstream_feeders.values() for f in rows
        ]
        for collection in (
            workstreams, open_qs, ideas, latest_progress, prio, scoped_prio,
            scoped_feeders,
        ):
            surfaced_ids.extend(n["id"] for n in collection)

    if (not workstreams and not open_qs and not ideas and not latest_progress
            and not prio and not orphan_count and not budget_line and not n_drift
            and not show_getting_started and not claude_md_synced
            and not wiring_notice and not receipt_items
            and not lifecycle_suggestions and not read_only_mode
            and not startup_write_warning):
        # Intensity bounds real startup pointers; it does not manufacture a
        # tier-advertisement banner when an established KB has nothing in the
        # brief's workstream/question/idea/progress surfaces.
        return ""

    parts = ["# latch — session brief\n"]
    if resolved_intensity == "quiet":
        parts.append(
            "_Quiet: compact startup context; correction notices stay on for "
            "supported prompt-hook hosts, and ambient prompt retrieval is off. "
            "The same gate check runs when invoked._\n"
        )
    elif resolved_intensity == "standard":
        parts.append(
            "_Standard: lean startup context; supported prompt-hook hosts surface "
            "judgment when the topic changes._\n"
        )
    else:
        parts.append(
            "_Full: Latch uses its broadest available startup and prompt-time "
            "surfacing; prompt retrieval depends on host support._\n"
        )
    if read_only_mode:
        parts.append(
            "_Latch loaded core KB context read-only. One or more write-side "
            "SessionStart updates—session registration, startup retrieval "
            "dedupe, and one-time lifecycle notices—could not run._\n"
        )
    elif startup_write_warning:
        parts.append(
            "_Latch loaded core KB context, but some SessionStart metadata "
            "could not be updated; see the Latch hook log._\n"
        )
    # New-user onboarding leads the brief (and is the one thing a brand-new,
    # otherwise-empty KB has to show). Self-removes once the KB fills.
    if show_getting_started:
        parts.append(
            _GETTING_STARTED_QUIET_BLOCK if policy.compact else _GETTING_STARTED_BLOCK
        )
    if orphan_count:
        parts.append(
            f"_{orphan_count} prior session(s) have unreviewed transcripts. "
            f"Run `/latch-compact` to summarize them on demand._\n"
        )
    if budget_line:
        parts.append(f"_{budget_line}_\n")
    if receipt_items or lifecycle_suggestions:
        parts.append("## Workstream lifecycle\n")
        for item in receipt_items:
            annotation = (
                " _(automatically opened; first surfacing)_"
                if item.get("op") == "OPEN" and item.get("origin") == "auto"
                else ""
            )
            parts.append(f"- {item['receipt']}{annotation}")
        for suggestion in lifecycle_suggestions:
            parts.append(f"- {suggestion}")
    if n_drift:
        parts.append(
            f"_⚠ {n_drift} body-edge/state drift item(s) flagged by the last "
            f"nightly sweep — run `bash bin/run_kb_drift.sh` (or read the latest "
            f"`drift-*.log`) to review and `latch_link`/fix._\n"
        )
    if wiring_notice:
        parts.append(wiring_notice + "\n")
    elif claude_md_synced:
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
            if policy.workstream_chars:
                parts.append(
                    f"  {_one_line(ws['body'], n=policy.workstream_chars)}"
                )
            wid = int(ws.get("workstream_id") or ws["id"])
            feeds = workstream_feeders.get(wid, [])
            if feeds:
                parts.append(
                    "  ↳ open feeders: " + "; ".join(
                        f"(id={f['id']}, {f['kind']}) "
                        f"{_one_line(str(f['title']), n=70)}"
                        for f in feeds
                    )
                )
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
                f"- (id={n['id']}{_by(n)}) **{n['title']}** — "
                f"{_one_line(n['body'], n=policy.idea_chars)}"
            )

    # Latest progress kept as a single one-liner pointer (not a full body dump).
    # Prevents the brief from anchoring the agent on yesterday's task.
    if latest_progress:
        s = latest_progress[0]
        parts.append(
            f"\n_Latest session note: (id={s['id']}, {s['updated_at']}{_by(s)}) "
            f"**{s['title']}** — `latch_get({s['id']})` for body._\n"
        )

    parts.append(
        "\n_Use `latch_search` / `latch_get` / `latch_recent` MCP tools to drill in. "
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
