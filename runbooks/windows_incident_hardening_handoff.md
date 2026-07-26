# Windows incident-hardening verification handoff

This handoff closes the one remaining item from the July 16 KB incident work:
physical Windows verification of the windowless MCP launcher and daemon process
lineage. It also reruns the deterministic KB deletion and backup safeguards on
Windows.

## Authority and exact source

- Repository: `https://github.com/open-latch/latch.git`
- Branch: `agent/kb-incident-hardening`
- Reviewed runtime baseline: `c104c7f1da0359f200cf0d45f3940bcbe7a5934c`

The branch may contain later handoff-documentation and generated-proof commits.
Before testing, verify that the reviewed runtime baseline is an ancestor and
that every file changed after it is one of those explicitly allowed artifacts:

```powershell
git fetch origin
git switch --force-create agent/kb-incident-hardening `
  --track origin/agent/kb-incident-hardening
git status --short --branch
git rev-parse HEAD
$SafetyCommit = "c104c7f1da0359f200cf0d45f3940bcbe7a5934c"
git merge-base --is-ancestor `
  $SafetyCommit HEAD
if ($LASTEXITCODE -ne 0) { throw "Reviewed runtime baseline is not an ancestor" }
$ChangedAfterSafety = @(git diff --name-only "$SafetyCommit..HEAD")
$UnexpectedAfterSafety = @(
  $ChangedAfterSafety | Where-Object {
    $_ -ne "runbooks/windows_incident_hardening_handoff.md" -and
    $_ -ne "runbooks/kb_durability.md" -and
    -not ($_.StartsWith("proof/"))
  }
)
if ($UnexpectedAfterSafety.Count -gt 0) {
  $UnexpectedAfterSafety | ForEach-Object { Write-Error "Unexpected post-baseline path: $_" }
  throw "Runtime or test source changed after the reviewed baseline"
}
$ChangedAfterSafety
```

Expected post-baseline diff: only this handoff file, its optional link from
`runbooks/kb_durability.md`, and generated files below `proof/`. The PowerShell
guard stops automatically if any runtime, test, workflow, or other unreviewed
path differs.

## Safety boundary

Run the deterministic test section only through `python -m pytest`. Pytest
creates an authenticated disposable vault and owns its cleanup. Never run a
test file directly, set `LATCH_KB_DIR` to a real KB for tests, delete a path
returned by `paths.project_dir()`, or use a production KB as a fixture.

For the live GUI reproduction, prefer Windows Sandbox, a VM, or a dedicated
Windows test account. If the symptom only occurs in an existing host install,
use read-only MCP calls and do not run maintenance, migration, seeding,
compaction, uninstall, or deletion commands during this handoff. Do not kill
all `python.exe` processes; unrelated Python applications may be running.

## 1. Prepare the checkout

Use PowerShell 7 or Windows PowerShell from a fresh clone:

```powershell
$LatchRoot = "C:\src\latch-kb-incident-hardening"
git clone https://github.com/open-latch/latch.git $LatchRoot
Set-Location $LatchRoot
git switch --track origin/agent/kb-incident-hardening

py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe --version
```

Do not continue if the branch/source checks above fail.

## 2. Run deterministic safeguards

These tests must not open or mutate the installed production KB:

```powershell
$Evidence = Join-Path $env:TEMP `
  ("latch-windows-evidence-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $Evidence | Out-Null

git status --short --branch |
  Set-Content (Join-Path $Evidence "git-status-before.txt")
git rev-parse HEAD |
  Set-Content (Join-Path $Evidence "git-head.txt")
Get-ComputerInfo |
  Select-Object WindowsProductName, WindowsVersion, OsBuildNumber, OsArchitecture |
  Format-List |
  Set-Content (Join-Path $Evidence "windows-version.txt")
.\.venv\Scripts\python.exe --version 2>&1 |
  Set-Content (Join-Path $Evidence "python-version.txt")

.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_vault_safety.py `
  tests/test_vault_backup.py `
  tests/test_destructive_path_guard.py `
  tests/test_schema_version.py `
  tests/test_selfheal.py `
  tests/test_install_engine.py `
  tests/test_mcp_lifecycle_contract.py 2>&1 |
  Tee-Object (Join-Path $Evidence "pytest-windows.txt")
if ($LASTEXITCODE -ne 0) { throw "Windows safeguard tests failed" }

git status --short --branch |
  Set-Content (Join-Path $Evidence "git-status-after.txt")
```

Pass criteria:

1. Pytest exits zero.
2. No test resolves or reports a production KB path.
3. The before/after Git status differs only by expected ignored caches, if any.
4. No Python console window appears during the pytest run.

## 3. Enable physical process attribution

The diagnostic variable must be visible to the GUI host, not only the current
PowerShell process. Set it at user scope, then fully exit the agent host:

```powershell
$LauncherLog = Join-Path $Evidence "mcp-launcher.log"
$ProcessLog = Join-Path $Evidence "python-processes.jsonl"
$env:LATCH_MCP_LAUNCHER_LOG = $LauncherLog
[Environment]::SetEnvironmentVariable(
  "LATCH_MCP_LAUNCHER_LOG", $LauncherLog, "User"
)
```

Use the branch's normal installer for only the host being tested. Run it from a
throwaway project in a sandbox/VM, or from the existing test project when the
symptom requires the real host configuration:

```powershell
# Claude Code engine, from the latch checkout:
.\bin\install_engine.ps1 --no-seed-prompt

# Codex, from the project that should receive AGENTS.md:
# & "$LatchRoot\bin\install_codex.ps1" --yes --no-seed-prompt

# Cursor, from the project being tested:
# & "$LatchRoot\bin\install_cursor.ps1" --yes --with-hooks
```

Run only the applicable installer. Do not seed or compact. Record the host and
version in `$Evidence\result.txt`. Fully exit the host after installation. For
a true cold start, reboot the sandbox/VM or sign out and back in. Do not force a
cold start by terminating every Python process.

## 4. Start the transient-process watcher

Open a second PowerShell window, set `$Evidence` to the same directory, and run
this watcher before opening Claude, Codex, or Cursor. It records each newly
observed Python process and whether Windows assigned it a top-level window:

```powershell
$ProcessLog = Join-Path $Evidence "python-processes.jsonl"
$Seen = @{}
$Until = (Get-Date).AddMinutes(5)

while ((Get-Date) -lt $Until) {
  Get-CimInstance Win32_Process `
    -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    ForEach-Object {
      $Key = "$($_.ProcessId):$($_.CreationDate)"
      $Gui = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
      $WindowHandle = if ($Gui) { [int64]$Gui.MainWindowHandle } else { 0 }
      $WindowTitle = if ($Gui) { $Gui.MainWindowTitle } else { "" }
      $WindowState = "$WindowHandle|$WindowTitle"
      if (-not $Seen.ContainsKey($Key) -or $Seen[$Key] -ne $WindowState) {
        [pscustomobject]@{
          ObservedAtUtc   = (Get-Date).ToUniversalTime().ToString("o")
          ProcessId       = $_.ProcessId
          ParentProcessId = $_.ParentProcessId
          Name            = $_.Name
          ExecutablePath  = $_.ExecutablePath
          CommandLine     = $_.CommandLine
          CreationDate    = $_.CreationDate
          MainWindowHandle = $WindowHandle
          MainWindowTitle  = $WindowTitle
        } | ConvertTo-Json -Compress |
          Add-Content -Path $ProcessLog -Encoding utf8
        $Seen[$Key] = $WindowState
      }
    }
  Start-Sleep -Milliseconds 100
}
```

If a window appears, immediately record its exact timestamp, title, and PID if
Task Manager exposes it. Take a screenshot. Then capture the full parent chain,
substituting the observed PID:

```powershell
$CurrentId = 12345
while ($CurrentId -gt 0) {
  $Row = Get-CimInstance Win32_Process -Filter "ProcessId=$CurrentId"
  if (-not $Row) { break }
  $Row | Select-Object ProcessId, ParentProcessId, Name, ExecutablePath,
    CommandLine, CreationDate | Format-List
  $CurrentId = [int]$Row.ParentProcessId
}
```

Do not diagnose from the window title alone.

## 5. Exercise cold and warm MCP starts

With the watcher running:

1. Start the selected host after the reboot/sign-in.
2. Open the test project and wait for latch MCP discovery.
3. Make one read-only call such as `latch_recent` or `latch_gate_report`.
4. Record whether a Python window appeared and whether the MCP call succeeded.
5. Fully exit and reopen the host three times without rebooting, making the same
   read-only call each time. These are warm shared-daemon/proxy starts.

Do not change TTL, warm-up timing, or process flags during this run.

## 6. Collect launcher and lifecycle evidence

The launcher log must contain the supervisor PID, parent PID, executable,
arguments, base executable, child executable, server path, creation flags, and
child PID/job assignment:

```powershell
Get-Content $LauncherLog -Tail 200 |
  Tee-Object (Join-Path $Evidence "mcp-launcher-tail.txt")
```

Resolve the active vault directory without opening the database, then copy only
the process lifecycle logs. Do not copy `kb.db`, transcripts, prompts, or
private project contents:

```powershell
$Vault = (& .\.venv\Scripts\python.exe -c `
  "import sys; sys.path.insert(0, 'src'); import paths; print(paths.project_dir())").Trim()
$Vault | Set-Content (Join-Path $Evidence "vault-path.txt")
Get-ChildItem $Vault -Filter "mcp_lifecycle-*.log" -ErrorAction SilentlyContinue |
  ForEach-Object {
    Copy-Item $_.FullName -Destination $Evidence
  }
Get-ChildItem $Vault -Filter "mcp_daemon*.log" -ErrorAction SilentlyContinue |
  ForEach-Object {
    Copy-Item $_.FullName -Destination $Evidence
  }
```

Review paths and command lines for private information before sharing. Then:

```powershell
Compress-Archive -Path (Join-Path $Evidence "*") `
  -DestinationPath "$Evidence.zip"
Write-Host "Evidence: $Evidence.zip"
```

Clear the temporary user-scoped diagnostic variable after the run:

```powershell
[Environment]::SetEnvironmentVariable(
  "LATCH_MCP_LAUNCHER_LOG", $null, "User"
)
Remove-Item Env:LATCH_MCP_LAUNCHER_LOG -ErrorAction SilentlyContinue
```

If this ran on a normal workstation rather than a disposable sandbox, rerun the
installer from the previously trusted stable latch checkout to restore that
host's MCP wiring. Do not delete the branch-created vault or backup directories
as cleanup; leave production-classified data for normal retention handling.

## Acceptance criteria

PASS requires all of the following:

- The exact safety implementation commit is present and no later runtime code
  differs.
- The Windows safeguard pytest tranche passes through pytest isolation.
- A cold MCP start and three warm restarts complete with successful read-only
  latch calls.
- No Latch-owned `python.exe` or `pythonw.exe` has a visible top-level window.
- Launcher evidence shows `pythonw.exe` supervising base `python.exe`, the child
  has `CREATE_NO_WINDOW`, and its PID is assigned to the kill-on-close job.
- The daemon lifecycle `daemon_spawned` event identifies the executable, argv,
  parent PID, child PID, and Windows flags including `CREATE_NO_WINDOW` and
  `CREATE_NEW_PROCESS_GROUP`, with `DETACHED_PROCESS` absent.

FAIL is any failed test, failed MCP call, missing attribution receipt, or visible
window belonging to a command line under this latch checkout. A visible window
from an unrelated Python application is not a Latch failure, but its parent
chain must prove that conclusion.

## Return this result

Send back the evidence zip plus this completed summary:

```text
Branch HEAD:
Windows edition/build/architecture:
Python version and path:
Host tested and version (Claude/Codex/Cursor):
Safeguard pytest result:
Cold start MCP call: PASS/FAIL
Cold start visible Python window: YES/NO
Warm restart 1: PASS/FAIL, window YES/NO
Warm restart 2: PASS/FAIL, window YES/NO
Warm restart 3: PASS/FAIL, window YES/NO
Launcher log present: YES/NO
Lifecycle log present: YES/NO
If a window appeared: timestamp, PID, title, command line, parent chain
Evidence zip path:
Notes:
```
