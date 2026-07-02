<#
.SYNOPSIS
  Verify latch's Codex preview wiring.

.DESCRIPTION
  Checks Codex config.toml, AGENTS.md, the MCP launch target, and Codex compact
  transcript resolution. This is the Codex counterpart to latch_doctor.ps1; it
  does not inspect or mutate Claude Code settings.
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

& $Py (Join-Path $KbHome "src/codex_doctor.py") @args
exit $LASTEXITCODE
