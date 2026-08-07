# Canonical outcome-measurement audit runbook

One explicit, offline canonical audit of the frozen outcome-measurement
contract v2.6: parse the S1 gate logs and S2 host transcripts named by an
audit envelope, fold them into receipts under the pinned protocol, and commit
a canonical report plus an authenticated lineage checkpoint. No model calls,
no network, no KB writes. This runbook covers running the audit and where its
outputs live; it does not cover envelope authoring or T0 window policy.

## Invariants

- The audit is idempotent: repeated runs over byte-identical evidence with a
  finalized prior produce the same clean report.
- Outputs never overwrite evidence. A report or checkpoint path that aliases
  a measured source root — including a case-variant or hardlinked spelling of
  a file-valued root — is refused before anything is written.
- The lineage checkpoint is a vault-local private artifact: written 0o600
  into the project's vault directory, gitignored, HMAC-authenticated under
  the project's vault identity. It stores exact source coordinates and
  cleartext `session_id`/`key_id`; its privacy comes from its placement, and
  it should never travel outside the vault. A report aimed at the checkpoint
  is refused.
- The report and checkpoint commit as a pair: a failed checkpoint write rolls
  back the committed report.

## Run one audit

```bash
bash bin/run_latch_outcome_audit.sh \
  --project /absolute/path/to/project \
  --envelope /absolute/path/to/audit_envelope.json \
  --report /absolute/path/to/canonical_report.json
```

`--lineage` defaults to the private `outcome-lineage.json` inside the
project's vault directory — the sanctioned location; pass it explicitly only
to relocate the checkpoint outside every measured root. `--contract` defaults
to the packaged frozen contract at
`artifacts/outcome-measurement/contract-v2.6.md`; supplying different bytes
invalidates the report with `contract_hash_mismatch`. On the first run in a
fresh window, add `--initialize-empty-lineage`; on every later run omit it —
the flag refuses to run when a checkpoint already exists.

The wrapper honors `LATCH_HOME`/`CLAUDE_KB_HOME` for the checkout root and
`LATCH_PYTHON`/`CLAUDE_KB_PYTHON` for the interpreter.

## Reading the result

- Exit 0: a non-invalidated canonical report was committed; a sorted-keys
  JSON summary is printed on stdout.
- Exit 1: the report was committed but is invalidated — read
  `oracles.invalidation_reasons` in the report.
- Exit 2: the audit refused to run (path aliasing, lock contention, envelope
  or checkpoint validation); a JSON error is printed on stderr and no output
  moved.

A second concurrent run against the same checkpoint fails closed on the
`<lineage>.lock` writer lock rather than interleaving.
