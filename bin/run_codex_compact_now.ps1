<#
.SYNOPSIS
  Manually compact the current Codex session into the latch KB.

.DESCRIPTION
  Codex preview counterpart of bin/run_codex_compact_now.sh. This wrapper is
  intentionally fail-closed: it delegates to src/codex_compact.py, which resolves
  the session from the first argument or $env:CODEX_THREAD_ID and validates a
  matching rollout transcript under ~/.codex/sessions. It never falls back to
  Claude Code transcripts under ~/.claude/projects. By default it uses Codex's
  own `codex exec` summarizer backend; pass --summarizer claude to exercise the
  legacy shared Claude CLI backend. Pass --background to validate the transcript
  and detach the compactor.

.EXAMPLE
  .\run_codex_compact_now.ps1
.EXAMPLE
  .\run_codex_compact_now.ps1 019eb473-b0cd-71c3-814b-32087eae8adc
#>
$ErrorActionPreference = "Stop"

$KbHome = if ($env:LATCH_HOME) {
  $env:LATCH_HOME
} elseif ($env:CLAUDE_KB_HOME) {
  $env:CLAUDE_KB_HOME
} else {
  (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")).Path
}

$Py = if ($env:LATCH_PYTHON) {
  $env:LATCH_PYTHON
} elseif ($env:CLAUDE_KB_PYTHON) {
  $env:CLAUDE_KB_PYTHON
} elseif (Test-Path (Join-Path $KbHome ".venv/Scripts/python.exe")) {
  Join-Path $KbHome ".venv/Scripts/python.exe"
} elseif (Test-Path (Join-Path $KbHome ".venv/bin/python")) {
  Join-Path $KbHome ".venv/bin/python"
} else {
  "python"
}

& $Py (Join-Path $KbHome "src/codex_compact.py") @args
exit $LASTEXITCODE
