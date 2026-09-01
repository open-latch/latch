"""Acceptance tests for the deterministic A2 predicate skeleton.

All rejected-path data in this module is synthetic.  In particular, no option,
reason, or scope-predicate text was copied from a Latch vault.
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"


def _predicate_module():
    sys.path.insert(0, str(_SRC))
    try:
        return importlib.import_module("latch.gate.predicate")
    finally:
        sys.path.remove(str(_SRC))


def _row(scope_predicate: str | None, row_id: int = 1) -> dict[str, object]:
    return {
        "id": row_id,
        "node_id": 700 + row_id,
        "option": f"synthetic rejected option {row_id}",
        "scope_predicate": scope_predicate,
        "reason": f"synthetic rejection reason {row_id}",
        "source": "declared",
    }


@pytest.mark.parametrize(
    ("scope_predicate", "expected_prefix", "is_compilable"),
    [
        ("file:src/example.py", "file", True),
        ("glob:src/**/*.py", "glob", True),
        ("package:synthetic.widgets", "package", True),
        ("import:synthetic.widgets.client", "import", True),
        ("api:SyntheticClient.create", "api", True),
        ("feature:synthetic-dashboard", "feature", False),
        ("positioning", "positioning", False),
        ("process", "process", False),
        ("distribution", "distribution", False),
        ("roadmap", "roadmap", False),
        ("architecture", "architecture", False),
        ("unknown:synthetic-value", "unknown", False),
        ("", None, False),
        (None, None, False),
    ],
)
def test_all_prefixes_compile_or_declare_uncompilable(
    scope_predicate, expected_prefix, is_compilable
):
    predicate = _predicate_module()

    check = predicate.compile_predicate(_row(scope_predicate))

    assert check.prefix == expected_prefix
    assert check.predicate == scope_predicate
    assert check.compilable is is_compilable
    if is_compilable:
        assert isinstance(check, predicate.CompiledCheck)
        assert check.value
    else:
        assert isinstance(check, predicate.UncompilableCheck)
        assert check.uncompilable_reason
        assert check.matches(predicate.ToolCallContext()) is False


def test_zero_llm_imports():
    code = """
import json
import sys

sys.path.insert(0, sys.argv[1])
from latch.gate import predicate

banned_roots = {
    "aiohttp", "anthropic", "budget", "httpx", "model_backends", "openai",
    "requests", "socket", "urllib", "urllib3",
}
loaded = sorted(
    name for name in sys.modules
    if (name.rsplit(".", 1)[-1] if name.startswith("latch.") else name.split(".", 1)[0]) in banned_roots
)
print(json.dumps(loaded))
raise SystemExit(bool(loaded))
"""
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code, str(_SRC)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []


def _assert_verdict_shape(verdict: dict[str, object]) -> None:
    assert set(verdict) == {"engine", "decision", "llm_calls", "matches"}
    assert verdict["engine"] == "predicate-v1"
    assert verdict["decision"] in {"block", "flag", "pass"}
    assert verdict["llm_calls"] == 0
    assert isinstance(verdict["matches"], list)
    for match in verdict["matches"]:
        assert set(match) == {
            "rejected_path_id",
            "node_id",
            "option",
            "predicate",
            "reason",
            "source",
        }


def test_verdict_shape_and_matching():
    predicate = _predicate_module()
    file_row = _row("file:src/synthetic_widget.py", row_id=41)
    unsupported_row = _row("feature:synthetic-dashboard", row_id=42)
    checks = predicate.compile_predicates([file_row, unsupported_row])

    matching = predicate.evaluate(
        checks,
        predicate.ToolCallContext(file_paths=("src/synthetic_widget.py",)),
    )
    _assert_verdict_shape(matching)
    assert matching == {
        "engine": "predicate-v1",
        "decision": "block",
        "llm_calls": 0,
        "matches": [
            {
                "rejected_path_id": 41,
                "node_id": 741,
                "option": "synthetic rejected option 41",
                "predicate": "file:src/synthetic_widget.py",
                "reason": "synthetic rejection reason 41",
                "source": "declared",
            }
        ],
    }

    non_matching = predicate.evaluate(
        checks,
        predicate.ToolCallContext(file_paths=("src/unrelated.py",)),
    )
    _assert_verdict_shape(non_matching)
    assert non_matching["decision"] == "pass"
    assert non_matching["matches"] == []

    unsupported_only = predicate.evaluate(
        [predicate.compile_predicate(unsupported_row)],
        predicate.ToolCallContext(command_text="synthetic-dashboard"),
    )
    _assert_verdict_shape(unsupported_only)
    assert unsupported_only["decision"] == "pass"
    assert unsupported_only["matches"] == []


def test_file_directory_matches_absolute_path_by_components():
    predicate = _predicate_module()
    check = predicate.compile_predicate(_row("file:src", row_id=73))

    verdict = predicate.evaluate(
        [check],
        predicate.ToolCallContext(file_paths=("/repo/src/pkg/mod.py",)),
    )

    assert verdict == {
        "engine": "predicate-v1",
        "decision": "block",
        "llm_calls": 0,
        "matches": [
            {
                "rejected_path_id": 73,
                "node_id": 773,
                "option": "synthetic rejected option 73",
                "predicate": "file:src",
                "reason": "synthetic rejection reason 73",
                "source": "declared",
            }
        ],
    }

    component_false_positive = predicate.evaluate(
        [check],
        predicate.ToolCallContext(file_paths=("/repo/srcish/pkg/mod.py",)),
    )
    assert component_false_positive["decision"] == "pass"
    assert component_false_positive["matches"] == []
