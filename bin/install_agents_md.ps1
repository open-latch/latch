$ErrorActionPreference = "Stop"

$KbHome = if ($env:LATCH_HOME) {
  $env:LATCH_HOME
} elseif ($env:CLAUDE_KB_HOME) {
  $env:CLAUDE_KB_HOME
} else {
  Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

if ($env:LATCH_PYTHON) {
  $Py = $env:LATCH_PYTHON
} elseif ($env:CLAUDE_KB_PYTHON) {
  $Py = $env:CLAUDE_KB_PYTHON
} elseif (Test-Path (Join-Path $KbHome ".venv/Scripts/python.exe")) {
  $Py = Join-Path $KbHome ".venv/Scripts/python.exe"
} elseif (Test-Path (Join-Path $KbHome ".venv/bin/python")) {
  $Py = Join-Path $KbHome ".venv/bin/python"
} else {
  $Py = "python"
}

& $Py (Join-Path $KbHome "src/agents_md_sync.py") @args
exit $LASTEXITCODE
