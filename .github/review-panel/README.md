# AI review panel

This workflow runs independent Claude and Codex reviewers against the same
immutable commit range, normalizes their structured receipts, and publishes one
deduplicated report. Reviewers cannot modify the repository or post comments.
The conditional artifact lane consumes evidence generated in a separate,
credential-free job.

Pull requests run on open, reopen, every synchronized head commit, and the
ready-for-review transition, including while the pull request is a draft.

The pull-request trigger is `pull_request_target`, so GitHub loads the workflow
definition from the trusted target branch rather than from the reviewed head.
The pull request that first installs this panel is therefore intentionally
dormant: the panel becomes active for subsequent pull requests after the
installation PR merges. Do not add a fallback that executes the control script
from the reviewed head.

## Lanes

- Claude: correctness/concurrency and security/abuse
- Codex: regressions/tests, architecture/portability, simplicity/consolidation
- Codex, when user-facing paths change: generated artifact/output review

Every lane reports the net complexity delta, structural surfaces added,
consolidation opportunities, and the simplest credible alternative.

The default panel makes five model calls per pull-request revision and a sixth
when user-facing paths change. Concurrency cancellation stops an older run when
a newer head commit arrives.

## Trust boundary

The repeated jobs are intentional security boundaries:

- orchestration, prompts, schemas, and aggregation run from the trusted base
  commit while the reviewed head lives in `.review-target`
- credentialed provider jobs keep `.review-target` as a bare Git object store,
  not a checked-out PR worktree; reviewers inspect immutable diffs and blobs
  with read-only Git commands
- reviewed files are inspected statically; credentialed reviewer jobs do not
  execute project code, tests, scripts, build tools, or package managers
- provider jobs have read-only repository permissions and cannot publish their
  own comments or edits
- artifact simulation runs without provider credentials
- only the deterministic aggregate job can update the sticky report

The control script uses the Python standard library and adds no project runtime
dependency.

## Required repository configuration

Add these Actions secrets:

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`

Provider steps are deliberately disabled when the reviewed head belongs to a
fork, even though `pull_request_target` can access target-repository secrets.
The workflow still starts and records unavailable lanes without passing
provider credentials to those steps. A maintainer can review the fork and run
the panel manually against trusted commits.

Optional Actions variables:

- `CODEX_REVIEW_CLI_VERSION`: defaults to the reviewed `0.145.0` CLI release
- `CODEX_REVIEW_MODEL`: leave unset to use the action default
- `CODEX_REVIEW_EFFORT`: defaults to `high`
- `CLAUDE_REVIEW_MODEL`: leave unset to use the action default
- `REVIEW_PANEL_ENFORCEMENT`: `advisory` by default; set to `enforce` after the
  shadow period
- `REVIEW_PANEL_REQUIRE_ALL_LANES`: `false` by default; set to `true` when
  provider availability should be merge-blocking

Enforced mode always requires at least one completed Claude lane, at least one
completed Codex lane, and the dedicated simplicity/consolidation lane. The
`REVIEW_PANEL_REQUIRE_ALL_LANES` variable controls whether every other
applicable specialist lane must also complete.

After the advisory period, make `AI Review Panel / panel-policy` a required
status check in the repository ruleset.

## Manual commit-range review

The workflow can review commits before a pull request:

```bash
gh workflow run ai-review-panel.yml \
  -f base_sha="$(git rev-parse HEAD^)" \
  -f head_sha="$(git rev-parse HEAD)"
```

The report is written to the workflow summary and uploaded as an artifact.
Pull-request runs also update one sticky panel comment.

## Updating action pins

All external actions are pinned to immutable commit SHAs. In particular, the
provider pins are the dereferenced commits behind their annotated `v1` tags,
not the mutable tag names or tag-object hashes. Review upstream release notes
and update both the SHA and version comment deliberately.
