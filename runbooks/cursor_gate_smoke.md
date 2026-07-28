# Cursor gate smoke proof

Use this runbook to prove the Cursor runtime path: Cursor can see latch through
MCP, use its own Agent CLI for model calls, enforce a `latch_gate` receipt
before coding-shaped edits, and manually compact only the current hooked
conversation. Historical transcript discovery is outside this proof.

## Success criteria

- `.cursor/mcp.json` registers the project `latch` MCP server.
- `.cursor/rules/latch.mdc` exists and tells Cursor to run `latch_gate` before
  implementation-shaped edits.
- `.cursor/commands/` contains latch-owned command prompts including
  `/latch-compact` and `/latch-seed`.
- `.cursor/skills/` contains the non-conflicting `source-command-*` workflow
  skills, and the checkout's `.cursor-plugin/plugin.json` validates.
- `AGENTS.md` carries the full shared latch contract.
- `.cursor/hooks.json` contains latch session, per-prompt gate-enforcement, and
  activity hooks with fail-closed prompt/mutation entries.
- `latch_cursor_doctor.sh` passes static checks and its read-only native Cursor
  backend probe.
- If Cursor's `agent` CLI is available, the doctor confirms critical tools:
  `latch_search`, `latch_get`, `latch_recent`, and `latch_gate`.
- In Cursor chat, a violating implementation request produces a visible
  **Latch gate** block before file edits.
- A mutation attempted before that receipt is denied; the same mutation can
  reach Cursor's normal permission flow after an exact-request gate receipt.
- `/latch-compact` resolves the exact current SessionStart marker and
  transcript, writes a rolling summary, and reports `current_session_only`.

## Install in the target project

Run from the project repo where Cursor should follow latch:

```bash
/path/to/latch/bin/install_cursor.sh --yes --with-hooks
/path/to/latch/bin/install_cursor.sh --check --with-hooks
/path/to/latch/bin/latch_cursor_doctor.sh --with-hooks
```

Run `agent login` first if Cursor Agent is not authenticated. Authentication
does not approve project MCP servers: separately approve the `latch` server in
Cursor when Cursor prompts. If `agent mcp list` reports `needs approval` or
`not approved`, static wiring exists but live MCP/gate acceptance is still
blocked. Before testing chat, open **Cursor Settings > Tools & MCP**, select the
current workspace, enable **latch**, confirm it reports **tools enabled** (the
exact count can grow with the MCP surface), and start a
fresh Agent chat. CLI `ready` and `agent mcp list-tools latch` output are
CLI-only proof and do not establish that the IDE workspace toggle is enabled.
Do not edit Cursor's private state or invent an undocumented CLI command to
bypass this user trust action. Use
`--model-backend claude` or `--model-backend codex` only to exercise an explicit
compatibility backend instead of native Cursor.

A one-time latch wiring repair after an engine upgrade may legitimately update
`.cursor/mcp.json`. Cursor can revoke the workspace toggle when that file
changes. When the installer, doctor, or Latch activity reports a repair,
re-enable **latch** in **Cursor Settings > Tools & MCP**, reconfirm it reports
**tools enabled**, and start a fresh Agent chat. A second task must not rewrite
current wiring.

Expected files in the target project:

```text
.cursor/mcp.json
.cursor/hooks.json
.cursor/rules/latch.mdc
.cursor/commands/latch-gate.md
.cursor/commands/latch-compact.md
.cursor/commands/latch-seed.md
.cursor/skills/source-command-latch-gate/SKILL.md
.cursor/skills/source-command-latch-seed/SKILL.md
AGENTS.md
```

Restart Cursor, or run:

```bash
agent mcp list
agent mcp list-tools latch
```

If `agent` is unavailable, the doctor reports a warning for the live CLI probe.
That warning does not invalidate the static install proof. If `agent` is
available but the server is unapproved, retain that as a separate user-action
gap. If approval succeeds and `latch_gate` is still missing from `list-tools`,
the install is not ready. If the IDE Agent reports that latch is unavailable
despite a successful CLI probe, inspect the workspace toggle rather than
repeatedly calling `latch_gate`: the gate cannot mint a receipt while its server
is absent. Static doctor success alone is not live visible-gate, native-backend,
plugin, or compaction acceptance.

Project-local command prompts should be visible from Cursor's `/` command menu
after reload. They are reusable prompts that call MCP tools or the checked-in
host-appropriate shell wrappers.

## Seed a proof target

From the hooked Cursor conversation, run `/latch-seed`. It must preview the
exact current transcript before any write. Copy the exact session id shown in
the current prompt context by `beforeSubmitPrompt` (the same id recorded
silently by SessionStart). Confirm that the preview tool completed successfully
and returned JSON; a preToolUse authorization alone must not arm apply. After
approving the preview, reply exactly `/latch-seed apply` before the generated
command reruns with `--apply --yes`. The equivalent preview is:

```bash
/path/to/latch/bin/latch_seed.sh --source cursor --cursor-session-id SESSION_ID --format json
```

Running without an explicit session id and its matching per-session marker must
fail unless the user supplied an explicit, readable `--cursor-transcript` path.
An unreadable explicit path must also fail.
For older Claude/Codex sources, use `--source both` separately.

Pick one concrete rejected path, governance rule, or prior agent mistake from
the seed report. Good proof fuel names a forbidden approach and the accepted
redirect.

For `/latch-pm`, confirm that Cursor calls the read-only `latch_pm_preview`
tool and displays its complete candidate/digest result before asking for
`/latch-pm apply`. Change the title, body, status, kind, links, or workstream in
a test apply and confirm the insert is denied; the exact previewed candidate
must succeed only once. A prose-only preview is not acceptance evidence.

For a no-history smoke, use the fixture path:

```bash
/path/to/latch/bin/latch_demo_no_history.sh --backend codex --keep
```

The no-history demo proves latch can build the gate evidence. The Cursor proof
still needs Cursor to show the receipt before edits.

## Prompt Cursor

Open the target project in Cursor and ask for the violating implementation. Use
a direct prompt, for example:

```text
Implement the rejected path from the seed report: <paste the forbidden approach/request>.
```

Expected Cursor behavior:

1. Cursor reads latch context through MCP when relevant.
2. Before the first implementation mutation, Cursor calls `latch_gate` with
   the request verbatim. Planning-only discussion does not gate. A rephrased
   request does not arm the mutation hook.
3. Cursor shows a foreground block shaped like:

   ```text
   Latch gate
   Latch ran the gate on this request.
   Recommendation: MODIFY or DO_NOT_PROCEED
   Summary: <why the request conflicts with saved evidence>
   Evidence:
   - id=<n> <kind> status=<status>: <title>
   Better next action: <compliant redirect>
   ```

4. Only after that receipt should Cursor continue, ask for confirmation, or
   implement the redirected path.

If Cursor tries to edit first, the `preToolUse` hook should deny the tool and
tell the agent to run `latch_gate` verbatim. If a file changes anyway, the smoke
proof failed.

### Check the contract boundary

Run these as separate smoke cases and record the visible tool-call count:

| Case | Prompt sequence | Expected general `latch_gate` calls |
| --- | --- | ---: |
| Implementation | `Change <small source/config/test/runtime behavior or implementing doc>.` | 1, before mutation |
| Non-implementation | `Explain the change, report status, then hand it off without editing.` | 0 |
| Latch capture | Run `/latch-pm` to prepare the decision, then `/latch-pm apply`. | 0; preview/apply uses its narrower operation receipt |
| Same scope | In one prompt, request an implementation plus its tests and verification. | 1 for the unchanged request |
| Scope change | After the first gated change, request a materially different implementation. | 1 new call on the changed request |

For Cursor, a later mutating prompt must still satisfy the current-prompt hook,
even when the broader task is unchanged. Do not count that host enforcement as
permission to gate the preceding explanation, handoff, or Latch-only prompt.

## Verify no pre-gate edits

Use git before and after the prompt:

```bash
git status --short > /tmp/latch-cursor-before.txt
# Run the Cursor prompt and wait for the visible Latch gate block.
git status --short > /tmp/latch-cursor-after.txt
diff -u /tmp/latch-cursor-before.txt /tmp/latch-cursor-after.txt
```

The diff should be empty until after the gate receipt appears. Also test these
negative cases; each mutation must remain denied:

- call `latch_gate` with a rephrased request,
- force a skipped/error gate result,
- wrap an inner `OK` gate result in `isError=true`, `success=false`, or a
  nonzero exit code, or in cancellation, timeout, denial, skip, or negative
  `ok`/completion state,
- combine contradictory tool aliases, input containers, servers, or nested
  tool names, including a native `Read` alias mixed with generic `MCP` evidence,
- submit a second prompt and try to reuse the prior receipt,
- invoke a mutation hook with missing/invalid input.

For an explicit latch slash workflow, also arm a normal `PROCEED` gate receipt
and prove that it still cannot authorize a missing-preview seed apply, changed
PM candidate, alternate launcher, script, project, or argument shape. Those
operations use an exclusive narrow receipt lane.

## Prove current-session compaction

From the same Cursor conversation, run `/latch-compact`. The command delegates
to the host-appropriate wrapper with the exact prompt-context session id
and must return JSON containing:

```json
{
  "ok": true,
  "current_session_only": true,
  "summary_written": true,
  "summary_node_id": 123
}
```

Then require the marker/transcript pair explicitly:

```bash
/path/to/latch/bin/latch_cursor_doctor.sh --with-hooks --require-compact
```

Negative proof matters: remove or alter the marker's `transcript_path`, pass a
different session id, or pass a different transcript path. Each attempt must
fail without scanning Cursor storage or falling back to Claude/Codex history.

Retain the live proof under a dated directory outside the repo. Save Cursor
version, doctor JSON, sanitized hooks config, before/after git status, the
visible gate block, and the denial message. Do not save raw prompt history or
private Cursor storage.

## Uninstall smoke

Preview removal without touching unrelated Cursor config:

```bash
/path/to/latch/bin/uninstall.sh --dry-run --cursor-only --cursor-project "$PWD"
```

Apply removal only when you mean to remove latch from this Cursor project:

```bash
/path/to/latch/bin/uninstall.sh --yes --cursor-only --cursor-project "$PWD"
/path/to/latch/bin/uninstall.sh --check --cursor-only --cursor-project "$PWD"
```

The uninstall path removes latch-owned Cursor MCP entries, the clean managed
Cursor rule, latch-owned Cursor command prompts, skills, and hook entries, and the
managed `AGENTS.md` region. It preserves unrelated `.cursor/mcp.json`
servers/settings, unrelated hooks, unrelated `.cursor/commands`, and unrelated
`.cursor/skills` files.

## Boundaries

Cursor is MCP plus Cursor Rules, project-local Cursor commands and skills,
`AGENTS.md`, a
native read-only Agent CLI model backend, and opt-in session/gate/activity
hooks. Current-session manual compaction depends only on the explicit
SessionStart handoff.

It deliberately does not install:

- Cursor transcript discovery

The optional plugin manifest distributes the workflow skills only. It does not
replace the installer-owned MCP/rule/hook runtime.
