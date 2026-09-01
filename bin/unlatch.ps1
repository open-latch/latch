<#
.SYNOPSIS
  Confirmed in-place vanilla-agent / escape-hatch mode for latch.

.DESCRIPTION
  No-argument mode prints current state and the exact confirmation word.
  State changes require: -Confirm unlatch or -Confirm latch.
#>
[CmdletBinding(PositionalBinding=$false)]
param([string]$Confirm = "")
$ErrorActionPreference = "Stop"
$KbHome = if ($env:LATCH_HOME) { $env:LATCH_HOME } `
          elseif ($env:CLAUDE_KB_HOME) { $env:CLAUDE_KB_HOME } `
          else { (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")).Path }

$Unlatched = Join-Path $KbHome "UNLATCHED"
$Disable = Join-Path $KbHome "DISABLE"
$DisableWrite = Join-Path $KbHome "DISABLE_WRITE"
$UnlatchState = Join-Path $KbHome "UNLATCH_STATE.json"
$ProjectDir = (Get-Location).Path

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

function Invoke-InstructionMask($Action) {
  $py = Resolve-Python
  if (-not $py) {
    throw "unlatch: no Python found; set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON) and re-run."
  }
  & $py (Join-Path $KbHome "src/latch/install/unlatch.py") $Action --project $ProjectDir
}

function Test-Unlatched {
  return [bool]($env:LATCH_UNLATCHED -or (Test-Path $Unlatched))
}

function Write-UnlatchedReceipt {
  @(
    "latch unlatched mode - created by bin/unlatch.ps1",
    "Latch is currently UNLATCHED.",
    "This is the agent without latch's project judgment layer.",
    "DISABLE enforces full influence-off; UNLATCHED makes the off state visible.",
    "Run /unlatch again to re-latch. KB data is not deleted.",
    "If LATCH_UNLATCHED is set, unset it too before expecting hooks to resume.",
    "UNLATCHED_LATCH_HOME=$KbHome"
  ) | Set-Content -Path $Unlatched -Encoding utf8
}

function Write-DisableReceipt {
  @(
    "latch kill switch - created by bin/unlatch.ps1",
    "Unlatched mode is active. Run /unlatch again to re-latch.",
    "If LATCH_UNLATCHED is set, unset it too before expecting hooks to resume."
  ) | Set-Content -Path $Disable -Encoding utf8
}

function Show-UnlatchedFacts {
  Write-Host "  disabled: prompt KB injection, gate guidance,"
  Write-Host "            Stop/SessionEnd compaction, self-heal, maintenance, and"
  Write-Host "            automatic latch writes for this latch install."
  Write-Host "  still true: KB files stay local and unchanged; latch remains installed;"
  Write-Host "              /unlatch and status commands remain available;"
  Write-Host "              non-latch tools/hooks are unaffected."
  Write-Host "  scope: install-level; if you change repos before re-latching, latch"
  Write-Host "         remains off and will say so."
}

function Show-StatusPrompt {
  Write-Host "latch unlatch status (KB_HOME=$KbHome)"
  if (Test-Unlatched) {
    Write-Host "  [UNLATCHED] Latch is currently UNLATCHED."
    Show-UnlatchedFacts
    Write-Host ""
    Write-Host "Switch back to LATCHED mode?"
    if ($env:LATCH_UNLATCHED) {
      Write-Host "Confirming latch cleans local unlatch files/state, but hooks stay off until LATCH_UNLATCHED is unset."
    } else {
      Write-Host "Latch hooks will resume on the next prompt."
    }
    Write-Host "Reply exactly: latch"
  } elseif ($env:LATCH_DISABLE -or $env:CLAUDE_KB_DISABLE -or (Test-Path $Disable)) {
    Write-Host "  [DISABLED] latch kill switch is active, but Unlatched mode is not set."
    Write-Host "             To re-enable the kill switch directly: pwsh $KbHome/bin/latch_enable.ps1"
  } else {
    Write-Host "  [LATCHED] Latch is currently LATCHED."
    Write-Host ""
    Write-Host "Switch to UNLATCHED mode?"
    Write-Host "This turns latch's project-judgment layer off for this latch install, masks latch-managed CLAUDE.md/AGENTS.md regions in this project, and leaves KB data intact."
    Write-Host "If you change repos before re-latching, latch remains off and will say so."
    Write-Host "To re-latch later, run /unlatch again."
    Write-Host "Reply exactly: unlatch"
  }

  if ($env:LATCH_DISABLE_WRITE -or $env:CLAUDE_KB_DISABLE_WRITE -or (Test-Path $DisableWrite)) {
    Write-Host "  [write-off] write-side kill switch is also active."
  }
}

if (-not $Confirm) {
  Show-StatusPrompt
  try {
    Invoke-InstructionMask "status"
  } catch {
    Write-Host "  instruction mask status unavailable; no state changed."
    Write-Host "  $($_.Exception.Message)"
  }
  exit 0
}

switch -CaseSensitive ($Confirm) {
  "unlatch" {
    if (Test-Unlatched) {
      Write-Host "Latch is already UNLATCHED for this latch install. Retrying instruction mask for the current project."
      Invoke-InstructionMask "off"
      if (-not (Test-Path $Unlatched)) {
        Write-UnlatchedReceipt
        Write-Host "created $Unlatched"
      }
      if (-not (Test-Path $Disable)) {
        Write-DisableReceipt
        Write-Host "created $Disable"
      }
      exit 0
    }
    Invoke-InstructionMask "off"
    Write-UnlatchedReceipt
    Write-Host "created $Unlatched"
    if (Test-Path $Disable) {
      Write-Host "full kill switch already present - $Disable exists."
    } else {
      Write-DisableReceipt
      Write-Host "created $Disable"
    }
    Write-Host "Latch is now UNLATCHED - this is the agent without latch's project judgment layer."
    Show-UnlatchedFacts
    Write-Host "  re-latch: run /unlatch again and confirm latch"
  }
  "latch" {
    if (-not (Test-Unlatched) -and -not (Test-Path $UnlatchState)) {
      Write-Host "Latch is already LATCHED. No action taken."
      exit 0
    }
    Invoke-InstructionMask "on"
    $removed = $false
    foreach ($f in @($Unlatched, $Disable, $DisableWrite)) {
      if (Test-Path $f) {
        Remove-Item -Force $f
        Write-Host "removed $f"
        $removed = $true
      }
    }
    $envStillOff = [bool]($env:LATCH_UNLATCHED -or $env:LATCH_DISABLE -or $env:CLAUDE_KB_DISABLE)
    if ($removed -and -not $envStillOff) {
      Write-Host "Latch is now LATCHED - hooks resume on the next prompt."
    } elseif ($envStillOff) {
      Write-Host "Latch files are LATCHED, but an environment disable flag is still set."
      Write-Host "Unset it before expecting hooks to resume."
    } else {
      Write-Host "Latch was already LATCHED. Nothing to do."
    }
  }
  default {
    Write-Error "unlatch: confirmation must be exactly 'unlatch' or 'latch'."
    exit 2
  }
}
