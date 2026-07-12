---
name: source-command-latch-pm
description: Seed one ruled-out decision and show latch catch it later. Use when the user invokes latch-pm or /latch-pm in Cursor.
---

# Latch PM proof for Cursor

Latch operation id: latch-pm prepare

Latch Cursor skill boundary: this workflow is safe for project-synced skills
and the Cursor plugin.

Read latch first. Ask for one concrete approach the user already ruled out and
why, sharpen at most once, then construct one exact staging decision. Call the
read-only `latch_pm_preview` MCP tool with the complete structured candidate:
`kind`, `title`, `body`, `status`, normalized `links`, and `workstream_id` when
known. The tool result is the approval card: it displays the exact candidate,
performs no write, and returns the canonical digest that `postToolUse` binds.
Do not substitute agent prose for this tool result.

Only after that preview succeeds, ask the user to reply exactly
`/latch-pm apply`. On that later turn, call `latch_insert` once with the exact same fields;
do not add artifacts, a session override, or changed links. Harmless JSON key
or link ordering is normalized, but any load-bearing field change is denied.
Link the current workstream through the previewed fields when known. Invite the
user to trigger the rejected path in a fresh
conversation and run `latch_gate` verbatim so the saved reason appears before
edits. Keep the proof framed as decision continuity, not generic memory. Never
auto-write or pretend a same-turn keyword demo is the production gate.
