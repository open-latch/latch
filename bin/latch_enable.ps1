<#
.SYNOPSIS
  Undo latch_disable.ps1 by removing the DISABLE sentinel so latch's hooks +
  compactor resume on the next prompt. Windows-native counterpart of
  bin/latch_enable.sh.

.DESCRIPTION
  By default removes ONLY the full-stop DISABLE file and leaves DISABLE_WRITE
  alone (a deliberate finer-grained control). Pass -All to also remove
  DISABLE_WRITE and return to fully-default behavior.

.EXAMPLE
  .\latch_enable.ps1
.EXAMPLE
  .\latch_enable.ps1 -All
#>
param([switch]$All)
$ErrorActionPreference = "Stop"
$KbHome = if ($env:LATCH_HOME) { $env:LATCH_HOME } `
          elseif ($env:CLAUDE_KB_HOME) { $env:CLAUDE_KB_HOME } `
          else { (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")).Path }

$removed = $false
$disable = Join-Path $KbHome "DISABLE"
$disableWrite = Join-Path $KbHome "DISABLE_WRITE"

if (Test-Path $disable) {
  Remove-Item -Force $disable
  Write-Host "removed $disable"
  $removed = $true
}

if ($All -and (Test-Path $disableWrite)) {
  Remove-Item -Force $disableWrite
  Write-Host "removed $disableWrite"
  $removed = $true
} elseif (Test-Path $disableWrite) {
  Write-Host "note: $disableWrite still present - write-side hooks"
  Write-Host "      (Stop/SessionEnd/compactor) stay OFF. Remove with: .\latch_enable.ps1 -All"
}

if ($removed) {
  Write-Host "latch ENABLED - hooks resume on the next prompt."
} else {
  Write-Host "latch was not disabled (no DISABLE file). Nothing to do."
}
