# Policy enforcement reference

This page documents the A4.1 C1-C3 enforcement surfaces that are present in
the source tree. They are a host-neutral capability contract, a shared policy
check, and an immutable Git-range check. Their presence in a checkout does not
prove that a host hook or CI job is installed, active, or blocking operations.

Private policy snapshots must remain local to their controlling machine. See
[Predicate policy snapshot v1](./predicate_policy_snapshot_v1.md) for the
snapshot lifecycle and [Predicate verdict v1](./predicate_verdict_v1.md) for
the underlying deterministic evaluator.

## Capability declarations

`latch.enforcement.capability` parses an exact, closed declaration under the
`latch-capability-declaration-v1` contract. A declaration records:

- host id, minimum host version, and hook event;
- one claimed mode: `enforce`, `observe`, or `ci-only`;
- intercepted and non-intercepted tool coverage;
- deny mechanisms, plus the exact canonical `allow` and `deny` vocabulary;
- timeout basis, behavior, receipt observation, and fixture ids;
- platform caveats and corpus fixture ids; and
- whether a local or self-hosted second line is required.

This synthetic declaration shows the exact nested shape:

```json
{
  "contract": "latch-capability-declaration-v1",
  "host_id": "example-host",
  "minimum_host_version": "1.2.3",
  "hook_event": "pre_tool_use",
  "deny_mechanisms": ["permission-decision-deny"],
  "claimed_mode": "enforce",
  "tool_coverage": {
    "intercepted": ["local-side-effecting-tools"],
    "not_intercepted": ["hosted-tools"],
    "complete": true
  },
  "timeout": {
    "basis": "empirical",
    "behavior": "fail-open",
    "receipt_on_timeout": false,
    "fixture_ids": ["example-timeout-posix-v1"]
  },
  "platform_caveats": ["native-windows-pending"],
  "vocabulary": ["allow", "deny"],
  "corpus_fixture_ids": ["example-pre-tool-use-corpus-v1"],
  "second_line_required": true
}
```

Timeout basis is one of `documented`, `empirical`, or `unknown`; timeout
behavior is one of `fail-open`, `fail-closed`, or `unknown`. The timeout driver
must return exactly three booleans: `timed_out`, `action_continued`, and
`receipt_observed`.

The schema requires `native-windows-pending` in the platform caveats and
requires `second_line_required` to be true. A Codex declaration cannot set a
minimum version below `0.124.0`. `ask` is not a load-bearing decision or deny
mechanism.

Claiming `enforce` is not enough to make enforcement effective.
`assess_capability(...)` changes the effective mode to `observe` unless all of
the following are true:

- runtime host id matches the declaration and its version meets or exceeds the
  declared minimum;
- Codex is at or above the declared floor and does not use the legacy
  `features.codex_hooks` flag;
- tool coverage is declared complete;
- the declaration records an empirical timeout basis, and a structurally
  valid, digest-consistent timeout receipt matches the host, version, hook,
  platform, fixture, timeout behavior, and receipt behavior;
- the evidence reports a complete, non-empty corpus observation with a
  declared fixture id and a syntactically valid SHA-256 manifest digest; and
- the required second line is available with `local` or `self-hosted` scope.

The assessment returns the effective mode, an `enforce_ready` boolean, and
stable reason codes for unmet conditions. It does not install host wiring.

The empirical timeout helper in `latch.enforcement.timeout_harness` accepts a
host-supplied driver and seals the observation returned by that driver in a
content-free `latch-timeout-probe-v1` receipt. It does not induce a host
timeout or independently attest what happened. Its SHA-256 digest detects
changes to the structural fields; it is not a signature or provenance
attestation. The receipt carries no action or policy text.

## Shared policy check

`latch.enforcement.core.check_policy(...)` evaluates one structured action
against one digest- and freshness-validated policy snapshot and returns a
`PolicyCheckResult`. That result carries the outcome, decision, exit code,
denial and exemption booleans, and a redacted
`latch-policy-check-receipt-v1` receipt. The shared outcome and process-exit
semantics are:

| Outcome | Exit | Reported result for side effects in `enforce` or `ci-only` |
| --- | ---: | --- |
| `pass` | `0` | `denied: false` |
| `block` | `10` | `denied: true` |
| `flag` | `20` | `denied: true`; non-success in CI |
| `invalid` | `30` | `denied: true`; represented by decision `flag` |

`denied` and the exit code are outputs for caller wiring to enforce. The core
does not itself interrupt a host tool; an integration must stop the operation
when the contract requires it.

In `observe` mode, `pass`, `block`, and `flag` all exit `0` and do not deny the
operation. `invalid` remains exit `30`, although it does not set `denied` in
observe mode. A pass removes only this Latch veto; it is not a general approval
from every other control.

Every call binds to one exact subject shape:

- `host-action`: repository, host, session, and action ids; or
- `git-range`: repository id plus full lowercase 40- or 64-character base and
  head commit ids.

For a `host-action`, `action_id` is a caller-owned identity token. The subject
digest binds the supplied subject tokens; neither it nor the receipt digest
hashes or attests the complete action contents.

The only read-only exemption is an action with exactly `policy_domain_id` and
`tool_name: latch.policy.inspect`, paired with `effect: read-only`. Labeling any
other action read-only produces an invalid result instead of bypassing policy
evaluation.

Receipts contain structural metadata: contract and engine versions, adapter
id and version, mode, effect, outcome, exit, denial and exemption booleans,
policy domain, snapshot digest, freshness token, the normalized subject and
its digest, row counts, matched structural ids, aggregate reason counts, and a
receipt digest. They omit policy text, predicate text, action text, action
paths, snapshot paths, and repository filesystem paths.

The receipt digest is a deterministic hash over the receipt fields. It is not
a signature or an identity attestation.

After a denied result, `recheck_after_snapshot_refresh(...)` permits a caller
to evaluate a newly published snapshot only for the same subject, adapter,
mode, effect, and policy domain. It does not publish or mutate a snapshot. A
successful retry adds the prior receipt digest as `retry_of`; any context
mismatch produces an invalid result.

### Reference executable

`bin/latch_policy_check.py` accepts exactly one snapshot path argument, reads
one JSON request from standard input, writes only the redacted receipt to
standard output, and returns the outcome exit code.

The outer JSON object must contain exactly `action`, `subject`, `adapter`,
`mode`, and `effect`; `adapter` must contain exactly `id` and `version`. Modes
are `enforce`, `observe`, or `ci-only`, and effects are `read-only` or
`side-effecting`. Extra or missing keys produce an invalid result.

```sh
python3 bin/latch_policy_check.py /private/runtime/policy.snapshot.json <<'JSON'
{
  "action": {
    "policy_domain_id": "example-domain",
    "project_root": "/work/example",
    "cwd": "/work/example",
    "tool_name": "example.write",
    "proposed_file_paths": ["src/example.py"],
    "diff_paths": [],
    "staged_paths": [],
    "import_names": [],
    "api_names": [],
    "evidence_complete": true,
    "evidence_provenance": ["example-host-v1"]
  },
  "subject": {
    "kind": "host-action",
    "repository_id": "example-repository",
    "host_id": "example-host",
    "session_id": "example-session",
    "action_id": "example-action"
  },
  "adapter": {"id": "example-host", "version": "1.0.0"},
  "mode": "enforce",
  "effect": "side-effecting"
}
JSON
```

The input can contain private coordinates needed for local evaluation. Callers
must protect that input even though the emitted receipt is redacted.

## Immutable Git-range check

`latch.enforcement.ci.check_ci_policy(...)` is a generic consumer for committed
Git trees. It accepts a snapshot path, repository root, opaque repository id,
full base and head commit ids, policy domain id, runner scope, and snapshot
kind.

The check verifies that both ids resolve exactly to the supplied commit
objects, then derives the changed path set with `git diff-tree`. Moving refs,
abbreviated ids, replacement objects, the current branch tip, dirty files, and
untracked files cannot change a run bound to the same base and head ids. The
module does not fetch commits, upload a snapshot, access the network, or
publish a remote check.

Git evidence is all-or-nothing. The parser accepts NUL-delimited, strict UTF-8
relative paths, capped at 2 MiB of raw output and 100,000 candidate paths. Git
object and diff operations each use a 60-second timeout. Rename detection is
disabled, so a rename is evaluated as deletion plus addition and both path
names are evidence. Oversized, truncated, malformed, unavailable, timed-out,
or otherwise incomplete evidence produces `flag` and a non-zero exit instead
of evaluating a partial path set.

The generic Git-range check evaluates changed path names, not file content. It
retains already-binding compiled `file:` and `glob:` checks in the binding set.
Already-binding `package:`, `import:`, and `api:` checks become aggregate
advisory residual counts and do not independently fail this check. Snapshot
freshness and the declared policy domain are still validated before
evaluation.

The consumer applies the declared runner and snapshot privacy rule as follows:

- `local` and `self-hosted` may consume digest- and freshness-validated private
  or synthetic snapshots;
- `hosted` rejects private snapshots before resolving either filesystem
  coordinate; and
- `hosted` can consume only a snapshot declared synthetic whose validated
  freshness-source kind also resolves to synthetic.

`runner_scope` is a caller declaration, not remote-host attestation. A real CI
integration must supply and protect that fact through its own trusted wiring.
Likewise, the `synthetic` classification comes from the snapshot's validated
freshness-source kind; it does not inspect or attest that the policy content is
genuinely synthetic.

### Reference executable

`bin/latch_ci_check.py` emits one redacted receipt to standard output and uses
the shared exit table above:

```sh
python3 bin/latch_ci_check.py \
  /private/runtime/policy.snapshot.json \
  /work/example \
  --repository-id example-repository \
  --base-sha 1111111111111111111111111111111111111111 \
  --head-sha 2222222222222222222222222222222222222222 \
  --policy-domain-id example-domain \
  --runner-scope self-hosted \
  --snapshot-kind private
```

Invalid argument shapes also produce a redacted `invalid` receipt rather than
echoing raw arguments to standard error.

## Current boundary

These components provide contracts and reference executables. They do not, by
themselves, parse a host's native payload, register a hook, render host deny
UX, provision a trusted runner, publish a policy snapshot, or install a CI
job. No production Claude Code, Codex, or Cursor capability declaration, host
integration, or empirical timeout fixture ships in this slice. Private
snapshots contain policy text and must not be committed, attached to a pull
request, or copied into logs. Native Windows enforcement remains pending, and
A4.2 is outside this slice. Treat any deployment claim as separate evidence
that the relevant host wiring, capability assessment, empirical timeout
evidence, payload-conformance corpus evidence, and local or self-hosted second
line are active in the target environment.
