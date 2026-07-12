---
description: Preview and apply latch seed candidates from the current Cursor conversation
---

Latch operation id: latch-seed preview

Seed latch from the exact current Cursor conversation recorded by the
`sessionStart` hook. This path never scans Cursor history directories or guesses
the latest chat.

Read the exact `Latch Cursor session id` from the current SessionStart context
and substitute it for `<CURSOR_SESSION_ID>` below. Do not use an id from another
chat or omit the argument.

First run a preview from the current project:

```bash
LATCH_SEED_BACKEND=<CURSOR_MODEL_BACKEND> \
LATCH_MODEL_BACKEND=<CURSOR_MODEL_BACKEND> \
bash <KB_HOME>/bin/latch_seed.sh --source cursor --cursor-session-id "<CURSOR_SESSION_ID>" --format json
```

```powershell
$env:LATCH_SEED_BACKEND = "<CURSOR_MODEL_BACKEND>"
$env:LATCH_MODEL_BACKEND = "<CURSOR_MODEL_BACKEND>"
& "<KB_HOME>/bin/latch_seed.ps1" --source cursor --cursor-session-id "<CURSOR_SESSION_ID>" --format json
```

Show the seed receipt and candidates. Do not write yet. If the source fails to
resolve, surface the error; do not choose a transcript from private Cursor
storage.

The preview attempt does not arm apply. Only the matching successful JSON tool
result recorded by `postToolUse` does. A failed, malformed, missing, or
cross-session result requires a new preview.

Only after the user approves the displayed candidates, ask them to reply
exactly `/latch-seed apply`, then rerun the same JSON command with `--apply --yes`.
Report the inserted staging node ids and the generated
catch-demo command. Never add `--yes` before the user confirms the preview.
