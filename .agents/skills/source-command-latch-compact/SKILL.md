---
name: source-command-latch-compact
description: Manually trigger a latch KB compaction for the current Codex session. Use when the user invokes $source-command-latch-compact, latch-compact, /latch-compact, asks to compact the current Codex session into latch, or wants the Codex equivalent of Claude Code's /latch-compact command.
---

# source-command-latch-compact

Use this skill when the user asks to run the migrated source command
`latch-compact` from Codex.

## Command Template

Run a compaction pass for the current Codex session against the per-project
knowledge base managed by the selected latch checkout.

Use the Codex-specific wrapper. Do **not** use
`bin/run_compact_now.sh` from Codex: that wrapper is Claude-shaped and may
select a `~/.claude/projects` transcript. The Codex wrapper resolves the
session from the first argument or `$CODEX_THREAD_ID`, validates the matching
`~/.codex/sessions/**/rollout-*.jsonl` by `session_meta.payload.id`, and fails
closed when it cannot prove the transcript.

This combines/overwrites the prior session_summary node with the latest
state (see `src/compactor.py`). By default the Codex wrapper uses the
Codex-native `codex exec` summarizer backend. Pass `--summarizer claude` only
when intentionally testing the legacy shared Claude CLI backend.

Steps:

1. Prefer the current Codex session id from `$CODEX_THREAD_ID`. If the
   environment does not expose it, ask the user for the Codex session id and
   pass it as the first positional argument. Do not infer from Claude
   transcripts.

2. Launch the Codex compactor with the Bash tool. Prefer `--background --wait`
   unless the user explicitly asks to keep the shell in the foreground or to
   fire-and-forget:

   ```bash
   latch_home="${LATCH_HOME:-}"
   if [ -z "$latch_home" ]; then
     latch_home="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
   fi
   if [ ! -x "$latch_home/bin/run_codex_compact_now.sh" ]; then
     echo "Could not find bin/run_codex_compact_now.sh; set LATCH_HOME to your latch checkout." >&2
     exit 1
   fi
   bash "$latch_home/bin/run_codex_compact_now.sh" --background --wait
   ```

   The parent process validates the Codex transcript before detaching, then
   waits for the child to write its final JSON line to the run's log slice.
   Stdout should include `ok`, `background`, `pid`, `log_path`,
   `summarizer_backend`, `transcript_path`, and, on success, `summary_node_id`.
   Use bare `--background` only when the user explicitly wants a fire-and-forget
   launch.
   Omit `--final` — manual `/latch-compact` is a rolling compact, not a
   session end. The session_summary node stays in `staging` status.

3. While the compactor is running, the selected latch checkout's project lock at
   `projects/<sanitized-cwd>/compactor.lock` is held.
   Any `kb_insert` / `kb_update` / `kb_link` / `kb_unlink` calls made by the
   main session will block for up to 60s waiting for the compactor to finish,
   then return `{"ok": false, "reason": "compaction_in_progress",
   "retry_after_s": 10}` if it still has not finished. Retry the write once; if
   it is still locked, ask the user whether to retry again or investigate.

4. When the shell returns, read stdout and report whether the final child JSON
   has `ok: true` and which `summary_node_id` was updated. Also include the PID,
   `log_path`, `summarizer_backend`, and `transcript_path`. The transcript path
   should be under `~/.codex/sessions`, never `~/.claude/projects`.

If anything fails, check `compactor.log` in the selected latch checkout.
