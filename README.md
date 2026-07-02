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
  <a href="#guided-quickstart">Quickstart</a> &middot;
  <a href="#the-first-proof">First proof</a> &middot;
  <a href="#rejected-path-demo">Rejected-path demo</a> &middot;
  <a href="#safety">Safety</a>
</p>

---

**TL;DR:** install latch once, run the quickstart script from a project repo,
choose Claude Code, Codex, or both, seed recent local sessions, then ask an
agent to violate a saved decision. latch should show a gate receipt before
edits, citing the decision, reason, source, and status it is enforcing.

Claude Code and Codex are powerful, but they can forget why a project chose one
path and rejected another. That is where drift starts: an agent re-litigates a
settled decision, violates a governance rule, or rebuilds the plausible thing
you already ruled out.

latch keeps those decisions, rejected paths, rationale, and source evidence in a
local project KB, then puts that judgment in the agent's path before files
change. Use Claude Code, Codex, or both. When you use both, they share the same
local latch KB, so a decision captured in one agent can gate work in the other.

latch runs locally, uses one SQLite KB store, needs no cloud account, and
targets macOS, Windows, and Linux with bash and PowerShell wrappers.

## The First Proof

Seed recent sessions -> pick a rejected path -> ask an agent to violate it ->
see a receipt before edits.

Example: your project previously rejected Redis-backed background jobs for local
work. Ask Claude Code or Codex to add Redis-backed email jobs. latch should cite
the saved rejection, explain the rationale, and recommend the compliant path
before files change.

The detailed run:

1. Run the guided quickstart from a real project repo.
2. Choose Claude Code, Codex, or both.
3. Confirm the doctor/check output says latch is connected.
4. Seed latch from recent local Claude/Codex sessions.
5. Review the structured seed report and approve useful staging evidence.
6. Pick the strongest 1-3 rejected-path or rule examples.
7. Ask a coding agent to violate one of them.
8. Expect a foreground **Latch gate** receipt before edits: latch ran the gate,
   cited the saved decision/rationale/source/status, and recommended the
   compliant path. The agent should not silently proceed.

That is the first product proof: prior judgment becomes a visible gate in the
next agent's path.

See [docs/first_run_mission.md](./docs/first_run_mission.md) for the short
first-run mission.

## Supported Now

- **Claude Code:** MCP tools, hooks, slash commands, `/latch-compact`, and the
  managed `CLAUDE.md` behavior contract.
- **Codex:** the same KB and MCP tools with Codex-specific `AGENTS.md`,
  SessionStart, Codex backend defaults, and a manual compaction wrapper.
- **Claude Code + Codex together:** one shared local latch KB, so decisions and
  rejected paths captured through either agent can gate both.

## Guided Quickstart

Prerequisites: **Claude Code or Codex**, **Python >= 3.11** on a
native-architecture interpreter, and [`uv`](https://docs.astral.sh/uv/)
recommended. If `uv` is not installed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Install latch once in a stable location such as `~/tools/latch` or
`D:\tools\latch`. From the cloned latch repo root:

```bash
cd /path/to/latch
uv venv --python 3.11 .venv
source .venv/bin/activate          # Windows Git Bash: source .venv/Scripts/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
uv pip install -r requirements.txt

bash bin/latch_doctor.sh           # Windows: .\bin\latch_doctor.ps1
```

Then run the quickstart from the project repo you want latch to wire:

```bash
/path/to/latch/bin/latch_quickstart.sh
# Windows: C:\path\to\latch\bin\latch_quickstart.ps1
```

The quickstart asks whether to wire Claude Code, Codex, or both. For
non-interactive runs, choose explicitly:

```bash
/path/to/latch/bin/latch_quickstart.sh --agents both
/path/to/latch/bin/latch_quickstart.sh --agents claude
/path/to/latch/bin/latch_quickstart.sh --agents codex
```

The quickstart delegates to the existing installers, syncs the project behavior
contract, runs doctor/check commands, then moves directly into seed-first setup.
It disables the per-installer seed prompts so there is one seed handoff at the
end, with the source set to `claude`, `codex`, or `both` from your choice.

One latch clone can serve many repos. Run the quickstart script again from each
project repo where you want the agent behavior contract. The manual steps below
are the underlying commands when you need to debug or drive one surface by hand.

### Claude Code

Install and open Claude Code once before applying latch; the installer
preflights for the `claude` CLI and stops before writing config if it is
missing.

```bash
# From the latch repo root.
bash bin/install_engine.sh         # Windows: .\bin\install_engine.ps1

# From each project repo where Claude Code should follow latch.
/path/to/latch/bin/install_claude_md.sh --yes
# Windows: C:\path\to\latch\bin\install_claude_md.ps1 --yes

# Verify any time.
bash /path/to/latch/bin/install_engine.sh --check
bash /path/to/latch/bin/latch_doctor.sh
```

Restart Claude Code after install so the tools and hooks load.

### Codex

Codex uses the same latch KB and MCP tool surface as Claude Code, with
Codex-specific wiring. The installer adds the `latch` MCP server to Codex
`config.toml`, installs the latch contract into `AGENTS.md`, and adds a
Codex `SessionStart` hook that surfaces the KB brief. It also sets
`LATCH_MODEL_BACKEND=codex` and `LATCH_GATE_BACKEND=codex`, so model-backed
`latch_gate`, heal, and tree calls use `codex exec` instead of quietly shelling out
to Claude.

Existing Codex configs that still use the old `claude-kb` server key are
recognized as a supported legacy alias; rerunning the installer migrates the
managed block to `latch`.

After installing latch's Python dependencies, run from the project root where
you want Codex to use latch:

```bash
# From the project repo where Codex should follow latch.
/path/to/latch/bin/install_codex.sh --yes
# Windows: C:\path\to\latch\bin\install_codex.ps1 --yes

# Verify any time.
/path/to/latch/bin/install_codex.sh --check
/path/to/latch/bin/latch_codex_doctor.sh
```

Restart Codex or start a new Codex thread after install so `config.toml`,
`hooks.json`, and `AGENTS.md` reload.

### Both Agents

Run both wiring sections. They intentionally point at the same local latch KB.
That cross-agent path is part of the first OSS value: Claude Code can capture a
decision, Codex can later hit the gate for it, and vice versa.

## Start By Seeding

After install, do not start with a blank KB if you have prior local sessions.
The quickstart prints a review-and-apply seed command like this:

```bash
/path/to/latch/bin/latch_seed.sh --source both --last-sessions 20 --apply
# Windows: C:\path\to\latch\bin\latch_seed.ps1 --source both --last-sessions 20 --apply
```

Use `--source claude`, `--source codex`, or `--source both`. Keep the default
small and focused; increase `--last-sessions N` only when the first report does
not find useful project judgment.

`--apply` is still review-first. The seed pass may use LLM calls, shows a
structured report, and writes only the staging candidates you approve at the
prompt. Omit `--apply` when you want a preview-only run:

```bash
/path/to/latch/bin/latch_seed.sh --source both --last-sessions 20
```

The report is the first value moment. Look for:

- decisions and the reasons behind them,
- rejected paths or approaches already ruled out,
- governance rules the agent should respect,
- source/status receipts showing where the evidence came from,
- a printed catch-demo command when a rejected path is available.

## Rejected-Path Demo

Keep the demo narrow. Use the strongest 1-3 rejected-path or governance-rule
examples from the seed report, then choose one to test.

1. Apply the seed evidence you approve.
2. Run the printed `/latch-gate` or `bin/run_latch_gate.sh` catch-demo command, or ask
   Claude Code/Codex to implement the rejected approach.
3. Expect a foreground **Latch gate** receipt: latch ran the gate, cited the
   saved decision/rationale/source/status, explained the conflict, and
   recommended the compliant path before file edits. The agent should not
   silently proceed.

If the first pass does not find a strong example, go wider on purpose:
increase `--last-sessions N`, switch sources, or use the no-history mission in
[docs/first_run_mission.md](./docs/first_run_mission.md). Do not make "scan
everything" the default.

## Using It Day To Day

You mostly do not operate latch. Once wired, the agent reads the KB before
answering, captures durable decisions as they happen, and runs `latch_gate` before
coding-shaped changes. When latch affects an answer, the agent should show a
short foreground receipt naming what it read or which gate fired.
To audit recent gate activity without writing anything, run `/latch-gate-report`
or `bin/latch_gate_report.sh`.

At natural stopping points, capture the session:

- Claude Code: run `/latch-compact`.
- Codex: run `/path/to/latch/bin/run_codex_compact_now.sh`.

Compaction is user-initiated because it spends a model call and writes a durable
summary into the KB.

## Safety

**Local-first storage.** latch stores project judgment locally in SQLite. It
does not require a cloud account.

**No latch cloud.** latch does not upload your KB to a latch service. Data
leaves your machine only when you run a model-backed path that uses the Claude,
Codex, or other backend you configured; those calls may send selected prompts,
snippets, and evidence context to that backend. Local eval runners use
throwaway KBs and do not read or write your live project DB.

**Kill switch.** If latch misbehaves, stop its hooks without uninstalling:

```bash
bash bin/latch_disable.sh
bash bin/latch_enable.sh
bash bin/latch_status.sh
```

**Uninstall.** Preview or remove latch wiring. KB data is kept unless you pass
`--purge`:

```bash
bash bin/uninstall.sh --dry-run
bash bin/uninstall.sh
```

## Proof Discipline

latch's local evals ask the first-OSS question directly: can the agent surface
binding project judgment, rejected paths, stale/reconciled status, the real why
behind decisions, and visible gate receipts?

Read the benchmark as a comparison against memory-like baselines, not as a
generic scorecard. The useful question is whether `latch_full` keeps recovering
current decision evidence when ordinary memory would miss stale rejected paths,
reconciliation context, or the documented why.

```bash
bash bin/latch_eval.sh
bash bin/latch_seed_report_eval.sh
```

See [benchmarks/README.md](./benchmarks/README.md) for fixture and JSON report
details.

## License And Public Boundary

The source code in this repository is licensed under the Apache License,
Version 2.0. See [LICENSE](./LICENSE) for the full license text and
[LICENSING.txt](./LICENSING.txt) for the copyright notice and license summary.
Third-party attribution notices for vendored assets are in [NOTICE](./NOTICE).

This public repo is the local single-player decision-seatbelt core: install,
doctor, seed/report, local KB, `latch_gate`, receipts, evals, and Claude Code /
Codex wiring. It is intended to be inspectable, forkable, and useful without a
cloud account.

The latch name and branding are not licensed under Apache 2.0. See
[TRADEMARK.md](./TRADEMARK.md) for lightweight trademark guidelines and
[CONTRIBUTING.md](./CONTRIBUTING.md) for contribution terms, including
AI-assisted contribution guidance.

## Prerequisites And Gotchas

- **Claude Code or Codex** for the integrated agent workflow.
- **Python >= 3.11**, native-architecture. Below 3.11 is unsupported (the latest
  numpy requires 3.11+). 3.12 / 3.13 work.
- **uv** is the recommended venv/dependency installer. In Git Bash on Windows,
  install it first with `curl -LsSf https://astral.sh/uv/install.sh | sh`, then
  activate the venv with `source .venv/Scripts/activate`.
- A working `python` on PATH, or set `LATCH_PYTHON` to its absolute path
  (`CLAUDE_KB_PYTHON` remains a legacy alias).

### Apple Silicon Arm64

Use a native arm64 Python. A venv built with an Intel Python under Rosetta
installs x86_64 wheels, and sqlite-vec's prebuilt x86_64 binary can crash at
extension-load time. Verify with:

```bash
python3 -c "import platform; print(platform.machine())"
```

It should print `arm64`. The doctor detects the mismatch and prints the remedy.

## Contributing And Internals

Install internals, architecture, maintenance machinery, and contributor details
live in [ARCHITECTURE.md](./ARCHITECTURE.md). The public docs stay focused on
the local decision-seatbelt workflow.
