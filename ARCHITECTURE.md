# latch — architecture & internals

> User-facing setup and the **why** live in [README.md](./README.md). This
> document is for contributors and for anyone debugging an install or operating
> latch in an unusual environment.
>
> **The code is the source of truth for the live tool & command surface**
> (`src/mcp_server.py` for MCP tools, `commands/` and `/help` in-session for
> slash commands). This doc describes the *shape* of that surface, not an
> exhaustive signature list — by design. An enumerated table in prose silently
> rots against the code (the old README listed 6 of 10 slash commands); pointing
> at the live source has nothing to drift.

## Repository layout

```
${LATCH_HOME}/
├── README.md                 # user-facing: what latch is, why, quickstart
├── ARCHITECTURE.md           # this file
├── LICENSE                   # Apache License 2.0
├── LICENSING.txt             # copyright notice and license summary
├── NOTICE                    # attribution notices for vendored artifacts
├── CONTRIBUTING.md           # contribution scope and DCO sign-off
├── TRADEMARK.md              # lightweight brand-use guidance
├── requirements.txt
├── settings_snippet.json     # template for ~/.claude/settings.json
├── claude_md_snippet.md      # template block for each project's CLAUDE.md
├── src/
│   ├── schema.sql            # SQLite schema (nodes, edges, sessions, FTS)
│   ├── paths.py              # pinned/shared KB selection + legacy CWD fallback
│   ├── db.py                 # SQLite helpers
│   ├── embeddings.py         # local ONNX MiniLM embedder (vendored all-MiniLM-L6-v2)
│   ├── search.py             # hybrid FTS + cosine retrieval
│   ├── mcp_server.py         # MCP tools: kb_search/get/recent/insert/...
│   ├── mcp_proxy.py          # lightweight per-host stdio compatibility process
│   ├── mcp_daemon.py         # shared multi-connection MCP/model owner
│   ├── mcp_broker.py         # discovery, election, leases, and bounded lifecycle
│   ├── compactor.py          # session -> KB nodes via Claude or Codex backend
│   ├── gate.py               # decision-chain assembly + classifier (kb_gate)
│   ├── heal.py               # on-insert + nightly contradiction healer
│   ├── maintenance.py        # heal / decay / promote / tree
│   ├── tree.py               # RAPTOR-style summary clustering
│   ├── budget.py             # daily LLM-call budget
│   ├── doctor.py             # cross-platform install verifier (latch_doctor)
│   ├── install_codex.py      # Codex MCP config + AGENTS.md installer
│   ├── install_cursor.py     # Cursor MCP + rules + commands + AGENTS.md installer
│   ├── agents_md_sync.py     # managed AGENTS.md region sync for Codex
│   ├── cursor_rules_sync.py  # managed Cursor .mdc activation rule sync
│   ├── claude_md_sync.py     # managed CLAUDE.md region sync for Claude Code
│   ├── managed_doc_sync.py   # shared managed-region mechanics
│   └── hooks/
│       ├── _common.py
│       ├── stop.py           # turn counter + auto-compact every 5 turns
│       ├── session_end.py    # final compact + promote to canonical
│       ├── session_start.py  # silent managed-document repair
│       └── user_prompt_submit.py  # bounded prompt context surfacing
├── bin/                      # wrappers: slash commands, installers, latch_doctor
├── commands/                 # slash command markdown (copy to ~/.claude/commands/)
├── vendor/                   # vendored ONNX MiniLM model + tokenizer (no download)
└── projects/                 # legacy unpinned layout only
    └── <sanitized-cwd>/
        └── kb.db
```

## Concepts

- **Selected local KB.** Runtime wiring is installed once, while the current
  filesystem location selects a KB through the nearest explicit scope root.
  Shared roots use the exact install-level global KB; Private roots use their
  own external vault; UNLATCHED roots open no KB; unsafe or unscoped locations
  under the explicit policy are LOCKED. Descendants inherit the nearest root.
  Existing pinned global-KB installs persist a `compatibility_global` policy so
  an upgrade does not silently lock old repositories. That compatibility ends
  only after a deliberate Shared-root migration with
  `--shared --require-explicit-scopes`. A direct `LATCH_KB_DIR` override remains
  install-wide and therefore cannot claim project separation.
- **Loose graph.** `nodes(kind, title, body, status, embedding)` +
  `edges(src, dst, relation)`. Relations are free-form strings; common
  patterns will emerge — codify later. Kinds: `fact`, `decision`, `progress`,
  `entity`, `preference`, `open_question`, `idea`, `workstream`, `summary`.
- **Status lifecycle.** New nodes default to `staging`. Promoted to
  `canonical` via `kb_update`, or automatically when `ref_count` clears the
  weekly-promote bar.
- **Hybrid retrieval.** FTS5 keyword + cosine over local embeddings,
  combined via reciprocal rank fusion in `search.py`.
- **Compaction.** Spawns a fresh summarizer backend process given (prior session
  summary + transcript + related KB nodes) and asks for JSON: an updated
  summary plus extracted nodes/links. Claude Code uses `claude -p`; the Codex
  manual wrapper opts into a Codex-native `codex exec` backend. The
  session_summary node is UPSERTed per `session_id` (latest overwrites prior);
  extracted facts stack.
- **Session lifecycle triggers.**
  - `Stop` hook (every turn) — increments turn counter, fires compactor in
    the background every 5 turns. Cheap, non-blocking.
  - `SessionEnd` hook — final compact, promotes summary to canonical.
  - `SessionStart` hooks perform silent host bootstrap such as exact session
    markers, transcript handoff, managed-file repair, and gate-state setup.
    Healthy startup emits no project brief.
  - `UserPromptSubmit` performs bounded local retrieval on every eligible
    prompt, including same-topic graph drill-down, and keeps hit, no-hit, and
    degraded-runtime receipts visible.
- **On-demand orientation.** Catch-up prompts route to the compact
  `latch_project_direction` report, add three raw `latch_recent` progress rows,
  then read the foreground node. Recent remains chronological rather than
  acting as a second synthesized brief.
- **Decision-chain gate.** `kb_gate` assembles a chain of related
  nodes (decisions, abandoned paths, active constraints) for a coding
  request, then classifies the request as `PROCEED` / `MODIFY` /
  `DO_NOT_PROCEED` / `NEEDS_HUMAN_JUDGMENT`. (Renamed from `kb_preflight`
  2026-05-19.)
- **Bounded MCP runtime.** Host-created stdio processes enter a lightweight
  proxy before importing FastMCP/ONNX. One lazily elected daemon per pinned
  vault/runtime key owns the warm model and serves multiple logical MCP
  sessions. Idle owners are reclaimed; over-cap idle proxies retire themselves
  through an LRU lease pool. See
  [docs/mcp_resource_architecture.md](./docs/mcp_resource_architecture.md) for
  the root-cause evidence, invariants, benchmarks, and rollback path.

## Tool & command surface

latch exposes **MCP tools** (callable inline by Claude Code, Codex, and
Cursor) plus host-specific command prompts where the host supports them.
The canonical, always-current list is the code:

- **MCP tools** — defined in `src/mcp_server.py`. Read/write KB tools
  (`kb_search`, `kb_get`, `kb_recent`, `kb_insert`, `kb_update`, `kb_link`,
  `kb_correct_plan`/`kb_correct_apply`, …) plus the gate (`kb_gate`, which
  assembles a decision chain for a coding request and returns a go/no-go
  verdict). In Claude Code they are auto-approved in-session by the server-level
  `mcp__latch` permission rule the installer adds; `mcp__claude-kb` remains as
  the legacy alias rule for existing registrations. In Codex preview they are
  configured through `config.toml` with the server-level
  `default_tools_approval_mode = "approve"`. In Cursor they are configured
  through project `.cursor/mcp.json`; the Cursor CLI can verify them with
  `agent mcp list-tools latch`.
- **Command prompts** — markdown in `commands/`, installed into
  `~/.claude/commands/` by the engine installer. Run **`/help`** in a session
  for the current set; `/latch-compact` is the one to know (summarizes the
  current Claude Code session into the KB on demand; `/kb-compact` remains a
  legacy alias). Cursor gets a curated project subset under `.cursor/commands/`
  for latch workflows that are safe today, including a current-session
  `/latch-compact` that summarizes only the exact conversation recorded by the
  opt-in SessionStart hook — latch does not scan Cursor's private history
  folders. Codex slash commands are not part of the current preview slice;
  Codex compaction uses the installed `latch-compact` skill or
  `bin/run_codex_compact_now.sh` / `bin/run_codex_compact_now.ps1`.

## Install internals

The engine installer (`bin/install_engine.{sh,ps1}`) is **not** a
`settings.json` paste — and here is why each step matters:

- **Registers the MCP server with `claude mcp add --scope user`.** Claude Code
  reads MCP servers only from the `claude mcp` registry (`~/.claude.json`) or a
  project `.mcp.json` — it does **not** read a `mcpServers` block in
  `settings.json`. A settings.json-only install leaves the hooks firing (so it
  looks half-alive) while the `kb_*` tools never connect. The installer uses the
  CLI so the server lands where Claude Code actually looks.
- **Merges the hooks** (SessionStart / UserPromptSubmit / Stop / SessionEnd)
  into `settings.json` — this half genuinely belongs there. Re-running re-points
  stale interpreter paths and never duplicates entries; your other hooks are
  preserved.
- **Adds server-level permission rules, `mcp__latch` and legacy
  `mcp__claude-kb`**, to `permissions.allow` — the bare server prefix
  auto-approves *every* tool the server exposes (current and future), so you are
  never prompted to accept `kb_get` / `kb_insert` / … one at a time. Fresh
  installs register the `latch` server name; existing `claude-kb` registrations
  continue to work.
- **Installs the slash commands** — copies `commands/*.md` into
  `~/.claude/commands/` with `<KB_HOME>` resolved (a plain `cp` would leave the
  placeholder unresolved). To (re)install only the commands after editing one:
  `bash bin/install_commands.sh` / `.\bin\install_commands.ps1` (idempotent).
- **Persists scope migration intent before wiring.** Fresh installs select the
  explicit-scope policy. Upgrades with an existing safe global pin preserve an
  exact `compatibility_global` binding; they do not reinterpret the installer's
  newly written pin as legacy state on retry. A user can later create a Shared
  boundary and require explicit scopes everywhere else with
  `latch --shared --require-explicit-scopes`.

The interpreter is resolved automatically (`$LATCH_PYTHON`, legacy
`$CLAUDE_KB_PYTHON`, else repo `.venv`); override with
`--python /abs/path/to/python`. Preview with
`--dry-run`; verify an existing wiring with `--check`.

### Per-project CLAUDE.md (the behavior half)

Engine wiring makes the tools and hooks *available*; it does not tell the
agent to *use* them. `bin/install_claude_md.{sh,ps1}`, run from a project root,
injects `claude_md_snippet.md` into a **managed region** delimited by
`<!-- BEGIN LATCH SNIPPET … -->` / `<!-- END LATCH SNIPPET -->`, substituting
`{{KB_HOME}}`. Everything outside the region is preserved; the target is backed
up to `CLAUDE.md.latchbak` first.

**The snippet is the single source of truth.** Never hand-edit the managed
region in a `CLAUDE.md` — edit `claude_md_snippet.md` and re-run the installer.
After the first wiring, the SessionStart hook auto-re-syncs the region whenever
you upgrade latch (silent, non-destructive, only on actual change).
`install_claude_md.sh --check` exits non-zero on drift (wire it into
pre-commit / CI). The snippet is Tier-A only (engine contract, no project
facts); keep project facts and decisions in the KB via `kb_insert`, never in
CLAUDE.md.

### Codex preview install (MCP + AGENTS.md + silent startup)

Codex support is intentionally adapter-specific. It does **not** reuse the
Claude Code installer, does **not** call `claude mcp add`, and does **not**
write to `~/.claude/settings.json`.

`bin/install_codex.{sh,ps1}` runs `src/install_codex.py`, which:

- **Manages a marked Codex `config.toml` block** containing
  `[mcp_servers.latch]`, the Python interpreter, `src/mcp_server.py`,
  server-level approval mode, and a Codex MCP env table with
  `LATCH_MODEL_BACKEND = "codex"` and `LATCH_GATE_BACKEND = "codex"`. Existing
  `[mcp_servers.claude-kb]` tables are treated as supported legacy latch-owned
  config. Model-backed gate, heal, and tree calls for Codex are Codex-native. It
  preserves unrelated Codex config and backs up `config.toml` to
  `config.toml.latchbak` before writing.
- **Replaces existing latch-owned `mcp_servers.latch` / `mcp_servers.claude-kb`
  tables** before appending the managed block, including stale nested env/tool
  tables, so TOML never ends up with duplicate table definitions or mismatched
  model backends.
- **Manages Codex `hooks.json` for the silent `SessionStart` bootstrap only.**
  It preserves unrelated user hooks, removes older latch-owned Codex
  `SessionStart` / `Stop` entries, and installs
  `src/hooks/codex_session_start.py`. It does not install Stop or SessionEnd
  compaction hooks.
- **Syncs `AGENTS.md`** by rendering the shared latch contract with
  Codex/AGENTS wording and distinct `LATCH AGENTS SNIPPET` markers. The
  mechanics live in `src/managed_doc_sync.py`; `src/claude_md_sync.py` keeps the
  existing Claude marker strings and CLI behavior.

Usage from the project root whose `AGENTS.md` should receive the latch contract:

```bash
/path/to/latch/bin/install_codex.sh --yes      # Windows: C:\path\to\latch\bin\install_codex.ps1 --yes
/path/to/latch/bin/install_codex.sh --check
```

Current boundary: this is still the Codex Act 1 wedge plus silent startup
bookkeeping. Manual compaction is
available through `bin/run_codex_compact_now.sh` or
`bin/run_codex_compact_now.ps1`: it resolves the Codex session from the first
arg or `$CODEX_THREAD_ID`, validates the matching
`~/.codex/sessions/**/rollout-*.jsonl` via `session_meta.payload.id`, and fails
closed without ever falling back to `~/.claude/projects`. It uses the
Codex-native `codex exec` summarizer backend by default, with `--summarizer
claude` available to exercise the legacy shared Claude CLI backend. Pass
`--background --wait` to validate the transcript in the parent process, detach
the compactor child, poll `codex_compact_background.log` for this launch's final
JSON, and return the completion result. Bare `--background` is reserved for
explicit fire-and-forget launches. Codex has a silent `SessionStart` hook for
the exact transcript marker and managed wiring repair, but automatic
Stop/SessionEnd turn/end compaction remains deliberately deferred.

### Cursor adapter install (project MCP + rules + commands + AGENTS.md)

Cursor support is a project-local adapter around the shared latch MCP server.
It does **not** write Claude Code or Codex config. The base install is
hook-free; pass `--with-hooks` to add current-session context and pre-edit gate
enforcement. Historical Cursor discovery is never automatic; the separate
initial-KB seed flow can explicitly enable a bounded, project-scoped local IDE
scan with `--cursor-history`. Each transcript id must agree across Cursor's
local project row, conversation/project membership, and typed composer header;
missing or changed metadata fails closed for that history leg. Cursor-only
seeding stops, while aggregate `--source all` seeding records the unavailable
leg and continues with other authorized sources.

`bin/install_cursor.{sh,ps1}` runs `src/install_cursor.py`, which:

- **Manages project `.cursor/mcp.json`** with a `mcpServers.latch` stdio server
  pointing at `src/mcp_server.py`. It preserves unrelated MCP servers and
  settings, removes older latch-owned legacy names, and uses the Cursor Agent
  CLI as the native model backend. Explicit `claude` and `codex` compatibility
  overrides remain available through `--model-backend`.
- **Manages `.cursor/rules/latch.mdc`** as a latch-owned always-apply Cursor
  project rule. The rule activates KB-first behavior and foreground
  `latch_gate` receipts.
- **Installs project-local commands and workflow skills** for supported latch
  operations, including current-session seed and compaction. Those workflows
  use only the exact conversation/transcript pair recorded by the opt-in
  SessionStart hook. A separate shell/quickstart initial-KB flow may scan only
  metadata-verified, non-subagent local IDE transcripts assigned to the current
  project after explicit `--cursor-history` consent; it excludes
  CLI/cloud/other-project/subagent data.
- **Fences shell-backed maintenance and gate commands to the current chat.**
  `latch-decay`, `latch-heal`, `latch-tree`, and the `latch-gate` shell fallback
  must use the exact session id re-injected by `beforeSubmitPrompt` as
  `LATCH_SESSION_ID`; omission or reuse of another chat id fails closed.
- **Syncs `AGENTS.md`** using the same shared managed-region mechanics as
  Codex, branded for Cursor on first wiring.
- **Optionally manages `.cursor/hooks.json`** when `--with-hooks` is passed.
  The hook layer adds a silent current-session marker, per-prompt fail-closed
  pre-edit gate enforcement, and post-tool latch activity context while
  preserving unrelated hooks.

Usage from the project root whose Cursor workspace should use latch:

```bash
/path/to/latch/bin/install_cursor.sh --yes --with-hooks
# Windows: C:\path\to\latch\bin\install_cursor.ps1 --yes --with-hooks

/path/to/latch/bin/install_cursor.sh --check --with-hooks
/path/to/latch/bin/latch_cursor_doctor.sh --with-hooks
```

`src/cursor_doctor.py` performs strict static checks for `.cursor/mcp.json`,
the MCP launch target, `AGENTS.md`, Cursor rules, commands, skills, and optional
hooks. With the native backend it also requires a reachable, authenticated
Cursor Agent CLI. CLI-side MCP visibility does not replace the user-controlled
IDE approval and workspace enablement steps.

See the [Cursor reference](./runbooks/cursor.md) for the full hook, privacy,
current-session, model-backend, doctor, and plugin boundaries. Use the
[Cursor gate smoke runbook](./runbooks/cursor_gate_smoke.md) for live
acceptance.

### Verify the wiring

- `bash bin/install_engine.sh --check` — every line should read `[OK]` (server
  registered as `latch` or supported legacy `claude-kb`, matching permission
  present, all hooks installed, no dead latch-owned `mcpServers` block). Or run
  `bash bin/latch_doctor.sh` for env + wiring.
- `bash bin/install_codex.sh --check` — Codex preview wiring should report the
  managed MCP block in Codex `config.toml`, the Codex `SessionStart` hook in
  `hooks.json`, and an up-to-date `AGENTS.md` managed region.
- `bash bin/latch_codex_doctor.sh` — Codex preview health check: static MCP
  launch target, `config.toml`, `hooks.json`, `AGENTS.md`, Codex compact
  transcript resolution, and a tiny probe of the current summarizer backend
  (`codex` by default, or `--summarizer claude`). Run from the target project
  root; pass `--session-id` when outside Codex or when `$CODEX_THREAD_ID` is
  unavailable. Pass `--skip-summarizer` for a static wiring-only check.
- `bash bin/install_cursor.sh --check --model-backend codex` — Cursor adapter
  wiring should report the managed MCP server, `AGENTS.md`, Cursor rule, and
  Cursor command prompts as current.
- `bash bin/latch_cursor_doctor.sh --model-backend codex` — Cursor adapter
  health check: static config/launch/rule/commands plus optional live
  `agent mcp list` / `agent mcp list-tools latch`.
- In a new session on a supported prompt-hook host, tail
  `<selected-kb-dir>/retrieve.log`; expect `path="vector"`/`"graph"` when
  retrieval runs. A first prompt may record `skip="embed_daemon_unavailable"`
  while the shared owner starts. Healthy SessionStart bookkeeping should emit
  no model-visible payload. Calling `kb_recent` in-session should return nodes
  (or an empty list).

## Maintenance (automatic — no setup)

Maintenance (local backup + heal + weekly decay/tree + prune) is
**self-triggering** off the MCP server lifecycle — no scheduler, no admin
rights, no cron/Task-Scheduler entry. On session start the MCP server checks an
elapsed-time cadence (`<selected-kb-dir>/maintenance_state.json`) and, if anything
is due, spawns the pass as a detached background process
(`src/selfheal.py`). It runs off-process so it never blocks your session; KB
reads stay live and writes briefly wait if they land mid-pass.

Cadence (elapsed since last run): heal every 48h, decay + tree weekly, local
`kb.db.bak` rotation every 12h. Identical on Windows, macOS, and Linux (only
runtime dependency is Python). Model-backed heal arbitration and tree summary
generation use Claude by default for existing Claude installs; adapter env such
as Codex's `LATCH_MODEL_BACKEND=codex` routes those calls through the selected
backend. Run a pass manually with
`python src/selfheal.py <project_dir>`, or via `/latch-heal` · `/latch-decay` ·
`/latch-tree`. Existing `/kb-*` command files are updated as compatibility
aliases when present; fresh command installs create only the `/latch-*` names.

**Optional git snapshot (off by default).** Maintenance does not touch git — a
forced `git push` would fail or hang on machines with no remote or credentials.
To commit + push each pass, set `CLAUDE_KB_GIT_SNAPSHOT=1` in the environment the
MCP server runs under. Best-effort and fully exception-wrapped: a git failure
can never break a pass. Configure your remote + credentials first.

## Environment notes

### Multi-user Windows machines

If multiple Windows users share a single latch install on the same machine, ACL
inheritance bites: only the user who first creates a project DB gets write
access, others fall back to read-only and every `kb_search` errors with
`attempt to write a readonly database`. For each shared project dir, run **once
per additional user** from an admin shell:

```powershell
$proj = "$env:LATCH_HOME\projects\<sanitized-cwd>"  # legacy: $env:CLAUDE_KB_HOME
icacls $proj /grant "<DOMAIN>\<USERNAME>:(OI)(CI)(M)"
```

`(OI)(CI)` (Object Inherit + Container Inherit) are essential — they propagate
the ACE to all files created inside the dir, regardless of which user creates
them. To recover a dir where the bug already manifested:

1. Close all Claude Code sessions (releases `kb.db-wal` / `kb.db-shm` handles).
2. Run the `icacls` grant above.
3. Delete `kb.db-wal` and `kb.db-shm` (safe — SQLite recreates them; if
   `kb.db-wal` is non-zero, run `sqlite3 kb.db "PRAGMA wal_checkpoint(TRUNCATE);"`
   first to flush).
4. Reopen Claude Code — SQLite recreates the WAL/SHM files with the fixed ACL
   inherited; both users get RW access.

### Kill switch — finer control

`bash bin/latch_disable.sh` (or `touch DISABLE`, or `export LATCH_DISABLE=1`;
legacy `CLAUDE_KB_DISABLE=1` still works)
no-ops **all** latch hooks + the compactor on the very next prompt — no restart;
latch-scoped (other hooks/skills/plugins keep running). `bin/latch_enable.sh`
resumes; `bin/latch_status.sh` reports state. Finer:
`bash bin/latch_disable.sh --write-only` (or the `DISABLE_WRITE` file /
`LATCH_DISABLE_WRITE` env var; legacy `CLAUDE_KB_DISABLE_WRITE` still works)
stops only the write-side hooks (Stop / SessionEnd / compactor) while leaving
read-side prompt retrieval and silent host bootstrap live.
`latch_enable.sh` leaves `DISABLE_WRITE` in place by default; `--all` removes
it too. A legacy install-wide `UNLATCHED` sentinel is global, not project-local;
the enable wrapper clears it only after validating and restoring its recorded
ancestor instruction files. Use `latch` / `unlatch` for the normal per-scope
state cycle. Windows: the `.ps1` equivalents.

### Logs

- `${LATCH_HOME}/hooks.log` — hook events
- `${LATCH_HOME}/compactor.log` — compactor outcomes
- `${LATCH_HOME}/maintenance.log` — heal/decay/tree outcomes
- `<selected-kb-dir>/retrieve.log` — per-prompt
  retrieval timing + path taken

## Uninstall (full detail)

The strict inverse of `bin/install_engine.sh` (+ the slash-command copy):

```bash
bash bin/uninstall.sh --dry-run    # preview exactly what will be removed
bash bin/uninstall.sh              # apply (asks to confirm; -y to skip)
bash bin/uninstall.sh --check      # verify nothing latch-owned remains
bash bin/uninstall.sh --cursor-project "$PWD" --dry-run
```

Removes **only** what latch added: latch-owned MCP registrations (`latch` plus
legacy `claude-kb`), the latch hooks + latch-owned MCP permissions in
`settings.json` (backed up to a timestamped `settings.json.latchbak-<UTC>`
first), and latch's Claude Code slash commands. Not
removed unless asked: the CLAUDE.md managed region (name it explicitly,
repeatable: `--claude-md /path/to/CLAUDE.md`), project-local Cursor wiring
(name the project explicitly with `--cursor-project /path/to/project`), and
your production KB data and protected backups. `--purge` removes only the
kill-switch sentinel files; there is no production-KB deletion path. Cursor
project removal strips only latch-owned
`.cursor/mcp.json` server entries, a clean latch Cursor rule, latch-owned
`.cursor/commands` files, and the managed `AGENTS.md` region. Restart the host
agent afterward so the MCP roster + commands reload. To remove latch entirely,
delete the repo. Windows: `bin\uninstall.ps1`.

## Contributing — release hygiene

The authoritative repository check is the `public-release-hygiene` GitHub
Actions job in `.github/workflows/public-release-hygiene.yml`. Maintainers should
mark that job as a required status check on the protected public branches so PRs
cannot merge unless the tracked tree passes:

```bash
bash bin/latch_public_release_check.sh
```

Local hooks are fast feedback for contributors, not the enforcement boundary. A
`.githooks/pre-commit` script blocks commits whose staged diff matches any
pattern in `.githooks/denylist.txt` (project terms from the sanitization origin).
It also runs the public release hygiene check, which scans tracked files for
private paths, internal-business terms, and markdown files that look like
planning artifacts. Hooks are per-clone, so each contributor enables them once
after cloning:

```bash
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit
```

Run the full check locally before public release:

```bash
bash bin/latch_public_release_check.sh
```

Override the hook only with explicit maintainer approval:
`git commit --no-verify`.
