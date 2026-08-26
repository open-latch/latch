"""Unit tests for the budget gate.

Covers two-category split (nonheal=100/day, heal default 33/day, env-overridable):
  * initial state, record_invocation, single and batch atomic admission
  * approve_today resets BOTH counters and unlocks both
  * date rollover, corrupt-JSON fallback
  * legacy state migration: `{count}` -> `{count_nonheal}` on first load
  * category isolation: exhausting one category does not block the other
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filelock import Timeout as FileLockTimeout


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


def _assert_raises_oserror(call, message):
    try:
        call()
    except OSError:
        return
    except Exception as exc:
        raise AssertionError(
            f"{message}: expected OSError-compatible failure, got {type(exc).__name__}"
        ) from exc
    raise AssertionError(f"{message}: expected OSError-compatible failure")


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


def test_filelock_timeout_is_oserror_compatible():
    _assert(
        issubclass(FileLockTimeout, OSError),
        f"seed sanitization requires OSError-compatible lock timeouts: "
        f"{FileLockTimeout.__mro__}",
    )
    print("PASS filelock_timeout_is_oserror_compatible")


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


def test_check_and_record_is_atomic_across_concurrent_callers():
    tmp = _tmp_project()
    original_save = budget._save_state

    def delayed_save(project_path, state):
        # Widen the read/write race deterministically. A correct lock covers
        # both the load and this save, so only one cap=1 caller can proceed.
        time.sleep(0.05)
        original_save(project_path, state)

    budget._save_state = delayed_save
    try:
        workers = 8
        ready = threading.Barrier(workers)

        def attempt():
            ready.wait(timeout=5)
            return budget.check_and_record(tmp, category="nonheal", cap=1)[0]

        with ThreadPoolExecutor(max_workers=workers) as pool:
            allowed = list(pool.map(lambda _index: attempt(), range(workers)))

        _assert(sum(allowed) == 1, f"cap=1 must allow exactly one caller: {allowed}")
        state = budget.status(tmp, nonheal_cap=1)
        _assert(state["nonheal"]["count"] == 1, state)
        print("PASS check_and_record_is_atomic_across_concurrent_callers")
    finally:
        budget._save_state = original_save
        _cleanup(tmp)


def test_reserve_available_grants_only_the_live_capacity():
    tmp = _tmp_project()
    try:
        cap = 5
        for _ in range(2):
            budget.check_and_record(tmp, category="heal", cap=cap)
        granted, state = budget.reserve_available(
            tmp, category="heal", requested=8, cap=cap,
        )
        _assert(granted == 3 and state["count_heal"] == cap,
                f"batch grant must stop exactly at cap: {(granted, state)}")
        granted, state = budget.reserve_available(
            tmp, category="heal", requested=1, cap=cap,
        )
        _assert(granted == 0 and state["count_heal"] == cap,
                f"exhausted batch must not bump the counter: {(granted, state)}")
        released, state = budget.release_reserved(
            tmp, category="heal", count=2, expected_date=state["date"],
        )
        _assert(released == 2 and state["count_heal"] == 3,
                f"unused reservations must return to capacity: {(released, state)}")
        print("PASS reserve_available_grants_only_the_live_capacity")
    finally:
        _cleanup(tmp)


def test_reserve_available_is_atomic_across_concurrent_callers():
    tmp = _tmp_project()
    original_save = budget._save_state

    def delayed_save(project_path, state):
        time.sleep(0.05)
        original_save(project_path, state)

    budget._save_state = delayed_save
    try:
        ready = threading.Barrier(2)

        def reserve():
            ready.wait(timeout=5)
            return budget.reserve_available(
                tmp, category="heal", requested=3, cap=3,
            )[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            grants = list(pool.map(lambda _index: reserve(), range(2)))

        _assert(sorted(grants) == [0, 3],
                f"concurrent batches must not over-grant: {grants}")
        _assert(budget.status(tmp, heal_cap=3)["heal"]["count"] == 3,
                "atomic batch grant must persist exactly the cap")
        print("PASS reserve_available_is_atomic_across_concurrent_callers")
    finally:
        budget._save_state = original_save
        _cleanup(tmp)


def test_release_reserved_does_not_cross_utc_rollover():
    tmp = _tmp_project()
    original_today = budget._today_iso
    day = {"value": "2026-08-25"}
    try:
        budget._today_iso = lambda: day["value"]
        granted, state = budget.reserve_available(
            tmp, category="heal", requested=2, cap=2,
        )
        _assert(granted == 2 and state["date"] == day["value"], state)

        old_date = state["date"]
        day["value"] = "2026-08-26"
        allowed, _ = budget.check_and_record(tmp, category="heal", cap=2)
        _assert(allowed, "new-day control invocation should be charged")
        released, state = budget.release_reserved(
            tmp, category="heal", count=2, expected_date=old_date,
        )
        _assert(released == 0 and state["date"] == day["value"],
                f"old-day slots must not decrement the new-day count: {state}")
        _assert(state["count_heal"] == 1,
                f"old-day release subtracted a new-day invocation: {state}")
        print("PASS release_reserved_does_not_cross_utc_rollover")
    finally:
        budget._today_iso = original_today
        _cleanup(tmp)


def test_release_reserved_does_not_cross_approval_reset():
    tmp = _tmp_project()
    try:
        granted, state = budget.reserve_available(
            tmp, category="heal", requested=2, cap=2,
        )
        _assert(granted == 2, state)
        reserved_date = state["date"]

        budget.approve_today(tmp)
        budget.record_invocation(tmp, category="heal")
        released, state = budget.release_reserved(
            tmp,
            category="heal",
            count=2,
            expected_date=reserved_date,
            expected_approved=False,
        )
        _assert(released == 0 and state["count_heal"] == 1,
                f"old reservation erased post-approval usage: {state}")
        print("PASS release_reserved_does_not_cross_approval_reset")
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
        budget.record_invocation(tmp, category="heal")
        s2 = budget.approve_today(tmp)
        _assert(s2["approved_dates"] == s1["approved_dates"],
                f"approved_dates duplicated: {s2['approved_dates']}")
        _assert(s2["count_heal"] == 1,
                f"repeated approval must not erase later usage: {s2}")
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


def test_corrupt_json_fails_closed_for_budget_consumers():
    tmp = _tmp_project()
    try:
        state_path = paths.project_dir(tmp) / "budget.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{{not json", encoding="utf-8")

        calls = (
            ("under_cap", lambda: budget.under_cap(tmp, cap=1)),
            (
                "check_and_record",
                lambda: budget.check_and_record(tmp, category="nonheal", cap=1),
            ),
            (
                "reserve_available",
                lambda: budget.reserve_available(
                    tmp, category="nonheal", requested=1, cap=1,
                ),
            ),
            (
                "record_invocation",
                lambda: budget.record_invocation(tmp, category="nonheal"),
            ),
            ("approve_today", lambda: budget.approve_today(tmp)),
        )
        for name, call in calls:
            _assert_raises_oserror(call, name)
            _assert(
                state_path.read_text(encoding="utf-8") == "{{not json",
                f"{name} must not overwrite corrupt state",
            )
        print("PASS corrupt_json_fails_closed_for_budget_consumers")
    finally:
        _cleanup(tmp)


def test_prepare_storage_detects_corrupt_state_before_consent():
    tmp = _tmp_project()
    try:
        state_path = paths.project_dir(tmp) / "budget.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{{not json", encoding="utf-8")

        _assert_raises_oserror(
            lambda: budget.prepare_storage(tmp),
            "prepare_storage",
        )
        _assert(
            state_path.read_text(encoding="utf-8") == "{{not json",
            "prepare_storage must not overwrite corrupt state",
        )
        _assert(
            not list(state_path.parent.glob(".latch-budget-write-probe-*")),
            "corrupt state should fail before the write probe",
        )
        print("PASS prepare_storage_detects_corrupt_state_before_consent")
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


if __name__ == "__main__":
    test_initial_state_is_empty()
    test_filelock_timeout_is_oserror_compatible()
    test_record_invocation_increments_per_category()
    test_check_and_record_gates_at_cap_per_category()
    test_check_and_record_is_atomic_across_concurrent_callers()
    test_reserve_available_grants_only_the_live_capacity()
    test_reserve_available_is_atomic_across_concurrent_callers()
    test_release_reserved_does_not_cross_utc_rollover()
    test_release_reserved_does_not_cross_approval_reset()
    test_categories_are_independent()
    test_approve_today_resets_both_and_unlocks()
    test_approve_today_is_idempotent()
    test_date_rollover_resets_both_counts()
    test_date_rollover_preserves_past_approvals()
    test_corrupt_json_falls_back_to_empty()
    test_corrupt_json_fails_closed_for_budget_consumers()
    test_prepare_storage_detects_corrupt_state_before_consent()
    test_legacy_count_field_migrates_to_nonheal()
    print("\nAll budget tests pass.")


def test_unreadable_budget_state_degrades_gate_without_spend(tmp_path, monkeypatch):
    """A corrupt budget store must route the gate to its designed no-spend
    degrade path (skipped verdict), not crash the tool surface."""
    import gate
    import paths

    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(paths, "is_disabled", lambda: False)
    monkeypatch.setattr(paths, "is_in_compact", lambda: False)

    def broken(*args, **kwargs):
        raise budget.BudgetStateError("budget state at /tmp/x is unreadable")

    monkeypatch.setattr(budget, "check_and_record", broken)

    def forbidden(*args, **kwargs):
        raise AssertionError("classifier must not spend on unreadable budget state")

    monkeypatch.setattr(gate, "_invoke_classifier_backend_once", forbidden)
    verdict = gate.classify_gate(
        {"chains": []}, project_path=str(tmp_path), backend="claude",
    )
    assert verdict.get("skipped") is True
    assert "budget state unavailable" in (verdict.get("error") or "")


def test_unreadable_budget_state_degrades_compaction_without_spend(
    tmp_path, monkeypatch,
):
    import compactor
    import paths

    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(paths, "is_disabled", lambda: False)
    monkeypatch.setattr(paths, "is_in_compact", lambda: False)

    def broken(*args, **kwargs):
        raise budget.BudgetStateError("budget state at /tmp/x is unreadable")

    monkeypatch.setattr(budget, "check_and_record", broken)
    result = compactor.run_compaction("sid", str(tmp_path), None)
    assert result == {
        "ok": False,
        "reason": "budget_state_error",
        "session_id": "sid",
    }
