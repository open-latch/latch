---
name: source-command-latch-pm
description: Seed one ruled-out decision and show latch catch it later. Use when the user invokes latch-pm or /latch-pm in Cursor.
---

# Latch PM proof for Cursor

Latch operation id: latch-pm prepare

Latch Cursor skill boundary: this workflow is safe for project-synced skills
and the Cursor plugin.

Read latch first. Ask for one concrete approach the user already ruled out and
why, sharpen at most once, then show the exact staging decision before writing.
Write only after showing the exact staging decision and asking the user to
reply exactly `/latch-pm apply`. That explicit operation confirmation permits
one staging decision `latch_insert`; link the current
workstream when known. Invite the user to trigger the rejected path in a fresh
conversation and run `latch_gate` verbatim so the saved reason appears before
edits. Keep the proof framed as decision continuity, not generic memory. Never
auto-write or pretend a same-turn keyword demo is the production gate.
