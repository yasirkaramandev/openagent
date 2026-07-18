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
for /f "delims=" %%v in ('powershell -NoProfile -Command "$t=Get-Content -LiteralPath $env:OA_VERSION_SOURCE -Raw; $m=[regex]::Match($t,'(?m)^__version__\s*=\s*[\"'']([^\"'']+)[\"'']\s*$'); if(-not $m.Success){exit 1}; $m.Groups[1].Value"') do if not defined EXPECTED_VERSION set "EXPECTED_VERSION=%%v"
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
echo [openagent-setup] [3/6] Installing OpenAgent ^(runtime deps only - no dev tools; your data is preserved^)
"!UV!" tool install --force --python 3.12 "%REPO_ROOT%"
if errorlevel 1 (
    call :die "install-openagent" "uv tool install failed for %REPO_ROOT%" "Re-run setup.bat; check the output above for the failing dependency."
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
powershell -NoProfile -ExecutionPolicy Bypass -Command "$m=[Environment]::GetEnvironmentVariable('Path','Machine'); $u=[Environment]::GetEnvironmentVariable('Path','User'); $env:Path=(@($m,$u) | Where-Object { $_ }) -join ';'; $expected=[IO.Path]::GetFullPath($env:OA_OPENAGENT_BIN).TrimEnd('\'); $c=Get-Command openagent -ErrorAction SilentlyContinue; if($null -eq $c -or -not [string]::Equals([IO.Path]::GetFullPath($c.Source).TrimEnd('\'),$expected,[StringComparison]::OrdinalIgnoreCase)){ Write-Error 'fresh PowerShell PATH resolves a different openagent'; exit 1 }; $pv=(& openagent version | Out-String).Trim(); if($LASTEXITCODE -ne 0 -or $pv -cne ('openagent '+$env:OA_EXPECTED_VERSION)){ Write-Error 'fresh PowerShell ran a different OpenAgent version'; exit 1 }; $first=(& cmd /d /c 'where openagent' | Select-Object -First 1).Trim(); if($LASTEXITCODE -ne 0 -or -not [string]::Equals([IO.Path]::GetFullPath($first).TrimEnd('\'),$expected,[StringComparison]::OrdinalIgnoreCase)){ Write-Error 'fresh CMD PATH resolves a different openagent'; exit 1 }; $cv=(& cmd /d /c 'openagent version' | Out-String).Trim(); if($LASTEXITCODE -ne 0 -or $cv -cne ('openagent '+$env:OA_EXPECTED_VERSION)){ Write-Error 'fresh CMD ran a different OpenAgent version'; exit 1 }"
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
