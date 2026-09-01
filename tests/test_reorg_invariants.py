"""Invariant tests for the flat-src to ``latch`` package migration."""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
TOOLS = ROOT / "tools" / "reorg"

sys.path.insert(0, str(SRC))

from latch.hosts import codex_hooks  # noqa: E402
from latch.hosts import cursor_gate_state  # noqa: E402
from latch.hosts import cursor_hooks  # noqa: E402
from latch.install import install_engine  # noqa: E402
from latch.install import versioning  # noqa: E402
from latch.mcp import mcp_broker  # noqa: E402
from latch.store import paths  # noqa: E402


def _mapped_entrypoints() -> list[Path]:
    mapping = {}
    for line in (TOOLS / "module_map.tsv").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        old, new = line.split("\t")
        mapping[old] = new
    entrypoints = []
    for line in (TOOLS / "entrypoints.txt").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        destination = ROOT / mapping[line]
        if destination.is_file():
            entrypoints.append(destination)
    return entrypoints


def test_literal_entrypoint_guards_load_in_isolation(tmp_path: Path):
    for name in ("VERSION", "KB_SCHEMA_VERSION", "WIRING_VERSION"):
        shutil.copy2(ROOT / name, tmp_path / name)

    program = r"""
import importlib
import importlib.util
from pathlib import Path
import sys

module_path = Path(sys.argv[1]).resolve()
source_root = str(Path(sys.argv[2]).resolve())
before = [str(Path(value).resolve()) for value in sys.path if value]
spec = importlib.util.spec_from_file_location("_latch_path_exec_" + module_path.stem, module_path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
importlib.import_module("latch")
after = [str(Path(value).resolve()) for value in sys.path if value]
before_src = [value for value in before if Path(value).name == "src"]
after_src = [value for value in after if Path(value).name == "src"]
assert after_src == [source_root, *before_src], (module_path, before_src, after_src)
"""
    env = os.environ.copy()
    env.update({
        "LATCH_HOME": str(tmp_path),
        "CLAUDE_KB_HOME": str(tmp_path),
        "LATCH_KB_DIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    env.pop("PYTHONPATH", None)

    failures = []
    for entrypoint in _mapped_entrypoints():
        result = subprocess.run(
            [sys.executable, "-c", program, str(entrypoint), str(SRC)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            failures.append(
                f"{entrypoint.relative_to(ROOT)}: {result.stderr or result.stdout}"
            )
    assert not failures, "\n".join(failures)


def test_paths_default_root_is_repository_root(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LATCH_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_KB_HOME", raising=False)
    assert paths._default_kb_root() == ROOT

    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("LATCH_HOME", None)
    env.pop("CLAUDE_KB_HOME", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from latch.store import paths; print(paths.KB_ROOT)",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert result.stdout.strip() == str(ROOT)


def test_schema_path_preserves_latch_home_override(tmp_path: Path):
    env = os.environ.copy()
    env["LATCH_HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(SRC)
    env.pop("CLAUDE_KB_HOME", None)
    program = (
        "from latch.store import paths; "
        "print(paths.KB_ROOT); print(paths.SCHEMA_PATH)"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert result.stdout.splitlines() == [
        str(tmp_path),
        str(tmp_path / "src" / "latch" / "store" / "schema.sql"),
    ]


def test_versioning_root_is_repository_root():
    assert versioning.ROOT == ROOT


def test_mcp_runtime_content_files_all_exist():
    missing = [path for path in mcp_broker._runtime_content_files() if not path.is_file()]
    assert missing == []


def _workflow_path_filters(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    values = []
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)paths:\s*$", line)
        if not match:
            continue
        indent = len(match.group(1))
        for candidate in lines[index + 1:]:
            if not candidate.strip():
                continue
            candidate_indent = len(candidate) - len(candidate.lstrip())
            if candidate_indent <= indent:
                break
            item = candidate.strip()
            if item.startswith("- "):
                values.append(json.loads(item[2:]))
    return values


_WORKFLOW_TRACKED_REFERENCE_RE = re.compile(
    r"""(?<![A-Za-z0-9_.-])(?P<reference>
        (?:
            (?:\.github|benchmarks|bin|commands|cursor_commands|cursor_skills|
               docs|runbooks|scripts|src|tests|proof|vendor)/
            [A-Za-z0-9_.*?/\[\]-]+\.(?:py|sh|ps1|json|ya?ml|md|txt|lock)
          |
            (?:requirements(?:-[A-Za-z0-9_-]+)?\.(?:txt|lock)|pyproject\.toml)
        )
        (?:::[A-Za-z0-9_.*?/\[\]-]+)?
    )""",
    re.VERBOSE,
)


def _workflow_tracked_references(path: Path) -> set[str]:
    return {
        match.group("reference")
        for match in _WORKFLOW_TRACKED_REFERENCE_RE.finditer(
            path.read_text(encoding="utf-8")
        )
    }


def _listed_test_nodes(path: Path) -> set[str]:
    return {
        line
        for raw in path.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    }


def test_workflow_path_filters_match_tracked_files():
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    known_nodes = _listed_test_nodes(TOOLS / "baseline_test_node_ids.txt")
    known_nodes |= _listed_test_nodes(TOOLS / "new_test_node_ids.txt")
    unmatched = {}
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        references = set(_workflow_path_filters(workflow))
        references |= _workflow_tracked_references(workflow)
        missing = []
        for reference in sorted(references):
            file_pattern, separator, _node = reference.partition("::")
            if not any(
                fnmatch.fnmatchcase(path, file_pattern) for path in tracked
            ):
                missing.append(reference)
            elif separator and reference not in known_nodes:
                missing.append(reference)
        if missing:
            unmatched[str(workflow.relative_to(ROOT))] = missing
    assert unmatched == {}


@pytest.mark.parametrize(
    "marker",
    ["/src/latch/hooks/", "/src/hooks/"],
    ids=["new", "legacy"],
)
def test_hook_ownership_recognizers_accept_both_layouts(marker: str):
    command = f"/python /repo{marker}codex_session_start.py"
    entry = {"hooks": [{"command": command}]}
    assert install_engine._is_latch_hook_entry(entry)
    assert codex_hooks._is_owned_command(command)
    cursor_command = f"/python /repo{marker}cursor_session_start.py"
    assert cursor_hooks._is_owned({"command": cursor_command})


@pytest.mark.parametrize(
    "relative",
    [
        "src/latch/gate/budget.py",
        "src/budget.py",
        "src/latch/pipeline/maintenance.py",
        "src/maintenance.py",
    ],
    ids=["new-budget", "legacy-budget", "new-maintenance", "legacy-maintenance"],
)
def test_command_ownership_recognizer_accepts_both_layouts(relative: str):
    assert install_engine._is_latch_command_body(f"python /repo/{relative}")

    command_pattern = (
        r"/src/(budget|maintenance|latch/gate/budget|latch/pipeline/maintenance)\.py"
    )
    assert re.search(command_pattern, f"/repo/{relative}")
    for wrapper in ("install_commands.sh", "install_commands.ps1"):
        body = (ROOT / "bin" / wrapper).read_text(encoding="utf-8")
        assert command_pattern in body

    doctor_body = (SRC / "latch" / "install" / "doctor.py").read_text(
        encoding="utf-8"
    )
    assert f'"/{relative}",' in doctor_body


@pytest.mark.parametrize(
    ("name", "relative", "verb"),
    [
        ("latch-budget-approve", "src/latch/gate/budget.py", "approve"),
        ("latch-budget-approve", "src/budget.py", "approve"),
        ("latch-heal", "src/latch/pipeline/maintenance.py", "nightly"),
        ("latch-heal", "src/maintenance.py", "nightly"),
    ],
    ids=["new-budget", "legacy-budget", "new-maintenance", "legacy-maintenance"],
)
def test_cursor_gate_accepts_both_managed_script_layouts(
    tmp_path: Path, name: str, relative: str, verb: str,
):
    command = f"{sys.executable} {paths.KB_ROOT / relative} {verb} {tmp_path}"
    payload = {
        "tool_name": "Shell",
        "tool_input": {"command": command, "cwd": str(tmp_path)},
    }
    operation = {"name": name, "phase": "run"}
    assert cursor_gate_state._operation_tool_matches(
        operation, payload, str(tmp_path)
    )
