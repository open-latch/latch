<#
.SYNOPSIS
  One-command Latch bootstrap for Windows PowerShell and PowerShell 7.

.DESCRIPTION
  Installs a private uv/Python runtime in a stable per-user directory, then
  delegates all agent configuration to Latch's backup-preserving quickstart.
  Run from the project repo that should be wired:

    irm https://raw.githubusercontent.com/open-latch/latch/main/install.ps1 | iex

  Reruns reconcile the existing revision. Source changes require -Upgrade and
  are refused when the managed checkout is dirty. Production KB data is never
  deleted by this script.
#>
[CmdletBinding()]
param(
  [string]$InstallDir,
  [string]$Project = (Get-Location).Path,
  [string]$Ref = $(if ($env:LATCH_INSTALL_REF) { $env:LATCH_INSTALL_REF } else { "main" }),
  [switch]$Upgrade,
  [switch]$DryRun,
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$QuickstartArgs = @()
)

$ErrorActionPreference = "Stop"
$DefaultRepository = "https://github.com/open-latch/latch.git"
$Repository = if ($env:LATCH_INSTALL_REPOSITORY) {
  $env:LATCH_INSTALL_REPOSITORY
} else {
  $DefaultRepository
}
$UvInstallerUrl = if ($env:LATCH_UV_INSTALLER_URL) {
  $env:LATCH_UV_INSTALLER_URL
} else {
  "https://astral.sh/uv/0.11.28/install.ps1"
}

function Fail([string]$Message) {
  throw "latch install: $Message"
}

function Note([string]$Message) {
  Write-Host ""
  Write-Host "==> $Message"
}

function Normalize-Repository([string]$Value) {
  if ($Value.StartsWith("git@github.com:")) {
    $Value = "https://github.com/" + $Value.Substring("git@github.com:".Length)
  } elseif ($Value.StartsWith("ssh://git@github.com/")) {
    $Value = "https://github.com/" + $Value.Substring("ssh://git@github.com/".Length)
  }
  $Value = $Value.TrimEnd("/")
  if ($Value.EndsWith(".git")) {
    $Value = $Value.Substring(0, $Value.Length - 4)
  }
  return $Value
}

function Invoke-Git {
  param([string[]]$Arguments)
  $output = @(& git @Arguments 2>&1)
  if ($LASTEXITCODE -ne 0) {
    Fail("git $($Arguments -join ' ') failed: $($output -join [Environment]::NewLine)")
  }
  return $output
}

function Validate-Checkout([string]$App) {
  if ((Get-Item -LiteralPath $App -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    Fail("refusing symlink/reparse-point install directory: $App")
  }
  if (-not (Test-Path -LiteralPath (Join-Path $App ".git") -PathType Container)) {
    Fail("existing install path is not a Latch Git checkout: $App")
  }
  $top = (Invoke-Git -Arguments @("-C", $App, "rev-parse", "--show-toplevel") | Select-Object -Last 1).Trim()
  if ([IO.Path]::GetFullPath($top).TrimEnd("\") -ne [IO.Path]::GetFullPath($App).TrimEnd("\")) {
    Fail("install path is nested inside another Git checkout: $App")
  }
  $actual = (Invoke-Git -Arguments @("-C", $App, "remote", "get-url", "origin") | Select-Object -Last 1).Trim()
  if ((Normalize-Repository $actual) -ne (Normalize-Repository $Repository)) {
    Fail("existing checkout origin is $actual, expected $Repository; refusing overwrite")
  }
  foreach ($relative in @("VERSION", "requirements.txt", "src\quickstart.py")) {
    if (-not (Test-Path -LiteralPath (Join-Path $App $relative) -PathType Leaf)) {
      Fail("checkout is missing required Latch file: $relative")
    }
  }
}

if (-not $InstallDir) {
  $dataRoot = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "Latch"
  } elseif ($env:APPDATA) {
    Join-Path $env:APPDATA "Latch"
  } else {
    Join-Path $HOME "AppData\Local\Latch"
  }
  $InstallDir = Join-Path $dataRoot "app"
}
$Project = (Resolve-Path -LiteralPath $Project).Path
if (-not [IO.Path]::IsPathRooted($InstallDir)) {
  $InstallDir = Join-Path (Get-Location).Path $InstallDir
}
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$InstallParent = Split-Path -Parent $InstallDir
$UvDir = if ($env:LATCH_UV_DIR) { $env:LATCH_UV_DIR } else { Join-Path $InstallParent "bin" }

if ($DryRun) {
  Write-Host "Latch one-command bootstrap plan (no writes)"
  Write-Host "  repository : $Repository"
  Write-Host "  ref        : $Ref"
  Write-Host "  install dir: $InstallDir"
  Write-Host "  project    : $Project"
  $mode = if (Test-Path -LiteralPath (Join-Path $InstallDir ".git")) {
    if ($Upgrade) { "explicit upgrade" } else { "keep current revision and reconcile" }
  } else {
    "staged fresh checkout"
  }
  Write-Host "  source mode: $mode"
  Write-Host "  runtime    : private uv + Python 3.11 virtual environment"
  Write-Host "  activation : guided quickstart, checks, then consented initial-KB review"
  return
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  Fail("Git is required; install Git for Windows and rerun")
}

function Resolve-Uv {
  if ($env:LATCH_UV) {
    $command = Get-Command $env:LATCH_UV -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    if (Test-Path -LiteralPath $env:LATCH_UV -PathType Leaf) {
      return (Resolve-Path -LiteralPath $env:LATCH_UV).Path
    }
    Fail("LATCH_UV does not resolve to an executable: $($env:LATCH_UV)")
  }
  $command = Get-Command uv -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }
  $managed = Join-Path $UvDir "uv.exe"
  if (Test-Path -LiteralPath $managed -PathType Leaf) { return $managed }
  $userUv = Join-Path $HOME ".local\bin\uv.exe"
  if (Test-Path -LiteralPath $userUv -PathType Leaf) { return $userUv }

  New-Item -ItemType Directory -Path $UvDir -Force | Out-Null
  $installer = Join-Path ([IO.Path]::GetTempPath()) ("latch-uv-{0}.ps1" -f [guid]::NewGuid())
  try {
    Invoke-WebRequest -UseBasicParsing -Uri $UvInstallerUrl -OutFile $installer
    $previous = $env:UV_UNMANAGED_INSTALL
    $env:UV_UNMANAGED_INSTALL = $UvDir
    try {
      & ([scriptblock]::Create((Get-Content -LiteralPath $installer -Raw))) | Out-Host
    } finally {
      $env:UV_UNMANAGED_INSTALL = $previous
    }
  } finally {
    if (Test-Path -LiteralPath $installer) {
      Remove-Item -LiteralPath $installer -Force
    }
  }
  if (-not (Test-Path -LiteralPath $managed -PathType Leaf)) {
    Fail("uv installer did not create $managed")
  }
  return $managed
}

function Get-RuntimePython([string]$App) {
  foreach ($candidate in @(
    (Join-Path $App ".venv\Scripts\python.exe"),
    (Join-Path $App ".venv\bin\python")
  )) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
  }
  return $null
}

function Prepare-Runtime([string]$App, [string]$Uv) {
  $python = Get-RuntimePython $App
  $previousNoConfig = $env:UV_NO_CONFIG
  $env:UV_NO_CONFIG = "1"
  try {
    if (-not $python) {
      & $Uv venv --python 3.11 (Join-Path $App ".venv") | Out-Host
      if ($LASTEXITCODE -ne 0) { return $false }
      $python = Get-RuntimePython $App
    }
    if (-not $python) { return $false }
    & $python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -ne 0) {
      Fail("existing Latch virtual environment is older than Python 3.11: $python")
    }
    $locked = Join-Path $App "requirements.lock"
    if (Test-Path -LiteralPath $locked -PathType Leaf) {
      & $Uv pip install --python $python --require-hashes -r $locked | Out-Host
    } else {
      # Compatibility for pre-runtime-lock releases.
      & $Uv pip install --python $python -r (Join-Path $App "requirements.txt") | Out-Host
    }
    return ($LASTEXITCODE -eq 0)
  } finally {
    $env:UV_NO_CONFIG = $previousNoConfig
  }
}

function Checkout-Source([string]$Target) {
  New-Item -ItemType Directory -Path $Target -Force | Out-Null
  Invoke-Git -Arguments @("-C", $Target, "init", "--quiet") | Out-Null
  Invoke-Git -Arguments @("-C", $Target, "remote", "add", "origin", $Repository) | Out-Null
  Invoke-Git -Arguments @("-C", $Target, "fetch", "--quiet", "--depth", "1", "origin", $Ref) | Out-Null
  Invoke-Git -Arguments @("-C", $Target, "checkout", "--quiet", "--detach", "FETCH_HEAD") | Out-Null
  Validate-Checkout $Target
}

New-Item -ItemType Directory -Path $InstallParent -Force | Out-Null
$Uv = Resolve-Uv
$stageRoot = $null
try {
  if (Test-Path -LiteralPath $InstallDir) {
    Validate-Checkout $InstallDir
    if ($Upgrade) {
      $dirty = @(Invoke-Git -Arguments @("-C", $InstallDir, "status", "--porcelain", "--untracked-files=normal"))
      if ($dirty.Count -gt 0) {
        Fail("upgrade refused because the install checkout is dirty; preserve or remove local changes first")
      }
      $oldCommit = (Invoke-Git -Arguments @("-C", $InstallDir, "rev-parse", "HEAD") | Select-Object -Last 1).Trim()
      Note "Fetching explicit Latch upgrade ref $Ref"
      Invoke-Git -Arguments @("-C", $InstallDir, "fetch", "--quiet", "--depth", "1", "origin", $Ref) | Out-Null
      $newCommit = (Invoke-Git -Arguments @("-C", $InstallDir, "rev-parse", "FETCH_HEAD") | Select-Object -Last 1).Trim()
      Invoke-Git -Arguments @("-C", $InstallDir, "checkout", "--quiet", "--detach", $newCommit) | Out-Null
      if (-not (Prepare-Runtime $InstallDir $Uv)) {
        Write-Warning "Runtime setup failed after source update; restoring $oldCommit."
        Invoke-Git -Arguments @("-C", $InstallDir, "checkout", "--quiet", "--detach", $oldCommit) | Out-Null
        [void](Prepare-Runtime $InstallDir $Uv)
        Fail("upgrade rolled back; the previous checkout remains installed")
      }
    } else {
      Note "Existing Latch checkout found; keeping its source revision"
      if (-not (Prepare-Runtime $InstallDir $Uv)) {
        Fail("runtime reconciliation failed; the checkout remains at $InstallDir")
      }
    }
  } else {
    $stageRoot = Join-Path $InstallParent (".latch-install-{0}" -f [guid]::NewGuid())
    Note "Fetching Latch $Ref into a staging checkout"
    Checkout-Source (Join-Path $stageRoot "app")
    if (Test-Path -LiteralPath $InstallDir) {
      Fail("install path appeared during bootstrap; refusing to merge into it: $InstallDir")
    }
    Move-Item -LiteralPath (Join-Path $stageRoot "app") -Destination $InstallDir
    Remove-Item -LiteralPath $stageRoot -Force
    $stageRoot = $null
    if (-not (Prepare-Runtime $InstallDir $Uv)) {
      Fail("runtime setup failed; the verified source checkout remains at $InstallDir for a safe rerun")
    }
  }
} finally {
  if ($stageRoot -and (Test-Path -LiteralPath $stageRoot)) {
    Remove-Item -LiteralPath $stageRoot -Recurse -Force
  }
}

$PythonPath = Get-RuntimePython $InstallDir
if (-not $PythonPath) { Fail("Latch runtime is missing after setup: $InstallDir\.venv") }
$Commit = (Invoke-Git -Arguments @("-C", $InstallDir, "rev-parse", "--short=12", "HEAD") | Select-Object -Last 1).Trim()
$Version = (Get-Content -LiteralPath (Join-Path $InstallDir "VERSION") -Raw).Trim()

Note "Running the guided Latch activation"
$env:LATCH_HOME = $InstallDir
$env:LATCH_PYTHON = $PythonPath
& $PythonPath (Join-Path $InstallDir "src\quickstart.py") --project $Project @QuickstartArgs
$quickstartRc = $LASTEXITCODE
if ($quickstartRc -ne 0) {
  [Console]::Error.WriteLine(
    "Latch app/runtime installation succeeded, but activation stopped with status $quickstartRc. " +
    "No app files were removed; fix the reported preflight/check and rerun."
  )
  exit $quickstartRc
}

Write-Host ""
Write-Host "Latch activation complete."
Write-Host "  version : $Version ($Commit)"
Write-Host "  app     : $InstallDir"
Write-Host "  project : $Project"
Write-Host "  unwire  : $(Join-Path $InstallDir 'bin\uninstall.ps1')"
Write-Host "The unwire command preserves the production KB and the app checkout."
