<!--
  latch managed agent contract for CLAUDE.md.

  Source of truth: claude_md_snippet.md. This region is installed by
  bin/install_claude_md.{sh,ps1}; do not hand-edit it in a project file.
  Re-run the installer to sync it. Detailed procedures live in
  {{KB_HOME}}/docs/agent-contract-reference.md.
-->

## Latch Contract — Mandatory

Latch is the project's decision-continuity layer. This file is soft bootstrap,
not the enforcement engine: keep project facts and decisions in Latch, and rely
on tool receipts, hooks, tests, and audits for stronger guarantees.

Use this checkpoint order: **read → reconcile → gate → resolve → capture →
report**. No silent Latch bypass and no invented Latch evidence.

### 1. Read and establish authority

Before responding to any prompt, query Latch with `latch_search`, `latch_get`,
or `latch_recent` (legacy: `kb_search`, `kb_get`, `kb_recent`). Auto-injected
`## KB hits` are teasers, not authority; fetch the full node before relying on
it. If no useful row was found, say so instead of inventing ids or history.

On the first KB call in a session, batch-load schemas:
`ToolSearch(query="mcp__latch latch_search latch_get latch_recent latch_gate")`.
If absent, try legacy discovery:
`ToolSearch(query="mcp__claude-kb kb_search kb_get kb_recent kb_gate")`.
Treat an exact zero-result lookup as non-definitive; verify with a live search
or recent call. If Latch or the SessionStart brief is missing, follow
`{{KB_HOME}}/README.md` setup.

Every `latch_get` / `kb_get` result has `reconciliation_banner`. When non-empty,
fetch every `linked_id` and read both nodes before acting. `reconciled_by` keeps
both nodes true in scope; `supersedes` makes the older node stale. Weigh
priorities from SessionStart or the gate. For sweeping directives, offer
`latch_priority_add`; capture only with user approval.

### 2. Gate and resolve implementation work

Before committing to an implementation plan for write/change/add/refactor/fix
work, call `latch_gate` with the user's request verbatim (legacy: `kb_gate`).
Skip it for pure explanation, status, search, or exploratory discussion.

For every non-skipped result, show a concise **Latch gate** block before normal
implementation narration: say Latch ran the gate; show the recommendation,
rationale, cited node ids/titles/status, receipt/source basis, risks or better
next action, and uncovered claims.

Stop and show `MODIFY`, `DO_NOT_PROCEED`, or `NEEDS_HUMAN_JUDGMENT`; continue
only after the user resolves or explicitly overrides it. On `PROCEED`, still
show the receipt. Resolve uncovered claims by their suggested remedy:
`hop_deeper`, `code_trace`, or `flag_to_user`.

If a gate is skipped, degraded, or has no recommendation, report that honestly
and never treat it as approval. Follow any host enforcement that blocks mutation.

### 3. Report and capture judgment

When a tool returns `kb_activity.must_display_to_user=true`, show its `summary`
and `why_it_matters` when present. Name material reads and important node ids;
report successful writes with node id/kind/title.

Capture durable decisions, findings, rejected paths, postmortems, and actual
user overrides or refusals in the same turn with `latch_insert` or
`latch_capture_decision` when appropriate. Do not capture routine chatter.

Facts, decisions, framing, history, parameters, rejected alternatives, results,
and gate criteria belong in Latch—not in the managed host file. Static project instructions
may hold paths, file rules, agent behavior, and environment setup.

### 4. Honor write and graph hints

Follow tool-returned hints before moving on: `plan_freshness_hint` means freshen
the linked plan/workstream; `claim_change_hint` means use
`latch_correct_plan` / `latch_correct_apply` instead of rewriting a canonical
claim; `orphan_hint` means add the matching edge or remove the id mention; and
`ship_edge_hint` means use implements/advances/depends_on as appropriate.

When a newer canonical fact/decision narrows an older one without replacing it,
link older → newer with `reconciled_by`. Tombstone invalid edges with
`latch_unlink`. Prefer `latch_append` for workstream/progress deltas and
`latch_update` for living plan text when no canonical claim changes. Promote a
fully shipped sequence plan to `status="canonical"`.

### 5. Compact deliberately

{{LATCH_COMPACTION_TEXT}}
