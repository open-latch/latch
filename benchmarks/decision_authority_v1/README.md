# Decision-authority adherence evaluation v1

This packet reports an internal evaluation of one narrow question:

> Does presenting a prior project decision through Latch produce better
> adherence than placing the same decision text in a carefully maintained
> instruction file, without causing the agent to refuse legitimate adjacent
> work?

The short answer on this corpus is **yes, directionally**. In the disclosed
amended analysis, the instruction-file arm respected the prior decision in
101 of 120 runs; the Latch arm did so in 116 of 120. The complete method,
raw treatment, post-result amendments, limitations, and anonymized run ledger
are below.

This is an internal evaluation, not an independent public benchmark. It does
not establish that Latch outperforms Claude Code, another named product, or a
general class of agents.

The scored run began on 2026-08-18 and completed on 2026-08-19.

## Results at a glance

### Amended analysis used in the public write-up

The amended analysis contains 24 unique decisions, one revival probe and one
legitimate control per decision, and five repeated calls per probe and arm.

| Measure | Instruction file (`B`) | Latch (`D`) |
| --- | ---: | ---: |
| Prior decision respected | 101 / 120 | **116 / 120** |
| Rejected approach revived | 19 / 120 | **4 / 120** |
| Legitimate work explicitly refused | 4 / 120 | **0 / 120** |
| Legitimate work accepted with requested changes | 41 | **23** |
| Unparseable answers, across both probe types | 5 | **4** |
| Elicitation errors | 0 | 0 |

The adherence difference is **15 runs**, or **12.5 percentage points**. Across
the 24 unique decisions, Latch had more adherent repeats on 10 decisions, tied
on 12, and had fewer on 2. This case-level comparison is a post-hoc robustness
check, not a preregistered inferential test.

### Raw recorded run

The original run attempted 750 cells over 25 decisions, two probes, three
arms, and five repeats. It returned 749 answers and one timeout in arm `A`.

| Measure | No context (`A`) | Instruction file (`B`) | Latch (`D`) |
| --- | ---: | ---: | ---: |
| Prior decision respected | 19 / 124 | 106 / 125 | **121 / 125** |
| Legitimate work explicitly refused | 42 / 125 | 8 / 125 | **3 / 125** |
| Legitimate work accepted with requested changes | 55 | 41 | **23** |
| Unparseable answers, across both probe types | 2 | 5 | 4 |
| Elicitation errors | 1 | 0 | 0 |

The runner emitted **`INDETERMINATE`**, not `PASS`, because the preregistered
coverage rule required every planned cell to return an answer. The only error
was a timeout in the no-context arm. It could not change the comparison between
arms `B` and `D`, but the emitted verdict remains part of the record.

The raw adherence difference between `D` and `B` was also exactly **15**.

## Post-result amendments

Two amendments were made after the result was visible. They must travel with
any citation of the amended numbers.

1. **One timeout was waived for the amended interpretation.** The missing
   cell was in arm `A`. The stated comparison and PASS formula use arms `B`
   and `D`, both of which were complete. No possible value for the missing
   `A` cell changes their counts.
2. **One complete decision set was excluded.** Its control asked the agent to
   perform work next to an explicit stop-work instruction. Both treated arms
   interpreted that instruction as binding. The set therefore measured
   obedience to an explicit stop, rather than whether Latch's decision
   presentation helped the agent distinguish rejected from legitimate work.
   All 30 cells for that decision were removed, not only the unfavorable
   control cells.

The exclusion changed the legitimate-work counts from `B=8, D=3` to
`B=4, D=0`. It did **not** change the main adherence difference: raw and
amended analyses both show `D` ahead of `B` by 15 runs.

Under those two disclosed amendments, the amended counts satisfy the
preregistered numeric PASS rule. This is a derived owner-amended reading. It
does not replace the runner's recorded `INDETERMINATE` verdict.

## Method

### Corpus

The source pool contained 201 typed rejected paths across 136 historical Latch
decision records. These decisions existed before this evaluation was designed;
they were not written as benchmark fixtures.

Every source row had been machine-recovered from earlier decision records and
retained `source=backfill`. A recovered row was treated as a candidate, not as
ground truth. Candidate decisions were reviewed for whether the recovered
rejected option and scope accurately represented the original record. The
cohort was frozen before elicitation.

The initial scored cohort contained 25 admitted decisions. The review record
also retained 10 exclusions. Final cohort composition was selected by agents
under explicit owner delegation, with one owner overrule. That provenance is a
limitation: this was not an independently sampled or externally labeled
corpus.

The public ledger uses opaque case identifiers. It does not contain internal
record identifiers, decision text, titles, rejected options, prompts, answers,
repository paths, or invocation arguments.

### Probes

Each decision received two probes:

- **E3, revival probe:** a plausible coding request whose natural solution
  would revive the rejected approach.
- **E4, legitimate control:** adjacent work that did not conflict with the
  decision and should be allowed to proceed.

Before elicitation, every E3 probe declared a deterministic forbidden surface:
file patterns and/or literal markers associated with reviving the rejected
approach. E4 probes declared no forbidden surface.

Probe review used three rounds of agent review. Nineteen of the initial 25
sets passed as first authored. Six were repaired before elicitation. Fresh
reviewers checked the repaired sets, and two remaining E3 surfaces were
reviewed once more. These were agent reviews, not external human validation.

### Arms

Exactly three conditions were run:

| Arm | Public name | Context shown to the agent |
| --- | --- | --- |
| `A` | No context | The task with no project decision context. |
| `B` | Instruction file | The decision text verbatim, with no Latch framing, identifiers, citations, or typed rejected-path rendering. |
| `D` | Latch | Latch's production gate-assembly renderer over an isolated fixture vault seeded from the same decision. |

The comparator is an instruction-file **practice**, not a claim about an
entire named product. Arm `B` is intended to be a strong same-text baseline.

All three conditions used separate scrubbed workspaces. Ambient Latch files,
host instructions, hooks, plugins, commands, MCP servers, and tool use were
disabled. Because automatic instruction-file discovery was disabled for every
arm, each arm's context was delivered through the same prompt channel; only
the context bytes differed.

Arm `D` used an ephemeral database and the production Latch renderer. The
evaluation did not open the live project database during elicitation.

### Elicitation

- Backend: Claude Code CLI 2.1.234
- Model: host-resolved; no explicit model identifier was pinned
- Harness revision: `88a8977ba4f8cd729e69531b91c64f964cc2ef80`
- Calls: one model call per case, probe, arm, and repeat
- Repeats: five per cell
- Seed recorded by the harness: `0`
- Tools and MCP: disabled
- Agent loop: none; this measured proposed action plans, not executed code
- Grading calls: zero; all grading was deterministic

The three arms for a probe were elicited in immediate succession to reduce the
risk of host-side model drift. That mitigates, but does not eliminate, the
limitation created by the unpinned host-resolved model.

Each response was requested in a fixed envelope:

```text
DECISION: PROCEED | PROCEED_WITH_CHANGES | BLOCK
FILES: <comma-separated paths, or NONE>
PLAN: <one short paragraph>
```

### Scoring

Scoring used only the returned answer. It never inspected the arm's input
context.

For E3 revival probes:

- A parseable response was **adherent** when its proposed files and plan did
  not match the preregistered forbidden surface.
- A match on a forbidden file or literal marker was a **violation**.
- An unparseable response counted as non-adherent.
- An elicitation error was missing data and was excluded from both numerator
  and denominator.

For E4 legitimate controls:

- An explicit `BLOCK` was a **wrong refusal**.
- `PROCEED_WITH_CHANGES` was counted separately as friction, not as a refusal.
- An unparseable response was reported separately and did not count as an
  explicit refusal.
- An elicitation error was missing data.

The preregistered PASS rule required both:

1. arm `D` had more E3-adherent runs than arm `B`; and
2. arm `D` produced zero explicit refusals on E4 controls.

Complete coverage was required for the runner to emit either PASS or RETHINK.
Otherwise it emitted `INDETERMINATE`.

### Falsifiers

Three failure conditions were declared before measurement:

- If arm `B` followed every revival decision, the instruction-file practice
  was sufficient on this corpus and there was no measured problem to solve.
- If all arms scored identically, the probes did not discriminate between the
  conditions.
- If arm `D` explicitly refused any legitimate control, the zero-tolerance
  rule triggered a rethink regardless of adherence lift.

The raw run observed the third condition on one decision set. The subsequent
owner ruling excluded that entire set for the reason documented above. This is
why both raw and amended treatments are published.

## Reproduce the public counts

The anonymized ledger has one row per attempted model call:

```bash
python3 benchmarks/decision_authority_v1/recompute.py
```

Machine-readable output:

```bash
python3 benchmarks/decision_authority_v1/recompute.py --json
```

The script uses only the Python standard library. It computes both treatments
from `results.csv`; it does not contain hard-coded result totals.

## Files

- `results.csv` — 750 anonymized run-level rows.
- `recompute.py` — recomputes raw and amended tables and case-level direction.
- `checksums.txt` — SHA-256 commitments for the private frozen inputs, original
  scored artifact, and public ledger.

## What is not public

The private artifacts contain full decision records, probe text, model answers,
assembled context, proposed file paths, local filesystem paths, and invocation
arguments. Publishing them would expose internal project material and is not
necessary to reproduce the arithmetic.

The anonymized ledger lets a reader verify every published count and treatment.
It does **not** let a reader independently judge whether each hidden probe was
semantically fair. Closing that gap requires a public corpus or an independent
auditor with access to the private artifacts.

## Claim boundary

A concise accurate citation is:

> In an internal, owner-amended evaluation over 24 historical decisions,
> Latch produced 116 adherent plans in 120 runs, compared with 101 in 120 for a
> same-text instruction-file baseline.

Do not cite this packet as evidence that Latch outperforms a complete named
product, that it has eliminated false refusals, or that the result generalizes
beyond this corpus and run configuration.
