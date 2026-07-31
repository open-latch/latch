---
description: Run the local subscription-backed adversarial code-review panel
argument-hint: "[--pr N | --range BASE...HEAD | --commit REV] [--post-pr]"
---

Run the shared Latch review panel locally for `$ARGUMENTS`.

Translate a bare PR number to `--pr N`. If the user supplies no target, let the
runner detect the current branch's pull request. Use `--post-pr` only when it is
present in the user's request; publishing is never implicit.

```bash
bash "<KB_HOME>/bin/latch-review" <resolved target arguments>
```

On Windows PowerShell use the installed native wrapper:

```powershell
& "<KB_HOME>/bin/latch-review.ps1" <resolved target arguments>
```

The runner uses Claude Code's `claude.ai` subscription login and Codex CLI's
ChatGPT login. It refuses provider API keys, alternate auth tokens, endpoint
overrides, and hosted-provider toggles, then launches the specialist lanes in
parallel and saves an ignored local report inside the target repository's Git
metadata.

Show the consolidated outcome, actionable findings, complexity and
consolidation risk, coverage gaps, exact models, and saved path. Exit `1` means
the report found policy signals requiring resolution. Exit `2` means the panel
failed to run. Do not claim a provider or lane completed unless its receipt
says `completed`.
