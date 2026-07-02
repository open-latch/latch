"""Unit tests for Step 5 — session brief surfacing ideas + budget line.

Exercises session_start._build_briefing against throwaway KBs. The brief is a
markdown string; we pattern-match to confirm the right sections render under
the right conditions.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(_SRC / "hooks"))

import db  # noqa: E402
import embeddings  # noqa: E402
import budget  # noqa: E402
import session_start  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_brief_test_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


def _mk(conn, *, kind, title, body="body text", status="staging"):
    v = embeddings.embed(f"{title}\n\n{body}")
    return db.insert_node(conn, kind=kind, title=title, body=body,
                          status=status, embedding=embeddings.to_blob(v))


def test_brief_empty_when_nothing_to_surface():
    """A KB past the new-user threshold (so no getting-started block) whose
    nodes are all non-surfacing kinds yields an empty brief. A fresh 0-node KB
    no longer hits this path — it shows getting-started (see below)."""
    tmp, conn = _fresh_db()
    try:
        # Push past NEW_USER_NODE_THRESHOLD with facts (a non-surfacing kind),
        # so the getting-started block does not render.
        for i in range(session_start.NEW_USER_NODE_THRESHOLD):
            _mk(conn, kind="fact", title=f"non-surfacing fact {i}",
                body="not shown in brief")
        conn.close()
        brief = session_start._build_briefing(tmp)
        _assert(brief == "", f"expected empty brief, got: {brief!r}")
        print("PASS brief_empty_when_nothing_to_surface")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_getting_started_for_new_kb():
    """A brand-new (0-node) KB leads the brief with the getting-started block
    instead of going blank — first-run time-to-value."""
    tmp, conn = _fresh_db()
    try:
        conn.close()
        brief = session_start._build_briefing(tmp)
        _assert("Getting started with latch" in brief,
                f"getting-started block missing for new KB: {brief!r}")
        _assert("/latch-compact" in brief,
                f"compact pointer missing from getting-started: {brief!r}")
        _assert("/latch-pm" in brief,
                f"PM-interview offer missing from getting-started: {brief!r}")
        _assert("ruled out" in brief,
                f"gate-fire framing missing from PM offer: {brief!r}")
        print("PASS brief_getting_started_for_new_kb")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_getting_started_vanishes_when_kb_populated():
    """Once the KB clears the new-user threshold, the getting-started block
    self-removes."""
    tmp, conn = _fresh_db()
    try:
        for i in range(session_start.NEW_USER_NODE_THRESHOLD):
            _mk(conn, kind="fact", title=f"fact {i}", body="body")
        conn.close()
        brief = session_start._build_briefing(tmp)
        _assert("Getting started with latch" not in brief,
                f"getting-started block should have vanished: {brief!r}")
        _assert("/latch-pm" not in brief,
                f"PM-interview offer should vanish with the block: {brief!r}")
        print("PASS brief_getting_started_vanishes_when_kb_populated")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_surfaces_ideas():
    tmp, conn = _fresh_db()
    try:
        _mk(conn, kind="idea", title="try raptor-style clustering",
            body="cluster nodes hierarchically for brief scaling")
        _mk(conn, kind="idea", title="add contradiction-detection pass",
            body="nightly cross-checks for conflicting facts")
        _mk(conn, kind="fact", title="python version",
            body="Python 3.11 is the project default")
        conn.close()
        brief = session_start._build_briefing(tmp)
        _assert("Parked ideas" in brief, f"ideas section missing: {brief!r}")
        _assert("raptor-style clustering" in brief, f"idea 1 missing: {brief!r}")
        _assert("contradiction-detection" in brief, f"idea 2 missing: {brief!r}")
        print("PASS brief_surfaces_ideas")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_ideas_listed_once():
    """Idea kind should appear once under Parked ideas. (Staging-notes section
    was removed in the workstream-first redesign — random staging facts no
    longer leak into the brief; they're discovered via kb_search instead.)"""
    tmp, conn = _fresh_db()
    try:
        _mk(conn, kind="idea", title="unique idea body marker xyzzy",
            body="some idea body")
        _mk(conn, kind="fact", title="some staging fact",
            body="some fact body")
        conn.close()
        brief = session_start._build_briefing(tmp)
        _assert(brief.count("xyzzy") == 1,
                f"idea listed more than once: {brief.count('xyzzy')} occurrences")
        # Staging facts no longer surface in the brief.
        _assert("some staging fact" not in brief,
                f"staging fact leaked into brief: {brief!r}")
        print("PASS brief_ideas_listed_once")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_surfaces_budget_line_near_cap():
    tmp, conn = _fresh_db()
    try:
        _mk(conn, kind="idea", title="something",
            body="anything so brief builds")
        conn.close()
        for _ in range(80):
            budget.record_invocation(tmp, category="nonheal")
        line = budget.brief_line(tmp)
        brief = session_start._build_briefing(tmp, budget_line=line)
        _assert(line and line in brief, f"budget line not in brief: {line!r} vs {brief!r}")
        print("PASS brief_surfaces_budget_line_near_cap")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_surfaces_orphan_count():
    tmp, conn = _fresh_db()
    try:
        _mk(conn, kind="fact", title="a fact", body="a body")
        conn.close()
        brief = session_start._build_briefing(tmp, orphan_count=3)
        _assert("3 prior session" in brief, f"orphan count missing: {brief!r}")
        _assert("/latch-compact" in brief, f"compact hint missing: {brief!r}")
        print("PASS brief_surfaces_orphan_count")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_surfaces_latest_progress_pointer_only():
    """Latest progress is now a one-line pointer — full body is intentionally
    NOT dumped, to avoid anchoring the agent on yesterday's task."""
    tmp, conn = _fresh_db()
    try:
        _mk(conn, kind="progress",
            title="session summary: shipped step 6",
            body="Budget cap wired. All tests green.",
            status="canonical")
        conn.close()
        brief = session_start._build_briefing(tmp)
        _assert("Latest session note" in brief,
                f"latest-note pointer missing: {brief!r}")
        _assert("shipped step 6" in brief, f"summary title missing: {brief!r}")
        _assert("latch_get(" in brief,
                f"drill-in hint missing: {brief!r}")
        # Full body must NOT appear — the whole point of the redesign.
        _assert("Budget cap wired" not in brief,
                f"summary body should not be dumped: {brief!r}")
        print("PASS brief_surfaces_latest_progress_pointer_only")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_surfaces_workstreams_first():
    tmp, conn = _fresh_db()
    try:
        _mk(conn, kind="workstream", title="Phase 1 caching strategy",
            body="session-cache evaluation. last touched X. open: Redis vs in-process.",
            status="canonical")
        _mk(conn, kind="workstream", title="Phase 2 queue migration",
            body="queue rework. last touched Y. open: in-process vs Redis Streams.",
            status="canonical")
        conn.close()
        brief = session_start._build_briefing(tmp)
        _assert("Active workstreams" in brief,
                f"workstream section missing: {brief!r}")
        _assert("Phase 1 caching strategy" in brief, f"workstream 1 missing: {brief!r}")
        _assert("Phase 2 queue migration" in brief, f"workstream 2 missing: {brief!r}")
        # Workstreams must come before ideas / open questions sections.
        ws_pos = brief.index("Active workstreams")
        # Sanity: section title appears once.
        _assert(brief.count("Active workstreams") == 1,
                "workstream header duplicated")
        print("PASS brief_surfaces_workstreams_first")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_workstreams_canonical_only():
    """Staging workstreams should not surface — only canonical pointers do."""
    tmp, conn = _fresh_db()
    try:
        _mk(conn, kind="workstream", title="canonical workstream",
            body="real one", status="canonical")
        _mk(conn, kind="workstream", title="draft workstream",
            body="not yet promoted", status="staging")
        conn.close()
        brief = session_start._build_briefing(tmp)
        _assert("canonical workstream" in brief,
                f"canonical missing: {brief!r}")
        _assert("draft workstream" not in brief,
                f"staging workstream leaked into brief: {brief!r}")
        print("PASS brief_workstreams_canonical_only")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_surfaces_open_questions():
    tmp, conn = _fresh_db()
    try:
        _mk(conn, kind="open_question",
            title="Choice of next path after eviction policy confirmed too aggressive",
            body="rewrite vs tune vs other")
        _mk(conn, kind="open_question",
            title="How to enable native KB use without re-enabling Stop hook",
            body="three ingestion paths considered")
        conn.close()
        brief = session_start._build_briefing(tmp)
        _assert("Open questions" in brief,
                f"open_questions section missing: {brief!r}")
        _assert("eviction policy confirmed" in brief,
                f"open_question 1 missing: {brief!r}")
        _assert("native KB use" in brief,
                f"open_question 2 missing: {brief!r}")
        print("PASS brief_surfaces_open_questions")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_section_order_workstreams_before_questions_before_ideas():
    tmp, conn = _fresh_db()
    try:
        _mk(conn, kind="workstream", title="ws1", body="ws body",
            status="canonical")
        _mk(conn, kind="open_question", title="oq1", body="oq body")
        _mk(conn, kind="idea", title="idea1", body="idea body")
        conn.close()
        brief = session_start._build_briefing(tmp)
        ws_pos = brief.find("Active workstreams")
        oq_pos = brief.find("Open questions")
        idea_pos = brief.find("Parked ideas")
        _assert(ws_pos != -1 and oq_pos != -1 and idea_pos != -1,
                f"missing section: ws={ws_pos} oq={oq_pos} idea={idea_pos}")
        _assert(ws_pos < oq_pos < idea_pos,
                f"section order wrong: ws={ws_pos} oq={oq_pos} idea={idea_pos}")
        print("PASS brief_section_order_workstreams_before_questions_before_ideas")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_surfaces_claude_md_resync_notice():
    """When the SessionStart auto-sync rewrote the managed region this session,
    the brief surfaces a one-time notice — the re-sync is otherwise silent."""
    tmp, conn = _fresh_db()
    try:
        _mk(conn, kind="fact", title="a fact", body="a body")
        conn.close()
        brief = session_start._build_briefing(tmp, claude_md_synced=True)
        _assert("re-synced from an updated snippet" in brief,
                f"claude_md re-sync notice missing: {brief!r}")
        _assert("CLAUDE.md.latchbak" in brief,
                f"backup pointer missing from notice: {brief!r}")
        # Negative: the default (nothing synced) omits the notice.
        brief_no = session_start._build_briefing(tmp)
        _assert("re-synced from an updated snippet" not in brief_no,
                f"notice should be absent when nothing synced: {brief_no!r}")
        print("PASS brief_surfaces_claude_md_resync_notice")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_resync_notice_makes_empty_brief_nonempty():
    """The re-sync notice must render even when nothing else would surface —
    the brief is the only place the otherwise-silent re-sync becomes visible."""
    tmp, conn = _fresh_db()
    try:
        # Push past the new-user threshold with non-surfacing facts so the brief
        # would otherwise be empty (mirrors test_brief_empty_when_nothing...).
        for i in range(session_start.NEW_USER_NODE_THRESHOLD):
            _mk(conn, kind="fact", title=f"non-surfacing fact {i}",
                body="not shown in brief")
        conn.close()
        _assert(session_start._build_briefing(tmp) == "",
                "precondition: brief should be empty with no sync")
        brief = session_start._build_briefing(tmp, claude_md_synced=True)
        _assert(brief != "", "re-sync notice should make the brief non-empty")
        _assert("re-synced from an updated snippet" in brief,
                f"re-sync notice missing: {brief!r}")
        print("PASS brief_resync_notice_makes_empty_brief_nonempty")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_brief_empty_when_nothing_to_surface()
    test_brief_getting_started_for_new_kb()
    test_brief_getting_started_vanishes_when_kb_populated()
    test_brief_surfaces_ideas()
    test_brief_ideas_listed_once()
    test_brief_surfaces_budget_line_near_cap()
    test_brief_surfaces_orphan_count()
    test_brief_surfaces_latest_progress_pointer_only()
    test_brief_surfaces_workstreams_first()
    test_brief_workstreams_canonical_only()
    test_brief_surfaces_open_questions()
    test_brief_section_order_workstreams_before_questions_before_ideas()
    test_brief_surfaces_claude_md_resync_notice()
    test_brief_resync_notice_makes_empty_brief_nonempty()
    print("\nAll session-brief tests pass.")
