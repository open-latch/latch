---
name: source-command-latch-review
description: Run the subscription-backed Latch code-review panel locally. Use when the user invokes $source-command-latch-review, latch-review, /latch-review, asks to send a PR, commit, or range to the Latch review panel, or requests the Codex equivalent of Claude Code's /latch-review command.
---

# Latch review

Run the shared five-lane adversarial panel through the user's Claude Code and
Codex CLI subscription logins. Do not substitute an in-chat review or claim
cross-provider coverage when a provider lane failed.

## Run

Resolve the Latch checkout, then translate the user's target into exactly one
of `--pr`, `--range`, or `--commit`. Omit the target to let the runner detect
the current branch's PR. Add `--post-pr` only when the user explicitly asks to
publish the consolidated report.

```bash
latch_home="<KB_HOME>"
if [ ! -x "$latch_home/bin/latch-review" ]; then
  latch_home="${LATCH_HOME:-${CLAUDE_KB_HOME:-}}"
fi
if [ -z "$latch_home" ] || [ ! -x "$latch_home/bin/latch-review" ]; then
  echo "Installed Latch review runner is unavailable; rerun bin/install_codex or set LATCH_HOME to a trusted Latch install." >&2
  exit 1
fi
bash "$latch_home/bin/latch-review" <resolved target arguments>
```

On Windows PowerShell, resolve the same installed Latch directory and run:

```powershell
& "<KB_HOME>/bin/latch-review.ps1" <resolved target arguments>
```

The runner must abort if a provider API key, alternate auth token, endpoint
override, or hosted-provider toggle is present. It requires Claude `claude.ai`
subscription auth and Codex `ChatGPT` auth, runs the specialist lanes in
parallel, and saves the untracked report inside the target repository's local
Git metadata.

After it finishes, summarize the panel outcome, actionable findings, complexity
risk, coverage gaps, exact models, and saved report path. A nonzero exit of `1`
means the report contains policy signals requiring resolution; exit `2` means
the runner itself failed. Never describe an uncompleted lane as reviewed.
