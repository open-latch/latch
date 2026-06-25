<#
.SYNOPSIS
  The panic button. Instantly no-op ALL latch hooks + the compactor by creating
  the DISABLE sentinel at the repo root. Windows-native counterpart of
  bin/latch_disable.sh.

.DESCRIPTION
  Checked first thing by every hook (`is_disabled()` in src/paths.py) and the
  compactor, so it takes effect on the very next prompt — no Claude Code restart
  needed. Latch-scoped: stops ONLY latch; your other skills/plugins/hooks keep
  working. The MCP server stays registered but inert unless a tool is called.

  Re-enable with:  .\latch_enable.ps1

.PARAMETER WriteOnly
  Create DISABLE_WRITE instead of DISABLE — no-ops only the write-side hooks
  (Stop / SessionEnd / compactor) while leaving the SessionStart brief +
  per-prompt KB injection live.

.EXAMPLE
  .\latch_disable.ps1
.EXAMPLE
  .\latch_disable.ps1 -WriteOnly
#>
param([switch]$WriteOnly)
$ErrorActionPreference = "Stop"
$KbHome = if ($env:LATCH_HOME) { $env:LATCH_HOME } `
          elseif ($env:CLAUDE_KB_HOME) { $env:CLAUDE_KB_HOME } `
          else { (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")).Path }

if ($WriteOnly) {
  $flag = "DISABLE_WRITE"
  $scope = "write-side hooks only (Stop/SessionEnd/compactor); reads stay live"
} else {
  $flag = "DISABLE"
  $scope = "all latch hooks + compactor"
}
$target = Join-Path $KbHome $flag

if (Test-Path $target) {
  Write-Host "latch already disabled: $target exists ($scope)."
} else {
  @("latch kill switch - created by bin/latch_disable.ps1",
    "Delete this file (or run bin/latch_enable.ps1) to resume.") |
    Set-Content -Path $target -Encoding utf8
  Write-Host "latch DISABLED - created $target"
  Write-Host "  scope: $scope"
}
Write-Host "  re-enable: pwsh $KbHome/bin/latch_enable.ps1"
