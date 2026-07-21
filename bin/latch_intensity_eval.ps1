$ErrorActionPreference = "Stop"
$KbHome = if ($env:LATCH_HOME) { $env:LATCH_HOME } `
          elseif ($env:CLAUDE_KB_HOME) { $env:CLAUDE_KB_HOME } `
          else { (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")).Path }
$Python = if ($env:LATCH_PYTHON) { $env:LATCH_PYTHON } else { $env:CLAUDE_KB_PYTHON }
if (-not $Python) {
  foreach ($candidate in @(
    (Join-Path $KbHome ".venv\Scripts\python.exe"),
    (Join-Path $KbHome ".venv\bin\python")
  )) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
      $Python = $candidate
      break
    }
  }
}
if (-not $Python) { $Python = "python" }
& $Python (Join-Path $KbHome "src\intensity_evals.py") @args
exit $LASTEXITCODE
