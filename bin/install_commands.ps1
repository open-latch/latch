$ErrorActionPreference = "Stop"

$LatchCommandHome = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Candidates = @(
  (Join-Path $LatchCommandHome ".venv/Scripts/python.exe"),
  (Join-Path $LatchCommandHome ".venv/bin/python")
)
$Python = $Candidates | Where-Object {
  Test-Path -LiteralPath $_ -PathType Leaf
} | Select-Object -First 1
if (-not $Python) {
  $PythonCommand = Get-Command python3, python -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if ($PythonCommand) { $Python = $PythonCommand.Source }
}
if (-not $Python) {
  [Console]::Error.WriteLine("error: Python is required to install Claude slash commands")
  exit 1
}

$PriorLatchHome = $env:LATCH_HOME
$PriorLegacyHome = $env:CLAUDE_KB_HOME
try {
  Remove-Item Env:LATCH_HOME -ErrorAction SilentlyContinue
  Remove-Item Env:CLAUDE_KB_HOME -ErrorAction SilentlyContinue
  & $Python (Join-Path $LatchCommandHome "src/install_engine.py") --commands-only @args
  $InstallExitCode = $LASTEXITCODE
} finally {
  if ($null -ne $PriorLatchHome) { $env:LATCH_HOME = $PriorLatchHome }
  if ($null -ne $PriorLegacyHome) { $env:CLAUDE_KB_HOME = $PriorLegacyHome }
}
if ($null -eq $InstallExitCode) { exit 1 }
exit $InstallExitCode
