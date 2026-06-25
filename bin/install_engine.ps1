<#
.SYNOPSIS
  Wire the latch KB engine into Claude Code. Windows-native counterpart of
  bin/install_engine.sh.

.DESCRIPTION
  The engine half of the latch install. All logic lives in
  src/install_engine.py (stdlib-only, shared with the bash wrapper). It:
    1. registers the MCP server via `claude mcp add --scope user` — the store
       Claude Code actually reads; it does NOT read mcpServers from
       settings.json;
    2. merges the SessionStart/UserPromptSubmit/Stop/SessionEnd hooks into
       ~/.claude/settings.json;
    3. adds server-level `mcp__latch` and legacy `mcp__claude-kb` permission
       rules so every kb_* tool is auto-approved (no per-tool prompts).
  Also removes dead latch-owned mcpServers blocks left by older installs.
  Idempotent and non-destructive (settings.json -> settings.json.latchbak).

  Interpreter resolution for running the installer: $env:LATCH_PYTHON, else
  legacy $env:CLAUDE_KB_PYTHON, else `python`. (The interpreter it REGISTERS
  is resolved inside the script and prefers the repo .venv.)

.EXAMPLE
  .\install_engine.ps1
.EXAMPLE
  .\install_engine.ps1 --dry-run
.EXAMPLE
  .\install_engine.ps1 --check
#>
$ErrorActionPreference = "Stop"
$KbHome = if ($env:LATCH_HOME) { $env:LATCH_HOME } `
          elseif ($env:CLAUDE_KB_HOME) { $env:CLAUDE_KB_HOME } `
          else { (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")).Path }
$Py = if ($env:LATCH_PYTHON) { $env:LATCH_PYTHON } elseif ($env:CLAUDE_KB_PYTHON) { $env:CLAUDE_KB_PYTHON } else { "python" }
& $Py (Join-Path $KbHome "src/install_engine.py") @args
exit $LASTEXITCODE
