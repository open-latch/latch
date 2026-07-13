$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LatchHome = if ($env:LATCH_HOME) { $env:LATCH_HOME } elseif ($env:CLAUDE_KB_HOME) { $env:CLAUDE_KB_HOME } else { Split-Path -Parent $ScriptDir }

if ($env:LATCH_PYTHON) {
  $Py = $env:LATCH_PYTHON
} elseif ($env:CLAUDE_KB_PYTHON) {
  $Py = $env:CLAUDE_KB_PYTHON
} elseif (Test-Path (Join-Path $LatchHome ".venv/Scripts/python.exe")) {
  $Py = Join-Path $LatchHome ".venv/Scripts/python.exe"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $Py = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
  $Py = "py"
} else {
  Write-Error "latch_proof_packet: no Python found (set LATCH_PYTHON)."
  exit 2
}

& $Py (Join-Path $LatchHome "src/proof_packet.py") @args
exit $LASTEXITCODE
