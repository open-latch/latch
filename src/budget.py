"""Daily budget caps on model invocations the KB tooling makes on its own.

Two independent counters, both per-project, both reset at UTC rollover:

  * `nonheal` (default 100/day) — covers compactor, latch_gate, tree
    summarization, and on-insert heal arbitration (insert_with_heal).
    "Generous" cap so normal coding-shaped work isn't gated; the cap exists
    so a runaway fan-out is still bounded.
  * `heal`    (default 33/day, was 50; override via CLAUDE_KB_HEAL_CAP) —
    nightly heal LLM arbitration only. The nightly pass walks every near-dup
    pair (two-tier: 0.50-0.85) and the fan-out is large; this is the original
    blast-radius cap from the 2026-04-23 fan-out incident. Lowered to ~2/3 so
    new installs don't surprise users with background LLM spend.

Single `budget.json` per project: `{date, count_nonheal, count_heal,
approved_dates}`. Lazy UTC date rollover. `/latch-budget-approve` resets BOTH
counters to 0 and adds today to `approved_dates` (idempotent, one switch).

Legacy migration: pre-split state had `{date, count, approved_dates}`. On
first load post-split, `count` is interpreted as `count_nonheal` and
`count_heal` seeds to 0 — the dominant historical use of the single counter
was non-heal traffic, and the legacy field is dropped on the next write.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Literal, Mapping

if TYPE_CHECKING:
    from filelock import FileLock

sys.path.insert(0, str(Path(__file__).parent))

import paths  # noqa: E402
import project_config  # noqa: E402


DEFAULT_NONHEAL_DAILY_CAP = 100
# Default heal cap is ~2/3 of the original 50 so a fresh install's first-run
# LLM spend stays modest (new users may not expect background heal cost).
# Override per environment with CLAUDE_KB_HEAL_CAP (e.g. set 50 to keep the
# original cap). The override is read once at import; the detached selfheal
# child inherits it from the MCP server env.
DEFAULT_HEAL_DAILY_CAP = int(os.environ.get("CLAUDE_KB_HEAL_CAP") or 33)
BUDGET_LOCK_TIMEOUT_S = 10.0

Category = Literal["nonheal", "heal"]
_CATEGORIES: tuple[Category, ...] = ("nonheal", "heal")


class BudgetStateError(OSError):
    """Existing budget state could not be trusted for a spending decision."""


class BudgetCliBindingError(RuntimeError):
    """Standalone CLI could not prove authority over the selected project KB."""


def _count_field(category: Category) -> str:
    return f"count_{category}"


def _default_cap(category: Category) -> int:
    return DEFAULT_NONHEAL_DAILY_CAP if category == "nonheal" else DEFAULT_HEAL_DAILY_CAP


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _state_path(project_path: str | None) -> Path:
    return paths.project_dir(project_path) / "budget.json"


def _state_lock(project_path: str | None) -> FileLock:
    # Read-only budget status does not need this dependency. Mutation paths
    # acquire the lock under the managed runtime.
    from filelock import FileLock

    state_path = _state_path(project_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(
        str(state_path.with_name(f"{state_path.name}.lock")),
        timeout=BUDGET_LOCK_TIMEOUT_S,
    )


def _empty_state() -> dict:
    return {
        "date": _today_iso(),
        "count_nonheal": 0,
        "count_heal": 0,
        "approved_dates": [],
    }


def _normalize_state(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("budget state must be a JSON object")

    # Legacy migration: pre-split `count` -> `count_nonheal`.
    if "count" in data and "count_nonheal" not in data:
        data["count_nonheal"] = int(data.pop("count") or 0)
        data.setdefault("count_heal", 0)
    else:
        data.pop("count", None)

    required = ("date", "count_nonheal", "count_heal", "approved_dates")
    if any(field not in data for field in required):
        raise ValueError("budget state is missing required fields")

    state_date = data["date"]
    if not isinstance(state_date, str):
        raise ValueError("budget date must be a string")
    try:
        parsed_date = date.fromisoformat(state_date)
    except ValueError as exc:
        raise ValueError("budget date must be ISO formatted") from exc
    if parsed_date.isoformat() != state_date:
        raise ValueError("budget date must use canonical ISO format")

    for field in ("count_nonheal", "count_heal"):
        value = data[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")

    approved_dates = data["approved_dates"]
    if not isinstance(approved_dates, list) or any(
        not isinstance(value, str) for value in approved_dates
    ):
        raise ValueError("approved_dates must be a list of strings")

    today = _today_iso()
    # Lazy rollover: stale `date` means it's a new day — reset both counters,
    # keep approvals.
    if state_date != today:
        data["date"] = today
        data["count_nonheal"] = 0
        data["count_heal"] = 0
    return data


def _load_state(
    project_path: str | None,
    *,
    fail_closed: bool = False,
) -> dict:
    p = _state_path(project_path)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_state()
    except (OSError, UnicodeError) as exc:
        if fail_closed:
            raise BudgetStateError(f"budget state at {p} is unreadable") from exc
        return _empty_state()
    try:
        return _normalize_state(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        if fail_closed:
            raise BudgetStateError(f"budget state at {p} is invalid") from exc
        return _empty_state()


def _save_state(project_path: str | None, state: dict) -> None:
    p = _state_path(project_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def prepare_storage(project_path: str | None) -> None:
    """Validate existing state and write-probe storage without spending budget."""
    with _state_lock(project_path):
        _load_state(project_path, fail_closed=True)
        parent = _state_path(project_path).parent
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".latch-budget-write-probe-",
            dir=parent,
        ) as probe:
            probe.write("{}")
            probe.flush()


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
    state = _load_state(project_path, fail_closed=True)
    if _today_iso() in state["approved_dates"]:
        return True
    if cap is None:
        cap = _default_cap(category)
    return state[_count_field(category)] < cap


def remaining_nonheal(project_path: str | None) -> int | None:
    """Calls left today in the non-heal category, or None when unlimited.

    None means today is approved (``/latch-budget-approve``), so the daily
    budget cannot be the binding constraint. Callers surfacing coverage need
    this to tell the user which cap actually truncates their run.
    """
    return status(project_path)["nonheal"]["remaining"]


def record_invocation(
    project_path: str | None,
    *,
    category: Category = "nonheal",
) -> int:
    """Increment today's count for `category` and persist. Returns post-increment count.
    Call this exactly once per model-backed attempt in the matching category."""
    with _state_lock(project_path):
        state = _load_state(project_path, fail_closed=True)
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
    """Atomically check and increment one category across callers.

    Returns (allowed, state_snapshot). If allowed=False, the counter is not
    bumped.
    """
    with _state_lock(project_path):
        state = _load_state(project_path, fail_closed=True)
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
    with _state_lock(project_path):
        state = _load_state(project_path, fail_closed=True)
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


def _cli_agent_context(env: Mapping[str, str]) -> bool:
    return project_config.is_agent_context(env)


def _cli_session_id(
    explicit: str | None,
    env: Mapping[str, str],
) -> str | None:
    ambient = project_config.current_agent_session_id(env)
    if explicit is not None and not explicit.strip():
        raise BudgetCliBindingError("--session-id requires a value")
    requested = explicit.strip() if explicit and explicit.strip() else None
    if requested is not None and ambient is not None and requested != ambient:
        raise BudgetCliBindingError(
            "--session-id does not match this agent task's session"
        )
    return requested or ambient


@contextmanager
def _cli_project_access(
    project_path: str | None,
    *,
    session_id: str | None,
    env: Mapping[str, str],
) -> Iterator[str]:
    """Validate one CLI task and lease its exact project target."""
    import lockfile  # CLI-only dependency

    try:
        project = str(project_config.project_root(project_path))
        with lockfile.project_access_lock(project):
            sid = _cli_session_id(session_id, env)
            target = project_config.resolve(project)
            if _cli_agent_context(env) and sid is None:
                raise BudgetCliBindingError(
                    "Latch cannot verify this agent task's project KB; "
                    "pass the current session with --session-id or start a fresh task"
                )
            if sid is not None:
                if project_config.current_session_revision(
                    project, sid,
                ) != target.revision:
                    raise BudgetCliBindingError(
                        "this agent task belongs to an older or different project KB; "
                        "start a fresh task before using latch-budget-approve"
                    )
            yield project
    except BudgetCliBindingError:
        raise
    except lockfile.ProjectTargetChangedError as exc:
        if exc.reason == "unlatched":
            raise BudgetCliBindingError(
                "Latch is Unlatched for this project; run /latch before using the budget command"
            ) from exc
        raise BudgetCliBindingError(
            "the project's Latch KB changed; rerun the budget command from a fresh task"
        ) from exc
    except project_config.ProjectConfigError as exc:
        raise BudgetCliBindingError(str(exc)) from exc


def _run_cli_command(
    subcommand: str,
    project_path: str | None,
    *,
    session_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict:
    values = os.environ if env is None else env
    with _cli_project_access(
        project_path,
        session_id=session_id,
        env=values,
    ) as project:
        if subcommand == "status":
            return status(project)
        if subcommand == "approve":
            return approve_today(project)
    raise ValueError(f"unknown subcommand {subcommand!r} -- use status|approve")


def main(argv: list[str] | None = None) -> int:
    # python budget.py <subcommand> [project_path] [--session-id ID]
    parser = argparse.ArgumentParser(
        description="Show or approve Latch's daily budget."
    )
    parser.add_argument(
        "subcommand",
        nargs="?",
        choices=("status", "approve"),
        default="status",
    )
    parser.add_argument("project_path", nargs="?")
    parser.add_argument("--session-id")
    args = parser.parse_args(argv)
    try:
        result = _run_cli_command(
            args.subcommand,
            args.project_path,
            session_id=args.session_id,
        )
    except BudgetCliBindingError as exc:
        print(f"latch-budget-{args.subcommand}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
