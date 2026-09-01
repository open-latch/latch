$ErrorActionPreference = "Stop"
$LatchHome = if ($env:LATCH_HOME) { $env:LATCH_HOME } elseif ($env:CLAUDE_KB_HOME) { $env:CLAUDE_KB_HOME } else { Split-Path -Parent $PSScriptRoot }
$Py = if ($env:LATCH_PYTHON) { $env:LATCH_PYTHON } elseif ($env:CLAUDE_KB_PYTHON) { $env:CLAUDE_KB_PYTHON } elseif (Test-Path (Join-Path $LatchHome ".venv\Scripts\python.exe")) { Join-Path $LatchHome ".venv\Scripts\python.exe" } elseif (Test-Path (Join-Path $LatchHome ".venv\bin\python")) { Join-Path $LatchHome ".venv\bin\python" } else { "python" }
& $Py (Join-Path $LatchHome "src\latch\install\update_latch.py") @args
exit $LASTEXITCODE
