<!--
  latch managed agent contract for CLAUDE.md.

  Source of truth: claude_md_snippet.md. Installed by
  bin/install_claude_md.{sh,ps1}; do not hand-edit. Re-run it to sync.
  Details: {{KB_HOME}}/docs/agent-contract-reference.md.
-->

## Latch Contract — Mandatory

Latch is the project's decision-continuity layer. Keep project facts and
decisions in Latch; receipts and enforcement provide stronger guarantees.

Use this checkpoint order: **read → reconcile → gate → resolve → capture →
report**. No silent Latch bypass and no invented Latch evidence.

### 1. Read and establish authority

Before responding to any prompt, make a live Latch read. Normally use
`latch_search`, `latch_get`, or `latch_recent` (legacy: `kb_search`,
`kb_get`, `kb_recent`). For "where did we leave off?", "catch me up", "resume",
or "what's next", use `latch_project_direction(compact=true)` as the initial
read, add `latch_recent(kind="progress", limit=3)` for raw chronology, then
use `latch_get` on the report's `foregrounded_item.id`.
Auto-injected `## KB hits` are teasers; fetch the full node before relying on
it. If no useful row was found, say so instead of inventing ids or history.

On the first KB call, batch-load schemas:
`ToolSearch(query="mcp__latch latch_search latch_get latch_recent latch_project_direction latch_gate")`.
If absent, try legacy discovery:
`ToolSearch(query="mcp__claude-kb kb_search kb_get kb_recent kb_project_direction kb_gate")`.
Treat an exact zero-result lookup as non-definitive; verify with a live search
or recent call. If tools are missing, follow `{{KB_HOME}}/README.md` setup.

Every `latch_get` / `kb_get` has `reconciliation_banner`. When non-empty,
fetch every `linked_id` and read both nodes. `reconciled_by` keeps both true in scope;
`supersedes` makes the older node stale. Weigh
priorities from `latch_priority_list` or the gate.
For sweeping directives, offer `latch_priority_add`; capture only with user approval.

### 2. Gate and resolve implementation work

Call `latch_gate` once on the user's current request verbatim before materially
changing code/config/test/runtime behavior or docs implementing the request. Skip
explanation/status/search/diagnosis/read-only review/audit, priorities/planning/
handoffs/Latch/report/admin work. Gate planning when implementation begins.
Reuse it for unchanged substeps/verification/narration; re-gate only material
scope changes. Legacy: `kb_gate`; honor host enforcement.

Show each non-skipped **Latch gate** first: recommendation/rationale; cited
ids/titles/status; receipt/source; risk/action; uncovered claims.

Stop `MODIFY`/`DO_NOT_PROCEED`/`NEEDS_HUMAN_JUDGMENT` for user resolution/
override; show PROCEED receipt. Fix gaps: hop_deeper/code_trace/flag_to_user.
Skipped/degraded/no verdict is not approval; report it.

### 3. Report and capture judgment

When `kb_activity.must_display_to_user=true`, show its `summary` and
`why_it_matters` when present. Name material reads and important node ids;
report successful writes with node id/kind/title. Show any
`kb_activity.lifecycle_notice` text too.

Capture durable decisions, findings, rejected paths, postmortems, and actual
user overrides or refusals in the same turn with `latch_insert` or
`latch_capture_decision` when appropriate. Do not capture routine chatter.

Project facts and decisions belong in Latch—not in the managed host file.
Static project instructions may hold paths, file rules, agent behavior, and
setup.

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
