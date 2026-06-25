<#
.SYNOPSIS
  One guided first-run path for Claude Code, Codex, or both.

.DESCRIPTION
  Windows-native counterpart of bin/latch_quickstart.sh. Delegates to the
  existing installers, doctors, and seed command.
#>
$ErrorActionPreference = "Stop"
$KbHome = if ($env:LATCH_HOME) { $env:LATCH_HOME } `
          elseif ($env:CLAUDE_KB_HOME) { $env:CLAUDE_KB_HOME } `
          else { (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")).Path }
$VenvPy = Join-Path $KbHome ".venv\Scripts\python.exe"
$Py = if ($env:LATCH_PYTHON) { $env:LATCH_PYTHON } `
      elseif ($env:CLAUDE_KB_PYTHON) { $env:CLAUDE_KB_PYTHON } `
      elseif (Test-Path $VenvPy) { $VenvPy } `
      else { "python" }
& $Py (Join-Path $KbHome "src\quickstart.py") @args
exit $LASTEXITCODE
