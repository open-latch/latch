<#
.SYNOPSIS
  Thin wrapper. Sync latch's CLAUDE.md engine-contract region into a project's
  CLAUDE.md from claude_md_snippet.md (the single source of truth). Windows-
  native counterpart of bin/install_claude_md.sh.

.DESCRIPTION
  All logic lives in src/claude_md_sync.py so this CLI, the SessionStart hook
  auto-sync, and the drift gate share ONE implementation. Non-destructive: backs
  up the target to <name>.latchbak, only rewrites the managed region, never
  deletes the file. Idempotent.

.EXAMPLE
  .\install_claude_md.ps1 C:/proj/CLAUDE.md          # sync the region
.EXAMPLE
  .\install_claude_md.ps1 --check C:/proj/CLAUDE.md  # verify only; exit 1 on drift
#>
$ErrorActionPreference = "Stop"
$KbHome = if ($env:LATCH_HOME) { $env:LATCH_HOME } `
          elseif ($env:CLAUDE_KB_HOME) { $env:CLAUDE_KB_HOME } `
          else { (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")).Path }
$Py = if ($env:LATCH_PYTHON) { $env:LATCH_PYTHON } elseif ($env:CLAUDE_KB_PYTHON) { $env:CLAUDE_KB_PYTHON } else { "python" }
& $Py (Join-Path $KbHome "src/claude_md_sync.py") @args
exit $LASTEXITCODE
