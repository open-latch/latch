---
name: source-command-latch-seed
description: Preview and apply latch seed candidates from the exact current Cursor conversation. Use when the user invokes latch-seed or /latch-seed in Cursor.
---

# Latch seed for Cursor

Latch operation id: latch-seed preview

Latch Cursor skill boundary: use only the current `sessionStart` marker or a
transcript path the user explicitly supplied. Never scan private Cursor history
directories.

Read the exact `Latch Cursor current session id` from the current prompt context;
the `beforeSubmitPrompt` hook re-injected it from this chat's payload. Pass
`--cursor-session-id ID` to the wrapper. Never omit it or reuse an id from
another chat.

Before any Shell call, read the workspace `.cursor/mcp.json` and use
`mcpServers.latch.env.LATCH_PYTHON` when present, otherwise
`mcpServers.latch.command`, as `<CURSOR_MCP_PYTHON>` and `LATCH_PYTHON`. Never fall back to a
PATH `python3`; the MCP interpreter owns latch's native dependencies.
Use `latch_home` only to construct the absolute wrapper path. Do not export
`LATCH_HOME` or `CLAUDE_KB_HOME` in the Shell call; managed Cursor operation
receipts do not allow those environment assignments.

Resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}` when set, otherwise use the
absolute checkout in the project-sync footer. Select native `cursor` for plugin
installs or the backend in that footer. Run the host-appropriate `bin/latch_seed.sh` or `.ps1`
wrapper with `--source cursor --format json` and the seed/model backend
environment set. First run preview-only and show the receipt/candidates. The
first Shell call must request Cursor `required_permissions: ["all"]`, because
the wrapper writes latch-owned budget/session state outside the open workspace.
Do not make a sandboxed attempt first and retry with permission: the managed
preview receipt is one-shot and consumed by the first exact attempt. Cursor's
normal user-approval flow still applies, and this does not authorize apply. The
attempt alone does not arm apply: only a matching successful JSON result
verified by `postToolUse` does. Failed, malformed, missing, cross-session, or
unexecuted previews require a new preview. Only after the user reviews that
preview, ask for an exact selection-bearing confirmation:

- Whole report: `/latch-seed apply all`
- Reject the whole nonempty report: `/latch-seed apply none`
- Scoped: `/latch-seed apply <ID> [<ID> ...]`, with every approved `cand-...`
  and/or `cluster-...` review ID as a separate space-delimited token, for
  example `/latch-seed apply cand-0123456789ab cluster-abcdef012345`.

Bare `/latch-seed apply`, prose standing in for IDs, duplicate IDs, and
`all`/`none` mixed with IDs are invalid. The managed receipt validates every
scoped ID against the bound preview and records only that exact selection.
`none` is valid only after a successful preview containing at least one
candidate.

For whole-report confirmation, rerun with
`--preview-digest DIGEST --apply --yes`. For reject-all confirmation, rerun with
`--preview-digest DIGEST --apply --dismiss-all`; never add `--yes` or an
approval selector. For scoped confirmation, omit `--yes` and translate exactly
the confirmed tokens to `--approve-candidate ID` and/or `--approve-cluster ID`;
do not add, remove, or substitute IDs. Unselected items are dismissed for those
exact source revisions. Use the exact returned JSON `preview_digest`. Apply
must load the cached reviewed candidates without a second model call; a
missing, changed, or stale digest, malformed confirmation, or unknown ID is a
hard stop. The apply Shell call must again request Cursor
`required_permissions: ["all"]` on its first and only attempt because it writes
staging KB state outside the workspace and consumes a one-shot receipt. Report
inserted staging node ids and the catch-demo command. A missing/mismatched
marker is a hard stop.
