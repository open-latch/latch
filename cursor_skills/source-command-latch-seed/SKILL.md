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
unexecuted previews require a new preview. Only after the user
approves that preview, ask them to reply exactly `/latch-seed apply`, then rerun
with `--preview-digest DIGEST --apply --yes`, using the exact returned JSON
`preview_digest`. Apply must
load the cached reviewed candidates without a second model call; a missing,
changed, or stale digest is a hard stop. The apply Shell call must again request
Cursor `required_permissions: ["all"]` on its first and only attempt because
it writes staging KB state outside the workspace and consumes a one-shot
receipt. Report inserted staging node
ids and the catch-demo command. A missing/mismatched marker is a hard stop.
