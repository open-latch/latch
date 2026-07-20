# Agent Contract Reference

This document holds details that do not need to occupy the always-loaded Latch
contract in a project's `CLAUDE.md` or `AGENTS.md`. The managed contract is a
small bootstrap for agent behavior; tool receipts, hooks, tests, and audits are
the stronger operational surfaces.

## Managed region lifecycle

`claude_md_snippet.md` is the shared source for the managed contract.
`src/claude_md_sync.py` renders it into `CLAUDE.md`, while
`src/agents_md_sync.py` renders it into `AGENTS.md` with target-specific file,
installer, and compaction wording. Do not edit generated regions by hand.

The files are project wiring artifacts. The portable source is the snippet,
renderers, installers, and tests. Generated root `CLAUDE.md`, `AGENTS.md`, and
`.latchbak` files should not be committed from a local installation.

The managed region carries `latch-wiring-version`. A newer engine repairs an
already-wired older region once, preserves project content outside the markers,
and writes a backup. It does not create a managed region in an unmanaged
project, and it refuses to downgrade a region written by a newer engine.

## Supported host surfaces

The shared contract obligations are the same across hosts, while enforcement
and lifecycle mechanics follow each host's supported surface.

| Host | Always-loaded contract | Additional supported surfaces |
| --- | --- | --- |
| Claude Code | Managed `CLAUDE.md` region | Session hooks and Claude commands |
| Codex | Managed `AGENTS.md` region | SessionStart shim and Codex skills |
| Cursor | Managed `AGENTS.md` plus `.cursor/rules/latch.mdc` | Project commands, skills, MCP wiring, and optional hooks |

Cursor's always-applied rule should contain Cursor-only gate and operation
receipt behavior. Shared read, authority, capture, write-hint, and compaction
rules belong in `AGENTS.md` so the two surfaces do not repeat a full contract.

## Intensity and host boundary

Quiet, Standard, and Full change automatic surfacing, not the managed
read/reconcile/gate/resolve/capture/report contract. The saved choice is
install-wide and is resolved without process caching from
`latch_settings.json`, with `LATCH_INTENSITY` as an explicit environment
override. A missing setting preserves Full for older installs. A malformed
saved setting falls back to Quiet; an invalid environment override uses a
valid saved setting when one exists and otherwise falls back to Quiet.
Quickstart rejects invalid explicit input, while status/doctor surface resolver
warnings.

Claude Code can vary both its SessionStart brief and similarity-based
UserPromptSubmit surfacing. Codex can vary its SessionStart brief but has no
similarity prompt hook. Cursor with hooks can vary its SessionStart brief while
keeping its pre-edit gate; Cursor without hooks currently has no
intensity-controlled runtime surface. The managed contract remains static at
all tiers, including its live Latch read before each response. Intensity
controls hook-added briefs and prompt context, not contract-driven tool use.

When invoked, every tier uses the same gate check and configuration. Do not
upgrade that into a claim of identical evidence, catches, or outcomes: prior
automatic reads, evolving KB state, and model behavior can differ. Tier
telemetry is observational and must not enter chain assembly or classification.

## Tool discovery

Some hosts defer MCP tool schemas until first use. Batch-load the primary tools
with:

```text
ToolSearch(query="mcp__latch latch_search latch_get latch_recent latch_gate")
```

Older installs may expose legacy names:

```text
ToolSearch(query="mcp__claude-kb kb_search kb_get kb_recent kb_gate")
```

A zero-result exact lookup is not proof that Latch is unavailable. Retry broad
discovery and verify with a live search or recent call before declaring the
project unwired.

## Read authority

When a supported prompt hook injects `## KB hits`, they are similarity-ranked
teasers. Fetch the actual node before using it as evidence. No silent Latch
bypass is allowed: when a read returns no relevant rows, say so; never invent
node ids, history, verdicts, or receipts.

Every `latch_get` / `kb_get` result includes `reconciliation_banner`. A
non-empty banner means the queried node remains true in its own scope but newer
canonical framing constrains it. Fetch every `linked_id` before acting.

`supersedes` is different: the older node is stale and should not be cited as
current truth. `latch_verify` can distinguish `OK`, `RECONCILED`, `STALE`, and
`NOT_FOUND` without a model call.

Standing priorities are short user directives surfaced by SessionStart and the
gate. Overall priorities apply everywhere; workstream priorities apply when the
request resolves to that workstream. They are guidance unless a user decision
or gate verdict makes them blocking. When a user states a sweeping directive
such as "always" or "from now on," offer to capture it with
`latch_priority_add`; write it only with the user's approval.

## Gate receipts and degraded states

`latch_gate` is the judgment layer for implementation-shaped requests. It
searches current and stale nodes, walks relevant relations, and returns a
recommendation with cited evidence.

For each non-skipped result, the visible **Latch gate** block should include:

- provenance that Latch ran on the request;
- recommendation and rationale;
- cited node ids, titles, and current status;
- source or receipt basis;
- risk or better next action when present; and
- uncovered claims with their required remedy.

`MODIFY`, `DO_NOT_PROCEED`, and `NEEDS_HUMAN_JUDGMENT` are stop-and-show
results. The user resolves or explicitly overrides them before implementation
continues. `PROCEED` still requires a visible receipt.

A skipped, timed-out, budget-blocked, parse-failed, or otherwise degraded gate
is not approval. Report the state honestly. Hosts with mutation enforcement may
require a usable current-prompt receipt before any write; other hosts continue
only within their documented safety boundary.

Uncovered claims have one of three remedies: walk deeper through the graph,
trace current code or runtime evidence, or flag the claim as an explicit user
assumption.

## Foreground activity

KB tools may return `kb_activity.must_display_to_user=true`. Surface the
returned `summary` and `why_it_matters` when present. Name material reads and
the important nodes. For successful writes, report node id, kind, and title.

Tool payload correctness does not prove that the human-facing response was
compliant. Foreground receipt tests must inspect the response surface too.

## Content placement and capture

Facts, decisions, history, parameters, rejected alternatives, results,
postmortems, and gate criteria belong in Latch. Static project instructions are
for paths, file rules, agent behavior, and environment setup. Ambient facts do
not participate in heal, traversal, authority, or reconciliation.

Capture durable decisions, findings, rejected paths, and postmortems in the
same turn they occur. Capture actual user overrides and refusals with their
reason. Do not turn ordinary conversation into noisy KB rows, and do not invent
decision language the user did not ratify.

## Write hints and graph agreement

Tool-returned hints are part of the write contract:

- `plan_freshness_hint`: reflect shipped progress in the linked plan or
  workstream body.
- `claim_change_hint`: use correction flow for a changed canonical assertion
  instead of erasing history with an in-place rewrite.
- `orphan_hint`: add the matching active graph edge or remove the body id
  mention.
- `ship_edge_hint`: use implements/advances/depends_on when that is the meaning
  of the progress edge.

Use `latch_append` for workstream/progress deltas and `latch_update` for living
plan text when no canonical claim changes. Tombstone invalid edges with
`latch_unlink` so they stop affecting reads and traversal without losing the
audit trail. Once every step in a sequence plan has shipped, promote the plan to
`status="canonical"` so future audits treat it as authoritative.

When a newer canonical fact or decision narrows an older canonical node without
fully replacing it, add `older --reconciled_by--> newer`. Both remain canonical,
and the older node's banner must surface the newer constraint. Full replacement
uses `newer --supersedes--> older`, with the older node stale.

## Compaction

Latch compaction persists session judgment and spends a model call. Offer it at
natural endpoints and let the user decide.

- Claude Code: `/latch-compact`, distinct from built-in `/compact`.
- Codex: the installed `latch-compact` skill uses the Codex transcript wrapper.
- Cursor: the installed command/skill uses only the exact current
  hook-provided transcript path.

Ordinary chat compaction only trims conversation context and writes nothing to
Latch.

## Verification boundary

Rendering tests can prove footprint, required wording, target substitution, and
safe update behavior. They cannot prove that a model will follow every rule or
that graph relations will remain healthy over time.

The dedicated `agent-contract-footprint` pull-request check compares the raw
shared snippet and the rendered Claude, Codex, and Cursor always-loaded surfaces
with the PR base. Any increase in lines, words, or UTF-8 bytes—and any loosening
of an absolute budget—requires a maintainer to apply the
`agent-contract-growth-approved` label. The label acknowledges reviewed growth;
it does not bypass the current hard ceilings, and raising a ceiling is itself a
review-signaled change. Prefer replacing or relocating existing detail into
this reference, tool receipts, hooks, or tests before asking agents to carry
more text on every prompt.

The contract evidence packet therefore separates deterministic contract and
authority cases from live-host scenarios. Long-running relation-use trends and
behavioral quality belong in the later verification pass, not in the installed
markdown contract.
