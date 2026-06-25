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
$disableWrite = Join-Path $KbHome "DISABLE_WRITE"

if ($env:LATCH_DISABLE) {
  Write-Host "  [DISABLED] `$env:LATCH_DISABLE is set - all hooks + compactor no-op."
} elseif ($env:CLAUDE_KB_DISABLE) {
  Write-Host "  [DISABLED] legacy `$env:CLAUDE_KB_DISABLE is set - all hooks + compactor no-op."
} elseif (Test-Path $disable) {
  Write-Host "  [DISABLED] $disable exists - all hooks + compactor no-op."
  Write-Host "             resume: .\bin\latch_enable.ps1"
} else {
  Write-Host "  [ENABLED ] no DISABLE sentinel / env var - hooks active."
}

if ($env:LATCH_DISABLE_WRITE) {
  Write-Host "  [write-off] `$env:LATCH_DISABLE_WRITE is set - Stop/SessionEnd/compactor no-op; reads live."
} elseif ($env:CLAUDE_KB_DISABLE_WRITE) {
  Write-Host "  [write-off] legacy `$env:CLAUDE_KB_DISABLE_WRITE is set - Stop/SessionEnd/compactor no-op; reads live."
} elseif (Test-Path $disableWrite) {
  Write-Host "  [write-off] $disableWrite exists - Stop/SessionEnd/compactor no-op; reads live."
}
