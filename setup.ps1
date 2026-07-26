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

function Invoke-Native {
    <#
    .SYNOPSIS
    Run a native command, letting it write to stderr, and return its exit code.

    .DESCRIPTION
    Windows PowerShell 5.1 turns a native command's stderr output into ErrorRecords, and this
    script sets $ErrorActionPreference = "Stop" — so an ordinary progress line aborts the
    installer. uv writes both "Downloading cpython-3.12.13…" and "Python 3.12 is already
    installed" to stderr, which meant the installer could fail either while doing its job or
    while discovering it had nothing to do.

    Suppressing stderr is the wrong fix: real errors live there too. The preference is instead
    narrowed to this one call, the output is shown, and the *exit code* decides success — which is
    what it was always supposed to decide.
    #>
    param([Parameter(Mandatory = $true)][scriptblock]$Command)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command 2>&1 | ForEach-Object { Write-Host $_ }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
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
    $code = Invoke-Native { & $Uv python install 3.12 }
    if ($code -ne 0) {
        Fail "install-python" "uv could not install managed Python 3.12" "Check network/proxy access, then re-run setup.ps1."
    }

    # By default install an exact official commit from the remote so the binary's provenance is a
    # pinned VCS commit and `openagent update` is checkout-independent afterwards. Set
    # OPENAGENT_SETUP_LOCAL=1 to install from this working tree for development instead (spec §20).
    $OfficialRemote = "https://github.com/yasirkaramandev/openagent.git"
    $LocalDev = ($env:OPENAGENT_SETUP_LOCAL -eq "1")
    $InstallChannel = if ($ExpectedVersion -match "(rc|a\d|b\d|dev)") { "candidate" } else { "stable" }
    if ($env:OPENAGENT_SETUP_CHANNEL) {
        if ($env:OPENAGENT_SETUP_CHANNEL -notin @("stable", "candidate", "dev")) {
            Fail "install-openagent" "unknown OPENAGENT_SETUP_CHANNEL '$($env:OPENAGENT_SETUP_CHANNEL)'" "Choose stable, candidate, or dev."
        }
        $InstallChannel = $env:OPENAGENT_SETUP_CHANNEL
    }
    $InstallCommit = ""
    $ChannelSourceRef = $null
    if ($LocalDev) {
        Write-Step "[3/6] Installing OpenAgent from this checkout (local-development mode)"
        $InstallSource = $RepoRoot
        $InstallSourceKind = "dev-local"
        try { $InstallCommit = (& git -C $RepoRoot rev-parse HEAD 2>$null).Trim() } catch { $InstallCommit = "" }
    } else {
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            Fail "install-openagent" "git is required for an official install" "Install git, or set OPENAGENT_SETUP_LOCAL=1 to install from this checkout."
        }
        $InstallCommit = (& git -C $RepoRoot rev-parse HEAD 2>$null)
        if (-not $InstallCommit) {
            Fail "install-openagent" "this directory is not a git checkout" "Set OPENAGENT_SETUP_LOCAL=1 to install from this directory instead."
        }
        $InstallCommit = $InstallCommit.Trim()
        # Fail closed unless this exact commit is on the *channel being installed* (spec §3.5).
        # Reachable-from-some-official-ref is not enough: GitHub serves every commit on every
        # branch, so the old test accepted any feature branch. stable = a published non-prerelease
        # tag; candidate = the release-candidate branch; dev = main. Anything else is a development
        # install and must say so with OPENAGENT_SETUP_LOCAL=1.
        if ($InstallChannel -eq "stable") {
            $TagMatch = $null
            $Tags = & git ls-remote --tags $OfficialRemote 2>$null
            foreach ($line in @($Tags)) {
                $parts = $line -split "\s+"
                if ($parts.Count -ge 2 -and $parts[0] -eq $InstallCommit) {
                    $name = $parts[1] -replace "\^\{\}$", "" -replace "^refs/tags/", ""
                    if ($name -notmatch "(rc|a\d|b\d|dev)") { $TagMatch = $name; break }
                }
            }
            if (-not $TagMatch) {
                Fail "install-openagent" "commit $InstallCommit is not a published stable release of the official repository" "Check out a release tag, use OPENAGENT_SETUP_CHANNEL=candidate or dev, or set OPENAGENT_SETUP_LOCAL=1 for a local install."
            }
            $ChannelSourceRef = "refs/tags/$TagMatch"
        } else {
            $ChannelSourceRef = if ($InstallChannel -eq "candidate") { "refs/heads/release-candidate" } else { "refs/heads/main" }
            & git -C $RepoRoot fetch -q $OfficialRemote $ChannelSourceRef 2>$null
            if ($LASTEXITCODE -ne 0) {
                Fail "install-openagent" "could not read $ChannelSourceRef from the official repository" "Check your network / proxy, then re-run."
            }
            $ChannelTip = (& git -C $RepoRoot rev-parse FETCH_HEAD 2>$null)
            if (-not $ChannelTip) {
                Fail "install-openagent" "could not resolve the tip of $ChannelSourceRef" "Re-run setup.ps1."
            }
            & git -C $RepoRoot merge-base --is-ancestor $InstallCommit $ChannelTip.Trim() 2>$null
            if ($LASTEXITCODE -ne 0) {
                Fail "install-openagent" "commit $InstallCommit is not on the $InstallChannel channel ($ChannelSourceRef)" "Feature-branch commits are not an install source. Check out a channel commit, or set OPENAGENT_SETUP_LOCAL=1 for a local install."
            }
        }
        Write-Step "[3/6] Installing OpenAgent from official commit $InstallCommit ($InstallChannel channel, $ChannelSourceRef)"
        $InstallSource = "git+$OfficialRemote@$InstallCommit"
        $InstallSourceKind = "official-github-vcs"
    }
    # Same stderr hazard: uv emits warnings here too ("Failed to hardlink files; falling back to
    # full copy" is routine on a CI runner and must not abort an otherwise good install).
    $code = Invoke-Native { & $Uv tool install --force --python 3.12 $InstallSource }
    if ($code -ne 0) {
        Fail "install-openagent" "uv tool install failed for $InstallSource" "Check the dependency error above, then re-run setup.ps1."
    }
    $ToolBin = (& $Uv tool dir --bin | Select-Object -First 1).Trim()
    if (-not $ToolBin) {
        Fail "install-openagent" "could not read uv's tool bin directory" "Re-run setup.ps1."
    }
    $OpenAgent = Join-Path $ToolBin "openagent.exe"
    if (-not (Test-Path -LiteralPath $OpenAgent -PathType Leaf)) {
        Fail "install-openagent" "openagent.exe is missing from $ToolBin" "The tool install may have been interrupted; re-run setup.ps1."
    }

    # Persist install provenance (spec §8, §20.1) so `openagent update` is channel-aware immediately.
    try {
        $OaHome = if ($env:OPENAGENT_HOME) { $env:OPENAGENT_HOME } else { Join-Path $HOME ".openagent" }
        New-Item -ItemType Directory -Force -Path $OaHome | Out-Null
        $meta = [ordered]@{
            schema_version        = 1
            manager               = "uv-tool"
            source                = $InstallSourceKind
            repository            = "yasirkaramandev/openagent"
            channel               = $InstallChannel
            channel_ref           = switch ($InstallChannel) { "candidate" { "release-candidate" } "dev" { "main" } default { $null } }
            installed_version     = $ExpectedVersion
            installed_commit      = if ($InstallCommit) { $InstallCommit } else { $null }
            last_accepted_version = $ExpectedVersion
            last_accepted_commit  = if ($InstallCommit) { $InstallCommit } else { $null }
            python                = "3.12"
            updated_at            = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
        }
        $metaPath = Join-Path $OaHome "install.json"
        ($meta | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $metaPath -Encoding utf8
        Write-Step "      recorded install provenance in $metaPath"
    } catch {
        Write-Warning "could not record install provenance (non-fatal)"
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
            # Doctor check payloads are heterogeneous. Under StrictMode, reading an absent dynamic
            # JSON property throws instead of yielding $null, so inspect the property bag first.
            $dataProperty = $check.PSObject.Properties["data"]
            if ($null -eq $dataProperty -or $null -eq $dataProperty.Value) { continue }
            $backupProperty = $dataProperty.Value.PSObject.Properties["backup_path"]
            if ($null -ne $backupProperty -and $backupProperty.Value) {
                $backupPath = [string]$backupProperty.Value
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
        $psMatches = @(& powershell.exe -NoProfile -Command '(Get-Command openagent -ErrorAction Stop).Source')
        $psResolveExit = $LASTEXITCODE
        $psResolved = [string]($psMatches | Select-Object -First 1)
        if ($psResolveExit -ne 0 -or -not (Test-SamePath $psResolved.Trim() $OpenAgent)) {
            throw "fresh PowerShell PATH resolves '$($psResolved.Trim())' instead of '$OpenAgent' (exit $psResolveExit)"
        }
        $psVersion = (& powershell.exe -NoProfile -Command 'openagent version' | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or $psVersion -cne "openagent $ExpectedVersion") {
            throw "fresh PowerShell did not run the installed OpenAgent version"
        }
        $cmdMatches = @(& cmd.exe /d /c "where openagent")
        $cmdWhereExit = $LASTEXITCODE
        $cmdResolved = [string]($cmdMatches | Select-Object -First 1)
        if ($cmdWhereExit -ne 0 -or -not (Test-SamePath $cmdResolved.Trim() $OpenAgent)) {
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
