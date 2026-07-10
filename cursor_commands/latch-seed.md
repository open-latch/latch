---
description: Preview and apply latch seed candidates from the current Cursor conversation
---

Seed latch from the exact current Cursor conversation recorded by the
`sessionStart` hook. This path never scans Cursor history directories or guesses
the latest chat.

First run a preview from the current project:

```bash
LATCH_SEED_BACKEND=<CURSOR_MODEL_BACKEND> \
LATCH_MODEL_BACKEND=<CURSOR_MODEL_BACKEND> \
bash <KB_HOME>/bin/latch_seed.sh --source cursor
```

```powershell
$env:LATCH_SEED_BACKEND = "<CURSOR_MODEL_BACKEND>"
$env:LATCH_MODEL_BACKEND = "<CURSOR_MODEL_BACKEND>"
& "<KB_HOME>/bin/latch_seed.ps1" --source cursor
```

Show the seed receipt and candidates. Do not write yet. If the source fails to
resolve, surface the error; do not choose a transcript from private Cursor
storage.

Only after the user approves the displayed candidates, rerun the same command
with `--apply --yes`. Report the inserted staging node ids and the generated
catch-demo command. Never add `--yes` before the user confirms the preview.
