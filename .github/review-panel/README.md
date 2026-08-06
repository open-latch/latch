# Local Latch review panel

The panel runs independent Claude and Codex reviewers against one immutable
commit range, validates their structured receipts, and writes one deduplicated
local report. It does not run in GitHub Actions and does not use provider API
keys.

## Run it

From the repository to review, set `LATCH_HOME` to the app path printed by the
installer (or to a source checkout), then run:

```bash
bash "$LATCH_HOME/bin/latch-review" --pr 73
bash "$LATCH_HOME/bin/latch-review" --range main...HEAD
bash "$LATCH_HOME/bin/latch-review" --commit HEAD
```

Windows PowerShell uses the native wrapper:

```powershell
& "$env:LATCH_HOME\bin\latch-review.ps1" --pr 73
```

The installed Claude command and Codex skill contain the resolved app path, so
normal in-agent use does not depend on a persistent environment variable.

With no scope argument, the runner attempts to detect the current branch's pull
request. Add `--post-pr` to a PR review only when you want the consolidated
report published as a sticky GitHub comment. Immediately before posting, the
runner verifies that the PR still points to the reviewed head SHA and refuses
to replace the comment if the PR advanced. The GitHub CLI is required only for
`--pr`, automatic PR detection, and `--post-pr`; an explicit `--range` or
`--commit` review without posting requires only Git and never invokes `gh`.

Claude Code users can run `/latch-review`; Codex users can ask to “send PR 73
to the Latch review panel.” Both host integrations call the same runner.

## Authentication and account-usage boundary

The runner requires:

- Claude Code authenticated through `claude.ai` with a subscription
- Codex CLI authenticated with `ChatGPT`
- Git for every review
- GitHub CLI authentication only for PR resolution, automatic PR detection, or
  an explicit `--post-pr`

The runner resolves both provider executables once to absolute real paths and
uses those exact paths for authentication checks and every review lane. If more
than one CLI is installed, select one explicitly with an absolute `CLAUDE_BIN`
or `CODEX_BIN`. Before starting any lane, it records both versions and verifies
from the selected Codex binary's offline `debug models --bundled` catalog that
`gpt-5.6-sol` supports `high` effort. An incompatible binary fails once during
preflight. A second local-only preflight runs the exact strict lane config with
an intentionally invalid output schema, proving the selected binary accepts the
permission and tool-isolation contract before any inference can begin. The
runner never silently downgrades the model or effort.

Before resolving the review scope or invoking a model, it fails closed if a
provider API key, alternate auth token, endpoint override, or hosted-provider
toggle is present. This includes `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, `ANTHROPIC_BASE_URL`, and
`OPENAI_BASE_URL`. Those variables are also removed from every child-process
environment. This prevents ambient configuration from silently changing the
account-usage or evidence destination.

Codex review lanes run with isolated temporary `HOME` and `CODEX_HOME`
directories so user, project, and bundled skills cannot enter the reviewer
prompt. The runner copies the short-lived access/identity token bundle and
account metadata required by Codex into that directory with mode `0600`; it
never copies the reusable refresh token. The access token must have at least
35 minutes remaining immediately before lanes start, and the isolated state is
deleted when the run ends.

The panel consumes the user's Claude and Codex subscription allowances.
Subscription auth plus the environment guard prevents API-key metering. The
CLIs cannot prove whether account-level extra usage, purchased credits, or
auto-top-up is enabled. Disable those account settings if you want a hard stop
at the included allowance; the runner prints this limitation before every
review.

## Pinned reviewers

The local runner records these values in every report's `metadata.json`:

| Provider | Model | Effort | Lanes |
|---|---|---|---|
| Claude | `claude-opus-5` | `high` | correctness/concurrency, security/abuse |
| Codex | `gpt-5.6-sol` | `high` | regressions/tests, architecture/portability, simplicity/consolidation |
| Codex | `gpt-5.6-sol` | `high` | artifact/output when user-facing paths change |

The simplicity/consolidation lane is mandatory. Every lane also reports net
complexity delta, structural surfaces, consolidation opportunities, and the
simplest credible alternative.

## Execution boundary

The runner:

- resolves a PR to its merge-base and exact head SHA
- fetches PR ancestry into a temporary bare repository, then binds review to
  the merge-base and exact head; the fetch has a five-minute timeout and never
  writes refs or `FETCH_HEAD` in the user's repository
- builds bounded prompts containing a path-prioritized diff, bounded head
  context, identifier matches, and path indexes once per immutable scope, then
  reuses that exact evidence frame for every applicable specialist lane; core
  runner, policy, installer, and regression-test changes receive evidence
  budget before peripheral files
- runs all applicable lanes in parallel with shell, web, connectors, MCP, and
  subagents disabled; Codex receives explicit `web_search="disabled"` and
  `tools.web_search=false` configuration plus a strict custom permission
  profile that grants only minimal runtime reads, denies tool reads from the
  generated workspace, and disables network access; this prevents an exposed
  local-image tool from reading review or user files even on Codex builds that
  cannot disable that tool by configuration; Claude receives an empty tool
  list plus a strict empty MCP configuration
- treats changed source and artifact bytes as untrusted prompt evidence
- never checks out or executes reviewed project code
- runs the conditional artifact/output lane against the same immutable static
  evidence; conclusions requiring a build or rendered output become coverage
  gaps because project code and recipes are never executed; path-owned runtime
  evidence requirements remain explicit coverage gaps but do not turn an
  otherwise clean static review into a permanent nonzero exit
- validates each receipt with the shared schema and aggregates with the shared
  deterministic policy; the provider-facing schema deliberately uses JSON
  Schema draft-07 and avoids regex lookaround, while trusted post-generation
  validation separately enforces repository-relative finding paths
- live-caps each provider stdout/stderr stream; very large PR ancestry can still
  consume temporary disk during the initial Git fetch before evidence caps
  apply; interrupt, timeout, and output-limit paths terminate the isolated
  provider process groups

Raw provider output, normalized receipts, `report.md`, `summary.json`, and
model/auth metadata are saved inside the repository's local Git metadata
(normally
`.git/latch/reviews/<run>/`), so it cannot be committed.

Exit `0` means the panel completed without a policy signal. Exit `1` means the
report contains a blocker, required human resolution, or an incomplete
required lane. Exit `2` means scope, authentication, or execution failed before
a trustworthy result could be produced. Exit `3` means a trustworthy local
report was produced but the explicitly requested GitHub publication failed;
`metadata.json` preserves both the review result and posting error.

## GitHub configuration

No provider Actions secrets or model variables are required. Do not configure
`OPENAI_API_KEY` or `ANTHROPIC_API_KEY` for this panel. There is no automatic
PR or push trigger and no required status check; starting a review is always a
local user action.

When migrating from the retired Actions panel, remove `AI Review Panel /
panel-policy` (and any `panel-schema` entry) from required repository checks.
Also delete unused `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` Actions secrets plus
the old `REVIEW_PANEL_*`, `CODEX_REVIEW_*`, and `CLAUDE_REVIEW_MODEL` variables.
