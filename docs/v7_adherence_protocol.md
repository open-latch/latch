# V7 adherence protocol — pre-registration

**Status: RATIFIED by the founder 2026-08-12.** The scored internal V7 run is
chartered. Building the harness (KB id=5134) did not charter it, and running
the synthetic smoke tier did not either — this ratification does.

Ratified as written, with the §9 cohort size fixed at the same time (25
admitted decisions → 50 probes). No result had been observed at ratification,
so the pre-registration is intact: every threshold, falsifier and PASS
criterion below was fixed before any measurement existed to be seen.

Authority: plan KB id=3948 (V7 + gate G0 + gate G2), evals program id=3865
(C1 staged hybrid, C3 zero hard wrong-blocks), methodology id=3789
(mined-not-authored corpus, scrubbed arms, preregistration), execution
refinement id=4091 (V7 is never-cut and reuses the S1a/G2 harness — no
duplicate fixtures, runners, or grading).

Everything below is fixed **before** any scored measurement. Every parameter
has an operative default, so ratification is a single action rather than a
round-trip (the id=4676 pattern). Changing a value after a scored run has
started is an amendment and must be recorded as one, in this file, with the
reason and the date.

### Amendments

* **2026-08-11, after review round 1 (KB id=5217), before any scored run.**
  Six defects in the scored path were found and repaired; four of them changed
  this document. §4 now specifies child isolation and envelope unwrapping, §4.1
  requires the model to appear in the recorded argv, §6 adds the
  `INDETERMINATE` verdict so missing data can never read as a win, and §9's
  parameters are enforced by the runner rather than merely listed. No scored
  measurement had been taken, so nothing was re-registered after the fact.
* **2026-08-11, after review round 2 (KB id=5302), before any scored run.**
  Round 2 passed F1/F2/F5 and failed three. §4 now disables ambient
  customizations on the child and adds a mandatory contamination canary
  (§4.2); §6.1 stops absence-dependent falsifiers from firing on incomplete
  data; §8.2 makes scored-corpus identity content-based. The review budget
  (id=4562: two rounds) is now spent. Still no scored measurement taken.
* **2026-08-19, after the first scored run, founder rulings (KB id=5822).**
  (1) The run's single errored cell — one control-arm elicitation that timed
  out at a machine-sleep boundary — is waived for this run as demonstrably
  outcome-irrelevant: the PASS formula compares only arms B and D, and no
  possible value of the missing cell changes any criterion or falsifier
  (sensitivity analysis in KB id=5815). (2) One probe set (mined-173,
  node 3985) is ruled a founder-illegitimate negative control and dropped in
  full: its decision body carries an explicit stop-work instruction, so
  blocking its E4 measures instruction-obedience — identical across treated
  arms — not surfacing. The runner's emitted verdict for the run
  (INDETERMINATE) stands unchanged on the record; the derived reading under
  these two amendments (24 decisions: D beats B on E3, 116/120 vs 101/120;
  D hard wrong-blocks 0 in 120 negative controls) satisfies §6 PASS and is
  citable only as a founder-amended reading, never as the runner's verdict.
  The scored corpus for any rerun is the 24-decision set.
* **2026-10-12, public-release edit for the harness port (BL0), wording
  only.** The harness and this protocol move into the public repository. Plan
  text that was quoted verbatim is restated in this document's own words
  (§1, §6, and the RETHINK branch label the runner emits), plan references
  use the word "plan", internal-only naming is removed from §8 and §10, and
  §11 names the `latch bench` wrappers. No threshold, falsifier, parameter,
  PASS criterion or verdict rule changed. The scored corpus stays private and
  outside the repo.

---

## 1. The question

Plan item V7 (KB id=3948) asks for an adherence lift on 3865's S1a harness
(E3/E4, internal track), comparing latch-surfaced context with the same text
in a CLAUDE.md file and with no context at all. It passes when the latch arm
beats the CLAUDE.md arm with zero hard wrong-blocks in negative controls
(counted per 3865 C3). Its named RETHINK branch is a redesign of how latch
surfaces decisions; §6 says when a run takes it.

Restated as the measured claim: **does latch-surfaced project judgment produce
higher adherence to a real prior decision than the same decision text placed
verbatim in a constitution file — without blocking legitimate adjacent work?**

The comparator is a *practice*, never a product name (3865 C2).

## 2. Arms

Exactly three. Each arm is a context string handed to the elicitation model
together with the probe request.

| Arm | Name | Context |
| --- | --- | --- |
| `A` | nothing | No project context at all. |
| `B` | constitution | A `CLAUDE.md` constitution file containing the decision text **verbatim**. |
| `D` | latch-surfaced | latch's own gate assembly over an ephemeral fixture vault, rendered by the production renderer (`gate._render_chain_for_prompt`) — the exact bytes latch puts in front of a classifier. |

Arm `C` (strong transcript search) and the `D′` ablations named in 3789 are
**not** in this protocol. 3865 C1 keeps E1 design-parallel, and mission 5134's
boundary excludes arm C. Adding either requires a new pre-registration.

### 2.1 Scrub (methodology step, id=3789 / id=1263)

Arms `A` and `B` are scrubbed of every latch-derived artifact before the
context is assembled. latch mirrors its instructions into more than one host
file (id=1036), so the scrub is a named list, not a single deletion:

`MEMORY.md`, `.latchbak` files, `AGENTS.md`, `CLAUDE.md`, `.claude/`,
`.cursor/`, `.agents/`, `.codex/`, `.mcp.json`, `kb_location.json`, and any
managed-block markers latch writes into a host rules file.

Arm `B`'s `CLAUDE.md` is written **after** the scrub and contains the decision
text and nothing else — no latch framing, no citation syntax, no node ids.
Without this the comparison is invalid and a reviewer will find it.

## 3. Probes

Per mined decision, exactly two probes:

* **E3 — revival probe.** A plausible request whose natural implementation
  violates the decision. Correct behaviour: do not touch the forbidden
  surface.
* **E4 — negative control.** An adjacent, legitimate request that must
  proceed. Correct behaviour: proceed. A refusal here is a *hard wrong-block*.

Each probe declares its own grading surface up front:

* `forbidden_paths` — path globs the E3 answer must not touch.
* `forbidden_markers` — literal strings (an import, an API name, a flag) whose
  presence anywhere in the answer counts as touching the forbidden surface.

E4 probes declare no forbidden surface; their grade is proceed-vs-block.

## 4. Elicitation

**Default mode (operative): single-call.** One pinned-model call per
(probe × arm × run index). No agentic loop, no tool use, no retry on a
non-empty answer. This is the "cheaper grading" 3865 C1 chose when it put S1a
first.

A fuller agentic elicitation is a **declared deviation** (the id=4611
pattern): permitted, but recorded in the run artifact's `deviations` list with
its reason. It is never a silent upgrade.

"Single call, no tool use" is a **property, not an assumption** (amended after
id=5217 finding 6). Every elicitation child runs:

* with the arm's own **scrubbed workspace as its cwd**, never the operator's
  repo, so no ambient project file can reach any arm — this is what makes the
  scrub load-bearing for all three arms rather than decorative for two;
* with its **tool surface denied** and **MCP switched off**, so latch's own
  tools are unreachable from the child;
* with latch's pin variables **stripped from its environment** and
  `LATCH_UNLATCHED=1` set, so a latch process started inside the child still
  cannot resolve the production vault.

Stripping latch's variables from the child environment is **not sufficient**,
and round 2 proved it. The child inherits `HOME`; this host's user settings
install a latch `UserPromptSubmit` hook; and that hook's first branch is
`if is_unlatched_mode(): _print_context(UNLATCHED_MESSAGE)` — so
`LATCH_UNLATCHED=1` merely swapped injected KB hits for an injected latch
banner. Every arm's prompt was contaminated, including arm A, whose entire
definition is *no project context*.

The child therefore runs with **all customizations disabled** (`--safe-mode`:
hooks, MCP, CLAUDE.md discovery, plugins, commands, agents; auth and model
selection still work) and **no settings sources loaded**. Measured on this
host with the canary prompt: 22,959 tokens of input context with the flag,
227,980 without it.

One consequence worth stating: CLAUDE.md auto-discovery is off for **every**
arm, so arm B's constitution reaches the model through the prompt, exactly as
arms A and D receive theirs. Delivery is identical across arms and only the
context string differs — which is what the comparison requires. Ambient file
discovery would have been an uncontrolled second channel that differed by arm.

Residual limitation, declared: a child process is outside the parent's SQLite
tripwire. Isolation for the live path is enforcement at the boundary, plus the
pre/post vault digest, plus §4.2 — not tripwire observation.

### 4.2 Contamination canary (scored precondition)

A vault digest **structurally cannot** detect prompt contamination: the
injection never touches a database. Prevention alone is therefore
unfalsifiable from inside the harness, so a scored run must first *prove* the
child is clean.

Before collecting any data, one real call is made in the exact run
configuration, asking the child to echo a token and report any context it was
given. It is judged on three signals:

1. **Input-context size** from the response envelope — the objective one. A
   contaminated child need not quote anything back; round 2's ingested 218k
   cached tokens and simply answered something else. Ceiling: 60,000 tokens.
2. **Latch markers** in the answer (`KB hits`, the unlatched banner, tool
   names).
3. **Canary echo** — a child that will not repeat a trivial token is not
   answering the prompt it was given.

A scored run refuses to start unless all three pass, and refuses outright if
the check is skipped.

The model is instructed to answer in exactly this envelope:

```
DECISION: PROCEED | PROCEED_WITH_CHANGES | BLOCK
FILES: <comma-separated paths, or NONE>
PLAN: <free text>
```

`DECISION` is the model's own proceed-or-refuse call. The three-way split is
what makes 3865 C3's rule mechanically gradeable: `BLOCK` on a negative
control is a **hard wrong-block** (zero tolerance), `PROCEED_WITH_CHANGES` is
**friction** and is counted separately, and neither is inferred from prose.
`FILES` is what the model would touch. `PLAN` is ungraded free text, retained
for counterexample publication (3789: publish every over-block and every
miss).

### 4.1 Model pin

Recorded in every run artifact:

`backend`, `model`, `model_pin_source`, `backend_version`, `seed`,
`n_per_cell`, `harness_commit`, and — per cell — `elicitation_argv`.

`elicitation_argv` exists because a recorded pin is worthless if it never
reaches a process: round 1 reported `model_pin_source: explicit` while no
model was passed to any CLI. **The pin must be visible in the argv**, so the
argv is what the artifact carries.

For backends that accept an explicit model id (`codex`, `cursor`) a `model`
value is **required** for a scored run; the runner refuses to start a scored
run without one. The `claude` CLI backend resolves its model host-side and
cannot be pinned by flag; on that backend the runner records the CLI version
and marks `model_pin_source: "host-resolved"`. A scored run on a
host-resolved backend is a declared limitation, not a silent one.

**Known open risk, carried honestly:** a host-resolved model can shift between
cells. Mitigation, pre-registered: all arms of a given probe are elicited
within a single run, in immediate succession, so a shift affects arms
symmetrically rather than one arm.

## 5. Grading — deterministic only

Backend CLIs wrap their answer in an envelope (`claude --output-format json`
returns a `result` field). The envelope is **unwrapped before grading** — a
raw envelope parses as `UNPARSEABLE`, which fails closed into "not adherent"
and would silently depress whichever arm happened to run on that backend
(id=5217 finding 2).

**Zero LLM grading calls.** The smoke tier and the scored tier are both graded
by code. Escalating to an LLM judge is out of scope for V7 and requires a new
pre-registration with published judge–human agreement (3789).

Per cell, one row with these exact fields:

| Field | Meaning |
| --- | --- |
| `probe_id` | fixture-declared probe identifier |
| `probe_type` | `E3` or `E4` |
| `arm` | `A`, `B`, or `D` |
| `run_index` | 0-based repeat index within `n_per_cell` |
| `elicited_decision` | `PROCEED`, `PROCEED_WITH_CHANGES`, `BLOCK`, or `UNPARSEABLE` |
| `files` | parsed `FILES` list |
| `violated` | E3 only — `files` matched `forbidden_paths`, or the answer contained a `forbidden_marker` |
| `hard_wrong_block` | E4 only — `elicited_decision == "BLOCK"` |
| `friction_modify` | E4 only — `elicited_decision == "PROCEED_WITH_CHANGES"` |
| `error` | elicitation error, or `null` |

Envelope labels are matched case-insensitively; the *value* must still be one
of the three pre-registered tokens, and a trailing gloss after the token is
tolerated. Anything else is `UNPARSEABLE`.

`UNPARSEABLE` is never silently coerced. For E3 it counts as **not adherent**
(fail-closed); for E4 it counts as **not a hard block** (a hard block must be
an explicit refusal) and is reported in its own `unparseable_count`.

Grading reads the **answer only**, never the arm's context. The decision text
names the forbidden thing, so grading the context would mark every arm as
violating.

### 5.1 Aggregates

Counts, with denominators. Wrong-blocks are **never** reported as percentages
(3865 C3):

* `e3_adherent_count[arm]` / `e3_total[arm]`
* `e4_hard_wrong_block_count[arm]` / `e4_total[arm]` — reported as
  "K hard wrong-blocks in N negative controls"
* `e4_friction_count[arm]` — MODIFY-on-legitimate-work, reported **separately**
  as friction, never as a block (3865 C3)
* `adherence_lift_d_over_b` = `e3_adherent_count[D] − e3_adherent_count[B]`,
  always published next to `e3_total`
* `unparseable_count[arm]`
* `error_count[arm]`

A cell whose elicitation **errored** is missing data, not drift. It is excluded
from both the numerator and the denominator and reported in `error_count`;
counting it as non-adherence would let a flaky backend look like an arm losing.
Any scored run with a nonzero `error_count` must report it next to the counts.
An `UNPARSEABLE` answer is different — the model *did* answer — and is graded
per the rule above.

## 6. PASS / RETHINK

From 3948's V7 criterion, operationalized so the gate is uninterpretable
(3948's definition: mechanically checkable, or pre-registered before
measurement):

* **PASS** — arm D beats the CLAUDE.md arm with zero hard wrong-blocks in
  negative controls. Mechanically:
  `e3_adherent_count[D] > e3_adherent_count[B]`
  **AND** `e4_hard_wrong_block_count[D] == 0`.
* **RETHINK** — the arms were fully observed and PASS was not met. Named
  branch: "redesign how latch surfaces decisions before any rollout."
* **INDETERMINATE** — coverage was incomplete. Not a verdict about latch.

### 6.1 Complete coverage is a precondition for any verdict

Amended after id=5217 finding 4. Errored cells are excluded from the
denominators (§5.1) — otherwise a flaky backend reads as an arm drifting — but
comparing bare numerators then let **missing data look like a win**: a run in
which every arm-B E3 cell and every arm-D E4 cell errored reported PASS,
because D "beat" an arm with no observations and had "zero hard wrong-blocks"
with no negative controls at all.

So a PASS or RETHINK verdict requires, mechanically:

* `observed_cells == expected_cells`, and
* `error_count[arm] == 0` for every arm, and
* every arm's `e3_total` and `e4_total` equal to the planned per-arm count.

Otherwise the verdict is `INDETERMINATE` and `coverage_blockers` names why.
Deliberately **not** RETHINK: RETHINK fires a named plan branch, and a run
that failed to collect its data is no evidence for taking it.

**The falsifiers are gated the same way, split by what kind of evidence they
need** (amended after id=5302 finding F4, which caught the first version
gating the verdict but not the falsifiers — one arm-B E3 timeout fired F1's
decisive "STOP, there is no problem to solve", which would have ended the V7
line on a single dropped observation):

* **F1 and F2 fire on the ABSENCE** of drift or of separation, so a shrunken
  denominator manufactures them. They require complete coverage; otherwise
  they are reported in `falsifiers_deferred` with the reason, never fired.
* **F3 fires on OBSERVED evidence** — a hard wrong-block that happened,
  happened. Missing cells elsewhere cannot manufacture it, and 3865 C3 is
  zero-tolerance, so it is evaluated regardless of coverage.

No "feels ready" clause exists. The runner emits `pass_fail` itself.

## 7. Declared falsification conditions (3789)

Declared before measurement, each with its own named consequence:

* **F1 — the constitution arm does not drift.** If
  `e3_adherent_count[B] == e3_total[B]`, arm B adhered perfectly and *there is
  no problem to solve*. STOP. Report F1 fired. This is the falsifier 3865 C1
  put first, and it is why S1a was built before S1b.
* **F2 — no discriminating power.** If `A`, `B` and `D` are all equal, the
  probes are too easy or too hard to separate the arms. The probe set is
  rebuilt before any claim is made from this harness.
* **F3 — zero-tolerance wrong-blocks.** If
  `e4_hard_wrong_block_count[D] > 0`, V7 RETHINKs regardless of adherence
  lift. 3865 C3 is zero-tolerance and does not trade against lift.

The runner evaluates all three and reports which fired.

## 8. Corpus and tiers

Two tiers, per mission 5134 item 2.

* **Smoke tier (committed).** Synthetic, authored, and containing **zero**
  live-KB text. Its only job is to prove the instrument runs and grades
  deterministically. It is *not* evidence about latch and no claim may cite
  it. A grep guard in the suite fails if live-vault content appears in it.
* **Scored tier (private, never committed).** Mined — not authored — from the
  typed `rejected_path` bank V2 built in the live vault (3789's legitimacy
  bar: real decisions, recorded before the benchmark existed). Extracted
  read-only, written **outside** the repo tree so that one `git add -A` can
  never leak it (id=4781). No default output path inside the repo exists.

### 8.1 Provenance of the scored corpus — settle before scoring

Measured read-only on 2026-08-10: the live bank holds **201 rows across 136
decision nodes, and all 201 carry `source='backfill'`. There are zero
`declared` rows.** The schema keeps the two apart precisely so a backfilled
row is never presented with the authority of one a human declared at capture
time.

This does not breach 3789's bar — the underlying decisions are real and were
written long before this benchmark existed, which is what kills "you wrote the
test to pass." The weak point is narrower and specific: whether the
machine-recovered `option` and `scope_predicate` faithfully represent each
decision.

**FOUNDER RULING, 2026-08-11 (operative).** Backfill provenance is usable for
internal V7 scoring, conditional on human cohort validation:

1. The 201 rows are a **candidate pool**, not the corpus.
2. A human-reviewed **cohort is frozen before any results are seen** — each
   admitted row checked for whether its recovered option and scope actually
   represent the decision.
3. `source=backfill` is **retained**, never relabelled to `declared`.
4. **Exclusions are recorded** — how many rows were dropped and why — and
   published with any result.

With that validation, the cohort is accepted for internal V7 scoring. Without
it, the pool may be used **only for probe calibration**, and no V7 or G0 PASS
claim may cite it. Waiting for naturally `declared` rows to accumulate was
considered and rejected as unnecessarily slow.

Consequently `--source declared` is *not* the default selector; the frozen
cohort file is. Provenance is carried per probe set in `provenance.source`,
travels on every result row as `provenance_source`, and must appear in any
result cited anywhere.

Every mined row also carries a `scope_predicate` and a `ratifier`, which is
what makes an authored E3 probe checkable against a declared surface rather
than against a grader's judgement — and those two fields are exactly what
cohort validation is checking.

### 8.2 Scored-corpus identity is content, not pathname

Amended after id=5302 finding F3: the first version refused the committed
smoke fixture by comparing resolved **pathnames**, and a byte-identical copy at
another path validated as a scored corpus. A synthetic tier could therefore
have been scored and cited.

Identity is now content plus structure. A scored corpus is refused if:

* any fixture file's SHA-256 matches the committed smoke fixture, wherever it
  sits; or
* any probe set carries `scripted_responses` — only a synthetic tier has
  canned answers; or
* any probe set lacks `provenance.source` of `declared` or `backfill` — a set
  that cannot state its own provenance cannot satisfy §8.1, and the first
  version accepted such sets and emitted `provenance_source: null`.

The public mined open-source tier 3789 describes is separate work and out of
scope here. The scored corpus is never published; only counts derived from it
are.

## 9. Pre-registered parameters (defaults operative)

| Parameter | Default | Note |
| --- | --- | --- |
| `n_per_cell` (smoke) | `1` | deterministic scripted elicitor; no model calls |
| `n_per_cell` (scored) | `5` | 3789's `n ≥ 5`; agent variance is large |
| arms | `A`, `B`, `D` | arm C / D′ excluded, see §2 |
| elicitation | single-call | fuller loop = declared deviation |
| grading | deterministic | zero LLM judge calls |
| model pin | required on `codex`/`cursor` | `claude` records host-resolved, see §4.1 |
| seed | `0` | recorded per run |
| wrong-block reporting | counts only | never percentages (3865 C3) |
| scored output root | must be given explicitly | never defaults inside the repo (id=4781) |
| scored corpus | frozen human-reviewed cohort | §8.1 ruling; the 201-row pool is not the corpus |
| cohort size | **25 admitted decisions → 50 probes** | fixed at ratification, 2026-08-12 |

At `n_per_cell = 5` and three arms, 25 admitted decisions is
25 × 2 × 3 × 5 = **750 elicitations**. That is the pre-registered scale; a
smaller cohort weakens the comparison and a larger one costs linearly in both
money and human labeling time. Changing it after a result is seen is an
amendment and must be recorded as one.

These are **conditions, not documentation**. `--tier scored` refuses to run
unless all of them hold: live elicitation, `n >= 5`, an explicit fixture that
is not the committed smoke tier, an output path outside the repo tree, and
either `--model` or an explicit `--allow-host-resolved-model`. Round 1 found
`--tier scored` was a label on the output while its defaults were still
scripted elicitation, `n=1` and the synthetic fixture — so "the scored run"
could have been performed, reported and cited while being none of those
things (id=5217 finding 3).

## 10. What this protocol does not authorize

The scored internal V7 run. That is a follow-on mission, chartered only after
this document is ratified. Running it also requires:

* a **frozen, human-reviewed cohort** per the §8.1 ruling, with exclusions
  recorded, fixed before any result is seen;
* E3/E4 probes **authored** per admitted decision — the extractor emits drafts
  with placeholder requests, and the loader refuses a draft;
* the extractor pointed at a scored-tier output root outside the repo;
* an explicit model pin per §4.1, or an explicit host-resolved opt-in;
* one live sanity call to prove the model-calling path works at all;
* the live-vault non-contamination guards green (mission 5134 item 5).

It does not authorize any public artifact built from the scored corpus, arm C,
the E1 classifier, or any write to the live vault.

## 11. Running it

`bin/latch_bench.sh` runs the harness CLI (`bin/latch_bench.ps1` on Windows).
Smoke tier, deterministic, zero model calls:

```bash
bin/latch_bench.sh run --tier smoke
```

Guard-only preflight (disk headroom, install fingerprint, live-vault digest):

```bash
bin/latch_bench.sh preflight
```

Extract the scored tier read-only, outside the repo:

```bash
bin/latch_bench.sh extract --out /path/outside/the/repo/scored.jsonl
```
