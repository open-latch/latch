# KB durability and test-safety runbook

## Invariants

- Every new vault gets one immutable SQLite identity: `test` only inside an
  authenticated pytest root, otherwise `production`.
- Every unidentified existing database is production. Missing or mismatched
  external registry metadata fails closed.
- Production vault deletion is not implemented. Uninstall and `--purge` retain
  production KBs and protected backups.
- The only recursive vault-deletion boundary accepts test vaults and requires
  the inherited capability, exact vault UUID, registry match, a non-symlink
  target, and realpath containment below the disposable test root.
- New production installs use the platform data directory outside the source
  checkout. An existing in-checkout pin is warned and never silently moved.

## Backups

`src/vault_backup.py` uses SQLite's online backup API and publishes a verified
database/manifest pair atomically in the independent durability root:

- macOS: `~/Library/Application Support/LatchBackups`
- Windows: `%LOCALAPPDATA%\LatchBackups`
- Linux: `$XDG_STATE_HOME/latch/backups` or `~/.local/state/latch/backups`

The first verified point each UTC day is protected for 30 days. Other cadence
points are protected for five days; self-heal attempts one every six hours.
Pruning has no force option, never removes the newest point, and deletes only an
expired pair whose vault UUID and SHA-256 match its readable manifest. Corrupt,
unknown, incomplete, or still-protected artifacts are retained.

Create and verify a snapshot:

```bash
.venv/bin/python src/vault_backup.py create
.venv/bin/python src/vault_backup.py verify-restore /absolute/path/to/snapshot.json
```

Run the safety proof:

```bash
.venv/bin/python -m pytest -q \
  tests/test_vault_safety.py \
  tests/test_vault_backup.py \
  tests/test_destructive_path_guard.py \
  tests/test_schema_version.py \
  tests/test_selfheal.py
```

Never use a direct test script as a substitute for pytest.

## Windows Python-window attribution

Claude, Codex, and Cursor use `pythonw.exe` plus `mcp_launcher_win.py` when the
complete managed launcher pair exists. The supervisor starts base `python.exe`
with `CREATE_NO_WINDOW`; daemon cold-start helpers additionally use hidden,
detached, no-window flags and bypass the venv redirector.

Before reproducing a visible Python window, enable the launcher receipt and
restart the host:

```powershell
$env:LATCH_MCP_LAUNCHER_LOG = "$env:TEMP\latch-mcp-launcher.log"
```

During reproduction, capture the visible process and its parent/command line:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match '^python(w)?\.exe$' } |
  Select-Object ProcessId, ParentProcessId, Name, ExecutablePath, CommandLine
Get-Content $env:LATCH_MCP_LAUNCHER_LOG -Tail 100
```

Correlate PID, parent PID, executable, argv, and timestamp with the launcher log
and the vault's `mcp_lifecycle-*.log`. Do not change another launch path based
only on the window title. The physical Windows reproduction is the authority
for whether any popup remains.
