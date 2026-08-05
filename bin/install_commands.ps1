<#
.SYNOPSIS
  Install latch's slash-command wrappers into the user's Claude Code commands dir.

.DESCRIPTION
  Claude Code only discovers slash commands under ~/.claude/commands/ (or a
  project's .claude/commands/); it does NOT scan this repo's commands/ folder.
  So the command source lives in the repo and must be copied into a scanned
  location. This is the Windows-native version of bin/install_commands.sh.

  Self-locating: resolves the repo root from this script's path and substitutes
  the <KB_HOME> placeholder in each command with the repo's ACTUAL location, so
  installed commands work regardless of clone location — no environment variable
  required. (The runtime LATCH_HOME env var remains an optional wrapper
  override; CLAUDE_KB_HOME is the legacy alias.)

  Idempotent — safe to re-run after editing any command. Override the
  destination with $env:CLAUDE_COMMANDS_DIR.
#>
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$KbHome = (Resolve-Path (Join-Path $ScriptDir "..")).Path -replace '\\', '/'
$SrcDir = Join-Path $ScriptDir "..\commands"
$DestDir = if ($env:CLAUDE_COMMANDS_DIR) { $env:CLAUDE_COMMANDS_DIR } `
           else { Join-Path $HOME ".claude/commands" }

# Reuse the Python installer's shell-literal renderer. Installation paths are
# passed as argv, so PowerShell never interprets their metacharacters.
$PythonBin = if ($env:LATCH_PYTHON) { $env:LATCH_PYTHON } `
             elseif ($env:CLAUDE_KB_PYTHON) { $env:CLAUDE_KB_PYTHON } `
             else { $null }
if (-not $PythonBin) {
    foreach ($candidate in @(
        (Join-Path $KbHome '.venv/Scripts/python.exe'),
        (Join-Path $KbHome '.venv/bin/python')
    )) {
        if (Test-Path -LiteralPath $candidate) { $PythonBin = $candidate; break }
    }
}
if (-not $PythonBin) {
    $pythonCommand = Get-Command python3, python -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($pythonCommand) { $PythonBin = $pythonCommand.Source }
}
if (-not $PythonBin) {
    throw "Python is required to render command installation paths safely"
}

$RenderCommandCode = @'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import install_engine as ie
body = Path(sys.argv[3]).read_text(encoding="utf-8")
body = ie.render_command_template(body, kb_home=sys.argv[2])
if sys.argv[5] == "kb-gate.md":
    body = body.replace("/bin/run_latch_gate.sh", "/bin/run_kb_gate.sh")
Path(sys.argv[4]).write_text(body, encoding="utf-8")
'@

function Write-RenderedCommand($Source, $Target, $Legacy = '__none__') {
    & $PythonBin -c $RenderCommandCode (Join-Path $KbHome 'src') $KbHome $Source $Target $Legacy
    if ($LASTEXITCODE -ne 0) {
        throw "failed to render command template $Source"
    }
}

if (-not (Test-Path $SrcDir)) {
    throw "no commands/ directory at $SrcDir"
}

New-Item -ItemType Directory -Force -Path $DestDir | Out-Null

$installed = 0
$updated = 0
$removed = 0
$skipped = 0
Get-ChildItem -Path $SrcDir -Filter *.md | ForEach-Object {
    Write-RenderedCommand $_.FullName (Join-Path $DestDir $_.Name)
    Write-Host "installed $($_.Name)"
    $script:installed++
}

function Test-LatchCommand($Path) {
    if (-not (Test-Path $Path)) { return $false }
    $body = Get-Content $Path -Raw
    $normalized = $body -replace '\\', '/'
    if ($body.Contains('<KB_HOME>')) { return $true }
    if ($body.Contains('<KB_HOME_POSIX_LITERAL>')) { return $true }
    if ($body.Contains('<LATCH_REVIEW_POSIX_LITERAL>')) { return $true }
    if ($body.Contains('<LATCH_REVIEW_POWERSHELL_LITERAL>')) { return $true }
    if ($normalized.Contains($KbHome)) { return $true }
    return ($normalized -match '/bin/(run_kb_gate|run_latch_gate|latch_baseline|unlatch|latch_gate_report|run_compact_now|run_latch_compact_now|run_kb_focus)\.sh|/bin/latch_direction\.sh|/bin/latch-review(\.ps1)?|/src/(budget|maintenance)\.py|kb_profile_(active|bind)|mission-control verification profile|trust-and-go verification profile')
}

function Update-LegacyAlias($Legacy, $Primary) {
    $legacyPath = Join-Path $DestDir $Legacy
    $primaryPath = Join-Path $SrcDir $Primary
    if (-not (Test-Path $legacyPath) -or -not (Test-Path $primaryPath)) { return }
    if (-not (Test-LatchCommand $legacyPath)) {
        Write-Host "skipped legacy alias $Legacy (looks user-owned)"
        $script:skipped++
        return
    }
    Write-RenderedCommand $primaryPath $legacyPath $Legacy
    Write-Host "updated legacy alias $Legacy -> $Primary"
    $script:updated++
}

Update-LegacyAlias "kb-budget-approve.md" "latch-budget-approve.md"
Update-LegacyAlias "kb-compact.md" "latch-compact.md"
Update-LegacyAlias "kb-decay.md" "latch-decay.md"
Update-LegacyAlias "kb-gate.md" "latch-gate.md"
Update-LegacyAlias "kb-gate-report.md" "latch-gate-report.md"
Update-LegacyAlias "kb-heal.md" "latch-heal.md"
Update-LegacyAlias "kb-tree.md" "latch-tree.md"

foreach ($stale in @("latch-baseline.md", "kb-focus.md", "kb-project-direction.md", "mission-control.md", "trust-and-go.md")) {
    $stalePath = Join-Path $DestDir $stale
    if (-not (Test-Path $stalePath)) { continue }
    if (-not (Test-LatchCommand $stalePath)) {
        Write-Host "skipped stale legacy command $stale (looks user-owned)"
        $skipped++
        continue
    }
    Remove-Item -Force $stalePath
    Write-Host "removed stale legacy command $stale"
    $removed++
}

Write-Host "Done - installed $installed command(s), updated $updated legacy alias(es), removed $removed stale command(s), skipped $skipped user-owned file(s) in $DestDir (KB_HOME=$KbHome)"
