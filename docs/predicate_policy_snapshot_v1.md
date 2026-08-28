# Predicate policy snapshot v1

`predicate-policy-snapshot-v1` is the private boundary between the complete,
read-only policy projection and the zero-model action evaluator. It is not a
public report, deployment artifact, hook, or policy-authority source.

## Lifecycle

1. `predicate_policy.project_policy_domain(connection, policy_domain_id)`
   selects every binding and advisory row in one explicit domain. It performs
   no search, ranking, ref-count ordering, top-k retrieval, or semantic slice.
2. `predicate_snapshot.publish_policy_snapshot(...)` calls that projector while
   fingerprinting its source immediately before and after projection and again
   after deterministic compilation.
3. Publication serializes all enforcement-relevant row and authority fields,
   compilation results, classifications, reason counts, domain, projection and
   predicate engine identifiers, projection freshness token, and external
   freshness evidence.
4. A stable SHA-256 digest covers the complete canonical document except the
   digest field itself. No volatile publication timestamp is present.
5. The file is written through a same-directory temporary file and atomic
   replacement. It is owner read/write (`0600`) where POSIX modes apply and the
   closest owner read/write mode under Windows ACL inheritance.
6. `predicate_consumer.evaluate_policy(...)` reloads and authenticates the
   snapshot, recomputes external freshness evidence, and evaluates the action.
   A preloaded object also rechecks freshness on every evaluation.

The publisher refuses a destination under the public source checkout. The
parent directory must already exist and must be a private runtime location.
Snapshots contain private policy text and must never be committed, attached to
a PR, printed, or copied into logs.

Binding rows must carry exactly the projected domain. Advisory rows may carry
that domain or a null domain for the explicitly unbound advisory bucket. A row
from any other domain makes publication invalid. Domain ids are 1-256
character opaque, log-safe ASCII identifiers. The first character is a letter
or digit; later characters may also use `.`, `_`, `:`, and `-`. They are not
filesystem coordinates or free prose. Freshness tokens use the same log-safe
character set.

## Freshness evidence

A snapshot must have exactly one source:

- For a file-backed vault, `source_vault_path` fingerprints the database plus
  the presence, size, and nanosecond modification metadata of its `-wal` and
  `-shm` sidecars. A missing database or any change flags until republished.
- Synthetic and in-memory projectors use `freshness_token_path`, a small
  external generation file controlled by the test or embedding application.
  Changing or replacing that file flags until republished.

The projection's own `freshness_token` is included in the policy digest, but it
is not accepted as self-attested freshness. External evidence is mandatory.
This intentionally flags on unrelated file-backed vault writes. That
conservative behavior prevents stale pass/block claims without adding DB
writer hooks or SQLite to the tool path.

If the source changes during every publication retry, or compilation raises,
the publisher atomically replaces any old snapshot with a redacted invalid
marker before returning an error. A previous snapshot therefore cannot remain
apparently current after a failed refresh.

## Snapshot shape

The file is canonical JSON. This abbreviated example is synthetic; real row
text remains private:

```json
{
  "snapshot_version": "predicate-policy-snapshot-v1",
  "engine": "predicate-v1",
  "projection_engine": "predicate-policy-projection-v1",
  "state": "ready",
  "policy_domain_id": "synthetic-project-a",
  "freshness_token": "sha256:synthetic-projection-generation",
  "freshness_source": {
    "kind": "token-file-v1",
    "path": "/private/runtime/source-generation.txt",
    "fingerprint": "0000000000000000000000000000000000000000000000000000000000000000"
  },
  "binding_rows": [],
  "advisory_rows": [],
  "reason_counts": {},
  "digest": "1111111111111111111111111111111111111111111111111111111111111111"
}
```

Every row record contains a private normalized row, its explicit
`binding|advisory` classification, and deterministic compiler output. Loading
recompiles each row and compares the stored compiler shape to the running
engine. Wrong version, engine, domain, digest, compiler output, source, or
freshness token fails closed.

Evaluation checks the external freshness source both before and immediately
after predicate matching. A source change during evaluation converts that
same call to a redacted `flag`; no contemporaneously stale pass or block is
returned.

Receipt counts do not overlap: `binding_rows` is the authority-binding total,
`advisory_rows` is the authority-advisory total, `binding_compiled` is the
compilable binding subset, and `uncompilable_rows` separately reports all
uncompilable rows.

## Library and reference consumer

The library returns two deliberately separate values:

- `PolicyEvaluation.verdict`: exact private `predicate-v1` core.
- `PolicyEvaluation.receipt`: redacted `predicate-policy-receipt-v1` structure.

The non-installed reference consumer accepts one canonical action object on
stdin and emits only the receipt:

```sh
python3 bin/predicate_consumer.py /private/runtime/policy.snapshot.json <<'JSON'
{
  "policy_domain_id": "synthetic-project-a",
  "project_root": "/synthetic/project",
  "cwd": "/synthetic/project",
  "tool_name": "synthetic.write",
  "proposed_file_paths": ["src/example.py"],
  "diff_paths": [],
  "staged_paths": [],
  "import_names": [],
  "api_names": [],
  "evidence_complete": true,
  "evidence_provenance": ["synthetic-example"]
}
JSON
```

Its receipt contains the decision, domain id, policy digest and freshness
token, non-overlapping binding/advisory counts, the binding compiled subset,
uncompilable count, matched row/node ids, aggregate reason codes, and
`llm_calls: 0`. It never emits policy text, action text, private paths, or the
complete private match objects.

This script is a contract example for A4 adapters. It is not installed or
activated by A2, and it performs no host payload parsing, hook registration,
deny UX, PR/CI integration, or deployment.
