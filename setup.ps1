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

function Test-SamePath([string]$Left, [string]$Right) {
    if (-not $Left -or -not $Right) { return $false }
    return [string]::Equals(
        [IO.Path]::GetFullPath($Left).TrimEnd("\"),
        [IO.Path]::GetFullPath($Right).TrimEnd("\"),
        [StringComparison]::OrdinalIgnoreCase
    )
}

try {
    $RepoRoot = Split-Path -Parent $PSCommandPath
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot "pyproject.toml") -PathType Leaf)) {
        Fail "locate-repo" "no pyproject.toml in $RepoRoot" "Run setup.ps1 from a cloned OpenAgent repository."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot "src\openagent") -PathType Container)) {
        Fail "locate-repo" "no src\openagent in $RepoRoot" "This does not look like the OpenAgent repository."
    }
    $versionSource = Get-Content -LiteralPath (Join-Path $RepoRoot "src\openagent\__init__.py") -Raw
    $versionMatch = [regex]::Match($versionSource, '(?m)^__version__\s*=\s*["'']([^"'']+)["'']\s*$')
    if (-not $versionMatch.Success) {
        Fail "locate-repo" "could not read the source OpenAgent version" "Restore src\openagent\__init__.py from the official repository."
    }
    $ExpectedVersion = $versionMatch.Groups[1].Value
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
    # Remove every existing copy, then prepend exactly once. Merely detecting ToolBin later in the
    # user PATH would leave an older OpenAgent first and make the installer update the wrong binary.
    $parts = @($userPath -split ";" | Where-Object {
        $_ -and -not [string]::Equals(
            $_.TrimEnd("\"), $ToolBin.TrimEnd("\"), [StringComparison]::OrdinalIgnoreCase
        )
    })
    [Environment]::SetEnvironmentVariable("Path", (@($ToolBin) + $parts) -join ";", "User")
    $persisted = [Environment]::GetEnvironmentVariable("Path", "User")
    $verified = @($persisted -split ";" | Where-Object { $_ }) | Where-Object {
        [string]::Equals($_.TrimEnd("\"), $ToolBin.TrimEnd("\"), [StringComparison]::OrdinalIgnoreCase)
    }
    if (-not $verified) {
        Fail "path" "the tool directory was not persisted to the user PATH" "Check user-registry access, then re-run setup.ps1."
    }
    $env:Path = "$ToolBin;$env:Path"

    Write-Step "[5/6] Verifying the installed entrypoint and machine-readable doctor output"
    $installedVersion = (& $OpenAgent version 2>$null | Out-String).Trim()
    $versionExit = $LASTEXITCODE
    if ($versionExit -ne 0) {
        Fail "verify" "openagent version failed" "Re-run setup.ps1 and report the output above."
    }
    if ($installedVersion -cne "openagent $ExpectedVersion") {
        Fail "verify-version" "installed binary reported '$installedVersion'; expected 'openagent $ExpectedVersion'" "The uv tool environment did not receive this checkout; re-run setup.ps1."
    }
    Write-Step "      version: $ExpectedVersion"

    $currentCommand = Get-Command openagent -ErrorAction SilentlyContinue
    if ($null -eq $currentCommand -or -not (Test-SamePath $currentCommand.Source $OpenAgent)) {
        $actual = if ($null -eq $currentCommand) { "not found" } else { $currentCommand.Source }
        Fail "verify-path" "PATH resolves '$actual' instead of '$OpenAgent'" "Remove or move the shadowing OpenAgent binary, then re-run setup.ps1."
    }
    Write-Step "      PATH: $($currentCommand.Source)"

    $doctorPath = Join-Path ([IO.Path]::GetTempPath()) ("openagent-doctor-" + [guid]::NewGuid() + ".json")
    try {
        & $OpenAgent doctor --json | Set-Content -LiteralPath $doctorPath -Encoding UTF8
        $doctorExit = $LASTEXITCODE
        try {
            $doctor = Get-Content -LiteralPath $doctorPath -Raw | ConvertFrom-Json
        } catch {
            Fail "verify-doctor" "doctor did not emit valid JSON" "Run '$OpenAgent doctor --json' and inspect the database diagnostic."
        }
        if ($null -eq $doctor.exit_code -or [int]$doctor.exit_code -ne $doctorExit) {
            Fail "verify-doctor" "doctor JSON exit_code does not match process exit $doctorExit" "Run '$OpenAgent doctor --json' and inspect the database diagnostic."
        }
        $backupPath = $null
        foreach ($check in @($doctor.checks)) {
            if ($null -ne $check.data -and $check.data.backup_path) {
                $backupPath = [string]$check.data.backup_path
                break
            }
        }
        if ($backupPath) { Write-Step "      database backup: $backupPath" }
        switch ($doctorExit) {
            0 { Write-Step "      doctor: ok" }
            1 { Write-Step "      doctor: warnings only (optional CLI/tool readiness may be incomplete)" }
            2 { Fail "verify-database" "Doctor found an incompatible, corrupt, or invalid OpenAgent database" "Preserve the backup shown above and run '$OpenAgent doctor --json'." }
            3 { Fail "verify-migration" "Doctor reports a failed or interrupted database migration" "Preserve the backup shown above; TUI launch has been blocked." }
            default { Fail "verify-doctor" "Doctor exited with unsupported code $doctorExit" "Run '$OpenAgent doctor --json'." }
        }
    } finally {
        Remove-Item -LiteralPath $doctorPath -Force -ErrorAction SilentlyContinue
    }

    # Prove fresh CMD and PowerShell processes resolve only from persisted Machine + User PATH.
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $freshPath = (@($machinePath, $persisted) | Where-Object { $_ }) -join ";"
    $oldPath = $env:Path
    try {
        $env:Path = $freshPath
        $env:OA_OPENAGENT_BIN = $OpenAgent
        $env:OA_EXPECTED_VERSION = $ExpectedVersion
        & powershell.exe -NoProfile -Command '$expected=[IO.Path]::GetFullPath($env:OA_OPENAGENT_BIN).TrimEnd("\"); $c=Get-Command openagent -ErrorAction SilentlyContinue; if($null -eq $c -or -not [string]::Equals([IO.Path]::GetFullPath($c.Source).TrimEnd("\"),$expected,[StringComparison]::OrdinalIgnoreCase)){exit 1}; $v=(& openagent version | Out-String).Trim(); if($LASTEXITCODE -ne 0 -or $v -cne ("openagent "+$env:OA_EXPECTED_VERSION)){exit 1}'
        if ($LASTEXITCODE -ne 0) { throw "fresh PowerShell resolved a different OpenAgent" }
        $cmdResolved = (& cmd.exe /d /c "where openagent" | Select-Object -First 1).Trim()
        if ($LASTEXITCODE -ne 0 -or -not (Test-SamePath $cmdResolved $OpenAgent)) {
            throw "fresh CMD PATH resolves a different openagent"
        }
        $cmdVersion = (& cmd.exe /d /c "openagent version" | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or $cmdVersion -cne "openagent $ExpectedVersion") {
            throw "fresh CMD did not run the installed OpenAgent version"
        }
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
