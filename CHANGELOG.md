# Changelog

## 2.0.1 - Unreleased

- Correct the 2.0.0 note on turning off request-text capture: the two
  controls are not interchangeable. The shared MCP runtime, which runs
  `latch_gate` for MCP hosts, is governed only by
  `"request_text_capture": false` in the vault `runtime_settings.json`;
  `LATCH_REQUEST_TEXT_CAPTURE=0`, like `LATCH_OUTCOME_EVENTS=0`, reaches only
  direct CLI processes and never the shared MCP runtime. The MCP server now
  says so once on stderr, and doctor warns, when either variable is set.

## 2.0.0 - 2026-10-09

- Move the Python sources into an importable `latch` package under
  `src/latch/`, described by a new `pyproject.toml` that adds no console
  scripts, so the `bin/` wrappers remain the command surface; project wiring
  moves to version 7. `bin/latch_update.sh` does not rewrite agent
  configuration, and hooks and MCP entries written by 1.1.0 still point at the
  removed flat `src/` files, so the automatic wiring repair cannot start: after
  updating, rerun the installer for every wired surface (for example
  `bash bin/latch_quickstart.sh --no-seed` from each wired project, or the
  matching `bin/install_*` script), then fully quit and reopen the agent and
  start a new task. This is a major version because an in-place update from
  1.x with `bin/latch_update.sh` leaves wired hosts broken until the installer
  is rerun.
- Bind decision and preference authority to explicit ratification kept as
  append-only history: only `latch_capture_decision` with
  `human_action="approve"` or a staging-to-canonical `latch_update` from a
  verified session can make one canonical, `reject` is recorded as a
  rejection, `latch_insert` and `latch_correct_apply` refuse to mint canonical
  judgment, and ref-count promotion now promotes only facts and progress
  notes. Decisions and preferences that were already canonical stay canonical
  but carry no ratification record.
- Stop unattended heal from changing ratified rulings: when on-insert heal
  would supersede, or nightly heal would supersede or reconcile, a ratified
  decision or preference, latch files one staging "Human review required"
  open question linked to both nodes and parks the pair instead. Nightly heal
  keeps deterministic verdicts in similarity order but spends its daily model
  budget on contradictions between the most-retrieved nodes first, weighting
  cross-workstream pairs, and records a structural `heal_run` row for every
  sweep, including failed ones.
- Write the verbatim text of each gate request to a private
  `gate-request-text-YYYY-MM-DD.jsonl` file in the local vault by default,
  owner-only on macOS and Linux, never opened through a symbolic link, and
  compressed after 30 days and deleted after one year by nightly maintenance;
  turn it off with `"request_text_capture": false` or
  `LATCH_REQUEST_TEXT_CAPTURE=0`. The retired `CLAUDE_KB_LOG_RAW_QUERY` flag
  no longer puts any request or claim text into `gate.log`, and doctor warns
  when it is still set.
- Pass an explicit `--model` on every Claude-backed background call instead of
  inheriting the Claude CLI's user default: `sonnet` unless
  `LATCH_GATE_CLAUDE_MODEL`, `LATCH_COMPACTOR_CLAUDE_MODEL`,
  `LATCH_HEAL_CLAUDE_MODEL`, `LATCH_TREE_CLAUDE_MODEL`,
  `LATCH_MAINTENANCE_CLAUDE_MODEL`, or `LATCH_CLAUDE_MODEL` selects another.
- Key the shared MCP runtime owner to the OS account and install root as well
  as the vault and runtime fingerprint, so accounts sharing one vault get
  separate model owners; this is coordination, not access control, and MCP
  tasks left open across the update are told to restart.
- Return an opaque `gate_call_id` and structural measurement metadata from
  `latch_gate`, render typed rejected-path rows under their owning ruling with
  verdicts citing them by id in `cited_rejected_paths`, attribute Codex gate
  calls to their threads from Codex's own session transcripts only on a
  unique match, and add an offline, model-free outcome audit
  (`bin/run_latch_outcome_audit.sh`) with its runbook.
- Add opt-in, zero-model policy checks (`bin/latch_policy_check.py` and
  `bin/latch_ci_check.py`) that evaluate rejected-path predicates from a
  private local snapshot against a proposed action or an exact Git commit
  range and print only redacted receipts; nothing installs them as hooks or CI
  jobs, and they do not change gate verdicts.
- Preflight sqlite-vec before any project or agent configuration is written,
  restoring the previous checkout and runtime when an upgrade fails it; fix
  the probe that failed every stock Windows PowerShell 5.1 install; and
  require `codex --version` to succeed, including for a Codex-backed Cursor
  setup, with standalone-CLI guidance on Windows.
- Run the full test suite on Ubuntu and Windows in CI, with LF normalization
  for hash-pinned artifacts and uv on the Windows leg; check proof-packet
  currency at tagged release, with a manual check-only readiness workflow,
  instead of on every merge; make a test fixture that expired on 2026-09-29
  clock-relative; and publish the decision-authority evaluation with its
  recompute script alongside a refreshed README.

## 1.1.0 - 2026-07-31

- Replace startup briefs and intensity tiers with healthy-silent session
  bootstrap, bounded hook retrieval, and on-demand project-direction reports.
- Harden vault durability with immutable production and test identities,
  verified external backups and restore tooling, fail-closed maintenance, and
  uninstall paths that never delete a production KB.
- Add explicit, project-scoped Cursor IDE history seeding and make coverage,
  limits, and evidence provenance visible instead of silently truncating
  history.
- Narrow Latch gating to material implementation changes and add local
  structural outcome events, coverage reporting, and in-session edit
  correlation.
- Fix Windows Codex shared and forced-legacy MCP startup through base Python by
  securely forwarding the validated Latch venv and surfacing invalid handoffs
  immediately; require successful Codex MCP initialization so a cold session
  cannot silently start without Latch tools, and repair older managed Codex
  wiring once from MCP startup or SessionStart before requiring a fresh task.
- Add a security-hardened pull-request review panel with read-only Claude and
  Codex reviewer lanes, immutable review scope, and isolated artifact evidence.
- Refresh onboarding, release-pinned install examples, proof language, and
  public documentation for the current workflow.

## 1.0.0 - 2026-07-23

- Add separate release, KB schema, and project-wiring versions.
- Add offline version and commit diagnostics to installers and doctor output.
- Add an explicit stable-release updater for clean official Git clones.
- Back up local KBs before schema upgrades and refuse newer unsupported schemas.
- Repair older managed Claude, Codex, and Cursor project wiring once at startup
  without maintaining a global project registry.
