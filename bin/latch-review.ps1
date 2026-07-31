$ErrorActionPreference = "Stop"

$LatchReviewHome = Split-Path -Parent $PSScriptRoot
if ($env:LATCH_PYTHON) {
  $Python = $env:LATCH_PYTHON
} elseif ($env:CLAUDE_KB_PYTHON) {
  $Python = $env:CLAUDE_KB_PYTHON
} elseif (Test-Path (Join-Path $LatchReviewHome ".venv/Scripts/python.exe")) {
  $Python = Join-Path $LatchReviewHome ".venv/Scripts/python.exe"
} else {
  $Python = "python"
}

& $Python (Join-Path $LatchReviewHome "src/local_review.py") @args
exit $LASTEXITCODE
