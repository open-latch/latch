---
description: Manually trigger a KB compaction for the current session
---

Run a compaction pass for the current Claude Code session against the
per-project knowledge base under `<KB_HOME>`.

The compaction runs in a separate `claude -p` subprocess that takes its
own context — it does NOT consume tokens from this session. We start it
in the background so the main session stays interactive while compaction
runs.

This combines/overwrites the prior session_summary node with the latest
state (see `src/compactor.py`).

Steps:

1. The wrapper targets the invoking session automatically: it reads
   `$CLAUDE_CODE_SESSION_ID` (set by Claude Code on the Bash subprocess)
   and selects that session's transcript explicitly — a concurrent
   session's newer transcript is never picked. An explicit session id can
   be passed as the first positional arg to compact a different session.

2. Start the compactor with the Bash tool **in the background** so the
   main session is not blocked while compaction runs (a full compact can
   take 30–90s):

   ```
   Bash(
     command="bash <KB_HOME>/bin/run_compact_now.sh",
     run_in_background=true,
     description="Run KB compaction in background"
   )
   ```

   Return control to the user immediately with the background shell id.
   Do not poll — the harness will notify you when the subprocess exits.
   Omit `--final` — manual `/kb-compact` is a rolling compact, not a
   session end. The session_summary node stays in `staging` status.

3. While the compactor is running, the project lock at
   `<KB_HOME>/projects/<sanitized-cwd>/compactor.lock` is held. Any
   `kb_insert` / `kb_update` / `kb_link` / `kb_unlink` calls made by the
   main session will block for up to 60s waiting for the compactor to
   finish, then return `{"ok": false, "reason": "compaction_in_progress",
   "retry_after_s": 10}` if it still hasn't finished. This is the
   deterministic write-side serialization — the busy payload's `message` tells you
   what to do: retry the write once, and if it is still locked, ask the
   user whether to retry again or investigate. Don't chase the lock.

4. When the harness notifies that the background shell completed, fetch
   its stdout and report which `summary_node_id` was updated and a
   one-line gist.

If anything fails, check `<KB_HOME>/compactor.log`.
