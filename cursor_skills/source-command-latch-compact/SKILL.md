---
name: source-command-latch-compact
description: Compact the exact current Cursor conversation into latch. Use when the user invokes latch-compact or /latch-compact in Cursor.
---

# Latch compact for Cursor

Latch operation id: latch-compact run

Latch Cursor skill boundary: use only the conversation id and
`transcript_path` recorded together by the current `sessionStart` hook. Never
scan Cursor history or fall back to Claude/Codex transcripts.

Read the exact `Latch Cursor current session id` from the current prompt context;
the `beforeSubmitPrompt` hook re-injected it from this chat's payload. Pass it
as the wrapper's first positional argument. Never omit it or reuse an id from
another chat.

Before the Shell call, read the workspace `.cursor/mcp.json` and use the exact
absolute `mcpServers.latch.command` as `LATCH_PYTHON`. Never fall back to a
PATH `python3`; the MCP interpreter owns latch's native dependencies.

Resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}` when set, otherwise use the
absolute checkout in the project-sync footer. Use backend `cursor` for a plugin
install; a project-synced skill uses the backend in that footer. Run the host-appropriate
`bin/run_cursor_compact_now.sh` or `.ps1` wrapper with
`LATCH_COMPACTOR_BACKEND` and `LATCH_MODEL_BACKEND` set to that backend. Do not
pass `--final`. The first Shell call must request Cursor
`required_permissions: ["all"]`, because compaction writes latch-owned
budget/session/KB state outside the open workspace. Do not make a sandboxed
attempt first and retry with permission: the managed compact receipt is
one-shot and consumed by the first exact attempt. Cursor's normal user-approval
flow still applies. Wait for the JSON result and report `summary_node_id`,
`summary_written`, extracted-node count, and `current_session_only`. Surface any
resolution error without choosing another transcript.
