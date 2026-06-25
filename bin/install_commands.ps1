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

if (-not (Test-Path $SrcDir)) {
    throw "no commands/ directory at $SrcDir"
}

New-Item -ItemType Directory -Force -Path $DestDir | Out-Null

$count = 0
Get-ChildItem -Path $SrcDir -Filter *.md | ForEach-Object {
    $content = Get-Content $_.FullName -Raw
    $content = $content -replace [regex]::Escape('<KB_HOME>'), $KbHome
    Set-Content -Path (Join-Path $DestDir $_.Name) -Value $content -NoNewline
    Write-Host "installed $($_.Name)"
    $count++
}

Write-Host "Done - $count command(s) installed to $DestDir (KB_HOME=$KbHome)"
