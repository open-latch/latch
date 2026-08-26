# Predicate verdict v1

`predicate-v1` is the deterministic, zero-LLM verdict contract for compiled
`rejected_path.scope_predicate` checks. It is a skeleton for later policy-hook
integration; it does not query Latch, call a model, consult a budget, or use the
network.

## Input context

`ToolCallContext` has five optional fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `tool_name` | string or null | Name of the proposed tool. |
| `file_paths` | sequence of strings or null | Files named by the tool call. |
| `command_text` | string or null | Command or source text available at the tool boundary. |
| `diff_paths` | sequence of strings or null | Files changed by an available diff. |
| `import_names` | sequence of strings or null | Parsed or caller-supplied module names. |

Missing fields are empty evidence. They never become a match by inference.

## Compilation

Predicates are parsed once at the first `:` into a lower-case type and a
trimmed value.

| Prefix | Deterministic check |
| --- | --- |
| `file:` | Component-aware path equality or containment over `file_paths` and `diff_paths`. |
| `glob:` | Case-sensitive `fnmatch` over slash-normalized file and diff paths. |
| `package:` | Package/module-component match over `import_names` and module-shaped paths. |
| `import:` | Exact/submodule match over `import_names` and Python import statements in `command_text`. |
| `api:` | Token-bounded API-identifier match over command text, tool name, and import names. |

`feature:`, the observed bare category values, unknown prefixes, malformed
values, empty strings, and `NULL` compile to `UncompilableCheck` with an explicit
`uncompilable_reason`. An uncompilable check never matches and is never silently
dropped.

## Output

Every evaluation returns exactly this JSON-compatible shape:

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

The allowed `decision` values are `block`, `flag`, and `pass`:

- `block`: one or more compiled predicates match. `matches` contains only the
  matching rows, in evaluation order.
- `pass`: no compiled predicate matches. `matches` is empty; an uncompilable
  predicate alone does not become a speculative match.
- `flag`: reserved for the later policy-integration layer. The v1 deterministic
  skeleton does not emit it by guessing that an uncompilable predicate applies.

Every match carries both `rejected_path_id` and `node_id`, so a PreToolUse
consumer can act on a verdict without a Latch read in the enforcement loop.
`llm_calls` is always the integer `0`.
