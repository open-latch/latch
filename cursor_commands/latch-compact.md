---
description: Compact the current Cursor conversation into latch
---

Run a rolling compaction for the current Cursor conversation against the
per-project latch KB under `<KB_HOME>`.

This path is fail-closed. It accepts only the session id and `transcript_path`
recorded together by the current Cursor `sessionStart` hook. It never searches
Cursor databases, guesses the latest chat, or falls back to Claude/Codex
transcripts.

Run the host-appropriate wrapper from the current project:

```bash
LATCH_COMPACTOR_BACKEND=<CURSOR_MODEL_BACKEND> \
LATCH_MODEL_BACKEND=<CURSOR_MODEL_BACKEND> \
bash <KB_HOME>/bin/run_cursor_compact_now.sh
```

```powershell
$env:LATCH_COMPACTOR_BACKEND = "<CURSOR_MODEL_BACKEND>"
$env:LATCH_MODEL_BACKEND = "<CURSOR_MODEL_BACKEND>"
& "<KB_HOME>/bin/run_cursor_compact_now.ps1"
```

The native Cursor Agent CLI summarizer is the default. Do not pass `--final`
for a normal manual compact; the rolling session summary stays staging. Wait
for the JSON result, then report `summary_node_id`, `summary_written`, and the
number of extracted nodes. If resolution fails, surface the error rather than
choosing another transcript.
