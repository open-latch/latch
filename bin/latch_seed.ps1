<#
.SYNOPSIS
  Seed latch from prior local agent work for immediate judgment value.

.DESCRIPTION
  Windows-native counterpart of bin/latch_seed.sh. Uses LLM calls with call-cap
  guardrails. Defaults to preview-only; pass --apply to write approved
  candidates as staging KB evidence.
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
& $Py (Join-Path $KbHome "src\seed.py") @args
exit $LASTEXITCODE
