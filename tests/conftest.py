"""pytest session setup: bind the suite to a disposable authenticated KB root.

**pytest is the supported test runner for latch.** This conftest creates a
capability-bound temporary root before any test module loads.  Every KB path,
including paths resolved by child processes, stays under that root.

Directly-executed test scripts are refused unless they explicitly load the same
bootstrap before importing runtime modules.
"""
import sys
from pathlib import Path

# tests/ on sys.path so the shared _isolation shim imports under pytest too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _isolation  # noqa: E402  (import side-effect: isolates configuration)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "authority: M3 authority test case (a human ruling outranks newer or "
        "nearer agent text); expected failures name the item that flips them",
    )


def pytest_terminal_summary(terminalreporter):
    """Print the scoreboard's authority count whenever authority cases ran."""
    precedence = ("passed", "xfailed", "flipped", "failed")
    outcomes = {}
    for category in ("passed", "xfailed", "xpassed", "failed", "error"):
        for report in terminalreporter.stats.get(category, ()):
            if "authority" not in getattr(report, "keywords", {}):
                continue
            outcome = category
            if category == "xpassed" or str(
                getattr(report, "longrepr", "")
            ).startswith("[XPASS(strict)]"):
                # A strict expected failure that passes is reported as failed:
                # the fix landed and its PR has to remove the marker.
                outcome = "flipped"
            elif category == "error":
                outcome = "failed"
            # One outcome per test; a teardown error outranks a passed call.
            previous = outcomes.get(report.nodeid)
            if previous is None or precedence.index(outcome) > precedence.index(previous):
                outcomes[report.nodeid] = outcome
    if not outcomes:
        return
    counts = {name: list(outcomes.values()).count(name) for name in precedence}
    line = (
        f"authority test {counts['passed']} of {len(outcomes)} "
        f"({counts['xfailed']} expected to fail"
    )
    if counts["flipped"]:
        line += f"; {counts['flipped']} now pass, remove their markers"
    if counts["failed"]:
        line += f"; {counts['failed']} failed"
    terminalreporter.write_line(line + ")")
