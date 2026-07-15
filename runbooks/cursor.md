# Cursor Adapter

Cursor uses the same local latch KB and MCP server as Claude Code and Codex,
with project-scoped Cursor wiring and an opt-in hook layer for current-session
context and pre-edit gate enforcement.

This reference carries the detailed Cursor contract so the main README can stay
focused on latch's first-value path. For a live, step-by-step acceptance test,
use the [Cursor gate smoke runbook](./cursor_gate_smoke.md).

## What The Installer Owns

The installer writes project-scoped:

- `.cursor/mcp.json`
- `.cursor/rules/latch.mdc`
- `.cursor/commands/*.md`
- `.cursor/skills/*/SKILL.md`
- the latch-managed region in `AGENTS.md`
- `.cursor/hooks.json` entries only when `--with-hooks` is passed

This makes Cursor safe to try in one repo without touching Claude Code or Codex
configuration. The installer merges its MCP and hook entries while preserving
unrelated Cursor configuration.

The base adapter provides the shared latch MCP tools, a Cursor-native activation
rule, project-local commands and workflow skills, and the shared `AGENTS.md`
behavior contract. Pass `--with-hooks` to add SessionStart briefing, an exact
current-session transcript handoff, per-prompt pre-edit gate enforcement, and
post-tool latch activity context.

The Cursor Agent CLI is the native model backend for gate, maintenance, seed,
and compaction calls. Current-session compaction and default Cursor seeding use
only the exact conversation/transcript pair provided by SessionStart. A user can
also supply a transcript path explicitly. Historical transcript discovery is
deliberately unsupported.

## Opt-In Hook Contract

The `.cursor/hooks.json` merge installs four latch hook paths:

- `sessionStart` records the current Cursor conversation/transcript pair for
  direct hook activity and explicit current-session workflows, re-syncs an
  already-managed `AGENTS.md`, and returns the KB brief through Cursor's native
  `additional_context` field. Cursor's reused MCP process has no verified
  per-request conversation id, so MCP structural rows remain unattributed
  instead of inheriting another interleaved conversation's project marker.
- `beforeSubmitPrompt` fingerprints the current prompt without storing its text,
  invalidates any prior gate receipt, and recognizes only explicit managed latch
  operations for a separate one-shot operation lane.
- `preToolUse` denies mutation-capable tools until the current prompt has a
  matching, usable `latch_gate` receipt. Exact native read tools remain
  available; free-form Shell, unknown, and malformed payloads deny. Explicit
  latch operations can consume one session/prompt/tool/argument-bound receipt
  instead; preview/apply and confirmation workflows require a later explicit
  operation confirmation, and the receipt is single-use. Managed-operation
  intent selects an exclusive lane: a missing, consumed, or mismatched narrow
  receipt denies without falling through to ordinary `latch_gate` authorization.
- `postToolUse` recognizes latch `kb_activity` and gate `findings` in a verified
  `latch_gate` result. It arms the current prompt only when the gate used the
  request verbatim and the outer result reports positive completion rather than
  failure, cancellation, timeout, denial, or skip. It rejects conflicting tool
  names, input containers, servers, and nested-tool identities. It advances seed
  preview state only after matching successful JSON and binds `/latch-pm` to the
  exact non-writing `latch_pm_preview` candidate digest. Failed, malformed,
  missing, or cross-session preview results cannot authorize apply. It also
  returns a concise instruction to surface the receipt.

Cursor has no equivalent of Claude Code's deterministic user-only
`systemMessage` channel. Receipt visibility therefore uses agent-context
delivery backed by the `AGENTS.md` foregrounding contract; latch does not claim
that Cursor renders the line directly.

Session and activity surfacing stay fail-open. Prompt invalidation and mutation
enforcement use `failClosed: true`: hook errors, empty payloads, skipped gates,
rephrased gate requests, and stale or cross-session receipts cannot authorize a
mutation. Disabling or unlatching latch disables this enforcement as well. The
hooks do not auto-compact or discover historical Cursor transcripts.

## Model And Current-Session Boundaries

The native model path uses an authenticated Cursor Agent CLI in headless
`--print` / JSON / Ask mode. It never passes `--force` or `--yolo`, and it runs
each model call in an empty temporary workspace. Explicit compatibility
overrides remain available with `--model-backend codex` or
`--model-backend claude`.

The installed `/latch-compact` command and `run_cursor_compact_now` wrappers
compact only the current conversation. Resolution fails closed unless the
opt-in SessionStart hook recorded an exact per-session conversation id and
`transcript_path` pair and the wrapper receives that surfaced session id
explicitly. Because Cursor may not retain the original SessionStart text in a
long chat, `beforeSubmitPrompt` re-injects the same payload-derived conversation
id on every prompt. latch never scans Cursor databases or guesses the most
recent chat.

The command/skill requests Cursor `required_permissions: ["all"]` on its first
Shell call because compaction writes latch-owned budget, session, and KB state
outside the open workspace. Cursor still presents its normal permission flow; a
sandboxed first attempt cannot be retried under the same one-shot managed
receipt. Shell-backed Cursor commands read the workspace latch server's absolute
interpreter from `.cursor/mcp.json` and set `LATCH_PYTHON` explicitly, so native
dependencies cannot drift to a different `PATH` Python. Installer overrides
preserve virtualenv interpreter symlinks for the same reason.

The installed `/latch-seed` command is also current-session-only. It previews
seed candidates as JSON from the exact marker/transcript pair. The preview
attempt alone does not arm apply; a matching successful `postToolUse` result
must be recorded before the separate `/latch-seed apply` confirmation can write
staging evidence. Its first preview Shell call requests the same external-state
permission; approving it permits the preview to update latch-owned budget and
session state, not to apply candidates.

A successful Cursor preview caches only candidate/source metadata, never
transcript text, and returns `preview_digest`. Apply requires that exact digest,
loads the reviewed cached set, and makes no second model call. `/latch-pm`
similarly uses the read-only `latch_pm_preview` MCP result—not agent prose—to
display and digest-bind every load-bearing decision field before one matching
staging insert.

`--cursor-transcript PATH` is available for a user-explicit file. latch never
enumerates Cursor's private history folders.

## Install And Verify

Run from the project repo where Cursor should follow latch:

```bash
/path/to/latch/bin/install_cursor.sh --yes --with-hooks
# Windows: C:\path\to\latch\bin\install_cursor.ps1 --yes --with-hooks

/path/to/latch/bin/install_cursor.sh --check --with-hooks
/path/to/latch/bin/latch_cursor_doctor.sh --with-hooks
```

Restart Cursor after installing hooks so it reloads `.cursor/hooks.json`.

## Live Host Prerequisites

Live acceptance has three separate, user-controlled prerequisites:

1. Authenticate the Cursor Agent CLI with `agent login`.
2. Approve the project `latch` MCP server when Cursor prompts.
3. In the IDE, open **Cursor Settings > Tools & MCP**, select the current
   workspace, enable **latch**, confirm it reports **tools enabled**, and start a
   fresh Agent chat. The exact tool count can grow with the MCP surface.

Run `agent mcp list` to inspect CLI-side discovery. A `needs approval` or
`not approved` result proves the static config was discovered, while even a CLI
`ready` or tool-list result does not prove the IDE workspace toggle is enabled.
Neither condition alone proves that IDE MCP tools or the visible gate can run.
Latch does not edit Cursor's private state or invent an undocumented CLI command
to bypass that trust action.

If latch performs a one-time project-wiring repair after an engine upgrade,
Cursor treats the `.cursor/mcp.json` change as new workspace wiring and may
disable the server again. Repeat the IDE enablement check after the repair
notice. The next task should report `unchanged`, not rewrite current wiring.

## Doctor Contract

With the native default, the doctor requires a reachable, authenticated Cursor
Agent CLI and validates its JSON Ask mode with a small read-only probe. Static
config, launch target, `AGENTS.md`, rule/command drift, and native-backend
failures are errors.

MCP list visibility remains a warning when the CLI cannot complete that separate
inspection. If it completes, missing critical tools such as `latch_gate` are
errors. A current-session compact marker is informational by default and can be
required with `--require-compact` during live acceptance.

Static doctor success is not a substitute for authenticated MCP, a visible
gate, plugin, native-backend, or current-session compaction acceptance receipts.

## Plugin Boundary

The repo also ships `.cursor-plugin/plugin.json` for Cursor's local or
marketplace plugin flow. The plugin deliberately exposes workflow skills only;
MCP, hooks, rules, and project commands remain installer-owned so they cannot
double-fire.

To test plugin skills locally, launch `agent --plugin-dir /path/to/latch` or
place the checkout at `~/.cursor/plugins/local/latch`. If you use plugin skills,
run the project installer and doctor with `--skip-skills` to avoid duplicate
skill names.

## Acceptance Path

Use the [Cursor gate smoke runbook](./cursor_gate_smoke.md) to verify
static wiring, host approval, visible pre-edit gate enforcement, current-session
seeding and compaction, negative receipt cases, and clean uninstall behavior.

The boundary is deliberate: Cursor support is MCP plus Cursor Rules,
project-local commands and skills, `AGENTS.md`, a native read-only Agent CLI
model backend, and opt-in session/gate/activity hooks. Historical transcript
discovery is not installed.
