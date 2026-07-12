# Changelog

## 0.1.0 - Unreleased

- Add separate release, KB schema, and project-wiring versions.
- Add offline version and commit diagnostics to installers and doctor output.
- Add an explicit stable-release updater for clean official Git clones.
- Back up local KBs before schema upgrades and refuse newer unsupported schemas.
- Repair older managed Claude, Codex, and Cursor project wiring once at startup
  without maintaining a global project registry.
