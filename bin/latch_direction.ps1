$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$KBHome = if ($env:LATCH_HOME) { $env:LATCH_HOME } elseif ($env:CLAUDE_KB_HOME) { $env:CLAUDE_KB_HOME } else { Split-Path -Parent $ScriptDir }

if ($env:LATCH_PYTHON) {
  $Py = $env:LATCH_PYTHON
} elseif ($env:CLAUDE_KB_PYTHON) {
  $Py = $env:CLAUDE_KB_PYTHON
} elseif (Test-Path (Join-Path $KBHome ".venv/Scripts/python.exe")) {
  $Py = Join-Path $KBHome ".venv/Scripts/python.exe"
} elseif (Test-Path (Join-Path $KBHome ".venv/bin/python")) {
  $Py = Join-Path $KBHome ".venv/bin/python"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $Py = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
  $Py = "py"
} else {
  Write-Error "latch_direction: no Python found (set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON) to your interpreter)."
  exit 2
}

& $Py (Join-Path $KBHome "src/project_direction.py") --project (Get-Location).Path @args
exit $LASTEXITCODE
