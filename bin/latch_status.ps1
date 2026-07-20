<#
.SYNOPSIS
  Report whether latch's kill switch is engaged. Windows-native counterpart of
  bin/latch_status.sh — a quick "is it off right now?" check.
#>
$ErrorActionPreference = "Stop"
$KbHome = if ($env:LATCH_HOME) { $env:LATCH_HOME } `
          elseif ($env:CLAUDE_KB_HOME) { $env:CLAUDE_KB_HOME } `
          else { (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")).Path }

Write-Host "latch status (KB_HOME=$KbHome)"

$disable = Join-Path $KbHome "DISABLE"
$unlatched = Join-Path $KbHome "UNLATCHED"
$disableWrite = Join-Path $KbHome "DISABLE_WRITE"

if ($env:LATCH_UNLATCHED) {
  Write-Host "  [UNLATCHED] `$env:LATCH_UNLATCHED is set - latch influence is OFF for vanilla-agent mode."
  Write-Host "             disabled: session briefs, prompt KB injection, compaction, self-heal, maintenance."
  Write-Host "             still true: KB files stay local/unchanged; latch remains installed; control commands/MCP registration remain."
  Write-Host "             resume: unset LATCH_UNLATCHED, then run /unlatch"
} elseif (Test-Path $unlatched) {
  Write-Host "  [UNLATCHED] $unlatched exists - latch influence is OFF for vanilla-agent mode."
  Write-Host "             disabled: session briefs, prompt KB injection, compaction, self-heal, maintenance."
  Write-Host "             still true: KB files stay local/unchanged; latch remains installed; control commands/MCP registration remain."
  Write-Host "             resume: run /unlatch"
} elseif ($env:LATCH_DISABLE) {
  Write-Host "  [DISABLED] `$env:LATCH_DISABLE is set - all hooks + compactor no-op."
} elseif ($env:CLAUDE_KB_DISABLE) {
  Write-Host "  [DISABLED] legacy `$env:CLAUDE_KB_DISABLE is set - all hooks + compactor no-op."
} elseif (Test-Path $disable) {
  Write-Host "  [DISABLED] $disable exists - all hooks + compactor no-op."
  Write-Host "             resume: .\bin\latch_enable.ps1"
} else {
  Write-Host "  [ENABLED ] no UNLATCHED/DISABLE sentinel or env var - hooks active."
}

$Python = if ($env:LATCH_PYTHON) { $env:LATCH_PYTHON } else { $env:CLAUDE_KB_PYTHON }
if (-not $Python) {
  foreach ($candidate in @(
    (Join-Path $KbHome ".venv\Scripts\python.exe"),
    (Join-Path $KbHome ".venv\bin\python")
  )) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
      $Python = $candidate
      break
    }
  }
}
if (-not $Python) { $Python = "python" }
try {
  & $Python (Join-Path $KbHome "src\intensity_cli.py")
  if ($LASTEXITCODE -gt 1) {
    Write-Host "  warning: intensity status command exited $LASTEXITCODE"
  }
} catch {
  Write-Host "Latch intensity: unavailable (could not run src\intensity_cli.py)"
}

if ($env:LATCH_DISABLE_WRITE) {
  Write-Host "  [write-off] `$env:LATCH_DISABLE_WRITE is set - Stop/SessionEnd/compactor no-op; reads live."
} elseif ($env:CLAUDE_KB_DISABLE_WRITE) {
  Write-Host "  [write-off] legacy `$env:CLAUDE_KB_DISABLE_WRITE is set - Stop/SessionEnd/compactor no-op; reads live."
} elseif (Test-Path $disableWrite) {
  Write-Host "  [write-off] $disableWrite exists - Stop/SessionEnd/compactor no-op; reads live."
}
