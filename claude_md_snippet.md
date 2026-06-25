<!--
  latch — CLAUDE.md engine-contract snippet (Tier A)
  --------------------------------------------------
  SINGLE SOURCE OF TRUTH for "how an agent must drive latch." This block is
  installed into a project's CLAUDE.md as a MANAGED REGION by
  bin/install_claude_md.{sh,ps1} (the {{KB_HOME}} placeholder is substituted
  with your latch install path, matching settings_snippet.json).

  DO NOT hand-edit the managed region in any CLAUDE.md. To change the contract,
  edit THIS file in the latch repo and re-run the installer — it overwrites the
  region in place (non-destructively; content outside the markers is preserved)
  and `install_claude_md --check` flags any drift. A direct CLAUDE.md edit would
  not propagate to the snippet or to other installs, and would be clobbered on
  the next sync.

  VERSIONED WITH THE ENGINE: when the contract changes (a new hint field, edge
  relation, or banner), this file changes in lockstep and the installer re-syncs.

  WHAT BELONGS HERE (Tier A): the irreducible bootstrap an agent needs before
  any engine signal exists, plus how to interpret the signals latch emits
  (banners, hints). Nothing here is project-specific.

  WHAT DOES NOT BELONG HERE (Tier B): your project's facts, decisions, history,
  parameters, file paths, directory ownership. Those go IN THE KB (via
  kb_insert), or OUTSIDE this managed region in CLAUDE.md — never inside the
  contract. A fact in CLAUDE.md is ambient and does not participate in heal,
  traversal, or the gate, so a stale CLAUDE.md fact silently overrides a newer
  canonical KB node.
-->

## KB usage — MANDATORY

**Before responding to any prompt, query the KB** via `kb_search`, `kb_get`, or
`kb_recent`. The KB carries the *why* behind decisions, what was ruled out,
open questions, and prior attempts — context that does not live in code or git.

**Auto-injected `## KB hits` are similarity-ranked teasers, not results.**
Treat them like seeing a filename in `git status`: they tell you something is
relevant, not what it says. Pull the actual nodes before answering.

**Batch-load schemas on the first KB call of a session.** The KB MCP tools are
deferred at session start. On the first KB call, load them in one round-trip:
`ToolSearch(query="select:mcp__latch__kb_search,mcp__latch__kb_get,mcp__latch__kb_recent")`.
Existing installs may still expose the legacy `mcp__claude-kb__*` names; if the
`mcp__latch__*` schemas are absent, retry the same selection with
`mcp__claude-kb__kb_search`, `mcp__claude-kb__kb_get`, and
`mcp__claude-kb__kb_recent`. Schemas stay loaded for the rest of the session.

**If the KB MCP tools or the SessionStart brief are missing**, latch is not yet
wired up for the current user. Follow `{{KB_HOME}}/README.md` per-user setup.

## KB visibility — MANDATORY

**Surface latch activity in the foreground.** If a KB read materially affects
your answer, briefly name the tool and important node ids/titles, or say no
relevant KB rows were found. If a KB write succeeds, say what was tracked or
updated and include the node id/kind/title when available.

Core KB tools may return a compact `kb_activity` object with
`must_display_to_user=true`; surface its `summary` in chat. Keep this to one
line unless returned hints require action. Gate calls use the stronger
`findings` block described below.

## KB content rule — MANDATORY

**Facts, decisions, framing, and history go in the KB. They do NOT go in
CLAUDE.md.**

- **Goes in KB** (`kb_insert`): parameters, architectures, decisions, "why we
  ruled X out", historical results, postmortems, gate criteria, performance
  numbers.
- **Goes in CLAUDE.md**: directory paths, file-write rules, agent behaviors
  (this snippet), session-start confirmations, environment configuration.

**Why:** a fact in CLAUDE.md is ambient context that does not participate in
heal, traversal, or the gate. When a later canonical KB node contradicts it,
nothing flags the conflict — the agent reads both, feels grounded by CLAUDE.md,
and silently picks the stale framing. If you reach for CLAUDE.md to record a
domain fact or a decision about how something behaves, write it to the KB
instead via `kb_insert(kind="fact" | "decision", ...)`.

## KB read hygiene — MANDATORY

**Every `kb_get` response includes a `reconciliation_banner` field. When
non-empty, you MUST fetch each listed node before treating the queried node's
body as authoritative.**

A node in the banner is **canonical and true in its own scope**, but its
framing has been constrained or updated by a newer canonical decision. Reading
the older node alone — and carrying its framing into new work — is the staleness
failure pattern latch exists to prevent.

```
"reconciliation_banner": [{"linked_id": <id>, "kind": ..., "title": ...}, ...]
```

Non-empty = stop, fetch each `linked_id`, read both before acting. Empty = no
reconciliation declared; the node body is current.

**Distinct from `supersedes`:** `supersedes` marks the old node `stale` and
removes it from default reads. `reconciled_by` leaves both nodes canonical — the
reader is responsible for cross-checking.

## kb_gate on coding-shaped prompts — MANDATORY

KB-first reads context. **`kb_gate` is the next layer: judgment.** Before you
commit to an implementation plan on a coding/build/implement/refactor/add/extend
prompt, call the `kb_gate` MCP tool with the user's request verbatim. It searches
the KB (including stale nodes), walks the canonical relations 1–2 hops, and
returns `{PROCEED | MODIFY | DO_NOT_PROCEED | NEEDS_HUMAN_JUDGMENT}` with cited
node ids and a recommended next action.

**Display the gate findings explicitly.** Every non-skipped `kb_gate` response
includes a `findings` object with `must_display_to_user=true`. Before you move
into native implementation narration, show a concise **Latch gate** block that
starts from provenance: **Latch ran `kb_gate` on this request / plan.** Then name
the recommendation, summary/rationale, cited KB evidence node ids/titles/status
(the status is the visible current-authority signal), receipt/source basis when
present, risk or better next action when present, and any uncovered claims. Do
this even for `PROCEED`: users should be able to see that latch supplied
judgment and evidence, not just infer it from the agent's prose.

**When to call:** the prompt asks you to write, change, add, refactor, extend,
fix, or rebuild code. Implementation-shaped intent.

**When NOT to call:** explanation, status questions, search, debugging output
already in front of you, exploratory design discussion before any code. The tool
budget is shared with the compactor and nightly heal — don't spend it on prompts
that wouldn't change behavior.

**Surface, don't auto-redirect:** if the verdict is MODIFY or DO_NOT_PROCEED,
surface the recommendation, the cited nodes, and the suggested next action to the
user before acting. The user decides whether to follow it. For `PROCEED`, still
surface the findings block, then continue normally.

**Uncovered claims:** `verdict.uncovered_claims[]` lists load-bearing claims the
gate found no backing for (it can be non-empty even on PROCEED). Before acting on
such a claim, resolve it per its engine-supplied `suggested_remedy` — `hop_deeper`
(walk the graph until it cites a node), `code_trace` (read the source, cite
`file:line`), or `flag_to_user` (surface as an explicit assumption; never silently
fill). Empty list = nothing unbacked.

**Skipped/error verdicts:** if `verdict.recommendation` is None (budget cap, kill
switch, parse failure), proceed with normal KB-first context. Don't block on a
skipped gate.

`/kb-gate "<request>"` is the manual escape hatch.

## Standing priorities — top of mind

A project may define **standing priorities** via `kb_priority_add` /
`kb_priority_list` / `kb_priority_retire` — short directives the user wants
weighed on builds (the complement of similarity retrieval; e.g. security review,
cross-platform installability). Priorities have two scopes:

- **Overall**: omit `workstream_id`; the directive applies everywhere and is
  injected into every `kb_gate` classifier prompt and the SessionStart brief.
- **Workstream**: pass `workstream_id=<workstream node id>`; the directive is
  additive guidance only when the current gate request resolves to that
  workstream. It also appears under that workstream in the brief for visibility.

Both scopes are capped at 5 active priorities by default. Weigh each in-scope
priority when planning — they are guidance to consider, not a hard gate.

**Offer to capture sweeping guidelines.** When the user states a standing
directive ("always …", "from now on …", "as a rule …"), offer to record it as a
priority so latch carries it forward. The per-prompt hook emits a deterministic
nudge on such phrasing — capture only with the user's go-ahead, never for a
task-local ask.

## KB write hygiene — MANDATORY

**Plan-freshness:** when you insert a ship/progress node linked to a plan node via
`implements`/`advances`/`depends_on`, you MUST reflect the new state in the linked
node's body (bodies get read; edges don't). `kb_insert` returns a
`plan_freshness_hint` field naming which (empty when none). Freshen each:
**workstream/progress** bodies via `kb_append(linked_id, "<line>")` (delta-only —
no full-body resend/re-embed); `kb_update` for a decision/plan body. Claim changes
to a canonical `fact`/`decision` still route through `kb_correct`, never an append.

**Promotion:** once all steps in a sequence plan have shipped, promote the plan
node to `status="canonical"` so future audits treat it as authoritative.

**Claim changes route through `kb_correct`, not a `kb_update` body rewrite:** a
change to *what a canonical `fact`/`decision` asserts* must go through
`kb_correct` (supersede/reconcile) so the old node survives as an auditable
tombstone/banner — an in-place body rewrite destroys the decision-change
transition. `kb_update` returns a `claim_change_hint` field, non-None when a
body edit on a canonical fact/decision materially shifts the embedding and is
not a pure append. It is a NUDGE, not a block: the write proceeds, but when it
fires, prefer redoing the edit via `kb_correct_plan`. `kb_update` in place stays
correct for non-claim edits (status promotion, typo, banner/cross-ref append)
and for living-summary kinds (workstream/plan/progress, per plan-freshness
above).

**`reconciled_by` mandate:** when you insert a canonical decision/fact that
updates a time scale, parameter, scope, or invariant established by an earlier
canonical node — without fully replacing it — add a `reconciled_by` edge from the
older node to the new one:

```
kb_link(src=<older_node>, dst=<new_node>, relation="reconciled_by")
```

This makes the older node's `kb_get` surface the new node as a banner. Use this
when the older fact remains true in its own scope but a newer decision
narrows / re-scopes / re-parameterises it. `reconciled_by` differs from
`supersedes`: supersedes is full replacement (old → stale); reconciled_by is
partial constraint (both canonical, with a cross-reference).

**Body-edge agreement:** when a node's body mentions another node by id
(`id=X`, "see id=X", "depends on id=X"), the corresponding active graph edge
MUST exist from this node to the referenced node. Bodies and graph cannot
disagree. When body framing introduces a reference, `kb_link`; when it
invalidates one, `kb_unlink`.

**Edge removal:** when refactoring a node's body such that an existing edge no
longer reflects its meaning, `kb_unlink(src, dst, relation)` to tombstone the
stale edge. Tombstone preserves the audit trail (the row persists with
`status='tombstoned'`) and is excluded from `kb_get` neighbors, banners, gate
traversal, and hints. Re-linking the same triple reactivates the row in place.

## Capturing & compaction — MANDATORY

The KB only pays rent if it actually accumulates the project's reasoning. Two
habits keep it filling without the user having to manage it — the user should
get value from latch without first learning to operate it.

**Capture as you go.** When a decision is made, a finding lands, an approach is
ruled out, or a postmortem is reached, write it to the KB *in that same turn*
via `kb_insert` — do not defer to the end of the task. Capture decisions and
durable findings, not running commentary: a node nobody would re-read is noise
that slows retrieval. (*What* belongs in the KB vs. CLAUDE.md is governed by
"KB content rule" above.)

**Offer to compact at natural endpoints.** `/kb-compact` summarizes the session
transcript into the KB — the other half of how the KB fills. **It is distinct
from Claude Code's built-in `/compact`**, which only trims the conversation
context window and writes nothing to the KB — only `/kb-compact` persists the
session, so always recommend it by name. It is user-
initiated by design: it spends a model call, so it never auto-runs. When you
reach a natural endpoint — a task completes, a working session winds down, or a
long context risks being lost before it is captured — **offer to run it** and
let the user decide. Detecting the endpoint is your job; confirming the action
is theirs. Do not let a substantive session end silently un-compacted.
