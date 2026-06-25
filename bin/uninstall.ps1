<#
.SYNOPSIS
  Remove latch's wiring from Claude Code. Windows-native counterpart of
  bin/uninstall.sh; the strict inverse of install_engine.ps1.

.DESCRIPTION
  All logic lives in src/uninstall_engine.py (stdlib-only, shared with the bash
  wrapper). It:
    1. deregisters latch-owned MCP servers via `claude mcp remove`;
    2. removes the SessionStart/UserPromptSubmit/Stop/SessionEnd hooks + the
       latch-owned MCP permission rules from ~/.claude/settings.json, preserving
       every other hook/permission;
    3. removes latch's slash commands from ~/.claude/commands/.
  Leaves your KB data (projects/) and the repo in place by default.
  settings.json is backed up to a timestamped settings.json.latchbak-<UTC>
  before any write. Idempotent.

  Interpreter resolution for running the uninstaller: $env:LATCH_PYTHON, else
  legacy $env:CLAUDE_KB_PYTHON, else `python`.

.EXAMPLE
  .\uninstall.ps1 --dry-run
.EXAMPLE
  .\uninstall.ps1 --check
.EXAMPLE
  .\uninstall.ps1 --claude-md C:/proj/CLAUDE.md --yes
.EXAMPLE
  .\uninstall.ps1 --purge
#>
$ErrorActionPreference = "Stop"
$KbHome = if ($env:LATCH_HOME) { $env:LATCH_HOME } `
          elseif ($env:CLAUDE_KB_HOME) { $env:CLAUDE_KB_HOME } `
          else { (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")).Path }
$Py = if ($env:LATCH_PYTHON) { $env:LATCH_PYTHON } elseif ($env:CLAUDE_KB_PYTHON) { $env:CLAUDE_KB_PYTHON } else { "python" }
& $Py (Join-Path $KbHome "src/uninstall_engine.py") @args
exit $LASTEXITCODE
