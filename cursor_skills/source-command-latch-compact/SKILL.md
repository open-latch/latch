---
name: source-command-latch-compact
description: Compact the exact current Cursor conversation into latch. Use when the user invokes latch-compact or /latch-compact in Cursor.
---

# Latch compact for Cursor

Latch Cursor skill boundary: use only the conversation id and
`transcript_path` recorded together by the current `sessionStart` hook. Never
scan Cursor history or fall back to Claude/Codex transcripts.

Resolve `latch_home` as `${CURSOR_PLUGIN_ROOT}` when set, otherwise use the
absolute checkout in the project-sync footer. Use backend `cursor` for a plugin
install; a project-synced skill uses the backend in that footer. Run the host-appropriate
`bin/run_cursor_compact_now.sh` or `.ps1` wrapper with
`LATCH_COMPACTOR_BACKEND` and `LATCH_MODEL_BACKEND` set to that backend. Do not
pass `--final`. Wait for the JSON result and report `summary_node_id`,
`summary_written`, extracted-node count, and `current_session_only`. Surface any
resolution error without choosing another transcript.
