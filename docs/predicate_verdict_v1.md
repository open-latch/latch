# Predicate verdict v1

`predicate-v1` is the exact deterministic verdict produced by the private A2
compiled-policy evaluator. The four top-level keys are stable:

```json
{
  "engine": "predicate-v1",
  "decision": "block",
  "llm_calls": 0,
  "matches": [
    {
      "rejected_path_id": 41,
      "node_id": 741,
      "option": "synthetic rejected option",
      "predicate": "file:src/synthetic_widget.py",
      "reason": "synthetic rejection reason",
      "source": "declared"
    }
  ]
}
```

The core verdict is private. A match can contain policy text, so callers must
not write the complete verdict to logs, CI output, or JSON stdout. The policy
consumer returns a separate `predicate-policy-receipt-v1` projection for those
surfaces. The receipt retains structural ids, counts, digests, and reason codes
without option, reason, predicate, action text, or path values.

## Canonical action envelope

The policy consumer constructs `ToolCallContext` from structured host-neutral
evidence:

| Field | Meaning |
| --- | --- |
| `policy_domain_id` | Explicit opaque project-policy domain. It is never inferred from cwd or repository text. |
| `project_root` | Absolute lexical project root for path containment. |
| `cwd` | Absolute cwd inside that root. |
| `tool_name` | Name of the proposed operation. |
| `proposed_file_paths` | Complete proposed path footprint. |
| `diff_paths` | Complete changed-path evidence when relevant. |
| `staged_paths` | Complete staged-path evidence when relevant. |
| `import_names` | Structured module/import identifiers. |
| `api_names` | Structured API identifiers. |
| `evidence_complete` | Explicit completeness attestation. |
| `evidence_provenance` | Non-empty structural provenance supplied by the host adapter. |
| `command_text` | Optional opaque local detail; never parsed as import, package, or API evidence. |

`file_paths` remains a low-level compatibility alias for the earlier skeleton.
A canonical policy action uses `proposed_file_paths`; conflicting values flag.
The five path/name collections must each be present as sequences; an empty
sequence is valid evidence. `evidence_complete` must be literal `true`.
Missing, malformed, foreign-root, traversal-bearing, conflicting, or incomplete
evidence produces aggregate-safe reason codes and prevents a compiled-policy
pass. Even with `evidence_complete: true`, an entirely empty mutation footprint
flags as `mutation_footprint_missing`; opaque command text is not a substitute
for structured evidence.

## Deterministic checks

| Prefix | Canonical check |
| --- | --- |
| `file:` | Exact project-relative path or directory-segment containment over proposed, diff, and staged paths. |
| `glob:` | Project-relative segment glob. `*` never crosses `/`; `**` is recursive and must occupy a whole path segment. |
| `package:` | Exact package or submodule match over structured `import_names` only. |
| `import:` | Exact import or submodule match over structured `import_names` only. |
| `api:` | Exact API identifier or member-prefix match over structured `api_names` only. |

Paths are normalized lexically against the explicit root. POSIX matching is
case-sensitive. Windows drive and UNC roots use Windows lexical containment
and case-insensitive comparison. Windows action segments ending in an ASCII
period or space, or containing an NTFS alternate-data-stream colon, flag as
noncanonical instead of being compared under a misleading lexical spelling.
Absolute predicates and predicates containing `..`, `.`, or empty path
segments do not compile; `file:.` is explicitly uncompilable rather than an
implicit whole-project rule. Dot-prefixed names such as `.config` remain
valid. Comments, quoted strings, shell text, filenames, and other prose never
supply package/import/API evidence.

Glob patterns are limited to 4,096 characters and 256 path segments. Action
paths, roots, and working directories are limited to 4,096 characters. An
over-limit predicate is uncompilable; over-limit action evidence flags. Glob
matching uses iterative segment state and never a backtracking regular
expression.

Unsupported or malformed predicates compile to `UncompilableCheck` with an
explicit private explanation. They remain accounted for but never match.

## Decisions

- `block` means at least one current, domain-bound, binding compiled rule
  matched. `matches` contains the private matching rows in policy order.
- `pass` means the policy domain, fresh snapshot, and action footprint are
  complete and no binding compiled rule matched. It is a compiled-policy pass,
  not proof that advisory or uncompilable rulings permit the action.
- `flag` means the evidence is indeterminate or unsafe to claim pass/block.
  Missing or stale policy state and malformed/incomplete action evidence flag.

A valid binding match still blocks when some additional evidence is incomplete;
an issue changes a no-match from pass to flag. Advisory and uncompilable
residuals do not create a global flag storm. Their aggregate counts and reason
codes appear separately in the redacted receipt.

`llm_calls` is always the integer `0`; the policy import path has no model,
budget, semantic-search, gate, SQLite, or network dependency.

The module retains the old five-field construction surface for skeleton-level
unit compatibility. That low-level mode must not be used as an authoritative
policy decision. The complete consumer always supplies the canonical envelope.
