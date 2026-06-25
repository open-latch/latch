<#
.SYNOPSIS
  Manual integration test for the latch uninstaller + kill switch on Windows -
  the PowerShell counterpart of tests/roundtrip_uninstall.sh.

.DESCRIPTION
  Installs latch into a fully SANDBOXED Claude Code config, exercises the kill
  switch, uninstalls, and asserts the REAL ~/.claude config + this repo are
  byte-unchanged. See tests/roundtrip_uninstall.md for the full write-up.

  Isolation (what keeps the test off your real machine):
    USERPROFILE -> sandbox  : Path.home() uses USERPROFILE on Windows (redirects
                              settings.json + ~/.claude). HOME also set.
    CLAUDE_CONFIG_DIR       : the `claude` CLI's user-scope MCP registry.
    LATCH_HOME             : snippet source, DISABLE files, KB data.
    CLAUDE_KB_HOME          : legacy alias coverage.
    CLAUDE_COMMANDS_DIR     : pins the slash-commands dir for both installers.
  SAFETY GATE: a --dry-run must report the SANDBOX settings path or the script
  ABORTS without writing. Worst case = "aborted", never "clobbered real config".

  Run:  pwsh -ExecutionPolicy Bypass -File .\tests\roundtrip_uninstall.ps1
        (Windows PowerShell 5.1: powershell -ExecutionPolicy Bypass -File ...)
.PARAMETER Commit
  Git committish to test (default HEAD).
#>
param([string]$Commit = 'HEAD')
$ErrorActionPreference = 'Stop'

$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$VenvPy = Join-Path $Repo '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPy)) { $VenvPy = 'python' }

$script:PASS = 0; $script:FAIL = 0
function Ok($m){ Write-Host "  PASS: $m"; $script:PASS++ }
function Bad($m){ Write-Host "  FAIL: $m" -ForegroundColor Red; $script:FAIL++ }
function FileHash($p){ if (Test-Path $p) { (Get-FileHash -Algorithm SHA256 $p).Hash } else { '' } }
# Read the registry FILE directly (deterministic; no `claude mcp get` health-
# check, which spawns the server and is timing-flaky).
function McpNode($p, $name='latch'){ if (-not (Test-Path $p)) { return $null }; try { (Get-Content $p -Raw | ConvertFrom-Json).mcpServers.$name } catch { $null } }
function McpCmd($p, $name='latch'){ $n = McpNode $p $name; if ($n) { $n.command } else { '' } }
function McpArgs($p, $name='latch'){ $n = McpNode $p $name; if ($n) { ($n.args -join ' ') } else { '' } }
function McpPresent($p, $name='latch'){ [bool](McpNode $p $name) }
function McpFingerprint($p){ @{ latch=(McpNode $p 'latch'); legacy=(McpNode $p 'claude-kb') } | ConvertTo-Json -Compress }
function CmdsHash($dir){ if (-not (Test-Path $dir)) { return '' }
  ((Get-ChildItem $dir -Filter *.md -EA SilentlyContinue | Sort-Object Name |
    ForEach-Object { "$($_.Name):$((Get-FileHash $_.FullName).Hash)" }) -join '|') }
function GitTracked(){ ((git -C $Repo status --porcelain=v1 --untracked-files=no) -join "`n") }

$RealHome      = $env:USERPROFILE
$RealDotClaude = Join-Path $RealHome '.claude.json'
$RealSettings  = Join-Path $RealHome '.claude\settings.json'
$RealCmds      = Join-Path $RealHome '.claude\commands'
$realSetBefore  = FileHash $RealSettings
$realMcpBefore  = McpFingerprint $RealDotClaude
$realCmdsBefore = CmdsHash $RealCmds
$realGitBefore  = GitTracked
# Snapshot real-repo kill-switch sentinels BEFORE the run: the leak check below
# asserts they are UNCHANGED across the run, not absent. A user who is mid-kill-
# switch legitimately has a DISABLE/DISABLE_WRITE in their repo, so an absolute-
# absence assert would false-fail; the sandbox writes its sentinels under
# LATCH_HOME / CLAUDE_KB_HOME (the sandbox), never the real repo, so
# "unchanged" is the test.
$repoDisBefore   = Test-Path (Join-Path $Repo 'DISABLE')
$repoDisWrBefore = Test-Path (Join-Path $Repo 'DISABLE_WRITE')
Write-Host "### REAL latch/legacy MCP fingerprint (must stay): $realMcpBefore"

$save = @{ USERPROFILE=$env:USERPROFILE; HOME=$env:HOME; CLAUDE_CONFIG_DIR=$env:CLAUDE_CONFIG_DIR;
           LATCH_HOME=$env:LATCH_HOME; CLAUDE_KB_HOME=$env:CLAUDE_KB_HOME; CLAUDE_COMMANDS_DIR=$env:CLAUDE_COMMANDS_DIR;
           LATCH_PYTHON=$env:LATCH_PYTHON; CLAUDE_KB_PYTHON=$env:CLAUDE_KB_PYTHON }
$SRoot = $null
try {
  $SRoot = Join-Path $env:TEMP ('latchsbx_' + [guid]::NewGuid().ToString('N').Substring(0,8))
  $Sbx = Join-Path $SRoot 'repo'; $H = Join-Path $SRoot 'home'; $Cfg = Join-Path $H '.claude'
  New-Item -ItemType Directory -Force -Path $Sbx, (Join-Path $Cfg 'commands') | Out-Null
  Write-Host "### sandbox: $SRoot (commit $Commit)"

  # Materialize the tree. Archive to a FILE then extract - do NOT pipe git|tar
  # in PowerShell (it corrupts the binary stream). tar ships with Win10+.
  $tarball = Join-Path $SRoot 'tree.tar'
  git -C $Repo archive --format=tar -o $tarball $Commit
  tar -x -f $tarball -C $Sbx
  if (-not (Test-Path (Join-Path $Sbx 'bin\uninstall.ps1'))) { throw "$Commit has no bin\uninstall.ps1" }

  $seed = @'
{
  "theme": "dark",
  "permissions": { "allow": ["Bash(ls:*)", "mcp__other-server__do_thing", "mcp__claude-kb__kb_get"] },
  "hooks": {
    "SessionStart": [ { "hooks": [ { "type": "command", "command": "echo NOT-LATCH-sessionstart" } ] } ],
    "PreToolUse":   [ { "matcher": "Bash", "hooks": [ { "type": "command", "command": "echo unrelated-pretooluse" } ] } ]
  }
}
'@
  [IO.File]::WriteAllText((Join-Path $Cfg 'settings.json'), $seed)
  [IO.File]::WriteAllText((Join-Path $Cfg 'commands\mycustom.md'), "my own custom command, not latch`n")

  $env:USERPROFILE         = $H        # Path.home() lever on Windows
  $env:HOME                = $H
  $env:CLAUDE_CONFIG_DIR   = $Cfg
  $env:LATCH_HOME          = $Sbx
  $env:CLAUDE_KB_HOME      = $Sbx
  $env:CLAUDE_COMMANDS_DIR = (Join-Path $Cfg 'commands')
  $env:LATCH_PYTHON        = $VenvPy
  $env:CLAUDE_KB_PYTHON    = $VenvPy
  $RegFile = Join-Path $Cfg '.claude.json'   # where claude writes the user-scope registry

  # --- safety gate ---------------------------------------------------------
  Write-Host "`n### safety gate: install_engine.ps1 --dry-run"
  $dry = & "$Sbx\bin\install_engine.ps1" --dry-run 2>&1 | Out-String
  Write-Host $dry
  if (($dry -replace '\\','/') -notlike "*$($Cfg -replace '\\','/')*") {
    throw "ABORT: --dry-run did not target the sandbox ($Cfg). Isolation failed; refusing to write."
  }
  Ok "dry-run targets the sandbox settings.json (isolation confirmed)"

  # --- INSTALL -------------------------------------------------------------
  Write-Host "`n### install_engine.ps1";   & "$Sbx\bin\install_engine.ps1" | Write-Host
  Write-Host "### install_commands.ps1";   & "$Sbx\bin\install_commands.ps1" | Select-Object -Last 2 | Write-Host

  Write-Host "`n### post-install assertions"
  $pyPost = @'
import json,sys
s=json.load(open(sys.argv[1])); allow=s.get("permissions",{}).get("allow",[]); hooks=s.get("hooks",{})
def chk(c,m): print(("  PASS: " if c else "  FAIL: ")+m)
chk(s.get("theme")=="dark","unrelated theme preserved")
chk("Bash(ls:*)" in allow and "mcp__other-server__do_thing" in allow,"unrelated perms preserved")
chk("mcp__latch" in allow,"server-level mcp__latch perm added")
chk("mcp__claude-kb" in allow,"legacy server-level mcp__claude-kb perm preserved")
ss=[h.get("command","") for g in hooks.get("SessionStart",[]) for h in g.get("hooks",[])]
chk(any("NOT-LATCH" in c for c in ss),"non-latch SessionStart hook preserved")
chk(any("/src/hooks/" in c for c in ss),"latch SessionStart hook added")
chk("PreToolUse" in hooks,"unrelated PreToolUse hook preserved")
for ev in ("UserPromptSubmit","Stop","SessionEnd"): chk(ev in hooks,"latch hook event "+ev+" added")
'@
  $r = $pyPost | & $VenvPy - (Join-Path $Cfg 'settings.json'); $r | Write-Host
  if ($r -match 'FAIL:') { $script:FAIL++ } else { $script:PASS++ }

  # Normalize separators before matching: install_engine forward-slashes the
  # registered server path, but $Sbx is a backslash Windows path, so a raw
  # -like never matches on Windows. (The .sh compares POSIX paths, which align.)
  $sbxFwd = $Sbx -replace '\\','/'
  if (((McpArgs $RegFile 'latch') -replace '\\','/') -like "*$sbxFwd*") { Ok "sandbox latch MCP registered -> sandbox repo" } else { Bad "sandbox latch MCP missing/wrong" }
  if (Get-ChildItem (Join-Path $Cfg 'commands') -Filter 'kb-*.md' -EA SilentlyContinue) { Ok "latch commands installed in sandbox" } else { Bad "latch commands not installed" }
  if ((McpFingerprint $RealDotClaude) -eq $realMcpBefore) { Ok "REAL registry unchanged by install (no leak)" } else { Bad "REAL registry changed by install" }

  # --- KILL SWITCH (all under sandbox KB_HOME) -----------------------------
  Write-Host "`n### kill switch: disable -> status -> enable"
  & "$Sbx\bin\latch_disable.ps1" | Write-Host
  if (Test-Path (Join-Path $Sbx 'DISABLE')) { Ok "latch_disable created DISABLE sentinel" } else { Bad "DISABLE not created" }
  # latch_status.ps1 reports via Write-Host (information stream #6); merge all
  # streams (*>&1) so Out-String actually captures the [DISABLED] line. (The .sh
  # status command echoes to stdout, which $(...) already captures.)
  $st = & "$Sbx\bin\latch_status.ps1" *>&1 | Out-String; Write-Host $st
  if ($st -match 'DISABLED') { Ok "latch_status reports DISABLED" } else { Bad "status did not report DISABLED" }
  & "$Sbx\bin\latch_enable.ps1" | Out-Null
  if (-not (Test-Path (Join-Path $Sbx 'DISABLE'))) { Ok "latch_enable cleared DISABLE" } else { Bad "DISABLE not cleared" }
  & "$Sbx\bin\latch_disable.ps1" -WriteOnly | Out-Null
  if (Test-Path (Join-Path $Sbx 'DISABLE_WRITE')) { Ok "--WriteOnly created DISABLE_WRITE (reads stay live)" } else { Bad "DISABLE_WRITE not created" }
  & "$Sbx\bin\latch_enable.ps1" -All | Out-Null
  if (-not (Test-Path (Join-Path $Sbx 'DISABLE')) -and -not (Test-Path (Join-Path $Sbx 'DISABLE_WRITE'))) { Ok "latch_enable -All cleared both sentinels" } else { Bad "enable -All left a sentinel" }
  if ((Test-Path (Join-Path $Repo 'DISABLE')) -eq $repoDisBefore -and (Test-Path (Join-Path $Repo 'DISABLE_WRITE')) -eq $repoDisWrBefore) { Ok "no kill-switch file leaked into the real repo" } else { Bad "kill-switch file LEAKED into real repo!" }

  # --- UNINSTALL -----------------------------------------------------------
  Write-Host "`n### uninstall.ps1 --yes"; & "$Sbx\bin\uninstall.ps1" --yes | Write-Host
  Write-Host "### uninstall.ps1 --check"; & "$Sbx\bin\uninstall.ps1" --check | Write-Host
  if ($LASTEXITCODE -eq 0) { Ok "uninstall --check clean (rc=0)" } else { Bad "uninstall --check rc=$LASTEXITCODE" }

  Write-Host "`n### post-uninstall assertions"
  $pyUn = @'
import json,sys
s=json.load(open(sys.argv[1])); allow=s.get("permissions",{}).get("allow",[]); hooks=s.get("hooks",{})
def chk(c,m): print(("  PASS: " if c else "  FAIL: ")+m)
chk(s.get("theme")=="dark","unrelated theme preserved")
chk("Bash(ls:*)" in allow and "mcp__other-server__do_thing" in allow,"unrelated perms preserved")
chk("mcp__latch" not in allow and not any(str(r).startswith("mcp__latch__") for r in allow),"primary latch perms stripped")
chk("mcp__claude-kb" not in allow and not any(str(r).startswith("mcp__claude-kb__") for r in allow),"legacy latch perms stripped")
ss=[h.get("command","") for g in hooks.get("SessionStart",[]) for h in g.get("hooks",[])]
chk(any("NOT-LATCH" in c for c in ss),"non-latch SessionStart hook preserved")
chk(not any("/src/hooks/" in c for c in ss),"latch SessionStart hook removed")
chk("PreToolUse" in hooks,"unrelated PreToolUse hook preserved")
for ev in ("UserPromptSubmit","Stop","SessionEnd"): chk(ev not in hooks,"latch hook event "+ev+" removed")
'@
  $r2 = $pyUn | & $VenvPy - (Join-Path $Cfg 'settings.json'); $r2 | Write-Host
  if ($r2 -match 'FAIL:') { $script:FAIL++ } else { $script:PASS++ }

  if (-not (McpPresent $RegFile 'latch') -and -not (McpPresent $RegFile 'claude-kb')) { Ok "sandbox MCP deregistered" } else { Bad "sandbox MCP still present" }
  if (Get-ChildItem (Join-Path $Cfg 'commands') -Filter 'kb-*.md' -EA SilentlyContinue) { Bad "latch commands NOT removed" } else { Ok "latch commands removed" }
  if (Test-Path (Join-Path $Cfg 'commands\mycustom.md')) { Ok "user command PRESERVED (not clobbered)" } else { Bad "user command removed!" }

  Write-Host "`n### REAL state verification"
  if ($realSetBefore  -eq (FileHash $RealSettings)) { Ok "REAL settings.json unchanged" } else { Bad "REAL settings.json CHANGED" }
  if ($realMcpBefore  -eq (McpFingerprint $RealDotClaude)) { Ok "REAL latch/legacy MCP registration unchanged" } else { Bad "REAL MCP registration CHANGED" }
  if ($realCmdsBefore -eq (CmdsHash $RealCmds))     { Ok "REAL ~/.claude/commands unchanged" } else { Bad "REAL commands dir CHANGED" }
  if ($realGitBefore  -eq (GitTracked))             { Ok "REAL repo tracked files unchanged" } else { Bad "REAL repo tracked files CHANGED" }
}
finally {
  foreach ($k in $save.Keys) { Set-Item "env:$k" -Value $save[$k] -ErrorAction SilentlyContinue }
  if ($SRoot -and (Test-Path $SRoot)) { Remove-Item -Recurse -Force $SRoot -ErrorAction SilentlyContinue }
}

Write-Host "`n### RESULT: $($script:PASS) passed, $($script:FAIL) failed"
if ($script:FAIL -eq 0) { Write-Host "### ALL GREEN - reversible on Windows; real config untouched." -ForegroundColor Green; exit 0 }
else { Write-Host "### SOME CHECKS FAILED - see above." -ForegroundColor Red; exit 1 }
