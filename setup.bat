@echo off
rem OpenAgent installer for Windows 10/11 (spec §1, §3-§5).
rem Run it from CMD:            setup.bat
rem ...or from PowerShell:      .\setup.bat
rem
rem Like setup.sh, this uses uv as the engine: it needs no pre-existing Python, installs a *managed*
rem Python 3.12 that belongs to uv (your system Python / Windows Store Python / py.exe are untouched),
rem installs OpenAgent from THIS repository into an isolated uv tool environment, and puts the
rem `openagent` command on your user PATH. Re-running UPDATES an existing install. It never installs
rem into system Python, never needs administrator rights, and never deletes your OpenAgent data.
rem
rem CI / automation: set OPENAGENT_SETUP_NO_LAUNCH=1 to verify without opening the TUI.

setlocal EnableExtensions EnableDelayedExpansion

rem A verified, pinned uv version for reproducible bootstraps.
set "UV_VERSION=0.11.28"

rem --------------------------------------------------------------------- 0. repository root
rem %~dp0 is this script's directory WITH a trailing backslash; strip it, keep spaces intact.
set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"

if not exist "%REPO_ROOT%\pyproject.toml" (
    call :die "locate-repo" "no pyproject.toml in %REPO_ROOT%" "Run setup.bat from inside a cloned OpenAgent repository."
    goto :eof
)
if not exist "%REPO_ROOT%\src\openagent" (
    call :die "locate-repo" "no src\openagent in %REPO_ROOT%" "This does not look like the OpenAgent repository."
    goto :eof
)
set "OA_VERSION_SOURCE=%REPO_ROOT%\src\openagent\__init__.py"
set "EXPECTED_VERSION="
for /f "usebackq tokens=1,2,3" %%a in ("%OA_VERSION_SOURCE%") do if "%%a"=="__version__" if "%%b"=="=" if not defined EXPECTED_VERSION set "EXPECTED_VERSION=%%~c"
if not defined EXPECTED_VERSION (
    call :die "locate-repo" "could not read the source OpenAgent version" "Restore src\openagent\__init__.py from the official repository."
    goto :eof
)
echo [openagent-setup] Installing OpenAgent from: %REPO_ROOT%

rem Note any pre-existing openagent so a shadowed command is never a silent surprise (§4).
for /f "delims=" %%p in ('where openagent 2^>nul') do (
    echo [openagent-setup] note: an 'openagent' command is already on PATH at %%p
)
where git >nul 2>&1 || echo [openagent-setup] warning: Git is not installed; git-worktree runs will fall back to isolated copies
where docker >nul 2>&1 || where podman >nul 2>&1 || echo [openagent-setup] note: Docker/Podman is absent; optional container-sandbox runs will be unavailable

rem --------------------------------------------------------------------- 1. uv
echo [openagent-setup] [1/6] Locating uv
set "UV="
for /f "delims=" %%i in ('where uv 2^>nul') do if not defined UV set "UV=%%i"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV=%USERPROFILE%\.cargo\bin\uv.exe"

if not defined UV (
    echo [openagent-setup]       uv not found - installing uv %UV_VERSION% ^(Astral standalone installer over HTTPS^)
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { irm https://astral.sh/uv/%UV_VERSION%/install.ps1 | iex } catch { Write-Error $_; exit 1 }"
    if errorlevel 1 (
        call :die "install-uv" "the uv installer failed" "Check your network / proxy, or install uv manually: https://docs.astral.sh/uv/"
        goto :eof
    )
    if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
    if not defined UV if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV=%USERPROFILE%\.cargo\bin\uv.exe"
    if not defined UV for /f "delims=" %%i in ('where uv 2^>nul') do if not defined UV set "UV=%%i"
)
if not defined UV (
    call :die "install-uv" "uv was installed but uv.exe could not be located" "Open a new terminal and re-run setup.bat."
    goto :eof
)
echo [openagent-setup]       using uv: !UV!

rem --------------------------------------------------------------------- 2. managed Python
echo [openagent-setup] [2/6] Installing a managed Python 3.12 ^(isolated; system/Store Python untouched^)
"!UV!" python install 3.12
if errorlevel 1 (
    call :die "install-python" "uv could not install a managed Python 3.12" "Check your network / proxy."
    goto :eof
)

rem --------------------------------------------------------------------- 3. OpenAgent tool
rem By default install an exact official commit from the remote, so provenance is a pinned VCS
rem commit and `openagent update` is checkout-independent afterwards. The commit must be on the
rem channel being installed (spec §3.5): stable = a published non-prerelease tag, candidate = the
rem release-candidate branch, dev = main. A feature-branch commit is not a production install
rem source — set OPENAGENT_SETUP_LOCAL=1 to install this working tree for development instead.
set "OFFICIAL_REMOTE=https://github.com/yasirkaramandev/openagent.git"
set "INSTALL_CHANNEL=stable"
echo %EXPECTED_VERSION% | findstr /R "rc a[0-9] b[0-9] dev" >nul && set "INSTALL_CHANNEL=candidate"
if defined OPENAGENT_SETUP_CHANNEL (
    if /I "%OPENAGENT_SETUP_CHANNEL%"=="stable" ( set "INSTALL_CHANNEL=stable"
    ) else if /I "%OPENAGENT_SETUP_CHANNEL%"=="candidate" ( set "INSTALL_CHANNEL=candidate"
    ) else if /I "%OPENAGENT_SETUP_CHANNEL%"=="dev" ( set "INSTALL_CHANNEL=dev"
    ) else (
        call :die "install-openagent" "unknown OPENAGENT_SETUP_CHANNEL '%OPENAGENT_SETUP_CHANNEL%'" "Choose stable, candidate, or dev."
        goto :eof
    )
)

set "INSTALL_COMMIT="
set "CHANNEL_SOURCE_REF="
if "%OPENAGENT_SETUP_LOCAL%"=="1" (
    echo [openagent-setup] [3/6] Installing OpenAgent from this checkout ^(local-development mode^)
    set "INSTALL_SOURCE=%REPO_ROOT%"
    set "INSTALL_SOURCE_KIND=dev-local"
    for /f "delims=" %%i in ('git -C "%REPO_ROOT%" rev-parse HEAD 2^>nul') do set "INSTALL_COMMIT=%%i"
) else (
    where git >nul 2>&1 || (
        call :die "install-openagent" "git is required for an official install" "Install git, or set OPENAGENT_SETUP_LOCAL=1 to install from this checkout."
        goto :eof
    )
    for /f "delims=" %%i in ('git -C "%REPO_ROOT%" rev-parse HEAD 2^>nul') do set "INSTALL_COMMIT=%%i"
    if not defined INSTALL_COMMIT (
        call :die "install-openagent" "this directory is not a git checkout" "Set OPENAGENT_SETUP_LOCAL=1 to install from this directory instead."
        goto :eof
    )
    if /I "!INSTALL_CHANNEL!"=="stable" (
        set "TAG_MATCH="
        for /f "tokens=1,2" %%a in ('git ls-remote --tags "%OFFICIAL_REMOTE%" 2^>nul') do (
            if /I "%%a"=="!INSTALL_COMMIT!" if not defined TAG_MATCH (
                set "CAND=%%b"
                set "CAND=!CAND:refs/tags/=!"
                set "CAND=!CAND:^{}=!"
                echo !CAND! | findstr /R "rc a[0-9] b[0-9] dev" >nul || set "TAG_MATCH=!CAND!"
            )
        )
        if not defined TAG_MATCH (
            call :die "install-openagent" "commit !INSTALL_COMMIT! is not a published stable release of the official repository" "Check out a release tag, use OPENAGENT_SETUP_CHANNEL=candidate or dev, or set OPENAGENT_SETUP_LOCAL=1 for a local install."
            goto :eof
        )
        set "CHANNEL_SOURCE_REF=refs/tags/!TAG_MATCH!"
    ) else (
        if /I "!INSTALL_CHANNEL!"=="candidate" ( set "CHANNEL_SOURCE_REF=refs/heads/release-candidate" ) else ( set "CHANNEL_SOURCE_REF=refs/heads/main" )
        git -C "%REPO_ROOT%" fetch -q "%OFFICIAL_REMOTE%" "!CHANNEL_SOURCE_REF!" 2>nul
        if errorlevel 1 (
            call :die "install-openagent" "could not read !CHANNEL_SOURCE_REF! from the official repository" "Check your network / proxy, then re-run."
            goto :eof
        )
        set "CHANNEL_TIP="
        for /f "delims=" %%i in ('git -C "%REPO_ROOT%" rev-parse FETCH_HEAD 2^>nul') do set "CHANNEL_TIP=%%i"
        if not defined CHANNEL_TIP (
            call :die "install-openagent" "could not resolve the tip of !CHANNEL_SOURCE_REF!" "Re-run setup.bat."
            goto :eof
        )
        git -C "%REPO_ROOT%" merge-base --is-ancestor "!INSTALL_COMMIT!" "!CHANNEL_TIP!" 2>nul
        if errorlevel 1 (
            call :die "install-openagent" "commit !INSTALL_COMMIT! is not on the !INSTALL_CHANNEL! channel ^(!CHANNEL_SOURCE_REF!^)" "Feature-branch commits are not an install source. Check out a channel commit, or set OPENAGENT_SETUP_LOCAL=1 for a local install."
            goto :eof
        )
    )
    echo [openagent-setup] [3/6] Installing OpenAgent from official commit !INSTALL_COMMIT! ^(!INSTALL_CHANNEL! channel, !CHANNEL_SOURCE_REF!^)
    set "INSTALL_SOURCE=git+%OFFICIAL_REMOTE%@!INSTALL_COMMIT!"
    set "INSTALL_SOURCE_KIND=official-github-vcs"
)

"!UV!" tool install --force --python 3.12 "!INSTALL_SOURCE!"
if errorlevel 1 (
    call :die "install-openagent" "uv tool install failed for !INSTALL_SOURCE!" "Re-run setup.bat; check the output above for the failing dependency."
    goto :eof
)

set "TOOL_BIN="
for /f "delims=" %%i in ('"!UV!" tool dir --bin 2^>nul') do if not defined TOOL_BIN set "TOOL_BIN=%%i"
if not defined TOOL_BIN (
    call :die "install-openagent" "could not read uv's tool bin directory" "Re-run setup.bat."
    goto :eof
)

set "OPENAGENT_BIN="
if exist "%TOOL_BIN%\openagent.exe" set "OPENAGENT_BIN=%TOOL_BIN%\openagent.exe"
if not defined OPENAGENT_BIN if exist "%TOOL_BIN%\openagent.cmd" set "OPENAGENT_BIN=%TOOL_BIN%\openagent.cmd"
if not defined OPENAGENT_BIN if exist "%TOOL_BIN%\openagent.bat" set "OPENAGENT_BIN=%TOOL_BIN%\openagent.bat"
if not defined OPENAGENT_BIN if exist "%TOOL_BIN%\openagent" set "OPENAGENT_BIN=%TOOL_BIN%\openagent"
if not defined OPENAGENT_BIN (
    call :die "install-openagent" "openagent executable missing in %TOOL_BIN% after install" "Re-run setup.bat."
    goto :eof
)

rem Persist install provenance so `openagent update` is channel-aware from the first run (spec §8,
rem §20.1). Written at %OPENAGENT_HOME%\install.json; never contains a secret. Non-fatal on failure,
rem but reported — an install that cannot record its channel will have to be repaired later.
set "OA_HOME=%OPENAGENT_HOME%"
if not defined OA_HOME set "OA_HOME=%USERPROFILE%\.openagent"
set "OA_CHANNEL_REF=null"
if /I "!INSTALL_CHANNEL!"=="candidate" set "OA_CHANNEL_REF=\"release-candidate\""
if /I "!INSTALL_CHANNEL!"=="dev" set "OA_CHANNEL_REF=\"main\""
set "OA_COMMIT_JSON=null"
if defined INSTALL_COMMIT set "OA_COMMIT_JSON=\"!INSTALL_COMMIT!\""
if not exist "!OA_HOME!" mkdir "!OA_HOME!" 2>nul
(
    echo {
    echo   "schema_version": 1,
    echo   "manager": "uv-tool",
    echo   "source": "!INSTALL_SOURCE_KIND!",
    echo   "repository": "yasirkaramandev/openagent",
    echo   "channel": "!INSTALL_CHANNEL!",
    echo   "channel_ref": !OA_CHANNEL_REF!,
    echo   "installed_version": "!EXPECTED_VERSION!",
    echo   "installed_commit": !OA_COMMIT_JSON!,
    echo   "last_accepted_version": "!EXPECTED_VERSION!",
    echo   "last_accepted_commit": !OA_COMMIT_JSON!,
    echo   "python": "3.12"
    echo }
) > "!OA_HOME!\install.json" 2>nul ^
    && echo [openagent-setup]       recorded install provenance in !OA_HOME!\install.json ^
    || echo [openagent-setup] WARN  could not record install provenance ^(non-fatal^)

rem --------------------------------------------------------------------- 4. PATH (user scope)
echo [openagent-setup] [4/6] Adding the 'openagent' command to your user PATH
rem uv's own mechanism for future shells...
"!UV!" tool update-shell >nul 2>&1
rem ...plus an idempotent, case-insensitive user-PATH update via the .NET API. NOT setx: setx
rem truncates long PATH values and can corrupt them. This never needs administrator rights and
rem never touches the system PATH. The PowerShell script wraps the write in try/catch and exits
rem non-zero on failure, and the exit code is REQUIRED to be checked (§7.1): a PATH write that fails
rem must fail the install, not be silently ignored.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $d='%TOOL_BIN%'; $p=[Environment]::GetEnvironmentVariable('Path','User'); if($null -eq $p){$p=''}; $parts=@($p -split ';' | Where-Object { $_ -and -not [string]::Equals($_.TrimEnd('\'),$d.TrimEnd('\'),[StringComparison]::OrdinalIgnoreCase) }); $np=(@($d)+$parts) -join ';'; [Environment]::SetEnvironmentVariable('Path',$np,'User'); Write-Host '[openagent-setup]       tool directory prepended exactly once on user PATH' } catch { Write-Error $_; exit 1 }"
if errorlevel 1 (
    call :die "path" "failed to write the user PATH via PowerShell" "Check PowerShell execution policy / registry access, then re-run setup.bat."
    goto :eof
)

rem Independently re-read the persisted User PATH from the registry and PROVE the tool dir is there
rem (§7.2). Without this, a failed/partial registry write would go unnoticed until a fresh shell.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$toolBin='%TOOL_BIN%'; $userPath=[Environment]::GetEnvironmentVariable('Path','User'); $parts=@($userPath -split ';' | Where-Object { $_ }); if(-not ($parts | Where-Object { [string]::Equals($_.TrimEnd('\'), $toolBin.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase) })){ Write-Error 'OpenAgent tool directory was not persisted to the user PATH'; exit 1 }; Write-Host '[openagent-setup]       verified on persisted user PATH'"
if errorlevel 1 (
    call :die "path" "the tool directory was not persisted to the user PATH" "Re-run setup.bat; if it persists, add %TOOL_BIN% to your user PATH manually."
    goto :eof
)

rem Make it work in THIS session too.
set "PATH=%TOOL_BIN%;%PATH%"

rem --------------------------------------------------------------------- 5. verify
echo [openagent-setup] [5/6] Verifying installation
set "VERSION_FILE=%TEMP%\openagent-version-%RANDOM%-%RANDOM%.txt"
"%OPENAGENT_BIN%" version >"!VERSION_FILE!" 2>nul
if errorlevel 1 (
    del /q "!VERSION_FILE!" >nul 2>&1
    call :die "verify" "openagent could not run - the entrypoint or an import is broken" "This is a real install failure; re-run setup.bat and report the output."
    goto :eof
)
set "INSTALLED_VERSION="
set /p INSTALLED_VERSION=<"!VERSION_FILE!"
del /q "!VERSION_FILE!" >nul 2>&1
if not "!INSTALLED_VERSION!"=="openagent !EXPECTED_VERSION!" (
    call :die "verify-version" "installed binary reported '!INSTALLED_VERSION!'; expected 'openagent !EXPECTED_VERSION!'" "The uv tool environment did not receive this checkout; re-run setup.bat."
    goto :eof
)
echo [openagent-setup]       version: !EXPECTED_VERSION!

set "OA_OPENAGENT_BIN=%OPENAGENT_BIN%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$expected=[IO.Path]::GetFullPath($env:OA_OPENAGENT_BIN).TrimEnd('\'); $c=Get-Command openagent -ErrorAction SilentlyContinue; if($null -eq $c -or -not [string]::Equals([IO.Path]::GetFullPath($c.Source).TrimEnd('\'),$expected,[StringComparison]::OrdinalIgnoreCase)){ Write-Error 'PATH resolves a different OpenAgent executable'; exit 1 }; Write-Host ('[openagent-setup]       PATH: '+$c.Source)"
if errorlevel 1 (
    call :die "verify-path" "PATH does not resolve the OpenAgent binary just installed" "Remove or move the shadowing OpenAgent binary, then re-run setup.bat."
    goto :eof
)

set "DOCTOR_JSON=%TEMP%\openagent-doctor-%RANDOM%-%RANDOM%.json"
"%OPENAGENT_BIN%" doctor --json >"!DOCTOR_JSON!" 2>nul
set "DOCTOR_EXIT=!ERRORLEVEL!"
set "OA_DOCTOR_JSON=!DOCTOR_JSON!"
set "OA_DOCTOR_EXIT=!DOCTOR_EXIT!"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $d=Get-Content -LiteralPath $env:OA_DOCTOR_JSON -Raw | ConvertFrom-Json; if($null -eq $d.exit_code -or [int]$d.exit_code -ne [int]$env:OA_DOCTOR_EXIT){ throw 'doctor JSON exit_code does not match process exit' }; foreach($c in @($d.checks)){ if($null -ne $c.data -and $c.data.backup_path){ Write-Host ('[openagent-setup]       database backup: '+[string]$c.data.backup_path); break } } } catch { Write-Error $_; exit 1 }"
if errorlevel 1 (
    del /q "!DOCTOR_JSON!" >nul 2>&1
    call :die "verify-doctor" "doctor did not emit valid JSON matching exit code !DOCTOR_EXIT!" "Run '%OPENAGENT_BIN% doctor --json' and inspect the database diagnostic."
    goto :eof
)
del /q "!DOCTOR_JSON!" >nul 2>&1
if "!DOCTOR_EXIT!"=="0" echo [openagent-setup]       doctor: ok
if "!DOCTOR_EXIT!"=="1" echo [openagent-setup]       doctor: warnings only ^(optional CLI/tool readiness may be incomplete^)
if "!DOCTOR_EXIT!"=="2" (
    call :die "verify-database" "Doctor found an incompatible, corrupt, or invalid OpenAgent database" "Preserve the backup shown above and run '%OPENAGENT_BIN% doctor --json'."
    goto :eof
)
if "!DOCTOR_EXIT!"=="3" (
    call :die "verify-migration" "Doctor reports a failed or interrupted database migration" "Preserve the backup shown above; TUI launch has been blocked."
    goto :eof
)
if not "!DOCTOR_EXIT!"=="0" if not "!DOCTOR_EXIT!"=="1" if not "!DOCTOR_EXIT!"=="2" if not "!DOCTOR_EXIT!"=="3" (
    call :die "verify-doctor" "Doctor exited with unsupported code !DOCTOR_EXIT!" "Run '%OPENAGENT_BIN% doctor --json'."
    goto :eof
)
rem Prove a *fresh* shell finds openagent by name using the PERSISTED PATH (§7.3). This must NOT
rem inject %TOOL_BIN% manually — that would pass even if the registry write had failed. Instead we
rem reconstruct the environment a brand-new login shell gets (System PATH + User PATH, read straight
rem from the registry) and run `openagent version` in a fresh CMD *and* a fresh PowerShell with it.
set "OA_EXPECTED_VERSION=%EXPECTED_VERSION%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$m=[Environment]::GetEnvironmentVariable('Path','Machine'); $u=[Environment]::GetEnvironmentVariable('Path','User'); $env:Path=(@($m,$u) | Where-Object { $_ }) -join ';'; $expected=[IO.Path]::GetFullPath($env:OA_OPENAGENT_BIN).TrimEnd('\'); $c=Get-Command openagent -ErrorAction SilentlyContinue; if($null -eq $c -or -not [string]::Equals([IO.Path]::GetFullPath($c.Source).TrimEnd('\'),$expected,[StringComparison]::OrdinalIgnoreCase)){ Write-Error 'fresh PowerShell PATH resolves a different openagent'; exit 1 }; $pv=(& openagent version | Out-String).Trim(); if($LASTEXITCODE -ne 0 -or $pv -cne ('openagent '+$env:OA_EXPECTED_VERSION)){ Write-Error 'fresh PowerShell ran a different OpenAgent version'; exit 1 }; $found=@(& cmd /d /c 'where openagent'); $whereExit=$LASTEXITCODE; $first=[string]($found | Select-Object -First 1); if($whereExit -ne 0 -or -not $first -or -not [string]::Equals([IO.Path]::GetFullPath($first.Trim()).TrimEnd('\'),$expected,[StringComparison]::OrdinalIgnoreCase)){ Write-Error ('fresh CMD PATH resolved '+$first.Trim()+' instead of '+$expected); exit 1 }; $cv=(& cmd /d /c 'openagent version' | Out-String).Trim(); if($LASTEXITCODE -ne 0 -or $cv -cne ('openagent '+$env:OA_EXPECTED_VERSION)){ Write-Error 'fresh CMD ran a different OpenAgent version'; exit 1 }"
if errorlevel 1 (
    call :die "verify" "openagent is not runnable by name from a fresh shell using the persisted PATH" "Open a new terminal; if it persists, re-run setup.bat."
    goto :eof
)

rem --------------------------------------------------------------------- 6. launch
if "%OPENAGENT_SETUP_NO_LAUNCH%"=="1" (
    echo [openagent-setup] [6/6] OPENAGENT_SETUP_NO_LAUNCH=1 set - skipping TUI launch. Install verified.
    echo [openagent-setup] Done. Open a new terminal and run: openagent
    endlocal
    exit /b 0
)
echo [openagent-setup] [6/6] Starting OpenAgent... ^(a new terminal will let you run 'openagent' directly^)
"%OPENAGENT_BIN%"
endlocal
exit /b 0

rem --------------------------------------------------------------------- helpers
:die
echo.
echo [openagent-setup] ERROR
echo [openagent-setup]   stage : %~1
echo [openagent-setup]   what  : %~2
echo [openagent-setup]   fix   : %~3
endlocal
exit /b 1
