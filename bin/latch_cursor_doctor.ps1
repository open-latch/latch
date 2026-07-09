<#
.SYNOPSIS
  Verify latch's Cursor preview wiring.

.DESCRIPTION
  Checks .cursor/mcp.json, AGENTS.md, the MCP launch target, and optionally
  Cursor CLI MCP visibility. It does not inspect or mutate Claude Code or Codex
  settings.
#>
$ErrorActionPreference = "Stop"

$KbHome = if ($env:LATCH_HOME) {
  $env:LATCH_HOME
} elseif ($env:CLAUDE_KB_HOME) {
  $env:CLAUDE_KB_HOME
} else {
  (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")).Path
}

$Py = if ($env:LATCH_PYTHON) {
  $env:LATCH_PYTHON
} elseif ($env:CLAUDE_KB_PYTHON) {
  $env:CLAUDE_KB_PYTHON
} elseif (Test-Path (Join-Path $KbHome ".venv/Scripts/python.exe")) {
  Join-Path $KbHome ".venv/Scripts/python.exe"
} elseif (Test-Path (Join-Path $KbHome ".venv/bin/python")) {
  Join-Path $KbHome ".venv/bin/python"
} else {
  "python"
}

& $Py (Join-Path $KbHome "src/cursor_doctor.py") @args
exit $LASTEXITCODE
