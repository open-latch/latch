"""Deterministic compiler and evaluator for typed rejected-path predicates.

This module is intentionally self-contained.  It must stay safe to import on a
PreToolUse path: no Latch connection, model backend, budget, or network stack is
required to compile or evaluate a predicate.
"""
from __future__ import annotations

from dataclasses import dataclass
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
_SAFE_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_GLOB_CHARS = 4096
_MAX_GLOB_SEGMENTS = 256
_MAX_PATH_CHARS = 4096


@dataclass(frozen=True)
class ToolCallContext:
    """Canonical host-neutral evidence for one proposed action.

    The first five fields retain the A2-skeleton construction surface.
    Canonical policy consumers additionally provide a domain, project root,
    cwd, proposed/diff/staged paths, structured identifiers, and an explicit
    completeness/provenance attestation.  Opaque command text is retained for
    consumers and receipts but is never parsed as package/import/API evidence.
    """

    tool_name: str | None = None
    file_paths: Sequence[str] | None = None
    command_text: str | None = None
    diff_paths: Sequence[str] | None = None
    import_names: Sequence[str] | None = None
    policy_domain_id: str | None = None
    project_root: str | None = None
    cwd: str | None = None
    proposed_file_paths: Sequence[str] | None = None
    staged_paths: Sequence[str] | None = None
    api_names: Sequence[str] | None = None
    evidence_complete: bool | None = None
    evidence_provenance: Mapping[str, str] | Sequence[str] | str | None = None


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

    A conclusive compiled match blocks even if unrelated evidence is
    incomplete.  With no match, malformed, foreign-root, conflicting, opaque,
    or incomplete canonical evidence flags instead of claiming a pass.
    Typed uncompilable checks remain advisory and never match.
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
    issues = context_evidence_issues(context)
    if matches:
        decision = "block"
    elif issues:
        decision = "flag"
    else:
        decision = "pass"
    return {
        "engine": ENGINE,
        "decision": decision,
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
    if prefix in {"file", "glob"}:
        normalized = value.replace("\\", "/")
        path_segments = normalized.split("/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", value):
            return f"{prefix} predicate must be project-relative"
        if any(part == ".." for part in path_segments):
            return f"{prefix} predicate contains path traversal"
        if any(part in {"", "."} for part in path_segments):
            return f"{prefix} predicate contains a non-canonical path segment"
        if prefix == "glob":
            if len(normalized) > _MAX_GLOB_CHARS:
                return "glob predicate exceeds the maximum length"
            if len(path_segments) > _MAX_GLOB_SEGMENTS:
                return "glob predicate has too many path segments"
            if any(
                "**" in segment and segment != "**"
                for segment in path_segments
            ):
                return "glob double-star must occupy a whole path segment"
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
    if not isinstance(values, Sequence):
        return ()
    return tuple(value for value in values if isinstance(value, str))


@dataclass(frozen=True)
class _CanonicalPath:
    value: str
    case_sensitive: bool
    root_anchored: bool


@dataclass(frozen=True)
class _PathModel:
    anchor: str
    root_parts: tuple[str, ...]
    windows: bool


_CANONICAL_CONTEXT_FIELDS = (
    "policy_domain_id",
    "project_root",
    "cwd",
    "proposed_file_paths",
    "staged_paths",
    "api_names",
    "evidence_complete",
    "evidence_provenance",
)


def _canonical_context(context: ToolCallContext) -> bool:
    return any(
        getattr(context, field_name) is not None
        for field_name in _CANONICAL_CONTEXT_FIELDS
    )


def _valid_nonempty_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "\x00" not in value
        and not any(ord(character) < 32 for character in value)
    )


def _malformed_sequence(values: object) -> bool:
    if values is None:
        return False
    if isinstance(values, str) or not isinstance(values, Sequence):
        return True
    return any(not _valid_nonempty_text(value) for value in values)


def _valid_provenance(value: object) -> bool:
    if isinstance(value, str):
        return _valid_nonempty_text(value)
    if isinstance(value, Mapping):
        return bool(value) and all(
            _valid_nonempty_text(key) and _valid_nonempty_text(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence):
        return bool(value) and all(_valid_nonempty_text(item) for item in value)
    return False


def context_evidence_issues(context: ToolCallContext) -> tuple[str, ...]:
    """Return aggregate-safe reason codes for unsafe canonical evidence.

    The codes intentionally contain field names only, never action or path
    text, so an outer policy receipt can reuse them without leaking content.
    Legacy skeleton contexts are accepted at this low-level seam; the complete
    policy evaluator constructs the canonical envelope and therefore receives
    fail-closed evidence checks.
    """
    if not _canonical_context(context):
        return ()

    issues: list[str] = []
    if (
        not isinstance(context.policy_domain_id, str)
        or _SAFE_OPAQUE_ID_RE.fullmatch(context.policy_domain_id) is None
    ):
        issues.append("policy_domain_missing")
    if not _valid_nonempty_text(context.tool_name):
        issues.append("tool_name_missing")
    if not _valid_nonempty_text(context.project_root):
        issues.append("project_root_missing")
    if not _valid_nonempty_text(context.cwd):
        issues.append("cwd_missing")

    required_sequences = (
        ("proposed_file_paths", context.proposed_file_paths),
        ("diff_paths", context.diff_paths),
        ("staged_paths", context.staged_paths),
        ("import_names", context.import_names),
        ("api_names", context.api_names),
    )
    for field_name, values in required_sequences:
        if values is None:
            issues.append(f"{field_name}_missing")
        elif _malformed_sequence(values):
            issues.append(f"malformed_{field_name}")

    if (
        context.file_paths is not None
        and context.proposed_file_paths is not None
        and _as_strings(context.file_paths)
        != _as_strings(context.proposed_file_paths)
    ):
        issues.append("conflicting_proposed_file_paths")

    if context.evidence_complete is False:
        issues.append("evidence_incomplete")
    elif context.evidence_complete is not True:
        issues.append("evidence_completeness_missing")
    elif not any(
        _as_strings(values)
        for values in (
            context.proposed_file_paths,
            context.diff_paths,
            context.staged_paths,
            context.import_names,
            context.api_names,
        )
    ):
        issues.append("mutation_footprint_missing")
    if not _valid_provenance(context.evidence_provenance):
        issues.append("evidence_provenance_missing")
    if context.command_text is not None and not isinstance(context.command_text, str):
        issues.append("malformed_command_text")

    import_names = _as_strings(context.import_names)
    if any(not _MODULE_RE.fullmatch(name) for name in import_names):
        issues.append("malformed_import_names")
    api_names = _as_strings(context.api_names)
    if any(not _API_RE.fullmatch(name) for name in api_names):
        issues.append("malformed_api_names")

    _, path_issues = _canonical_context_paths(context)
    issues.extend(path_issues)
    return tuple(dict.fromkeys(issues))


def _looks_windows_absolute(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", value)) or value.startswith(
        ("\\\\", "//")
    )


def _contains_traversal(value: str, *, windows: bool) -> bool:
    separator = r"[\\/]+" if windows else r"/+"
    return any(part == ".." for part in re.split(separator, value))


def _styled_parts(value: str, *, windows: bool) -> tuple[str, ...]:
    separator = r"[\\/]+" if windows else r"/+"
    return tuple(
        part
        for part in re.split(separator, value)
        if part not in {"", "."}
    )


def _has_noncanonical_windows_segment(parts: Sequence[str]) -> bool:
    return any(
        part.endswith((".", " ")) or ":" in part
        for part in parts
    )


def _absolute_path_parts(
    value: str,
    *,
    windows: bool,
) -> tuple[str, tuple[str, ...]] | None:
    if not windows:
        if not value.startswith("/"):
            return None
        return "/", _styled_parts(value[1:], windows=False)

    normalized = value.replace("/", "\\")
    if normalized.startswith("\\\\"):
        components = _styled_parts(normalized[2:], windows=True)
        if (
            len(components) < 2
            or _has_noncanonical_windows_segment(components[:2])
        ):
            return None
        anchor = f"//{components[0]}/{components[1]}".casefold()
        return anchor, components[2:]
    drive = re.match(r"^([A-Za-z]):\\", normalized)
    if drive is None:
        return None
    return f"{drive.group(1).casefold()}:", _styled_parts(
        normalized[3:],
        windows=True,
    )


def _same_parts(
    left: Sequence[str],
    right: Sequence[str],
    *,
    windows: bool,
) -> bool:
    if windows:
        return tuple(part.casefold() for part in left) == tuple(
            part.casefold() for part in right
        )
    return tuple(left) == tuple(right)


def _within_root(
    candidate_parts: Sequence[str],
    root_parts: Sequence[str],
    *,
    windows: bool,
) -> bool:
    return len(candidate_parts) >= len(root_parts) and _same_parts(
        candidate_parts[: len(root_parts)],
        root_parts,
        windows=windows,
    )


def _build_path_model(context: ToolCallContext) -> tuple[_PathModel | None, list[str]]:
    issues: list[str] = []
    root_text = context.project_root
    if not _valid_nonempty_text(root_text):
        return None, ["project_root_missing"]
    assert isinstance(root_text, str)
    if len(root_text) > _MAX_PATH_CHARS:
        return None, ["malformed_project_root"]

    windows = _looks_windows_absolute(root_text)
    if _contains_traversal(root_text, windows=windows):
        return None, ["path_traversal:project_root"]
    if not windows and "\\" in root_text:
        return None, ["malformed_project_root"]
    root = _absolute_path_parts(root_text, windows=windows)
    if root is None:
        return None, ["malformed_project_root"]
    root_anchor, root_parts = root
    if windows and _has_noncanonical_windows_segment(root_parts):
        return None, ["noncanonical_windows_path:project_root"]

    cwd_text = context.cwd
    if not _valid_nonempty_text(cwd_text):
        cwd_parts = root_parts
        issues.append("cwd_missing")
    else:
        assert isinstance(cwd_text, str)
        if len(cwd_text) > _MAX_PATH_CHARS:
            return None, ["malformed_cwd"]
        if _contains_traversal(cwd_text, windows=windows):
            return None, ["path_traversal:cwd"]
        if windows:
            if cwd_text.startswith(("/", "\\")) and not _looks_windows_absolute(
                cwd_text
            ):
                return None, ["foreign_cwd"]
        else:
            if _looks_windows_absolute(cwd_text) or "\\" in cwd_text:
                return None, ["foreign_cwd"]
        absolute_cwd = _absolute_path_parts(cwd_text, windows=windows)
        if absolute_cwd is None:
            return None, ["malformed_cwd"]
        cwd_anchor, cwd_parts = absolute_cwd
        if windows and _has_noncanonical_windows_segment(cwd_parts):
            return None, ["noncanonical_windows_path:cwd"]
        if cwd_anchor != root_anchor or not _within_root(
            cwd_parts,
            root_parts,
            windows=windows,
        ):
            return None, ["foreign_cwd"]
    return (
        _PathModel(
            anchor=root_anchor,
            root_parts=root_parts,
            windows=windows,
        ),
        issues,
    )


def _path_groups(context: ToolCallContext) -> tuple[tuple[str, object], ...]:
    proposed = (
        context.proposed_file_paths
        if context.proposed_file_paths is not None
        else context.file_paths
    )
    return (
        ("proposed_file_paths", proposed),
        ("diff_paths", context.diff_paths),
        ("staged_paths", context.staged_paths),
    )


def _canonicalize_candidate(
    raw: str,
    *,
    field_name: str,
    model: _PathModel,
) -> tuple[_CanonicalPath | None, str | None]:
    if not _valid_nonempty_text(raw):
        return None, f"malformed_path:{field_name}"
    if len(raw) > _MAX_PATH_CHARS:
        return None, f"malformed_path:{field_name}"
    if _contains_traversal(raw, windows=model.windows):
        return None, f"path_traversal:{field_name}"

    if model.windows:
        if raw.startswith(("/", "\\")) and not _looks_windows_absolute(raw):
            return None, f"foreign_path:{field_name}"
    else:
        if _looks_windows_absolute(raw) or "\\" in raw:
            return None, f"foreign_path:{field_name}"

    absolute = _absolute_path_parts(raw, windows=model.windows)
    if absolute is None:
        if model.windows and (
            re.match(r"^[A-Za-z]:", raw) or raw.startswith(("/", "\\"))
        ):
            return None, f"foreign_path:{field_name}"
        candidate_parts = model.root_parts + _styled_parts(
            raw,
            windows=model.windows,
        )
        candidate_anchor = model.anchor
    else:
        candidate_anchor, candidate_parts = absolute
    if model.windows and _has_noncanonical_windows_segment(candidate_parts):
        return None, f"noncanonical_windows_path:{field_name}"
    if candidate_anchor != model.anchor or not _within_root(
        candidate_parts,
        model.root_parts,
        windows=model.windows,
    ):
        return None, f"foreign_path:{field_name}"
    relative_parts = candidate_parts[len(model.root_parts) :]
    value = "/".join(relative_parts)
    return (
        _CanonicalPath(
            value=value,
            case_sensitive=not model.windows,
            root_anchored=True,
        ),
        None,
    )


def _canonical_context_paths(
    context: ToolCallContext,
) -> tuple[tuple[_CanonicalPath, ...], tuple[str, ...]]:
    model, model_issues = _build_path_model(context)
    if model is None:
        return (), tuple(model_issues)

    paths: list[_CanonicalPath] = []
    issues = list(model_issues)
    for field_name, raw_values in _path_groups(context):
        if raw_values is None or isinstance(raw_values, str):
            continue
        if not isinstance(raw_values, Sequence):
            continue
        for raw in raw_values:
            if not isinstance(raw, str):
                continue
            path, issue = _canonicalize_candidate(
                raw,
                field_name=field_name,
                model=model,
            )
            if issue is not None:
                issues.append(issue)
            elif path is not None:
                paths.append(path)
    return tuple(paths), tuple(dict.fromkeys(issues))


def _context_paths(context: ToolCallContext) -> tuple[_CanonicalPath, ...]:
    if _canonical_context(context):
        paths, _ = _canonical_context_paths(context)
        return paths
    raw_paths = (
        _as_strings(context.file_paths)
        + _as_strings(context.diff_paths)
        + _as_strings(context.staged_paths)
    )
    return tuple(
        _CanonicalPath(
            value="/".join(_path_parts(path)),
            case_sensitive=True,
            root_anchored=False,
        )
        for path in raw_paths
    )


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


def _project_file_matches(expected: str, candidate: _CanonicalPath) -> bool:
    expected_value = "/".join(_path_parts(expected))
    candidate_value = candidate.value
    if not candidate.case_sensitive:
        expected_value = expected_value.casefold()
        candidate_value = candidate_value.casefold()
    if not candidate.root_anchored:
        expected_parts = tuple(expected_value.split("/"))
        candidate_parts = tuple(candidate_value.split("/"))
        width = len(expected_parts)
        return any(
            candidate_parts[index : index + width] == expected_parts
            for index in range(len(candidate_parts) - width + 1)
        )
    return candidate_value == expected_value or candidate_value.startswith(
        f"{expected_value}/"
    )


def _segment_glob_matches(pattern: str, candidate: str) -> bool:
    """Match ``*``/``?`` inside one path segment without regex backtracking."""
    pattern_index = 0
    candidate_index = 0
    star_index = -1
    retry_index = -1
    while candidate_index < len(candidate):
        if pattern_index < len(pattern) and (
            pattern[pattern_index] == "?"
            or pattern[pattern_index] == candidate[candidate_index]
        ):
            pattern_index += 1
            candidate_index += 1
        elif pattern_index < len(pattern) and pattern[pattern_index] == "*":
            star_index = pattern_index
            retry_index = candidate_index
            pattern_index += 1
        elif star_index >= 0:
            retry_index += 1
            candidate_index = retry_index
            pattern_index = star_index + 1
        else:
            return False
    while pattern_index < len(pattern) and pattern[pattern_index] == "*":
        pattern_index += 1
    return pattern_index == len(pattern)


def _segment_glob_path_matches(pattern: str, candidate: str) -> bool:
    """Match project-relative segments with iterative, bounded globstar state."""
    pattern_segments = tuple(pattern.split("/"))
    candidate_segments = () if not candidate else tuple(candidate.split("/"))
    previous = [True] + [False] * len(candidate_segments)
    for pattern_segment in pattern_segments:
        current = [False] * (len(candidate_segments) + 1)
        if pattern_segment == "**":
            current[0] = previous[0]
            for index in range(1, len(candidate_segments) + 1):
                current[index] = previous[index] or current[index - 1]
        else:
            for index, candidate_segment in enumerate(candidate_segments, start=1):
                current[index] = previous[index - 1] and _segment_glob_matches(
                    pattern_segment,
                    candidate_segment,
                )
        previous = current
    return previous[-1]


def _project_glob_matches(pattern: str, candidate: _CanonicalPath) -> bool:
    candidate_value = candidate.value
    normalized_pattern = pattern.replace("\\", "/")
    if not candidate.case_sensitive:
        candidate_value = candidate_value.casefold()
        normalized_pattern = normalized_pattern.casefold()
    return _segment_glob_path_matches(normalized_pattern, candidate_value)


def _structured_module_matches(actual: str, expected: str) -> bool:
    return actual == expected or actual.startswith(f"{expected}.")


def _structured_api_matches(actual: str, expected: str) -> bool:
    return actual == expected or any(
        actual.startswith(f"{expected}{separator}") for separator in (".", ":", "/")
    )


def _matches(prefix: str, value: str, context: ToolCallContext) -> bool:
    if prefix == "file":
        return any(
            _project_file_matches(value, path) for path in _context_paths(context)
        )
    if prefix == "glob":
        return any(
            _project_glob_matches(value, path) for path in _context_paths(context)
        )
    if prefix == "package":
        return any(
            _structured_module_matches(candidate, value)
            for candidate in _as_strings(context.import_names)
            if _MODULE_RE.fullmatch(candidate)
        )
    if prefix == "import":
        return any(
            _structured_module_matches(candidate, value)
            for candidate in _as_strings(context.import_names)
            if _MODULE_RE.fullmatch(candidate)
        )
    if prefix == "api":
        return any(
            _structured_api_matches(candidate, value)
            for candidate in _as_strings(context.api_names)
            if _API_RE.fullmatch(candidate)
        )
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
    "context_evidence_issues",
    "coverage_prefix",
    "evaluate",
    "parse_scope_predicate",
]
