# Cursor gate smoke proof

Use this runbook to prove the Cursor adapter path: Cursor can see latch through
MCP, is activated by a project rule, has project-local latch command prompts,
and enforces a `latch_gate` receipt before coding-shaped edits. This is not a
native Cursor backend, transcript-discovery, or native compaction proof.

## Success criteria

- `.cursor/mcp.json` registers the project `latch` MCP server.
- `.cursor/rules/latch.mdc` exists and tells Cursor to run `latch_gate` before
  implementation-shaped edits.
- `.cursor/commands/` contains latch-owned command prompts for supported manual
  workflows, excluding native Cursor compaction.
- `AGENTS.md` carries the full shared latch contract.
- `.cursor/hooks.json` contains latch session, per-prompt gate-enforcement, and
  activity hooks with fail-closed prompt/mutation entries.
- `latch_cursor_doctor.sh` passes static checks.
- If Cursor's `agent` CLI is available, the doctor confirms critical tools:
  `latch_search`, `latch_get`, `latch_recent`, and `latch_gate`.
- In Cursor chat, a violating implementation request produces a visible
  **Latch gate** block before file edits.
- A mutation attempted before that receipt is denied; the same mutation can
  reach Cursor's normal permission flow after an exact-request gate receipt.

## Install in the target project

Run from the project repo where Cursor should follow latch:

```bash
/path/to/latch/bin/install_cursor.sh --yes --with-hooks --model-backend codex
/path/to/latch/bin/install_cursor.sh --check --with-hooks --model-backend codex
/path/to/latch/bin/latch_cursor_doctor.sh --with-hooks --model-backend codex
```

Use `--model-backend claude` instead if Claude is the configured gate backend.

Expected files in the target project:

```text
.cursor/mcp.json
.cursor/hooks.json
.cursor/rules/latch.mdc
.cursor/commands/latch-gate.md
AGENTS.md
```

Restart Cursor, or run:

```bash
agent mcp list
agent mcp list-tools latch
```

If `agent` is unavailable, the doctor reports a warning for the live CLI probe.
That warning does not invalidate the static install proof. If `agent` is
available and `latch_gate` is missing from `list-tools`, the install is not
ready.

Project-local command prompts should be visible from Cursor's `/` command menu
after reload. They are reusable prompts that call MCP tools or shell wrappers;
they do not install native Cursor compaction.

## Seed a proof target

Use the same seed-first path as the hook proof runbook:

```bash
/path/to/latch/bin/latch_seed.sh --source both --last-sessions 20 --apply
```

Pick one concrete rejected path, governance rule, or prior agent mistake from
the seed report. Good proof fuel names a forbidden approach and the accepted
redirect.

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
2. Before presenting an implementation plan or editing files, Cursor calls
   `latch_gate` with the request verbatim. A rephrased request does not arm the
   mutation hook.
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
  nonzero exit code,
- combine contradictory tool aliases, input containers, servers, or nested
  tool names,
- submit a second prompt and try to reuse the prior receipt,
- invoke a mutation hook with missing/invalid input.

For an explicit latch slash workflow, also arm a normal `PROCEED` gate receipt
and prove that it still cannot authorize a missing-preview seed apply, changed
PM candidate, alternate launcher, script, project, or argument shape. Those
operations use an exclusive narrow receipt lane.

Retain the live proof under a dated directory outside the repo. Save Cursor
version, doctor JSON, sanitized hooks config, before/after git status, the
visible gate block, and the denial message. Do not save raw prompt history or
private Cursor storage.

## Uninstall smoke

Preview removal without touching unrelated Cursor config:

```bash
/path/to/latch/bin/uninstall.sh --dry-run --cursor-project "$PWD"
```

Apply removal only when you mean to remove latch from this Cursor project:

```bash
/path/to/latch/bin/uninstall.sh --yes --cursor-project "$PWD"
/path/to/latch/bin/uninstall.sh --check --cursor-project "$PWD"
```

The uninstall path removes latch-owned Cursor MCP entries, the clean managed
Cursor rule, latch-owned Cursor command prompts and hook entries, and the
managed `AGENTS.md` region. It preserves unrelated `.cursor/mcp.json`
servers/settings, unrelated hooks, and unrelated `.cursor/commands` files.

## Boundaries

Cursor adapter is MCP plus Cursor Rules, project-local Cursor commands,
`AGENTS.md`, and opt-in session/gate/activity hooks.

It deliberately does not install:

- native Cursor model-backed gate calls
- Cursor transcript discovery
- native Cursor compaction
- plugins or skills packaging

If the design-partner proof needs any of those, scope a follow-up PR from the
observed failure rather than broadening this smoke path.
