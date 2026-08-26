"""Deterministic compiler and evaluator for typed rejected-path predicates.

This module is intentionally self-contained.  It must stay safe to import on a
PreToolUse path: no Latch connection, model backend, budget, or network stack is
required to compile or evaluate a predicate.
"""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
import re
from typing import Any, ClassVar, Iterable, Mapping, Sequence


ENGINE = "predicate-v1"
COMPILED_PREFIXES = frozenset({"file", "glob", "package", "import", "api"})
DECLARED_UNCOMPILABLE_PREFIXES = frozenset(
    {"feature", "positioning", "process", "distribution", "roadmap", "architecture"}
)
_KNOWN_PREFIXES = COMPILED_PREFIXES | DECLARED_UNCOMPILABLE_PREFIXES
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_MODULE_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_API_RE = re.compile(r"^[A-Za-z_]\w*(?:[.:/-][A-Za-z_]\w*)*$")


@dataclass(frozen=True)
class ToolCallContext:
    """Inputs available to the deterministic evaluation surface.

    Every field is optional so callers can supply only facts already known at
    their tool boundary.  Missing fields are empty evidence, never a match.
    """

    tool_name: str | None = None
    file_paths: Sequence[str] | None = None
    command_text: str | None = None
    diff_paths: Sequence[str] | None = None
    import_names: Sequence[str] | None = None


@dataclass(frozen=True)
class ParsedScopePredicate:
    predicate: str | None
    prefix: str | None
    value: str | None
    error: str | None


@dataclass(frozen=True)
class CompiledCheck:
    rejected_path_id: int
    node_id: int
    option: str
    predicate: str
    reason: str
    source: str
    prefix: str
    value: str

    compilable: ClassVar[bool] = True

    def matches(self, context: ToolCallContext) -> bool:
        return _matches(self.prefix, self.value, context)


@dataclass(frozen=True)
class UncompilableCheck:
    rejected_path_id: int
    node_id: int
    option: str
    predicate: str | None
    reason: str
    source: str
    prefix: str | None
    value: str | None
    uncompilable_reason: str

    compilable: ClassVar[bool] = False

    def matches(self, context: ToolCallContext) -> bool:
        del context
        return False


PredicateCheck = CompiledCheck | UncompilableCheck


def parse_scope_predicate(scope_predicate: object) -> ParsedScopePredicate:
    """Parse ``type:value`` without guessing semantics for malformed input."""
    if scope_predicate is None:
        return ParsedScopePredicate(None, None, None, "scope predicate is NULL")
    if not isinstance(scope_predicate, str):
        return ParsedScopePredicate(
            None, None, None, "scope predicate must be text or NULL"
        )

    raw = scope_predicate.strip()
    if not raw:
        return ParsedScopePredicate(scope_predicate, None, None, "scope predicate is empty")

    if ":" not in raw:
        # These five bare values were observed in the banked taxonomy.  Keep
        # their aggregate bucket while declaring them uncompilable.  Arbitrary
        # bare text is not returned as a prefix because coverage output is
        # aggregate-only and must never leak predicate text.
        bare_prefix = raw.lower() if raw.lower() in _KNOWN_PREFIXES else None
        return ParsedScopePredicate(
            scope_predicate,
            bare_prefix,
            None,
            "scope predicate has no type:value separator",
        )

    raw_prefix, raw_value = raw.split(":", 1)
    prefix = raw_prefix.strip().lower()
    value = raw_value.strip()
    if not prefix or not _PREFIX_RE.fullmatch(prefix):
        return ParsedScopePredicate(
            scope_predicate, None, value or None, "scope predicate has an invalid prefix"
        )
    if not value:
        return ParsedScopePredicate(
            scope_predicate, prefix, None, "scope predicate has an empty value"
        )
    return ParsedScopePredicate(scope_predicate, prefix, value, None)


def compile_predicate(row: Mapping[str, Any] | object) -> PredicateCheck:
    """Compile one rejected_path-shaped row into a typed check."""
    rejected_path_id = _required_int(row, "rejected_path_id", fallback="id")
    node_id = _required_int(row, "node_id")
    option = str(_row_value(row, "option", ""))
    reason = str(_row_value(row, "reason", ""))
    source = str(_row_value(row, "source", "declared"))
    parsed = parse_scope_predicate(_row_value(row, "scope_predicate", None))

    error = parsed.error
    if error is None and parsed.prefix not in COMPILED_PREFIXES:
        error = f"unsupported predicate prefix: {parsed.prefix}"
    if error is None:
        error = _validate_compilable_value(parsed.prefix, parsed.value)

    common = {
        "rejected_path_id": rejected_path_id,
        "node_id": node_id,
        "option": option,
        "predicate": parsed.predicate,
        "reason": reason,
        "source": source,
        "prefix": parsed.prefix,
        "value": parsed.value,
    }
    if error is not None:
        return UncompilableCheck(**common, uncompilable_reason=error)
    return CompiledCheck(**common)


def compile_predicates(
    rows: Iterable[Mapping[str, Any] | object],
) -> list[PredicateCheck]:
    return [compile_predicate(row) for row in rows]


def evaluate(
    compiled_checks: Iterable[PredicateCheck], context: ToolCallContext
) -> dict[str, object]:
    """Return the predicate-v1 verdict for a tool-call context.

    Compiled matches block.  Non-matches and typed uncompilable checks do not
    match and therefore pass.  The ``flag`` vocabulary member is reserved for
    the later policy-integration phase; this deterministic skeleton never
    guesses that an uncompilable predicate applies.
    """
    matches: list[dict[str, object]] = []
    for check in compiled_checks:
        if not check.compilable or not check.matches(context):
            continue
        matches.append(
            {
                "rejected_path_id": check.rejected_path_id,
                "node_id": check.node_id,
                "option": check.option,
                "predicate": check.predicate,
                "reason": check.reason,
                "source": check.source,
            }
        )
    return {
        "engine": ENGINE,
        "decision": "block" if matches else "pass",
        "llm_calls": 0,
        "matches": matches,
    }


def coverage_prefix(scope_predicate: object) -> str:
    """Return a safe aggregate bucket, never a predicate value."""
    parsed = parse_scope_predicate(scope_predicate)
    if parsed.prefix is not None:
        return parsed.prefix
    if scope_predicate is None:
        return "<null>"
    if isinstance(scope_predicate, str) and not scope_predicate.strip():
        return "<empty>"
    return "<invalid>"


def _validate_compilable_value(prefix: str | None, value: str | None) -> str | None:
    if prefix is None or value is None:
        return "predicate did not produce a typed value"
    if "\x00" in value:
        return "predicate value contains a NUL byte"
    if prefix in {"package", "import"} and not _MODULE_RE.fullmatch(value):
        return f"{prefix} predicate is not a module identifier"
    if prefix == "api" and not _API_RE.fullmatch(value):
        return "api predicate is not an API identifier"
    return None


def _row_value(row: Mapping[str, Any] | object, name: str, default: Any) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    keys = getattr(row, "keys", None)
    if callable(keys) and name in keys():
        return row[name]  # type: ignore[index]
    return getattr(row, name, default)


def _required_int(
    row: Mapping[str, Any] | object, name: str, fallback: str | None = None
) -> int:
    value = _row_value(row, name, None)
    if value is None and fallback is not None:
        value = _row_value(row, fallback, None)
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} is required")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _as_strings(values: Sequence[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        return (values,)
    return tuple(value for value in values if isinstance(value, str))


def _context_paths(context: ToolCallContext) -> tuple[str, ...]:
    return _as_strings(context.file_paths) + _as_strings(context.diff_paths)


def _path_parts(value: str) -> tuple[str, ...]:
    parts: list[str] = []
    for part in value.replace("\\", "/").split("/"):
        if not part or part == ".":
            continue
        if part == ".." and parts and parts[-1] != "..":
            parts.pop()
        else:
            parts.append(part)
    return tuple(parts)


def _file_matches(expected: str, candidate: str) -> bool:
    expected_parts = _path_parts(expected)
    candidate_parts = _path_parts(candidate)
    if not expected_parts or len(candidate_parts) < len(expected_parts):
        return False
    # A repo-relative directory can occur inside an absolute tool path.  Match
    # whole contiguous components so ``file:src`` contains ``/repo/src/x.py``
    # but never the substring-adjacent ``/repo/srcish/x.py``.
    width = len(expected_parts)
    return any(
        candidate_parts[index : index + width] == expected_parts
        for index in range(len(candidate_parts) - width + 1)
    )


def _module_parts(value: str) -> tuple[str, ...]:
    normalized = value.replace("\\", "/").strip("./")
    if normalized.endswith(".py"):
        normalized = normalized[:-3]
    return tuple(part for part in normalized.replace("/", ".").split(".") if part)


def _contains_module(actual: str, expected: str) -> bool:
    actual_parts = _module_parts(actual)
    expected_parts = _module_parts(expected)
    if not expected_parts or len(actual_parts) < len(expected_parts):
        return False
    width = len(expected_parts)
    return any(
        actual_parts[index : index + width] == expected_parts
        for index in range(len(actual_parts) - width + 1)
    )


def _imports_from_text(text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    found: list[str] = []
    for match in re.finditer(r"(?m)(?:^|[;\n])\s*import\s+([^\n;#]+)", text):
        for part in match.group(1).split(","):
            name = part.strip().split(" as ", 1)[0].strip()
            if _MODULE_RE.fullmatch(name):
                found.append(name)
    for match in re.finditer(
        r"(?m)(?:^|[;\n])\s*from\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
        r"\s+import\s+([^\n;#]+)",
        text,
    ):
        base = match.group(1)
        found.append(base)
        for part in match.group(2).split(","):
            name = part.strip().split(" as ", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z_]\w*", name):
                found.append(f"{base}.{name}")
    return tuple(found)


def _identifier_occurs(identifier: str, text: str | None) -> bool:
    if not text:
        return False
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])",
            text,
        )
    )


def _matches(prefix: str, value: str, context: ToolCallContext) -> bool:
    if prefix == "file":
        return any(_file_matches(value, path) for path in _context_paths(context))
    if prefix == "glob":
        pattern = value.replace("\\", "/")
        return any(
            fnmatchcase(path.replace("\\", "/"), pattern)
            for path in _context_paths(context)
        )
    if prefix == "package":
        candidates = _as_strings(context.import_names) + _context_paths(context)
        return any(_contains_module(candidate, value) for candidate in candidates)
    if prefix == "import":
        candidates = _as_strings(context.import_names) + _imports_from_text(
            context.command_text
        )
        return any(
            candidate == value or candidate.startswith(f"{value}.")
            for candidate in candidates
        )
    if prefix == "api":
        texts = (
            context.command_text,
            context.tool_name,
            " ".join(_as_strings(context.import_names)),
        )
        return any(_identifier_occurs(value, text) for text in texts)
    return False


__all__ = [
    "COMPILED_PREFIXES",
    "CompiledCheck",
    "ENGINE",
    "ParsedScopePredicate",
    "PredicateCheck",
    "ToolCallContext",
    "UncompilableCheck",
    "compile_predicate",
    "compile_predicates",
    "coverage_prefix",
    "evaluate",
    "parse_scope_predicate",
]
