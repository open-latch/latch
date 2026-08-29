# Background model policy

Latch never delegates a background model choice to a host CLI. Every gate,
compaction, heal arbitration, and tree-summary subprocess receives an explicit
model argument. Public defaults are provider-specific:

| Backend | Default | Primary override |
| --- | --- | --- |
| Claude | `sonnet` | `LATCH_CLAUDE_MODEL` |
| Codex | `gpt-5` | `LATCH_CODEX_MODEL` |
| Cursor | `gpt-5` | `LATCH_CURSOR_MODEL` |

## Resolution order

Purpose-specific selectors win first, followed by maintenance-wide and generic
selectors, then the backend's public default. Legacy provider-prefixed selectors
remain supported where they already existed.

| Purpose | Claude | Codex | Cursor |
| --- | --- | --- | --- |
| Gate classifier and adversary | `LATCH_GATE_CLAUDE_MODEL` | `LATCH_GATE_CODEX_MODEL`, `CODEX_GATE_MODEL` | `LATCH_GATE_CURSOR_MODEL`, `CURSOR_GATE_MODEL` |
| Compaction | `LATCH_COMPACTOR_CLAUDE_MODEL` | `LATCH_COMPACTOR_CODEX_MODEL`, `CODEX_COMPACTOR_MODEL` | `LATCH_COMPACTOR_CURSOR_MODEL`, `CURSOR_COMPACTOR_MODEL` |
| Heal arbitration | `LATCH_HEAL_CLAUDE_MODEL` | `LATCH_HEAL_CODEX_MODEL`, `CODEX_HEAL_MODEL` | `LATCH_HEAL_CURSOR_MODEL`, `CURSOR_HEAL_MODEL` |
| Tree summaries | `LATCH_TREE_CLAUDE_MODEL` | `LATCH_TREE_CODEX_MODEL`, `CODEX_TREE_MODEL` | `LATCH_TREE_CURSOR_MODEL`, `CURSOR_TREE_MODEL` |

After the purpose-specific entries, Claude falls back through
`LATCH_MAINTENANCE_CLAUDE_MODEL` and `LATCH_CLAUDE_MODEL`. Codex falls back
through `LATCH_MAINTENANCE_CODEX_MODEL`, `CODEX_MAINTENANCE_MODEL`, and
`LATCH_CODEX_MODEL`. Cursor falls back through
`LATCH_MAINTENANCE_CURSOR_MODEL`, `LATCH_CURSOR_MODEL`, and `CURSOR_MODEL`.

For example, `LATCH_CLAUDE_MODEL=opus` selects Opus for every Claude-backed
background call, while `LATCH_TREE_CLAUDE_MODEL=haiku` changes only tree
summaries. The equivalent Codex and Cursor selectors follow the table above.

## Detached maintenance and failures

Shared connections validate model selectors as non-secret policy. A detached
maintenance child receives the complete selector set for its selected backend,
but it never receives connection credentials.

Empty or otherwise invalid model policy fails before launching a model process.
Deterministic heal work does not validate model configuration: integrity and
deterministic reconciliation continue, and validation occurs only when a pair
actually reaches the LLM boundary. An incomplete model-bound heal remains due
instead of advancing the detached scheduler's heal timestamp.

## Telemetry

Gate and heal structural records use `model`. Compaction status uses
`summarizer_model`. Tree results use `model`, and text maintenance logs include
the selected model without recording request text.
