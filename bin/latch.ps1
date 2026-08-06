<# Read-only status by default; confirmed scope configuration on request. #>
[CmdletBinding()]
param(
  [ValidateSet("latch")]
  [string]$Confirm,
  [switch]$Shared,
  [switch]$Private,
  [string]$KbDir,
  [switch]$NewKb,
  [switch]$RequireExplicitScopes
)

$ErrorActionPreference = "Stop"
$KbHome = if ($env:LATCH_HOME) { $env:LATCH_HOME } elseif ($env:CLAUDE_KB_HOME) { $env:CLAUDE_KB_HOME } else { Split-Path -Parent $PSScriptRoot }
$ProjectInput = (Get-Location).Path

function Resolve-LatchPython {
  if ($env:LATCH_PYTHON) { return $env:LATCH_PYTHON }
  if ($env:CLAUDE_KB_PYTHON) { return $env:CLAUDE_KB_PYTHON }
  foreach ($candidate in @(
    (Join-Path $KbHome ".venv/Scripts/python.exe"),
    (Join-Path $KbHome ".venv/bin/python")
  )) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
  }
  foreach ($name in @("python3", "python")) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
  }
  throw "latch: no Python found; set LATCH_PYTHON"
}

$Python = Resolve-LatchPython
$Controller = Join-Path $KbHome "src/project_mode.py"
if (-not $PSBoundParameters.ContainsKey("Confirm")) {
  & $Python $Controller status --project $ProjectInput --intent latch
  exit $LASTEXITCODE
}
if ($Confirm -cne "latch") { throw "latch: confirmation must be exactly 'latch'" }
if ($Shared -and $Private) { throw "latch: choose Shared or Private, not both" }
if ($RequireExplicitScopes -and -not $Shared) {
  throw "latch: -RequireExplicitScopes is an existing-global migration and requires -Shared"
}

$ModeArgs = @("latch", "--project", $ProjectInput, "--confirm", "latch")
if ($Shared) { $ModeArgs += "--shared" }
if ($Private) { $ModeArgs += "--private" }
if ($KbDir) { $ModeArgs += @("--kb-dir", $KbDir) }
if ($NewKb) { $ModeArgs += "--new-kb" }
if ($RequireExplicitScopes) { $ModeArgs += "--require-explicit-scopes" }
& $Python $Controller @ModeArgs
exit $LASTEXITCODE
