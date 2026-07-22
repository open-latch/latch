<p align="center">
  <img src="./docs/assets/latch-logo.svg" alt="latch" width="520">
</p>

<p align="center">
  <strong>latch stops your coding agent from rebuilding what your project already ruled out.</strong>
</p>

<p align="center">
  <em>A decision seatbelt for coding agents.</em>
</p>

<p align="center">
  Local-first &middot; reversible &middot; cited. A fresh agent proposes a settled-and-rejected
  path; latch fires a gate with the receipt before a single file changes.
</p>

<p align="center">
  <a href="#the-receipt">The receipt</a> &middot;
  <a href="#why-a-gate-not-a-rule">Why a gate</a> &middot;
  <a href="#get-started">Get started</a> &middot;
  <a href="#supported-agents">Agents</a> &middot;
  <a href="#safety-and-control">Safety</a>
</p>

---

## The receipt

**A fresh agent proposes a path this project already rejected:**

```text
Request: Implement email sending by adding a Redis-backed background job queue.
```

**latch's gate fires before any edit — and returns the receipt:**

```text
Recommendation: DO_NOT_PROCEED
Summary: The request directly contradicts the canonical decision for this demo app: do not add a background job queue, with Redis-backed background jobs explicitly named as the rejected path (id=1). The allowed path is to keep the app single-process and, if background work is needed for email sending, use an inline task runner and document its limits.
Risk if proceed: Adding Redis and a worker process would violate the install-light, easy-to-inspect demo constraint and repeat the rejected Redis-backed queue path.
Better next action: Implement email sending with the single-process inline task runner approach and document the delivery/latency limits.
Cited evidence:
- id=1 decision status=canonical: No background job queue for the no-history demo app
Worktree changed before/after gate: no
```

Read it field by field:

| Field | What it is |
| --- | --- |
| `Recommendation` | The go/no-go verdict: `DO_NOT_PROCEED`. |
| `Summary` | The settled decision and exactly why the request conflicts with it. |
| `Risk if proceed` | What breaks if the agent ignores the decision. |
| `Better next action` | The compliant alternative to take instead. |
| `Cited evidence` | The exact source node: `id=1`, kind `decision`, `status=canonical`. |
| `Worktree changed` | `no`. The gate ran **before** edits. Nothing on disk moved. |

That last line is the point. The rejected path was caught and cited, and your tree is untouched.

This exact receipt is checked in at [`proof/README.md`](./proof/README.md), captured with the
`codex` backend. It comes from a synthetic no-history demo, so it proves the gate path — not that
latch read anyone's real history. Seeding your own sessions is what does that.

## Why a gate, not a rule

Give an agent your history and it becomes better-informed, not bound: a bigger transcript is still
just context it can talk past. Give it a spec, a rule, or a `CLAUDE.md` and you hand it authority it
can ignore — or quote in one breath and violate in the same diff.

latch is the runtime gate that closes that gap. It keeps your project's cited decisions, rejected
paths, rationale, and evidence in a local KB, then puts a go/no-go verdict in the agent's path
**before files change** — and shows you the receipt. That is decision continuity, not a bigger
transcript or a generic recall layer.

latch is an unlock, not just a guardrail — it guides the agent and protects you at the same time.
Because it keeps the agent inside decisions you've already made instead of rebuilding a path you ruled
out three sessions ago, you stop re-catching the same mistakes and start handing the agent more. That
earned confidence is the part token-savers and history-search tools don't give you: not cheaper runs
or better recall, but faith that the agent is working within your judgment — so you can watch less and
run more agents in parallel without each one re-litigating what you already settled. The judgment
stays yours; latch just keeps the agent inside it.

It runs locally on one SQLite KB, needs no cloud account, and targets macOS, Windows, and Linux.
Claude Code, Codex, and Cursor share the same KB, so judgment captured through one agent can gate
another.

## Get started

**Prerequisites:** Git, and at least one installed agent CLI — **Claude Code**, **Codex**, or
**Cursor Agent**.

**Install in one command.** Open a terminal in the repo you want latch to protect.

macOS, Linux, or Windows Git Bash:

```bash
curl --proto '=https' --tlsv1.2 -LsSf https://raw.githubusercontent.com/open-latch/latch/main/install.sh | bash
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/open-latch/latch/main/install.ps1 | iex
```

That's the install. The quickstart wires your agent, asks how proactive latch should be, and then
prompts you to seed. **Restart the agent** afterward so its tools and hooks load.

**Then seed — this is the real first step, not an optional demo.** An empty KB catches nothing;
seeding is what lets latch gate the decisions already in *your* history. Point it at your recent
sessions:

```bash
/path/to/latch/bin/latch_seed.sh --source both --last-sessions 20 --apply
# Windows: C:\path\to\latch\bin\latch_seed.ps1 --source both --last-sessions 20 --apply
```

It is review-first — the pass prints a structured report and writes only the candidates you approve.
The full walkthrough and source options are in [See it catch](#see-it-catch-seed-then-gate); to read
the installer first, pin a release, pick an intensity tier, or install to a custom directory, see
[Install details](#install-details).

## See it catch: seed, then gate

The fixture proves the mechanism. The value is catching a decision from *your* history that your
next agent would plausibly revive.

**1. Seed your history** (you did this in [Get started](#get-started) — here are the source
options). Use `--source claude`, `codex`, `cursor`, `both`, or `all`; `--apply` is review-first and
writes only the candidates you approve, while omitting it previews only. From a hooked Cursor
conversation, `/latch-seed` is the normal path; Cursor uses only that exact hook-provided transcript
and never scans its private history folders.

**2. Pick one strong proof target** the seed surfaced:

- a concrete rejected approach or governing rule,
- the reason or allowed alternative,
- source and current status evidence,
- enough specificity that another agent could plausibly violate it.

**3. Ask an agent to implement that rejected approach.** Expect a foreground **Latch gate** receipt
before edits, cited against your own decision. A `SKIPPED`, `recommendation: null`, empty-evidence,
or `PROCEED` result on a plainly violating request **is not the proof.**

**Prove no edits with your own eyes.** Capture `git status` before and after a real gate call:

```bash
git status --short > /tmp/latch-proof.before
/path/to/latch/bin/run_latch_gate.sh '<generated request>' | tee /tmp/latch-gate-proof.json
git status --short > /tmp/latch-proof.after
diff -u /tmp/latch-proof.before /tmp/latch-proof.after
```

The diff should be empty — the gate ran, and no files moved.

**No history to seed yet?** Run the public-safe fixture in a throwaway repo. It exercises the gate
path without touching your data:

```bash
/path/to/latch/bin/latch_demo_no_history.sh
# Windows: C:\path\to\latch\bin\latch_demo_no_history.ps1
```

The fixture is synthetic: it proves the gate fires, not that latch understood your history. The
[first-run mission](./docs/first_run_mission.md) and the
[proof-ready demo runbook](./runbooks/hook_proof_demo.md) carry the exact paths, success criteria,
and receipt checks.

## Supported agents

The guided quickstart wires whichever you choose.

| Agent | What latch installs | Boundary |
| --- | --- | --- |
| Claude Code | MCP tools, hooks, slash commands, `/latch-compact`, managed `CLAUDE.md` contract | Restart after install so tools and hooks load |
| Codex | Shared MCP tools + KB, user skills, `AGENTS.md`, SessionStart hook, Codex backend defaults | Start a new task after install; compaction is manual |
| Cursor | Project MCP, Rule, commands, skills, `AGENTS.md`; optional session/gate/activity hooks | Current-session seed/compact only; no historical transcript discovery |
| Multiple agents | One shared local latch KB | A decision captured through one agent can gate the others |

Cursor also needs three user-controlled live steps: authenticate with `agent login`, approve the
project MCP server, and enable **latch** in **Cursor Settings > Tools & MCP**. Static doctor success
alone does not prove the IDE gate is live. Per-surface manual install and doctor commands live in
[ARCHITECTURE.md](./ARCHITECTURE.md); see the [Cursor reference](./runbooks/cursor.md) and
[Cursor gate smoke](./runbooks/cursor_gate_smoke.md).

## Using latch day to day

You mostly do not operate latch. Once wired, the agent reads the KB before answering, captures
durable decisions as they happen, and runs `latch_gate` before coding-shaped changes — showing a
short foreground receipt when latch shapes an answer or a gate fires. Audit recent gate activity
without writing anything via `/latch-gate-report` or `bin/latch_gate_report.sh`.

At natural stopping points, capture the session so tomorrow's agent inherits today's judgment.
Compaction is user-initiated because it spends a model call and writes a durable summary into the KB:

- Claude Code: `/latch-compact`
- Codex: `/path/to/latch/bin/run_codex_compact_now.sh`
- Cursor: `/latch-compact` from the current hooked conversation

## Safety and control

**Local-first.** latch stores project judgment locally in SQLite and requires no cloud account. It
never uploads your KB. Data leaves your machine only when you run a model-backed path (gate,
compaction, heal), which may send selected prompts, snippets, and evidence to the Claude, Codex, or
Cursor backend *you* configured.

**Kill switch.** Stop latch hooks without uninstalling:

```bash
bash bin/latch_disable.sh
bash bin/latch_enable.sh
bash bin/latch_status.sh
```

**Unlatch.** Turn the automatic judgment layer off for this install, then back on. It masks latch's
managed `CLAUDE.md` / `AGENTS.md` regions while off; it does not delete the KB, uninstall latch, or
disable the agent's native tools:

```bash
bash bin/unlatch.sh
bash bin/unlatch.sh --confirm unlatch
bash bin/unlatch.sh --confirm latch
```

**Uninstall.** Preview or remove latch wiring. KB data is kept unless you pass `--purge`:

```bash
bash bin/uninstall.sh --dry-run
bash bin/uninstall.sh

# Remove only latch-owned Cursor wiring from the current project:
bash bin/uninstall.sh --yes --cursor-only --cursor-project "$PWD"
```

## Where the gate doesn't help

An honest guardrail names its limits.

- **It warns and cites; it doesn't handcuff.** The gate recommends — you can still choose to proceed.
  latch stops silent drift, not deliberate decisions.
- **An empty KB catches nothing.** With no captured judgment there is nothing to gate. Seed first.
- **A lost reason is reconstructed, not invented.** When the original rationale isn't recoverable,
  the agent presents a reconstruction for your confirmation rather than asserting it as fact.
- **The fixtures are instruments, not scorecards.** The bundled evals prove the gate path on small
  suites; they are not broad claims about every repo or model.

## Proof and limits

The checked-in [V1 public proof packet](./proof/README.md) pairs one observed model-backed receipt
with two small deterministic fixture suites:

| Evidence | Result | Meaning |
| --- | ---: | --- |
| Live pre-edit gate | `DO_NOT_PROCEED` | Cited a canonical rejected path; worktree unchanged |
| Decision-evidence fixture | `latch_full` 8/8; `memory_like` 4/8 | Small internal ablation, not a third-party benchmark |
| Seed-report fixture | 16/16 | Deterministic capture/filtering checks; zero model calls |

Regenerate them:

```bash
bash bin/latch_eval.sh
bash bin/latch_seed_report_eval.sh
bash bin/latch_proof_packet.sh --check
```

Read the `memory_like` row as an internal active-search-only ablation, not a benchmark of any
third-party product. The `latch_full` row is a gate-retrieval mode, **not** the Full intensity tier —
don't read it as a Quiet/Standard/Full comparison or a rebuild-savings claim. Details in
[benchmarks/README.md](./benchmarks/README.md).

## Install details

**What the installer does.** The bootstrap preserves your project path and installs a private,
isolated [`uv`](https://docs.astral.sh/uv/) + Python 3.11 runtime — it does not touch your shell
profile or require a system Python. It asks which agent surfaces to wire, asks how proactively latch
should surface judgment, runs their doctor checks, and offers a bounded initial-KB review: selected
transcripts are listed and redacted before any model call, and nothing is written until you approve
it. Rerunning the command repairs and reconciles the existing install while keeping its current
Latch source revision; it never silently switches to a newer release or commit. One installation
serves many repos; `--install-dir PATH` (PowerShell `-InstallDir PATH`) picks an
alternate location. Install internals and per-surface manual setup live in
[ARCHITECTURE.md](./ARCHITECTURE.md).

**Pass quickstart options** through the piped Bash form after `bash -s --`:

```bash
curl --proto '=https' --tlsv1.2 -LsSf https://raw.githubusercontent.com/open-latch/latch/main/install.sh | bash -s -- --agents both
```

**Inspect or pin before you run.** To inspect either installer first, download the script and read it
locally. The piped command follows `main`; for a stable build, pin both the script and the checkout
ref so the install cannot drift back to `main`:

```bash
curl --proto '=https' --tlsv1.2 -LsSf https://raw.githubusercontent.com/open-latch/latch/vX.Y.Z/install.sh | LATCH_INSTALL_REF=vX.Y.Z bash
```

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/open-latch/latch/vX.Y.Z/install.ps1))) -Ref vX.Y.Z
```

### Choose Latch intensity

Quickstart also asks, **“How proactively should Latch surface project
judgment?”** The choice is install-wide: it applies to every project and host
using that Latch installation.

| Level | Automatic surfacing | Honest tradeoff |
| --- | --- | --- |
| Quiet | Up to 1 workstream and 1 open question at startup; no hook-added similarity hits | Lowest ambient context; contract-driven Latch reads and the gate still surface prior judgment |
| Standard | Lightweight local topic-similarity check on each eligible prompt; injects up to 3 KB hits only on the first prompt or a topic change; startup brief up to 3 workstreams, 2 questions, and 2 ideas | Fresh-install default; gives up same-topic injection, 2 hit slots, Full no-hit receipts, Full guideline nudges, and the broader brief |
| **Full — best protection** | Up to 5 KB hits on every eligible prompt, including same-topic follow-ups; startup brief up to 5 workstreams, 3 questions, and 5 ideas; explicit no-hit receipts and standing-guideline capture nudges | Uses the most prompt context; recommended for long-lived, multi-agent, handoff-heavy, or costly-to-rebuild projects |

Every tier keeps the static managed project contract, including its live Latch
read before each response, plus correction reminders where a prompt hook
supports them. Intensity controls hook-added briefs and prompt context—not
whether the agent can or should query Latch. The same gate check and
configuration run when invoked. That does **not** promise identical evidence,
catches, or outcomes: automatic context and project state can differ between
runs.

Host capabilities bound what intensity can change:

| Host | Intensity-controlled runtime surface |
| --- | --- |
| Claude Code | Startup brief and similarity-based prompt surfacing |
| Codex | Startup brief; Codex has no similarity-based prompt hook |
| Cursor with hooks | Startup brief; the mechanical pre-edit gate remains enabled |
| Cursor without hooks | No current intensity-controlled runtime surface; managed guidance remains unchanged |

Quickstart and the installers default a genuinely fresh install to Standard and
save that choice in `latch_settings.json`. During quickstart, a settings-less
install with existing KB evidence is treated as an older install and its
previously shipped Full behavior is saved. A manually wired, settings-less
runtime does not inspect KB evidence; it resolves to legacy Full.

At ordinary runtime, `LATCH_INTENSITY` is a process-scoped override and does not
edit the saved choice. If a valid `LATCH_INTENSITY` is present while quickstart
runs, however, quickstart treats it as an explicit installation choice and
persists it install-wide on apply. Unset it before quickstart if the override
was only a temporary experiment. You can choose Full non-interactively:

```bash
bash install.sh --agents both --latch-intensity full
```

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/open-latch/latch/main/install.ps1))) -LatchIntensity full
```

Retier the whole installation later without reinstalling:

```bash
/path/to/latch/bin/latch_intensity.sh full
# Windows: & C:\path\to\latch\bin\latch_intensity.ps1 full
```

Latch does not yet claim a universal percentage, time, or dollar reduction in
rebuild work. In the frozen `intensity_v1` policy fixture, the expected
guardrail reference is present in ambient hook context for `0/5` Quiet, `2/5`
Standard, and `5/5` Full constructed opportunities. Hook-emitted context across
the seven synthetic prompt events is `0`, `1,712`, and `3,056` characters. That
count excludes startup briefs, the static contract, explicit tool-call context,
correction/profile nudges, latency, and actual reconstruction work. Authored
scores and relative risk weights make the result true by construction: it is a
policy regression contract, not a retrieval-quality benchmark, observed
developer savings, or proof that the agent noticed or used the reference.
Read the checked-in
[`intensity_v1` receipt](./benchmarks/results/intensity_v1_receipt.json) or
regenerate that exact portable artifact atomically with:

```bash
bash bin/latch_intensity_eval.sh --write-receipt
# Windows: .\bin\latch_intensity_eval.ps1 --write-receipt
```

The existing decision-evidence benchmark separately tests whether gate
retrieval finds the right rejected paths and rationale. Intensity is recorded
in local structural prompt, gate, correction, reconciliation, and gate-outcome
events so future multi-turn evals can replace proxy weights with observed,
scenario-bounded rebuild outcomes.

## Versions and platform

Stable builds use immutable `vX.Y.Z` Git tags and matching Releases; `main` is development code, not
the update channel. Check and apply updates explicitly:

```bash
bash bin/latch_version.sh          # Windows: .\bin\latch_version.ps1
bash bin/latch_update.sh --check
bash bin/latch_update.sh --dry-run
bash bin/latch_update.sh --yes
```

The updater operates only on a clean official `open-latch/latch` clone, backs up every discovered KB
before a schema upgrade, and refuses to open a KB written by a newer schema.

**Python >= 3.11** (3.12 and 3.13 also work), native architecture. On Apple Silicon use a native
arm64 Python — a Rosetta/Intel venv can crash sqlite-vec at extension-load time. Verify with:

```bash
python3 -c "import platform; print(platform.machine())"
```

It should print `arm64`; the doctor detects the mismatch and prints the remedy.

## License and public boundary

Source in this repository is licensed under the Apache License, Version 2.0. See
[LICENSE](./LICENSE), [LICENSING.txt](./LICENSING.txt), and [NOTICE](./NOTICE).

This public repo is the local single-player decision-seatbelt core: install, doctor, seed/report,
local KB, `latch_gate`, receipts, evals, and Claude Code / Codex / Cursor adapter wiring —
inspectable, forkable, and useful without a cloud account. The latch name and branding are not
licensed under Apache 2.0; see [TRADEMARK.md](./TRADEMARK.md) and [CONTRIBUTING.md](./CONTRIBUTING.md).

## Documentation

| Start here | Go deeper |
| --- | --- |
| [First-run mission](./docs/first_run_mission.md) | [Proof-ready demo](./runbooks/hook_proof_demo.md) |
| [Public proof packet](./proof/README.md) | [Benchmarks](./benchmarks/README.md) |
| [Cursor reference](./runbooks/cursor.md) | [Cursor gate smoke](./runbooks/cursor_gate_smoke.md) |
| [Architecture](./ARCHITECTURE.md) | [Shared MCP runtime](./docs/mcp_resource_architecture.md) |

Install internals, per-surface manual install, and maintenance machinery live in
[ARCHITECTURE.md](./ARCHITECTURE.md). This README stays focused on the local decision-seatbelt
workflow.
