# Changelog

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
