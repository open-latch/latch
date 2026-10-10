#!/usr/bin/env python3
"""Apply the deterministic flat-src to ``src/latch`` package migration.

The transformation is intentionally mechanical and idempotent.  It moves only
paths named in ``module_map.tsv``, rewrites imports with LibCST, installs the
uniform literal-path entrypoint guard, and applies occurrence-counted literal
patches from ``literal_patches.json``.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import textwrap
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import libcst as cst
except ImportError as exc:  # pragma: no cover - exercised by invocation smoke
    raise SystemExit(
        "LibCST is required only for this codemod. Create an isolated tool "
        "environment and install tools/reorg/requirements.txt."
    ) from exc


TOOL_DIR = Path(__file__).resolve().parent
ROOT = TOOL_DIR.parents[1]
MAP_FILE = TOOL_DIR / "module_map.tsv"
OPTIONAL_FILE = TOOL_DIR / "optional_sources.txt"
ENTRYPOINT_FILE = TOOL_DIR / "entrypoints.txt"
PATCH_FILE = TOOL_DIR / "literal_patches.json"
BOOTSTRAP_FILE = TOOL_DIR / "bootstrap_counts.tsv"
BASELINE_NODES_FILE = TOOL_DIR / "baseline_test_node_ids.txt"
NEW_NODES_FILE = TOOL_DIR / "new_test_node_ids.txt"

PACKAGE_DIRS = (
    "latch",
    "latch/store",
    "latch/retrieval",
    "latch/gate",
    "latch/pipeline",
    "latch/mcp",
    "latch/hosts",
    "latch/install",
    "latch/evals",
    "latch/proof",
    "latch/common",
    "latch/hooks",
)

GUARD = (
    'if __package__ in (None, ""):\n'
    "    import sys, pathlib\n"
    "    sys.path.insert(0, str(next(p for p in pathlib.Path(__file__).resolve().parents if p.name == \"src\")))\n"
)

ORPHAN_BOOTSTRAP_DESTINATIONS = {
    Path("src/latch/hooks/codex_session_start.py"),
    Path("src/latch/hooks/cursor_before_submit.py"),
    Path("src/latch/hooks/cursor_post_tool_use.py"),
    Path("src/latch/hooks/cursor_pre_tool_use.py"),
    Path("src/latch/hooks/cursor_session_start.py"),
    Path("src/latch/hooks/vscode_session_start.py"),
}

ROOT_ANCHOR_ALLOWLIST = {
    ("src/latch/hosts/codex_session.py", "marker_root = path.parents[1]"),
    (
        "src/latch/hosts/codex_session.py",
        "and _owned_path(path.parents[1], directory=True)",
    ),
    ("src/latch/install/install_codex.py", "parent = parent.parent"),
    ("src/latch/store/vault_backup.py", "root = snapshot_dir.parents[1]"),
}

DYNAMIC_IMPORT_ALLOWLIST = {
    (
        "src/latch/store/workstream_automation.py",
        'return importlib.import_module("latch.store.workstreams")',
    ),
    (
        "src/latch/install/doctor.py",
        '"        importlib.import_module(m)\\n"',
    ),
    (
        "tests/test_reorg_invariants.py",
        'importlib.import_module("latch")',
    ),
    (
        "tests/test_predicate_compiler.py",
        'return importlib.import_module("latch.gate.predicate")',
    ),
    (
        "tests/test_predicate_contract.py",
        'return importlib.import_module(f"latch.gate.{name}")',
    ),
    (
        "tests/test_predicate_coverage.py",
        'return importlib.import_module("latch.gate.predicate_coverage")',
    ),
    (
        "tests/test_predicate_semantics.py",
        'return importlib.import_module("latch.gate.predicate")',
    ),
    (
        "tests/test_predicate_snapshot.py",
        'return importlib.import_module(f"latch.gate.{name}")',
    ),
}

LEGACY_LAYOUT_ALLOWANCES = (
    (
        ".agents/skills/source-command-*/SKILL.md",
        '[ -f "$candidate/src/mcp_server.py" ]',
        8,
    ),
    (
        ".agents/skills/source-command-*/SKILL.md",
        '[ ! -f "$latch_home/src/mcp_server.py" ]',
        7,
    ),
    (
        ".agents/skills/source-command-*/SKILL.md",
        'maintenance_script="$latch_home/src/maintenance.py"',
        3,
    ),
    (
        ".agents/skills/source-command-*/SKILL.md",
        'budget_script="$latch_home/src/budget.py"',
        2,
    ),
    (
        ".github/scripts/agent_contract_footprint.py",
        'read_text, "src/latch/hosts/managed_doc_sync.py", "src/managed_doc_sync.py"',
        1,
    ),
    (
        ".github/scripts/agent_contract_footprint.py",
        'read_text, "src/latch/hosts/claude_md_sync.py", "src/claude_md_sync.py"',
        1,
    ),
    (
        ".github/scripts/agent_contract_footprint.py",
        'read_text, "src/latch/hosts/agents_md_sync.py", "src/agents_md_sync.py"',
        1,
    ),
    (
        "src/latch/install/install_engine.py",
        'LEGACY_LATCH_HOOK_MARKER = "/src/hooks/"',
        1,
    ),
    ("src/latch/install/install_engine.py", '    "/src/budget.py",', 1),
    ("src/latch/install/install_engine.py", '    "/src/maintenance.py",', 1),
    ("src/latch/install/doctor.py", '        "/src/budget.py",', 1),
    ("src/latch/install/doctor.py", '        "/src/maintenance.py",', 1),
    (
        "src/latch/hosts/codex_hooks.py",
        'for marker in (f"/src/latch/hooks/{name}", f"/src/hooks/{name}")',
        1,
    ),
    (
        "src/latch/hosts/cursor_hooks.py",
        'for marker in (f"/src/latch/hooks/{name}", f"/src/hooks/{name}")',
        1,
    ),
    (
        "src/latch/hosts/codex_wiring.py",
        'for marker in (f"/src/latch/hooks/{name}", f"/src/hooks/{name}")',
        1,
    ),
    ("src/latch/hosts/codex_wiring.py", 'ROOT / "src" / name,', 1),
    (
        "src/latch/hosts/cursor_gate_state.py",
        '_trusted_script(script, "src/budget.py")',
        1,
    ),
    (
        "src/latch/hosts/cursor_gate_state.py",
        '_trusted_script(script, "src/maintenance.py")',
        1,
    ),
    ("src/latch/install/uninstall_engine.py", "``/src/hooks/``", 1),
    (
        "bin/install_commands.sh",
        r"/src/(budget|maintenance|latch/gate/budget|latch/pipeline/maintenance)\.py",
        1,
    ),
    (
        "bin/install_commands.ps1",
        r"/src/(budget|maintenance|latch/gate/budget|latch/pipeline/maintenance)\.py",
        1,
    ),
)

CANONICAL_DYNAMIC_LAYOUT_ALLOWANCES = (
    (
        "src/latch/install/quickstart.py",
        'return str(KB_HOME / "src" / _SOURCE_ENTRYPOINTS[name])',
        1,
    ),
    ("src/latch/store/paths.py", "<KB_ROOT>/src/", 1),
)


@dataclass(frozen=True)
class Move:
    old: Path
    new: Path


class ReorgError(RuntimeError):
    pass


def _data_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def load_moves() -> list[Move]:
    moves: list[Move] = []
    for line in _data_lines(MAP_FILE):
        parts = line.split("\t")
        if len(parts) != 2:
            raise ReorgError(f"bad module_map.tsv row: {line!r}")
        old, new = (Path(part) for part in parts)
        moves.append(Move(old, new))

    old_paths = [move.old for move in moves]
    new_paths = [move.new for move in moves]
    if len(set(old_paths)) != len(old_paths):
        raise ReorgError("module_map.tsv contains duplicate source paths")
    if len(set(new_paths)) != len(new_paths):
        raise ReorgError("module_map.tsv contains duplicate destinations")
    for move in moves:
        if move.old.name != move.new.name:
            raise ReorgError(f"Rule 0 basename violation: {move.old} -> {move.new}")
        if not str(move.old).startswith("src/") or not str(move.new).startswith("src/latch/"):
            raise ReorgError(f"path outside the authorized source layout: {move}")
    return moves


def load_optional() -> set[Path]:
    return {Path(line) for line in _data_lines(OPTIONAL_FILE)}


def load_entrypoints() -> set[Path]:
    return {Path(line) for line in _data_lines(ENTRYPOINT_FILE)}


def load_bootstrap_counts() -> dict[Path, int]:
    counts: dict[Path, int] = {}
    for line in _data_lines(BOOTSTRAP_FILE):
        parts = line.split("\t")
        if len(parts) != 2:
            raise ReorgError(f"bad bootstrap_counts.tsv row: {line!r}")
        path = Path(parts[0])
        try:
            count = int(parts[1])
        except ValueError as exc:
            raise ReorgError(f"bad bootstrap count: {line!r}") from exc
        if count <= 0 or path in counts:
            raise ReorgError(f"invalid bootstrap count row: {line!r}")
        counts[path] = count
    return counts


def source_payloads() -> set[Path]:
    source = ROOT / "src"
    if not source.exists():
        return set()
    return {
        path.relative_to(ROOT)
        for path in source.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix in {".py", ".sql"}
    }


def validate_layout_closure(moves: list[Move], *, after: bool) -> None:
    old_paths = {move.old for move in moves}
    new_paths = {move.new for move in moves}
    generated_inits = {
        Path("src") / relative / "__init__.py" for relative in PACKAGE_DIRS
    }
    allowed = (new_paths if after else old_paths | new_paths) | generated_inits
    unexpected = source_payloads() - allowed
    if unexpected:
        stage = "post-reorg" if after else "pre-reorg"
        raise ReorgError(
            f"{stage} src payloads missing from module_map.tsv: {sorted(unexpected)}"
        )


def validate_data(
    moves: list[Move],
    optional: set[Path],
    entrypoints: set[Path],
    bootstrap_counts: dict[Path, int],
) -> None:
    sources = {move.old for move in moves}
    unknown_optional = optional - sources
    unknown_entrypoints = entrypoints - sources
    if unknown_optional:
        raise ReorgError(f"optional sources missing from map: {sorted(unknown_optional)}")
    if unknown_entrypoints:
        raise ReorgError(f"entrypoints missing from map: {sorted(unknown_entrypoints)}")
    unknown_bootstraps = set(bootstrap_counts) - sources
    if unknown_bootstraps:
        raise ReorgError(
            f"bootstrap count paths missing from map: {sorted(unknown_bootstraps)}"
        )

    required = sources - optional
    for old in sorted(required):
        move = next(item for item in moves if item.old == old)
        if not (ROOT / old).exists() and not (ROOT / move.new).exists():
            raise ReorgError(f"required mapped file is absent at both paths: {old}")
    validate_layout_closure(moves, after=False)


def run_git(*args: str) -> None:
    subprocess.run(["git", *args], cwd=ROOT, check=True)


def move_modules(moves: list[Move], optional: set[Path], *, check: bool) -> list[str]:
    changes: list[str] = []
    for move in moves:
        old = ROOT / move.old
        new = ROOT / move.new
        if old.exists() and new.exists():
            raise ReorgError(f"move collision: both {move.old} and {move.new} exist")
        if old.exists():
            changes.append(f"move {move.old} -> {move.new}")
            if not check:
                new.parent.mkdir(parents=True, exist_ok=True)
                run_git("mv", str(move.old), str(move.new))
        elif new.exists():
            continue
        elif move.old not in optional:
            raise ReorgError(f"required source disappeared: {move.old}")
    return changes


def _package_doc(relative_dir: str) -> str:
    label = relative_dir.replace("/", ".")
    return f'"""{label} package."""\n'


def ensure_packages(*, check: bool) -> list[str]:
    changes: list[str] = []
    for relative_dir in PACKAGE_DIRS:
        init = ROOT / "src" / relative_dir / "__init__.py"
        expected = _package_doc(relative_dir)
        if init.exists():
            actual = init.read_text(encoding="utf-8")
            if actual != expected:
                raise ReorgError(f"package initializer is not docstring-only: {init.relative_to(ROOT)}")
            continue
        changes.append(f"create {init.relative_to(ROOT)}")
        if not check:
            init.parent.mkdir(parents=True, exist_ok=True)
            init.write_text(expected, encoding="utf-8")
    return changes


def _dotted_name(node: cst.BaseExpression | None) -> str | None:
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        left = _dotted_name(node.value)
        if left is not None:
            return f"{left}.{node.attr.value}"
    return None


def _dotted_expr(name: str) -> cst.BaseExpression:
    expression = cst.parse_expression(name)
    if not isinstance(expression, (cst.Name, cst.Attribute)):
        raise ReorgError(f"invalid module path: {name}")
    return expression


def import_mapping(moves: list[Move]) -> dict[str, str]:
    mapping: dict[str, str] = {"hooks": "latch.hooks"}
    leaf_destinations: dict[str, str] = {}
    for move in moves:
        if move.old.suffix != ".py":
            continue
        old_dotted = str(move.old.with_suffix("")).removeprefix("src/").replace("/", ".")
        new_dotted = str(move.new.with_suffix("")).removeprefix("src/").replace("/", ".")
        mapping[old_dotted] = new_dotted
        leaf = move.old.stem
        prior = leaf_destinations.get(leaf)
        if prior is not None and prior != new_dotted:
            raise ReorgError(f"ambiguous flat import basename: {leaf}")
        leaf_destinations[leaf] = new_dotted
    mapping.update(leaf_destinations)
    return mapping


class ImportRewriter(cst.CSTTransformer):
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    def leave_Import(
        self, original_node: cst.Import, updated_node: cst.Import
    ) -> cst.BaseSmallStatement:
        if len(updated_node.names) != 1:
            return updated_node
        alias = updated_node.names[0]
        old_name = _dotted_name(alias.name)
        destination = self.mapping.get(old_name or "")
        if destination is None:
            return updated_node
        if "." in (old_name or "") and alias.asname is None:
            raise ReorgError(
                f"dotted import requires a binding-aware explicit rule: import {old_name}"
            )
        parent, leaf = destination.rsplit(".", 1)
        rewritten_alias = alias.with_changes(name=cst.Name(leaf))
        return cst.ImportFrom(
            module=_dotted_expr(parent),
            names=[rewritten_alias],
            semicolon=updated_node.semicolon,
        )

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.ImportFrom:
        if updated_node.relative:
            return updated_node
        old_name = _dotted_name(updated_node.module)
        destination = self.mapping.get(old_name or "")
        if destination is None:
            return updated_node
        return updated_node.with_changes(module=_dotted_expr(destination))


def _is_sys_path_insert(statement: cst.BaseSmallStatement) -> bool:
    if not isinstance(statement, cst.Expr) or not isinstance(statement.value, cst.Call):
        return False
    func = statement.value.func
    return (
        isinstance(func, cst.Attribute)
        and func.attr.value == "insert"
        and isinstance(func.value, cst.Attribute)
        and func.value.attr.value == "path"
        and isinstance(func.value.value, cst.Name)
        and func.value.value.value == "sys"
    )


def remove_top_level_bootstraps(module: cst.Module) -> tuple[cst.Module, int]:
    body: list[cst.BaseStatement] = []
    removed = 0
    for statement in module.body:
        if isinstance(statement, cst.SimpleStatementLine):
            kept = [small for small in statement.body if not _is_sys_path_insert(small)]
            removed += len(statement.body) - len(kept)
            if not kept:
                continue
            statement = statement.with_changes(body=kept)
        body.append(statement)
    return module.with_changes(body=body), removed


def _is_docstring(statement: cst.BaseStatement) -> bool:
    return (
        isinstance(statement, cst.SimpleStatementLine)
        and len(statement.body) == 1
        and isinstance(statement.body[0], cst.Expr)
        and isinstance(statement.body[0].value, (cst.SimpleString, cst.ConcatenatedString))
    )


def _is_future_import(statement: cst.BaseStatement) -> bool:
    return (
        isinstance(statement, cst.SimpleStatementLine)
        and len(statement.body) == 1
        and isinstance(statement.body[0], cst.ImportFrom)
        and _dotted_name(statement.body[0].module) == "__future__"
    )


def _statement_code(statement: cst.BaseStatement) -> str:
    return cst.Module(body=[statement]).code


def guard_count(module: cst.Module) -> int:
    return sum(_statement_code(statement) == GUARD for statement in module.body)


def has_guard(module: cst.Module) -> bool:
    return guard_count(module) == 1


def add_guard(module: cst.Module) -> cst.Module:
    count = guard_count(module)
    if count == 1:
        return module
    if count:
        raise ReorgError(f"entrypoint contains {count} guard headers")
    body = list(module.body)
    index = 1 if body and _is_docstring(body[0]) else 0
    while index < len(body) and _is_future_import(body[index]):
        index += 1
    body.insert(index, cst.parse_statement(GUARD))
    return module.with_changes(body=body)


def python_files() -> Iterable[Path]:
    for relative in ("src", "tests", "bin", "scripts"):
        root = ROOT / relative
        if root.exists():
            yield from sorted(root.rglob("*.py"))


def rewrite_python(
    moves: list[Move],
    entrypoints: set[Path],
    *,
    check: bool,
    expected_bootstrap_removals: dict[Path, int],
) -> list[str]:
    mapping = import_mapping(moves)
    destination_for = {move.old: move.new for move in moves}
    guarded_destinations = {destination_for[path] for path in entrypoints}
    changes: list[str] = []
    removed_bootstraps: dict[Path, int] = {}

    for path in python_files():
        original = path.read_text(encoding="utf-8")
        try:
            module = cst.parse_module(original)
        except cst.ParserSyntaxError as exc:
            raise ReorgError(f"LibCST could not parse {path.relative_to(ROOT)}: {exc}") from exc
        rewritten = module.visit(ImportRewriter(mapping))
        relative = path.relative_to(ROOT)
        if str(relative).startswith("src/latch/"):
            rewritten, removed = remove_top_level_bootstraps(rewritten)
            if removed:
                removed_bootstraps[relative] = removed
        if relative in guarded_destinations:
            rewritten = add_guard(rewritten)
        updated = rewritten.code
        if updated != original:
            changes.append(f"rewrite {relative}")
            if not check:
                path.write_text(updated, encoding="utf-8")

    if removed_bootstraps != expected_bootstrap_removals:
        raise ReorgError(
            "per-file module-level bootstrap removal drift: expected "
            f"{expected_bootstrap_removals}, removed {removed_bootstraps}"
        )
    return changes


def apply_literal_patches(*, check: bool) -> list[str]:
    raw = json.loads(PATCH_FILE.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ReorgError("literal_patches.json must contain a list")
    changes: list[str] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ReorgError(f"literal patch #{index} is not an object")
        try:
            before = item["before"]
            after = item["after"]
            expected = int(item.get("count", 1))
            pending_after = int(item.get("pending_after", 0))
            converged_before = int(
                item.get("converged_before", expected * after.count(before))
            )
            converged_after = int(
                item.get(
                    "converged_after",
                    pending_after - expected * before.count(after) + expected,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReorgError(f"malformed literal patch #{index}: {item!r}") from exc
        optional = bool(item.get("optional", False))
        if ("path" in item) == ("glob" in item):
            raise ReorgError(
                f"literal patch #{index} must contain exactly one of path or glob"
            )
        if "path" in item:
            relative = Path(item["path"])
            targets = [ROOT / relative]
        else:
            pattern = item["glob"]
            if not isinstance(pattern, str):
                raise ReorgError(f"literal patch #{index} has a non-string glob")
            targets = sorted(path for path in ROOT.glob(pattern) if path.is_file())
            relative = Path(pattern)
        missing = [path for path in targets if not path.exists()]
        if missing or not targets:
            if optional:
                continue
            raise ReorgError(f"literal patch target is absent: {relative}")
        texts = {path: path.read_text(encoding="utf-8") for path in targets}
        before_count = sum(value.count(before) for value in texts.values())
        after_count = sum(value.count(after) for value in texts.values())
        pending = (expected, pending_after)
        converged = (converged_before, converged_after)
        if (before_count, after_count) == pending:
            updated = {path: value.replace(before, after) for path, value in texts.items()}
            changes.append(f"patch {relative}: {item.get('label', index)}")
            if not check:
                for path, value in updated.items():
                    if value != texts[path]:
                        path.write_text(value, encoding="utf-8")
        elif (before_count, after_count) != converged:
            raise ReorgError(
                "literal patch is mixed or drifted in "
                f"{relative}: expected pending {pending} or converged "
                f"{converged}, found ({before_count}, {after_count}) for "
                f"{item.get('label', index)}"
            )
    return changes


class FlatImportFinder(cst.CSTVisitor):
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.hits: list[str] = []

    def visit_Import(self, node: cst.Import) -> None:
        for alias in node.names:
            name = _dotted_name(alias.name)
            if name in self.mapping:
                self.hits.append(f"import {name}")

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        if node.relative:
            return
        name = _dotted_name(node.module)
        if name in self.mapping:
            self.hits.append(f"from {name}")


class SourceInvariantFinder(cst.CSTVisitor):
    def __init__(self) -> None:
        self.sys_path_inserts = 0
        self.src_names = 0

    def visit_Call(self, node: cst.Call) -> None:
        func = node.func
        if (
            isinstance(func, cst.Attribute)
            and func.attr.value == "insert"
            and isinstance(func.value, cst.Attribute)
            and func.value.attr.value == "path"
            and isinstance(func.value.value, cst.Name)
            and func.value.value.value == "sys"
        ):
            self.sys_path_inserts += 1

    def visit_Name(self, node: cst.Name) -> None:
        if node.value == "SRC":
            self.src_names += 1


def _scoped_layout_files() -> list[Path]:
    roots = (
        "src",
        "bin",
        "commands",
        "cursor_commands",
        "cursor_skills",
        ".agents",
        ".githooks",
        ".github",
    )
    paths: list[Path] = []
    for relative in roots:
        root = ROOT / relative
        if root.exists():
            paths.extend(
                path
                for path in root.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            )
    paths.extend(
        ROOT / name for name in ("install.sh", "install.ps1", "settings_snippet.json")
    )
    return sorted(set(paths))


def _text_files(paths: Iterable[Path]) -> dict[Path, str]:
    result: dict[Path, str] = {}
    for path in paths:
        try:
            result[path] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
    return result


def verify_dynamic_imports() -> list[str]:
    hits: set[tuple[str, str]] = set()
    for path in python_files():
        relative = str(path.relative_to(ROOT))
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if "importlib.import_module(" in stripped or "__import__(" in stripped:
                hits.add((relative, stripped))
            if re.search(r"python\s+-c\s+.*import", stripped):
                hits.add((relative, stripped))
    unexpected = hits - DYNAMIC_IMPORT_ALLOWLIST
    missing = {
        item
        for item in DYNAMIC_IMPORT_ALLOWLIST
        if (ROOT / item[0]).exists() and item not in hits
    }
    failures = [f"unapproved dynamic import: {item}" for item in sorted(unexpected)]
    failures.extend(f"missing dynamic-import allowance: {item}" for item in sorted(missing))

    doctor = ROOT / "src/latch/install/doctor.py"
    if doctor.exists():
        text = doctor.read_text(encoding="utf-8")
        required = (
            'REQUIRED_MODULES = ["mcp", "onnxruntime", "tokenizers", '
            '"numpy", "sqlite_vec"]'
        )
        if required not in text:
            failures.append("doctor REQUIRED_MODULES probe changed or lost its whitelist")
        if 'f"sys.path.insert(0, {str(src_dir)!r})\\n"' not in text:
            failures.append("doctor embed probe lost the isolated src-path insert")
        if '"from latch.retrieval import embeddings\\n"' not in text:
            failures.append("doctor embed probe does not import packaged embeddings")
    return failures


def verify_embedded_flat_imports(mapping: dict[str, str]) -> list[str]:
    """Reject executable Python snippets that retain pre-package imports."""
    failures: list[str] = []
    for path in python_files():
        source = path.read_text(encoding="utf-8")
        try:
            outer = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # LibCST verification reports syntax elsewhere.
            failures.append(f"cannot parse embedded-import host {path}: {exc}")
            continue
        for constant in ast.walk(outer):
            if not isinstance(constant, ast.Constant) or not isinstance(
                constant.value, str
            ):
                continue
            if "import " not in constant.value and "from " not in constant.value:
                continue
            try:
                embedded = ast.parse(textwrap.dedent(constant.value))
            except SyntaxError:
                continue
            hits: set[str] = set()
            for node in ast.walk(embedded):
                if isinstance(node, ast.Import):
                    hits.update(alias.name for alias in node.names if alias.name in mapping)
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.level == 0
                    and node.module in mapping
                ):
                    hits.add(node.module)
            if hits:
                relative = path.relative_to(ROOT)
                failures.append(
                    f"embedded flat import in {relative}:{constant.lineno}: "
                    f"{sorted(hits)}"
                )
    return failures


def verify_layout_strings(moves: list[Move]) -> list[str]:
    texts = _text_files(_scoped_layout_files())
    failures: list[str] = []
    allowances = LEGACY_LAYOUT_ALLOWANCES + CANONICAL_DYNAMIC_LAYOUT_ALLOWANCES
    for pattern, literal, expected in allowances:
        targets = sorted(path for path in ROOT.glob(pattern) if path.is_file())
        count = sum(texts.get(path, "").count(literal) for path in targets)
        if count != expected:
            failures.append(
                f"layout allowance drift for {pattern} {literal!r}: "
                f"expected {expected}, found {count}"
            )
        for path in targets:
            if path in texts:
                texts[path] = texts[path].replace(literal, "")

    forbidden: set[tuple[str, str]] = set()
    old_variants: set[str] = set()
    for move in moves:
        old = str(move.old)
        parts = old.split("/")
        old_variants.update(
            {
                old,
                old.replace("/", "\\"),
                " / ".join(f'"{part}"' for part in parts),
                " / ".join(repr(part) for part in parts),
            }
        )
    generic_patterns = (
        re.compile(r"/src/(?!latch/)"),
        re.compile(
            r"""(?<![\w/])src/(?!latch/)[^\s"'`<>)}\],;]+"""
        ),
        re.compile(r'''["']src["']\s*/\s*[A-Za-z_]'''),
        re.compile(r"src\\(?!latch\\)"),
    )
    for path, text in texts.items():
        relative = str(path.relative_to(ROOT))
        for line in text.splitlines():
            stripped = line.strip()
            quoted_components = re.findall(
                r'''["']src["']\s*/\s*["']([^"']+)["']''', line
            )
            if any(component != "latch" for component in quoted_components) or any(
                variant in line for variant in old_variants
            ) or any(
                pattern.search(line) for pattern in generic_patterns
            ):
                forbidden.add((relative, stripped))
    failures.extend(f"unpatched layout string: {item}" for item in sorted(forbidden))
    return failures


def verify_collection(pytest_python: str, baseline_file: Path) -> list[str]:
    baseline_lines = _data_lines(baseline_file)
    addition_lines = _data_lines(NEW_NODES_FILE)
    baseline = set(baseline_lines)
    additions = set(addition_lines)
    expected_lines = list(baseline_lines)
    if (ROOT / "tests/test_reorg_invariants.py").exists():
        expected_lines.extend(addition_lines)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [pytest_python, "-m", "pytest", "-p", "no:cacheprovider", "--collect-only", "-q"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    if result.returncode != 0:
        detail = (result.stdout + "\n" + result.stderr)[-4000:]
        return [f"pytest collection failed with {pytest_python}: {detail}"]
    actual_lines = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    ]
    expected_counter = Counter(expected_lines)
    actual_counter = Counter(actual_lines)
    missing = sorted((expected_counter - actual_counter).elements())
    extra = sorted((actual_counter - expected_counter).elements())
    failures: list[str] = []
    if missing:
        failures.append(f"pytest collection lost node IDs: {missing[:20]}")
    if extra:
        failures.append(f"pytest collection added unapproved node IDs: {extra[:20]}")
    if (ROOT / "tests/test_reorg_invariants.py").exists():
        delta = set(actual_lines) - baseline
        if delta != additions:
            failures.append(
                "new invariant-test delta does not equal new_test_node_ids.txt"
            )
    return failures


def verify(
    moves: list[Move],
    optional: set[Path],
    entrypoints: set[Path],
    *,
    pytest_python: str | None,
    baseline_file: Path,
) -> None:
    mapping = import_mapping(moves)
    destination_for = {move.old: move.new for move in moves}
    guarded_destinations = {destination_for[old] for old in entrypoints}
    failures: list[str] = []

    validate_layout_closure(moves, after=True)

    for move in moves:
        old = ROOT / move.old
        new = ROOT / move.new
        if old.exists() and new.exists():
            failures.append(f"collision: {move.old} and {move.new}")
        elif old.exists():
            failures.append(f"unmoved source: {move.old}")
        elif not new.exists() and move.old not in optional:
            failures.append(f"missing required destination: {move.new}")

    for path in python_files():
        module = cst.parse_module(path.read_text(encoding="utf-8"))
        finder = FlatImportFinder(mapping)
        module.visit(finder)
        if finder.hits:
            failures.append(f"flat imports in {path.relative_to(ROOT)}: {finder.hits}")
        relative = path.relative_to(ROOT)
        if str(relative).startswith("src/latch/"):
            expected_guard = 1 if relative in guarded_destinations else 0
            actual_guards = guard_count(module)
            if actual_guards != expected_guard:
                failures.append(
                    f"guard count in {relative}: expected {expected_guard}, "
                    f"found {actual_guards}"
                )
            invariants = SourceInvariantFinder()
            module.visit(invariants)
            if invariants.sys_path_inserts != expected_guard:
                failures.append(
                    f"sys.path.insert count in {relative}: expected "
                    f"{expected_guard}, found {invariants.sys_path_inserts}"
                )
            if relative in ORPHAN_BOOTSTRAP_DESTINATIONS and invariants.src_names:
                failures.append(
                    f"orphaned bootstrap name SRC remains in {relative}"
                )

    anchor_hits: set[tuple[str, str]] = set()
    for path in sorted((ROOT / "src/latch").rglob("*.py")):
        relative = str(path.relative_to(ROOT))
        for line in path.read_text(encoding="utf-8").splitlines():
            if "parent.parent" in line or "parents[1]" in line:
                anchor_hits.add((relative, line.strip()))
    for item in sorted(anchor_hits - ROOT_ANCHOR_ALLOWLIST):
        failures.append(f"unapproved numeric root anchor: {item}")
    for item in sorted(ROOT_ANCHOR_ALLOWLIST - anchor_hits):
        failures.append(f"root-anchor allowance disappeared or drifted: {item}")

    failures.extend(verify_dynamic_imports())
    failures.extend(verify_embedded_flat_imports(mapping))
    failures.extend(verify_layout_strings(moves))
    if pytest_python is not None:
        failures.extend(verify_collection(pytest_python, baseline_file))

    if failures:
        raise ReorgError("verification failed:\n  " + "\n  ".join(failures))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether applying the codemod would change the tree",
    )
    parser.add_argument(
        "--pytest-python",
        default=os.environ.get("LATCH_REORG_PYTEST_PYTHON", sys.executable),
        help=(
            "Python interpreter used for the --check collection gate; it must "
            "have the repository's test dependencies installed"
        ),
    )
    parser.add_argument(
        "--baseline-node-ids",
        type=Path,
        default=BASELINE_NODES_FILE,
        help="lane-specific sorted pytest node-ID baseline",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    moves = load_moves()
    optional = load_optional()
    entrypoints = load_entrypoints()
    bootstrap_counts = load_bootstrap_counts()
    validate_data(moves, optional, entrypoints, bootstrap_counts)
    pending_old = {move.old for move in moves if (ROOT / move.old).exists()}
    destination_for = {move.old: move.new for move in moves}
    expected_bootstrap_removals = {
        destination_for[path]: count
        for path, count in bootstrap_counts.items()
        if path in pending_old
    }

    changes: list[str] = []
    changes.extend(move_modules(moves, optional, check=args.check))
    changes.extend(ensure_packages(check=args.check))
    changes.extend(
        rewrite_python(
            moves,
            entrypoints,
            check=args.check,
            expected_bootstrap_removals=(
                {} if args.check else expected_bootstrap_removals
            ),
        )
    )
    changes.extend(apply_literal_patches(check=args.check))

    if args.check:
        if changes:
            print("reorg is not converged:", file=sys.stderr)
            for change in changes:
                print(f"  {change}", file=sys.stderr)
            return 1
        verify(
            moves,
            optional,
            entrypoints,
            pytest_python=args.pytest_python,
            baseline_file=args.baseline_node_ids,
        )
        print("reorg is converged")
        return 0

    verify(
        moves,
        optional,
        entrypoints,
        pytest_python=None,
        baseline_file=args.baseline_node_ids,
    )
    print(f"reorg applied; {len(changes)} path/file operations")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ReorgError, subprocess.CalledProcessError) as exc:
        print(f"reorg failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
