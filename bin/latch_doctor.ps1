<#
.SYNOPSIS
  Thin wrapper around src/doctor.py — the cross-platform install verifier.
  Windows-native counterpart of bin/latch_doctor.sh.

.DESCRIPTION
  Run AFTER installing latch to confirm the environment can actually load and
  run the tool (deps fully installed, sqlite-vec loads, embedder runs). All
  logic lives in src/doctor.py. Exit code 0 = healthy; non-zero = a hard check
  failed. Interpreter resolution mirrors src/install_engine.py:resolve_python so
  the doctor tests the same interpreter the hooks run under:
  $env:LATCH_PYTHON, else legacy $env:CLAUDE_KB_PYTHON, else the repo venv
  (.venv\Scripts\python.exe), else `python`.

.EXAMPLE
  .\latch_doctor.ps1
.EXAMPLE
  .\latch_doctor.ps1 --skip-embed --json
#>
$ErrorActionPreference = "Stop"
$KbHome = if ($env:LATCH_HOME) { $env:LATCH_HOME } `
          elseif ($env:CLAUDE_KB_HOME) { $env:CLAUDE_KB_HOME } `
          else { (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")).Path }
$VenvWin = Join-Path $KbHome ".venv\Scripts\python.exe"
$VenvPosix = Join-Path $KbHome ".venv/bin/python"
$Py = if     ($env:LATCH_PYTHON) { $env:LATCH_PYTHON }
      elseif ($env:CLAUDE_KB_PYTHON) { $env:CLAUDE_KB_PYTHON }
      elseif (Test-Path $VenvWin)    { $VenvWin }
      elseif (Test-Path $VenvPosix)  { $VenvPosix }
      else                           { "python" }
& $Py (Join-Path $KbHome "src/doctor.py") @args
exit $LASTEXITCODE
