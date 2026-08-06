$ErrorActionPreference = "Stop"

$LatchReviewHome = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $LatchReviewHome ".venv/Scripts/python.exe"
if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
  $Python = Join-Path $LatchReviewHome ".venv/Scripts/python.exe"
} else {
  $PythonCommand = Get-Command python3, python -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if (-not $PythonCommand) {
    [Console]::Error.WriteLine("latch-review: Python 3.11 or newer was not found.")
    exit 2
  }
  $Python = $PythonCommand.Source
}

try {
  & $Python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
  if ($LASTEXITCODE -ne 0) {
    [Console]::Error.WriteLine("latch-review: selected interpreter is not Python 3.11 or newer: $Python")
    exit 2
  }
  & $Python (Join-Path $LatchReviewHome "src/local_review.py") @args
  $ReviewExitCode = $LASTEXITCODE
} catch {
  [Console]::Error.WriteLine("latch-review: failed to start Python: $($_.Exception.Message)")
  exit 2
}
if ($null -eq $ReviewExitCode) { exit 2 }
exit $ReviewExitCode
