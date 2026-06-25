"""Unit tests for the budget gate.

Covers two-category split (nonheal=100/day, heal default 33/day, env-overridable):
  * initial state, record_invocation per category, check_and_record gating
  * approve_today resets BOTH counters and unlocks both
  * date rollover, corrupt-JSON fallback
  * brief_line surfacing logic (quiet, near-cap, at-cap, approved)
  * legacy state migration: `{count}` -> `{count_nonheal}` on first load
  * category isolation: exhausting one category does not block the other
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _utc_date_iso(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=offset_days)).isoformat()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import budget  # noqa: E402
import paths  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp_project():
    return tempfile.mkdtemp(prefix="kb_budget_test_")


def _cleanup(tmp):
    shutil.rmtree(tmp, ignore_errors=True)


def test_initial_state_is_empty():
    tmp = _tmp_project()
    try:
        s = budget.status(tmp)
        _assert(s["nonheal"]["count"] == 0, s)
        _assert(s["heal"]["count"] == 0, s)
        _assert(s["nonheal"]["remaining"] == budget.DEFAULT_NONHEAL_DAILY_CAP, s)
        _assert(s["heal"]["remaining"] == budget.DEFAULT_HEAL_DAILY_CAP, s)
        _assert(s["approved_today"] is False, s)
        print("PASS initial_state_is_empty")
    finally:
        _cleanup(tmp)


def test_record_invocation_increments_per_category():
    tmp = _tmp_project()
    try:
        _assert(budget.record_invocation(tmp, category="nonheal") == 1, "nonheal #1 -> 1")
        _assert(budget.record_invocation(tmp, category="nonheal") == 2, "nonheal #2 -> 2")
        _assert(budget.record_invocation(tmp, category="heal") == 1, "heal #1 -> 1 (independent)")
        raw = json.loads((paths.project_dir(tmp) / "budget.json").read_text(encoding="utf-8"))
        _assert(raw["count_nonheal"] == 2 and raw["count_heal"] == 1, raw)
        _assert("count" not in raw, f"legacy `count` field leaked back: {raw}")
        print("PASS record_invocation_increments_per_category")
    finally:
        _cleanup(tmp)


def test_check_and_record_gates_at_cap_per_category():
    tmp = _tmp_project()
    try:
        cap = 5
        for i in range(cap):
            allowed, _ = budget.check_and_record(tmp, category="nonheal", cap=cap)
            _assert(allowed, f"nonheal under cap ({i+1}/{cap}) should be allowed")
        allowed, state = budget.check_and_record(tmp, category="nonheal", cap=cap)
        _assert(allowed is False, "nonheal over cap should be denied")
        _assert(state["count_nonheal"] == cap, f"counter stays at cap on denial: {state}")
        print("PASS check_and_record_gates_at_cap_per_category")
    finally:
        _cleanup(tmp)


def test_categories_are_independent():
    """Exhausting one category must NOT block the other — the whole point of the split."""
    tmp = _tmp_project()
    try:
        nonheal_cap = 3
        heal_cap = 2
        for _ in range(nonheal_cap):
            budget.check_and_record(tmp, category="nonheal", cap=nonheal_cap)
        allowed, _ = budget.check_and_record(tmp, category="nonheal", cap=nonheal_cap)
        _assert(allowed is False, "nonheal at cap")
        # heal should still be wide open
        for i in range(heal_cap):
            allowed, _ = budget.check_and_record(tmp, category="heal", cap=heal_cap)
            _assert(allowed, f"heal {i+1}/{heal_cap} should pass despite nonheal at cap")
        # heal at its own cap
        allowed, _ = budget.check_and_record(tmp, category="heal", cap=heal_cap)
        _assert(allowed is False, "heal at cap")
        print("PASS categories_are_independent")
    finally:
        _cleanup(tmp)


def test_approve_today_resets_both_and_unlocks():
    tmp = _tmp_project()
    try:
        cap = 3
        for _ in range(cap):
            budget.check_and_record(tmp, category="nonheal", cap=cap)
            budget.check_and_record(tmp, category="heal", cap=cap)
        # both blocked
        _assert(budget.check_and_record(tmp, category="nonheal", cap=cap)[0] is False, "nonheal cap")
        _assert(budget.check_and_record(tmp, category="heal", cap=cap)[0] is False, "heal cap")

        budget.approve_today(tmp)
        s = budget.status(tmp, nonheal_cap=cap, heal_cap=cap)
        _assert(s["nonheal"]["count"] == 0, f"nonheal reset: {s}")
        _assert(s["heal"]["count"] == 0, f"heal reset: {s}")
        _assert(s["approved_today"] is True, s)
        _assert(s["nonheal"]["remaining"] is None and s["heal"]["remaining"] is None,
                f"approved means no remaining cap: {s}")
        # Further calls all allowed in both buckets
        for _ in range(20):
            _assert(budget.check_and_record(tmp, category="nonheal", cap=cap)[0],
                    "approved day allows all nonheal calls")
            _assert(budget.check_and_record(tmp, category="heal", cap=cap)[0],
                    "approved day allows all heal calls")
        print("PASS approve_today_resets_both_and_unlocks")
    finally:
        _cleanup(tmp)


def test_approve_today_is_idempotent():
    tmp = _tmp_project()
    try:
        s1 = budget.approve_today(tmp)
        s2 = budget.approve_today(tmp)
        _assert(s2["approved_dates"] == s1["approved_dates"],
                f"approved_dates duplicated: {s2['approved_dates']}")
        print("PASS approve_today_is_idempotent")
    finally:
        _cleanup(tmp)


def test_date_rollover_resets_both_counts():
    tmp = _tmp_project()
    try:
        state_path = paths.project_dir(tmp) / "budget.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        yesterday = _utc_date_iso(-1)
        state_path.write_text(
            json.dumps({
                "date": yesterday, "count_nonheal": 999, "count_heal": 999,
                "approved_dates": [],
            }),
            encoding="utf-8",
        )
        s = budget.status(tmp)
        _assert(s["nonheal"]["count"] == 0, f"stale date should reset nonheal: {s}")
        _assert(s["heal"]["count"] == 0, f"stale date should reset heal: {s}")
        _assert(s["date"] != yesterday, f"date should roll forward: {s}")
        print("PASS date_rollover_resets_both_counts")
    finally:
        _cleanup(tmp)


def test_date_rollover_preserves_past_approvals():
    tmp = _tmp_project()
    try:
        state_path = paths.project_dir(tmp) / "budget.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        yesterday = _utc_date_iso(-1)
        state_path.write_text(
            json.dumps({
                "date": yesterday, "count_nonheal": 5, "count_heal": 3,
                "approved_dates": [yesterday],
            }),
            encoding="utf-8",
        )
        s = budget.status(tmp)
        _assert(s["approved_today"] is False,
                f"yesterday's approval should not unlock today: {s}")
        _assert(s["nonheal"]["count"] == 0, f"nonheal reset: {s}")
        _assert(s["heal"]["count"] == 0, f"heal reset: {s}")
        print("PASS date_rollover_preserves_past_approvals")
    finally:
        _cleanup(tmp)


def test_corrupt_json_falls_back_to_empty():
    tmp = _tmp_project()
    try:
        state_path = paths.project_dir(tmp) / "budget.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{{not json", encoding="utf-8")
        s = budget.status(tmp)
        _assert(s["nonheal"]["count"] == 0, f"corrupt file should fall back: {s}")
        _assert(s["heal"]["count"] == 0, f"corrupt file should fall back: {s}")
        _assert(s["approved_today"] is False, s)
        print("PASS corrupt_json_falls_back_to_empty")
    finally:
        _cleanup(tmp)


def test_legacy_count_field_migrates_to_nonheal():
    """Pre-split state had `{date, count, approved_dates}`. First load should
    migrate the old `count` into `count_nonheal` and seed `count_heal=0`,
    then drop the legacy field on next write."""
    tmp = _tmp_project()
    try:
        state_path = paths.project_dir(tmp) / "budget.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        today = _utc_date_iso()
        state_path.write_text(
            json.dumps({"date": today, "count": 27, "approved_dates": []}),
            encoding="utf-8",
        )
        s = budget.status(tmp)
        _assert(s["nonheal"]["count"] == 27, f"legacy count -> count_nonheal: {s}")
        _assert(s["heal"]["count"] == 0, f"heal seeds to 0: {s}")
        # Trigger a write so the legacy field gets dropped on disk
        budget.record_invocation(tmp, category="heal")
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        _assert("count" not in raw, f"legacy `count` field not dropped: {raw}")
        _assert(raw["count_nonheal"] == 27, raw)
        _assert(raw["count_heal"] == 1, raw)
        print("PASS legacy_count_field_migrates_to_nonheal")
    finally:
        _cleanup(tmp)


def test_brief_line_quiet_when_both_below_threshold():
    tmp = _tmp_project()
    try:
        line = budget.brief_line(tmp)
        _assert(line is None, f"empty should be quiet: {line!r}")
        for _ in range(60):
            budget.record_invocation(tmp, category="nonheal")
        for _ in range(30):
            budget.record_invocation(tmp, category="heal")
        # 60/100 nonheal = 60%, 30/50 heal = 60% — both below 75%. Pin caps so
        # the test exercises the threshold logic independent of the default.
        line = budget.brief_line(tmp, nonheal_cap=100, heal_cap=50)
        _assert(line is None, f"both below 75% should be quiet: {line!r}")
        print("PASS brief_line_quiet_when_both_below_threshold")
    finally:
        _cleanup(tmp)


def test_brief_line_surfaces_only_loud_category():
    """Non-heal near cap, heal quiet — only non-heal surfaces."""
    tmp = _tmp_project()
    try:
        for _ in range(80):
            budget.record_invocation(tmp, category="nonheal")
        for _ in range(5):
            budget.record_invocation(tmp, category="heal")
        line = budget.brief_line(tmp, nonheal_cap=100, heal_cap=50)
        _assert(line is not None, "expected non-None line")
        _assert("80/100 non-heal" in line, f"non-heal not surfaced: {line!r}")
        _assert("heal" not in line.replace("non-heal", ""),
                f"heal leaked into line when it should be quiet: {line!r}")
        print("PASS brief_line_surfaces_only_loud_category")
    finally:
        _cleanup(tmp)


def test_brief_line_surfaces_both_when_both_near_cap():
    tmp = _tmp_project()
    try:
        for _ in range(80):
            budget.record_invocation(tmp, category="nonheal")
        for _ in range(40):
            budget.record_invocation(tmp, category="heal")
        line = budget.brief_line(tmp, nonheal_cap=100, heal_cap=50)
        _assert(line is not None, "expected non-None line")
        _assert("80/100 non-heal" in line and "40/50 heal" in line,
                f"both categories should surface: {line!r}")
        print("PASS brief_line_surfaces_both_when_both_near_cap")
    finally:
        _cleanup(tmp)


def test_brief_line_at_cap_shows_approve_hint():
    tmp = _tmp_project()
    try:
        for _ in range(100):
            budget.record_invocation(tmp, category="nonheal")
        line = budget.brief_line(tmp)
        _assert(line is not None and "/kb-budget-approve" in line,
                f"expected unlock hint: {line!r}")
        _assert("100/100 non-heal" in line, f"expected at-cap count: {line!r}")
        print("PASS brief_line_at_cap_shows_approve_hint")
    finally:
        _cleanup(tmp)


def test_brief_line_at_cap_heal_only_shows_approve_hint():
    """Heal at cap on its own (the today=2026-05-20 scenario) should also
    surface the unlock hint."""
    tmp = _tmp_project()
    try:
        for _ in range(50):
            budget.record_invocation(tmp, category="heal")
        line = budget.brief_line(tmp, heal_cap=50)
        _assert(line is not None and "/kb-budget-approve" in line,
                f"expected unlock hint when heal alone is at cap: {line!r}")
        _assert("50/50 heal" in line, f"expected at-cap heal count: {line!r}")
        print("PASS brief_line_at_cap_heal_only_shows_approve_hint")
    finally:
        _cleanup(tmp)


def test_brief_line_surfaces_approved_state():
    tmp = _tmp_project()
    try:
        budget.approve_today(tmp)
        budget.record_invocation(tmp, category="nonheal")
        budget.record_invocation(tmp, category="nonheal")
        budget.record_invocation(tmp, category="heal")
        line = budget.brief_line(tmp)
        _assert(line is not None and "approved" in line.lower(),
                f"expected approved line: {line!r}")
        _assert("non-heal 2" in line and "heal 1" in line,
                f"expected per-category counts: {line!r}")
        print("PASS brief_line_surfaces_approved_state")
    finally:
        _cleanup(tmp)


if __name__ == "__main__":
    test_initial_state_is_empty()
    test_record_invocation_increments_per_category()
    test_check_and_record_gates_at_cap_per_category()
    test_categories_are_independent()
    test_approve_today_resets_both_and_unlocks()
    test_approve_today_is_idempotent()
    test_date_rollover_resets_both_counts()
    test_date_rollover_preserves_past_approvals()
    test_corrupt_json_falls_back_to_empty()
    test_legacy_count_field_migrates_to_nonheal()
    test_brief_line_quiet_when_both_below_threshold()
    test_brief_line_surfaces_only_loud_category()
    test_brief_line_surfaces_both_when_both_near_cap()
    test_brief_line_at_cap_shows_approve_hint()
    test_brief_line_at_cap_heal_only_shows_approve_hint()
    test_brief_line_surfaces_approved_state()
    print("\nAll budget tests pass.")
