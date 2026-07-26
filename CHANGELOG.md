# Changelog

## Unreleased

- Add explicit, default-off Cursor IDE history seeding that verifies each
  conversation against Cursor's local project membership and non-subagent
  metadata, binds the opt-in into cached preview/apply receipts, and lets
  aggregate `--source all` runs continue when the Cursor-history leg is
  unavailable while keeping Cursor-only runs fail-closed.

## 1.0.0 - 2026-07-23

- Add separate release, KB schema, and project-wiring versions.
- Add offline version and commit diagnostics to installers and doctor output.
- Add an explicit stable-release updater for clean official Git clones.
- Back up local KBs before schema upgrades and refuse newer unsupported schemas.
- Repair older managed Claude, Codex, and Cursor project wiring once at startup
  without maintaining a global project registry.
