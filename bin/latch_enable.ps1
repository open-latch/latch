<#
.SYNOPSIS
  Undo latch_disable.ps1 by removing the DISABLE sentinel so latch's hooks +
  compactor resume on the next prompt. Windows-native counterpart of
  bin/latch_enable.sh.

.DESCRIPTION
  By default removes the full-stop DISABLE file and UNLATCHED receipt, while
  leaving DISABLE_WRITE alone (a deliberate finer-grained control). Pass -All to
  also remove DISABLE_WRITE and return to fully-default behavior.

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
$ProjectDir = (Get-Location).Path

$removed = $false
$disable = Join-Path $KbHome "DISABLE"
$unlatched = Join-Path $KbHome "UNLATCHED"
$disableWrite = Join-Path $KbHome "DISABLE_WRITE"
$unlatchState = Join-Path $KbHome "UNLATCH_STATE.json"

function Resolve-Python {
  if ($env:LATCH_PYTHON) { return $env:LATCH_PYTHON }
  if ($env:CLAUDE_KB_PYTHON) { return $env:CLAUDE_KB_PYTHON }
  $venv = Join-Path $KbHome ".venv/bin/python"
  if (Test-Path $venv) { return $venv }
  $winVenv = Join-Path $KbHome ".venv/Scripts/python.exe"
  if (Test-Path $winVenv) { return $winVenv }
  $py = Get-Command python3 -ErrorAction SilentlyContinue
  if ($py) { return $py.Source }
  $py = Get-Command python -ErrorAction SilentlyContinue
  if ($py) { return $py.Source }
  return $null
}

function Restore-UnlatchedInstructions {
  $py = Resolve-Python
  if (-not $py) {
    throw "latch_enable: UNLATCHED is active but no Python was found to restore project instruction files. Set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON), then run bin/latch.ps1 -Confirm latch."
  }
  & $py (Join-Path $KbHome "src/unlatch.py") on --project $ProjectDir --legacy-state
  if ($LASTEXITCODE -ne 0) {
    throw "latch_enable: verified legacy Unlatch recovery failed; global UNLATCHED was not removed"
  }
}

if ((Test-Path $unlatched) -or (Test-Path $unlatchState)) {
  Restore-UnlatchedInstructions
}

if (Test-Path $disable) {
  Remove-Item -Force $disable
  Write-Host "removed $disable"
  $removed = $true
}

if (Test-Path $unlatched) {
  Remove-Item -Force $unlatched
  Write-Host "removed $unlatched"
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

$envStillOff = [bool]($env:LATCH_UNLATCHED -or $env:LATCH_DISABLE -or $env:CLAUDE_KB_DISABLE)

if ($removed -and -not $envStillOff) {
  Write-Host "latch ENABLED - hooks resume on the next prompt."
} elseif ($envStillOff) {
  Write-Host "latch files are ENABLED, but an environment disable flag is still set."
  Write-Host "Unset it before expecting hooks to resume."
} else {
  Write-Host "latch was not disabled (no DISABLE or UNLATCHED file). Nothing to do."
}
