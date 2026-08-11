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
  Write-Host "             disabled: prompt KB injection, compaction, self-heal, maintenance."
  Write-Host "             still true: KB files stay local/unchanged; latch remains installed; control commands/MCP registration remain."
  Write-Host "             resume: unset LATCH_UNLATCHED, then run /latch"
} elseif (Test-Path $unlatched) {
  Write-Host "  [UNLATCHED-GLOBAL] $unlatched exists - every project is OFF."
  Write-Host "             disabled: prompt KB injection, compaction, self-heal, maintenance."
  Write-Host "             still true: KB files stay local/unchanged; latch remains installed; control commands/MCP registration remain."
  Write-Host "             resume: .\bin\latch_enable.ps1"
} elseif ($env:LATCH_DISABLE) {
  Write-Host "  [DISABLED] `$env:LATCH_DISABLE is set - all hooks + compactor no-op."
} elseif ($env:CLAUDE_KB_DISABLE) {
  Write-Host "  [DISABLED] legacy `$env:CLAUDE_KB_DISABLE is set - all hooks + compactor no-op."
} elseif (Test-Path $disable) {
  Write-Host "  [DISABLED] $disable exists - all hooks + compactor no-op."
  Write-Host "             resume: .\bin\latch_enable.ps1"
} else {
  Write-Host "  [GLOBAL-CLEAR] no install-wide UNLATCHED/DISABLE switch is active."
}

$statusPython = $null
if ($env:LATCH_PYTHON) {
  $statusPython = $env:LATCH_PYTHON
} elseif ($env:CLAUDE_KB_PYTHON) {
  $statusPython = $env:CLAUDE_KB_PYTHON
} elseif (Test-Path (Join-Path $KbHome ".venv/Scripts/python.exe")) {
  $statusPython = Join-Path $KbHome ".venv/Scripts/python.exe"
} elseif (Test-Path (Join-Path $KbHome ".venv/bin/python")) {
  $statusPython = Join-Path $KbHome ".venv/bin/python"
} else {
  foreach ($name in @("python3", "python")) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($command) {
      $statusPython = $command.Source
      break
    }
  }
}
$projectStatusCode = 0
if ($statusPython) {
  Write-Host ""
  & $statusPython (Join-Path $KbHome "src/project_mode.py") status --project (Get-Location).Path
  $projectStatusCode = $LASTEXITCODE
} else {
  Write-Host ""
  Write-Host "  [UNKNOWN] project mode unavailable because Python was not found; no state changed."
}

if ($env:LATCH_DISABLE_WRITE) {
  Write-Host "  [write-off] `$env:LATCH_DISABLE_WRITE is set - Stop/SessionEnd/compactor no-op; reads live."
} elseif ($env:CLAUDE_KB_DISABLE_WRITE) {
  Write-Host "  [write-off] legacy `$env:CLAUDE_KB_DISABLE_WRITE is set - Stop/SessionEnd/compactor no-op; reads live."
} elseif (Test-Path $disableWrite) {
  Write-Host "  [write-off] $disableWrite exists - Stop/SessionEnd/compactor no-op; reads live."
}

if ($projectStatusCode -ne 0) { exit $projectStatusCode }
