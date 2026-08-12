<# Read-only status by default; confirmed OFF transition on request. #>
[CmdletBinding()]
param(
  [ValidateSet("unlatch")]
  [string]$Confirm
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
  throw "unlatch: no Python found; set LATCH_PYTHON"
}

$Python = Resolve-LatchPython
$Controller = Join-Path $KbHome "src/project_mode.py"
if (-not $PSBoundParameters.ContainsKey("Confirm")) {
  & $Python $Controller status --project $ProjectInput --intent unlatch
  exit $LASTEXITCODE
}
if ($Confirm -cne "unlatch") {
  throw "unlatch: confirmation must be exactly 'unlatch'"
}
& $Python $Controller unlatch --project $ProjectInput --confirm unlatch
exit $LASTEXITCODE
