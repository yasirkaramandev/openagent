# OpenAgent installer for Windows PowerShell 5.1+ / PowerShell 7.
# No administrator rights or pre-installed Python are required. Re-running upgrades the isolated
# uv tool and preserves every OpenAgent database, credential reference, project marker, and run.

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$UvVersion = "0.11.28"

function Write-Step([string]$Message) {
    Write-Host "[openagent-setup] $Message"
}

function Fail([string]$Stage, [string]$What, [string]$Fix) {
    [Console]::Error.WriteLine("")
    [Console]::Error.WriteLine("[openagent-setup] ERROR")
    [Console]::Error.WriteLine("[openagent-setup]   stage : $Stage")
    [Console]::Error.WriteLine("[openagent-setup]   what  : $What")
    [Console]::Error.WriteLine("[openagent-setup]   fix   : $Fix")
    exit 1
}

function Find-Uv {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $command) { return $command.Source }
    foreach ($candidate in @(
        (Join-Path $HOME ".local\bin\uv.exe"),
        (Join-Path $HOME ".cargo\bin\uv.exe")
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

try {
    $RepoRoot = Split-Path -Parent $PSCommandPath
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot "pyproject.toml") -PathType Leaf)) {
        Fail "locate-repo" "no pyproject.toml in $RepoRoot" "Run setup.ps1 from a cloned OpenAgent repository."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot "src\openagent") -PathType Container)) {
        Fail "locate-repo" "no src\openagent in $RepoRoot" "This does not look like the OpenAgent repository."
    }
    Write-Step "Installing OpenAgent from: $RepoRoot"

    $existing = Get-Command openagent -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        Write-Step "note: an 'openagent' command is already on PATH at $($existing.Source)"
    }
    if ($null -eq (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Step "warning: Git is not installed; git-worktree runs will fall back to isolated copies"
    }
    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue) -and
        $null -eq (Get-Command podman -ErrorAction SilentlyContinue)) {
        Write-Step "note: Docker/Podman is absent; optional container-sandbox runs will be unavailable"
    }

    $Uv = Find-Uv
    if ($null -eq $Uv) {
        Write-Step "[1/6] Installing uv $UvVersion (Astral standalone installer over HTTPS)"
        $installer = Invoke-RestMethod -Uri "https://astral.sh/uv/$UvVersion/install.ps1"
        Invoke-Expression $installer
        $Uv = Find-Uv
    } else {
        Write-Step "[1/6] Using existing uv: $Uv"
    }
    if ($null -eq $Uv) {
        Fail "install-uv" "uv was installed but uv.exe could not be located" "Open a new terminal and re-run setup.ps1."
    }

    Write-Step "[2/6] Installing managed Python 3.12 (system/Store Python is untouched)"
    & $Uv python install 3.12
    if ($LASTEXITCODE -ne 0) {
        Fail "install-python" "uv could not install managed Python 3.12" "Check network/proxy access, then re-run setup.ps1."
    }

    Write-Step "[3/6] Installing or upgrading OpenAgent in an isolated uv tool environment"
    & $Uv tool install --force --python 3.12 $RepoRoot
    if ($LASTEXITCODE -ne 0) {
        Fail "install-openagent" "uv tool install failed for $RepoRoot" "Check the dependency error above, then re-run setup.ps1."
    }
    $ToolBin = (& $Uv tool dir --bin | Select-Object -First 1).Trim()
    if (-not $ToolBin) {
        Fail "install-openagent" "could not read uv's tool bin directory" "Re-run setup.ps1."
    }
    $OpenAgent = Join-Path $ToolBin "openagent.exe"
    if (-not (Test-Path -LiteralPath $OpenAgent -PathType Leaf)) {
        Fail "install-openagent" "openagent.exe is missing from $ToolBin" "The tool install may have been interrupted; re-run setup.ps1."
    }

    Write-Step "[4/6] Persisting the tool directory on the user PATH"
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($userPath -split ";" | Where-Object { $_ })
    $present = $parts | Where-Object {
        [string]::Equals($_.TrimEnd("\"), $ToolBin.TrimEnd("\"), [StringComparison]::OrdinalIgnoreCase)
    }
    if (-not $present) {
        [Environment]::SetEnvironmentVariable("Path", (@($ToolBin) + $parts) -join ";", "User")
    }
    $persisted = [Environment]::GetEnvironmentVariable("Path", "User")
    $verified = @($persisted -split ";" | Where-Object { $_ }) | Where-Object {
        [string]::Equals($_.TrimEnd("\"), $ToolBin.TrimEnd("\"), [StringComparison]::OrdinalIgnoreCase)
    }
    if (-not $verified) {
        Fail "path" "the tool directory was not persisted to the user PATH" "Check user-registry access, then re-run setup.ps1."
    }
    $env:Path = "$ToolBin;$env:Path"

    Write-Step "[5/6] Verifying the installed entrypoint and machine-readable doctor output"
    & $OpenAgent version
    if ($LASTEXITCODE -ne 0) {
        Fail "verify" "openagent version failed" "Re-run setup.ps1 and report the output above."
    }
    $doctorPath = Join-Path ([IO.Path]::GetTempPath()) ("openagent-doctor-" + [guid]::NewGuid() + ".json")
    try {
        & $OpenAgent doctor --json | Set-Content -LiteralPath $doctorPath -Encoding UTF8
        Get-Content -LiteralPath $doctorPath -Raw | ConvertFrom-Json | Out-Null
    } finally {
        Remove-Item -LiteralPath $doctorPath -Force -ErrorAction SilentlyContinue
    }

    # Prove fresh CMD and PowerShell processes resolve only from persisted Machine + User PATH.
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $freshPath = (@($machinePath, $persisted) | Where-Object { $_ }) -join ";"
    $oldPath = $env:Path
    try {
        $env:Path = $freshPath
        & cmd.exe /d /c "openagent version"
        if ($LASTEXITCODE -ne 0) { throw "fresh CMD could not resolve openagent" }
        & powershell.exe -NoProfile -Command "openagent version"
        if ($LASTEXITCODE -ne 0) { throw "fresh PowerShell could not resolve openagent" }
    } finally {
        $env:Path = $oldPath
    }

    if ($env:OPENAGENT_SETUP_NO_LAUNCH -eq "1") {
        Write-Step "[6/6] OPENAGENT_SETUP_NO_LAUNCH=1; install verified, TUI launch skipped"
        exit 0
    }
    Write-Step "[6/6] Starting OpenAgent"
    & $OpenAgent
    exit $LASTEXITCODE
} catch {
    Fail "unexpected" $_.Exception.Message "Fix the reported condition and re-run setup.ps1."
}
