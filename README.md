<p align="center">
  <img src="./docs/assets/latch-logo.svg" alt="latch" width="520">
</p>

<p align="center">
  <strong>A decision seatbelt for coding agents.</strong>
</p>

<p align="center">
  Local-first &middot; reversible &middot; stops agents before they repeat paths you already ruled out.
</p>

<p align="center">
  <a href="#the-first-proof">First proof</a> &middot;
  <a href="#get-started">Get started</a> &middot;
  <a href="#supported-agents">Supported agents</a> &middot;
  <a href="#safety-and-control">Safety</a>
</p>

---

**TL;DR:** install latch once, run the guided quickstart from a project repo,
choose Claude Code, Codex, Cursor, or all three, choose how proactively Latch
should surface project judgment, and seed recent local sessions. Then ask an
agent to violate a saved decision. latch should show a cited gate receipt before
edits instead of silently reviving the rejected path.

Coding agents do not just forget facts. They forget why a project chose one
path and rejected another. That is where drift starts: an agent re-litigates a
settled decision, violates a governance rule, or rebuilds the plausible thing
you already ruled out.

latch keeps cited decisions, rejected paths, rationale, and source evidence in
a local project KB, then puts that judgment in the agent's path before files
change. It is decision continuity, not a larger transcript or a generic memory
layer.

latch runs locally, uses one SQLite KB store, needs no cloud account, and
targets macOS, Windows, and Linux with bash and PowerShell wrappers. Claude
Code, Codex, and Cursor can share the same KB, so judgment captured through one
agent can gate another.

## The First Proof

**Seed recent sessions -> choose one rejected path -> ask an agent to violate
it -> see a cited receipt before edits.**

Example: your project previously rejected Redis-backed background jobs for
local work. Ask an agent to add Redis-backed email jobs. latch should cite the
saved rejection, explain the rationale, and recommend the compliant path before
files change.

A live receipt should make latch's role obvious:

```text
Latch gate receipt:
Latch ran latch_gate on the request.
Recommendation: MODIFY or DO_NOT_PROCEED
Summary: the request conflicts with the saved "no background job queue" decision.
Risk if proceed: adding Redis and a worker repeats the rejected queue path.
Cited evidence:
- id=1 decision status=canonical: No background job queue for the no-history demo app
```

The checked-in [V1 public proof packet](./proof/README.md) separates one observed
model-backed receipt from small deterministic fixture suites:

| Evidence | Result | Meaning |
| --- | ---: | --- |
| Live pre-edit gate | `DO_NOT_PROCEED` | Cited a canonical rejected path; worktree unchanged |
| Decision-evidence fixture | `latch_full` 8/8; `memory_like` 4/8 | Small internal ablation, not a third-party benchmark |
| Seed-report fixture | 16/16 | Deterministic capture/filtering checks; zero model calls |

The `latch_full` row is a gate-retrieval mode, not the Full intensity tier. It
must not be used as a Quiet/Standard/Full comparison or as a rebuild-savings
claim.

No useful history yet? Run the public-safe fixture in a throwaway repo:

```bash
/path/to/latch/bin/latch_demo_no_history.sh
# Windows: C:\path\to\latch\bin\latch_demo_no_history.ps1
```

That fixture is synthetic; it proves the gate path, not that latch understood a
particular user's history. The real first-value path is to seed your own recent
sessions and catch one decision your next agent might plausibly violate.

## Get Started

Prerequisites: **Git** and at least one installed agent CLI: **Claude Code**,
**Codex**, or **Cursor Agent**. The installer bootstraps a private
[`uv`](https://docs.astral.sh/uv/) and native Python 3.11 environment; it does
not modify your shell profile or require a system Python. Release installs use
the repository's hashed, cross-platform dependency lock.

Open a terminal in the project repo you want latch to protect, then run one
command.

macOS, Linux, or Windows Git Bash:

```bash
curl --proto '=https' --tlsv1.2 -LsSf https://raw.githubusercontent.com/open-latch/latch/main/install.sh | bash
```

To forward quickstart options through the piped Bash form, pass them after
`bash -s --`, for example:

```bash
curl --proto '=https' --tlsv1.2 -LsSf https://raw.githubusercontent.com/open-latch/latch/main/install.sh | bash -s -- --agents both
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/open-latch/latch/main/install.ps1 | iex
```

The bootstrap preserves your starting project path, downloads Latch into the
platform user-data directory, installs its isolated runtime, asks which agent
surfaces to wire, runs their doctor checks, and offers the bounded initial-KB
review. Selected local transcripts are listed and redacted before any model
call; no seed candidate is written until you approve it.

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

The current pre-release command follows `main` and prints the exact installed
commit. For a stable release, pin both the downloaded script and the checkout
ref in the same command so the install cannot drift back to `main`:

```bash
curl --proto '=https' --tlsv1.2 -LsSf https://raw.githubusercontent.com/open-latch/latch/vX.Y.Z/install.sh | LATCH_INSTALL_REF=vX.Y.Z bash
```

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/open-latch/latch/vX.Y.Z/install.ps1))) -Ref vX.Y.Z
```

To inspect either installer before executing it, download the script first and
read it locally.

Rerunning the command is a repair/reconcile operation: it keeps the installed
source revision and idempotently refreshes dependencies and wiring. It never
silently upgrades. A local script invocation can request an explicit upgrade,
which refuses dirty source checkouts:

```bash
bash /path/to/install.sh --upgrade --ref main
# PowerShell: .\install.ps1 -Upgrade -Ref main
```

For a non-interactive or local-clone invocation, pass quickstart choices after
the bootstrap options:

```bash
bash install.sh --agents both
bash install.sh --agents cursor --cursor-with-hooks
bash install.sh --agents all --cursor-with-hooks --latch-intensity full
```

Default app locations are `~/.local/share/latch/app` on Linux,
`~/Library/Application Support/Latch/app` on macOS, and
`%LOCALAPPDATA%\Latch\app` on Windows. Use `--install-dir PATH` or PowerShell
`-InstallDir PATH` to choose another stable location. One installation serves
many repos; rerun its `bin/latch_quickstart` wrapper from each project that
should receive the agent contract.

If you are developing Latch from this source checkout, the same path works
without a remote download:

```bash
bash install.sh --install-dir "$PWD" --project /path/to/your/project --agents codex
# Existing developer environments can continue to run bin/latch_quickstart directly.
```

The initial quickstart defaults to available Claude/Codex history because no
Cursor conversation exists yet. After opening a hooked Cursor conversation,
run `/latch-seed`; Cursor uses only that exact hook-provided transcript.

## First Value: Seed, Then Gate

Do not start with a blank KB if you have prior local sessions. Start with the
smallest useful review-and-apply scan:

```bash
/path/to/latch/bin/latch_seed.sh --source both --last-sessions 20 --apply
# Windows: C:\path\to\latch\bin\latch_seed.ps1 --source both --last-sessions 20 --apply
```

Use `--source claude`, `codex`, `cursor`, `both`, or `all`. Cursor source
resolution is intentionally narrow: it uses the exact current SessionStart
marker named by `--cursor-session-id`, or a transcript path you supply
explicitly. It never scans Cursor's private history folders.

From a hooked Cursor conversation, `/latch-seed` is the normal path. The
equivalent preview command is:

```bash
/path/to/latch/bin/latch_seed.sh --source cursor --cursor-session-id SESSION_ID --format json
# Review first; apply only the candidates you approve.
```

`--apply` is still review-first: the seed pass prints a structured report and
writes only the staging candidates you approve. Omit `--apply` for preview-only
operation. Keep the default scan focused; increase `--last-sessions N` only if
the first report does not find useful project judgment.

Look for one strong proof target:

- a concrete rejected approach or governing rule,
- the reason or allowed alternative,
- source and current status evidence,
- enough specificity that another agent could plausibly violate it.

Then run the printed `/latch-gate` or `bin/run_latch_gate.sh` catch-demo command,
or ask an agent to implement that rejected approach. Expect a foreground
**Latch gate** receipt before edits: latch ran the gate, cited the saved
decision/rationale/source/status, explained the conflict, and recommended the
compliant path. `SKIPPED`, `recommendation: null`, empty evidence, or `PROCEED`
on a plainly violating request is not the proof.

For a shell proof, capture a no-edit receipt:

```bash
git status --short > /tmp/latch-proof.before
/path/to/latch/bin/run_latch_gate.sh '<generated request>' | tee /tmp/latch-gate-proof.json
git status --short > /tmp/latch-proof.after
diff -u /tmp/latch-proof.before /tmp/latch-proof.after
```

The diff should be empty. If the first pass is weak, widen the session window
once or switch sources before falling back to the no-history fixture. The short
[first-run mission](./docs/first_run_mission.md) and the
[proof-ready demo runbook](./runbooks/hook_proof_demo.md) carry the exact paths,
success criteria, and receipt checks.

## Supported Agents

| Agent | What latch installs | Important boundary |
| --- | --- | --- |
| Claude Code | MCP tools, hooks, slash commands, `/latch-compact`, managed `CLAUDE.md` contract | Restart after install so tools and hooks load |
| Codex | Shared MCP tools and KB, user skills, `AGENTS.md`, SessionStart hook, Codex model backend defaults | Start a new task after install; compaction is manual |
| Cursor | Project MCP, Rule, commands, skills, `AGENTS.md`; optional session/gate/activity hooks | Current-session seed/compact only; no historical transcript discovery |
| Multiple agents | One shared local latch KB | A decision captured through one agent can gate the others |

The guided quickstart is the recommended path. These are the underlying manual
install and verification commands when you need one surface by itself.

### Claude Code

Install and open Claude Code once before applying latch; the installer stops
before writing config if the `claude` CLI is missing.

```bash
# From the latch repo root.
bash bin/install_engine.sh         # Windows: .\bin\install_engine.ps1

# From each project repo where Claude Code should follow latch.
/path/to/latch/bin/install_claude_md.sh --yes
# Windows: C:\path\to\latch\bin\install_claude_md.ps1 --yes

bash /path/to/latch/bin/install_engine.sh --check
bash /path/to/latch/bin/latch_doctor.sh
```

### Codex

The installer adds the `latch` MCP server to Codex `config.toml`, syncs Latch's
bundled workflows into `$HOME/.agents/skills` (on Windows,
`%USERPROFILE%\.agents\skills`), installs the contract into `AGENTS.md`, and
adds the SessionStart hook. Existing installs using the legacy `claude-kb`
server key are migrated when the managed block is refreshed. Same-named
user-owned skills are never overwritten.

```bash
# From the project repo where Codex should follow latch.
/path/to/latch/bin/install_codex.sh --yes
# Windows: C:\path\to\latch\bin\install_codex.ps1 --yes

/path/to/latch/bin/install_codex.sh --check
/path/to/latch/bin/latch_codex_doctor.sh
```

Codex exposes installed workflows through `/skills` or an explicit skill
mention such as `$source-command-latch-compact`; it does not create top-level
`/latch-*` slash commands. Codex can also select a workflow implicitly from its
description. It detects new skills automatically; restart Codex if a newly
installed workflow does not appear.

### Cursor

Cursor is project-scoped. The installer owns `.cursor/mcp.json`, the latch
Rule, project commands and skills, and the managed `AGENTS.md` region; pass
`--with-hooks` for SessionStart context and fail-closed pre-edit gate
enforcement. It preserves unrelated Cursor configuration.

```bash
# From the project repo where Cursor should follow latch.
/path/to/latch/bin/install_cursor.sh --yes --with-hooks
# Windows: C:\path\to\latch\bin\install_cursor.ps1 --yes --with-hooks

/path/to/latch/bin/install_cursor.sh --check --with-hooks
/path/to/latch/bin/latch_cursor_doctor.sh --with-hooks
```

Cursor also requires three user-controlled live prerequisites: authenticate the
Agent CLI with `agent login`, approve the project MCP server, and enable
**latch** for the workspace in **Cursor Settings > Tools & MCP**. Static doctor
success or CLI visibility alone does not prove the IDE gate is live.

Read the [Cursor reference](./runbooks/cursor.md) for the complete vetted
hook, privacy, current-session, backend, doctor, and plugin boundaries. Use the
[Cursor gate smoke](./runbooks/cursor_gate_smoke.md) for live acceptance.

## Using It Day To Day

You mostly do not operate latch. Once wired, the agent reads the KB before
answering, captures durable decisions as they happen, and runs `latch_gate`
before coding-shaped changes. When latch affects an answer, the agent should
show a short foreground receipt naming what it read or which gate fired. Audit
recent gate activity without writing anything with `/latch-gate-report` or
`bin/latch_gate_report.sh`.

At natural stopping points, capture the session:

- Claude Code: `/latch-compact`
- Codex: `/path/to/latch/bin/run_codex_compact_now.sh`
- Cursor: `/latch-compact` from the current hooked conversation, or the
  `run_cursor_compact_now` wrapper with the surfaced session id

Compaction is user-initiated because it spends a model call and writes a durable
summary into the KB.

latch shares its heavyweight local MCP/model runtime within each pinned vault
instead of loading a separate embedding model per task. The operational contract
and benchmark evidence live in
[`docs/mcp_resource_architecture.md`](./docs/mcp_resource_architecture.md).

## Safety And Control

**Local-first storage.** latch stores project judgment locally in SQLite and
does not require a cloud account.

**No latch cloud.** latch does not upload your KB to a latch service. Data leaves
your machine only when you run a model-backed path using the Claude, Codex,
Cursor, or other backend you configured; those calls may send selected prompts,
snippets, and evidence context to that backend. Local eval runners use throwaway
KBs and do not read or write your live project DB.

**Kill switch.** Stop latch hooks without uninstalling:

```bash
bash bin/latch_disable.sh
bash bin/latch_enable.sh
bash bin/latch_status.sh
```

**Unlatch.** Turn the automatic judgment layer off for this install, then turn
it back on when ready:

```bash
bash bin/unlatch.sh
bash bin/unlatch.sh --confirm unlatch
bash bin/unlatch.sh --confirm latch
```

Unlatched mode is install-level and masks latch's managed `CLAUDE.md` /
`AGENTS.md` regions while off. It does not clone the project, delete the KB,
uninstall latch, call a latch cloud service, collect telemetry, or disable the
agent's native memory, model context, repo access, or other tools. It is a rough
vanilla-agent sanity check, not a controlled benchmark.

**Uninstall.** Preview or remove latch wiring. KB data is kept unless you pass
`--purge`:

```bash
bash bin/uninstall.sh --dry-run
bash bin/uninstall.sh

# Remove only latch-owned Cursor wiring from the current project:
bash bin/uninstall.sh --dry-run --cursor-only --cursor-project "$PWD"
bash bin/uninstall.sh --yes --cursor-only --cursor-project "$PWD"
```

## Proof Discipline And Limits

latch's local evals ask the first-OSS question directly: can the agent surface
binding project judgment, rejected paths, stale/reconciled status, the documented
why behind decisions, and visible gate receipts?

```bash
bash bin/latch_eval.sh
bash bin/latch_seed_report_eval.sh
bash bin/latch_proof_packet.sh --check
```

Read the benchmark as a comparison against the checked-in `memory_like`
ablation, not a generic scorecard or a benchmark of a third-party memory
product. The small fixtures are proof instruments, not broad claims about every
repository or model. When a prior reason is not recoverable, the agent should
present a reconstruction for confirmation rather than assert it as fact.

See [benchmarks/README.md](./benchmarks/README.md) for fixture and JSON report
details and the [public proof packet](./proof/README.md) for the observed receipt,
exact boundaries, and reproduction commands.

Proof verification checks the tooling commit immediately before the generated
artifacts. In a depth-1 clone, run `git fetch --deepen=2` before retrying
`bash bin/latch_proof_packet.sh --check`; repeat if necessary or fetch the full
history.

## Versions And Updates

Stable latch builds use immutable `vX.Y.Z` Git tags and matching GitHub Releases.
The moving `main` branch is development code, not the update channel.

| Version | Purpose |
| --- | --- |
| `LATCH_VERSION` (`VERSION`) | User-facing release |
| `KB_SCHEMA_VERSION` | Local SQLite compatibility |
| `WIRING_VERSION` | Copied project integration files |

Show installed versions, check for an update, preview it, then apply explicitly:

```bash
bash bin/latch_version.sh          # Windows: .\bin\latch_version.ps1
bash bin/latch_version.sh --json
bash bin/latch_update.sh --check
bash bin/latch_update.sh --dry-run
bash bin/latch_update.sh --yes
```

The updater operates only on a clean official `open-latch/latch` clone on
`main` or an existing release checkout. It refuses modified files, development
branches, forks, and ambiguous origins; installs an exact release tag; refreshes
dependencies and existing Claude command copies; and backs up every discovered
local KB before a schema upgrade. latch refuses to open a KB written by a newer
schema rather than guessing or downgrading it.

Already-wired projects carry a small wiring version in their latch-owned marker.
On the next SessionStart, the agent performs a local marker-only comparison:
current wiring is silent and write-free, older managed wiring is repaired once
with a receipt and backup, and newer wiring is never downgraded. Unmanaged repos
remain untouched. Cursor without hooks performs the same check when its existing
project MCP server starts. Restart the relevant agent when a receipt says hooks
or MCP configuration changed.

Release tags are forward updates. Restoring code across a KB schema change also
requires restoring the corresponding `kb.db.bak.schema-*` backup.

## Platform Notes

- **Python >= 3.11**, native-architecture. Python 3.12 and 3.13 also work.
- **uv** is the recommended venv/dependency installer. In Git Bash on Windows,
  activate the venv with `source .venv/Scripts/activate`.
- A working `python` must be on `PATH`, or set `LATCH_PYTHON` to its absolute
  path (`CLAUDE_KB_PYTHON` remains a legacy alias).

### Apple Silicon Arm64

Use a native arm64 Python. A venv built with an Intel Python under Rosetta
installs x86_64 wheels, and sqlite-vec's prebuilt x86_64 binary can crash at
extension-load time. Verify with:

```bash
python3 -c "import platform; print(platform.machine())"
```

It should print `arm64`. The doctor detects the mismatch and prints the remedy.

## License And Public Boundary

The source code in this repository is licensed under the Apache License,
Version 2.0. See [LICENSE](./LICENSE), [LICENSING.txt](./LICENSING.txt), and
[NOTICE](./NOTICE).

This public repo is the local single-player decision-seatbelt core: install,
doctor, seed/report, local KB, `latch_gate`, receipts, evals, and Claude Code /
Codex / Cursor adapter wiring. It is intended to be inspectable, forkable, and
useful without a cloud account.

The latch name and branding are not licensed under Apache 2.0. See
[TRADEMARK.md](./TRADEMARK.md) for the trademark guidelines and
[CONTRIBUTING.md](./CONTRIBUTING.md) for contribution terms, including
AI-assisted contribution guidance.

## Documentation

| Start here | Go deeper |
| --- | --- |
| [First-run mission](./docs/first_run_mission.md) | [Proof-ready demo](./runbooks/hook_proof_demo.md) |
| [Public proof packet](./proof/README.md) | [Benchmarks](./benchmarks/README.md) |
| [Cursor reference](./runbooks/cursor.md) | [Cursor gate smoke](./runbooks/cursor_gate_smoke.md) |
| [Architecture](./ARCHITECTURE.md) | [Shared MCP runtime](./docs/mcp_resource_architecture.md) |

Install internals, maintenance machinery, and contributor details live in
[ARCHITECTURE.md](./ARCHITECTURE.md). The README stays focused on the local
decision-seatbelt workflow.
