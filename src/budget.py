"""Daily budget caps on model invocations the KB tooling makes on its own.

Two independent counters, both per-project, both reset at UTC rollover:

  * `nonheal` (default 100/day) — covers compactor, kb_gate, tree
    summarization, and on-insert heal arbitration (insert_with_heal).
    "Generous" cap so normal coding-shaped work isn't gated; the cap exists
    so a runaway fan-out is still bounded.
  * `heal`    (default 33/day, was 50; override via CLAUDE_KB_HEAL_CAP) —
    nightly heal LLM arbitration only. The nightly pass walks every near-dup
    pair (two-tier: 0.50-0.85) and the fan-out is large; this is the original
    blast-radius cap from the 2026-04-23 fan-out incident. Lowered to ~2/3 so
    new installs don't surprise users with background LLM spend.

Single `budget.json` per project: `{date, count_nonheal, count_heal,
approved_dates}`. Lazy UTC date rollover. `/kb-budget-approve` resets BOTH
counters to 0 and adds today to `approved_dates` (idempotent, one switch).

Legacy migration: pre-split state had `{date, count, approved_dates}`. On
first load post-split, `count` is interpreted as `count_nonheal` and
`count_heal` seeds to 0 — the dominant historical use of the single counter
was non-heal traffic, and the legacy field is dropped on the next write.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).parent))

import paths  # noqa: E402


DEFAULT_NONHEAL_DAILY_CAP = 100
# Default heal cap is ~2/3 of the original 50 so a fresh install's first-run
# LLM spend stays modest (new users may not expect background heal cost).
# Override per environment with CLAUDE_KB_HEAL_CAP (e.g. set 50 to keep the
# original cap). The override is read once at import; the detached selfheal
# child inherits it from the MCP server env.
DEFAULT_HEAL_DAILY_CAP = int(os.environ.get("CLAUDE_KB_HEAL_CAP") or 33)

Category = Literal["nonheal", "heal"]
_CATEGORIES: tuple[Category, ...] = ("nonheal", "heal")


def _count_field(category: Category) -> str:
    return f"count_{category}"


def _default_cap(category: Category) -> int:
    return DEFAULT_NONHEAL_DAILY_CAP if category == "nonheal" else DEFAULT_HEAL_DAILY_CAP


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _state_path(project_path: str | None) -> Path:
    return paths.project_dir(project_path) / "budget.json"


def _empty_state() -> dict:
    return {
        "date": _today_iso(),
        "count_nonheal": 0,
        "count_heal": 0,
        "approved_dates": [],
    }


def _load_state(project_path: str | None) -> dict:
    p = _state_path(project_path)
    if not p.exists():
        return _empty_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    today = _today_iso()
    # Legacy migration: pre-split `count` -> `count_nonheal`. Done before the
    # rollover check so the migration also fires on a stale-dated legacy file.
    if "count" in data and "count_nonheal" not in data:
        data["count_nonheal"] = int(data.pop("count") or 0)
        data.setdefault("count_heal", 0)
    # Lazy rollover: stale `date` means it's a new day — reset both counters,
    # keep approvals.
    if data.get("date") != today:
        data["date"] = today
        data["count_nonheal"] = 0
        data["count_heal"] = 0
    data.setdefault("count_nonheal", 0)
    data.setdefault("count_heal", 0)
    data.setdefault("approved_dates", [])
    # Drop legacy `count` if it's still around alongside the new fields.
    data.pop("count", None)
    return data


def _save_state(project_path: str | None, state: dict) -> None:
    p = _state_path(project_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def is_approved_today(project_path: str | None) -> bool:
    state = _load_state(project_path)
    return _today_iso() in state["approved_dates"]


def under_cap(
    project_path: str | None,
    *,
    category: Category = "nonheal",
    cap: int | None = None,
) -> bool:
    """Read-only check: would the next invocation in this category be allowed?"""
    state = _load_state(project_path)
    if _today_iso() in state["approved_dates"]:
        return True
    if cap is None:
        cap = _default_cap(category)
    return state[_count_field(category)] < cap


def record_invocation(
    project_path: str | None,
    *,
    category: Category = "nonheal",
) -> int:
    """Increment today's count for `category` and persist. Returns post-increment count.
    Call this exactly once per model-backed attempt in the matching category."""
    state = _load_state(project_path)
    field = _count_field(category)
    state[field] = state.get(field, 0) + 1
    _save_state(project_path, state)
    return state[field]


def check_and_record(
    project_path: str | None,
    *,
    category: Category = "nonheal",
    cap: int | None = None,
) -> tuple[bool, dict]:
    """Atomic (relative to holder of the project lock) check-and-increment for
    a category. Returns (allowed, state_snapshot). If allowed=False, the
    counter is NOT bumped."""
    state = _load_state(project_path)
    approved = _today_iso() in state["approved_dates"]
    field = _count_field(category)
    if cap is None:
        cap = _default_cap(category)
    if not approved and state.get(field, 0) >= cap:
        return False, state
    state[field] = state.get(field, 0) + 1
    _save_state(project_path, state)
    return True, state


def approve_today(project_path: str | None) -> dict:
    """Add today to the approved list and reset BOTH counters to 0. Idempotent.
    Approving mid-day when either cap is spent immediately unlocks all further
    work for the rest of the UTC day."""
    state = _load_state(project_path)
    today = _today_iso()
    if today not in state["approved_dates"]:
        state["approved_dates"].append(today)
    state["count_nonheal"] = 0
    state["count_heal"] = 0
    _save_state(project_path, state)
    return state


def status(
    project_path: str | None,
    *,
    nonheal_cap: int = DEFAULT_NONHEAL_DAILY_CAP,
    heal_cap: int = DEFAULT_HEAL_DAILY_CAP,
) -> dict:
    state = _load_state(project_path)
    today = _today_iso()
    approved = today in state["approved_dates"]
    out: dict = {
        "date": state["date"],
        "approved_today": approved,
    }
    for category, cap in (("nonheal", nonheal_cap), ("heal", heal_cap)):
        count = state.get(_count_field(category), 0)
        out[category] = {
            "count": count,
            "cap": cap,
            "remaining": None if approved else max(0, cap - count),
        }
    return out


def brief_line(
    project_path: str | None,
    *,
    nonheal_cap: int = DEFAULT_NONHEAL_DAILY_CAP,
    heal_cap: int = DEFAULT_HEAL_DAILY_CAP,
) -> str | None:
    """One-line session-brief summary. Quiet when both categories are comfortably
    under cap. Surfaces whichever category(ies) are >=75% or at cap; if both
    are loud, both are surfaced in one line."""
    s = status(project_path, nonheal_cap=nonheal_cap, heal_cap=heal_cap)
    if s["approved_today"]:
        return (
            f"Budget: manually approved for today "
            f"(non-heal {s['nonheal']['count']}, heal {s['heal']['count']})."
        )

    def _is_at_cap(c: dict) -> bool:
        return c["remaining"] == 0

    def _is_near_cap(c: dict, cap: int) -> bool:
        return c["count"] >= int(cap * 0.75)

    nh_at = _is_at_cap(s["nonheal"])
    h_at = _is_at_cap(s["heal"])
    nh_near = _is_near_cap(s["nonheal"], nonheal_cap)
    h_near = _is_near_cap(s["heal"], heal_cap)

    if not (nh_near or h_near):
        return None

    parts: list[str] = []
    if nh_near:
        parts.append(f"{s['nonheal']['count']}/{nonheal_cap} non-heal")
    if h_near:
        parts.append(f"{s['heal']['count']}/{heal_cap} heal")
    body = ", ".join(parts)

    if nh_at or h_at:
        return (
            f"Budget: {body} used today — auto-compact is paused. "
            f"Run `/kb-budget-approve` to unlock the rest of today."
        )
    return f"Budget: {body} used today."


if __name__ == "__main__":
    # python budget.py <subcommand> [project_path]
    argv = sys.argv[1:]
    sub = argv[0] if argv else "status"
    project = argv[1] if len(argv) > 1 else None
    if sub == "status":
        print(json.dumps(status(project), indent=2))
    elif sub == "approve":
        print(json.dumps(approve_today(project), indent=2))
    else:
        print(f"unknown subcommand {sub!r} — use status|approve", file=sys.stderr)
        sys.exit(2)
