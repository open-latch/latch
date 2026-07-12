---
description: Preview and apply latch seed candidates from the current Cursor conversation
---

Latch operation id: latch-seed preview

Seed latch from the exact current Cursor conversation recorded by the
`sessionStart` hook. This path never scans Cursor history directories or guesses
the latest chat.

Read the exact `Latch Cursor current session id` from the current prompt context;
the `beforeSubmitPrompt` hook re-injected it from this chat's payload. Substitute
it for `<CURSOR_SESSION_ID>` below. Do not use an id from another chat or omit
the argument.

Read the workspace `.cursor/mcp.json` and take the exact absolute `command`
from `mcpServers.latch` as `<CURSOR_MCP_PYTHON>`. Set `LATCH_PYTHON` to
that interpreter on every Shell call below. Do not fall back to a PATH
`python3`: the MCP interpreter owns latch's native dependencies.

First run a preview from the current project:

On that first Shell call, request Cursor `required_permissions: ["all"]`.
The wrapper writes latch-owned budget/session state outside the open workspace,
so Cursor's normal sandbox cannot complete the preview. Do not try the
sandboxed call first and then retry: the managed preview receipt is one-shot
and is consumed by the first exact attempt. This permission request still uses
Cursor's normal user-approval flow; it does not authorize seed apply.

```bash
LATCH_PYTHON="<CURSOR_MCP_PYTHON>" \
LATCH_SEED_BACKEND=<CURSOR_MODEL_BACKEND> \
LATCH_MODEL_BACKEND=<CURSOR_MODEL_BACKEND> \
bash <KB_HOME>/bin/latch_seed.sh --source cursor --cursor-session-id "<CURSOR_SESSION_ID>" --format json
```

```powershell
$env:LATCH_PYTHON = "<CURSOR_MCP_PYTHON>"
$env:LATCH_SEED_BACKEND = "<CURSOR_MODEL_BACKEND>"
$env:LATCH_MODEL_BACKEND = "<CURSOR_MODEL_BACKEND>"
& "<KB_HOME>/bin/latch_seed.ps1" --source cursor --cursor-session-id "<CURSOR_SESSION_ID>" --format json
```

Show the seed receipt, `preview_digest`, and candidates. Do not write yet. If the source fails to
resolve, surface the error; do not choose a transcript from private Cursor
storage.

The preview attempt does not arm apply. Only the matching successful JSON tool
result recorded by `postToolUse` does. A failed, malformed, missing, or
cross-session result requires a new preview.

Only after the user approves the displayed candidates, ask them to reply
exactly `/latch-seed apply`, then rerun the same JSON command with
`--preview-digest "<PREVIEW_DIGEST>" --apply --yes`. Use the exact digest
from the approved preview. Apply loads that cached candidate set and makes no
second model call; a missing, changed, or stale digest must fail closed.
On this apply Shell call, again request Cursor
`required_permissions: ["all"]` on the first and only attempt: apply writes
staging KB state outside the workspace, and its one-shot receipt cannot be
retried after a sandbox denial.
Report the inserted staging node ids and the generated
catch-demo command. Never add `--yes` before the user confirms the preview.
