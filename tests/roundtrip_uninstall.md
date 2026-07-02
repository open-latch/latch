# Uninstall + kill-switch round-trip test

Manual integration test for the latch **uninstaller** and **kill switch** (the
inverse of `install_engine`). It is intentionally *not* a pytest unit test: it
shells out to the `claude` CLI and mutates Claude Code config, so it runs by
hand, not in CI.

- `tests/roundtrip_uninstall.sh` — macOS / Linux (bash)
- `tests/roundtrip_uninstall.ps1` — Windows (PowerShell)

Both are byte-for-byte equivalent in intent and print `PASS:` / `FAIL:` lines
plus a final `RESULT: N passed, M failed`.

## What it does

Inside a throwaway sandbox it: installs latch (`install_engine` +
`install_commands`), exercises the kill switch (`latch_disable` / `latch_status`
/ `latch_enable`), uninstalls (`uninstall`), and then verifies your **real**
Claude Code config and this repo are untouched.

## Why a sandbox is mandatory

latch's install/uninstall mutate **global, machine-level** Claude Code state,
not per-repo state:

| surface | location |
|---|---|
| hooks + permissions | `~/.claude/settings.json` (`Path.home()`-based) |
| user-scope MCP server `latch` plus legacy alias `claude-kb` | `mcpServers.latch` / `mcpServers.claude-kb` in `~/.claude.json` |
| slash commands | `~/.claude/commands/*.md` |

There is exactly one server name and one shared `settings.json`, so a second
real install/uninstall would collide with — and an uninstall would tear out —
your live wiring. The test redirects every surface with env vars:

| env var | redirects |
|---|---|
| `HOME` (POSIX) / `USERPROFILE` (Windows) | `settings.json` + `~/.claude` (`Path.home()`) |
| `CLAUDE_CONFIG_DIR` | the `claude` CLI's user-scope MCP registry |
| `LATCH_HOME` / legacy `CLAUDE_KB_HOME` | snippet source, `DISABLE` files, KB data |
| `LATCH_PYTHON` / legacy `CLAUDE_KB_PYTHON` | interpreter used by wrappers |
| `CLAUDE_COMMANDS_DIR` | the slash-commands dir (both installers honor it) |

> **Windows note:** Python's `Path.home()` follows `%USERPROFILE%`, *not*
> `$HOME`, and `settings.json`'s path has no env override — so on Windows
> `USERPROFILE` is the lever that protects your real config. The PowerShell
> script sets both.

**Safety gate:** before any write, the script runs `install_engine --dry-run`
and aborts unless the reported `settings :` path is inside the sandbox. So the
worst case is an early "aborted", never a clobbered real config. The real
`settings.json` / `~/.claude.json` / `commands` / repo are read (hashed) before
and after and asserted unchanged.

## Assertions (19 checks)

- **Install** is additive: registers `latch`, merges latch hooks, adds
  `mcp__latch` plus legacy `mcp__claude-kb` permissions, installs latch's
  slash commands — while preserving unrelated theme / perms / hooks already
  present.
- **Kill switch**: full disable sets `DISABLE` and makes `is_disabled()` true
  (and `is_write_disabled()`, which it implies); `--write-only` sets
  `DISABLE_WRITE` with reads still live; enable/`--all` clear them; nothing
  leaks into the real repo.
- **Uninstall** is a complete inverse: removes latch-owned MCP registrations,
  hooks, permissions, a seeded *stale* `mcp__claude-kb__kb_get` per-tool perm,
  and latch's slash commands; preserves unrelated config and a user-owned
  command; `--check` exits 0.
- **Real state** untouched across the whole run.

## Run

```bash
bash tests/roundtrip_uninstall.sh            # tests HEAD
bash tests/roundtrip_uninstall.sh <committish>
```

```powershell
pwsh -ExecutionPolicy Bypass -File .\tests\roundtrip_uninstall.ps1
pwsh -ExecutionPolicy Bypass -File .\tests\roundtrip_uninstall.ps1 -Commit <committish>
```

Prereqs: the `claude` CLI on `PATH`, a repo `.venv` (else it falls back to a
`PATH` python), and `tar` (built into Windows 10+; swap for `git worktree add`
if absent).

## Status

The bash harness is **verified green (19/19, deterministic over repeated runs)**
on macOS (arm64). The PowerShell port is a careful translation that has not yet
been run on a Windows host — its `--dry-run` safety gate makes a failed
isolation abort rather than touch the real config.
